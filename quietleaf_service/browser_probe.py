"""Opt-in, one-shot WTR-LAB browser diagnostic; never writes chapters or cookies."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


ORIGIN = "https://wtr-lab.com"
NOVEL_PATH = "/en/novel/51691/game-arrival-100-drop-rate-im-invincible"
TOKEN_NAME = "__Secure-better-auth.session_token"
SOURCE_HOSTS = {"wtr-lab.com", "img.wtr-lab.com"}
ALLOWED_HOSTS = SOURCE_HOSTS | {"challenges.cloudflare.com"}
MARKERS = re.compile(r"(?:»[A-Za-z0-9_-]{1,64}|※[0-9]{1,6})[⛬〓]")


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def account_cookie(header: str) -> dict:
    """Select only the login token, even from a header containing JSON cookies."""
    if len(header) > 32_768 or any(ord(char) < 32 or ord(char) > 126 for char in header):
        raise ValueError("Invalid session cookie")
    matches = []
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == TOKEN_NAME and value:
            matches.append(value)
    if len(matches) != 1:
        raise ValueError("Set QUIETLEAF_WTRLAB_COOKIE to the WTR-LAB account session cookie")
    return {"name": TOKEN_NAME, "value": matches[0], "url": ORIGIN,
            "secure": True, "httpOnly": True, "sameSite": "Lax"}


def chapter_state(text: str) -> str:
    lower = text.casefold()
    if any(value in lower for value in ("verify you are human", "performing security verification",
                                        "checking your browser", "just a moment", "verify that you are human")):
        return "verification_required"
    if any(value in lower for value in ("you are not logged in", "guests can preview", "need to log in",
                                        "ai translation requires registration", "sign in to unlock")):
        return "account_required"
    if any(value in lower for value in ("this chapter requires unlocking", "unlock chapter", "chapter is locked")):
        return "chapter_locked"
    if MARKERS.search(text):
        return "unresolved_glossary"
    return "chapter_text" if len(text.strip()) >= 500 else "not_ready"


class RequestGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.requests = 0

    async def route(self, route) -> None:
        request = route.request
        url = urlsplit(request.url)
        if (url.scheme != "https" or url.hostname not in ALLOWED_HOSTS
                or url.port not in (None, 443) or url.username or url.password):
            await route.abort()
            return
        if request.resource_type in {"image", "font", "media"} and url.hostname in SOURCE_HOSTS:
            await route.abort()
            return
        if url.hostname in SOURCE_HOSTS:
            async with self.lock:
                await asyncio.sleep(max(0.0, 2.0 - (time.monotonic() - self.last_request)))
                if self.requests >= 200:
                    await route.abort()
                    return
                self.last_request = time.monotonic()
                self.requests += 1
                await route.continue_()
            return
        await route.continue_()


async def run_probe(cookie: dict, *, headed: bool, browser_path: str | None) -> bool:
    from camoufox.async_api import AsyncCamoufox

    options = {"headless": False if headed else ("virtual" if sys.platform == "linux" else True)}
    if browser_path:
        options["executable_path"] = browser_path
    gate = RequestGate()
    emit("browser_starting", platform=sys.platform, camoufox=importlib.metadata.version("camoufox"),
         mode="headed" if headed else str(options["headless"]))
    async with AsyncCamoufox(**options) as browser:
        context = await browser.new_context(service_workers="block")
        await context.add_cookies([cookie])
        await context.route("**/*", gate.route)
        page = await context.new_page()
        page.set_default_timeout(5_000)
        page.set_default_navigation_timeout(180_000)
        results = []
        for number in (1, 11):
            emit("chapter_loading", chapter=number)
            await page.goto(f"{ORIGIN}{NOVEL_PATH}/chapter-{number}", wait_until="domcontentloaded")
            deadline = time.monotonic() + 150
            state, body = "not_ready", ""
            while time.monotonic() < deadline:
                if urlsplit(page.url).hostname != "wtr-lab.com":
                    state = "unexpected_navigation"
                    break
                # Inspect rendered prose, not a raw API response that may still need verification.
                candidate = page.locator(f"#chapter-{number} .chapter-body")
                body = "\n".join(await candidate.all_inner_texts())
                state = chapter_state(body)
                if state == "not_ready":
                    visible = await page.locator("body").inner_text()
                    gate_state = chapter_state((await page.title()) + "\n" + visible)
                    if gate_state in {"verification_required", "account_required", "chapter_locked"}:
                        state = gate_state
                if state != "not_ready":
                    break
                await asyncio.sleep(2)
            emit("chapter_result", chapter=number, status=state,
                 characters=len(body) if state == "chapter_text" else 0,
                 lin_feng_present="Lin Feng" in body if state == "chapter_text" else False,
                 source_requests=gate.requests)
            results.append(state)
            if state != "chapter_text":
                break
        await context.close()
        return results == ["chapter_text", "chapter_text"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-browser", action="store_true", help="Explicitly download the browser before testing")
    parser.add_argument("--headed", action="store_true", help="Show the browser on an existing display")
    parser.add_argument("--browser-path", help="Use an already installed Camoufox executable")
    parser.add_argument("--timeout", type=int, default=540, help="Overall test timeout in seconds (60–900)")
    args = parser.parse_args(argv)
    if not 60 <= args.timeout <= 900:
        parser.error("--timeout must be between 60 and 900")
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        cookie = account_cookie(os.getenv("QUIETLEAF_WTRLAB_COOKIE", ""))
    except ValueError:
        emit("configuration_error", reason="missing_or_invalid_account_cookie")
        return 2
    if os.getenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "").lower() not in {"1", "true", "yes"}:
        emit("configuration_error", reason="source_access_authorization_required")
        return 2
    if sys.platform == "linux" and not args.headed and not shutil.which("Xvfb"):
        emit("configuration_error", reason="install_xvfb_on_amp_host")
        return 2
    try:
        importlib.metadata.version("camoufox")
    except importlib.metadata.PackageNotFoundError:
        emit("configuration_error", reason="install_browser_test_extra")
        return 2
    if args.fetch_browser:
        # Do not pass account credentials to the browser package downloader.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("QUIETLEAF_")}
        if subprocess.run([sys.executable, "-m", "camoufox", "fetch"], env=environment).returncode:
            emit("setup_failed", reason="browser_download_failed")
            return 2
    try:
        success = asyncio.run(asyncio.wait_for(
            run_probe(cookie, headed=args.headed, browser_path=args.browser_path), timeout=args.timeout,
        ))
        emit("probe_complete", success=success)
        return 0 if success else 1
    except TimeoutError:
        emit("probe_failed", reason="overall_timeout")
    except KeyboardInterrupt:
        emit("probe_failed", reason="interrupted")
    except Exception as exc:
        # Browser errors can contain URLs, request bodies or headers. Never print them.
        emit("probe_failed", reason="browser_or_navigation_error", error_type=type(exc).__name__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
