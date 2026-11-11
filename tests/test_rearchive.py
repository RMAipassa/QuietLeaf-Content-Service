import sqlite3
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from quietleaf_service.adapters import SourceError
from quietleaf_service.app import make_app
from quietleaf_service.archive import ArchiveStore, ArchiveWorker
from quietleaf_service.novelping import NovelPingSource


TOKEN = "rearchive-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


class Source:
    id = "fixture"
    name = "Refresh fixture"

    def __init__(self):
        self.calls = []
        self.fail = ""
        self.locked = set()
        self.generation = 1
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.count = 3

    def details(self, book_id):
        return {"id": book_id, "title": "Original test book", "coverUrl": ""}

    def chapters(self, book_id):
        return [{"id": f"chapter-{index}", "title": f"Chapter {index}", "ordinal": index - 1,
                 "downloadable": f"chapter-{index}" not in self.locked} for index in range(1, self.count + 1)]

    def text(self, book_id, chapter_id):
        self.calls.append(chapter_id)
        if chapter_id == self.fail:
            raise SourceError("Temporary source failure")
        if self.block:
            self.entered.set()
            assert self.release.wait(10)
        return f"Fresh original text {self.generation} for {chapter_id}."


def seed(store, source, book_id="book", count=3):
    store.save_book(source.id, source.details(book_id))
    chapters = source.chapters(book_id)[:count]
    store.save_chapters(source.id, book_id, chapters)
    for chapter in chapters:
        store.save_text(source.id, book_id, chapter["id"], "Old original text for " + chapter["id"])


def process(store, source):
    claim = store.next_job()
    assert claim is not None
    ArchiveWorker(store, [source]).process(claim)
    return store.job(claim["id"])


@pytest.mark.parametrize("following", [False, True])
def test_refresh_refetches_saved_chapters_and_keeps_follow_settings_and_ids(tmp_path, following):
    source = Source()
    source.count = 4
    store = ArchiveStore(tmp_path)
    seed(store, source, count=3)
    store.follow(source.id, "book")
    if not following:
        store.unfollow(source.id, "book")
    follow_before = store.follow_status(source.id, "book")
    queued = store.enqueue(source.id, "book", refresh=True)
    assert queued["refresh"] is True and queued["total"] == 3 and queued["downloaded"] == 0
    assert store.enqueue(source.id, "book", refresh=True)["id"] == queued["id"]
    assert store.text(source.id, "book", "chapter-1") == "Old original text for chapter-1"

    job = process(store, source)

    assert job["status"] == "completed" and job["downloaded"] == job["total"] == 3
    assert source.calls == ["chapter-1", "chapter-2", "chapter-3"]
    assert store.text(source.id, "book", "chapter-1") == "Fresh original text 1 for chapter-1."
    assert store.text(source.id, "book", "chapter-4") is None
    assert store.follow_status(source.id, "book") == follow_before
    assert [chapter["id"] for chapter in store.chapters(source.id, "book")] == ["chapter-1", "chapter-2", "chapter-3", "chapter-4"]
    new_pass = store.enqueue(source.id, "book", refresh=True)
    assert new_pass["id"] != queued["id"] and new_pass["downloaded"] == 0 and new_pass["total"] == 3


def test_failed_refresh_keeps_old_copies_and_retry_skips_successful_replacements(tmp_path):
    source = Source()
    store = ArchiveStore(tmp_path)
    seed(store, source)
    source.fail = "chapter-2"
    job = store.enqueue(source.id, "book", refresh=True)
    failed = process(store, source)
    assert failed["status"] == "failed" and failed["downloaded"] == 1
    assert store.text(source.id, "book", "chapter-2") == "Old original text for chapter-2"
    assert store.stats(source.id, "book")["downloadedChapters"] == 3

    source.fail = ""
    store.control(job["id"], "retry")
    assert process(store, source)["status"] == "completed"
    assert source.calls.count("chapter-1") == 1
    assert source.calls.count("chapter-2") == 2
    assert source.calls.count("chapter-3") == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_inflight_refresh_cannot_overwrite_after_pause_or_cancel(tmp_path, action):
    source = Source()
    source.count = 1
    source.block = True
    store = ArchiveStore(tmp_path)
    seed(store, source, count=1)
    job = store.enqueue(source.id, "book", refresh=True)
    claim = store.next_job()
    worker = ArchiveWorker(store, [source])
    thread = threading.Thread(target=worker.process, args=(claim,))
    thread.start()
    try:
        assert source.entered.wait(5)
        assert store.text(source.id, "book", "chapter-1") == "Old original text for chapter-1"
        store.control(job["id"], action)
    finally:
        source.release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert store.text(source.id, "book", "chapter-1") == "Old original text for chapter-1"
    assert store.refresh_targets(job["id"])[0]["refreshed"] == 0
    store.control(job["id"], "resume" if action == "pause" else "retry")
    assert process(store, source)["status"] == "completed"


