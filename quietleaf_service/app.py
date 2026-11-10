from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from dotenv import load_dotenv

from . import __version__
from .adapters import CatalogSource, HtmlSource, Source, SourceError, require_id
from .archive import (
    MAX_ARCHIVE_CONCURRENCY,
    MAX_CHECK_INTERVAL_SECONDS,
    MIN_CHECK_INTERVAL_SECONDS,
    ArchiveStore,
    ArchiveWorker,
)
from .home import HomeFeedWorker
from .novelping import NovelPingSource
from .wtrlab import WtrLabSource
from .private_library import MAX_UPLOAD_BYTES, PrivateLibrarySource
from .metadata import publication_status


class BookSelection(BaseModel):
    source: str
    bookId: str


class ArchiveBatch(BaseModel):
    items: list[BookSelection] = Field(min_length=1, max_length=50)


class OperationalConfiguration(BaseModel):
    archiveConcurrency: int = Field(ge=1, le=MAX_ARCHIVE_CONCURRENCY)
    followIntervalSeconds: int = Field(
        ge=MIN_CHECK_INTERVAL_SECONDS, le=MAX_CHECK_INTERVAL_SECONDS
    )
    activeIntervalSeconds: int = Field(
        ge=MIN_CHECK_INTERVAL_SECONDS, le=MAX_CHECK_INTERVAL_SECONDS
    )


ADMIN_COOKIE = "quietleaf_admin"
ADMIN_SESSION_SECONDS = 30 * 24 * 60 * 60
ADMIN_LOGIN_BODY_BYTES = 16 * 1024
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW = 5 * 60
MAX_TRACKED_LOGIN_ADDRESSES = 4096


