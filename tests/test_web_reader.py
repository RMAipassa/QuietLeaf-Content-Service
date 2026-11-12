from concurrent.futures import ThreadPoolExecutor

import pytest
from io import BytesIO
from zipfile import ZipFile
from PIL import Image
from fastapi.testclient import TestClient

from quietleaf_service.app import make_app
from quietleaf_service.archive import ArchiveStore
from quietleaf_service.adapters import SourceError


TOKEN = "web-reader-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


class Source:
    id = "fixture"
    name = "Must not fetch upstream"

    def details(self, *_):
        pytest.fail("The web reader must not fetch book metadata upstream")

    def chapters(self, *_):
        pytest.fail("The web reader must not fetch a chapter list upstream")

    def text(self, *_):
        pytest.fail("The web reader must not fetch missing chapters upstream")


def seed(store):
    store.save_book("fixture", {"id": "book", "title": "Original saved novel", "author": "Writer"})
    store.save_chapters("fixture", "book", [
        {"id": "one", "title": "First chapter", "ordinal": 0},
        {"id": "two", "title": "Second chapter", "ordinal": 1},
        {"id": "missing", "title": "Not saved", "ordinal": 2},
    ])
    store.save_text("fixture", "book", "one", '<script>not_html()</script>\n\nOriginal saved story.')
    store.save_text("fixture", "book", "two", "Second original story.")


def update(chapter="one", fraction=0.25, revision=0, client="android-client", event="android-event-1"):
    return {"chapterId": chapter, "fraction": fraction, "baseRevision": revision, "clientId": client, "eventId": event}


def test_reader_shell_is_public_but_content_is_authenticated_and_archive_only(tmp_path):
    store = ArchiveStore(tmp_path)
    seed(store)
    with TestClient(make_app([Source()], TOKEN, tmp_path)) as api:
        shell = api.get("/reader?source=fixture&book=book")
        assert shell.status_code == 200 and "QuietLeaf Web Reader" in shell.text
        assert "Original saved novel" not in shell.text and TOKEN not in shell.text
        assert "default-src 'none'" in shell.headers["content-security-policy"]
        assert shell.headers["cache-control"] == "no-store"
        for asset in ("reader.js", "reader.css", "progress.js"):
            assert api.get("/admin/assets/" + asset).status_code == 200
        assert api.get("/v1/reader/books").status_code == 401
        book = api.get("/v1/reader/books/fixture/book", headers=AUTH).json()
        assert book["book"]["title"] == "Original saved novel"
        assert [row["archived"] for row in book["chapters"]] == [True, True, False]
        chapter = api.get("/v1/reader/books/fixture/book/chapters/one", headers=AUTH)
        assert chapter.status_code == 200 and chapter.json()["text"].startswith("<script>")
        assert chapter.json()["origin"] == "archive" and chapter.json()["savedAt"]
        assert chapter.headers["cache-control"] == "private, no-store"
        raw = api.get("/v1/reader/books/fixture/book/chapters/one?plain=true&raw=true", headers=AUTH)
        assert raw.headers["content-type"].startswith("text/plain")
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert api.get("/v1/reader/books/fixture/book/chapters/missing", headers=AUTH).status_code == 404
        assert store.reading_position("fixture", "book") is None


