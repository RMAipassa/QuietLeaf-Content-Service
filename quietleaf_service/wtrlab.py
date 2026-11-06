from __future__ import annotations

import base64
import copy
import json
import re
import threading
import time
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .adapters import MAX_TEXT_BYTES, HtmlSource, SourceError, require_id
from .content import clean_chapter_text
from .metadata import display_title


MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_CHAPTERS = 10_000
GLOSSARY_KEY = bytes.fromhex("16c3e08f3a5fbccd2742b7393ab3c10fe764b06199fa570195d70f5d2a0e84f1")
GLOSSARY_MARKER = re.compile(r"»([A-Za-z0-9_-]{1,64})⛬")
CHAPTER_ID = re.compile(r"chapter-([1-9][0-9]*)")
BOOK_ID_QUERY = re.compile(r"(?:#|id:\s*)?([0-9]+)", re.I)
GENRES = {
    number: label.replace("-", " ").title()
    for number, label in enumerate((
        "action", "adult", "adventure", "comedy", "drama", "ecchi", "erciyuan",
        "fan-fiction", "fantasy", "game", "gender-bender", "harem", "historical",
        "horror", "josei", "martial-arts", "mature", "mecha", "military", "mystery",
        "psychological", "romance", "school-life", "sci-fi", "seinen", "shoujo",
        "shoujo-ai", "shounen", "shounen-ai", "slice-of-life", "smut", "sports",
        "supernatural", "tragedy", "urban-life", "wuxia", "xianxia", "xuanhuan",
        "yaoi", "yuri",
    ), start=1)
}


class WtrLabBookNotFound(SourceError):
    pass


