from pathlib import Path

from fastapi.testclient import TestClient

from quietleaf_service.adapters import CatalogSource
from quietleaf_service.app import ADMIN_COOKIE, ADMIN_LOGIN_BODY_BYTES, make_app


TOKEN = "private-dashboard-token"
PASSWORD = "private-dashboard-password"


def make_client(tmp_path):
    catalog = Path(__file__).parent.parent / "catalog"
    app = make_app([CatalogSource(catalog)], TOKEN, tmp_path, admin_password=PASSWORD)
    return TestClient(app, base_url="https://testserver")


def test_admin_shell_is_public_but_contains_no_private_data(tmp_path):
    with make_client(tmp_path) as client:
        page = client.get("/admin")
        assert page.status_code == 200
        assert "QuietLeaf Service Console" in page.text
        assert TOKEN not in page.text
        assert PASSWORD not in page.text
        assert "Admin password" in page.text
        assert "Operational settings" in page.text
        assert "archive-concurrency" in page.text
        assert "rearchive-selected" in page.text
        assert "At the Lighthouse" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["referrer-policy"] == "no-referrer"

        assert client.get("/admin/assets/admin.css").status_code == 200
        script = client.get("/admin/assets/admin.js")
        assert script.status_code == 200
        assert "/v1/archive/books" in script.text
        assert "/v1/archive/config" in script.text
        assert "/v1/archive/refresh" in script.text
        assert "Re-archive saved chapters" in script.text
        assert "/v1/home/cache" in script.text
        assert "Authorization" not in script.text
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
        assert authorized.json()["releaseVersion"] == "0.16.0"
        assert authorized.json()["archiveWorkerRunning"] is True
        assert authorized.json()["archiveConcurrency"] == 3
        assert authorized.json()["activeArchiveJobs"] == 0


def test_admin_password_creates_persistent_secure_session(tmp_path):
    with make_client(tmp_path) as client:
        rejected = client.post("/admin/login", json={"password": "not-the-password"})
        assert rejected.status_code == 401
        assert client.post("/admin/login", json={"password": TOKEN}).status_code == 401
        assert ADMIN_COOKIE not in client.cookies

        login = client.post("/admin/login", json={"password": PASSWORD})
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        cookie = login.headers["set-cookie"].lower()
        assert f"{ADMIN_COOKIE}=" in cookie
        assert "max-age=2592000" in cookie
        assert "httponly" in cookie
        assert "; secure" in cookie
        assert "samesite=strict" in cookie
        session = client.cookies[ADMIN_COOKIE]
        assert client.get("/v1/health").status_code == 200

    with make_client(tmp_path) as restarted_client:
        restored = restarted_client.get(
            "/v1/health", headers={"Cookie": f"{ADMIN_COOKIE}={session}"}
        )
        assert restored.status_code == 200


