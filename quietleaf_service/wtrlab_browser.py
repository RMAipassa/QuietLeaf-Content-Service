"""Production WTR-LAB chapter reading through its normal authenticated UI."""
from __future__ import annotations

import importlib.util
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from .adapters import MAX_TEXT_BYTES, SourceError, require_id
from .browser_probe import account_cookie
from .content import clean_chapter_text
from .wtrlab import CHAPTER_ID, GLOSSARY_MARKER, WtrLabSource


def installed_browser_path(configured: str = "") -> str:
    if importlib.util.find_spec("camoufox") is None:
        raise SourceError("WTR-LAB reader UI needs the browser-test extra. Install .[browser-test] in AMP's Python environment.")
    if sys.platform == "linux" and shutil.which("Xvfb") is None:
        raise SourceError("WTR-LAB reader UI needs Xvfb on the AMP host. Install it before retrying this job.")
    try:
        if configured:
            path = Path(configured).resolve()
            if not path.is_file():
                raise ValueError("missing executable")
            return str(path)
        from camoufox.browser_pin import effective_pin, matches
        from camoufox.multiversion import list_installed, load_config
        from camoufox.pkgman import launch_path
        # Avoid the launcher's automatic download/cache migration during inspection.
        config = load_config()
        installed = [item for item in list_installed() if item.version.is_supported()]
        pin = effective_pin(config)
        if pin:
            installed = [item for item in installed if matches(pin, item.repo_name, item.version.version or "", item.version.build)]
        selected = next((item for item in installed if item.relative_path == config.get("active_version")),
                        installed[0] if installed else None)
        if selected is None:
            raise ValueError("no installed compatible browser")
        return launch_path(selected.path)
    except Exception:
        raise SourceError("WTR-LAB reader UI browser is not installed for the AMP service account. Run python -m quietleaf_service.browser_setup in that environment.") from None


def browser_preflight(configured: str = "") -> dict:
    """Safe inspection only: no browser launch, download, credentials or filesystem paths."""
    result = {"packageInstalled": importlib.util.find_spec("camoufox") is not None,
              "virtualDisplayAvailable": sys.platform != "linux" or shutil.which("Xvfb") is not None,
              "browserInstalled": False, "ready": False}
    try:
        installed_browser_path(configured)
        result["browserInstalled"] = True
        result["ready"] = True
    except SourceError as exc:
        result["message"] = str(exc)
    return result


class WtrLabReaderUiSource(WtrLabSource):
    chapter_text_mode = "reader-ui"

    def __init__(self, base_url: str = "https://wtr-lab.com", client=None, access_authorized: bool = False,
                 session_cookie: str = "", browser_path: str = "", browser_factory=None):
        super().__init__(base_url, client, access_authorized, session_cookie)
        origin = urlsplit(base_url)
        if origin.scheme != "https" or origin.hostname != "wtr-lab.com" or origin.port not in (None, 443):
            raise SourceError("WTR-LAB reader UI requires https://wtr-lab.com")
        try:
            self._browser_cookie = account_cookie(session_cookie)
        except ValueError:
            raise SourceError("WTR-LAB reader UI requires QUIETLEAF_WTRLAB_COOKIE with your signed-in account session") from None
        self._configured_browser_path = browser_path
        self._browser_factory = browser_factory
        self._browser = None
        self._browser_state = "not-started"
        self._browser_lock = threading.Lock()
        self._reader_lock = threading.Lock()
        self._closed = False

    @property
    def browser_state(self) -> str:
        return self._browser_state

    def browser_diagnostics(self) -> dict:
        # Snapshot scalars/replaced dictionaries; diagnostics must not wait for browser startup.
        browser = self._browser
        return {"state": self._browser_state,
                "activity": dict(getattr(browser, "reader_activity", None) or {}),
                "networkRequests": getattr(getattr(browser, "gate", None), "requests", 0),
                "verificationRequested": len(getattr(browser, "verification_seen", set())),
                "verificationCompleted": len(getattr(browser, "verification_completed", set()))}

    def _reserve_browser_request(self):
        # This is the same lock/clock used by the inherited HTTP metadata and cover paths.
        # Do not hold it around a reader operation: its asset requests need this gate too.
        with self._lock:
            delay = self.min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()

    def _get_browser(self):
        with self._browser_lock:
            if self._closed:
                raise SourceError("WTR-LAB reader UI is stopping")
            if self._browser is None:
                self._browser_state = "starting"
                try:
                    if self._browser_factory:
                        self._browser = self._browser_factory(self._browser_cookie, self._reserve_browser_request)
                    else:
                        # Shared with the diagnostics; production always supplies the real book path.
                        from .browser_archive_probe import BrowserTransport
                        path = installed_browser_path(self._configured_browser_path)
                        self._browser = BrowserTransport(self._browser_cookie, path, None, reader_ui=True,
                                                         reserve_request=self._reserve_browser_request)
                    self._browser_state = "running"
                except SourceError:
                    self._browser_state = "setup-or-start-error"
                    raise
                except Exception:
                    self._browser_state = "setup-or-start-error"
                    raise SourceError("WTR-LAB reader UI could not start under the AMP service account") from None
            return self._browser

    def text(self, book_id: str, chapter_id: str) -> str:
        book_id = self._book_id(book_id)
        match = CHAPTER_ID.fullmatch(require_id(chapter_id))
        if not match:
            raise SourceError("Invalid WTR-LAB chapter identifier")
        with self._reader_lock:
            slug = self._slug(book_id)
            path = f"/en/novel/{book_id}/{quote(slug, safe='')}"
            text = self._get_browser().read_chapter(int(match.group(1)), path)
            text = clean_chapter_text(self.id, text)
            if not text.strip() or GLOSSARY_MARKER.search(text):
                raise SourceError("WTR-LAB reader returned empty or unresolved chapter text; saved chapters retained")
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise SourceError("Chapter is too large")
            return text

    def close(self):
        with self._browser_lock:
            self._closed = True
            browser, self._browser = self._browser, None
            self._browser_state = "closed"
        try:
            if browser is not None:
                browser.close()
        finally:
            self.client.close()
