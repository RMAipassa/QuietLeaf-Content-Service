from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from PIL import Image, ImageOps

from .adapters import MAX_TEXT_BYTES, SourceError, require_id


MAX_UPLOAD_BYTES = 100_000_000
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_PAGES = 2000
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PDF_LOCK = threading.Lock()


def natural_key(name: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def zip_index(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    items = archive.infolist()
    if len(items) > 10000 or sum(item.file_size for item in items) > MAX_EXPANDED_BYTES:
        raise SourceError("The document archive is too large")
    result = {}
    for item in items:
        path = PurePosixPath(item.filename)
        if (path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename
                or item.flag_bits & 1 or item.file_size > MAX_ENTRY_BYTES):
            raise SourceError("The document contains an unsafe, encrypted, or oversized entry")
        if item.filename in result:
            raise SourceError("The document contains duplicate archive paths")
        result[item.filename] = item
    return result


def read_entry(archive: zipfile.ZipFile, name: str, limit: int = MAX_ENTRY_BYTES) -> bytes:
    if archive.getinfo(name).file_size > limit:
        raise SourceError("A document entry is too large")
    with archive.open(name) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SourceError("A document entry is too large")
    return data


def jpeg_page(data: bytes, max_edge: int = 2400) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000 or min(image.size) < 1:
                raise SourceError("The image page dimensions are too large")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_edge, max_edge))
            if image.mode in {"RGBA", "LA"} or image.info.get("transparency") is not None:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88)
            return output.getvalue()
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("The image page could not be read") from exc


def pdf_page(path: Path, index: int, max_edge: int = 2400) -> bytes:
    import pypdfium2 as pdfium

    try:
        with PDF_LOCK, pdfium.PdfDocument(str(path)) as document:
            if not 0 <= index < len(document):
                raise SourceError("Page not found")
            page = document[index]
            try:
                width, height = page.get_size()
                if min(width, height) <= 0:
                    raise SourceError("Invalid PDF page dimensions")
                bitmap = page.render(scale=min(2.0, max_edge / max(width, height)))
                try:
                    output = io.BytesIO()
                    bitmap.to_pil().convert("RGB").save(output, format="JPEG", quality=88)
                    return output.getvalue()
                finally:
                    bitmap.close()
            finally:
                page.close()
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("The PDF could not be opened; encrypted PDFs require an unlocked file") from exc


def resolve_epub(base: str, href: str) -> str | None:
    target = urlsplit(href)
    if target.scheme or target.netloc or target.path.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(target.path)))
    return None if path.startswith("../") or path == ".." or "\\" in path else path


