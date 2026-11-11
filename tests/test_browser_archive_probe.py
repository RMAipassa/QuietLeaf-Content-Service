import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from quietleaf_service.adapters import SourceError
from quietleaf_service.archive import ArchiveStore, ArchiveWorker
from quietleaf_service.browser_archive_probe import BOOK_ID, NOVEL_PATH, BrowserArchiveSource, BrowserTransport, main, reader_state


class Transport:
    def __init__(self, fail_at=None, html_challenge=False):
        self.fail_at = fail_at
        self.html_challenge = html_challenge
        self.chapters_requested = []
        self.closed = False

    def request(self, method, target, body, max_bytes, accept):
        parsed = urlsplit(target)
        if parsed.path.startswith("/en/novel/"):
            data = {"props": {"pageProps": {"serie": {"serie_data": {
                "raw_id": int(BOOK_ID), "chapter_count": 45,
                "data": {"title": "Archive browser fixture", "author": "Test Writer"},
            }}}}}
            return {"status": 200, "text": '<script id="__NEXT_DATA__">' + json.dumps(data) + "</script>"}
        if parsed.path == f"/api/chapters/{BOOK_ID}":
            return {"status": 200, "text": json.dumps({"chapters": [
                {"order": n, "title": f"Chapter {n}", "locked": False} for n in range(1, 46)
            ]})}
        if parsed.path == "/api/reader/get":
            assert method == "POST"
            number = body["chapter_no"]
            self.chapters_requested.append(number)
            if number == self.fail_at:
                if self.html_challenge:
                    return {"status": 403, "challenge": True, "text": "<title>Just a moment...</title>"}
                return {"status": 200, "text": json.dumps({"success": False, "requireTurnstile": True})}
            return {"status": 200, "text": json.dumps({"success": True,
                "content_url": f"/api/v2/reader/content?chapter={number}"})}
        if parsed.path == "/api/v2/reader/content":
            number = int(parse_qs(parsed.query)["chapter"][0])
            return {"status": 200, "text": json.dumps({"data": {"data": {
                "body": [f"※0⛬ crossed the village in original fixture chapter {number}."],
                "glossary_data": {"version": 3, "terms": [["Lin Feng", "林峰"]]},
            }}})}
        pytest.fail("Unexpected browser request")

    def close(self):
        self.closed = True


def process(store, source):
    claim = store.next_job()
    assert claim is not None
    ArchiveWorker(store, [source], concurrency=1).process(claim)
    return store.job(claim["id"])


def test_real_archive_worker_saves_forty_consecutive_browser_chapters(tmp_path):
    transport = Transport()
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40, transport=transport)
    store = ArchiveStore(tmp_path)
    store.enqueue(source.id, BOOK_ID)
    result = process(store, source)
    assert result["status"] == "completed"
    assert result["downloaded"] == result["total"] == 40
    assert transport.chapters_requested == list(range(1, 41))
    assert store.stats(source.id, BOOK_ID)["downloadedChapters"] == 40
    for number in range(1, 41):
        assert store.text(source.id, BOOK_ID, f"chapter-{number}") == (
            f"Lin Feng crossed the village in original fixture chapter {number}."
        )
    source.close()
    assert transport.closed


@pytest.mark.parametrize("html_challenge", [False, True])
def test_challenge_at_twenty_six_stops_and_retry_retains_checkpoint(tmp_path, html_challenge):
    transport = Transport(fail_at=26, html_challenge=html_challenge)
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40, transport=transport)
    store = ArchiveStore(tmp_path)
    queued = store.enqueue(source.id, BOOK_ID)
    result = process(store, source)
    assert result["status"] == "failed"
    assert result["downloaded"] == 25
    assert "verification" in result["error"]
    assert transport.chapters_requested == list(range(1, 27))
    assert source.last_attempted_chapter == "chapter-26"
    assert store.text(source.id, BOOK_ID, "chapter-26") is None
    assert store.stats(source.id, BOOK_ID)["downloadedChapters"] == 25

    # Restart the store to exercise its durable leases/checkpoints before explicit retry.
    restarted = ArchiveStore(tmp_path)
    restarted.control(queued["id"], "retry")
    transport.fail_at = None
    result = process(restarted, source)
    assert result["status"] == "completed" and result["downloaded"] == 40
    assert transport.chapters_requested == list(range(1, 27)) + list(range(26, 41))
    source.close()


def test_browser_transport_cannot_be_redirected_to_another_host():
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40, transport=Transport())
    with pytest.raises(SourceError, match="unsafe|off-site"):
        source._request("GET", "https://evil.example/capture")
    source.close()


