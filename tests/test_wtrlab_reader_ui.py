import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from quietleaf_service.adapters import SourceError
from quietleaf_service.app import build_from_env, make_app
from quietleaf_service.archive import ArchiveStore
from quietleaf_service.wtrlab_browser import WtrLabReaderUiSource
from quietleaf_service.browser_probe import RequestGate
from quietleaf_service.browser_archive_probe import BrowserTransport


TOKEN = "private-service-test-token"
COOKIE = "__Secure-better-auth.session_token=private-account-test-cookie"
AUTH = {"Authorization": "Bearer " + TOKEN}


class Browser:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.fail_at = None

    def read_chapter(self, number, path):
        self.calls.append((number, path))
        if number == self.fail_at:
            raise SourceError("WTR-LAB reader did not complete verification; saved chapters retained")
        return (f"Original rendered chapter {number} from {path}. Lin Feng returned home.\n\n" * 10).strip()

    def close(self):
        self.closed = True


def routes(request):
    path = request.url.path
    if path == "/api/novel/get-by-raw-id":
        book_id = json.loads(request.content)["raw_id"]
        return httpx.Response(200, json={"success": True, "novel": {"raw_id": book_id, "slug": f"book-{book_id}"}})
    if path.startswith("/en/novel/"):
        book_id = int(path.split("/")[3])
        record = {"raw_id": book_id, "slug": f"book-{book_id}", "chapter_count": 335,
                  "data": {"title": "Original reader UI fixture", "author": "Writer"}}
        data = {"props": {"pageProps": {"serie": {"serie_data": record}}}}
        return httpx.Response(200, text='<script id="__NEXT_DATA__">' + json.dumps(data) + "</script>")
    if path.startswith("/api/chapters/"):
        params = parse_qs(request.url.query.decode())
        return httpx.Response(200, json={"chapters": [
            {"order": n, "title": f"Chapter {n}", "locked": False}
            for n in range(int(params["start"][0]), int(params["end"][0]) + 1)
        ]})
    assert path != "/api/reader/get", "Production reader UI must not call the old HTTP text path"
    return httpx.Response(404)


def source(browser=None):
    browser = browser or Browser()
    factories = []

    def factory(cookie, reserve):
        factories.append((cookie, reserve))
        return browser

    adapter = WtrLabReaderUiSource(client=httpx.Client(transport=httpx.MockTransport(routes)),
                                   access_authorized=True, session_cookie=COOKIE, browser_factory=factory)
    adapter.min_interval = 0
    return adapter, browser, factories


def wait_job(api, job_id, status):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = api.get(f"/v1/archive/jobs/{job_id}", headers=AUTH).json()
        if job["status"] == status:
            return job
        time.sleep(0.02)
    pytest.fail(f"Job did not reach {status}: {job}")


def test_production_source_is_lazy_unbounded_and_supports_other_novels():
    adapter, browser, factories = source()
    assert adapter.id == "wtr-lab" and adapter.chapter_text_mode == "reader-ui"
    assert adapter.browser_state == "not-started"
    assert len(adapter.chapters("51691")) == 335
    assert factories == []
    assert "chapter 96" in adapter.text("51691", "chapter-96")
    assert "chapter 335" in adapter.text("2", "chapter-335")
    assert browser.calls == [(96, "/en/novel/51691/book-51691"), (335, "/en/novel/2/book-2")]
    assert len(factories) == 1
    adapter.close()
    assert browser.closed and adapter.browser_state == "closed"


def test_real_archive_api_resumes_the_95_chapter_checkpoint(tmp_path):
    adapter, browser, _ = source()
    store = ArchiveStore(tmp_path)
    store.save_book(adapter.id, adapter.details("51691"))
    store.save_chapters(adapter.id, "51691", adapter.chapters("51691"))
    for n in range(1, 96):
        store.save_text(adapter.id, "51691", f"chapter-{n}", f"Original saved chapter {n}.")
    browser.fail_at = 97
    with TestClient(make_app([adapter], TOKEN, tmp_path)) as api:
        job = api.post("/v1/archive/wtr-lab/51691", headers=AUTH).json()
        failed = wait_job(api, job["id"], "failed")
        assert failed["downloaded"] == 96 and failed["total"] == 335
        assert [number for number, _ in browser.calls] == [96, 97]
        assert api.get("/v1/books/wtr-lab/51691/chapters/chapter-1/text", headers=AUTH).text == "Original saved chapter 1."
        browser.fail_at = None
        assert api.post(f"/v1/archive/jobs/{job['id']}/retry", headers=AUTH).status_code == 200
        completed = wait_job(api, job["id"], "completed")
        assert completed["downloaded"] == completed["total"] == 335
        assert [number for number, _ in browser.calls] == [96, 97] + list(range(97, 336))
    assert browser.closed


