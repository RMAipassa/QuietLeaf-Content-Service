from pathlib import Path

from fastapi.testclient import TestClient

from quietleaf_service.adapters import CatalogSource
from quietleaf_service.app import make_app


TOKEN = "private-dashboard-token"


def make_client(tmp_path):
    catalog = Path(__file__).parent.parent / "catalog"
    return TestClient(make_app([CatalogSource(catalog)], TOKEN, tmp_path))


def test_admin_shell_is_public_but_contains_no_private_data(tmp_path):
    with make_client(tmp_path) as client:
        page = client.get("/admin")
        assert page.status_code == 200
        assert "QuietLeaf Service Console" in page.text
        assert TOKEN not in page.text
        assert "At the Lighthouse" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["referrer-policy"] == "no-referrer"

        assert client.get("/admin/assets/admin.css").status_code == 200
        script = client.get("/admin/assets/admin.js")
        assert script.status_code == 200
        assert "/v1/archive/books" in script.text
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/", follow_redirects=False).headers["location"] == "/admin"


def test_only_exact_admin_resources_bypass_bearer_auth(tmp_path):
    with make_client(tmp_path) as client:
        missing = client.get("/v1/health")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert client.get("/v1/health", headers={"Authorization": "Basic nope"}).status_code == 401
        client.cookies.set("Authorization", TOKEN)
        assert client.get("/v1/health").status_code == 401
        assert client.get("/admin-not-public").status_code == 401
        assert client.get("/admin/assets/index.html").status_code == 401

        authorized = client.get("/v1/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert authorized.status_code == 200
        assert authorized.json()["releaseVersion"] == "0.8.0"
        assert authorized.json()["archiveWorkerRunning"] is True


def test_dashboard_management_endpoints_remain_authenticated(tmp_path):
    with make_client(tmp_path) as client:
        paths = ["/v1/archive/books", "/v1/archive/jobs", "/v1/archive/follows"]
        assert all(client.get(path).status_code == 401 for path in paths)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert all(client.get(path, headers=headers).status_code == 200 for path in paths)
        books = client.get("/v1/archive/books", headers=headers).json()
        assert books == {"items": [], "total": 0}


def test_failed_job_without_a_book_can_be_cleared(tmp_path):
    with make_client(tmp_path) as client:
        store = client.app.state.archive_store
        store.follow("collection", "failed-book")
        job = store.enqueue("collection", "failed-book")
        store.update_job(job["id"], status="failed", error="No chapter list")
        headers = {"Authorization": f"Bearer {TOKEN}"}

        response = client.delete("/v1/archive/books/collection/failed-book", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"deleted": False}
        assert store.job(job["id"]) is None
        assert store.follow_status("collection", "failed-book") is None
