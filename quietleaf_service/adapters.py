from __future__ import annotations

import json
import re
import time
import threading
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_TEXT_BYTES = 2 * 1024 * 1024


class SourceError(Exception):
    pass


class Source(Protocol):
    id: str
    name: str

    def search(self, query: str, genre: str = "") -> list[dict]: ...
    def recent(self) -> list[dict]: ...
    def details(self, book_id: str) -> dict: ...
    def chapters(self, book_id: str) -> list[dict]: ...
    def text(self, book_id: str, chapter_id: str) -> str: ...


def require_id(value: str) -> str:
    if not ID.fullmatch(value):
        raise SourceError("Invalid item identifier")
    return value


class CatalogSource:
    def __init__(self, root: Path, source_id: str = "collection"):
        self.root = root.resolve()
        self.id = require_id(source_id)
        path = self.root / "catalog.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.name = raw.get("name", "Personal collection")
        self.books = {}
        for book in raw.get("books", []):
            book_id = require_id(book["id"])
            if book_id in self.books:
                raise ValueError("Duplicate book ID")
            for chapter in book.get("chapters", []):
                require_id(chapter["id"])
            ids = [chapter["id"] for chapter in book.get("chapters", [])]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate chapter ID")
            self.books[book_id] = book

    def _book(self, book_id: str) -> dict:
        try:
            return self.books[require_id(book_id)]
        except KeyError:
            raise SourceError("Book not found")

    def _metadata(self, book: dict) -> dict:
        return {
            "id": book["id"], "title": book["title"],
            "author": book.get("author", "Unknown"),
            "description": book.get("description", ""),
            "coverUrl": book.get("coverUrl", ""),
            "genre": book.get("genre", ""),
            "chapterCount": len(book.get("chapters", [])),
        }

    def search(self, query: str, genre: str = "") -> list[dict]:
        needle = query.casefold().strip()
        topic = genre.casefold().strip()
        return [self._metadata(book) for book in self.books.values()
                if (not needle or needle in (book["title"] + " " + book.get("author", "")).casefold())
                and (not topic or topic in book.get("genre", "").casefold())]

    def recent(self) -> list[dict]:
        return [self._metadata(book) for book in sorted(
            self.books.values(), key=lambda b: b.get("updated", ""), reverse=True)]

    def details(self, book_id: str) -> dict:
        return self._metadata(self._book(book_id))

    def chapters(self, book_id: str) -> list[dict]:
        book = self._book(book_id)
        return [{"id": ch["id"], "title": ch["title"], "ordinal": number,
                 "downloadable": bool(ch.get("file") and ch.get("copyPermitted", False))}
                for number, ch in enumerate(book.get("chapters", []))]

    def text(self, book_id: str, chapter_id: str) -> str:
        book = self._book(book_id)
        chapter = next((ch for ch in book.get("chapters", []) if ch["id"] == require_id(chapter_id)), None)
        if chapter is None:
            raise SourceError("Chapter not found")
        if not chapter.get("copyPermitted", False) or not chapter.get("file"):
            raise SourceError("This chapter was listed as metadata only and cannot be downloaded")
        candidate = (self.root / chapter["file"]).resolve()
        if not candidate.is_relative_to(self.root) or not candidate.is_file():
            raise SourceError("Invalid chapter file")
        if candidate.stat().st_size > MAX_TEXT_BYTES:
            raise SourceError("Chapter is too large")
        return candidate.read_text(encoding="utf-8")