def test_refresh_checkpoints_survive_service_restart_and_revoke_old_leases(tmp_path):
    source = Source()
    store = ArchiveStore(tmp_path)
    seed(store, source)
    source.fail = "chapter-2"
    job = store.enqueue(source.id, "book", refresh=True)
    process(store, source)
    store.control(job["id"], "retry")
    old_claim = store.next_job()
    restarted = ArchiveStore(tmp_path)
    assert restarted.job(job["id"])["refresh"] is True
    assert restarted.job(job["id"])["status"] == "queued"
    assert not store.save_text_if_running(job["id"], old_claim["run_token"], source.id, "book", "chapter-2", "stale response", 2)
    source.fail = ""
    assert process(restarted, source)["status"] == "completed"
    assert source.calls.count("chapter-1") == 1
    assert restarted.job(job["id"])["downloaded"] == 3


def test_unavailable_chapters_are_reported_after_refreshing_the_remaining_targets(tmp_path):
    source = Source()
    store = ArchiveStore(tmp_path)
    seed(store, source)
    source.locked = {"chapter-2"}
    job = store.enqueue(source.id, "book", refresh=True)
    result = process(store, source)
    assert result["status"] == "failed" and result["downloaded"] == 2 and result["total"] == 3
    assert "saved copies were retained" in result["error"]
    assert source.calls == ["chapter-1", "chapter-3"]
    assert store.text(source.id, "book", "chapter-2") == "Old original text for chapter-2"
    source.locked.clear()
    store.control(job["id"], "retry")
    assert process(store, source)["status"] == "completed"
    assert source.calls == ["chapter-1", "chapter-3", "chapter-2"]


def test_refresh_batch_is_atomic_and_rejects_conflicting_jobs(tmp_path):
    source = Source()
    store = ArchiveStore(tmp_path)
    seed(store, source, "first")
    with pytest.raises(SourceError, match="no saved chapters"):
        store.enqueue_refresh_batch([(source.id, "first"), (source.id, "missing")])
    assert store.jobs() == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM refresh_chapters").fetchone()[0] == 0
    active = store.enqueue(source.id, "first")
    with pytest.raises(SourceError, match="active archive job"):
        store.enqueue(source.id, "first", refresh=True)
    store.control(active["id"], "cancel")
    seed(store, source, "second")
    jobs = store.enqueue_refresh_batch([(source.id, "first"), (source.id, "first"), (source.id, "second")])
    assert len(jobs) == 2 and all(job["refresh"] for job in jobs)


@pytest.mark.parametrize(("replacement", "error"), [("x" * 51, "storage limit"), ("", "Empty")])
def test_refresh_obeys_quota_and_validation_without_erasing_a_valid_old_copy(tmp_path, replacement, error):
    source = Source()
    source.count = 1
    store = ArchiveStore(tmp_path, max_bytes=50)
    seed(store, source, count=1)
    source.text = lambda *_: replacement
    job = store.enqueue(source.id, "book", refresh=True)
    result = process(store, source)
    assert result["status"] == "failed" and result["downloaded"] == 0
    assert error in result["error"]
    assert store.text(source.id, "book", "chapter-1") == "Old original text for chapter-1"
    assert store.refresh_targets(job["id"])[0]["refreshed"] == 0
    source.text = lambda *_: "y" * 45
    store.control(job["id"], "retry")
    assert process(store, source)["status"] == "completed"
    assert store.stats(source.id, "book")["storageBytes"] == 45