def test_diagnostics_are_authenticated_safe_and_show_the_actual_mode(tmp_path, monkeypatch):
    adapter, browser, factories = source()
    monkeypatch.setattr("quietleaf_service.wtrlab_browser.browser_preflight", lambda _path: {"ready": True})
    store = ArchiveStore(tmp_path)
    queued = store.enqueue(adapter.id, "51691")
    claim = store.next_job()
    store.finish_job_if_running(queued["id"], claim["run_token"], adapter.id, "51691", "failed",
                                COOKIE.partition("=")[2] + " " + TOKEN)
    with TestClient(make_app([adapter], TOKEN, tmp_path)) as api:
        assert api.get("/v1/diagnostics").status_code == 401
        response = api.get("/v1/diagnostics", headers=AUTH)
        assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        item = next(s for s in response.json()["sources"] if s["id"] == "wtr-lab")
        assert item["chapterTextMode"] == "reader-ui" and item["browserState"] == "not-started"
        assert item["accountSessionConfigured"] is True
        assert factories == []
        assert "private-account-test-cookie" not in response.text and TOKEN not in response.text
        assert response.json()["recentJobs"][0]["error"] == "[redacted] [redacted]"
        assert api.get("/v1/health", headers=AUTH).json()["sourceCapabilities"]["wtr-lab"]["chapterTextMode"] == "reader-ui"


def test_diagnostics_require_configured_authentication():
    with TestClient(make_app([])) as api:
        assert api.get("/v1/diagnostics").status_code == 403


def test_http_and_browser_share_one_source_rate_limit(monkeypatch):
    adapter, _, _ = source()
    adapter.min_interval = 2
    adapter._last_request = 9
    clock, sleeps = [10.0], []
    monkeypatch.setattr("quietleaf_service.wtrlab_browser.time.monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("quietleaf_service.wtrlab_browser.time.sleep", sleep)
    # The metadata request and a browser reservation must update the same clock.
    adapter._request_url("GET", "https://wtr-lab.com/en/novel/2/book-2", max_bytes=10000, accept="text/html")
    adapter._reserve_browser_request()
    adapter._request_url("GET", "https://wtr-lab.com/en/novel/2/book-2", max_bytes=10000, accept="text/html")
    assert sleeps == [1.0, 2.0, 2.0]
    adapter.close()


def test_build_from_env_selects_reader_ui_without_changing_source_id(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "catalog.json").write_text('{"name":"Fixture","books":[]}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIETLEAF_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("QUIETLEAF_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("QUIETLEAF_API_TOKEN", TOKEN)
    monkeypatch.setenv("QUIETLEAF_ENABLE_WTRLAB", "1")
    monkeypatch.setenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "1")
    monkeypatch.setenv("QUIETLEAF_WTRLAB_COOKIE", COOKIE)
    monkeypatch.setenv("QUIETLEAF_WTRLAB_READER_UI", "1")
    monkeypatch.setenv("QUIETLEAF_WTRLAB_BROWSER_PATH", "private-path")
    captured = []

    def factory(**kwargs):
        captured.append(kwargs)
        return source()[0]

    monkeypatch.setattr("quietleaf_service.wtrlab_browser.WtrLabReaderUiSource", factory)
    with TestClient(build_from_env()) as api:
        assert api.get("/v1/health", headers=AUTH).json()["sourceCapabilities"]["wtr-lab"]["chapterTextMode"] == "reader-ui"
    assert captured[0]["browser_path"] == "private-path"


def test_production_gate_has_no_diagnostic_request_cap_and_uses_shared_reservation():
    reserved = []

    class Route:
        request = SimpleNamespace(url="https://wtr-lab.com/api/reader/get", resource_type="fetch", method="POST")

        async def abort(self):
            pytest.fail("Production must not stop at the diagnostic request budget")

        async def continue_(self):
            reserved.append("continued")

    gate = RequestGate(None, lambda: reserved.append("reserved"))
    gate.requests = 1000
    asyncio.run(gate.route(Route()))
    assert reserved == ["reserved", "continued"] and gate.requests == 1001


def test_reader_state_is_isolated_when_switching_novels():
    path = "/en/novel/2/other-book"
    story = "Lin Feng returned to the village in an original test story.\n" * 30

    class Page:
        url = "https://wtr-lab.com" + path + "/chapter-96"

        def locator(self, _selector):
            return self

        async def all_inner_texts(self):
            return [story]

        async def title(self):
            return "Chapter 96"

    transport = BrowserTransport.__new__(BrowserTransport)
    transport.page = Page()
    transport.active_novel_path = "/en/novel/51691/first-book"
    transport.verification_seen = {96}
    transport.verification_completed = {96}
    transport.reader_errors = {96: "locked"}
    assert asyncio.run(transport._read_chapter(96, path)) == story
    assert transport.reader_errors == {} and transport.verification_seen == set()
    assert {k: transport.reader_activity[k] for k in ("bookId", "chapter", "phase")} == {
        "bookId": "2", "chapter": 96, "phase": "idle"}
    assert transport.reader_activity["bodyCharacters"] == len(story)