def test_invalid_or_password_incompatible_session_is_rejected(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get(
            "/v1/health", headers={"Cookie": f"{ADMIN_COOKIE}=9999999999.nonce.not-hex"}
        ).status_code == 401
        client.post("/admin/login", json={"password": PASSWORD})
        session = client.cookies[ADMIN_COOKIE]

    catalog = Path(__file__).parent.parent / "catalog"
    changed_app = make_app(
        [CatalogSource(catalog)], TOKEN, tmp_path, admin_password="different-admin-password"
    )
    with TestClient(changed_app, base_url="https://testserver") as changed_client:
        response = changed_client.get(
            "/v1/health", headers={"Cookie": f"{ADMIN_COOKIE}={session}"}
        )
        assert response.status_code == 401


def test_admin_cookie_is_secure_behind_https_proxy_but_allows_local_http(tmp_path):
    catalog = Path(__file__).parent.parent / "catalog"
    app = make_app([CatalogSource(catalog)], TOKEN, tmp_path, admin_password=PASSWORD)
    with TestClient(app, base_url="http://127.0.0.1") as local_client:
        local = local_client.post("/admin/login", json={"password": PASSWORD})
        assert "; secure" not in local.headers["set-cookie"].lower()

    proxy_app = make_app([CatalogSource(catalog)], TOKEN, tmp_path, admin_password=PASSWORD)
    with TestClient(proxy_app, base_url="http://127.0.0.1") as proxy_client:
        proxied = proxy_client.post(
            "/admin/login",
            json={"password": PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "; secure" in proxied.headers["set-cookie"].lower()


def test_admin_session_requires_confirmation_for_writes_and_can_log_out(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 200

        assert client.delete("/v1/home/cache").status_code == 403
        confirmed = client.delete("/v1/home/cache", headers={"X-QuietLeaf-Admin": "1"})
        assert confirmed.status_code == 200

        assert client.post("/admin/logout").status_code == 403
        logout = client.post("/admin/logout", headers={"X-QuietLeaf-Admin": "1"})
        assert logout.status_code == 200
        assert logout.json() == {"authenticated": False}
        assert client.get("/v1/health").status_code == 401


def test_bearer_token_can_write_without_admin_confirmation(tmp_path):
    with make_client(tmp_path) as client:
        response = client.delete(
            "/v1/home/cache", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200


def test_admin_login_is_rate_limited(tmp_path):
    with make_client(tmp_path) as client:
        for _ in range(5):
            assert client.post("/admin/login", json={"password": "wrong-password"}).status_code == 401

        blocked = client.post("/admin/login", json={"password": PASSWORD})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0


def test_admin_login_rejects_oversized_or_malformed_requests(tmp_path):
    with make_client(tmp_path) as client:
        oversized = client.post(
            "/admin/login",
            content=b"x" * (ADMIN_LOGIN_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        assert oversized.status_code == 413
        malformed = client.post(
            "/admin/login", content=b"not-json", headers={"Content-Type": "application/json"}
        )
        assert malformed.status_code == 422


def test_api_token_is_admin_password_fallback(tmp_path):
    catalog = Path(__file__).parent.parent / "catalog"
    app = make_app([CatalogSource(catalog)], TOKEN, tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post("/admin/login", json={"password": TOKEN})
        assert login.status_code == 200
        assert client.get("/v1/health").status_code == 200


def test_dashboard_management_endpoints_remain_authenticated(tmp_path):
    with make_client(tmp_path) as client:
        paths = ["/v1/archive/books", "/v1/archive/jobs", "/v1/archive/follows",
                 "/v1/archive/config"]
        assert all(client.get(path).status_code == 401 for path in paths)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert all(client.get(path, headers=headers).status_code == 200 for path in paths)
        books = client.get("/v1/archive/books", headers=headers).json()
        assert books == {"items": [], "total": 0}


def test_operational_configuration_is_validated_applied_and_persisted(tmp_path):
    with make_client(tmp_path) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/v1/archive/config").status_code == 401
        assert client.get("/v1/archive/config", headers=headers).json() == {
            "archiveConcurrency": 3,
            "followIntervalSeconds": 3600,
            "activeIntervalSeconds": 1800,
        }
        invalid = client.put("/v1/archive/config", headers=headers, json={
            "archiveConcurrency": 4,
            "followIntervalSeconds": 3600,
            "activeIntervalSeconds": 1800,
        })
        assert invalid.status_code == 422

        saved = client.put("/v1/archive/config", headers=headers, json={
            "archiveConcurrency": 2,
            "followIntervalSeconds": 7200,
            "activeIntervalSeconds": 2400,
        })
        assert saved.status_code == 200
        assert saved.json() == {
            "archiveConcurrency": 2,
            "followIntervalSeconds": 7200,
            "activeIntervalSeconds": 2400,
        }
        health = client.get("/v1/health", headers=headers).json()
        assert health["archiveConcurrency"] == 2
        assert health["followIntervalSeconds"] == 7200
        assert health["activeIntervalSeconds"] == 2400

    with make_client(tmp_path) as restarted:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert restarted.get("/v1/archive/config", headers=headers).json() == saved.json()
        assert restarted.app.state.archive_worker.concurrency == 2


def test_failed_job_without_a_book_can_be_cleared(tmp_path):
    client = make_client(tmp_path)
    store = client.app.state.archive_store
    store.follow("collection", "failed-book")
    job = store.enqueue("collection", "failed-book")
    store.update_job(job["id"], status="failed", error="No chapter list")
    with client:
        headers = {"Authorization": f"Bearer {TOKEN}"}

        response = client.delete("/v1/archive/books/collection/failed-book", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"deleted": False}
        assert store.job(job["id"]) is None
        assert store.follow_status("collection", "failed-book") is None
