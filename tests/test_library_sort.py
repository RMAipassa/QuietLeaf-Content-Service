import sqlite3

from fastapi.testclient import TestClient

from quietleaf_service.app import make_app
from quietleaf_service.archive import ArchiveStore

AUTH = {"Authorization": "Bearer sorting-fixture-token"}


def test_added_timestamp_survives_metadata_refresh_and_legacy_migration(tmp_path, monkeypatch):
    store = ArchiveStore(tmp_path)
    monkeypatch.setattr("quietleaf_service.archive.time.time", lambda: 1000)
    store.save_book("fixture", {"id": "first", "title": "First"})
    monkeypatch.setattr("quietleaf_service.archive.time.time", lambda: 9000)
    store.save_book("fixture", {"id": "first", "title": "Refreshed metadata"})
    assert store.book("fixture", "first")["addedAt"] == 1000000
    store.follow("fixture", "first", now=500)
    # Simulate an existing installation without the new stable timestamp column.
    with sqlite3.connect(store.database) as db:
        db.execute("ALTER TABLE books DROP COLUMN created_at")
    restarted = ArchiveStore(tmp_path)
    assert restarted.book("fixture", "first")["addedAt"] == 500000
    restarted.save_book("fixture", {"id": "first", "title": "Another refresh"})
    assert restarted.book("fixture", "first")["addedAt"] == 500000


def test_archive_sorting_uses_real_reads_and_keeps_unread_books_last(tmp_path):
    store = ArchiveStore(tmp_path)
    for number, title in enumerate(("Zulu", "Alpha", "Unread"), 1):
        book_id = str(number)
        store.save_book("fixture", {"id": book_id, "title": title})
        store.follow("fixture", book_id)
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE books SET created_at=CAST(id AS INTEGER)*100")
    store.record_activity("fixture", "1", now=300)
    store.record_activity("fixture", "2", now=500)
    assert [b["id"] for b in store.list_books(sort="last_read_desc")] == ["2", "1", "3"]
    assert [b["id"] for b in store.list_books(sort="last_read_asc")] == ["1", "2", "3"]
    assert [b["id"] for b in store.list_books(sort="added_desc")] == ["3", "2", "1"]
    assert [b["id"] for b in store.list_books(sort="added_asc")] == ["1", "2", "3"]
    assert [b["id"] for b in store.list_books(sort="title")] == ["2", "3", "1"]
    assert store.list_books(sort="last_read_desc", limit=1, offset=1)[0]["lastReadAt"] == 300000


def test_reader_paginates_a_globally_sorted_archive_and_private_library(tmp_path):
    store = ArchiveStore(tmp_path)
    store.save_book("fixture", {"id": "archive", "title": "Archive"})
    store.save_chapters("fixture", "archive", [{"id": "one", "title": "One", "ordinal": 0}])
    store.save_text("fixture", "archive", "one", "Original saved prose")
    app = make_app([], "sorting-fixture-token", tmp_path)
    # No lifespan worker is needed: all requests inspect local saved data.
    with TestClient(app) as api:
        private = api.post("/v1/private/books?filename=sort.txt", headers=AUTH, content=b"Original private prose").json()["book"]
        private_id = private["id"]
        with sqlite3.connect(store.database) as db:
            db.execute("UPDATE books SET created_at=100")
        with sqlite3.connect(app.state.private_library.database) as db:
            db.execute("UPDATE books SET created_at=200")
        position = {"chapterId": "chapter-1", "fraction": 0.4, "baseRevision": 0,
                    "clientId": "browser-client", "eventId": "browser-event"}
        assert api.put(f"/v1/reading-progress/private/{private_id}", headers=AUTH, json=position).status_code == 200
        for sort, expected in (("added_desc", [private_id, "archive"]), ("added_asc", ["archive", private_id]),
                               ("last_read_desc", [private_id, "archive"]), ("last_read_asc", [private_id, "archive"])):
            pages = [api.get("/v1/reader/books", headers=AUTH, params={"sort": sort, "limit": 1, "offset": offset}).json() for offset in range(2)]
            assert [p["items"][0]["id"] for p in pages] == expected
            assert all(p["total"] == 2 for p in pages)
        item = api.get("/v1/private/books?sort=last_read_desc", headers=AUTH).json()["items"][0]
        assert item["lastReadAt"] > 0 and item["addedAt"] == 200000
        assert api.get("/v1/reader/books?q=private", headers=AUTH).json()["total"] == 0  # Title is sort.txt, not its prose.
        assert api.get("/v1/reader/books?q=Archive", headers=AUTH).json()["total"] == 1
        for endpoint in ("/v1/reader/books", "/v1/archive/books", "/v1/private/books"):
            assert api.get(endpoint + "?sort=invalid", headers=AUTH).status_code == 422
