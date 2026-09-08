from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import AttachmentRef, InboundEnvelope, JobRecord, SavedFile


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                key = str(key)
            safe[key] = _json_safe(item)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


class SqliteJobStore:
    EDITOR_SAFETY_RETENTION = 30 * 24 * 60 * 60
    EDITOR_SAFETY_MAX_RECORDS = 100000

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT,
                    message_id TEXT NOT NULL,
                    reply_to_message_id TEXT,
                    text TEXT,
                    attachment_index INTEGER NOT NULL,
                    external_file_id TEXT,
                    original_file_name TEXT NOT NULL,
                    mime_type TEXT,
                    original_size_bytes INTEGER,
                    file_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    transport_meta_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at TEXT,
                    locked_by TEXT,
                    locked_at TEXT,
                    result_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_next_attempt ON jobs(status, next_attempt_at, created_at)"
            )

    def _prepare_editor_safety(self, connection, now):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS editor_report_safety ("
            "editor TEXT NOT NULL, chat_id TEXT NOT NULL, report_key TEXT NOT NULL, "
            "task_key TEXT NOT NULL, recorded_at REAL NOT NULL, "
            "PRIMARY KEY (editor, chat_id, report_key, task_key))"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_editor_safety_time ON editor_report_safety(recorded_at)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS editor_safety_state ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), blocked_until REAL NOT NULL)"
        )
        connection.execute(
            "DELETE FROM editor_report_safety WHERE recorded_at <= ?",
            (now - self.EDITOR_SAFETY_RETENTION,),
        )

    def remember_editor_report(self, editor, chat_id, report_key, task_key, now=None):
        now = time.time() if now is None else now
        with self._lock, self._connect() as connection:
            self._prepare_editor_safety(connection, now)
            existing = connection.execute(
                "SELECT 1 FROM editor_report_safety WHERE editor=? AND chat_id=? AND report_key=? AND task_key=?",
                (editor, str(chat_id), report_key, task_key),
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM editor_report_safety").fetchone()[0]
            if existing or count < self.EDITOR_SAFETY_MAX_RECORDS:
                connection.execute(
                    "INSERT OR REPLACE INTO editor_report_safety VALUES (?, ?, ?, ?, ?)",
                    (editor, str(chat_id), report_key, task_key, now),
                )
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO editor_safety_state VALUES (1, ?)",
                    (now + self.EDITOR_SAFETY_RETENTION,),
                )
            connection.commit()

    def editor_report_conflicts(self, editor, chat_id, report_key, task_key, now=None):
        now = time.time() if now is None else now
        with self._lock, self._connect() as connection:
            self._prepare_editor_safety(connection, now)
            blocked = connection.execute(
                "SELECT 1 FROM editor_safety_state WHERE blocked_until > ?", (now,)
            ).fetchone()
            conflict = connection.execute(
                "SELECT 1 FROM editor_report_safety "
                "WHERE editor=? AND chat_id=? AND report_key=? AND task_key<>? LIMIT 1",
                (editor, str(chat_id), report_key, task_key),
            ).fetchone()
            connection.commit()
            return bool(blocked or conflict)

    def enqueue_file(self, envelope: InboundEnvelope, attachment: AttachmentRef, saved_file: SavedFile) -> JobRecord:
        dedupe_key = envelope.attachment_dedupe_key(attachment)
        now = utc_now_iso()
        transport_meta_json = json.dumps(_json_safe(attachment.adapter_meta), ensure_ascii=False, sort_keys=True)

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    dedupe_key, source, chat_id, sender_id, sender_name, message_id, reply_to_message_id, text,
                    attachment_index, external_file_id, original_file_name, mime_type, original_size_bytes,
                    file_path, file_sha256, file_size_bytes, transport_meta_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    dedupe_key,
                    envelope.source,
                    envelope.chat_id,
                    envelope.sender_id,
                    envelope.sender_name,
                    envelope.message_id,
                    envelope.reply_to_message_id,
                    envelope.text,
                    attachment.index,
                    attachment.external_id,
                    saved_file.original_file_name,
                    saved_file.mime_type,
                    attachment.size_bytes,
                    str(saved_file.path),
                    saved_file.sha256,
                    saved_file.size_bytes,
                    transport_meta_json,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            connection.commit()

        if row is None:
            raise RuntimeError(f"Failed to fetch job for dedupe_key={dedupe_key}")
        return self._row_to_job(row)

    def get_job(self, job_id: int) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def get_by_dedupe_key(self, dedupe_key: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        return self._row_to_job(row) if row else None

    def claim_next_queued(self, worker_name: str) -> JobRecord | None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()

            if row is None:
                connection.commit()
                return None

            connection.execute(
                """
                UPDATE jobs
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_name, now, now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            connection.commit()

        return self._row_to_job(claimed) if claimed else None

    def claim_next_bridgeable(self, worker_name: str, sources: tuple[str, ...]) -> JobRecord | None:
        if not sources:
            return None

        now = utc_now_iso()
        placeholders = ",".join("?" for _ in sources)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = 'queued'
                  AND source IN ({placeholders})
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (*sources, now),
            ).fetchone()

            if row is None:
                connection.commit()
                return None

            connection.execute(
                """
                UPDATE jobs
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_name, now, now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            connection.commit()

        return self._row_to_job(claimed) if claimed else None

    def mark_done(self, job_id: int, result_path: str | None = None, note: str | None = None) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'done',
                    result_path = ?,
                    last_error = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (result_path, note, now, job_id),
            )

    def mark_failed(self, job_id: int, error_text: str, retry_delay_seconds: int | None = None) -> None:
        now = datetime.now(timezone.utc)
        next_attempt_at = None
        status = "failed"
        if retry_delay_seconds is not None and retry_delay_seconds > 0:
            status = "queued"
            next_attempt_at = (now + timedelta(seconds=retry_delay_seconds)).isoformat()

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    last_error = ?,
                    next_attempt_at = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, error_text[:2000], next_attempt_at, now.isoformat(), job_id),
            )

    def mark_delivery_pending(self, job_id: int, note: str):
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='delivery_pending', last_error=?, next_attempt_at=NULL, "
                "locked_by=NULL, locked_at=NULL, updated_at=? WHERE id=?",
                (note, utc_now_iso(), job_id),
            )

    def mark_waiting_editor(self, job_id: int, note: str | None = None) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'awaiting_editor',
                    last_error = ?,
                    next_attempt_at = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (note, now, job_id),
            )

    def mark_pending_before_done(
        self,
        cutoff_iso: str,
        sources: tuple[str, ...] | None = None,
        note: str = "skipped_pre_start",
    ) -> int:
        params: list[Any] = [note, utc_now_iso(), cutoff_iso]
        source_sql = ""
        if sources:
            placeholders = ",".join("?" for _ in sources)
            source_sql = f" AND source IN ({placeholders})"
            params.extend(sources)

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status = 'done',
                    result_path = NULL,
                    last_error = ?,
                    next_attempt_at = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE status IN ('queued', 'processing')
                  AND created_at < ?
                  {source_sql}
                """,
                params,
            )
            return cursor.rowcount or 0

    def release_stale_processing(self, older_than_seconds: int = 900, sources: tuple[str, ...] | None = None) -> int:
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=max(older_than_seconds, 0))).isoformat()
        params: list[Any] = [threshold]
        source_sql = ""
        if sources:
            placeholders = ",".join("?" for _ in sources)
            source_sql = f" AND source IN ({placeholders})"
            params.extend(sources)

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status = 'queued',
                    last_error = COALESCE(last_error, 'stale lock recovered'),
                    next_attempt_at = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE status = 'processing'
                  AND locked_at IS NOT NULL
                  AND locked_at <= ?
                  {source_sql}
                """,
                [utc_now_iso(), *params],
            )
            return cursor.rowcount or 0

    def get_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM jobs
                GROUP BY status
                """
            ).fetchall()
        stats = {"queued": 0, "processing": 0, "awaiting_editor": 0, "delivery_pending": 0, "done": 0, "failed": 0}
        for row in rows:
            stats[row["status"]] = row["total"]
        stats["total"] = sum(stats.values())
        return stats

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_vk_counter_jobs(self, peer_id: str, sender_ids: tuple[str, ...] = ()) -> list[JobRecord]:
        params: list[Any] = ["vk", str(peer_id)]
        sender_sql = ""
        if sender_ids:
            placeholders = ",".join("?" for _ in sender_ids)
            sender_sql = f" AND sender_id IN ({placeholders})"
            params.extend(str(item) for item in sender_ids)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE source = ?
                  AND chat_id = ?
                  {sender_sql}
                ORDER BY created_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_vk_jobs_by_sender(self, sender_ids: tuple[str, ...] = ()) -> list[JobRecord]:
        """Возвращает все VK-джобы по sender_id без фильтрации по peer_id."""
        params: list[Any] = ["vk"]
        sender_sql = ""
        if sender_ids:
            placeholders = ",".join("?" for _ in sender_ids)
            sender_sql = f" AND sender_id IN ({placeholders})"
            params.extend(str(item) for item in sender_ids)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE source = ?
                  {sender_sql}
                ORDER BY created_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_vk_peer_ids_by_sender(self, sender_ids: tuple[str, ...] = ()) -> list[str]:
        """Возвращает VK peer_id, где выбранные sender_id уже присылали документы."""
        params: list[Any] = ["vk"]
        sender_sql = ""
        if sender_ids:
            placeholders = ",".join("?" for _ in sender_ids)
            sender_sql = f" AND sender_id IN ({placeholders})"
            params.extend(str(item) for item in sender_ids)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chat_id, MIN(created_at) AS first_seen_at, MIN(id) AS first_job_id
                FROM jobs
                WHERE source = ?
                  {sender_sql}
                GROUP BY chat_id
                ORDER BY first_seen_at ASC, first_job_id ASC
                """,
                params,
            ).fetchall()
        return [str(row["chat_id"]) for row in rows if row["chat_id"] not in (None, "")]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        transport_meta = json.loads(row["transport_meta_json"]) if row["transport_meta_json"] else {}
        return JobRecord(
            job_id=row["id"],
            dedupe_key=row["dedupe_key"],
            source=row["source"],
            chat_id=row["chat_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            message_id=row["message_id"],
            reply_to_message_id=row["reply_to_message_id"],
            text=row["text"],
            attachment_index=row["attachment_index"],
            original_file_name=row["original_file_name"],
            mime_type=row["mime_type"],
            file_path=row["file_path"],
            file_sha256=row["file_sha256"],
            file_size_bytes=row["file_size_bytes"],
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            locked_by=row["locked_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_path=row["result_path"],
            transport_meta=transport_meta,
        )
