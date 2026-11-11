"""Small SQLite helpers for the Telegram quiz bot."""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# QUIZ_BOT_DB_PATH lets a future host keep the database on a persistent disk.
# The default location is unchanged: next to the bot code.
DB_PATH = Path(
    os.getenv("QUIZ_BOT_DB_PATH", "").strip()
    or Path(__file__).with_name("quiz_bot.sqlite3")
)
logger = logging.getLogger(__name__)

SOURCE_METADATA_FIELDS = (
    "official_title",
    "document_number",
    "adoption_date",
    "source_url",
)


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


_QUESTION_MIGRATIONS = {
    "source_verified": "INTEGER NOT NULL DEFAULT 1",
    "source_review_note": "TEXT NOT NULL DEFAULT ''",
    "source_excerpt": "TEXT NOT NULL DEFAULT ''",
    "document_type": "TEXT NOT NULL DEFAULT 'unknown'",
    "original_question": "TEXT NOT NULL DEFAULT ''",
    "original_answer": "TEXT NOT NULL DEFAULT ''",
    "review_status": "TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'",
    "generation_run_id": "INTEGER",
    "legal_basis": "TEXT NOT NULL DEFAULT ''",
    "section_start": "INTEGER",
    "section_end": "INTEGER",
    "quote_start": "INTEGER",
    "quote_end": "INTEGER",
    "legal_article": "TEXT NOT NULL DEFAULT ''",
    "legal_part": "TEXT NOT NULL DEFAULT ''",
    "legal_clause": "TEXT NOT NULL DEFAULT ''",
}
_SOURCE_MIGRATIONS = {
    field: "TEXT NOT NULL DEFAULT ''" for field in SOURCE_METADATA_FIELDS
}
_RUN_MIGRATIONS = {"needs_review_count": "INTEGER NOT NULL DEFAULT 0"}
_EXPECTED_TABLES = {"sources", "generation_runs", "questions", "skipped_items", "settings"}


def _pending_migrations(connection: sqlite3.Connection, tables: set[str]) -> list[str]:
    statements: list[str] = []
    for table, columns in (
        ("questions", _QUESTION_MIGRATIONS),
        ("sources", _SOURCE_MIGRATIONS),
        ("generation_runs", _RUN_MIGRATIONS),
    ):
        if table not in tables:
            continue  # created below with every column
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing:
                statements.append(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
    return statements


def _backup_database(connection: sqlite3.Connection) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = DB_PATH.with_name(f"{DB_PATH.stem}.backup-{stamp}.sqlite3")
    destination = sqlite3.connect(backup_path)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    return backup_path


def initialize_database() -> None:
    """Create missing tables and add missing columns (additive only).

    Before changing the schema of an existing database, a full copy is saved
    next to it as ``<name>.backup-<UTC timestamp>.sqlite3``. Existing rows are
    never modified or deleted.
    """
    existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    with _database() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        pending = _pending_migrations(connection, tables)
        if existed and tables and (pending or not _EXPECTED_TABLES <= tables):
            backup_path = _backup_database(connection)
            logger.info("Database backed up before schema update: %s", backup_path.name)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                official_title TEXT NOT NULL DEFAULT '',
                document_number TEXT NOT NULL DEFAULT '',
                adoption_date TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT ''
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
                published_message_id INTEGER,
                legal_basis TEXT NOT NULL DEFAULT '',
                section_start INTEGER,
                section_end INTEGER,
                quote_start INTEGER,
                quote_end INTEGER,
                legal_article TEXT NOT NULL DEFAULT '',
                legal_part TEXT NOT NULL DEFAULT '',
                legal_clause TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS skipped_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_run_id INTEGER REFERENCES generation_runs(id),
                source_id INTEGER,
                source_title TEXT NOT NULL,
                source_reference TEXT NOT NULL DEFAULT '',
                section_start INTEGER,
                section_end INTEGER,
                original_question TEXT NOT NULL DEFAULT '',
                original_answer TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        for statement in pending:
            connection.execute(statement)


def add_source(title: str, content: str) -> int:
    with _database() as connection:
        cursor = connection.execute(
            "INSERT INTO sources (title, content, created_at) VALUES (?, ?, ?)",
            (title, content, _now()),
        )
        return int(cursor.lastrowid)


_SOURCE_COLUMNS = (
    "id, title, content, created_at, official_title, document_number, "
    "adoption_date, source_url"
)


def list_sources() -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_source(source_id: int) -> dict[str, Any] | None:
    with _database() as connection:
        row = connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    return dict(row) if row else None


def update_source_metadata(source_id: int, **metadata: str) -> bool:
    """Set legal-act metadata entered by the administrator (never by the AI).

    Only the metadata columns change; the stored document text is untouched.
    """
    unknown = set(metadata) - set(SOURCE_METADATA_FIELDS)
    if unknown:
        raise ValueError(f"Unknown metadata fields: {sorted(unknown)}")
    if not metadata:
        return False
    assignments = ", ".join(f"{field} = ?" for field in metadata)
    with _database() as connection:
        cursor = connection.execute(
            f"UPDATE sources SET {assignments} WHERE id = ?",
            (*metadata.values(), source_id),
        )
        return cursor.rowcount == 1


def get_sources_by_ids(source_ids: list[int]) -> list[dict[str, Any]]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    with _database() as connection:
        rows = connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources "
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
    skipped_items: list[dict[str, Any]] | None = None,
) -> tuple[int, list[int]]:
    """Atomically save a complete run, its drafts and its skipped Q&A pairs."""
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
                    legal_basis, section_start, section_end, quote_start,
                    quote_end, legal_article, legal_part, legal_clause,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
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
                    question.get("legal_basis", ""),
                    question.get("section_start"),
                    question.get("section_end"),
                    question.get("quote_start"),
                    question.get("quote_end"),
                    question.get("legal_article", ""),
                    question.get("legal_part", ""),
                    question.get("legal_clause", ""),
                    now,
                    now,
                ),
            )
            question_ids.append(int(cursor.lastrowid))
        for item in skipped_items or []:
            connection.execute(
                """
                INSERT INTO skipped_items (
                    generation_run_id, source_id, source_title, source_reference,
                    section_start, section_end, original_question,
                    original_answer, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item.get("source_id"),
                    item.get("source_title", ""),
                    item.get("source_reference", ""),
                    item.get("section_start"),
                    item.get("section_end"),
                    item.get("original_question", ""),
                    item.get("original_answer", ""),
                    item["reason"],
                    now,
                ),
            )
    return run_id, question_ids


def list_skipped_items(generation_run_id: int) -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT * FROM skipped_items WHERE generation_run_id = ? ORDER BY id",
            (generation_run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


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
    quote_start: int | None = None,
    quote_end: int | None = None,
    legal_article: str = "",
    legal_part: str = "",
    legal_clause: str = "",
) -> bool:
    """Update a pending draft. Original Q&A text and source links stay unchanged."""
    with _database() as connection:
        cursor = connection.execute(
            """
            UPDATE questions
            SET question = ?, options_json = ?, correct_index = ?,
                source_title = ?, source_reference = ?, source_quote = ?,
                source_verified = ?, source_review_note = ?,
                review_status = ?, quote_start = ?, quote_end = ?,
                legal_article = ?, legal_part = ?, legal_clause = ?,
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
                quote_start,
                quote_end,
                legal_article,
                legal_part,
                legal_clause,
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
