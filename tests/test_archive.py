import time

from fastapi.testclient import TestClient

from quietleaf_service.app import make_app
from quietleaf_service.archive import ArchiveStore


class ExampleSource:
    id = "fixture"
    name = "Original test story"

    def __init__(self):
        self.offline = False
        self.fail_chapter = ""
        self.calls = []
        self.chapter_total = 3

    def _check(self):
        if self.offline:
            raise ConnectionError("Source offline")

    def search(self, query, genre=""):
        self._check()
        return [self.details("test-story")]

    def recent(self):
        return self.search("")

    def details(self, book_id):
        self._check()
        return {"id": book_id, "title": "A Completely Original Test Story",
                "author": "QuietLeaf Team", "description": "Archive fixture.", "genre": "Fiction", "coverUrl": ""}

    def chapters(self, book_id):
        self._check()
        return [{"id": f"chapter-{i}", "title": f"Chapter {i}", "ordinal": i,
                 "downloadable": True} for i in range(1, self.chapter_total + 1)]

    def text(self, book_id, chapter_id):
        self._check()
        self.calls.append(chapter_id)
        if chapter_id == self.fail_chapter:
            raise ConnectionError("Temporary failure")
        return f"Original test content for {chapter_id}."


def authorized():
    return {"Authorization": "Bearer private-key"}


def finished(api, job_id, state="completed"):
    stop_at = time.monotonic() + 3
    while time.monotonic() < stop_at:
        job = api.get(f"/v1/archive/jobs/{job_id}", headers=authorized()).json()
        if job["status"] == state:
            return job
        time.sleep(0.02)
    assert False, f"Archive job never reached {state}: {job}"


def test_archive_remains_readable_after_source_goes_offline_and_adapter_removed(tmp_path):
    source = ExampleSource()
    with TestClient(make_app([source], "private-key", tmp_path)) as api:
        assert api.get("/v1/archive/books").status_code == 401
        started = api.post("/v1/archive/fixture/test-story", headers=authorized())
        assert started.status_code == 202
        job = finished(api, started.json()["id"])
        assert (job["total"], job["downloaded"]) == (3, 3)
        stats = api.get("/v1/archive/books/fixture/test-story", headers=authorized()).json()
        assert stats["chapterCount"] == 3
        assert stats["downloadedChapters"] == 3
        assert stats["storageBytes"] > 10
        source.offline = True
        assert api.get("/v1/books/fixture/test-story", headers=authorized()).json()["title"].startswith("A Completely")
        assert len(api.get("/v1/books/fixture/test-story/chapters", headers=authorized()).json()["items"]) == 3
        assert "Original test content" in api.get(
            "/v1/books/fixture/test-story/chapters/chapter-2/text", headers=authorized()).text
        assert any(b["id"] == "test-story" for b in api.get(
            "/v1/search?q=Original", headers=authorized()).json()["items"])
    with TestClient(make_app([], "private-key", tmp_path)) as api:
        assert api.get("/v1/books/fixture/test-story", headers=authorized()).status_code == 200
        assert "Original test content" in api.get(
            "/v1/books/fixture/test-story/chapters/chapter-3/text", headers=authorized()).text
        assert len(api.get("/v1/search?q=Original", headers=authorized()).json()["items"]) == 1
        assert len(api.get("/v1/recent", headers=authorized()).json()["items"]) == 1


def test_retry_skips_already_archived_chapters(tmp_path):
    source = ExampleSource()
    source.fail_chapter = "chapter-2"
    with TestClient(make_app([source], "private-key", tmp_path)) as api:
        first = api.post("/v1/archive/fixture/test-story", headers=authorized()).json()
        failed = finished(api, first["id"], "failed")
        assert (failed["total"], failed["downloaded"]) == (3, 1)
        source.fail_chapter = ""
        retry = api.post("/v1/archive/fixture/test-story", headers=authorized()).json()
        finished(api, retry["id"])
        assert source.calls.count("chapter-1") == 1
        assert source.calls.count("chapter-2") == 2
        assert source.calls.count("chapter-3") == 1
        again = api.post("/v1/archive/fixture/test-story", headers=authorized()).json()
        finished(api, again["id"])
        assert len(source.calls) == 4


def test_queued_job_recovers_from_restart(tmp_path):
    store = ArchiveStore(tmp_path)
    job = store.enqueue("fixture", "test-story")
    store.update_job(job["id"], status="running")
    with TestClient(make_app([ExampleSource()], "private-key", tmp_path)) as api:
        finished(api, job["id"])
        assert store.stats("fixture", "test-story")["downloadedChapters"] == 3


