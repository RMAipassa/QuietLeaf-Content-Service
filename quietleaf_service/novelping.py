from __future__ import annotations

import copy
import re
import threading
import time
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .adapters import HtmlSource, SourceError, require_id


class NovelPingSource(HtmlSource):
    HOME_PATHS = {
        "popular": "/sort/popular",
        "completed": "/sort/complete",
        "fresh": "/sort/updates",
        "new": "/sort/popular?sort=NEW",
        "hot": "/sort/hot",
    }
    HOME_CACHE_SECONDS = 10 * 60

    def __init__(self, base_url: str = "https://novelping.com", search_path: str = "/search?keyword={query}", client=None,
                 image_hosts: tuple[str, ...] = (), access_authorized: bool = False):
        if not search_path.startswith("/") or not search_path.count("{query}") == 1 or search_path.startswith("//"):
            raise SourceError("Invalid search path")
        trusted_image_hosts = tuple(dict.fromkeys(("images.novelping.com", *image_hosts)))
        super().__init__({
            "id": "novelping",
            "name": "NovelPing (experimental)",
            "baseUrl": base_url,
            "publicPageParsingEnabled": True,
            "minIntervalSeconds": 2.5,
            "selectors": {},
            "paths": {},
            "allowedImageHosts": list(trusted_image_hosts),
        }, client=client)
        self.search_path = search_path
        self.agent = "QuietLeafPersonalReader/0.3"
        self.access_authorized = access_authorized
        self._home_cache = None
        self._home_cached_at = float("-inf")
        self._home_lock = threading.Lock()

    def _page(self, path: str) -> BeautifulSoup:
        target = self._guard_url(path)
        if not self.access_authorized or not urlsplit(target).path.startswith("/book/"):
            return super()._page(path)
        return BeautifulSoup(self._fetch(target), "html.parser")

    def _book_id(self, url: str) -> str | None:
        parts = urlsplit(self._guard_url(url)).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "book":
            return None
        try:
            return require_id(parts[1])
        except SourceError:
            return None

    def _book_entries(self, page: BeautifulSoup, query: str = "", genre: str = "") -> list[dict]:
        result = []
        seen = set()
        needle = query.strip().casefold()
        links = page.select('#search-results h3.novel-title > a[href*="/book/"]')
        if not links:
            links = page.select('a[href*="/book/"]')
        for link in links:
            href = link.get("href", "")
            try:
                book_id = self._book_id(href)
            except SourceError:
                continue
            if not book_id or book_id in seen:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                cover = link.select_one("img[alt]")
                title = cover.get("alt", "").strip() if cover else ""
            if not title or title.casefold() in {"read", "read now", "details", "more", "chapter list"}:
                continue
            wrapper = link.find_parent("div", class_="row") or link.find_parent(class_=re.compile("novel|book|item|card", re.I))
            context = wrapper or link.parent or link
            author = self._nearby_text(context, (".author", ".byline", '[class*="author"]')) or "Unknown"
            genre_text = self._nearby_text(context, (".genre", '[class*="genre"]'))
            if needle and needle not in f"{title} {author}".casefold():
                continue
            if genre and genre.casefold() not in genre_text.casefold():
                continue
            seen.add(book_id)
            cover_url = self._cover_url(context, search_result=True)
            result.append({
                "id": book_id,
                "title": title,
                "author": author,
                "description": "",
                "coverUrl": cover_url,
                "genre": genre_text,
            })
            if len(result) >= 100:
                break
        return result

    def _cover_url(self, page, search_result: bool = False) -> str:
        candidates = []
        if not search_result:
            for selector in (
                'meta[property="og:image"]', 'meta[name="twitter:image"]',
                'meta[itemprop="image"]',
            ):
                candidates.extend(page.select(selector))
        for selector in (
            'img.novel-cover-loading', 'img.cover',
            '.book-cover img', '.novel-cover img', '.cover img',
            '.book-img img', '[class*="cover"] img',
            '[class*="thumb"] img',
        ):
            candidates.extend(page.select(selector))
        if search_result:
            candidates.extend(page.select('a[href*="/book/"] img'))
        for element in candidates:
            for attr in ('data-src', 'data-original', 'data-lazy-src', 'content', 'src'):
                candidate = element.get(attr)
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                try:
                    return self.image_url(candidate)
                except SourceError:
                    continue
        return ""

    @staticmethod
    def _nearby_text(node, selectors: tuple[str, ...]) -> str:
        for selector in selectors:
            match = node.select_one(selector)
            if match:
                value = match.get_text(" ", strip=True)
                if value:
                    return value
        return ""

    def search(self, query: str, genre: str = "") -> list[dict]:
        if not query.strip():
            return self.recent()
        path = self.search_path.replace("{query}", quote(query[:100], safe=""))
        return self._book_entries(self._page(path), query, genre)

    def recent(self) -> list[dict]:
        return self._book_entries(self._page("/"))

    def home_feed(self) -> dict[str, list[dict]]:
        with self._home_lock:
            if self._home_cache is not None and time.monotonic() - self._home_cached_at < self.HOME_CACHE_SECONDS:
                return copy.deepcopy(self._home_cache)
            feed = {
                category: self._book_entries(self._page(path))[:20]
                for category, path in self.HOME_PATHS.items()
            }
            self._home_cache = feed
            self._home_cached_at = time.monotonic()
            return copy.deepcopy(feed)

    @staticmethod
    def _field(page: BeautifulSoup, label: str) -> str:
        pattern = re.compile(r"^" + re.escape(label) + r"\s*:\s*", re.I)
        for node in page.find_all(["dt", "li", "div", "span", "strong", "b", "h3", "p"]):
            if node.find(["dt", "li", "div", "span", "strong", "b", "h3", "p"]):
                continue
            value = node.get_text(" ", strip=True)
            if pattern.match(value):
                remainder = pattern.sub("", value).strip()
                if remainder:
                    return remainder
                sibling = node.find_next_sibling()
                if sibling:
                    return sibling.get_text(" ", strip=True)
                parent = node.parent
                if parent:
                    adjacent = node.find_next("a")
                    if adjacent and adjacent in parent.descendants:
                        return adjacent.get_text(" ", strip=True)
        return ""

    def details(self, book_id: str) -> dict:
        page = self._page(f"/book/{require_id(book_id)}")
        heading = page.select_one("h1") or page.select_one("h2")
        if not heading:
            raise SourceError("NovelPing book layout has changed")
        description = self._nearby_text(page, (
            "#novel-description-content", ".novel-description", "#description", ".description", '[class*="synopsis"]',
            '[class*="description"]',
        ))
        author_meta = page.select_one('meta[property="og:novel:author"]')
        genre_meta = page.select_one('meta[property="og:novel:genre"]')
        author = author_meta.get("content", "").strip() if author_meta else ""
        genre = genre_meta.get("content", "").strip() if genre_meta else ""
        return {
            "id": book_id,
            "title": heading.get_text(" ", strip=True),
            "author": author or self._field(page, "Author") or "Unknown",
            "description": description,
            "genre": ", ".join(part.strip().title() for part in genre.split(",") if part.strip()) or self._field(page, "Genre"),
            "coverUrl": self._cover_url(page),
        }

    def chapters(self, book_id: str) -> list[dict]:
        book_id = require_id(book_id)
        if self.access_authorized:
            target = self._guard_url(f"/ajax/chapter-archive?novelId={quote(book_id, safe='')}")
            page = BeautifulSoup(self._fetch(target), "html.parser")
        else:
            page = self._page(f"/book/{book_id}")
        prefix = f"/book/{book_id}/"
        chapters = {}
        for link in page.select('a[href*="/book/"]'):
            try:
                url = self._guard_url(link.get("href", ""))
                path = urlsplit(url).path
                if not path.startswith(prefix):
                    continue
                suffix = path[len(prefix):]
                if "/" in suffix or not suffix.startswith("chapter-"):
                    continue
                chapter_id = require_id(suffix)
            except SourceError:
                continue
            title = link.get_text(" ", strip=True) or chapter_id.replace("-", " ").title()
            number = re.match(r"chapter-(\d+)(?:-|$)", chapter_id, re.I)
            item = (int(number.group(1)) if number else None, chapter_id, title)
            existing = chapters.get(chapter_id)
            if existing is None or (existing[2].casefold() in {"read", "read now"} and
                                    title.casefold() not in {"read", "read now"}):
                chapters[chapter_id] = item
        if not chapters:
            raise SourceError("No chapter links found in the NovelPing chapter archive")
        chapters = list(chapters.values())
        if all(item[0] is not None for item in chapters):
            chapters.sort(key=lambda item: (item[0], item[1]))
        return [{"id": chapter_id, "title": title, "ordinal": index, "downloadable": True}
                for index, (_, chapter_id, title) in enumerate(chapters)]

    def text(self, book_id: str, chapter_id: str) -> str:
        book_id = require_id(book_id)
        chapter_id = require_id(chapter_id)
        if not chapter_id.startswith("chapter-"):
            raise SourceError("Invalid chapter identifier")
        page = self._page(f"/book/{book_id}/{chapter_id}")
        for selector in (
            "#chapter-content", "#chr-content", ".chapter-content", ".chapter-body", "#chapterBody",
            ".reading-content", ".read-content", '[class*="chapterContent"]',
            '[class*="chapter-content"]', "article",
        ):
            node = page.select_one(selector)
            if node is None:
                continue
            for child in node.select("script,style,noscript,iframe,nav,button,form,.advertisement,.ads"):
                child.decompose()
            chunks = [p.get_text(" ", strip=True) for p in node.select("p")]
            text = "\n\n".join(chunk for chunk in chunks if chunk)
            if len(text) < 80:
                text = node.get_text("\n", strip=True)
            if len(text) >= 80:
                return text
        raise SourceError("Chapter text container was not found or was empty")