def test_progress_round_trip_conflict_and_idempotent_lost_ack(tmp_path):
    store = ArchiveStore(tmp_path)
    seed(store)
    path = "/v1/reading-progress/fixture/book"
    with TestClient(make_app([Source()], TOKEN, tmp_path)) as api:
        assert api.get(path).status_code == 401
        assert api.get(path, headers=AUTH).json() == {"progress": None}
        first = api.put(path, headers=AUTH, json=update()).json()["progress"]
        assert first["revision"] == 1 and first["chapterId"] == "one" and first["fraction"] == 0.25
        # A retry after losing the HTTP acknowledgement returns the same revision.
        assert api.put(path, headers=AUTH, json=update()).json()["progress"]["revision"] == 1
        assert api.put(path, headers=AUTH, json=update("two", 0.4, 1)).status_code == 422
        web = update("two", 0.75, 1, "browser-client", "browser-event-1")
        assert api.put(path, headers=AUTH, json=web).json()["progress"]["revision"] == 2
        offline = update("one", 0.8, 1, "android-client", "android-event-2")
        rejected = api.put(path, headers=AUTH, json=offline)
        assert rejected.status_code == 409 and rejected.json()["progress"]["chapterId"] == "two"
        assert api.get(path, headers=AUTH).json()["progress"]["revision"] == 2
        # Old lost acknowledgements cannot rebase over a different device's newer state.
        assert api.put(path, headers=AUTH, json=update()).status_code == 409
        assert api.put(path, headers=AUTH, json=update("absent", 0, 2)).status_code == 422
        assert api.get("/v1/reading-progress", headers=AUTH).json()["total"] == 1
    restarted = ArchiveStore(tmp_path)
    assert restarted.reading_position("fixture", "book")["chapterId"] == "two"
    assert restarted.text("fixture", "book", "one").startswith("<script>")


def test_concurrent_writers_have_one_winner(tmp_path):
    store = ArchiveStore(tmp_path)
    seed(store)
    def write(client):
        return store.save_reading_position("fixture", "book", "one", 0.5, 0, client, client + "-event")[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ("android-client", "browser-client")))
    assert sorted(results) == [False, True]
    assert store.reading_position("fixture", "book")["revision"] == 1


@pytest.mark.parametrize("fraction", [-0.1, 1.1, float("nan"), float("inf"), True])
def test_invalid_progress_does_not_write(tmp_path, fraction):
    store = ArchiveStore(tmp_path)
    with pytest.raises(SourceError):
        store.save_reading_position("fixture", "book", "one", fraction, 0, "android-client", "android-event")
    assert store.reading_position("fixture", "book") is None


def test_private_uploaded_text_can_be_read_without_upstream_access(tmp_path):
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        body = b"An original uploaded text story.\n\nA second paragraph."
        uploaded = api.post("/v1/private/books?filename=original.txt", headers=AUTH, content=body)
        assert uploaded.status_code == 201
        book_id = uploaded.json()["book"]["id"]
        book = api.get(f"/v1/reader/books/private/{book_id}", headers=AUTH).json()
        assert book["chapters"][0]["archived"] is True
        result = api.get(f"/v1/reader/books/private/{book_id}/chapters/chapter-1", headers=AUTH).json()
        assert result["text"] == body.decode() and result["origin"] == "private-library"


def test_admin_session_can_sync_and_confirmation_is_required_for_writes(tmp_path):
    store = ArchiveStore(tmp_path)
    seed(store)
    with TestClient(make_app([Source()], TOKEN, tmp_path), base_url="https://testserver") as api:
        assert api.post("/admin/login", json={"password": TOKEN}).status_code == 200
        path = "/v1/reading-progress/fixture/book"
        assert api.put(path, json=update()).status_code == 403
        assert api.put(path, headers={"X-QuietLeaf-Admin": "1"}, json=update()).status_code == 200


def test_uploaded_page_volumes_have_a_reader_and_shared_page_position(tmp_path):
    image = BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    volume = BytesIO()
    with ZipFile(volume, "w") as archive:
        archive.writestr("1.png", image.getvalue())
        archive.writestr("2.png", image.getvalue())
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        uploaded = api.post("/v1/private/books?filename=original.cbz", headers=AUTH, content=volume.getvalue())
        assert uploaded.status_code == 201
        book_id = uploaded.json()["book"]["id"]
        document = api.get(f"/v1/reader/books/private/{book_id}/chapters/document", headers=AUTH).json()
        assert document["mediaType"] == "cbz" and document["pageCount"] == 2
        assert "text" not in document
        assert api.get(f"/v1/private/books/{book_id}/pages/0", headers=AUTH).headers["content-type"] == "image/jpeg"
        position = update("document", 1.0)
        result = api.put(f"/v1/reading-progress/private/{book_id}", headers=AUTH, json=position)
        assert result.status_code == 200 and result.json()["progress"]["fraction"] == 1.0