def test_retry_completed_refresh_runs_again_and_delete_cleans_checkpoints(tmp_path):
    source = Source()
    store = ArchiveStore(tmp_path)
    seed(store, source)
    job = store.enqueue(source.id, "book", refresh=True)
    assert process(store, source)["status"] == "completed"
    source.generation = 2
    assert store.control(job["id"], "retry")["downloaded"] == 0
    assert process(store, source)["status"] == "completed"
    assert len(source.calls) == 6
    assert store.text(source.id, "book", "chapter-1") == "Fresh original text 2 for chapter-1."
    store.delete_book(source.id, "book")
    assert store.refresh_targets(job["id"]) == []


def test_rearchive_replaces_flattened_hidden_ads_using_the_current_html_extractor(tmp_path):
    story = "An original test paragraph follows a traveller through a quiet village on a long journey home."

    def respond(request):
        if request.url.path == "/book/book":
            return httpx.Response(200, text="<h1>Original test book</h1>")
        if request.url.path == "/ajax/chapter-archive":
            return httpx.Response(200, text='<a href="/book/book/chapter-1">Chapter 1</a>')
        if request.url.path == "/book/book/chapter-1":
            return httpx.Response(200, text=f'<article><p>{story}<span style="display:none">A previously unknown hidden advertisement.</span></p></article>')
        return httpx.Response(404)

    source = NovelPingSource(client=httpx.Client(transport=httpx.MockTransport(respond)), access_authorized=True)
    source.min_interval = 0
    store = ArchiveStore(tmp_path)
    store.save_book(source.id, {"id": "book", "title": "Original test book"})
    store.save_chapters(source.id, "book", [{"id": "chapter-1", "title": "One", "ordinal": 0}])
    store.save_text(source.id, "book", "chapter-1", story + " A previously unknown hidden advertisement.")
    job = store.enqueue(source.id, "book", refresh=True)
    assert process(store, source)["status"] == "completed"
    assert store.text(source.id, "book", "chapter-1") == story
    with store._connect() as db:
        assert db.execute("SELECT body FROM chapters WHERE source='novelping'").fetchone()[0] == story


def test_refresh_endpoints_require_auth_and_support_console_sessions_and_batch_validation(tmp_path):
    source = Source()
    app = make_app([source], TOKEN, tmp_path)
    with TestClient(app, base_url="https://testserver") as api:
        store = app.state.archive_store
        seed(store, source)
        assert api.post("/v1/archive/fixture/book/refresh").status_code == 401
        assert api.post("/v1/archive/refresh", json={"items": [{"source": "fixture", "bookId": "book"}]}).status_code == 401
        assert api.post("/v1/archive/refresh", headers=AUTH, json={"items": [
            {"source": "fixture", "bookId": "book"}, {"source": "missing", "bookId": "book"}]}).status_code == 404
        assert store.jobs() == []
        assert api.post("/v1/archive/private/book/refresh", headers=AUTH).status_code == 409
        assert api.post("/v1/archive/refresh", headers=AUTH, json={"items": []}).status_code == 422
        assert api.post("/admin/login", json={"password": TOKEN}).status_code == 200
        assert api.post("/v1/archive/fixture/book/refresh").status_code == 403
        response = api.post("/v1/archive/fixture/book/refresh", headers={"X-QuietLeaf-Admin": "1"})
        assert response.status_code == 202 and response.json()["refresh"] is True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and store.job(response.json()["id"])["status"] != "completed":
            time.sleep(0.02)
        assert store.job(response.json()["id"])["status"] == "completed"
        assert store.text(source.id, "book", "chapter-1") == "Fresh original text 1 for chapter-1."
        source.generation = 2
        batch = api.post("/v1/archive/refresh", headers=AUTH, json={"items": [{"source": "fixture", "bookId": "book"}]})
        assert batch.status_code == 202 and batch.json()["queued"] == 1
        assert batch.json()["items"][0]["refresh"] is True
        batch_id = batch.json()["items"][0]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and store.job(batch_id)["status"] != "completed":
            time.sleep(0.02)
        assert store.job(batch_id)["status"] == "completed"
        assert store.text(source.id, "book", "chapter-1") == "Fresh original text 2 for chapter-1."
        assert store.follow_status(source.id, "book") is None