def parse_epub(archive: zipfile.ZipFile, items: dict) -> dict:
    if "META-INF/encryption.xml" in items:
        encrypted = ElementTree.fromstring(read_entry(archive, "META-INF/encryption.xml", MAX_TEXT_BYTES))
        for node in encrypted.iter():
            if node.tag.rsplit("}", 1)[-1] == "CipherReference":
                path = unquote(urlsplit(node.attrib.get("URI", "")).path)
                if PurePosixPath(path).suffix.lower() not in {".ttf", ".otf", ".woff", ".woff2"}:
                    raise SourceError("This EPUB has encrypted content; upload an unlocked EPUB")
    container = ElementTree.fromstring(read_entry(archive, "META-INF/container.xml", MAX_TEXT_BYTES))
    rootfile = next((node.attrib.get("full-path") for node in container.iter() if node.tag.rsplit("}", 1)[-1] == "rootfile"), None)
    if rootfile not in items:
        raise SourceError("EPUB package was not found")
    package = ElementTree.fromstring(read_entry(archive, rootfile, MAX_TEXT_BYTES))
    manifest = {}
    title = author = ""
    for node in package.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "item":
            manifest[node.attrib.get("id")] = node.attrib
        elif tag == "title" and not title:
            title = "".join(node.itertext()).strip()
        elif tag == "creator" and not author:
            author = "".join(node.itertext()).strip()
    chapters, pages = [], []
    for node in package.iter():
        if node.tag.rsplit("}", 1)[-1] != "itemref":
            continue
        item = manifest.get(node.attrib.get("idref"), {})
        path = resolve_epub(rootfile, item.get("href", ""))
        if not path or path not in items:
            continue
        if item.get("media-type") in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            pages.append(path)
            continue
        if item.get("media-type") not in {"application/xhtml+xml", "text/html", "image/svg+xml"}:
            continue
        page = BeautifulSoup(read_entry(archive, path, MAX_TEXT_BYTES), "html.parser")
        for element in page.select("script, style, noscript, nav"):
            element.decompose()
        heading = page.select_one("h1, h2") or page.select_one("title")
        for image in page.find_all(["img", "image"]):
            href = image.get("src") or image.get("xlink:href") or image.get("href") or ""
            resolved = resolve_epub(path, href)
            if resolved in items and PurePosixPath(resolved).suffix.lower() in IMAGE_SUFFIXES:
                pages.append(resolved)
        for line_break in page.find_all("br"):
            line_break.replace_with("\n")
        body = page.body or page
        for block in body.find_all(["p", "div", "section", "h1", "h2", "h3", "h4", "li", "blockquote", "pre"]):
            block.append("\n\n")
        text = body.get_text("", strip=False)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = "\n".join(line.strip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            chapters.append({"title": heading.get_text(" ", strip=True) if heading else f"Chapter {len(chapters) + 1}", "text": text})
    if pages and sum(len(chapter["text"]) for chapter in chapters) <= max(1000, 100 * len(pages)):
        return {"mediaType": "epub-images", "pages": pages, "chapters": [], "title": title, "author": author}
    if not chapters:
        raise SourceError("The EPUB contains no readable chapters or image pages")
    cover = next((resolve_epub(rootfile, item.get("href", "")) for item in manifest.values()
                  if "cover-image" in item.get("properties", "").split()), None)
    if not cover:
        cover_id = next((node.attrib.get("content") for node in package.iter()
                         if node.tag.rsplit("}", 1)[-1] == "meta" and node.attrib.get("name") == "cover"), None)
        cover = resolve_epub(rootfile, manifest.get(cover_id, {}).get("href", "")) if cover_id else None
    return {"mediaType": "text", "chapters": chapters, "pages": [], "title": title, "author": author,
            "coverEntry": cover if cover in items else (pages[0] if pages else None)}


class PrivateLibrarySource:
    id = "private"
    name = "Private library"

    def __init__(self, root: Path, max_bytes: int = 10 * 1024 ** 3, max_upload_bytes: int = MAX_UPLOAD_BYTES):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "library.sqlite3"
        self.max_bytes = max_bytes
        self.max_upload_bytes = max_upload_bytes
        self.lock = threading.RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL, media_type TEXT NOT NULL,
                filename TEXT NOT NULL, extension TEXT NOT NULL, original_bytes INTEGER NOT NULL,
                stored_bytes INTEGER NOT NULL, created_at INTEGER NOT NULL, manifest TEXT NOT NULL)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _row(self, book_id: str):
        require_id(book_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise SourceError("Private library book not found")
        return row

    def _metadata(self, row) -> dict:
        manifest = json.loads(row["manifest"])
        return {"source": self.id, "id": row["id"], "title": row["title"], "author": row["author"],
                "description": "Uploaded to your private library.", "genre": "Owned books" if row["media_type"] == "text" else "Manga / illustrated books",
                "coverUrl": "", "coverArchived": (self.root / row["id"] / "cover.jpg").is_file(),
                "mediaType": row["media_type"], "pageCount": manifest.get("pageCount", 0),
                "chapterCount": len(manifest.get("chapters", [])) if row["media_type"] == "text" else 1,
                "originalName": row["filename"], "originalBytes": row["original_bytes"], "uploadedAt": row["created_at"]}

    def listing(self, query: str = "", offset: int = 0, limit: int = 100) -> dict:
        pattern = "%" + query.strip()[:100] + "%"
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM books WHERE title LIKE ? OR author LIKE ?", (pattern, pattern)).fetchone()[0]
            rows = db.execute("SELECT * FROM books WHERE title LIKE ? OR author LIKE ? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (pattern, pattern, min(100, limit), max(0, offset))).fetchall()
        return {"items": [self._metadata(row) for row in rows], "total": count, "maxUploadBytes": self.max_upload_bytes}

    def search(self, query: str, genre: str = "") -> list[dict]:
        return [book for book in self.listing(query)["items"] if not genre or genre.casefold() in book["genre"].casefold()]

    def recent(self) -> list[dict]:
        return self.search("")

    def home_feed(self) -> dict:
        books = self.recent()[:20]
        return {"popular": [], "completed": [], "fresh": books, "new": books, "hot": []}

    def details(self, book_id: str) -> dict:
        return self._metadata(self._row(book_id))

    def original(self, book_id: str) -> tuple[Path, str]:
        row = self._row(book_id)
        return self.root / book_id / ("original" + row["extension"]), row["filename"]

    def offline_document(self, book_id: str) -> tuple[Path, str]:
        row = self._row(book_id)
        if row["media_type"] == "epub-images":
            return self.root / book_id / "offline.cbz", Path(row["filename"]).stem + ".cbz"
        return self.original(book_id)

    def chapters(self, book_id: str) -> list[dict]:
        row = self._row(book_id)
        manifest = json.loads(row["manifest"])
        if row["media_type"] != "text":
            return [{"id": "document", "title": f"Complete volume · {manifest['pageCount']} pages", "ordinal": 0, "downloadable": True}]
        return [{"id": f"chapter-{number + 1}", "title": chapter["title"], "ordinal": number, "downloadable": True}
                for number, chapter in enumerate(manifest["chapters"])]

    def text(self, book_id: str, chapter_id: str) -> str:
        manifest = json.loads(self._row(book_id)["manifest"])
        if not re.fullmatch(r"chapter-[1-9][0-9]*", chapter_id):
            raise SourceError("Invalid private chapter identifier")
        index = int(chapter_id.removeprefix("chapter-")) - 1
        if index >= len(manifest.get("chapters", [])):
            raise SourceError("Private chapter not found")
        return (self.root / book_id / f"chapter-{index + 1}.txt").read_text(encoding="utf-8")

    def cover(self, book_id: str) -> bytes:
        self._row(book_id)
        path = self.root / book_id / "cover.jpg"
        if not path.is_file():
            raise SourceError("This private book has no cover")
        return path.read_bytes()

    def page(self, book_id: str, number: int) -> bytes:
        with self.lock:
            row = self._row(book_id)
            manifest = json.loads(row["manifest"])
            if not 0 <= number < manifest.get("pageCount", 0):
                raise SourceError("Page not found")
            path, _ = self.original(book_id)
            if row["media_type"] == "pdf":
                return pdf_page(path, number)
            with zipfile.ZipFile(path) as archive:
                return jpeg_page(read_entry(archive, manifest["pages"][number]))

    def import_file(self, temporary: Path, filename: str, title: str = "", author: str = "") -> dict:
        extension = Path(filename).suffix.lower()
        if extension not in {".txt", ".epub", ".cbz", ".pdf"}:
            raise SourceError("Choose a TXT, EPUB, CBZ, or PDF file")
        if not temporary.is_file() or not 0 < temporary.stat().st_size <= self.max_upload_bytes:
            raise SourceError("The upload is empty or exceeds the file size limit")
        digest = hashlib.sha256()
        with temporary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        book_id = digest.hexdigest()
        with self.lock:
            with self._connect() as db:
                existing = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if existing:
                return {"book": self._metadata(existing), "duplicate": True}
            with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as stage:
                target = Path(stage)
                original = target / ("original" + extension)
                shutil.copyfile(temporary, original)
                try:
                    manifest = self._parse(original, extension)
                    chapters = manifest.get("chapters", [])
                    for number, chapter in enumerate(chapters):
                        data = chapter.pop("text").encode("utf-8")
                        if len(data) > MAX_TEXT_BYTES:
                            raise SourceError("An imported chapter exceeds the size limit")
                        (target / f"chapter-{number + 1}.txt").write_bytes(data)
                    if len(chapters) > 10000 or not 0 <= manifest.get("pageCount", 0) <= MAX_PAGES:
                        raise SourceError("The document has too many chapters or pages")
                    used = sum(path.stat().st_size for path in target.iterdir())
                    with self._connect() as db:
                        total = db.execute("SELECT COALESCE(SUM(stored_bytes),0) FROM books").fetchone()[0]
                        if total + used > self.max_bytes:
                            raise SourceError("Private library storage limit reached")
                        destination = self.root / book_id
                        target.rename(destination)
                        try:
                            db.execute("INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,?)", (
                                book_id, (title.strip() or manifest.pop("title", "") or Path(filename).stem)[:250],
                                (author.strip() or manifest.pop("author", "") or "Unknown author")[:250], manifest["mediaType"],
                                Path(filename.replace("\\", "/")).name[:250], extension, original.stat().st_size if original.exists() else temporary.stat().st_size,
                                used, int(time.time()), json.dumps(manifest)))
                            db.commit()
                        except Exception:
                            shutil.rmtree(destination)
                            raise
                    return {"book": self.details(book_id), "duplicate": False}
                except SourceError:
                    raise
                except Exception as exc:
                    raise SourceError("The document could not be imported; check that it is a supported, unlocked file") from exc

    def _parse(self, original: Path, extension: str) -> dict:
        if extension == ".txt":
            if original.stat().st_size > MAX_ENTRY_BYTES:
                raise SourceError("The TXT book is too large")
            text = original.read_text(encoding="utf-8-sig")
            if not text.strip():
                raise SourceError("The text file is empty")
            parts, offset = [], 0
            while offset < len(text):
                end = min(len(text), offset + 250_000)
                if end < len(text):
                    split = text.rfind("\n\n", offset + 125_000, end)
                    if split >= 0:
                        end = split + 2
                parts.append(text[offset:end])
                offset = end
            return {"mediaType": "text", "pageCount": 0,
                    "chapters": [{"title": "Complete book" if len(parts) == 1 else f"Part {index + 1}", "text": part}
                                 for index, part in enumerate(parts)], "pages": []}
        if extension == ".pdf":
            import pypdfium2 as pdfium
            with PDF_LOCK, pdfium.PdfDocument(str(original)) as document:
                count = len(document)
            if not 0 < count <= MAX_PAGES:
                raise SourceError("The PDF page count is unsupported")
            (original.parent / "cover.jpg").write_bytes(pdf_page(original, 0, 600))
            return {"mediaType": "pdf", "pageCount": count, "chapters": [], "pages": []}
        with zipfile.ZipFile(original) as archive:
            items = zip_index(archive)
            if extension == ".epub":
                manifest = parse_epub(archive, items)
            else:
                pages = sorted((name for name in items if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES
                                and not any(part.startswith((".", "__MACOSX")) for part in PurePosixPath(name).parts)), key=lambda name: (natural_key(name), name))
                manifest = {"mediaType": "cbz", "pages": pages, "chapters": []}
            pages = manifest.get("pages", [])
            if len(pages) > MAX_PAGES or sum(items[name].file_size for name in pages) > MAX_EXPANDED_BYTES or (manifest["mediaType"] != "text" and not pages):
                raise SourceError("The document has no supported pages or too many pages")
            manifest["pageCount"] = len(pages)
            cover = manifest.pop("coverEntry", None) or (pages[0] if pages else None)
            if cover:
                (original.parent / "cover.jpg").write_bytes(jpeg_page(read_entry(archive, cover), 600))
            if manifest["mediaType"] == "epub-images":
                with zipfile.ZipFile(original.parent / "offline.cbz", "w", compression=zipfile.ZIP_DEFLATED) as offline:
                    for index, name in enumerate(pages):
                        offline.writestr(f"{index + 1:06d}{PurePosixPath(name).suffix.lower()}", read_entry(archive, name))
            return manifest

    def delete(self, book_id: str) -> bool:
        with self.lock:
            self._row(book_id)
            path = self.root / book_id
            tombstone = self.root / (".delete-" + book_id)
            if path.exists():
                path.rename(tombstone)
            try:
                with self._connect() as db:
                    db.execute("DELETE FROM books WHERE id=?", (book_id,))
            except Exception:
                if tombstone.exists():
                    tombstone.rename(path)
                raise
            if tombstone.exists():
                shutil.rmtree(tombstone)
            return True
