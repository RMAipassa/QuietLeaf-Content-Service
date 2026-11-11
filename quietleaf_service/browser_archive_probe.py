"""Bounded real ArchiveWorker job using one authenticated Camoufox session."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .adapters import SourceError
from .archive import ArchiveStore, ArchiveWorker
from .browser_probe import NOVEL_PATH, ORIGIN, RequestGate, account_cookie, emit
from .wtrlab import WtrLabSource


BOOK_ID = "51691"
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

    def __init__(self, cookie: dict, browser_path: str | None, max_requests: int):
        self.cookie = cookie
        self.browser_path = browser_path
        self.max_requests = max_requests
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self._run, name="wtrlab-browser", daemon=True)
        self.thread.start()
        try:
            self.ready.result(timeout=90)
        except Exception:
            raise SourceError("Camoufox could not start under the application service account") from None

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError("browser_start_failed"))

    async def _serve(self):
        from camoufox.async_api import AsyncCamoufox

        options = {"headless": "virtual" if sys.platform == "linux" else True}
        if self.browser_path:
            options["executable_path"] = self.browser_path
        async with AsyncCamoufox(**options) as browser:
            self.loop = asyncio.get_running_loop()
            self.stopped = asyncio.Event()
            self.context = await browser.new_context(service_workers="block")
            await self.context.add_cookies([self.cookie])
            self.gate = RequestGate(self.max_requests)
            await self.context.route("**/*", self.gate.route)
            self.page = await self.context.new_page()
            self.ready.set_result(None)
            await self.stopped.wait()
            await self.context.close()

    async def _request(self, method: str, target: str, body: dict | None, max_bytes: int, accept: str):
        if self.page.url == "about:blank":
            emit("browser_bootstrap", chapter=1)
            await self.page.goto(ORIGIN + NOVEL_PATH + "/chapter-1", wait_until="domcontentloaded", timeout=180_000)
        if urlsplit(self.page.url).hostname != "wtr-lab.com":
            raise SourceError("WTR-LAB browser navigated away from its configured origin")
        return await self.page.evaluate(FETCH, {"url": target, "method": method, "body": body, "limit": max_bytes, "accept": accept})

    def request(self, method: str, target: str, body: dict | None, max_bytes: int, accept: str) -> dict:
        future = asyncio.run_coroutine_threadsafe(self._request(method, target, body, max_bytes, accept), self.loop)
        try:
            return future.result(timeout=330)
        except Exception:
            future.cancel()
            raise SourceError("WTR-LAB browser request failed or timed out; saved chapters were retained") from None

    def close(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stopped.set)
            self.thread.join(timeout=30)


class BrowserArchiveSource(WtrLabSource):
    """Existing WTR contracts/glossary over browser fetch, bounded to a test range."""

    def __init__(self, session_cookie: str, chapter_limit: int, *, browser_path: str | None = None,
                 transport=None):
        cookie = account_cookie(session_cookie)
        super().__init__(access_authorized=True, session_cookie=f"{cookie['name']}={cookie['value']}")
        self.chapter_limit = chapter_limit
        self._remember_slug(BOOK_ID, NOVEL_PATH.rsplit("/", 1)[1])
        self.browser = transport or BrowserTransport(cookie, browser_path, 200 + chapter_limit * 8)
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
    args = parser.parse_args(argv)
    if not 1 <= args.chapters <= 100 or not 60 <= args.timeout <= 3600:
        parser.error("Use 1–100 chapters and a 60–3600 second timeout")
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
        if sys.platform == "linux" and not shutil.which("Xvfb"):
            raise SourceError("Install Xvfb on the AMP host before running this test")
        emit("archive_probe_starting", chapters=args.chapters, platform=sys.platform)
        source = BrowserArchiveSource(os.getenv("QUIETLEAF_WTRLAB_COOKIE", ""), args.chapters,
                                      browser_path=args.browser_path)
        store = ArchiveStore(root)
        job = run_job(store, source, args.timeout)
        texts = [store.text(source.id, BOOK_ID, f"chapter-{number}") or "" for number in range(1, args.chapters + 1)]
        saved = sum(bool(text) for text in texts)
        distinct = len({hashlib.sha256(text.encode("utf-8")).digest() for text in texts if text})
        minimum = min(map(len, texts))
        success = job["status"] == "completed" and job["downloaded"] == saved == distinct == args.chapters and minimum >= 500
        emit("archive_probe_complete", success=success, saved_chapters=saved, job_status=job["status"],
             fetched_this_run=len(source.fetched_chapters), last_attempted=source.last_attempted_chapter,
             distinct_chapters=distinct, min_characters=minimum)
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