@pytest.mark.parametrize(("start", "end"), [(1, 20), (39, 58), (20, 20), (60, 60)])
def test_refresh_range_is_one_based_inclusive_and_leaves_other_text_untouched(tmp_path, start, end):
    source = Source()
    source.count = 60
    store = ArchiveStore(tmp_path)
    seed(store, source, count=60)
    store.follow(source.id, "book")
    follow = store.follow_status(source.id, "book")
    queued = store.enqueue(source.id, "book", refresh=True, start_chapter=start, end_chapter=end)
    assert queued["range_start"] == start and queued["range_end"] == end
    assert queued["total"] == end - start + 1
    assert process(store, source)["status"] == "completed"
    assert source.calls == [f"chapter-{n}" for n in range(start, end + 1)]
    for n in range(1, 61):
        expected = f"Fresh original text 1 for chapter-{n}." if start <= n <= end else f"Old original text for chapter-{n}"
        assert store.text(source.id, "book", f"chapter-{n}") == expected
    assert store.follow_status(source.id, "book") == follow


def test_range_positions_use_the_full_index_not_the_saved_subset_or_ordinal_value(tmp_path):
    source = Source()
    source.count = 10
    store = ArchiveStore(tmp_path)
    store.save_book(source.id, source.details("book"))
    chapters = source.chapters("book")
    for chapter in chapters:
        chapter["ordinal"] = (chapter["ordinal"] + 1) * 10
    store.save_chapters(source.id, "book", chapters)
    for n in (1, 4, 7, 10):
        store.save_text(source.id, "book", f"chapter-{n}", f"Old chapter {n}.")
    job = store.enqueue(source.id, "book", refresh=True, start_chapter=3, end_chapter=7)
    assert job["total"] == 2
    assert [row["id"] for row in store.refresh_targets(job["id"])] == ["chapter-4", "chapter-7"]
    assert process(store, source)["status"] == "completed"
    assert source.calls == ["chapter-4", "chapter-7"]
    assert store.text(source.id, "book", "chapter-1") == "Old chapter 1."
    assert store.text(source.id, "book", "chapter-3") is None


@pytest.mark.parametrize(("start", "end"), [(None, 20), (1, None), (0, 20), (-1, 20), (20, 1),
                                            (1, 61), (1, 100001), (True, 2), (1.5, 4)])
def test_invalid_ranges_do_not_create_jobs(tmp_path, start, end):
    source = Source()
    source.count = 60
    store = ArchiveStore(tmp_path)
    seed(store, source, count=1)
    store.save_chapters(source.id, "book", source.chapters("book"))
    with pytest.raises(SourceError):
        store.enqueue(source.id, "book", refresh=True, start_chapter=start, end_chapter=end)
    assert store.jobs() == []


def test_empty_saved_range_and_range_on_normal_archive_are_rejected(tmp_path):
    source = Source()
    store = ArchiveStore(tmp_path)
    seed(store, source, count=1)
    store.save_chapters(source.id, "book", source.chapters("book"))
    with pytest.raises(SourceError, match="range has no saved chapters"):
        store.enqueue(source.id, "book", refresh=True, start_chapter=2, end_chapter=3)
    with pytest.raises(SourceError, match="only supported when re-archiving"):
        store.enqueue(source.id, "book", start_chapter=1, end_chapter=1)
    assert store.jobs() == []


def test_active_refresh_deduplicates_only_the_same_range(tmp_path):
    source = Source()
    source.count = 60
    store = ArchiveStore(tmp_path)
    seed(store, source, count=60)
    job = store.enqueue(source.id, "book", refresh=True, start_chapter=39, end_chapter=58)
    assert store.enqueue(source.id, "book", refresh=True, start_chapter=39, end_chapter=58)["id"] == job["id"]
    for bounds in ((1, 20), (None, None)):
        with pytest.raises(SourceError, match="different re-archive range"):
            store.enqueue(source.id, "book", refresh=True, start_chapter=bounds[0], end_chapter=bounds[1])
    assert len(store.jobs()) == 1


