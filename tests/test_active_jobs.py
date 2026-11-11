from fastapi.testclient import TestClient

from quietleaf_service.app import make_app


def test_active_jobs_are_authenticated_and_not_limited_by_recent_history(tmp_path):
    headers = {"Authorization": "Bearer active-jobs-test-token"}
    with TestClient(make_app([], "active-jobs-test-token", tmp_path)) as api:
        api.app.state.archive_worker.stop()
        store = api.app.state.archive_store
        with store._connect() as db:
            db.executemany("""INSERT INTO jobs(id,source,book_id,status,total,downloaded,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?,?)""", [
                (f"history-{n}", "fixture", f"book-{n}", "completed", 10, 10, 1000 + n, 1000 + n)
                for n in range(110)
            ] + [
                ("old-running", "wtr-lab", "51691", "running", 335, 95, 1, 1),
                ("old-queued", "fixture", "queued-book", "queued", 0, 0, 2, 2),
                ("old-paused", "fixture", "paused-book", "paused", 20, 10, 3, 3),
            ])
        assert api.get("/v1/archive/jobs/active").status_code == 401
        active = api.get("/v1/archive/jobs/active", headers=headers)
        assert active.status_code == 200
        assert [job["id"] for job in active.json()["items"]] == ["old-running", "old-queued", "old-paused"]
        recent = api.get("/v1/archive/jobs", headers=headers).json()["items"]
        assert len(recent) == 100 and all(job["status"] == "completed" for job in recent)
        history = api.get("/v1/archive/jobs", params={"history_only": True}, headers=headers).json()["items"]
        assert len(history) == 100 and all(job["status"] == "completed" for job in history)
        diagnostic = api.get("/v1/diagnostics", headers=headers).json()
        assert {job["id"] for job in diagnostic["activeJobs"]} == {"old-running", "old-queued", "old-paused"}


def test_recent_history_tracks_a_retried_jobs_latest_activity(tmp_path):
    with TestClient(make_app([], "active-jobs-test-token", tmp_path)) as api:
        api.app.state.archive_worker.stop()
        store = api.app.state.archive_store
        with store._connect() as db:
            db.executemany("""INSERT INTO jobs(id,source,book_id,status,created_at,updated_at)
                              VALUES(?,?,?,?,?,?)""", [
                ("older-retried", "fixture", "old", "failed", 1, 300),
                ("newer-finished", "fixture", "new", "completed", 200, 200),
            ])
        assert [job["id"] for job in store.jobs(history_only=True)] == ["older-retried", "newer-finished"]
