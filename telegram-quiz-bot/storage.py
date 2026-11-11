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

            CREATE TABLE IF NOT EXISTS generation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_count INTEGER NOT NULL,
                sections_total INTEGER NOT NULL,
                sections_processed INTEGER NOT NULL,
                question_count INTEGER NOT NULL,
                duplicates_removed INTEGER NOT NULL DEFAULT 0,
                unverified_count INTEGER NOT NULL DEFAULT 0,
                needs_review_count INTEGER NOT NULL DEFAULT 0,
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
                source_verified INTEGER NOT NULL DEFAULT 1,
                source_review_note TEXT NOT NULL DEFAULT '',
                source_excerpt TEXT NOT NULL DEFAULT '',
                document_type TEXT NOT NULL DEFAULT 'unknown',
                original_question TEXT NOT NULL DEFAULT '',
                original_answer TEXT NOT NULL DEFAULT '',
                review_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
                generation_run_id INTEGER REFERENCES generation_runs(id),
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
        question_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(questions)")
        }
        migrations = {
            "source_verified": (
                "ALTER TABLE questions ADD COLUMN source_verified "
                "INTEGER NOT NULL DEFAULT 1"
            ),
            "source_review_note": (
                "ALTER TABLE questions ADD COLUMN source_review_note "
                "TEXT NOT NULL DEFAULT ''"
            ),
            "source_excerpt": (
                "ALTER TABLE questions ADD COLUMN source_excerpt "
                "TEXT NOT NULL DEFAULT ''"
            ),
            "document_type": (
                "ALTER TABLE questions ADD COLUMN document_type "
                "TEXT NOT NULL DEFAULT 'unknown'"
            ),
            "original_question": (
                "ALTER TABLE questions ADD COLUMN original_question "
                "TEXT NOT NULL DEFAULT ''"
            ),
            "original_answer": (
                "ALTER TABLE questions ADD COLUMN original_answer "
                "TEXT NOT NULL DEFAULT ''"
            ),
            "review_status": (
                "ALTER TABLE questions ADD COLUMN review_status "
                "TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'"
            ),
            "generation_run_id": (
                "ALTER TABLE questions ADD COLUMN generation_run_id INTEGER"
            ),
        }
        for column, statement in migrations.items():
            if column not in question_columns:
                connection.execute(statement)
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(generation_runs)")
        }
        if "needs_review_count" not in run_columns:
            connection.execute(
                "ALTER TABLE generation_runs ADD COLUMN "
                "needs_review_count INTEGER NOT NULL DEFAULT 0"
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
    source_verified: bool = True,
    source_review_note: str = "",
    generation_run_id: int | None = None,
    source_excerpt: str = "",
    document_type: str = "unknown",
    original_question: str = "",
    original_answer: str = "",
    review_status: str = "NEEDS_REVIEW",
) -> int:
    now = _now()
    with _database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO questions (
                question, options_json, correct_index, source_title,
                source_reference, source_quote, source_ids_json, status,
                source_verified, source_review_note, generation_run_id,
                source_excerpt, document_type, original_question, original_answer,
                review_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                json.dumps(options, ensure_ascii=False),
                correct_index,
                source_title,
                source_reference,
                source_quote,
                json.dumps(source_ids),
                int(source_verified),
                source_review_note,
                generation_run_id,
                source_excerpt,
                document_type,
                original_question,
                original_answer,
                review_status,
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


def list_questions(
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with _database() as connection:
        if status:
            rows = connection.execute(
                "SELECT * FROM questions WHERE status = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM questions ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [_question_from_row(row) for row in rows]


def _question_from_row(row: sqlite3.Row) -> dict[str, Any]:
    question = dict(row)
    question["options"] = json.loads(question.pop("options_json"))
    question["source_ids"] = json.loads(question.pop("source_ids_json"))
    question["source_verified"] = bool(question["source_verified"])
    return question


def question_texts_for_dedupe(limit: int = 5000) -> list[str]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT question FROM questions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [row["question"] for row in rows]


def save_generation_collection(
    source_count: int,
    sections_total: int,
    questions: list[dict[str, Any]],
    duplicates_removed: int,
) -> tuple[int, list[int]]:
    """Atomically save a complete run and its draft questions."""
    now = _now()
    unverified_count = sum(not question["source_verified"] for question in questions)
    needs_review_count = sum(
        question.get("review_status", "NEEDS_REVIEW") == "NEEDS_REVIEW"
        for question in questions
    )
    with _database() as connection:
        run_cursor = connection.execute(
            """
            INSERT INTO generation_runs (
                source_count, sections_total, sections_processed,
                question_count, duplicates_removed, unverified_count,
                needs_review_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_count,
                sections_total,
                sections_total,
                len(questions),
                duplicates_removed,
                unverified_count,
                needs_review_count,
                now,
            ),
        )
        run_id = int(run_cursor.lastrowid)
        question_ids = []
        for question in questions:
            cursor = connection.execute(
                """
                INSERT INTO questions (
                    question, options_json, correct_index, source_title,
                    source_reference, source_quote, source_ids_json,
                    source_verified, source_review_note, generation_run_id,
                    source_excerpt, document_type, original_question,
                    original_answer, review_status,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    question["question"],
                    json.dumps(question["options"], ensure_ascii=False),
                    question["correct_index"],
                    question["source_title"],
                    question["source_reference"],
                    question["source_quote"],
                    json.dumps(question["source_ids"]),
                    int(question["source_verified"]),
                    question["source_review_note"],
                    run_id,
                    question.get("source_excerpt", ""),
                    question.get("document_type", "unknown"),
                    question.get("original_question", ""),
                    question.get("original_answer", ""),
                    question.get("review_status", "NEEDS_REVIEW"),
                    now,
                    now,
                ),
            )
            question_ids.append(int(cursor.lastrowid))
    return run_id, question_ids


def set_question_status(question_id: int, status: str) -> bool:
    if status not in {"approved", "rejected"}:
        raise ValueError("Unsupported question status.")
    with _database() as connection:
        verification_clause = (
            " AND source_verified = 1 AND review_status = 'READY_FOR_APPROVAL'"
            if status == "approved"
            else ""
        )
        cursor = connection.execute(
            "UPDATE questions SET status = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'" + verification_clause,
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
    source_verified: bool,
    source_review_note: str,
    review_status: str,
) -> bool:
    with _database() as connection:
        cursor = connection.execute(
            """
            UPDATE questions
            SET question = ?, options_json = ?, correct_index = ?,
                source_title = ?, source_reference = ?, source_quote = ?,
                source_verified = ?, source_review_note = ?,
                review_status = ?,
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
                int(source_verified),
                source_review_note,
                review_status,
                _now(),
                question_id,
            ),
        )
        return cursor.rowcount == 1


def next_approved_question() -> dict[str, Any] | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM questions WHERE status = 'approved' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return _question_from_row(row) if row else None


def mark_question_published(question_id: int, message_id: int) -> bool:
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = 'published', published_message_id = ?, "
            "updated_at = ? WHERE id = ? AND status = 'approved'",
            (message_id, _now(), question_id),
        )
        return cursor.rowcount == 1


def save_schedule(daily_times: list[str]) -> None:
    with _database() as connection:
        connection.execute(
            "INSERT INTO settings (key, value) VALUES ('daily_times', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(daily_times),),
        )


def get_schedule() -> list[str]:
    with _database() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'daily_times'"
        ).fetchone()
    return json.loads(row["value"]) if row else []