class WtrLabSource(HtmlSource):
    HOME_PATHS = {
        "popular": ("/en/ranking/all_time", "list"),
        "completed": ("/en/novel-list?status=completed", "series"),
        "fresh": ("/en/novel-list?orderBy=update&order=desc", "series"),
        "new": ("/en/novel-list?orderBy=date&order=desc", "series"),
        "hot": ("/en/trending", "list"),
    }

    def __init__(self, base_url: str = "https://wtr-lab.com", client: httpx.Client | None = None,
                 access_authorized: bool = False):
        if not access_authorized:
            raise SourceError("WTR-LAB requires explicit access authorization")
        super().__init__({
            "id": "wtr-lab",
            "name": "WTR-LAB",
            "baseUrl": base_url,
            "accessAuthorized": True,
            "minIntervalSeconds": 2,
            "selectors": {},
            "paths": {},
            "allowedImageHosts": ["img.wtr-lab.com"],
        }, client=client)
        self.agent = "QuietLeafPersonalReader/0.4"
        self._slugs: dict[str, str] = {}
        self._slug_lock = threading.Lock()
        self._home_cache: dict[str, list[dict]] | None = None
        self._home_lock = threading.Lock()

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 max_bytes: int = MAX_JSON_BYTES, accept: str = "application/json") -> bytes:
        target = self._guard_url(path)
        return self._request_url(method, target, body=body, max_bytes=max_bytes, accept=accept)

    def _request_url(self, method: str, target: str, *, body: dict | None = None,
                     max_bytes: int, accept: str, redirect_host: str | None = None) -> bytes:
        redirected_to = ""
        with self._lock:
            delay = self.min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            headers = {"User-Agent": self.agent, "Accept": accept}
            with self.client.stream(method, target, headers=headers, json=body) as response:
                if response.is_redirect:
                    location = response.headers.get("Location", "")
                    redirected = urlsplit(location)
                    if (not redirect_host or method != "GET" or redirected.scheme != "https"
                            or redirected.hostname != redirect_host or redirected.port not in (None, 443)
                            or redirected.username or redirected.password or redirected.query or redirected.fragment
                            or not redirected.path.startswith("/") or len(location) > 2048):
                        raise SourceError("WTR-LAB redirect blocked; the integration may need an update")
                    redirected_to = redirected.geturl()
                elif response.status_code != 200:
                    raise SourceError(f"WTR-LAB returned HTTP {response.status_code}")
                content = bytearray()
                if not redirected_to:
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise SourceError("WTR-LAB response is too large")
        if redirected_to:
            return self._request_url(
                "GET", redirected_to, max_bytes=max_bytes, accept=accept, redirect_host=None,
            )
        return bytes(content)

    def _json(self, method: str, path: str, body: dict | None = None,
              max_bytes: int = MAX_JSON_BYTES) -> dict:
        try:
            payload = json.loads(self._request(method, path, body=body, max_bytes=max_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SourceError("WTR-LAB returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SourceError("WTR-LAB returned an unexpected response")
        if payload.get("requireTurnstile") or payload.get("turnstile_required"):
            raise SourceError("WTR-LAB requires interactive verification; QuietLeaf will not bypass it")
        if payload.get("success") is False:
            message = payload.get("error")
            if message == "Novel not found":
                raise WtrLabBookNotFound("WTR-LAB book was not found")
            raise SourceError(message if isinstance(message, str) and message else "WTR-LAB rejected the request")
        return payload

    def _next_data(self, path: str, key: str) -> list[dict] | dict:
        html = self._request("GET", path, max_bytes=2 * 1024 * 1024, accept="text/html")
        page = BeautifulSoup(html, "html.parser")
        script = page.select_one("script#__NEXT_DATA__")
        try:
            payload = json.loads(script.string if script else "")
            data = payload["props"]["pageProps"][key]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SourceError("WTR-LAB page layout has changed") from exc
        if not isinstance(data, (list, dict)):
            raise SourceError("WTR-LAB page layout has changed")
        return data

    @staticmethod
    def _book_id(value: object) -> str:
        book_id = require_id(str(value))
        if not book_id.isdigit() or book_id.startswith("0"):
            raise SourceError("Invalid WTR-LAB book identifier")
        return book_id

    def _remember_slug(self, book_id: str, value: object) -> None:
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]{1,200}", value):
            return
        with self._slug_lock:
            self._slugs[book_id] = value

    def image_url(self, raw: str) -> str:
        target = super().image_url(raw)
        parsed = urlsplit(target)
        if parsed.hostname == "img.wtr-lab.com" and parsed.path.startswith("/cdn/") and not parsed.query:
            source = quote(parsed.path.removeprefix("/cdn/"), safe="/")
            return self._guard_url(f"/api/v2/img?src={source}")
        return target

    def _slug(self, book_id: str) -> str:
        with self._slug_lock:
            cached = self._slugs.get(book_id)
        if cached:
            return cached
        payload = self._json("POST", "/api/novel/get-by-raw-id", {"raw_id": int(book_id)})
        novel = payload.get("novel")
        if not isinstance(novel, dict):
            raise SourceError("WTR-LAB returned invalid book metadata")
        if str(novel.get("raw_id")) != book_id:
            raise SourceError("WTR-LAB returned mismatched book metadata")
        slug = novel.get("slug")
        self._remember_slug(book_id, slug)
        with self._slug_lock:
            resolved = self._slugs.get(book_id)
        if not resolved:
            raise SourceError("WTR-LAB returned an invalid book slug")
        return resolved

    @staticmethod
    def _plain_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        page = BeautifulSoup(value, "html.parser")
        for node in page(["script", "style"]):
            node.decompose()
        return page.get_text("\n", strip=True)

    def _book(self, record: dict) -> dict:
        book_id = self._book_id(record.get("raw_id"))
        self._remember_slug(book_id, record.get("slug"))
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        title = data.get("title") or record.get("title") or raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SourceError("WTR-LAB returned a book without a title")
        author = data.get("author") or record.get("author") or raw.get("author") or "Unknown"
        image = data.get("image") or record.get("image") or ""
        try:
            cover_url = self.image_url(image) if image else ""
        except SourceError:
            cover_url = ""
        genre_ids = record.get("genres") if isinstance(record.get("genres"), list) else []
        genres = [GENRES[number] for number in genre_ids if isinstance(number, int) and number in GENRES]
        result = {
            "id": book_id,
            "title": display_title(self.id, title),
            "author": str(author).strip() or "Unknown",
            "description": self._plain_text(data.get("description", record.get("description", ""))),
            "coverUrl": cover_url,
            "genre": ", ".join(genres),
        }
        chapter_count = record.get("chapter_count")
        if isinstance(chapter_count, int) and chapter_count >= 0:
            result["chapterCount"] = chapter_count
        return result

    def _books(self, records: object, genre: str = "") -> list[dict]:
        if not isinstance(records, list):
            raise SourceError("WTR-LAB returned an invalid book list")
        wanted = genre.strip().casefold()
        books = []
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                book = self._book(record)
            except SourceError:
                continue
            if book["id"] in seen or (wanted and wanted not in book["genre"].casefold()):
                continue
            seen.add(book["id"])
            books.append(book)
            if len(books) == 100:
                break
        return books

    def search(self, query: str, genre: str = "") -> list[dict]:
        text = query.strip()[:128]
        if not text:
            return self.recent()
        id_query = BOOK_ID_QUERY.fullmatch(text)
        if id_query:
            book_id = str(int(id_query.group(1)))
            if book_id == "0":
                return []
            try:
                book = self.details(book_id)
            except WtrLabBookNotFound:
                return []
            topic = genre.strip().casefold()
            return [book] if not topic or topic in book["genre"].casefold() else []
        if sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in text) < 3:
            return []
        payload = self._json("POST", "/api/search", {"text": text})
        return self._books(payload.get("data"), genre)

    def recent(self) -> list[dict]:
        return self._books(self._next_data("/en/novel-list?orderBy=update&order=desc", "series"))

    def home_feed(self) -> dict[str, list[dict]]:
        with self._home_lock:
            if self._home_cache is None:
                raise SourceError("Home feed has not been refreshed")
            return copy.deepcopy(self._home_cache)

    def refresh_home_feed(self, stop_event: threading.Event | None = None) -> None:
        feed = {}
        for category, (path, key) in self.HOME_PATHS.items():
            if stop_event is not None and stop_event.is_set():
                return
            feed[category] = self._books(self._next_data(path, key))[:20]
        if stop_event is not None and stop_event.is_set():
            return
        with self._home_lock:
            if stop_event is not None and stop_event.is_set():
                return
            self._home_cache = feed

    def details(self, book_id: str) -> dict:
        book_id = self._book_id(book_id)
        slug = self._slug(book_id)
        record = self._next_data(f"/en/novel/{book_id}/{quote(slug, safe='')}", "serie")
        if not isinstance(record, dict):
            raise SourceError("WTR-LAB returned invalid book details")
        data = record.get("serie_data")
        if not isinstance(data, dict) or str(data.get("raw_id")) != book_id:
            raise SourceError("WTR-LAB returned mismatched book details")
        return self._book(data)

    def chapters(self, book_id: str) -> list[dict]:
        book_id = self._book_id(book_id)
        count = self.details(book_id).get("chapterCount")
        if not isinstance(count, int):
            raise SourceError("WTR-LAB did not provide a chapter count")
        if count > MAX_CHAPTERS:
            raise SourceError("WTR-LAB chapter count exceeds the supported limit")
        chapters = []
        seen = set()
        for start in range(1, count + 1, 250):
            payload = self._json("GET", f"/api/chapters/{book_id}?start={start}&end={min(count, start + 249)}")
            page = payload.get("chapters", payload.get("data"))
            if isinstance(page, dict):
                page = page.get("chapters")
            if not isinstance(page, list):
                raise SourceError("WTR-LAB returned an invalid chapter list")
            for row in page:
                if not isinstance(row, dict):
                    continue
                number = row.get("order", row.get("chapter_no"))
                if not isinstance(number, int) or number < 1 or number in seen:
                    continue
                seen.add(number)
                title = row.get("title")
                if not isinstance(title, str) or not title.strip():
                    title = f"Chapter {number}"
                locked = row.get("locked") is True or row.get("is_locked") is True
                chapters.append({
                    "id": f"chapter-{number}",
                    "title": display_title(self.id, title),
                    "ordinal": 0,
                    "downloadable": not locked,
                    "_number": number,
                })
        chapters.sort(key=lambda chapter: chapter["_number"])
        for ordinal, chapter in enumerate(chapters):
            chapter["ordinal"] = ordinal
            chapter.pop("_number")
        if len(chapters) != count:
            raise SourceError("WTR-LAB returned an incomplete chapter list")
        return chapters

    @staticmethod
    def _decode_glossary(entries: object) -> dict[str, str]:
        if not isinstance(entries, dict):
            return {}
        decoded = {}
        cipher = AESGCM(GLOSSARY_KEY)
        for marker, pair in entries.items():
            if not isinstance(marker, str) or not isinstance(pair, list) or not pair:
                continue
            value = pair[0] if isinstance(pair[0], str) else ""
            encrypted = pair[1] if len(pair) > 1 and isinstance(pair[1], dict) else None
            if not value and encrypted:
                try:
                    iv = base64.b64decode(encrypted["iv"], validate=True)
                    ciphertext = base64.b64decode(encrypted["ct"], validate=True)
                    value = cipher.decrypt(iv, ciphertext, None).decode("utf-8")
                except (KeyError, TypeError, ValueError, UnicodeDecodeError, InvalidTag):
                    value = ""
            if value:
                decoded[marker] = value
        return decoded

    def text(self, book_id: str, chapter_id: str) -> str:
        book_id = self._book_id(book_id)
        match = CHAPTER_ID.fullmatch(require_id(chapter_id))
        if not match:
            raise SourceError("Invalid WTR-LAB chapter identifier")
        number = int(match.group(1))
        payload = self._json("POST", "/api/reader/get", {
            "translate": "ai",
            "language": "en",
            "raw_id": int(book_id),
            "chapter_no": number,
            "retry": False,
            "force_retry": False,
        })
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if data.get("requireTurnstile") or data.get("turnstile_required"):
            raise SourceError("WTR-LAB requires interactive verification; QuietLeaf will not bypass it")
        content_url = data.get("content_url")
        if not isinstance(content_url, str):
            raise SourceError("WTR-LAB did not provide chapter content")
        target = self._guard_url(content_url)
        content_path = urlsplit(target).path
        if content_path != "/api/v2/reader/content" and not content_path.startswith("/api/v2/reader/content/"):
            raise SourceError("WTR-LAB returned an unsafe content URL")
        content = self._json("GET", target)
        outer = content.get("data")
        chapter = outer.get("data") if isinstance(outer, dict) else None
        if not isinstance(chapter, dict):
            raise SourceError("WTR-LAB returned invalid chapter content")
        body = chapter.get("body")
        if not isinstance(body, list):
            raise SourceError("WTR-LAB returned invalid chapter text")
        glossary = chapter.get("glossary_data")
        if not isinstance(glossary, dict) and isinstance(outer, dict):
            glossary = outer.get("glossary_data")
        terms = glossary.get("terms") if isinstance(glossary, dict) else glossary
        replacements = self._decode_glossary(terms)
        paragraphs = []
        for item in body:
            if not isinstance(item, str):
                continue
            hydrated = GLOSSARY_MARKER.sub(lambda found: replacements.get(found.group(1), found.group(0)), item)
            value = self._plain_text(hydrated)
            if value:
                paragraphs.append(value)
        text = clean_chapter_text(self.id, "\n\n".join(paragraphs))
        if not text:
            raise SourceError("WTR-LAB returned empty chapter text")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise SourceError("Chapter is too large")
        return text

    def cover_image(self, cover_url: str) -> tuple[str, bytes]:
        target = self.image_url(cover_url)
        binary = self._request_url(
            "GET", target, max_bytes=2 * 1024 * 1024,
            accept="image/jpeg,image/png,image/webp,image/gif", redirect_host="img.wtr-lab.com",
        )
        if binary.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", binary
        if binary.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", binary
        if binary[:4] == b"RIFF" and binary[8:12] == b"WEBP":
            return "image/webp", binary
        if binary.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", binary
        raise SourceError("Unsupported cover image format")
