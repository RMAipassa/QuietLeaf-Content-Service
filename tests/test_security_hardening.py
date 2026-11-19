import asyncio
import importlib.metadata
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from quietleaf_service.adapters import CatalogSource
from quietleaf_service.app import build_from_env, make_app
from quietleaf_service.archive import ArchiveStore
from quietleaf_service.document_limits import DOCUMENT_MEMORY_BYTES, document_preflight
from quietleaf_service.login_limits import LoginLimits


TOKEN = "security-regression-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
CATALOG = Path(__file__).resolve().parents[1] / "catalog"


def test_missing_credentials_refuse_storage_before_creation_or_migration(tmp_path):
    new = tmp_path / "new"
    with pytest.raises(ValueError, match="authentication"):
        make_app([], archive_dir=new)
    assert not new.exists()
    existing = ArchiveStore(tmp_path / "existing")
    existing.save_book("fixture", {"id": "story", "title": "Private fixture"})
    before = existing.database.read_bytes()
    for explicit_sample in (False, True):
        with pytest.raises(ValueError):
            make_app([], archive_dir=existing.root, allow_anonymous_sample=explicit_sample)
    assert existing.database.read_bytes() == before


def test_explicit_anonymous_sample_is_stateless_and_normal_auth_still_works(tmp_path):
    with pytest.raises(ValueError, match="explicitly"):
        make_app([CatalogSource(CATALOG)])
    with TestClient(make_app([CatalogSource(CATALOG)], allow_anonymous_sample=True)) as client:
        assert client.get("/v1/health").json()["archiveEnabled"] is False
        assert client.get("/v1/books/collection/lighthouse-demo/chapters/chapter-1/text").status_code == 200
        assert client.get("/v1/private/books").status_code == 503
    with TestClient(make_app([], admin_password="only-admin-fixture-password", archive_dir=tmp_path),
                    base_url="https://testserver") as client:
        assert client.get("/v1/health").status_code == 401
        assert client.post("/admin/login", json={"password": "only-admin-fixture-password"}).status_code == 200
        assert client.get("/v1/health").status_code == 200


def test_environment_sample_opt_in_cannot_open_persistent_or_live_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("QUIETLEAF_CATALOG_DIR", str(CATALOG))
    with pytest.raises(ValueError, match="authentication|QUIETLEAF_API_TOKEN"):
        build_from_env()
    monkeypatch.setenv("QUIETLEAF_ALLOW_ANONYMOUS_SAMPLE", "1")
    with TestClient(build_from_env()) as client:
        assert client.get("/v1/health").json()["archiveEnabled"] is False
    for name, value in (("QUIETLEAF_ARCHIVE_DIR", str(tmp_path / "archive")),
                        ("QUIETLEAF_ENABLE_WTRLAB", "1"), ("QUIETLEAF_HTML_SOURCES", "unread-fixture.json")):
        with monkeypatch.context() as change:
            change.setenv(name, value)
            with pytest.raises(ValueError, match="sample mode"):
                build_from_env()
    assert not (tmp_path / "archive").exists()


def test_all_personal_api_responses_and_errors_are_noncacheable(tmp_path):
    app = make_app([], TOKEN, tmp_path)
    store = app.state.archive_store
    store.save_book("fixture", {"id": "story", "title": "Saved fixture"})
    store.save_chapters("fixture", "story", [{"id": "one", "title": "One"}])
    store.save_text("fixture", "story", "one", "Original fictional prose.")
    paths = ["/v1/health", "/v1/books/fixture/story", "/v1/books/fixture/story/chapters/one/text",
             "/v1/archive/books", "/v1/archive/jobs/missing", "/v1/reading-progress",
             "/v1/reader/books/fixture/story/chapters/one?plain=true&raw=true"]
    with TestClient(app) as client:
        for path in paths:
            for headers in ({}, AUTH):
                response = client.get(path, headers=headers)
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(paths[2], headers=AUTH).headers["content-type"].startswith("text/plain")


def test_dependency_inventory_and_resource_probe_are_authenticated_and_sanitized(tmp_path):
    with TestClient(make_app([], TOKEN, tmp_path)) as client:
        assert client.get("/v1/diagnostics").status_code == 401
        response = client.get("/v1/diagnostics", headers=AUTH)
        assert response.status_code == 200
        payload = response.json()
        assert payload["dependencyVersions"]["Pillow"] == importlib.metadata.version("Pillow")
        assert payload["dependencyVersions"]["cryptography"] == importlib.metadata.version("cryptography")
        assert payload["dependencyVersions"]["PDFium"]
        assert payload["documentProcessing"]["ready"] is True
        assert payload["documentProcessing"]["memoryBytes"] == DOCUMENT_MEMORY_BYTES
        assert TOKEN not in response.text
        assert str(tmp_path) not in response.text
        assert "environment" not in payload and "paths" not in payload


def test_atomic_login_admission_blocks_staged_guesses_and_ignores_forwarded_ip_header():
    app = make_app([], TOKEN)

    async def exercise():
        release = asyncio.Event()
        admitted = asyncio.Event()
        rejected = asyncio.Event()
        started = denied = 0

        async def body():
            nonlocal started
            started += 1
            if started == 2:
                admitted.set()
            await release.wait()
            yield b'{"password":"wrong-fixture-password"}'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            async def attempt(number):
                nonlocal denied
                response = await client.post("/admin/login", content=body(), headers={
                    "Content-Type": "application/json", "X-Forwarded-For": f"192.0.2.{number}"})
                if response.status_code == 429:
                    denied += 1
                    if denied == 10:
                        rejected.set()
                return response.status_code
            tasks = [asyncio.create_task(attempt(n)) for n in range(12)]
            try:
                await asyncio.wait_for(admitted.wait(), 5)
                await asyncio.wait_for(rejected.wait(), 5)
            finally:
                release.set()
            results = await asyncio.gather(*tasks)
            assert results.count(401) == 2 and results.count(429) == 10
            assert app.state.login_limits.inflight == 0
            assert (await client.post("/admin/login", json={"password": TOKEN})).status_code == 200
    asyncio.run(exercise())


def test_login_body_deadline_releases_inflight_slot(monkeypatch):
    monkeypatch.setattr("quietleaf_service.app.ADMIN_LOGIN_BODY_SECONDS", 0.02)
    app = make_app([], TOKEN)

    async def exercise():
        async def slow_body():
            await asyncio.Event().wait()
            yield b"never"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            response = await client.post("/admin/login", content=slow_body())
            assert response.status_code == 408
            assert app.state.login_limits.inflight == 0
            assert (await client.post("/admin/login", json={"password": TOKEN})).status_code == 200
    asyncio.run(exercise())


def test_login_global_budget_client_cap_success_and_expiry_recovery():
    now = [0]
    limits = LoginLimits(clock=lambda: now[0], globally=3, global_inflight=2)
    assert limits.reserve("one") == limits.reserve("two") == 0
    assert limits.reserve("three") == 1
    limits.release("one", success=True)
    limits.release("two")
    assert limits.reserve("one") == 0
    limits.release("one")
    assert limits.reserve("new-address") == 300
    now[0] = 301
    assert limits.reserve("new-address") == 0
    limits.release("new-address")
    assert limits.inflight == 0


def test_parser_probe_enforces_supported_os_limits():
    probe = document_preflight()
    assert probe == {"ready": True, "memoryBytes": DOCUMENT_MEMORY_BYTES, "cpuSeconds": 20, "wallSeconds": 35}