def test_deletion_and_storage_limit(tmp_path):
    source = ExampleSource()
    with TestClient(make_app([source], "private-key", tmp_path, archive_max_bytes=10)) as api:
        job = api.post("/v1/archive/fixture/test-story", headers=authorized()).json()
        failed = finished(api, job["id"], "failed")
        assert "storage limit" in failed["error"]
        assert api.get("/v1/archive/books/fixture/test-story", headers=authorized()).json()["downloadedChapters"] == 0
        deleted = api.delete("/v1/archive/books/fixture/test-story", headers=authorized())
        assert deleted.json()["deleted"] is True
        assert api.get("/v1/archive/books/fixture/test-story", headers=authorized()).status_code == 404


def test_archive_writes_require_token(tmp_path):
    with TestClient(make_app([ExampleSource()], "", tmp_path)) as api:
        assert api.post("/v1/archive/fixture/test-story").status_code == 403
        assert api.get("/v1/archive/books").status_code == 403


def test_experimental_novelping_adapter_can_feed_the_archive_using_fixture_html(tmp_path):
    import httpx
    from quietleaf_service.novelping import NovelPingSource

    original_html = "<div class='chapter-content'><p>" + ("A wholly original archive test. " * 5) + "</p></div>"

    def mock_site(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/book/fixture-book":
            return httpx.Response(200, text="""
                <h1>Fixture Book</h1>
                <a href='/book/fixture-book/chapter-2-middle'>Second</a>
                <a href='/book/fixture-book/chapter-1-opening'>First</a>
            """)
        if request.url.path.startswith("/book/fixture-book/chapter-"):
            return httpx.Response(200, text=original_html)
        return httpx.Response(404)

    source = NovelPingSource(client=httpx.Client(transport=httpx.MockTransport(mock_site)))
    source.min_interval = 0
    with TestClient(make_app([source], "private-key", tmp_path)) as api:
        response = api.post("/v1/archive/novelping/fixture-book", headers=authorized())
        assert response.status_code == 202
        finished(api, response.json()["id"])
        stats = api.get("/v1/archive/books/novelping/fixture-book", headers=authorized()).json()
        assert stats["downloadedChapters"] == 2
        chapters = api.get("/v1/books/novelping/fixture-book/chapters", headers=authorized()).json()["items"]
        assert [row["id"] for row in chapters] == ["chapter-1-opening", "chapter-2-middle"]
        text = api.get("/v1/books/novelping/fixture-book/chapters/chapter-1-opening/text",
                       headers=authorized()).text
        assert "wholly original archive test" in text


def test_batch_archiving_follows_without_phone_download(tmp_path):
    source = ExampleSource()
    with TestClient(make_app([source], "private-key", tmp_path)) as api:
        payload = {"items": [
            {"source": "fixture", "bookId": "test-story"},
            {"source": "fixture", "bookId": "test-story"},
        ]}
        r = api.post("/v1/archive/batch", headers=authorized(), json=payload)
        assert r.status_code == 202
        assert r.json()["queued"] == 1
        assert r.json()["phoneDownload"] is False
        finished(api, r.json()["items"][0]["id"])
        follow = api.get("/v1/archive/follows", headers=authorized()).json()["items"]
        assert len(follow) == 1
        assert follow[0]["enabled"] == 1
        assert api.get("/v1/archive/books/fixture/test-story", headers=authorized()).json()["downloadedChapters"] == 3


def test_batch_validation_is_atomic_and_secured(tmp_path):
    with TestClient(make_app([ExampleSource()], "private-key", tmp_path)) as api:
        data = {"items": [
            {"source": "fixture", "bookId": "test-story"},
            {"source": "nonexistent", "bookId": "test-story"},
        ]}
        assert api.post("/v1/archive/batch", json=data).status_code == 401
        assert api.post("/v1/archive/batch", headers=authorized(), json=data).status_code == 404
        assert api.get("/v1/archive/jobs", headers=authorized()).json()["items"] == []
        assert api.post("/v1/archive/batch", headers=authorized(), json={"items": []}).status_code == 422
        assert api.post("/v1/archive/batch", headers=authorized(), json={"items": [{"source":"fixture", "bookId":"valid"}] * 51}).status_code == 422


def test_follow_cadence_activity_and_restart(tmp_path):
    store = ArchiveStore(tmp_path, poll_seconds=3600, active_poll_seconds=1800)
    store.follow("fixture", "test-story", now=100_000)
    assert store.reserve_due(now=103_599) == []
    assert store.reserve_due(now=103_600) == [("fixture", "test-story")]
    assert store.reserve_due(now=103_600) == []
    assert store.record_activity("fixture", "test-story", now=103_700)
    status = store.follow_status("fixture", "test-story")
    assert status["next_check_at"] == 105_500
    restarted = ArchiveStore(tmp_path, poll_seconds=3600, active_poll_seconds=1800)
    assert restarted.reserve_due(now=105_499) == []
    assert restarted.reserve_due(now=105_500) == [("fixture", "test-story")]
    assert restarted.follow_status("fixture", "test-story")["next_check_at"] == 107_300
    restarted.record_check("fixture", "test-story", "temporary outage", now=105_500)
    status = restarted.follow_status("fixture", "test-story")
    assert status["failure_count"] == 1
    assert status["next_check_at"] == 109_100
    restarted.record_check("fixture", "test-story", now=109_100)
    assert restarted.follow_status("fixture", "test-story")["failure_count"] == 0
    assert restarted.unfollow("fixture", "test-story")
    assert restarted.reserve_due(now=200_000) == []


def test_scheduler_archives_only_new_chapters_and_keeps_old_text(tmp_path):
    source = ExampleSource()
    app = make_app([source], "private-key", tmp_path)
    with TestClient(app) as api:
        first = api.post("/v1/archive/fixture/test-story", headers=authorized()).json()
        finished(api, first["id"])
        assert source.calls == ["chapter-1", "chapter-2", "chapter-3"]
        source.chapter_total = 4
        store = app.state.archive_store
        with store._connect() as db:
            db.execute("UPDATE follows SET next_check_at=? WHERE source=? AND book_id=?", (1, "fixture", "test-story"))
        app.state.archive_worker.wake()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if store.stats("fixture", "test-story")["downloadedChapters"] == 4:
                break
            time.sleep(0.02)
        assert store.stats("fixture", "test-story")["downloadedChapters"] == 4
        assert source.calls == ["chapter-1", "chapter-2", "chapter-3", "chapter-4"]
        assert api.get("/v1/books/fixture/test-story/chapters/chapter-4/text", headers=authorized()).text == "Original test content for chapter-4."
        source.offline = True
        assert api.get("/v1/books/fixture/test-story/chapters/chapter-4/text", headers=authorized()).status_code == 200


def test_unfollow_preserves_archive_and_read_activity_is_safe(tmp_path):
    with TestClient(make_app([ExampleSource()], "private-key", tmp_path)) as api:
        job = api.post("/v1/archive/fixture/test-story", headers=authorized()).json()
        finished(api, job["id"])
        assert api.post("/v1/archive/activity/fixture/test-story", headers=authorized()).json()["recorded"]
        result = api.delete("/v1/archive/follows/fixture/test-story", headers=authorized())
        assert result.json()["updated"] is True
        assert api.post("/v1/archive/activity/fixture/test-story", headers=authorized()).json()["recorded"] is False
        assert api.get("/v1/archive/books/fixture/test-story", headers=authorized()).json()["downloadedChapters"] == 3


def test_cover_storage_limit_and_deletion(tmp_path):
    import pytest
    from quietleaf_service.adapters import SourceError

    store = ArchiveStore(tmp_path, max_bytes=45)
    src = ExampleSource()
    store.save_book("fixture", src.details("test-story"))
    jpeg = b"\xff\xd8\xff" + b"picture-data"
    store.save_cover("fixture", "test-story", "image/jpeg", jpeg)
    assert store.cover("fixture", "test-story") == ("image/jpeg", jpeg)
    assert store.stats("fixture", "test-story")["coverStorageBytes"] == len(jpeg)
    store.save_chapters("fixture", "test-story", src.chapters("test-story"))
    with pytest.raises(SourceError, match="storage limit"):
        store.save_text("fixture", "test-story", "chapter-1", "X" * 40)
    with pytest.raises(SourceError, match="Invalid cover"):
        store.save_cover("fixture", "test-story", "image/jpeg", b"<script>alert(1)</script>")
    assert store.delete_book("fixture", "test-story")
    assert store.cover("fixture", "test-story") is None
