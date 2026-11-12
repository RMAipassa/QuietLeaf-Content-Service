import time

from fastapi.testclient import TestClient

from quietleaf_service.app import make_app
from quietleaf_service.archive import ArchiveStore

TOKEN = "follow-resume-fixture-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


class Source:
    id = "fixture"
    name = "Follow fixture"


def test_resuming_follow_keeps_saved_data_and_read_history_and_schedules_a_check(tmp_path):
    store = ArchiveStore(tmp_path)
    store.save_book("fixture", {"id": "book", "title": "Original follow fixture"})
    store.save_chapters("fixture", "book", [{"id": "one", "title": "One", "ordinal": 0}])
    store.save_text("fixture", "book", "one", "Original saved chapter")
    store.follow("fixture", "book", now=1000)
    store.record_activity("fixture", "book", now=2000)
    store.unfollow("fixture", "book")
    store.save_reading_position("fixture", "book", "one", 0.25, 0, "fixture-client", "fixture-event")
    position = store.reading_position("fixture", "book")
    with store._connect() as db:
        db.execute("UPDATE follows SET next_check_at=?,failure_count=3,last_error='Previous outage'", (int(time.time()) + 86400,))
    # Without a lifespan context the worker stays stopped, so we can inspect the pending schedule.
    api = TestClient(make_app([Source()], TOKEN, tmp_path))
    path = "/v1/archive/follows/fixture/book/resume"
    assert api.post(path).status_code == 401
    response = api.post(path, headers=AUTH)
    assert response.status_code == 200
    follow = response.json()["follow"]
    assert follow["enabled"] == 1 and follow["next_check_at"] <= int(time.time())
    assert follow["created_at"] == 1000 and follow["last_read_at"] == 2000
    assert follow["failure_count"] == 0 and follow["last_error"] == ""
    assert store.text("fixture", "book", "one") == "Original saved chapter"
    assert store.reading_position("fixture", "book") == position
    assert store.jobs() == []
    assert api.post(path, headers=AUTH).json()["follow"] == follow
    assert api.post("/v1/archive/follows/fixture/missing/resume", headers=AUTH).status_code == 404


def test_browser_resume_requires_confirmation_and_can_be_stopped_again(tmp_path):
    store = ArchiveStore(tmp_path)
    store.follow("fixture", "book")
    store.unfollow("fixture", "book")
    api = TestClient(make_app([Source()], TOKEN, tmp_path), base_url="https://testserver")
    assert api.post("/admin/login", json={"password": TOKEN}).status_code == 200
    path = "/v1/archive/follows/fixture/book"
    assert api.post(path + "/resume").status_code == 403
    assert api.post(path + "/resume", headers={"X-QuietLeaf-Admin": "1"}).status_code == 200
    assert api.delete(path, headers={"X-QuietLeaf-Admin": "1"}).json()["updated"] is True
    assert store.follow_status("fixture", "book")["enabled"] == 0
