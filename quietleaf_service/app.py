from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import __version__
from .adapters import CatalogSource, HtmlSource, Source, SourceError, require_id
from .archive import ArchiveStore, ArchiveWorker
from .novelping import NovelPingSource


class BookSelection(BaseModel):
    source: str
    bookId: str


class ArchiveBatch(BaseModel):
    items: list[BookSelection] = Field(min_length=1, max_length=50)


def make_app(sources: list[Source], token: str = "", archive_dir: Path | None = None,
             archive_max_bytes: int = 2 * 1024 * 1024 * 1024,
             follow_interval: int = 3600, active_interval: int = 1800) -> FastAPI:
    by_id = {source.id: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("Duplicate source identifier")
    archive = ArchiveStore(archive_dir, archive_max_bytes, follow_interval, active_interval) if archive_dir else None
    worker = ArchiveWorker(archive, sources) if archive else None

    @asynccontextmanager
    async def lifespan(_):
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="QuietLeaf Content API", version=__version__, docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.archive_worker = worker
    app.state.archive_store = archive
    web_root = Path(__file__).with_name("web")
    app.mount("/admin/assets", StaticFiles(directory=web_root), name="admin-assets")
    public_paths = {
        "/", "/admin", "/admin/", "/healthz",
        "/admin/assets/admin.css", "/admin/assets/admin.js",
    }

    @app.middleware("http")
    async def bearer_auth(request, call_next):
        public = request.method in {"GET", "HEAD"} and request.url.path in public_paths
        if token and not public:
            scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
            valid = (separator and scheme.lower() == "bearer" and supplied and
                     secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")))
            if not valid:
                return JSONResponse({"detail": "Invalid API token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        response = await call_next(request)
        if request.url.path in public_paths:
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

    def source_for(source_id: str) -> Source:
        try:
            return by_id[require_id(source_id)]
        except (KeyError, SourceError):
            raise HTTPException(status_code=404, detail="Unknown source")

    def writable_archive() -> ArchiveStore:
        if archive is None:
            raise HTTPException(status_code=501, detail="Archive is not configured")
        if not token:
            raise HTTPException(status_code=403, detail="Set an API token before enabling archive management")
        return archive

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
                    if archive and archive.has_cover(src.id, entry["id"]):
                        item["coverArchived"] = True
                    books.append(item)
            except Exception:
                errors.append({"source": src.id, "message": "Source temporarily unavailable"})
        if archive:
            query = params[0] if operation == "search" and params else ""
            found = {(item["source"], item["id"]) for item in books}
            for item in archive.list_books(query):
                key = item["source"], item["id"]
                if key not in found:
                    if operation != "search" or not params[1] or params[1].casefold() in item["genre"].casefold():
                        books.append(item)
                        found.add(key)
        return {"items": books, "errors": errors}

    @app.get("/v1/health")
    def health():
        return {"service": "quietleaf-content-api", "version": 1,
                "releaseVersion": __version__,
                "archiveEnabled": archive is not None,
                "archiveWorkerRunning": bool(worker and worker.thread and worker.thread.is_alive()),
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

    @app.get("/v1/recent")
    def recent():
        return combined(sources, "recent")

    @app.get("/v1/books/{source_id}/{book_id}")
    def book(source_id: str, book_id: str):
        source_id, book_id = apply(require_id, source_id), apply(require_id, book_id)
        src = by_id.get(source_id)
        cached = archive.book(source_id, book_id) if archive else None
        if src:
            try:
                result = {"source": source_id, **src.details(book_id)}
                if archive and archive.has_cover(source_id, book_id):
                    result["coverArchived"] = True
                return result
            except SourceError as exc:
                if cached:
                    return cached
                raise HTTPException(status_code=422, detail=str(exc))
            except Exception:
                if cached:
                    return cached
                raise HTTPException(status_code=502, detail="Source offline and book not archived")
        if cached:
            return cached
        raise HTTPException(status_code=404, detail="Book not found")

    @app.get("/v1/books/{source_id}/{book_id}/cover")
    def archived_cover(source_id: str, book_id: str):
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
        cached = archive.chapters(source_id, book_id) if archive else []
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
                if cached:
                    return {"items": cached}
                raise HTTPException(status_code=502, detail="Source offline and chapter list not archived")
        if cached:
            return {"items": cached}
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
        storage = writable_archive()
        return {"deleted": apply(storage.delete_book, source_id, book_id)}

    @app.post("/v1/archive/{source_id}/{book_id}", status_code=202)
    def archive_book(source_id: str, book_id: str):
        storage = writable_archive()
        source_for(source_id)
        apply(storage.follow, source_id, book_id)
        job = apply(storage.enqueue, source_id, book_id)
        worker.wake()
        return job

    @app.post("/v1/archive/batch", status_code=202)
    def batch_archive(selection: ArchiveBatch):
        storage = writable_archive()
        normalized = list(dict.fromkeys(
            (apply(require_id, item.source), apply(require_id, item.bookId))
            for item in selection.items
        ))
        for source_id, _ in normalized:
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
    archive_dir = Path(os.getenv("QUIETLEAF_ARCHIVE_DIR", str(Path(__file__).resolve().parent.parent / "archive")))
    archive_cap = int(os.getenv("QUIETLEAF_ARCHIVE_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
    return make_app(sources, os.getenv("QUIETLEAF_API_TOKEN", ""), archive_dir, archive_cap,
                    int(os.getenv("QUIETLEAF_FOLLOW_INTERVAL_SECONDS", "3600")),
                    int(os.getenv("QUIETLEAF_ACTIVE_INTERVAL_SECONDS", "1800")))


app = build_from_env()
