import asyncio
import json
from types import SimpleNamespace

import pytest

from quietleaf_service import browser_probe as probe


def test_cookie_selection_excludes_clearance_and_personal_json_cookies():
    header = '_ga=analytics; g_state={"i_l":0}; cf_clearance=clearance; __Secure-better-auth.session_token=test%2Bvalue%3D; NEXT_LOCALE=en'
    cookie = probe.account_cookie(header)
    assert cookie["name"] == probe.TOKEN_NAME
    assert cookie["value"] == "test%2Bvalue%3D"
    assert cookie["url"] == "https://wtr-lab.com"
    assert cookie["secure"] is True
    assert cookie["httpOnly"] is True


@pytest.mark.parametrize("header", ["", "token=other", "__Secure-better-auth.session_token=", "token=secret\r\nHeader: bad",
                                    "__Secure-better-auth.session_token=a; __Secure-better-auth.session_token=b"])
def test_missing_invalid_and_ambiguous_cookies_are_rejected(header):
    with pytest.raises(ValueError):
        probe.account_cookie(header)


@pytest.mark.parametrize(("text", "state"), [
    ("Just a moment...", "verification_required"),
    ("AI Translation Requires Registration. Guests can preview the first 10 chapters." * 20, "account_required"),
    ("Sign in to unlock and read.", "account_required"),
    ("This chapter requires unlocking for AI translation.", "chapter_locked"),
    ("A story about ※13⛬." * 50, "unresolved_glossary"),
    ("Lin Feng went home.\n" * 50, "chapter_text"),
    ("Loading...", "not_ready"),
])
def test_probe_never_treats_gate_screens_as_chapter_success(text, state):
    assert probe.chapter_state(text) == state


def test_route_gate_spaces_requests_and_blocks_unapproved_destinations(monkeypatch):
    clock, sleeps, results = [10.0], [], []
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(probe.asyncio, "sleep", sleep)

    class Route:
        def __init__(self, url, resource_type="script"):
            self.request = SimpleNamespace(url=url, resource_type=resource_type)

        async def abort(self):
            results.append("blocked")

        async def continue_(self):
            results.append("allowed")

    async def run():
        gate = probe.RequestGate()
        for url in ("https://wtr-lab.com/chapter-1", "https://wtr-lab.com/script.js",
                    "https://evil.example/capture", "http://wtr-lab.com/", "https://wtr-lab.com:444/",
                    "https://challenges.cloudflare.com/script.js"):
            await gate.route(Route(url))
        await gate.route(Route("https://img.wtr-lab.com/cover", "image"))
        assert gate.requests == 2

    asyncio.run(run())
    assert sleeps == [0.0, 2.0]
    assert results == ["allowed", "allowed", "blocked", "blocked", "blocked", "allowed", "blocked"]


def test_probe_configuration_errors_do_not_echo_credentials(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIETLEAF_WTRLAB_COOKIE", "private-secret-but-invalid")
    assert probe.main([]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["reason"] == "missing_or_invalid_account_cookie"
    assert "private-secret" not in output


def test_browser_errors_are_redacted_and_fail_the_probe(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIETLEAF_WTRLAB_COOKIE", "__Secure-better-auth.session_token=private-test-session")
    monkeypatch.setenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "1")
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _name: "0.5.9")
    monkeypatch.setattr(probe.shutil, "which", lambda _name: "/fake/Xvfb")

    async def failure(*args, **kwargs):
        raise RuntimeError("A browser error containing Cookie: private-test-session")

    monkeypatch.setattr(probe, "run_probe", failure)
    assert probe.main([]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_type"] == "RuntimeError"
    assert "private-test-session" not in output
