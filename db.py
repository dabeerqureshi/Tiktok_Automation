"""
SQLite database manager — tracks processed riddle videos and render failures.

Ensures no vid_N is downloaded, rendered, or uploaded more than once, and
prevents infinite retry loops on broken files:

  status     meaning
  -------    ---------------------------------------------------------
  completed  Rendered + uploaded successfully — never reprocessed
  pending    Cleared failure state (file replaced or retry window opened)
  failed     A render attempt failed; retried with exponential backoff,
             then exhausted after MAX_RENDER_ATTEMPTS
             (auto-resets when the source file's md5 changes)
"""
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

import config

logger = logging.getLogger("TikTokDaemon")


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a WAL-mode SQLite connection."""
    path = Path(db_path) if db_path else config.DB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path: Optional[Path] = None):
    """Creates the processed_renders table if it doesn't exist. Migrates old schema."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_renders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ai_filename     TEXT UNIQUE NOT NULL,
                final_filename  TEXT,
                ai_drive_id     TEXT,
                final_drive_id  TEXT,
                sheet_row       INTEGER,
                answer          TEXT,
                status          TEXT DEFAULT 'completed',
                processed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Non-destructive migration for old tables that may have orig_* columns
        cursor.execute("PRAGMA table_info(processed_renders)")
        existing_cols = {row[1] for row in cursor.fetchall()}

        new_cols = {
            "ai_drive_id":    "TEXT",
            "final_drive_id": "TEXT",
            "sheet_row":      "INTEGER",
            "answer":         "TEXT",
            "md5_checksum":   "TEXT",
            "attempts":       "INTEGER DEFAULT 0",
            "last_error":     "TEXT",
            "next_retry_at":  "REAL",
        }
        for col, col_type in new_cols.items():
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE processed_renders ADD COLUMN {col} {col_type}")

        conn.commit()


def get_record(ai_filename: str, db_path: Optional[Path] = None) -> Optional[Dict]:
    """Returns the full DB record for a file, or None if it was never seen."""
    with get_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM processed_renders WHERE ai_filename = ?",
            (ai_filename,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def is_already_processed(ai_filename: str, db_path: Optional[Path] = None) -> bool:
    """Returns True if ai_filename has already been rendered + uploaded."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM processed_renders WHERE ai_filename = ? AND status = 'completed'",
            (ai_filename,)
        )
        return cursor.fetchone() is not None


def mark_as_processed(
    ai_filename: str,
    final_filename: str,
    sheet_row: Optional[int] = None,
    answer: Optional[str] = None,
    ai_drive_id: Optional[str] = None,
    final_drive_id: Optional[str] = None,
    md5_checksum: Optional[str] = None,
    db_path: Optional[Path] = None,
):
    """Inserts or updates a completed render record and clears any failure state."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO processed_renders
                (ai_filename, final_filename, ai_drive_id, final_drive_id,
                 sheet_row, answer, md5_checksum, status, attempts,
                 last_error, next_retry_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 0, NULL, NULL)
            ON CONFLICT(ai_filename) DO UPDATE SET
                final_filename  = excluded.final_filename,
                ai_drive_id     = excluded.ai_drive_id,
                final_drive_id  = excluded.final_drive_id,
                sheet_row       = excluded.sheet_row,
                answer          = excluded.answer,
                md5_checksum    = excluded.md5_checksum,
                status          = 'completed',
                attempts        = 0,
                last_error      = NULL,
                next_retry_at   = NULL,
                processed_at    = CURRENT_TIMESTAMP
        ''', (ai_filename, final_filename, ai_drive_id, final_drive_id,
              sheet_row, answer, md5_checksum))
        conn.commit()


def _backoff_delay(attempt: int) -> float:
    """Returns the backoff delay in seconds for the given attempt number."""
    delays = config.RETRY_BACKOFF_DELAYS or [300]
    idx = min(max(int(attempt) - 1, 0), len(delays) - 1)
    return float(delays[idx])


def register_failure(
    ai_filename: str,
    error: str,
    md5_checksum: Optional[str] = None,
    ai_drive_id: Optional[str] = None,
    sheet_row: Optional[int] = None,
    answer: Optional[str] = None,
    permanent: bool = False,
    db_path: Optional[Path] = None,
) -> int:
    """
    Records a failed processing attempt with exponential backoff.

    `permanent=True` marks the file as exhausted immediately (used for
    unrecoverable errors like a missing sheet row or an invalid clip — these
    only reprocess once the source file is replaced, i.e. its md5 changes).

    Returns the new attempt count.
    """
    rec = get_record(ai_filename, db_path)
    attempt = int(rec["attempts"]) + 1 if rec and rec.get("attempts") else 1
    if permanent:
        attempt = int(config.MAX_RENDER_ATTEMPTS)

    delay = _backoff_delay(attempt) if not permanent else 0.0
    next_retry_at = time.time() + delay if not permanent else 0.0
    error_snippet = (error or "unknown error")[:500]

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO processed_renders
                (ai_filename, ai_drive_id, sheet_row, answer, md5_checksum,
                 status, attempts, last_error, next_retry_at)
            VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?)
            ON CONFLICT(ai_filename) DO UPDATE SET
                ai_drive_id     = excluded.ai_drive_id,
                sheet_row       = excluded.sheet_row,
                answer          = excluded.answer,
                md5_checksum    = excluded.md5_checksum,
                status          = 'failed',
                attempts        = excluded.attempts,
                last_error      = excluded.last_error,
                next_retry_at   = excluded.next_retry_at,
                processed_at    = CURRENT_TIMESTAMP
        ''', (ai_filename, ai_drive_id, sheet_row, answer, md5_checksum,
              attempt, error_snippet, next_retry_at))
        conn.commit()

    if permanent or not delay:
        logger.warning(
            f"  Failure for '{ai_filename}' (permanent): {error_snippet} "
            f"— no auto-retry until the source file is replaced in Drive."
        )
    else:
        logger.warning(
            f"  Failure #{attempt} for '{ai_filename}': {error_snippet} "
            f"— next retry in {int(delay)}s."
        )
    return attempt


def reset_failure(ai_filename: str, db_path: Optional[Path] = None):
    """Clears failure state so the file can be reprocessed (file replaced / retry due)."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE processed_renders SET status='pending', attempts=0, "
            "last_error=NULL, next_retry_at=NULL WHERE ai_filename = ?",
            (ai_filename,),
        )
        conn.commit()


def is_retryable(ai_filename: str, current_md5: Optional[str] = None,
                 db_path: Optional[Path] = None) -> bool:
    """
    Returns True if the file should be processed/probed right now.

    - Unknown file            → True
    - Completed               → False (already done)
    - Failed & file replaced  → True (md5 changed → auto-reset)
    - Failed & attempts capped → False (exhausted)
    - Failed & in backoff     → False (wait for next_retry_at)
    - Failed & retry due      → True
    """
    rec = get_record(ai_filename, db_path)
    if rec is None:
        return True
    status = rec.get("status")
    if status == "completed":
        return False
    if status == "failed":
        stored_md5 = rec.get("md5_checksum") or ""
        if current_md5 and stored_md5 and current_md5 != stored_md5:
            return True
        if int(rec.get("attempts") or 0) >= int(config.MAX_RENDER_ATTEMPTS):
            return False
        return time.time() >= float(rec.get("next_retry_at") or 0.0)
    return True  # 'pending' or any other status


def get_processed_count(db_path: Optional[Path] = None) -> int:
    """Returns the total number of successfully completed riddle videos."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM processed_renders WHERE status = 'completed'")
        row = cursor.fetchone()
        return row[0] if row else 0
