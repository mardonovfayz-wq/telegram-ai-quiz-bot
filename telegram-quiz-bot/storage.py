"""Small SQLite helpers for the Telegram quiz bot."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DB_PATH = Path(__file__).with_name("quiz_bot.sqlite3")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _database():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_database() -> None:
    """Create the database tables if they do not exist yet."""
    with _database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_index INTEGER NOT NULL,
                source_title TEXT NOT NULL,
                source_reference TEXT NOT NULL,
                source_quote TEXT NOT NULL,
                source_ids_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def add_source(title: str, content: str) -> int:
    with _database() as connection:
        cursor = connection.execute(
            "INSERT INTO sources (title, content, created_at) VALUES (?, ?, ?)",
            (title, content, _now()),
        )
        return int(cursor.lastrowid)


def list_sources() -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT id, title, content, created_at FROM sources ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_sources_by_ids(source_ids: list[int]) -> list[dict[str, Any]]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    with _database() as connection:
        rows = connection.execute(
            f"SELECT id, title, content, created_at FROM sources "
            f"WHERE id IN ({placeholders})",
            source_ids,
        ).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    return [by_id[source_id] for source_id in source_ids if source_id in by_id]


def add_question(
    question: str,
    options: list[str],
    correct_index: int,
    source_title: str,
    source_reference: str,
    source_quote: str,
    source_ids: list[int],
) -> int:
    now = _now()
    with _database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO questions (
                question, options_json, correct_index, source_title,
                source_reference, source_quote, source_ids_json, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                question,
                json.dumps(options, ensure_ascii=False),
                correct_index,
                source_title,
                source_reference,
                source_quote,
                json.dumps(source_ids),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def get_question(question_id: int) -> dict[str, Any] | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
    return _question_from_row(row) if row else None


def list_questions(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with _database() as connection:
        if status:
            rows = connection.execute(
                "SELECT * FROM questions WHERE status = ? "
                "ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM questions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_question_from_row(row) for row in rows]


def _question_from_row(row: sqlite3.Row) -> dict[str, Any]:
    question = dict(row)
    question["options"] = json.loads(question.pop("options_json"))
    question["source_ids"] = json.loads(question.pop("source_ids_json"))
    return question


def set_question_status(question_id: int, status: str) -> bool:
    if status not in {"approved", "rejected"}:
        raise ValueError("Unsupported question status.")
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, _now(), question_id),
        )
        return cursor.rowcount == 1


def update_question(
    question_id: int,
    question: str,
    options: list[str],
    correct_index: int,
    source_title: str,
    source_reference: str,
    source_quote: str,
) -> bool:
    with _database() as connection:
        cursor = connection.execute(
            """
            UPDATE questions
            SET question = ?, options_json = ?, correct_index = ?,
                source_title = ?, source_reference = ?, source_quote = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                question,
                json.dumps(options, ensure_ascii=False),
                correct_index,
                source_title,
                source_reference,
                source_quote,
                _now(),
                question_id,
            ),
        )
        return cursor.rowcount == 1


def next_approved_question() -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM questions WHERE status = 'approved' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return _question_from_row(row) if row else None


def mark_question_published(question_id: int, message_id: int) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = 'published', published_message_id = ?, "
            "updated_at = ? WHERE id = ? AND status = 'approved'",
            (message_id, _now(), question_id),
        )
        return cursor.rowcount == 1


def save_schedule(daily_times: list[str]) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO settings (key, value) VALUES ('daily_times', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(daily_times),),
        )


def get_schedule() -> list[str]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'daily_times'"
        ).fetchone()
    return json.loads(row["value"]) if row else []