class HtmlSource:
    def __init__(self, config: dict, client: httpx.Client | None = None):
        self.id = require_id(config["id"])
        self.name = config["name"]
        if config.get("accessAuthorized") is not True and config.get("publicPageParsingEnabled") is not True:
            raise SourceError("Explicit authorization or opt-in is required for HTML extraction")
        root = urlsplit(config["baseUrl"])
        if root.scheme != "https" or not root.hostname or root.port not in (None, 443) or root.username or root.password or root.query or root.fragment or root.path not in ("", "/"):
            raise SourceError("A trusted HTTPS origin is required")
        self.base = "https://" + root.netloc
        self.host = root.hostname.lower()
        self.allowed_image_hosts = {self.host} | {host.strip().lower() for host in config.get("allowedImageHosts", []) if host.strip()}
        if any(not re.fullmatch(r"[a-z0-9.-]+", host) or host.startswith(".") or host.endswith(".") for host in self.allowed_image_hosts):
            raise SourceError("Invalid allowed image hostname")
        self.selectors = config["selectors"]
        self.paths = config["paths"]
        self.client = client or httpx.Client(follow_redirects=False, timeout=12.0, trust_env=False)
        self.robots = RobotFileParser()
        self._image_robots = {}
        self._robots_fetched = float("-inf")
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.min_interval = max(1.0, float(config.get("minIntervalSeconds", 2)))
        self.agent = "QuietLeafSource/0.2 (+personal reader)"

    def _guard_url(self, path: str) -> str:
        target = urlsplit(urljoin(self.base + "/", path))
        if target.scheme != "https" or target.hostname != self.host or target.port not in (None, 443) or target.username or target.password:
            raise SourceError("An unsafe link or off-site redirect was rejected")
        return target.geturl()

    def image_url(self, raw: str) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise SourceError("Missing cover image URL")
        target = urlsplit(urljoin(self.base + "/", raw.strip()))
        if (target.scheme != "https" or target.hostname not in self.allowed_image_hosts
                or target.port not in (None, 443) or target.username or target.password
                or target.fragment):
            raise SourceError("Untrusted cover image URL")
        return target.geturl()

    def cover_image(self, cover_url: str) -> tuple[str, bytes]:
        target = self.image_url(cover_url)
        image_host = urlsplit(target).hostname
        if image_host == self.host:
            if time.monotonic() - self._robots_fetched > 3600:
                self._page("/")
            rules = self.robots
        else:
            now = time.monotonic()
            cached = self._image_robots.get(image_host)
            if cached is None or now - cached[0] > 3600:
                contents = self._fetch(f"https://{image_host}/robots.txt", robots_request=True)
                rules = RobotFileParser()
                rules.parse(contents.splitlines())
                self._image_robots[image_host] = (now, rules)
            else:
                rules = cached[1]
        if not rules.can_fetch(self.agent, target):
            raise SourceError("robots.txt disallows the cover image")
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            with self.client.stream("GET", target, headers={"User-Agent": self.agent, "Accept": "image/jpeg,image/png,image/webp,image/gif"}) as response:
                if response.is_redirect or response.status_code != 200:
                    raise SourceError("Cover image is unavailable")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise SourceError("Cover image is too large")
        binary = bytes(data)
        if binary.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", binary
        if binary.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", binary
        if binary[:4] == b"RIFF" and binary[8:12] == b"WEBP":
            return "image/webp", binary
        if binary.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", binary
        raise SourceError("Unsupported cover image format")

    def _fetch(self, url: str, robots_request: bool = False) -> str:
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            response = self.client.get(url, headers={"User-Agent": self.agent, "Accept": "text/html"})
        if response.is_redirect:
            raise SourceError("Redirect blocked; verify domain ownership and change configuration manually")
        if response.status_code == 404 and robots_request:
            return ""
        if response.status_code != 200:
            raise SourceError(f"Upstream returned HTTP {response.status_code}")
        if len(response.content) > MAX_TEXT_BYTES:
            raise SourceError("Upstream response is too large")
        return response.text

    def _page(self, path: str) -> BeautifulSoup:
        target = self._guard_url(path)
        now = time.monotonic()
        if now - self._robots_fetched > 3600:
            robots = self._fetch(self._guard_url("/robots.txt"), robots_request=True)
            self.robots.parse(robots.splitlines())
            self._robots_fetched = time.monotonic()
        if not self.robots.can_fetch(self.agent, target):
            raise SourceError("robots.txt disallows this path")
        return BeautifulSoup(self._fetch(target), "html.parser")

    def _stable_id(self, url: str) -> str:
        path = urlsplit(self._guard_url(url)).path.strip("/")
        item = path.split("/")[-1]
        return require_id(item)

    def _node(self, el, css: str | None, default: str = "") -> str:
        item = el.select_one(css) if css else None
        return item.get_text(" ", strip=True) if item else default

    def _entries(self, html: BeautifulSoup) -> list[dict]:
        s = self.selectors
        found = []
        for node in html.select(s["result"]):
            link = node.select_one(s["resultLink"])
            if not link or not link.get("href"):
                continue
            href = self._guard_url(link["href"])
            cover_url = ""
            if s.get("resultCover"):
                element = node.select_one(s["resultCover"])
                if element:
                    try:
                        cover_url = self.image_url(element.get("data-src") or element.get("src") or "")
                    except SourceError:
                        pass
            found.append({"id": self._stable_id(href), "title": link.get_text(" ", strip=True),
                          "author": self._node(node, s.get("resultAuthor"), "Unknown"),
                          "description": "", "coverUrl": cover_url, "genre": ""})
        return found

    def search(self, query: str, genre: str = "") -> list[dict]:
        path = self.paths["search"].replace("{query}", quote(query[:100], safe=""))
        return self._entries(self._page(path))

    def recent(self) -> list[dict]:
        return self._entries(self._page(self.paths["recent"]))

    def details(self, book_id: str) -> dict:
        html = self._page(self.paths["book"].replace("{id}", require_id(book_id)))
        s = self.selectors
        cover_url = ""
        if s.get("cover"):
            element = html.select_one(s["cover"])
            if element:
                try:
                    cover_url = self.image_url(element.get("data-src") or element.get("src") or "")
                except SourceError:
                    pass
        return {"id": book_id, "title": self._node(html, s["title"]),
                "author": self._node(html, s.get("author"), "Unknown"),
                "description": self._node(html, s.get("description")),
                "genre": self._node(html, s.get("genre")), "coverUrl": cover_url}

    def chapters(self, book_id: str) -> list[dict]:
        html = self._page(self.paths["book"].replace("{id}", require_id(book_id)))
        result = []
        seen = set()
        for chapter in html.select(self.selectors["chapterLink"]):
            if chapter.get("href"):
                href = self._guard_url(chapter["href"])
                chapter_id = self._stable_id(href)
                if chapter_id in seen:
                    continue
                seen.add(chapter_id)
                result.append({"id": chapter_id, "title": chapter.get_text(" ", strip=True),
                               "ordinal": len(result), "downloadable": True})
        return result

    def text(self, book_id: str, chapter_id: str) -> str:
        require_id(book_id)
        html = self._page(self.paths["chapter"].replace("{id}", require_id(book_id)).replace("{chapter}", require_id(chapter_id)))
        content = html.select_one(self.selectors["chapterBody"])
        if content is None:
            raise SourceError("Chapter structure changed")
        for el in content.select("script,style,noscript,iframe"):
            el.decompose()
        return content.get_text("\n", strip=True)
