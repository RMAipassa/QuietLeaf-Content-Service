from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
import threading
import time
from pathlib import Path
from uuid import uuid4

from .adapters import MAX_TEXT_BYTES, Source, SourceError, require_id
from .content import clean_chapter_text
from .library_sort import BookSort, book_order
from .metadata import display_title, publication_status


MIN_CHECK_INTERVAL_SECONDS = 1800
MAX_CHECK_INTERVAL_SECONDS = 365 * 24 * 60 * 60
MAX_ARCHIVE_CONCURRENCY = 3
ARCHIVE_STOP_TIMEOUT_SECONDS = 15


class ArchiveStore:
    def __init__(self, root: Path, max_bytes: int = 2 * 1024 * 1024 * 1024,
                 poll_seconds: int = 3600, active_poll_seconds: int = 1800,
                 active_days: int = 2, archive_concurrency: int = 3):
        if not 1 <= archive_concurrency <= MAX_ARCHIVE_CONCURRENCY:
            raise ValueError(f"Archive concurrency must be between 1 and {MAX_ARCHIVE_CONCURRENCY}")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "archive.sqlite3"
        self.max_bytes = max_bytes
        self.poll_seconds = max(MIN_CHECK_INTERVAL_SECONDS, poll_seconds)
        self.active_poll_seconds = max(MIN_CHECK_INTERVAL_SECONDS, active_poll_seconds)
        self.archive_concurrency = archive_concurrency
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
                    refresh INTEGER NOT NULL DEFAULT 0,
                    range_start INTEGER, range_end INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    run_token TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_chapters (
                    job_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
                    refreshed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id, chapter_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
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
                CREATE TABLE IF NOT EXISTS configuration (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reading_progress (
                    source TEXT NOT NULL, book_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
                    fraction REAL NOT NULL, revision INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    client_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    PRIMARY KEY(source, book_id)
                );
                CREATE INDEX IF NOT EXISTS follows_due ON follows(enabled, next_check_at);
                CREATE INDEX IF NOT EXISTS chapters_by_book ON chapters(source, book_id, ordinal);
                CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);
            """)
            book_columns = {row["name"] for row in db.execute("PRAGMA table_info(books)").fetchall()}
            if "publication_status" not in book_columns:
                db.execute("ALTER TABLE books ADD COLUMN publication_status TEXT NOT NULL DEFAULT 'unknown'")
            if "created_at" not in book_columns:
                db.execute("ALTER TABLE books ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")
                db.execute("""UPDATE books SET created_at=(SELECT MIN(stamp) FROM (
                    SELECT books.updated_at AS stamp UNION ALL
                    SELECT created_at FROM jobs WHERE jobs.source=books.source AND jobs.book_id=books.id UNION ALL
                    SELECT created_at FROM follows WHERE follows.source=books.source AND follows.book_id=books.id
                ) WHERE stamp>0)""")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
            if "run_token" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN run_token TEXT NOT NULL DEFAULT ''")
            if "refresh" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN refresh INTEGER NOT NULL DEFAULT 0")
            for column in ("range_start", "range_end"):
                if column not in job_columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} INTEGER")
            db.execute("UPDATE jobs SET status='queued',run_token='' WHERE status='running'")
            defaults = {
                "archive_concurrency": self.archive_concurrency,
                "follow_interval_seconds": self.poll_seconds,
                "active_interval_seconds": self.active_poll_seconds,
            }
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO configuration(key,value) VALUES(?,?)",
                           (key, value))
            configured = {row["key"]: int(row["value"]) for row in db.execute(
                "SELECT key,value FROM configuration"
            ).fetchall()}
            self.archive_concurrency = min(
                MAX_ARCHIVE_CONCURRENCY,
                max(1, configured.get("archive_concurrency", self.archive_concurrency)),
            )
            self.poll_seconds = max(
                MIN_CHECK_INTERVAL_SECONDS,
                configured.get("follow_interval_seconds", self.poll_seconds),
            )
            self.active_poll_seconds = max(
                MIN_CHECK_INTERVAL_SECONDS,
                configured.get("active_interval_seconds", self.active_poll_seconds),
            )

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
            "source": row["source"], "id": row["id"], "title": display_title(row["source"], row["title"]),
            "author": row["author"], "description": row["description"],
            "coverUrl": row["cover_url"], "genre": row["genre"],
            "archived": True, "coverArchived": bool(row["cover_archived"]),
            "publicationStatus": publication_status(row["publication_status"]),
            "addedAt": row["created_at"] * 1000, "lastReadAt": row["last_read_at"],
        }

    @staticmethod
    def _job_row(row):
        if not row:
            return None
        result = dict(row)
        result.pop("run_token", None)
        result["refresh"] = bool(result.get("refresh", 0))
        return result

    def save_book(self, source: str, book: dict):
        source = require_id(source)
        book_id = require_id(book["id"])
        with self.lock, self._connect() as db:
            db.execute("""
                INSERT INTO books(source, id, title, author, description, cover_url, genre, updated_at, publication_status, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, id) DO UPDATE SET
                    title=excluded.title, author=excluded.author,
                    description=excluded.description, cover_url=excluded.cover_url,
                    genre=excluded.genre, updated_at=excluded.updated_at, publication_status=excluded.publication_status
            """, (source, book_id, book["title"], book.get("author", ""),
                  book.get("description", ""), book.get("coverUrl", ""),
                  book.get("genre", ""), int(time.time()), publication_status(book.get("publicationStatus")), int(time.time())))

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
            row = db.execute(self._book_select() + " WHERE source=? AND id=?",
                              (require_id(source), require_id(book_id))).fetchone()
        return self._book_row(row)

    @staticmethod
    def _book_select():
        return """SELECT books.*, EXISTS(SELECT 1 FROM covers WHERE
            covers.source=books.source AND covers.book_id=books.id) AS cover_archived,
            MAX(COALESCE((SELECT last_read_at*1000 FROM follows WHERE
                follows.source=books.source AND follows.book_id=books.id),0),
                COALESCE((SELECT updated_at FROM reading_progress WHERE
                reading_progress.source=books.source AND reading_progress.book_id=books.id),0)) AS last_read_at
            FROM books"""

    def chapters(self, source: str, book_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT id,title,ordinal,body IS NOT NULL AS stored
                                 FROM chapters WHERE source=? AND book_id=?
                                 ORDER BY ordinal, id""",
                              (require_id(source), require_id(book_id))).fetchall()
        return [{"id": row["id"], "title": display_title(source, row["title"]), "ordinal": row["ordinal"],
                 "downloadable": bool(row["stored"]), "archived": bool(row["stored"])} for row in rows]

    def text(self, source: str, book_id: str, chapter_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("""SELECT body FROM chapters
                                 WHERE source=? AND book_id=? AND id=?""",
                             tuple(map(require_id, (source, book_id, chapter_id)))).fetchone()
        return clean_chapter_text(source, row["body"]) if row and row["body"] is not None else None

    def inspected_chapter(self, source: str, book_id: str, chapter_id: str, raw: bool = False) -> dict | None:
        with self._connect() as db:
            row = db.execute("""SELECT id,title,ordinal,body,bytes,updated_at FROM chapters
                                WHERE source=? AND book_id=? AND id=? AND body IS NOT NULL""",
                             tuple(map(require_id, (source, book_id, chapter_id)))).fetchone()
        if row is None:
            return None
        cleaned = clean_chapter_text(source, row["body"])
        text = row["body"] if raw else cleaned
        return {"id": row["id"], "title": display_title(source, row["title"]), "ordinal": row["ordinal"],
                "text": text, "characters": len(text), "storedBytes": row["bytes"], "savedAt": row["updated_at"],
                "storedCharacters": len(row["body"]), "cleanupApplied": cleaned != row["body"],
                "origin": "archive", "mediaType": "text"}

    @staticmethod
    def _position_row(row) -> dict | None:
        if row is None:
            return None
        return {"source": row["source"], "bookId": row["book_id"], "chapterId": row["chapter_id"],
                "fraction": row["fraction"], "revision": row["revision"], "updatedAt": row["updated_at"],
                "clientId": row["client_id"], "eventId": row["event_id"]}

    def reading_position(self, source: str, book_id: str) -> dict | None:
        with self._connect() as db:
            return self._position_row(db.execute("SELECT * FROM reading_progress WHERE source=? AND book_id=?",
                                                (require_id(source), require_id(book_id))).fetchone())

    def reading_positions(self, offset: int = 0, limit: int = 200) -> dict:
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM reading_progress ORDER BY updated_at DESC,source,book_id
                                 LIMIT ? OFFSET ?""", (min(500, max(1, limit)), max(0, offset))).fetchall()
            total = db.execute("SELECT COUNT(*) FROM reading_progress").fetchone()[0]
        return {"items": [self._position_row(row) for row in rows], "total": total}

    def save_reading_position(self, source: str, book_id: str, chapter_id: str, fraction: float,
                             base_revision: int, client_id: str, event_id: str) -> tuple[bool, dict | None]:
        source, book_id, chapter_id, client_id, event_id = map(require_id, (source, book_id, chapter_id, client_id, event_id))
        if (type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1
                or type(base_revision) is not int or not 0 <= base_revision < 2 ** 63 - 1):
            raise SourceError("Invalid reading position")
        fraction = round(float(fraction), 9)
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reading_progress WHERE source=? AND book_id=?", (source, book_id)).fetchone()
            if row and row["client_id"] == client_id and row["event_id"] == event_id:
                if row["chapter_id"] != chapter_id or row["fraction"] != fraction:
                    raise SourceError("Reading-progress event ID was reused with different data")
                return True, self._position_row(row)
            current_revision = row["revision"] if row else 0
            if base_revision != current_revision:
                return False, self._position_row(row)
            db.execute("""INSERT INTO reading_progress(source,book_id,chapter_id,fraction,revision,updated_at,client_id,event_id)
                          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,book_id) DO UPDATE SET
                          chapter_id=excluded.chapter_id,fraction=excluded.fraction,revision=excluded.revision,
                          updated_at=excluded.updated_at,client_id=excluded.client_id,event_id=excluded.event_id""",
                       (source, book_id, chapter_id, fraction, current_revision + 1, int(time.time() * 1000), client_id, event_id))
            updated = db.execute("SELECT * FROM reading_progress WHERE source=? AND book_id=?", (source, book_id)).fetchone()
            return True, self._position_row(updated)

    def list_books(self, query: str = "", source: str = "", limit: int = 200,
                   sort: BookSort = "added_desc", offset: int = 0) -> list[dict]:
        params = []
        filters = []
        if query.strip():
            filters.append("(title LIKE ? OR author LIKE ? OR source LIKE ?)")
            params.extend(["%" + query.strip()[:100] + "%"] * 3)
        if source:
            filters.append("source=?")
            params.append(require_id(source))
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._connect() as db:
            rows = db.execute(self._book_select() + where + " ORDER BY " + book_order(sort) + " LIMIT ? OFFSET ?",
                              (*params, max(1, limit), max(0, offset))).fetchall()
        return [self._book_row(r) for r in rows]

    def book_count(self, query: str = "") -> int:
        with self._connect() as db:
            pattern = "%" + query.strip()[:100] + "%"
            return int(db.execute("SELECT COUNT(*) FROM books WHERE title LIKE ? OR author LIKE ? OR source LIKE ?", (pattern, pattern, pattern)).fetchone()[0])

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

    def operational_settings(self) -> dict:
        with self.lock:
            return {
                "archiveConcurrency": self.archive_concurrency,
                "followIntervalSeconds": self.poll_seconds,
                "activeIntervalSeconds": self.active_poll_seconds,
            }

    def update_operational_settings(self, archive_concurrency: int,
                                    follow_interval_seconds: int,
                                    active_interval_seconds: int) -> dict:
        if not 1 <= archive_concurrency <= MAX_ARCHIVE_CONCURRENCY:
            raise SourceError(f"Archive concurrency must be between 1 and {MAX_ARCHIVE_CONCURRENCY}")
        for label, value in (
            ("Normal check interval", follow_interval_seconds),
            ("Active-book interval", active_interval_seconds),
        ):
            if not MIN_CHECK_INTERVAL_SECONDS <= value <= MAX_CHECK_INTERVAL_SECONDS:
                raise SourceError(
                    f"{label} must be between {MIN_CHECK_INTERVAL_SECONDS} and "
                    f"{MAX_CHECK_INTERVAL_SECONDS} seconds"
                )
        with self.lock:
            with self._connect() as db:
                values = {
                    "archive_concurrency": archive_concurrency,
                    "follow_interval_seconds": follow_interval_seconds,
                    "active_interval_seconds": active_interval_seconds,
                }
                for key, value in values.items():
                    db.execute("""INSERT INTO configuration(key,value) VALUES(?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                               (key, value))
                intervals_changed = (
                    follow_interval_seconds != self.poll_seconds
                    or active_interval_seconds != self.active_poll_seconds
                )
                if intervals_changed:
                    stamp = int(time.time())
                    rows = db.execute("""SELECT source,book_id,last_read_at,failure_count
                                         FROM follows WHERE enabled=1""").fetchall()
                    for row in rows:
                        interval = (active_interval_seconds
                                    if row["last_read_at"] >= stamp - self.active_days * 86400
                                    else follow_interval_seconds)
                        if row["failure_count"]:
                            interval = min(86400, interval * 2 ** row["failure_count"])
                        db.execute("""UPDATE follows SET next_check_at=?
                                      WHERE source=? AND book_id=?""",
                                   (stamp + interval, row["source"], row["book_id"]))
            self.archive_concurrency = archive_concurrency
            self.poll_seconds = follow_interval_seconds
            self.active_poll_seconds = active_interval_seconds
            return {
                "archiveConcurrency": self.archive_concurrency,
                "followIntervalSeconds": self.poll_seconds,
                "activeIntervalSeconds": self.active_poll_seconds,
            }

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

    @staticmethod
    def _validate_refresh_range(refresh: bool, start_chapter: int | None, end_chapter: int | None) -> None:
        if start_chapter is None and end_chapter is None:
            return
        if not refresh:
            raise SourceError("Chapter ranges are only supported when re-archiving")
        if (type(start_chapter) is not int or type(end_chapter) is not int
                or not 1 <= start_chapter <= end_chapter <= 100_000):
            raise SourceError("Provide both chapter bounds as whole numbers: 1 <= start <= end <= 100000")

    def _enqueue(self, db, source: str, book_id: str, refresh: bool,
                 start_chapter: int | None = None, end_chapter: int | None = None) -> dict:
        self._validate_refresh_range(refresh, start_chapter, end_chapter)
        row = db.execute("""SELECT * FROM jobs WHERE source=? AND book_id=?
                            AND status IN ('queued','running','paused') ORDER BY created_at DESC LIMIT 1""",
                         (source, book_id)).fetchone()
        if row:
            if refresh and not row["refresh"]:
                raise SourceError("Finish or cancel the active archive job before re-archiving this novel")
            if refresh and (row["range_start"], row["range_end"]) != (start_chapter, end_chapter):
                raise SourceError("A different re-archive range is already active for this novel; finish or cancel it first")
            return self._job_row(row)
        total = 0
        if refresh:
            targets_sql = "SELECT id FROM chapters WHERE source=? AND book_id=? AND body IS NOT NULL"
            target_params = (source, book_id)
            if start_chapter is not None:
                indexed = db.execute("SELECT COUNT(*) FROM chapters WHERE source=? AND book_id=?",
                                     (source, book_id)).fetchone()[0]
                if end_chapter > indexed:
                    raise SourceError(f"Chapter range exceeds the {indexed} indexed chapters for {source}/{book_id}")
                # Slice the full chapter index first, so missing saved bodies do not shift positions.
                targets_sql = """SELECT id FROM (
                    SELECT id,body IS NOT NULL AS stored FROM chapters WHERE source=? AND book_id=?
                    ORDER BY ordinal,id LIMIT ? OFFSET ?) WHERE stored=1"""
                target_params = (source, book_id, end_chapter - start_chapter + 1, start_chapter - 1)
            total = db.execute("SELECT COUNT(*) FROM (" + targets_sql + ")", target_params).fetchone()[0]
            if not total:
                raise SourceError("This chapter range has no saved chapters to re-archive" if start_chapter is not None
                                  else "This novel has no saved chapters to re-archive")
        stamp = int(time.time())
        job_id = uuid4().hex
        db.execute("""INSERT INTO jobs(id,source,book_id,status,refresh,total,range_start,range_end,created_at,updated_at)
                      VALUES(?,?,?,'queued',?,?,?,?,?,?)""",
                   (job_id, source, book_id, int(refresh), total, start_chapter, end_chapter, stamp, stamp))
        if refresh:
            db.execute("INSERT INTO refresh_chapters(job_id,chapter_id) SELECT ?,id FROM (" + targets_sql + ")",
                       (job_id, *target_params))
        return self._job_row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue(self, source: str, book_id: str, refresh: bool = False,
                start_chapter: int | None = None, end_chapter: int | None = None) -> dict:
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._enqueue(db, require_id(source), require_id(book_id), refresh, start_chapter, end_chapter)

    def enqueue_refresh_batch(self, items: list[tuple[str, str]], start_chapter: int | None = None,
                              end_chapter: int | None = None) -> list[dict]:
        if not 1 <= len(items) <= 50:
            raise SourceError("Select between 1 and 50 novels")
        normalized = list(dict.fromkeys((require_id(source), require_id(book_id)) for source, book_id in items))
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return [self._enqueue(db, source, book_id, True, start_chapter, end_chapter) for source, book_id in normalized]

    def refresh_targets(self, job_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT r.chapter_id AS id,r.refreshed FROM refresh_chapters r
                                 JOIN jobs j ON j.id=r.job_id
                                 LEFT JOIN chapters c ON c.source=j.source AND c.book_id=j.book_id AND c.id=r.chapter_id
                                 WHERE r.job_id=? ORDER BY c.ordinal,r.chapter_id""", (require_id(job_id),)).fetchall()
        return [dict(row) for row in rows]

    def job(self, job_id: str):
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (require_id(job_id),)).fetchone()
        return self._job_row(row)

    def job_count(self, history_only: bool = False):
        where = " WHERE status NOT IN ('queued','running','paused')" if history_only else ""
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs" + where).fetchone()[0]

    def jobs(self, history_only: bool = False, offset: int = 0, limit: int = 100):
        where = " WHERE status NOT IN ('queued','running','paused')" if history_only else ""
        with self._connect() as db:
            return [self._job_row(row) for row in db.execute(
                "SELECT * FROM jobs" + where + " ORDER BY updated_at DESC,created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                (min(500, max(1, limit)), max(0, offset))).fetchall()]

    def active_jobs(self):
        with self._connect() as db:
            return [self._job_row(row) for row in db.execute("""SELECT * FROM jobs
                WHERE status IN ('queued','running','paused')
                ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                         created_at,rowid""").fetchall()]

    def next_job(self, excluding: set[tuple[str, str]] | None = None):
        excluded = excluding or set()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            filters = ["status='queued'"]
            params = []
            for source, book_id in excluded:
                filters.append("NOT (source=? AND book_id=?)")
                params.extend((source, book_id))
            row = db.execute(f"""SELECT * FROM jobs WHERE {' AND '.join(filters)}
                                 ORDER BY created_at, id LIMIT 1""", params).fetchone()
            if not row:
                return None
            stamp = int(time.time())
            run_token = uuid4().hex
            claimed = db.execute("""UPDATE jobs SET status='running',run_token=?,updated_at=?
                                    WHERE id=? AND status='queued'""",
                                 (run_token, stamp, row["id"])).rowcount
            if not claimed:
                return None
            result = dict(row)
            result.update(status="running", run_token=run_token, updated_at=stamp)
            return result

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
        if status is not None and status != "running":
            values["run_token"] = ""
        columns = ",".join(f"{key}=?" for key in values)
        with self.lock, self._connect() as db:
            db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), require_id(job_id)))

    @staticmethod
    def _running_claim(db, job_id: str, run_token: str) -> bool:
        return db.execute("""SELECT 1 FROM jobs
                             WHERE id=? AND status='running' AND run_token=?""",
                          (require_id(job_id), run_token)).fetchone() is not None

    def update_job_if_running(self, job_id: str, run_token: str,
                              status: str | None = None, total: int | None = None,
                              downloaded: int | None = None, error: str | None = None) -> bool:
        values = {"updated_at": int(time.time())}
        if status is not None:
            values["status"] = status
            if status != "running":
                values["run_token"] = ""
        if total is not None:
            values["total"] = total
        if downloaded is not None:
            values["downloaded"] = downloaded
        if error is not None:
            values["error"] = error[:300]
        columns = ",".join(f"{key}=?" for key in values)
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                f"UPDATE jobs SET {columns} WHERE id=? AND status='running' AND run_token=?",
                (*values.values(), require_id(job_id), run_token),
            ).rowcount
        return bool(changed)

    def finish_job_if_running(self, job_id: str, run_token: str, source: str,
                              book_id: str, status: str, error: str = "") -> bool:
        if status not in {"completed", "failed"}:
            raise ValueError("Archive jobs can only finish as completed or failed")
        source, book_id = require_id(source), require_id(book_id)
        stamp = int(time.time())
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._running_claim(db, job_id, run_token):
                return False
            refreshing = bool(db.execute("SELECT refresh FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
            follow = db.execute("""SELECT failure_count,last_read_at FROM follows
                                   WHERE source=? AND book_id=?""",
                                (source, book_id)).fetchone() if not refreshing else None
            if follow:
                failures = min(follow["failure_count"] + 1, 8) if error else 0
                interval = self.interval_for(follow["last_read_at"], stamp)
                if error:
                    interval = min(86400, interval * 2 ** failures)
                db.execute("""UPDATE follows SET last_checked_at=?,
                              last_success_at=CASE WHEN ?='' THEN ? ELSE last_success_at END,
                              last_error=?,failure_count=?,next_check_at=?
                              WHERE source=? AND book_id=?""",
                           (stamp, error, stamp, error[:300], failures, stamp + interval,
                            source, book_id))
            db.execute("""UPDATE jobs SET status=?,error=?,run_token='',updated_at=?
                          WHERE id=? AND status='running' AND run_token=?""",
                       (status, error[:300], stamp, require_id(job_id), run_token))
            return True

    def save_metadata_if_running(self, job_id: str, run_token: str,
                                 source: str, book: dict, chapters: list[dict]) -> bool:
        source = require_id(source)
        book_id = require_id(book["id"])
        if len(chapters) > 100_000:
            raise SourceError("Book has too many chapters")
        normalized = [
            (require_id(chapter["id"]), chapter["title"], int(chapter.get("ordinal", index)))
            for index, chapter in enumerate(chapters)
        ]
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._running_claim(db, job_id, run_token):
                return False
            db.execute("""
                INSERT INTO books(source, id, title, author, description, cover_url, genre, updated_at, publication_status)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, id) DO UPDATE SET
                    title=excluded.title, author=excluded.author,
                    description=excluded.description, cover_url=excluded.cover_url,
                    genre=excluded.genre, updated_at=excluded.updated_at, publication_status=excluded.publication_status
            """, (source, book_id, book["title"], book.get("author", ""),
                  book.get("description", ""), book.get("coverUrl", ""),
                  book.get("genre", ""), int(time.time()), publication_status(book.get("publicationStatus"))))
            for chapter_id, title, ordinal in normalized:
                db.execute("""
                    INSERT INTO chapters(source, book_id, id, title, ordinal)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(source, book_id, id) DO UPDATE SET
                        title=excluded.title, ordinal=excluded.ordinal
                """, (source, book_id, chapter_id, title, ordinal))
            return True

    def save_cover_if_running(self, job_id: str, run_token: str, source: str,
                              book_id: str, mime_type: str, data: bytes) -> bool:
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
            db.execute("BEGIN IMMEDIATE")
            if not self._running_claim(db, job_id, run_token):
                return False
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
            return True

    def save_text_if_running(self, job_id: str, run_token: str, source: str,
                             book_id: str, chapter_id: str, body: str,
                             downloaded: int) -> bool:
        size = len(body.encode("utf-8"))
        if not body.strip() or size > MAX_TEXT_BYTES:
            raise SourceError("Empty or oversized chapter")
        source, book_id, chapter_id = map(require_id, (source, book_id, chapter_id))
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._running_claim(db, job_id, run_token):
                return False
            refresh = bool(db.execute("SELECT refresh FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
            if refresh:
                target = db.execute("SELECT refreshed FROM refresh_chapters WHERE job_id=? AND chapter_id=?",
                                    (job_id, chapter_id)).fetchone()
                if target is None:
                    raise SourceError("Chapter is not part of this re-archive job")
                if target["refreshed"]:
                    return True
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
            if refresh:
                db.execute("UPDATE refresh_chapters SET refreshed=1 WHERE job_id=? AND chapter_id=?", (job_id, chapter_id))
                downloaded = db.execute("SELECT COUNT(*) FROM refresh_chapters WHERE job_id=? AND refreshed=1", (job_id,)).fetchone()[0]
            db.execute("""UPDATE jobs SET downloaded=?,updated_at=?
                          WHERE id=? AND status='running' AND run_token=?""",
                       (downloaded, int(time.time()), require_id(job_id), run_token))
            return True

    def requeue_if_running(self, job_id: str, run_token: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("""UPDATE jobs SET status='queued',run_token='',updated_at=?
                          WHERE id=? AND status='running' AND run_token=?""",
                       (int(time.time()), require_id(job_id), run_token))

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
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM jobs WHERE id=?",
                                 (require_id(job_id),)).fetchone()
            if current is None:
                raise SourceError("Job not found")
            if current["status"] not in accepted:
                raise SourceError("Job action not possible from this state")
            if target == "queued":
                duplicate = db.execute("""SELECT 1 FROM jobs WHERE source=? AND book_id=?
                                          AND id<>? AND status IN ('queued','running','paused')
                                          LIMIT 1""",
                                       (current["source"], current["book_id"], current["id"])).fetchone()
                if duplicate:
                    raise SourceError("Another archive job is already active for this novel")
            if action == "retry" and current["refresh"] and current["status"] == "completed":
                db.execute("UPDATE refresh_chapters SET refreshed=0 WHERE job_id=?", (current["id"],))
                db.execute("UPDATE jobs SET downloaded=0 WHERE id=?", (current["id"],))
            db.execute("UPDATE jobs SET status=?,error='',run_token='',updated_at=? WHERE id=?",
                       (target, int(time.time()), current["id"]))
            updated = db.execute("SELECT * FROM jobs WHERE id=?", (current["id"],)).fetchone()
            return self._job_row(updated)

    def delete_book(self, source: str, book_id: str):
        source, book_id = require_id(source), require_id(book_id)
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
    def __init__(self, store: ArchiveStore, sources: list[Source], concurrency: int = 3):
        if not 1 <= concurrency <= MAX_ARCHIVE_CONCURRENCY:
            raise ValueError(f"Archive concurrency must be between 1 and {MAX_ARCHIVE_CONCURRENCY}")
        self.store = store
        self.sources = {source.id: source for source in sources}
        self.concurrency = concurrency
        self.stop_signal = threading.Event()
        self.condition = threading.Condition()
        self.threads: dict[int, threading.Thread] = {}
        self.active_books: set[tuple[str, str]] = set()
        self.active_claims: dict[str, str] = {}
        self.started = False

    def _launch_thread(self, index: int) -> None:
        thread = threading.Thread(target=self._run_slot, args=(index,), daemon=True,
                                  name=f"quietleaf-archive-worker-{index + 1}")
        self.threads[index] = thread
        thread.start()

    def _start_missing_threads(self) -> None:
        for index in range(self.concurrency):
            current = self.threads.get(index)
            if current is not None and current.is_alive():
                continue
            self._launch_thread(index)

    def _run_slot(self, index: int) -> None:
        current = threading.current_thread()
        try:
            self.run(index)
        finally:
            with self.condition:
                if (self.threads.get(index) is current and self.started
                        and not self.stop_signal.is_set() and index < self.concurrency):
                    self._launch_thread(index)
                else:
                    if self.threads.get(index) is current:
                        self.threads.pop(index, None)
                    if self.stop_signal.is_set() and not any(
                        thread.is_alive() for thread in self.threads.values()
                    ):
                        self.started = False

    def start(self):
        with self.condition:
            old_threads = ([thread for thread in self.threads.values() if thread.is_alive()]
                           if self.started and self.stop_signal.is_set() else [])
        deadline = time.monotonic() + ARCHIVE_STOP_TIMEOUT_SECONDS
        for thread in old_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in old_threads):
            raise RuntimeError("Archive workers are still stopping")
        with self.condition:
            if self.started and not self.stop_signal.is_set():
                return
            self.stop_signal.clear()
            self.started = True
            self._start_missing_threads()

    def stop(self):
        with self.condition:
            self.stop_signal.set()
            self.condition.notify_all()
            threads = list(self.threads.values())
        deadline = time.monotonic() + ARCHIVE_STOP_TIMEOUT_SECONDS
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        with self.condition:
            claims = list(self.active_claims.items())
            if not any(thread.is_alive() for thread in self.threads.values()):
                self.started = False
        for job_id, run_token in claims:
            self.store.requeue_if_running(job_id, run_token)

    def set_concurrency(self, concurrency: int) -> None:
        if not 1 <= concurrency <= MAX_ARCHIVE_CONCURRENCY:
            raise ValueError(f"Archive concurrency must be between 1 and {MAX_ARCHIVE_CONCURRENCY}")
        with self.condition:
            self.concurrency = concurrency
            if self.started:
                self._start_missing_threads()
            self.condition.notify_all()

    def update_operational_settings(self, archive_concurrency: int,
                                    follow_interval_seconds: int,
                                    active_interval_seconds: int) -> dict:
        with self.condition:
            settings = self.store.update_operational_settings(
                archive_concurrency, follow_interval_seconds, active_interval_seconds
            )
            self.concurrency = settings["archiveConcurrency"]
            if self.started:
                self._start_missing_threads()
            self.condition.notify_all()
            return settings

    def wake(self):
        with self.condition:
            self.condition.notify_all()

    def is_running(self) -> bool:
        with self.condition:
            return (self.started and not self.stop_signal.is_set()
                    and all(index in self.threads and self.threads[index].is_alive()
                            for index in range(self.concurrency)))

    def active_count(self) -> int:
        with self.condition:
            return len(self.active_books)

    def delete_book(self, source: str, book_id: str) -> bool:
        key = require_id(source), require_id(book_id)
        with self.condition:
            return self.store.delete_book(*key)

    def run(self, index: int):
        while not self.stop_signal.is_set():
            job = None
            with self.condition:
                if self.stop_signal.is_set() or index >= self.concurrency:
                    current = threading.current_thread()
                    if index >= self.concurrency and self.threads.get(index) is current:
                        self.threads.pop(index, None)
                    return
                job = self.store.next_job(self.active_books)
                if job:
                    self.active_books.add((job["source"], job["book_id"]))
                    self.active_claims[job["id"]] = job["run_token"]
                else:
                    due = self.store.reserve_due()
                    for source_id, book_id in due:
                        if source_id in self.sources:
                            self.store.enqueue(source_id, book_id)
                        else:
                            self.store.record_check(source_id, book_id, "Source adapter not configured")
                    if not due and not self.stop_signal.is_set():
                        self.condition.wait(30)
            if job:
                try:
                    self.process(job)
                finally:
                    if self.stop_signal.is_set():
                        self.store.requeue_if_running(job["id"], job["run_token"])
                    with self.condition:
                        self.active_books.discard((job["source"], job["book_id"]))
                        self.active_claims.pop(job["id"], None)
                        self.condition.notify_all()
                continue

    def process(self, job: dict):
        job_id, source_id, book_id = job["id"], job["source"], job["book_id"]
        run_token = job["run_token"]
        adapter = self.sources.get(source_id)
        if not adapter:
            self.store.finish_job_if_running(
                job_id, run_token, source_id, book_id, "failed",
                "Source adapter is not configured"
            )
            return
        try:
            details = adapter.details(book_id)
            if self.stop_signal.is_set():
                return
            chapter_list = adapter.chapters(book_id)
            if self.stop_signal.is_set():
                return
            if not self.store.save_metadata_if_running(
                job_id, run_token, source_id, details, chapter_list
            ):
                return
            if details.get("coverUrl") and not self.store.has_cover(source_id, book_id):
                fetch_cover = getattr(adapter, "cover_image", None)
                if callable(fetch_cover):
                    try:
                        mime, binary = fetch_cover(details["coverUrl"])
                        if self.stop_signal.is_set():
                            return
                        if not self.store.save_cover_if_running(
                            job_id, run_token, source_id, book_id, mime, binary
                        ):
                            return
                    except Exception:
                        pass
            available = {ch["id"]: ch for ch in chapter_list if ch.get("downloadable", False)}
            unavailable = []
            refreshing = bool(job.get("refresh"))
            if refreshing:
                targets = self.store.refresh_targets(job_id)
                downloaded = sum(bool(target["refreshed"]) for target in targets)
                pending = [target for target in targets if not target["refreshed"]]
                candidates = [available[target["id"]] for target in pending if target["id"] in available]
                unavailable = [target["id"] for target in pending if target["id"] not in available]
                total = len(targets)
            else:
                candidates = list(available.values())
                downloaded = sum(self.store.text(source_id, book_id, ch["id"]) is not None for ch in candidates)
                total = len(candidates)
            if not self.store.update_job_if_running(
                job_id, run_token, total=total, downloaded=downloaded
            ):
                return
            for chapter in candidates:
                if self.stop_signal.is_set():
                    return
                if not refreshing and self.store.text(source_id, book_id, chapter["id"]) is not None:
                    continue
                body = clean_chapter_text(source_id, adapter.text(book_id, chapter["id"]))
                if self.stop_signal.is_set():
                    return
                if not self.store.save_text_if_running(
                    job_id, run_token, source_id, book_id, chapter["id"], body,
                    downloaded + 1
                ):
                    return
                downloaded += 1
            if unavailable:
                raise SourceError(f"{len(unavailable)} saved chapter(s) are no longer downloadable from this source (first: {unavailable[0]}). Their saved copies were retained.")
            self.store.finish_job_if_running(
                job_id, run_token, source_id, book_id, "completed"
            )
        except Exception as exc:
            self.store.finish_job_if_running(
                job_id, run_token, source_id, book_id, "failed", str(exc)[:300]
            )
