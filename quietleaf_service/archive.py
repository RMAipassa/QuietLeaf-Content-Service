from __future__ import annotations

import sqlite3
from contextlib import contextmanager
import threading
import time
from pathlib import Path
from uuid import uuid4

from .adapters import MAX_TEXT_BYTES, Source, SourceError, require_id


class ArchiveStore:
    def __init__(self, root: Path, max_bytes: int = 2 * 1024 * 1024 * 1024,
                 poll_seconds: int = 3600, active_poll_seconds: int = 1800, active_days: int = 2):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "archive.sqlite3"
        self.max_bytes = max_bytes
        self.poll_seconds = max(1800, poll_seconds)
        self.active_poll_seconds = max(1800, active_poll_seconds)
        self.active_days = max(1, active_days)
        self.lock = threading.RLock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    source TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '', genre TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL, PRIMARY KEY(source, id)
                );
                CREATE TABLE IF NOT EXISTS covers (
                    source TEXT NOT NULL, book_id TEXT NOT NULL,
                    mime_type TEXT NOT NULL, data BLOB NOT NULL, bytes INTEGER NOT NULL,
                    PRIMARY KEY(source, book_id),
                    FOREIGN KEY(source, book_id) REFERENCES books(source, id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    source TEXT NOT NULL, book_id TEXT NOT NULL, id TEXT NOT NULL,
                    title TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    body TEXT, bytes INTEGER NOT NULL DEFAULT 0, updated_at INTEGER,
                    PRIMARY KEY(source, book_id, id),
                    FOREIGN KEY(source, book_id) REFERENCES books(source, id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, book_id TEXT NOT NULL,
                    status TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
                    downloaded INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS follows (
                    source TEXT NOT NULL, book_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, last_read_at INTEGER NOT NULL DEFAULT 0,
                    last_checked_at INTEGER NOT NULL DEFAULT 0,
                    last_success_at INTEGER NOT NULL DEFAULT 0,
                    next_check_at INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source, book_id)
                );
                CREATE INDEX IF NOT EXISTS follows_due ON follows(enabled, next_check_at);
                CREATE INDEX IF NOT EXISTS chapters_by_book ON chapters(source, book_id, ordinal);
                CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);
            """)
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _book_row(row):
        if not row:
            return None
        return {
            "source": row["source"], "id": row["id"], "title": row["title"],
            "author": row["author"], "description": row["description"],
            "coverUrl": row["cover_url"], "genre": row["genre"],
            "archived": True, "coverArchived": bool(row["cover_archived"]),
        }

    def save_book(self, source: str, book: dict):
        source = require_id(source)
        book_id = require_id(book["id"])
        with self.lock, self._connect() as db:
            db.execute("""
                INSERT INTO books(source, id, title, author, description, cover_url, genre, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, id) DO UPDATE SET
                    title=excluded.title, author=excluded.author,
                    description=excluded.description, cover_url=excluded.cover_url,
                    genre=excluded.genre, updated_at=excluded.updated_at
            """, (source, book_id, book["title"], book.get("author", ""),
                  book.get("description", ""), book.get("coverUrl", ""),
                  book.get("genre", ""), int(time.time())))

    def cover(self, source: str, book_id: str) -> tuple[str, bytes] | None:
        with self._connect() as db:
            row = db.execute("SELECT mime_type,data FROM covers WHERE source=? AND book_id=?",
                             (require_id(source), require_id(book_id))).fetchone()
        return (row["mime_type"], row["data"]) if row else None

    def has_cover(self, source: str, book_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM covers WHERE source=? AND book_id=?",
                             (require_id(source), require_id(book_id))).fetchone()
        return row is not None

    def save_cover(self, source: str, book_id: str, mime_type: str, data: bytes) -> None:
        if len(data) > 2 * 1024 * 1024 or not data:
            raise SourceError("Empty or oversized cover")
        signatures = {
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
            "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        }
        if not signatures.get(mime_type, False):
            raise SourceError("Invalid cover image")
        source, book_id = require_id(source), require_id(book_id)
        with self.lock, self._connect() as db:
            old = db.execute("SELECT bytes FROM covers WHERE source=? AND book_id=?",
                             (source, book_id)).fetchone()
            used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM chapters").fetchone()[0]
            used += db.execute("SELECT COALESCE(SUM(bytes),0) FROM covers").fetchone()[0]
            if used - (old["bytes"] if old else 0) + len(data) > self.max_bytes:
                raise SourceError("Archive storage limit reached")
            db.execute("""INSERT INTO covers(source,book_id,mime_type,data,bytes) VALUES(?,?,?,?,?)
                          ON CONFLICT(source,book_id) DO UPDATE SET
                          mime_type=excluded.mime_type,data=excluded.data,bytes=excluded.bytes""",
                       (source, book_id, mime_type, data, len(data)))

    def save_chapters(self, source: str, book_id: str, chapters: list[dict]):
        source = require_id(source)
        book_id = require_id(book_id)
        if len(chapters) > 100_000:
            raise SourceError("Book has too many chapters")
        with self.lock, self._connect() as db:
            for idx, chapter in enumerate(chapters):
                db.execute("""
                    INSERT INTO chapters(source, book_id, id, title, ordinal)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(source, book_id, id) DO UPDATE SET
                        title=excluded.title, ordinal=excluded.ordinal
                """, (source, book_id, require_id(chapter["id"]),
                      chapter["title"], int(chapter.get("ordinal", idx))))

    def save_text(self, source: str, book_id: str, chapter_id: str, body: str):
        size = len(body.encode("utf-8"))
        if not body.strip() or size > MAX_TEXT_BYTES:
            raise SourceError("Empty or oversized chapter")
        source, book_id, chapter_id = map(require_id, (source, book_id, chapter_id))
        with self.lock, self._connect() as db:
            old = db.execute("SELECT bytes FROM chapters WHERE source=? AND book_id=? AND id=?",
                             (source, book_id, chapter_id)).fetchone()
            if old is None:
                raise SourceError("Chapter is missing from the archive index")
            used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM chapters").fetchone()[0]
            used += db.execute("SELECT COALESCE(SUM(bytes),0) FROM covers").fetchone()[0]
            if used - old["bytes"] + size > self.max_bytes:
                raise SourceError("Archive storage limit reached")
            db.execute("""UPDATE chapters SET body=?, bytes=?, updated_at=?
                          WHERE source=? AND book_id=? AND id=?""",
                       (body, size, int(time.time()), source, book_id, chapter_id))

    def book(self, source: str, book_id: str):
        with self._connect() as db:
            row = db.execute("""SELECT books.*, EXISTS(SELECT 1 FROM covers WHERE
                             covers.source=books.source AND covers.book_id=books.id) AS cover_archived
                             FROM books WHERE source=? AND id=?""",
                             (require_id(source), require_id(book_id))).fetchone()
        return self._book_row(row)

    def chapters(self, source: str, book_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT id,title,ordinal,body IS NOT NULL AS stored
                                 FROM chapters WHERE source=? AND book_id=?
                                 ORDER BY ordinal, id""",
                              (require_id(source), require_id(book_id))).fetchall()
        return [{"id": row["id"], "title": row["title"], "ordinal": row["ordinal"],
                 "downloadable": bool(row["stored"]), "archived": bool(row["stored"])} for row in rows]

    def text(self, source: str, book_id: str, chapter_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("""SELECT body FROM chapters
                                 WHERE source=? AND book_id=? AND id=?""",
                             tuple(map(require_id, (source, book_id, chapter_id)))).fetchone()
        return row["body"] if row else None

    def list_books(self, query: str = "", source: str = "", limit: int = 200) -> list[dict]:
        params = []
        filters = []
        if query.strip():
            filters.append("(title LIKE ? OR author LIKE ?)")
            params.extend(["%" + query.strip()[:100] + "%"] * 2)
        if source:
            filters.append("source=?")
            params.append(require_id(source))
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._connect() as db:
            rows = db.execute("""SELECT books.*, EXISTS(SELECT 1 FROM covers WHERE
                              covers.source=books.source AND covers.book_id=books.id) AS cover_archived
                              FROM books""" + where + " ORDER BY updated_at DESC LIMIT ?",
                              (*params, limit)).fetchall()
        return [self._book_row(r) for r in rows]

    def book_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM books").fetchone()[0])

    def stats(self, source: str, book_id: str):
        book = self.book(source, book_id)
        if book is None:
            return None
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) AS total, COUNT(body) AS downloaded,
                                 COALESCE(SUM(bytes),0) AS bytes FROM chapters
                                 WHERE source=? AND book_id=?""", (source, book_id)).fetchone()
        image_bytes = len(self.cover(source, book_id)[1]) if self.has_cover(source, book_id) else 0
        return {**book, "chapterCount": row["total"], "downloadedChapters": row["downloaded"],
                "storageBytes": row["bytes"] + image_bytes, "coverStorageBytes": image_bytes}

    def interval_for(self, last_read_at: int, now: int) -> int:
        if last_read_at and now - last_read_at <= self.active_days * 86400:
            return self.active_poll_seconds
        return self.poll_seconds

    def follow(self, source: str, book_id: str, now: int | None = None) -> dict:
        source, book_id = require_id(source), require_id(book_id)
        stamp = int(time.time()) if now is None else now
        with self.lock, self._connect() as db:
            db.execute("""INSERT INTO follows(source,book_id,enabled,created_at,next_check_at)
                          VALUES(?,?,1,?,?)
                          ON CONFLICT(source,book_id) DO UPDATE SET enabled=1""",
                       (source, book_id, stamp, stamp + self.poll_seconds))
        return self.follow_status(source, book_id)

    def unfollow(self, source: str, book_id: str) -> bool:
        with self.lock, self._connect() as db:
            count = db.execute("UPDATE follows SET enabled=0 WHERE source=? AND book_id=?",
                               (require_id(source), require_id(book_id))).rowcount
        return bool(count)

    def follow_status(self, source: str, book_id: str):
        with self._connect() as db:
            row = db.execute("SELECT * FROM follows WHERE source=? AND book_id=?",
                             (require_id(source), require_id(book_id))).fetchone()
        return dict(row) if row else None

    def follows(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM follows ORDER BY enabled DESC, next_check_at, source, book_id").fetchall()]

    def record_activity(self, source: str, book_id: str, now: int | None = None) -> bool:
        source, book_id = require_id(source), require_id(book_id)
        stamp = int(time.time()) if now is None else now
        with self.lock, self._connect() as db:
            count = db.execute("""UPDATE follows SET last_read_at=?,
                                next_check_at=MIN(next_check_at, ?)
                                WHERE source=? AND book_id=? AND enabled=1""",
                               (stamp, stamp + self.active_poll_seconds, source, book_id)).rowcount
        return bool(count)

    def reserve_due(self, now: int | None = None, limit: int = 10) -> list[tuple[str, str]]:
        stamp = int(time.time()) if now is None else now
        with self.lock, self._connect() as db:
            rows = db.execute("""SELECT source,book_id,last_read_at FROM follows
                                 WHERE enabled=1 AND next_check_at <= ?
                                 ORDER BY CASE WHEN last_read_at >= ? THEN 0 ELSE 1 END,
                                          next_check_at ASC LIMIT ?""",
                              (stamp, stamp - self.active_days * 86400, min(limit, 50))).fetchall()
            for row in rows:
                interval = self.interval_for(row["last_read_at"], stamp)
                db.execute("""UPDATE follows SET next_check_at=?
                              WHERE source=? AND book_id=?""",
                           (stamp + interval, row["source"], row["book_id"]))
        return [(row["source"], row["book_id"]) for row in rows]

    def record_check(self, source: str, book_id: str, error: str = "", now: int | None = None):
        stamp = int(time.time()) if now is None else now
        with self.lock, self._connect() as db:
            row = db.execute("SELECT failure_count,last_read_at FROM follows WHERE source=? AND book_id=?",
                             (source, book_id)).fetchone()
            if not row:
                return
            failures = min(row["failure_count"] + 1, 8) if error else 0
            interval = self.interval_for(row["last_read_at"], stamp)
            if error:
                interval = min(86400, interval * 2 ** failures)
            db.execute("""UPDATE follows SET last_checked_at=?,last_success_at=CASE WHEN ?='' THEN ? ELSE last_success_at END,
                          last_error=?,failure_count=?,next_check_at=? WHERE source=? AND book_id=?""",
                       (stamp, error, stamp, error[:300], failures, stamp + interval, source, book_id))

    def enqueue(self, source: str, book_id: str) -> dict:
        source, book_id = require_id(source), require_id(book_id)
        with self.lock, self._connect() as db:
            row = db.execute("""SELECT * FROM jobs WHERE source=? AND book_id=?
                                AND status IN ('queued','running','paused') ORDER BY created_at DESC LIMIT 1""",
                             (source, book_id)).fetchone()
            if row:
                return dict(row)
            stamp = int(time.time())
            job_id = uuid4().hex
            db.execute("""INSERT INTO jobs(id,source,book_id,status,created_at,updated_at)
                          VALUES(?,?,?,'queued',?,?)""", (job_id, source, book_id, stamp, stamp))
        return self.job(job_id)

    def job(self, job_id: str):
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (require_id(job_id),)).fetchone()
        return dict(row) if row else None

    def jobs(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()]

    def next_job(self):
        with self.lock, self._connect() as db:
            row = db.execute("""SELECT * FROM jobs WHERE status='queued'
                                ORDER BY created_at, id LIMIT 1""").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?",
                       (int(time.time()), row["id"]))
            return dict(row)

    def update_job(self, job_id: str, status: str | None = None, total: int | None = None,
                   downloaded: int | None = None, error: str | None = None):
        values = {"updated_at": int(time.time())}
        if status is not None:
            values["status"] = status
        if total is not None:
            values["total"] = total
        if downloaded is not None:
            values["downloaded"] = downloaded
        if error is not None:
            values["error"] = error[:300]
        columns = ",".join(f"{key}=?" for key in values)
        with self.lock, self._connect() as db:
            db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), require_id(job_id)))

    def control(self, job_id: str, action: str):
        states = {
            "pause": ({"queued", "running"}, "paused"),
            "resume": ({"paused", "failed"}, "queued"),
            "cancel": ({"queued", "running", "paused", "failed"}, "canceled"),
            "retry": ({"failed", "canceled", "completed"}, "queued"),
        }
        if action not in states:
            raise SourceError("Unknown job action")
        accepted, target = states[action]
        with self.lock:
            current = self.job(job_id)
            if current is None:
                raise SourceError("Job not found")
            if current["status"] not in accepted:
                raise SourceError("Job action not possible from this state")
            self.update_job(job_id, status=target, error="")
            return self.job(job_id)

    def delete_book(self, source: str, book_id: str):
        source, book_id = require_id(source), require_id(book_id)
        with self.lock, self._connect() as db:
            active = db.execute("""SELECT 1 FROM jobs WHERE source=? AND book_id=?
                                    AND status IN ('queued','running','paused') LIMIT 1""",
                                (source, book_id)).fetchone()
            if active:
                raise SourceError("Cancel the archive job before deleting the book")
            count = db.execute("DELETE FROM books WHERE source=? AND id=?", (source, book_id)).rowcount
            db.execute("DELETE FROM jobs WHERE source=? AND book_id=?", (source, book_id))
            db.execute("DELETE FROM follows WHERE source=? AND book_id=?", (source, book_id))
        return bool(count)


class ArchiveWorker:
    def __init__(self, store: ArchiveStore, sources: list[Source]):
        self.store = store
        self.sources = {source.id: source for source in sources}
        self.stop_signal = threading.Event()
        self.wake_signal = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="quietleaf-archive-worker")
            self.thread.start()

    def stop(self):
        self.stop_signal.set()
        self.wake_signal.set()
        if self.thread:
            self.thread.join(timeout=2)

    def wake(self):
        self.wake_signal.set()

    def run(self):
        while not self.stop_signal.is_set():
            job = self.store.next_job()
            if job:
                self.process(job)
                continue
            due = self.store.reserve_due()
            for source_id, book_id in due:
                if source_id in self.sources:
                    self.store.enqueue(source_id, book_id)
                else:
                    self.store.record_check(source_id, book_id, "Source adapter not configured")
            if due:
                continue
            self.wake_signal.wait(30)
            self.wake_signal.clear()

    def process(self, job: dict):
        job_id, source_id, book_id = job["id"], job["source"], job["book_id"]
        adapter = self.sources.get(source_id)
        if not adapter:
            self.store.update_job(job_id, status="failed", error="Source adapter is not configured")
            self.store.record_check(source_id, book_id, "Source adapter not configured")
            return
        try:
            details = adapter.details(book_id)
            chapter_list = adapter.chapters(book_id)
            self.store.save_book(source_id, details)
            if details.get("coverUrl") and not self.store.has_cover(source_id, book_id):
                fetch_cover = getattr(adapter, "cover_image", None)
                if callable(fetch_cover):
                    try:
                        mime, binary = fetch_cover(details["coverUrl"])
                        self.store.save_cover(source_id, book_id, mime, binary)
                    except Exception:
                        pass
            self.store.save_chapters(source_id, book_id, chapter_list)
            candidates = [ch for ch in chapter_list if ch.get("downloadable", False)]
            downloaded = sum(self.store.text(source_id, book_id, ch["id"]) is not None for ch in candidates)
            self.store.update_job(job_id, total=len(candidates), downloaded=downloaded)
            for chapter in candidates:
                if self.stop_signal.is_set():
                    return
                status = self.store.job(job_id)["status"]
                if status != "running":
                    return
                if self.store.text(source_id, book_id, chapter["id"]) is not None:
                    continue
                body = adapter.text(book_id, chapter["id"])
                if self.store.job(job_id)["status"] != "running":
                    return
                self.store.save_text(source_id, book_id, chapter["id"], body)
                downloaded += 1
                self.store.update_job(job_id, downloaded=downloaded)
            if self.store.job(job_id)["status"] == "running":
                self.store.record_check(source_id, book_id)
                self.store.update_job(job_id, status="completed", error="")
        except Exception as exc:
            if self.store.job(job_id)["status"] == "running":
                self.store.update_job(job_id, status="failed", error=str(exc)[:300])
                self.store.record_check(source_id, book_id, str(exc)[:300])
