"""
SQLite database manager — tracks processed riddle videos.
Ensures no vid_N is downloaded, rendered, or uploaded more than once.

Schema updated for the 4-segment pipeline (no orig_filename/orig_drive_id).
"""
import sqlite3
from pathlib import Path
from typing import Optional
import config


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
        }
        for col, col_type in new_cols.items():
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE processed_renders ADD COLUMN {col} {col_type}")

        conn.commit()


def is_already_processed(ai_filename: str, db_path: Optional[Path] = None) -> bool:
    """Returns True if ai_filename has already been completed."""
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
    status: str = "completed",
    db_path: Optional[Path] = None,
):
    """Inserts or updates a completed render record."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO processed_renders
                (ai_filename, final_filename, ai_drive_id, final_drive_id,
                 sheet_row, answer, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ai_filename) DO UPDATE SET
                final_filename  = excluded.final_filename,
                ai_drive_id     = excluded.ai_drive_id,
                final_drive_id  = excluded.final_drive_id,
                sheet_row       = excluded.sheet_row,
                answer          = excluded.answer,
                status          = excluded.status,
                processed_at    = CURRENT_TIMESTAMP
        ''', (ai_filename, final_filename, ai_drive_id, final_drive_id,
               sheet_row, answer, status))
        conn.commit()


def get_processed_count(db_path: Optional[Path] = None) -> int:
    """Returns the total number of successfully completed riddle videos."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM processed_renders WHERE status = 'completed'")
        row = cursor.fetchone()
        return row[0] if row else 0