def test_range_checkpoint_survives_failure_restart_and_explicit_retry(tmp_path):
    source = Source()
    source.count = 60
    store = ArchiveStore(tmp_path)
    seed(store, source, count=60)
    source.fail = "chapter-42"
    job = store.enqueue(source.id, "book", refresh=True, start_chapter=39, end_chapter=58)
    failed = process(store, source)
    assert failed["status"] == "failed" and failed["downloaded"] == 3 and failed["total"] == 20
    assert store.text(source.id, "book", "chapter-42") == "Old original text for chapter-42"
    restarted = ArchiveStore(tmp_path)
    assert restarted.job(job["id"])["range_start"] == 39 and restarted.job(job["id"])["range_end"] == 58
    restarted.control(job["id"], "retry")
    source.fail = ""
    source.count = 61
    completed = process(restarted, source)
    assert completed["status"] == "completed" and completed["downloaded"] == completed["total"] == 20
    assert source.calls == [f"chapter-{n}" for n in range(39, 43)] + [f"chapter-{n}" for n in range(42, 59)]
    assert restarted.text(source.id, "book", "chapter-59") == "Old original text for chapter-59"
    assert restarted.text(source.id, "book", "chapter-61") is None


def test_batch_range_validation_is_atomic(tmp_path):
    source = Source()
    source.count = 60
    store = ArchiveStore(tmp_path)
    seed(store, source, "first", count=60)
    source.count = 10
    seed(store, source, "short", count=10)
    with pytest.raises(SourceError, match="indexed chapters"):
        store.enqueue_refresh_batch([(source.id, "first"), (source.id, "short")], 39, 58)
    assert store.jobs() == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM refresh_chapters").fetchone()[0] == 0


def test_range_endpoints_validate_payloads_and_apply_the_range_to_each_selected_book(tmp_path):
    source = Source()
    source.count = 60
    with TestClient(make_app([source], TOKEN, tmp_path)) as api:
        api.app.state.archive_worker.stop()
        store = api.app.state.archive_store
        seed(store, source, "first", count=60)
        seed(store, source, "second", count=60)
        for body in ({"startChapter": 1}, {"startChapter": 20, "endChapter": 1},
                     {"startChapter": True, "endChapter": 20}, {"startChapter": 1.5, "endChapter": 20},
                     {"startchapter": 1, "endchapter": 20}):
            assert api.post("/v1/archive/fixture/first/refresh", headers=AUTH, json=body).status_code == 422
        one = api.post("/v1/archive/fixture/first/refresh", headers=AUTH,
                       json={"startChapter": 39, "endChapter": 58})
        assert one.status_code == 202 and one.json()["total"] == 20
        assert (one.json()["range_start"], one.json()["range_end"]) == (39, 58)
        assert api.post(f"/v1/archive/jobs/{one.json()['id']}/cancel", headers=AUTH).status_code == 200
        batch = api.post("/v1/archive/refresh", headers=AUTH, json={
            "items": [{"source": "fixture", "bookId": "first"}, {"source": "fixture", "bookId": "second"}],
            "startChapter": 1, "endChapter": 20,
        })
        assert batch.status_code == 202 and batch.json()["queued"] == 2
        assert all((job["range_start"], job["range_end"], job["total"]) == (1, 20, 20)
                   for job in batch.json()["items"])


def test_existing_job_schema_migrates_to_optional_range_bounds(tmp_path):
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        db.execute("""CREATE TABLE jobs(id TEXT PRIMARY KEY,source TEXT,book_id TEXT,status TEXT,
                      total INTEGER DEFAULT 0,downloaded INTEGER DEFAULT 0,refresh INTEGER DEFAULT 0,
                      error TEXT DEFAULT '',run_token TEXT DEFAULT '',created_at INTEGER,updated_at INTEGER)""")
        db.execute("INSERT INTO jobs(id,source,book_id,status,created_at,updated_at) VALUES('old','fixture','book','completed',1,1)")
    store = ArchiveStore(tmp_path)
    job = store.job("old")
    assert job["status"] == "completed" and job["range_start"] is None and job["range_end"] is None