def test_reader_ui_mode_archives_rendered_text_instead_of_direct_reader_api(tmp_path):
    class UITransport(Transport):
        def __init__(self):
            super().__init__()
            self.rendered = []

        def read_chapter(self, number):
            self.rendered.append(number)
            return (f"Lin Feng crossed the village in rendered chapter {number}.\n\n" * 20).strip()

    transport = UITransport()
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40,
                                  transport=transport, reader_ui=True)
    store = ArchiveStore(tmp_path)
    store.enqueue(source.id, BOOK_ID)
    result = process(store, source)
    assert result["status"] == "completed" and result["downloaded"] == 40
    assert transport.rendered == list(range(1, 41))
    assert transport.chapters_requested == []
    assert "rendered chapter 28" in store.text(source.id, BOOK_ID, "chapter-28")
    source.close()


def test_long_story_dialogue_is_not_mistaken_for_cloudflare():
    text = ('"Just a moment," Lin Feng said, then described the village.\n' * 30)
    assert reader_state("Chapter 28 - Game Arrival", text) == "text"
    assert reader_state("Just a moment...", "Verifying...") == "verification"
    assert reader_state("Chapter 28", "AI Translation Requires Registration") == "account"


def test_normal_ui_verification_is_allowed_to_finish_without_a_scripted_click(monkeypatch, capsys):
    story = "Lin Feng crossed the village after the site finished loading.\n" * 30

    class Locator:
        def __init__(self):
            self.reads = 0

        async def all_inner_texts(self):
            self.reads += 1
            return [""] if self.reads == 1 else [story]

    class Page:
        url = "https://wtr-lab.com" + NOVEL_PATH + "/chapter-28"

        def __init__(self):
            self.body = Locator()

        def locator(self, _selector):
            return self.body

        async def title(self):
            return "Chapter 28 - Game Arrival"

    async def sleep(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", sleep)
    transport = BrowserTransport.__new__(BrowserTransport)
    transport.page = Page()
    transport.verification_seen = {28}
    transport.verification_completed = set()
    transport.reader_errors = {}
    transport.active_novel_path = NOVEL_PATH
    assert asyncio.run(transport._read_chapter(28)) == story
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [entry["event"] for entry in events] == ["reader_waiting_for_site_verification", "reader_verification_completed"]
    assert events[-1]["completion"] == "site_ui_without_scripted_clicks"


def test_only_public_static_assets_are_cached_and_api_calls_keep_the_request_gate():
    class Response:
        status = 200
        request = SimpleNamespace(method="GET")

        def __init__(self, url, headers):
            self.url, self.headers = url, headers

        async def body(self):
            return b"public script"

        async def all_headers(self):
            return self.headers

    class Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url, method="GET")
            self.cached = False

        async def fulfill(self, **kwargs):
            self.cached = True
            assert kwargs["body"] == b"public script"

    class Gate:
        def __init__(self):
            self.calls = []

        async def route(self, route):
            self.calls.append(route.request.url)

    transport = BrowserTransport.__new__(BrowserTransport)
    transport.static_cache, transport.static_cache_bytes = {}, 0
    transport.gate = Gate()

    async def run():
        asset = "https://wtr-lab.com/_next/static/chunks/public.js"
        await transport._cache_static_response(Response(asset, {"content-type": "application/javascript"}))
        await transport._cache_static_response(Response("https://wtr-lab.com/api/reader/get", {"content-type": "application/javascript"}))
        await transport._cache_static_response(Response("https://challenges.cloudflare.com/script.js", {"content-type": "application/javascript"}))
        await transport._cache_static_response(Response("https://wtr-lab.com/_next/static/private.js",
                                                       {"content-type": "application/javascript", "set-cookie": "private=value"}))
        assert list(transport.static_cache) == [asset]
        cached = Route(asset)
        await transport._route(cached)
        assert cached.cached and transport.gate.calls == []
        await transport._route(Route("https://wtr-lab.com/api/reader/get"))
        assert transport.gate.calls == ["https://wtr-lab.com/api/reader/get"]

    asyncio.run(run())


def test_chromium_only_patches_cannot_be_applied_to_firefox(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--archive-dir", str(tmp_path / "camoufox-archive-test-flags"), "--stealth"])
    assert error.value.code == 2


def test_cloak_requires_private_license_configuration_before_any_browser_starts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "1")
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)
    assert main(["--archive-dir", str(tmp_path / "camoufox-archive-test-cloak"), "--engine", "cloakbrowser"]) == 1
    output = capsys.readouterr().out
    assert "CLOAKBROWSER_LICENSE_KEY" in output