def make_app(sources: list[Source], token: str = "", archive_dir: Path | None = None,
             archive_max_bytes: int = 2 * 1024 * 1024 * 1024,
             follow_interval: int = 3600, active_interval: int = 1800,
              admin_password: str = "", archive_concurrency: int = 3,
              private_max_bytes: int = 10 * 1024 ** 3, private_max_upload_bytes: int = MAX_UPLOAD_BYTES) -> FastAPI:
    if admin_password and not 12 <= len(admin_password) <= 1024:
        raise ValueError("Admin password must contain between 12 and 1024 characters")
    private_library = PrivateLibrarySource(archive_dir / "private-library", private_max_bytes, private_max_upload_bytes) if archive_dir and (token or admin_password) else None
    sources = [*sources, private_library] if private_library else list(sources)
    by_id = {source.id: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("Duplicate source identifier")
    archive = ArchiveStore(
        archive_dir, archive_max_bytes, follow_interval, active_interval,
        archive_concurrency=archive_concurrency,
    ) if archive_dir else None
    worker = ArchiveWorker(archive, sources, archive.archive_concurrency) if archive else None
    home_feed_worker = HomeFeedWorker(sources)

    @asynccontextmanager
    async def lifespan(_):
        try:
            home_feed_worker.start()
            if worker:
                worker.start()
            yield
        finally:
            try:
                home_feed_worker.stop()
            finally:
                if worker:
                    worker.stop()

    app = FastAPI(title="QuietLeaf Content API", version=__version__, docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.archive_worker = worker
    app.state.archive_store = archive
    app.state.home_feed_worker = home_feed_worker
    app.state.private_library = private_library
    upload_gate = asyncio.Semaphore(2)
    web_root = Path(__file__).with_name("web")
    app.mount("/admin/assets", StaticFiles(directory=web_root), name="admin-assets")
    effective_admin_password = admin_password or token
    session_key = hashlib.scrypt(
        effective_admin_password.encode("utf-8"),
        salt=b"quietleaf-admin-session-v1",
        n=2 ** 14,
        r=8,
        p=1,
        dklen=32,
    ) if effective_admin_password else b""
    failed_logins: dict[str, list[float]] = {}
    failed_login_lock = Lock()
    public_paths = {
        "/", "/admin", "/admin/", "/healthz",
        "/admin/assets/admin.css", "/admin/assets/admin.js",
    }
    protected_response_paths = public_paths | {"/admin/login", "/admin/logout"}

    def issue_admin_session() -> str:
        payload = f"{int(time.time()) + ADMIN_SESSION_SECONDS}.{secrets.token_urlsafe(24)}"
        signature = hmac.new(session_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def valid_admin_session(value: str) -> bool:
        if not value or not session_key:
            return False
        try:
            payload, supplied_signature = value.rsplit(".", 1)
            expires_text, _ = payload.split(".", 1)
            expires = int(expires_text)
            payload_bytes = payload.encode("ascii")
            supplied_signature_bytes = bytes.fromhex(supplied_signature)
        except (TypeError, UnicodeEncodeError, ValueError):
            return False
        expected_signature = hmac.new(session_key, payload_bytes, hashlib.sha256).digest()
        return (secrets.compare_digest(supplied_signature_bytes, expected_signature) and
                expires >= int(time.time()))

    def login_retry_after(address: str) -> int:
        now = time.monotonic()
        with failed_login_lock:
            for stored_address, attempts in list(failed_logins.items()):
                recent = [attempt for attempt in attempts if now - attempt < LOGIN_ATTEMPT_WINDOW]
                if recent:
                    failed_logins[stored_address] = recent
                else:
                    failed_logins.pop(stored_address, None)
            recent = failed_logins.get(address, [])
            if len(recent) < LOGIN_ATTEMPT_LIMIT:
                return 0
            return max(1, int(LOGIN_ATTEMPT_WINDOW - (now - recent[0])))

    def record_failed_login(address: str) -> None:
        with failed_login_lock:
            if address not in failed_logins and len(failed_logins) >= MAX_TRACKED_LOGIN_ADDRESSES:
                failed_logins.pop(next(iter(failed_logins)))
            failed_logins.setdefault(address, []).append(time.monotonic())

    def clear_failed_logins(address: str) -> None:
        with failed_login_lock:
            failed_logins.pop(address, None)

    def use_secure_cookie(request: Request) -> bool:
        forwarded_scheme = request.headers.get("x-forwarded-proto", "").partition(",")[0].strip()
        if request.url.scheme == "https" or forwarded_scheme == "https":
            return True
        hostname = request.url.hostname or ""
        try:
            local = ip_address(hostname).is_loopback
        except ValueError:
            local = hostname.casefold() == "localhost"
        return not local

    @app.middleware("http")
    async def bearer_auth(request, call_next):
        public = ((request.method in {"GET", "HEAD"} and request.url.path in public_paths) or
                  (request.method == "POST" and request.url.path == "/admin/login"))
        if (token or admin_password) and not public:
            scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
            valid_bearer = bool(token and separator and scheme.lower() == "bearer" and supplied and
                                secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")))
            valid_session = valid_admin_session(request.cookies.get(ADMIN_COOKIE, ""))
            if not valid_bearer and not valid_session:
                return JSONResponse({"detail": "Authentication required"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            if (valid_session and not valid_bearer and request.method not in {"GET", "HEAD"} and
                    request.headers.get("x-quietleaf-admin") != "1"):
                return JSONResponse({"detail": "Admin request confirmation required"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith(("/v1/private/", "/v1/books/private/")):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path in protected_response_paths:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def root():
        return RedirectResponse("/admin", status_code=307)

    @app.api_route("/admin", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/admin/", methods=["GET", "HEAD"], include_in_schema=False)
    def admin():
        return FileResponse(web_root / "index.html", media_type="text/html")

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def health_probe():
        return {"status": "ok"}

    @app.post("/admin/login", include_in_schema=False)
    async def admin_login(request: Request, response: Response):
        if not effective_admin_password:
            raise HTTPException(status_code=503, detail="Admin password is not configured")
        address = request.client.host if request.client else "unknown"
        retry_after = login_retry_after(address)
        if retry_after:
            raise HTTPException(status_code=429, detail="Too many login attempts",
                                headers={"Retry-After": str(retry_after)})
        try:
            declared_size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request size")
        if declared_size > ADMIN_LOGIN_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Login request is too large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > ADMIN_LOGIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Login request is too large")
            body.extend(chunk)
        try:
            login_data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="Password is required")
        password = login_data.get("password") if isinstance(login_data, dict) else None
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise HTTPException(status_code=422, detail="Password is required")
        valid = secrets.compare_digest(
            password.encode("utf-8"), effective_admin_password.encode("utf-8")
        )
        if not valid:
            record_failed_login(address)
            raise HTTPException(status_code=401, detail="Invalid admin password")
        clear_failed_logins(address)
        response.set_cookie(
            ADMIN_COOKIE,
            issue_admin_session(),
            max_age=ADMIN_SESSION_SECONDS,
            path="/",
            secure=use_secure_cookie(request),
            httponly=True,
            samesite="strict",
        )
        return {"authenticated": True, "expiresInSeconds": ADMIN_SESSION_SECONDS}

    @app.post("/admin/logout", include_in_schema=False)
    def admin_logout(request: Request, response: Response):
        response.delete_cookie(
            ADMIN_COOKIE,
            path="/",
            secure=use_secure_cookie(request),
            httponly=True,
            samesite="strict",
        )
        return {"authenticated": False}

    def source_for(source_id: str) -> Source:
        try:
            return by_id[require_id(source_id)]
        except (KeyError, SourceError):
            raise HTTPException(status_code=404, detail="Unknown source")

    def writable_archive() -> ArchiveStore:
        if archive is None:
            raise HTTPException(status_code=501, detail="Archive is not configured")
        if not (token or admin_password):
            raise HTTPException(status_code=403, detail="Set an API token or admin password before enabling archive management")
        return archive

    def private_store() -> PrivateLibrarySource:
        if private_library is None:
            raise HTTPException(status_code=503, detail="Private library requires archive storage and an API token or admin password")
        return private_library

    @app.get("/v1/private/books")
    def private_books(q: str = Query(default="", max_length=100), offset: int = Query(default=0, ge=0),
                      limit: int = Query(default=100, ge=1, le=100)):
        return private_store().listing(q, offset, limit)

    @app.post("/v1/private/books", status_code=201)
    async def upload_private_book(request: Request, filename: str = Query(max_length=250),
                                  title: str = Query(default="", max_length=250), author: str = Query(default="", max_length=250)):
        store = private_store()
        if Path(filename).suffix.lower() not in {".txt", ".epub", ".cbz", ".pdf"}:
            raise HTTPException(status_code=422, detail="Choose a TXT, EPUB, CBZ, or PDF file")
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid upload size")
        if declared < 0 or declared > store.max_upload_bytes:
            raise HTTPException(status_code=413, detail="The file exceeds the private upload size limit")
        async with upload_gate:
            with tempfile.TemporaryDirectory(prefix=".upload-", dir=store.root) as directory:
                path = Path(directory) / "upload"
                received = 0
                with path.open("wb") as output:
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > store.max_upload_bytes:
                            raise HTTPException(status_code=413, detail="The file exceeds the private upload size limit")
                        await asyncio.to_thread(output.write, chunk)
                if not received:
                    raise HTTPException(status_code=422, detail="The upload is empty")
                return await asyncio.to_thread(apply, store.import_file, path, filename, title, author)

    @app.get("/v1/private/books/{book_id}/file")
    def private_original(book_id: str):
        path, filename = apply(private_store().original, book_id)
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    @app.get("/v1/private/books/{book_id}/pages/{number}")
    def private_page(book_id: str, number: int):
        return Response(apply(private_store().page, book_id, number), media_type="image/jpeg")

    @app.get("/v1/private/books/{book_id}/download")
    def private_offline_document(book_id: str):
        path, filename = apply(private_store().offline_document, book_id)
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    @app.delete("/v1/private/books/{book_id}")
    def delete_private_book(book_id: str):
        return {"deleted": apply(private_store().delete, book_id)}

    def apply(method, *args):
        try:
            return method(*args)
        except SourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            raise HTTPException(status_code=502, detail="Source temporarily unavailable")

    def combined(selected, operation, *params):
        books = []
        errors = []
        for src in selected:
            try:
                for entry in getattr(src, operation)(*params):
                    item = {"source": src.id, **entry}
                    item["publicationStatus"] = publication_status(item.get("publicationStatus"))
                    if archive and archive.has_cover(src.id, entry["id"]):
                        item["coverArchived"] = True
                    books.append(item)
            except Exception:
                errors.append({"source": src.id, "message": "Source temporarily unavailable"})
        if archive:
            query = params[0] if operation == "search" and params else ""
            found = {(item["source"], item["id"]): item for item in books}
            for item in archive.list_books(query):
                key = item["source"], item["id"]
                if key in found:
                    if found[key].get("publicationStatus", "unknown") == "unknown":
                        found[key]["publicationStatus"] = item.get("publicationStatus", "unknown")
                else:
                    if operation != "search" or not params[1] or params[1].casefold() in item["genre"].casefold():
                        books.append(item)
                        found[key] = item
        return {"items": books, "errors": errors}

    @app.get("/v1/health")
    def health():
        return {"service": "quietleaf-content-api", "version": 1,
                "releaseVersion": __version__,
                "archiveEnabled": archive is not None,
                "privateLibraryEnabled": private_library is not None,
                "sourceCapabilities": {source.id: {"genreSearch": bool(getattr(source, "supports_genre_search", True))} for source in sources},
                "privateMaxUploadBytes": private_library.max_upload_bytes if private_library else 0,
                "archiveWorkerRunning": bool(worker and worker.is_running()),
                "archiveConcurrency": worker.concurrency if worker else 0,
                "activeArchiveJobs": worker.active_count() if worker else 0,
                "followIntervalSeconds": archive.poll_seconds if archive else None,
                "activeIntervalSeconds": archive.active_poll_seconds if archive else None,
                "sources": [{"id": s.id, "name": s.name} for s in sources]}

    @app.get("/v1/search")
    def search(q: str = Query(default="", max_length=100),
               genre: str = Query(default="", max_length=60), source: str = ""):
        if source:
            apply(require_id, source)
            if source not in by_id and not archive:
                raise HTTPException(status_code=404, detail="Unknown source")
            selected = [by_id[source]] if source in by_id else []
        else:
            selected = sources
        result = combined(selected, "search", q, genre)
        if source:
            result["items"] = [item for item in result["items"] if item["source"] == source]
        return result

    @app.get("/v1/home")
    def home(source: str = ""):
        if source:
            apply(require_id, source)
            if source not in by_id:
                raise HTTPException(status_code=404, detail="Unknown source")
        categories = ("popular", "completed", "fresh", "new", "hot")
        result = {category: [] for category in categories}
        seen = {category: set() for category in categories}
        errors = []
        archived_metadata = {(item["source"], item["id"]): item for item in archive.list_books()} if archive else {}
        for src in ([by_id[source]] if source else sources):
            capability = getattr(src, "home_feed", None)
            if not callable(capability):
                continue
            try:
                feed = capability()
                if not isinstance(feed, dict):
                    raise TypeError("Invalid home feed")
                additions = {category: [] for category in categories}
                pending = {category: set() for category in categories}
                for category in categories:
                    entries = feed.get(category, [])
                    if not isinstance(entries, list):
                        raise TypeError("Invalid home feed section")
                    for entry in entries:
                        if len(result[category]) + len(additions[category]) >= 20:
                            break
                        if not isinstance(entry, dict):
                            raise TypeError("Invalid home feed item")
                        key = (src.id, entry["id"])
                        if key in seen[category] or key in pending[category]:
                            continue
                        item = {**entry, "source": src.id}
                        item["publicationStatus"] = publication_status(item.get("publicationStatus"))
                        if item["publicationStatus"] == "unknown":
                            item["publicationStatus"] = archived_metadata.get(key, {}).get("publicationStatus", "unknown")
                        if archive and archive.has_cover(src.id, entry["id"]):
                            item["coverArchived"] = True
                        additions[category].append((key, item))
                        pending[category].add(key)
            except Exception:
                if len(errors) < 20:
                    errors.append({"source": src.id, "message": "Source temporarily unavailable"})
                continue
            for category in categories:
                for key, item in additions[category]:
                    seen[category].add(key)
                    result[category].append(item)
        result["errors"] = errors
        return result

    @app.delete("/v1/home/cache")
    async def clear_home_cache():
        target = home_feed_worker.request_refresh()
        await asyncio.to_thread(home_feed_worker.wait_for, target)
        return {"clearedSources": home_feed_worker.source_count}

    @app.get("/v1/recent")
    def recent(source: str = ""):
        if source:
            apply(require_id, source)
            if source not in by_id:
                raise HTTPException(status_code=404, detail="Unknown source")
        result = combined([by_id[source]] if source else sources, "recent")
        if source:
            result["items"] = [item for item in result["items"] if item["source"] == source]
        return result

    @app.get("/v1/books/{source_id}/{book_id}")
    def book(source_id: str, book_id: str, refresh: bool = False):
        source_id, book_id = apply(require_id, source_id), apply(require_id, book_id)
        src = by_id.get(source_id)
        cached = archive.book(source_id, book_id) if archive else None
        if cached and not refresh:
            return cached
        if src:
            try:
                result = {"source": source_id, **src.details(book_id)}
                result["publicationStatus"] = publication_status(result.get("publicationStatus"))
                if cached:
                    archive.save_book(source_id, result)
                if archive and archive.has_cover(source_id, book_id):
                    result["coverArchived"] = True
                return result
            except SourceError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except Exception:
                raise HTTPException(status_code=502, detail="Source metadata refresh failed; saved details were retained" if cached else "Source offline and book not archived")
        if cached:
            raise HTTPException(status_code=422, detail="Source is not configured; saved book details were retained")
        raise HTTPException(status_code=404, detail="Book not found")

    @app.get("/v1/books/{source_id}/{book_id}/cover")
    def archived_cover(source_id: str, book_id: str):
        if source_id == "private" and private_library:
            return Response(apply(private_library.cover, book_id), media_type="image/jpeg")
        if archive is None:
            raise HTTPException(status_code=404, detail="Cover unavailable")
        cover = archive.cover(apply(require_id, source_id), apply(require_id, book_id))
        if cover is None:
            raise HTTPException(status_code=404, detail="Cover not archived")
        return Response(content=cover[1], media_type=cover[0],
                        headers={"Cache-Control": "private, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/books/{source_id}/{book_id}/chapters")
    def chapters(source_id: str, book_id: str):
        source_id, book_id = apply(require_id, source_id), apply(require_id, book_id)
        src = by_id.get(source_id)
        cached_book = archive.book(source_id, book_id) if archive else None
        cached = archive.chapters(source_id, book_id) if archive else []
        if cached_book:
            return {"items": cached}
        if src:
            try:
                fresh = src.chapters(book_id)
                existing = {row["id"]: row for row in cached}
                for row in fresh:
                    if row["id"] in existing and existing[row["id"]]["archived"]:
                        row["archived"] = True
                if archive and archive.book(source_id, book_id):
                    archive.save_chapters(source_id, book_id, fresh)
                seen = {row["id"] for row in fresh}
                merged = fresh + [row for row in cached if row["id"] not in seen]
                merged.sort(key=lambda row: (row["ordinal"], row["id"]))
                return {"items": merged}
            except Exception:
                raise HTTPException(status_code=502, detail="Source offline and chapter list not archived")
        raise HTTPException(status_code=404, detail="Chapter list not found")

    @app.get("/v1/books/{source_id}/{book_id}/chapters/{chapter_id}/text", response_class=PlainTextResponse)
    def chapter_text(source_id: str, book_id: str, chapter_id: str):
        source_id, book_id, chapter_id = (apply(require_id, value) for value in (source_id, book_id, chapter_id))
        stored = archive.text(source_id, book_id, chapter_id) if archive else None
        if stored is not None:
            return PlainTextResponse(stored, media_type="text/plain; charset=utf-8")
        src = by_id.get(source_id)
        if not src:
            raise HTTPException(status_code=404, detail="Chapter not archived and source unavailable")
        return PlainTextResponse(apply(src.text, book_id, chapter_id),
                                 media_type="text/plain; charset=utf-8")

    @app.get("/v1/archive/books")
    def archived_books(q: str = ""):
        storage = writable_archive()
        items = storage.list_books(q)
        return {"items": items, "total": storage.book_count() if not q.strip() else len(items)}

    @app.get("/v1/archive/books/{source_id}/{book_id}")
    def archived_book(source_id: str, book_id: str):
        storage = writable_archive()
        item = apply(storage.stats, source_id, book_id)
        if not item:
            raise HTTPException(status_code=404, detail="Book not archived")
        item["follow"] = storage.follow_status(source_id, book_id)
        return item

    @app.delete("/v1/archive/books/{source_id}/{book_id}")
    def delete_archived_book(source_id: str, book_id: str):
        writable_archive()
        source_id, book_id = apply(require_id, source_id), apply(require_id, book_id)
        return {"deleted": apply(worker.delete_book, source_id, book_id)}

    @app.post("/v1/archive/{source_id}/{book_id}", status_code=202)
    def archive_book(source_id: str, book_id: str):
        storage = writable_archive()
        if source_id == "private":
            raise HTTPException(status_code=409, detail="Private library items are already stored on this server")
        source_for(source_id)
        apply(storage.follow, source_id, book_id)
        job = apply(storage.enqueue, source_id, book_id)
        worker.wake()
        return job

    @app.post("/v1/archive/{source_id}/{book_id}/refresh", status_code=202)
    def refresh_archived_book(source_id: str, book_id: str):
        storage = writable_archive()
        if source_id == "private":
            raise HTTPException(status_code=409, detail="Private library originals cannot be re-archived from a source")
        source_for(source_id)
        job = apply(storage.enqueue, source_id, book_id, True)
        worker.wake()
        return job

    @app.post("/v1/archive/refresh", status_code=202)
    def refresh_archived_batch(selection: ArchiveBatch):
        storage = writable_archive()
        normalized = list(dict.fromkeys(
            (apply(require_id, item.source), apply(require_id, item.bookId)) for item in selection.items
        ))
        for source_id, _ in normalized:
            if source_id == "private":
                raise HTTPException(status_code=409, detail="Private library originals cannot be re-archived from a source")
            source_for(source_id)
        jobs = apply(storage.enqueue_refresh_batch, normalized)
        worker.wake()
        return {"items": jobs, "queued": len(jobs), "phoneDownload": False}

    @app.post("/v1/archive/batch", status_code=202)
    def batch_archive(selection: ArchiveBatch):
        storage = writable_archive()
        normalized = list(dict.fromkeys(
            (apply(require_id, item.source), apply(require_id, item.bookId))
            for item in selection.items
        ))
        for source_id, _ in normalized:
            if source_id == "private":
                raise HTTPException(status_code=409, detail="Private library items are already stored on this server")
            source_for(source_id)
        jobs = []
        for source_id, book_id in normalized:
            storage.follow(source_id, book_id)
            jobs.append(storage.enqueue(source_id, book_id))
        worker.wake()
        return {"items": jobs, "queued": len(jobs), "phoneDownload": False}

    @app.get("/v1/archive/follows")
    def followed_books():
        return {"items": writable_archive().follows()}

    @app.delete("/v1/archive/follows/{source_id}/{book_id}")
    def stop_following(source_id: str, book_id: str):
        return {"updated": apply(writable_archive().unfollow, source_id, book_id)}

    @app.post("/v1/archive/activity/{source_id}/{book_id}")
    def reading_activity(source_id: str, book_id: str):
        return {"recorded": apply(writable_archive().record_activity, source_id, book_id)}

    @app.get("/v1/archive/jobs")
    def archive_jobs():
        return {"items": writable_archive().jobs()}

    @app.get("/v1/archive/config")
    def archive_configuration():
        return writable_archive().operational_settings()

    @app.put("/v1/archive/config")
    def update_archive_configuration(configuration: OperationalConfiguration):
        writable_archive()
        settings = apply(
            worker.update_operational_settings,
            configuration.archiveConcurrency,
            configuration.followIntervalSeconds,
            configuration.activeIntervalSeconds,
        )
        return settings

    @app.get("/v1/archive/jobs/{job_id}")
    def archive_job(job_id: str):
        job = writable_archive().job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.post("/v1/archive/jobs/{job_id}/{action}")
    def archive_control(job_id: str, action: str):
        result = apply(writable_archive().control, job_id, action)
        if action in {"resume", "retry"}:
            worker.wake()
        return result

    return app


def build_from_env() -> FastAPI:
    load_dotenv(Path.cwd() / ".env", override=False)
    root = Path(os.getenv("QUIETLEAF_CATALOG_DIR", str(Path(__file__).resolve().parent.parent / "catalog")))
    sources: list[Source] = [CatalogSource(root)]
    config_path = os.getenv("QUIETLEAF_HTML_SOURCES", "")
    if config_path:
        configs = json.loads(Path(config_path).read_text(encoding="utf-8"))
        sources.extend(HtmlSource(item) for item in configs)
    if os.getenv("QUIETLEAF_ENABLE_NOVELPING", "").lower() in {"1", "true", "yes"}:
        sources.append(NovelPingSource(
            base_url=os.getenv("QUIETLEAF_NOVELPING_BASE_URL", "https://novelping.com"),
            search_path=os.getenv("QUIETLEAF_NOVELPING_SEARCH_PATH", "/search?keyword={query}"),
            image_hosts=tuple(host.strip() for host in os.getenv("QUIETLEAF_NOVELPING_IMAGE_HOSTS", "").split(",") if host.strip()),
            access_authorized=os.getenv("QUIETLEAF_NOVELPING_ACCESS_AUTHORIZED", "").lower() in {"1", "true", "yes"},
        ))
    if os.getenv("QUIETLEAF_ENABLE_WTRLAB", "").lower() in {"1", "true", "yes"}:
        sources.append(WtrLabSource(
            base_url=os.getenv("QUIETLEAF_WTRLAB_BASE_URL", "https://wtr-lab.com"),
            access_authorized=os.getenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "").lower() in {"1", "true", "yes"},
            session_cookie=os.getenv("QUIETLEAF_WTRLAB_COOKIE", ""),
        ))
    archive_dir = Path(os.getenv("QUIETLEAF_ARCHIVE_DIR", str(Path(__file__).resolve().parent.parent / "archive")))
    archive_cap = int(os.getenv("QUIETLEAF_ARCHIVE_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
    return make_app(
        sources,
        token=os.getenv("QUIETLEAF_API_TOKEN", ""),
        archive_dir=archive_dir,
        archive_max_bytes=archive_cap,
        follow_interval=int(os.getenv("QUIETLEAF_FOLLOW_INTERVAL_SECONDS", "3600")),
        active_interval=int(os.getenv("QUIETLEAF_ACTIVE_INTERVAL_SECONDS", "1800")),
        admin_password=os.getenv("QUIETLEAF_ADMIN_PASSWORD", ""),
        archive_concurrency=int(os.getenv("QUIETLEAF_ARCHIVE_CONCURRENCY", "3")),
        private_max_bytes=int(os.getenv("QUIETLEAF_PRIVATE_MAX_BYTES", str(10 * 1024 ** 3))),
        private_max_upload_bytes=int(os.getenv("QUIETLEAF_PRIVATE_MAX_UPLOAD_BYTES", str(MAX_UPLOAD_BYTES))),
    )


app = build_from_env()
