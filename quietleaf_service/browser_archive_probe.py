"""Bounded real ArchiveWorker job using one authenticated browser session."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .adapters import SourceError
from .archive import ArchiveStore, ArchiveWorker
from .browser_probe import NOVEL_PATH, ORIGIN, RequestGate, account_cookie, emit
from .content import clean_chapter_text
from .wtrlab import GLOSSARY_MARKER, WtrLabSource


BOOK_ID = "51691"


def reader_state(title: str, text: str) -> str:
    """Check actual gate screens without mistaking story dialogue for a challenge."""
    heading = title.strip().casefold()
    lower = text.casefold()
    if heading.startswith(("just a moment", "attention required")):
        return "verification"
    if any(value in lower for value in ("ai translation requires registration", "guests can preview ai translation",
                                        "you are not logged in!")):
        return "account"
    if "this chapter requires unlocking" in lower or "sign in to unlock and read" in lower:
        return "locked"
    if GLOSSARY_MARKER.search(text):
        return "glossary"
    return "text" if len(text.strip()) >= 500 else "waiting"


@asynccontextmanager
async def browser_backend(engine: str, path: str | None, headed: bool, blink_flag: bool):
    if engine == "camoufox":
        from camoufox.async_api import AsyncCamoufox

        options = {"headless": False if headed else ("virtual" if sys.platform == "linux" else True)}
        if path:
            options["executable_path"] = path
        async with AsyncCamoufox(**options) as browser:
            yield browser
    elif engine == "cloakbrowser":
        from cloakbrowser import launch_async

        options = {"headless": not headed}
        if path:
            options["executable_path"] = path
        browser = await launch_async(**options)
        try:
            yield browser
        finally:
            await browser.close()
    else:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            options = {"headless": not headed}
            if path:
                options["executable_path"] = path
            if blink_flag:
                options["args"] = ["--disable-blink-features=AutomationControlled"]
            browser = await playwright.chromium.launch(**options)
            try:
                yield browser
            finally:
                await browser.close()


FETCH = """async ({url, method, body, limit, accept}) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120000);
    try {
        const response = await fetch(url, {method, credentials:'same-origin', redirect:'error',
            headers:body === null ? {Accept:accept} : {Accept:accept, 'Content-Type':'application/json'},
            body:body === null ? undefined : JSON.stringify(body), signal:controller.signal});
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let size = 0, text = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > limit) { await reader.cancel(); throw new Error('response_limit'); }
            text += decoder.decode(value, {stream:true});
        }
        text += decoder.decode();
        return {status:response.status, challenge:response.headers.get('cf-mitigated') === 'challenge', text};
    } finally { clearTimeout(timeout); }
}"""


class BrowserTransport:
    """Own Playwright on one thread; synchronous workers share its request gate."""

    def __init__(self, cookie: dict, browser_path: str | None, max_requests: int | None, *,
                 engine: str = "camoufox", headed: bool = False, stealth: bool = False, blink_flag: bool = False,
                 reader_ui: bool = False, reserve_request=None):
        self.cookie = cookie
        self.browser_path = browser_path
        self.max_requests = max_requests
        self.engine, self.headed, self.stealth, self.blink_flag = engine, headed, stealth, blink_flag
        self.reader_ui = reader_ui
        self.reserve_request = reserve_request
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self._run, name="wtrlab-browser", daemon=True)
        self.thread.start()
        try:
            self.ready.result(timeout=90)
        except Exception:
            raise SourceError("The selected browser could not start under the application service account") from None

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError("browser_start_failed"))

    async def _serve(self):
        async with browser_backend(self.engine, self.browser_path, self.headed, self.blink_flag) as browser:
            self.loop = asyncio.get_running_loop()
            self.stopped = asyncio.Event()
            self.context = await browser.new_context(service_workers="block")
            if self.stealth:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(self.context)
            await self.context.add_cookies([self.cookie])
            self.gate = RequestGate(self.max_requests, self.reserve_request)
            self.static_cache = {}
            self.static_cache_bytes = 0
            await self.context.route("**/*", self._route)
            self.page = await self.context.new_page()
            self.verification_seen = set()
            self.verification_completed = set()
            self.reader_errors = {}
            self.active_novel_path = None
            self.reader_activity = None
            self.page.on("response", self._reader_response)
            self.page.on("response", self._cache_static_response)
            self.ready.set_result(None)
            await self.stopped.wait()
            await self.context.close()

    async def _route(self, route):
        cached = self.static_cache.get(route.request.url)
        if cached is not None and route.request.method == "GET":
            mime, body = cached
            await route.fulfill(status=200, headers={"Content-Type": mime}, body=body)
            return
        await self.gate.route(route)

    async def _cache_static_response(self, response):
        url = urlsplit(response.url)
        if (url.scheme != "https" or url.hostname != "wtr-lab.com" or url.port not in (None, 443)
                or url.username or url.password or not url.path.startswith("/_next/static/")
                or response.status != 200 or response.request.method != "GET"
                or response.url in self.static_cache):
            return
        try:
            headers = await response.all_headers()
            mime = headers.get("content-type", "")
            if ("javascript" not in mime and "css" not in mime) or "set-cookie" in headers:
                return
            body = await response.body()
            if len(body) <= 2 * 1024 * 1024 and self.static_cache_bytes + len(body) <= 64 * 1024 * 1024:
                self.static_cache[response.url] = (mime, body)
                self.static_cache_bytes += len(body)
        except Exception:
            pass

    async def _reader_response(self, response):
        target = urlsplit(response.url)
        if target.hostname != "wtr-lab.com" or target.path != "/api/reader/get":
            return
        try:
            params = json.loads(response.request.post_data or "{}")
            number = params.get("chapter_no")
            payload = await response.json()
            if type(number) is not int or not isinstance(payload, dict):
                return
            active = getattr(self, "active_novel_path", None)
            if active and params.get("raw_id") is not None and str(params["raw_id"]) != active.split("/")[3]:
                return
            if payload.get("requireTurnstile") or payload.get("turnstile_required"):
                self.verification_seen.add(number)
                emit("reader_verification_requested", chapter=number)
            if payload.get("code") in (1401, "1401", "CHAPTER_LOCKED"):
                self.reader_errors[number] = "account" if str(payload["code"]) == "1401" else "locked"
        except Exception:
            pass

    async def _read_chapter(self, number: int, novel_path: str = NOVEL_PATH) -> str:
        if getattr(self, "active_novel_path", None) != novel_path:
            self.active_novel_path = novel_path
            self.verification_seen.clear()
            self.verification_completed.clear()
            self.reader_errors.clear()
        self.reader_activity = {"bookId": novel_path.split("/")[3], "chapter": number, "phase": "navigating"}
        target = f"{novel_path}/chapter-{number}"
        if urlsplit(self.page.url).path != target:
            # Use the site's own Next link when present so its reader/verification UI runs normally.
            absolute = ORIGIN + target
            links = self.page.locator(f'a[href="{target}"]:visible, a[href^="{target}?"]:visible, '
                                      f'a[href="{absolute}"]:visible, a[href^="{absolute}?"]:visible')
            if await links.count():
                emit("reader_navigation", chapter=number, method="site_link")
                await links.first.click(timeout=15_000)
            else:
                emit("reader_navigation", chapter=number, method="page_navigation")
                await self.page.goto(ORIGIN + target, wait_until="domcontentloaded", timeout=180_000)
        deadline = time.monotonic() + 180
        waiting_reported = False
        while time.monotonic() < deadline:
            if urlsplit(self.page.url).hostname != "wtr-lab.com":
                raise SourceError("Reader navigated away from WTR-LAB")
            text = "\n\n".join(await self.page.locator(f"#chapter-{number} .chapter-body").all_inner_texts())
            state = reader_state(await self.page.title(), text)
            error = self.reader_errors.get(number)
            if error in {"account", "locked"} or state in {"account", "locked"}:
                raise SourceError("WTR-LAB reader requires account access or chapter unlocking; job stopped")
            if state == "text":
                if not urlsplit(self.page.url).path.startswith(novel_path + "/chapter-"):
                    raise SourceError("Reader returned text from an unexpected novel")
                if (number in self.verification_seen or waiting_reported) and number not in self.verification_completed:
                    emit("reader_verification_completed", chapter=number, completion="site_ui_without_scripted_clicks")
                    self.verification_completed.add(number)
                self.reader_activity = {**self.reader_activity, "phase": "idle"}
                return text
            if (state == "verification" or number in self.verification_seen) and not waiting_reported:
                emit("reader_waiting_for_site_verification", chapter=number)
                waiting_reported = True
                self.reader_activity = {**self.reader_activity, "phase": "verifying"}
            # Let the site's widget and callback complete naturally. Never click or inject a token.
            await asyncio.sleep(2)
        raise SourceError(f"WTR-LAB reader did not complete verification/rendering for chapter {number}; saved chapters retained")

    def read_chapter(self, number: int, novel_path: str = NOVEL_PATH) -> str:
        future = asyncio.run_coroutine_threadsafe(self._read_chapter(number, novel_path), self.loop)
        try:
            return future.result(timeout=390)
        except SourceError:
            self.reader_activity = {**(self.reader_activity or {}), "phase": "error"}
            raise
        except Exception:
            future.cancel()
            self.reader_activity = {**(self.reader_activity or {}), "phase": "error"}
            raise SourceError("WTR-LAB reader navigation failed; saved chapters retained") from None

    async def _request(self, method: str, target: str, body: dict | None, max_bytes: int, accept: str):
        if self.page.url == "about:blank":
            emit("browser_bootstrap", chapter=1)
            await self.page.goto(ORIGIN + NOVEL_PATH + "/chapter-1", wait_until="domcontentloaded", timeout=180_000)
            if self.reader_ui:
                await self._read_chapter(1)
        if urlsplit(self.page.url).hostname != "wtr-lab.com":
            raise SourceError("WTR-LAB browser navigated away from its configured origin")
        return await self.page.evaluate(FETCH, {"url": target, "method": method, "body": body, "limit": max_bytes, "accept": accept})

    def request(self, method: str, target: str, body: dict | None, max_bytes: int, accept: str) -> dict:
        future = asyncio.run_coroutine_threadsafe(self._request(method, target, body, max_bytes, accept), self.loop)
        try:
            return future.result(timeout=390 if self.reader_ui else 330)
        except Exception:
            future.cancel()
            raise SourceError("WTR-LAB browser request failed or timed out; saved chapters were retained") from None

    def close(self):
        if self.thread.is_alive():
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.stopped.set)
            self.thread.join(timeout=30)


class BrowserArchiveSource(WtrLabSource):
    """Existing WTR contracts/glossary over browser fetch, bounded to a test range."""

    def __init__(self, session_cookie: str, chapter_limit: int, *, browser_path: str | None = None,
                 transport=None, reader_ui: bool = False, engine: str = "camoufox", headed: bool = False,
                 stealth: bool = False, blink_flag: bool = False, chapter_delay: float = 0):
        cookie = account_cookie(session_cookie)
        super().__init__(access_authorized=True, session_cookie=f"{cookie['name']}={cookie['value']}")
        self.chapter_limit = chapter_limit
        self._remember_slug(BOOK_ID, NOVEL_PATH.rsplit("/", 1)[1])
        self.browser = transport or BrowserTransport(cookie, browser_path, 200 + chapter_limit * 30,
                                                      engine=engine, headed=headed, stealth=stealth, blink_flag=blink_flag,
                                                      reader_ui=reader_ui)
        self.reader_ui, self.chapter_delay = reader_ui, chapter_delay
        self.last_attempted_chapter = None
        self.fetched_chapters = []

    def _request_url(self, method: str, target: str, *, body: dict | None = None,
                     max_bytes: int, accept: str, redirect_host: str | None = None) -> bytes:
        target = self._guard_url(target)
        if urlsplit(target).hostname != "wtr-lab.com":
            raise SourceError("Browser archive test only supports same-origin WTR-LAB requests")
        with self._lock:
            response = self.browser.request(method, target, body, max_bytes, accept)
        text = response.get("text", "")
        lower = text.casefold()
        if response.get("challenge") or "<title>just a moment" in lower or "<title>attention required" in lower:
            raise SourceError("WTR-LAB browser verification required; job stopped and saved chapters retained")
        status = response.get("status")
        if status == 401:
            raise self._account_error()
        if status != 200:
            raise SourceError(f"WTR-LAB browser returned HTTP {status}; job stopped and saved chapters retained")
        encoded = text.encode("utf-8")
        if len(encoded) > max_bytes:
            raise SourceError("WTR-LAB browser response is too large")
        return encoded

    def details(self, book_id: str) -> dict:
        result = super().details(book_id)
        # This diagnostic focuses on chapter jobs; covers are not part of its request budget.
        result["coverUrl"] = ""
        return result

    def chapters(self, book_id: str) -> list[dict]:
        selected = super().chapters(book_id)[:self.chapter_limit]
        if ([row["id"] for row in selected] != [f"chapter-{n}" for n in range(1, self.chapter_limit + 1)]
                or any(not row["downloadable"] for row in selected)):
            raise SourceError("The selected test range is incomplete or contains locked chapters")
        return selected

    def text(self, book_id: str, chapter_id: str) -> str:
        self.last_attempted_chapter = chapter_id
        if self.chapter_delay:
            time.sleep(self.chapter_delay)
        if self.reader_ui:
            if book_id != BOOK_ID or not chapter_id.startswith("chapter-"):
                raise SourceError("Unsupported reader UI test chapter")
            with self._lock:
                text = self.browser.read_chapter(int(chapter_id.removeprefix("chapter-")))
            text = clean_chapter_text(self.id, text)
        else:
            text = super().text(book_id, chapter_id)
        self.fetched_chapters.append(chapter_id)
        return text

    def close(self):
        self.browser.close()
        self.client.close()


def run_job(store: ArchiveStore, source: BrowserArchiveSource, timeout: int) -> dict:
    """Use the real queue, leases and atomic writes; retry only explicitly on rerun."""
    prior = next((job for job in store.jobs() if job["source"] == source.id and job["book_id"] == BOOK_ID
                  and job["status"] in {"failed", "paused", "queued"}), None)
    if prior and prior["status"] != "queued":
        store.control(prior["id"], "resume" if prior["status"] == "paused" else "retry")
    queued = prior or store.enqueue(source.id, BOOK_ID)
    worker = ArchiveWorker(store, [source], concurrency=1)
    worker.start()
    deadline, previous = time.monotonic() + timeout, None
    try:
        while True:
            job = store.job(queued["id"])
            snapshot = job["status"], job["downloaded"], job["total"], job["error"]
            if snapshot != previous:
                emit("archive_progress", job_id=job["id"], status=job["status"], downloaded=job["downloaded"],
                     total=job["total"], error=job["error"])
                previous = snapshot
            if job["status"] in {"completed", "failed", "paused", "cancelled"}:
                return job
            if time.monotonic() >= deadline:
                store.control(job["id"], "pause")
                emit("archive_timeout", saved_chapters=job["downloaded"])
                return store.job(job["id"])
            time.sleep(1)
    finally:
        worker.stop()
        store.unfollow(source.id, BOOK_ID)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", required=True, help="Dedicated test archive; never use the production archive")
    parser.add_argument("--chapters", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--browser-path")
    parser.add_argument("--resume", action="store_true", help="Explicitly resume an existing test archive/checkpoint")
    parser.add_argument("--reader-ui", action="store_true", help="Read rendered chapters through the normal site UI")
    parser.add_argument("--engine", choices=("camoufox", "chromium", "cloakbrowser"), default="camoufox")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--stealth", action="store_true", help="Apply playwright-stealth to plain Chromium only")
    parser.add_argument("--blink-flag", action="store_true", help="Set AutomationControlled flag on plain Chromium only")
    parser.add_argument("--chapter-delay", type=float, default=0, help="Additional delay before each chapter (0–60 seconds)")
    args = parser.parse_args(argv)
    if not 1 <= args.chapters <= 100 or not 60 <= args.timeout <= 3600:
        parser.error("Use 1–100 chapters and a 60–3600 second timeout")
    if not 0 <= args.chapter_delay <= 60:
        parser.error("--chapter-delay must be between 0 and 60")
    if (args.stealth or args.blink_flag) and args.engine != "chromium":
        parser.error("Stealth/AutomationControlled options apply only to the plain Chromium comparison")
    load_dotenv(Path.cwd() / ".env", override=False)
    root = Path(args.archive_dir).resolve()
    if not root.name.startswith("camoufox-archive-test"):
        parser.error("The dedicated output directory name must start with camoufox-archive-test")
    if (root / "archive.sqlite3").exists() and not args.resume:
        parser.error("Use a fresh test directory, or --resume to keep existing saved chapters")
    source = None
    try:
        if os.getenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "").lower() not in {"1", "true", "yes"}:
            raise SourceError("Explicit WTR-LAB source authorization is required")
        if sys.platform == "linux" and args.engine == "camoufox" and not args.headed and not shutil.which("Xvfb"):
            raise SourceError("Install Xvfb on the AMP host before running this test")
        if args.engine == "cloakbrowser" and not os.getenv("CLOAKBROWSER_LICENSE_KEY"):
            raise SourceError("Set your CloakBrowser license key privately in CLOAKBROWSER_LICENSE_KEY")
        emit("archive_probe_starting", chapters=args.chapters, platform=sys.platform, engine=args.engine,
             reader_ui=args.reader_ui, headed=args.headed, stealth=args.stealth, chapter_delay=args.chapter_delay)
        source = BrowserArchiveSource(os.getenv("QUIETLEAF_WTRLAB_COOKIE", ""), args.chapters,
                                      browser_path=args.browser_path, reader_ui=args.reader_ui, engine=args.engine,
                                      headed=args.headed, stealth=args.stealth, blink_flag=args.blink_flag,
                                      chapter_delay=args.chapter_delay)
        store = ArchiveStore(root)
        job = run_job(store, source, args.timeout)
        texts = [store.text(source.id, BOOK_ID, f"chapter-{number}") or "" for number in range(1, args.chapters + 1)]
        saved = sum(bool(text) for text in texts)
        distinct = len({hashlib.sha256(text.encode("utf-8")).digest() for text in texts if text})
        minimum = min(map(len, texts))
        success = job["status"] == "completed" and job["downloaded"] == saved == distinct == args.chapters and minimum >= 500
        emit("archive_probe_complete", success=success, saved_chapters=saved, job_status=job["status"],
             fetched_this_run=len(source.fetched_chapters), last_attempted=source.last_attempted_chapter,
             distinct_chapters=distinct, min_characters=minimum,
             verification_requested=len(getattr(source.browser, "verification_seen", set())),
             verification_completed=len(getattr(source.browser, "verification_completed", set())))
        return 0 if success else 1
    except (SourceError, ValueError) as exc:
        # Browser failures are converted to fixed messages by the transport above.
        emit("archive_probe_failed", reason=str(exc))
    except Exception as exc:
        emit("archive_probe_failed", reason="browser_or_archive_error", error_type=type(exc).__name__)
    finally:
        if source:
            source.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
