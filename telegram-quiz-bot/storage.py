"""Small SQLite helpers for the Telegram quiz bot."""

import hashlib
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

# A failed section is retried on later runs at most this many times in total.
MAX_SECTION_ATTEMPTS = 3
SECTION_STATUSES = ("unused", "used", "empty", "unsuitable", "failed")

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
    "approved_at": "TEXT",
    "approved_by": "TEXT NOT NULL DEFAULT ''",
    "publish_claimed_at": "TEXT",
    "publish_attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_publish_error": "TEXT NOT NULL DEFAULT ''",
}
_SOURCE_MIGRATIONS = {
    field: "TEXT NOT NULL DEFAULT ''" for field in SOURCE_METADATA_FIELDS
}
_SOURCE_MIGRATIONS["content_sha256"] = "TEXT NOT NULL DEFAULT ''"
_RUN_MIGRATIONS = {
    "needs_review_count": "INTEGER NOT NULL DEFAULT 0",
    "mode": "TEXT NOT NULL DEFAULT 'full'",
    "failed_sections": "INTEGER NOT NULL DEFAULT 0",
}
_EXPECTED_TABLES = {
    "sources",
    "generation_runs",
    "questions",
    "skipped_items",
    "settings",
    "source_sections",
    "audit_log",
    "daily_runs",
}

# Question lifecycle. "publishing" means a poll may already be in the channel
# (the send was started but its result is unknown); it is never retried
# automatically, the administrator resolves it with /resolve.
# "held" means an approved question was stopped before publishing because an
# equivalent question is already published.
QUESTION_STATUSES = ("pending", "approved", "rejected", "publishing", "published", "held")
SYSTEM_ACTOR = "system"


def _audit(
    connection: sqlite3.Connection,
    actor: str,
    action: str,
    question_id: int | None = None,
    run_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO audit_log (created_at, actor, action, question_id, run_id, details_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            _now(),
            actor,
            action,
            question_id,
            run_id,
            json.dumps(details or {}, ensure_ascii=False),
        ),
    )


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
                source_url TEXT NOT NULL DEFAULT '',
                content_sha256 TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS source_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id),
                section_number INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                section_sha256 TEXT NOT NULL,
                section_kind TEXT NOT NULL DEFAULT 'prose',
                status TEXT NOT NULL DEFAULT 'unused',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_run_id INTEGER,
                updated_at TEXT NOT NULL,
                UNIQUE (source_id, start_offset, end_offset, section_sha256)
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
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'full',
                failed_sections INTEGER NOT NULL DEFAULT 0
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
                legal_clause TEXT NOT NULL DEFAULT '',
                approved_at TEXT,
                approved_by TEXT NOT NULL DEFAULT '',
                publish_claimed_at TEXT,
                publish_attempts INTEGER NOT NULL DEFAULT 0,
                last_publish_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                question_id INTEGER,
                run_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS daily_runs (
                run_date TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                generation_run_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}'
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


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def add_source(title: str, content: str) -> int:
    with _database() as connection:
        cursor = connection.execute(
            "INSERT INTO sources (title, content, created_at, content_sha256) "
            "VALUES (?, ?, ?, ?)",
            (title, content, _now(), content_hash(content)),
        )
        return int(cursor.lastrowid)


def find_source_by_content(content: str) -> dict[str, Any] | None:
    """An already uploaded source with exactly the same extracted text.

    Older rows have no stored hash; their hash is computed in memory and the
    rows themselves are not modified.
    """
    digest = content_hash(content)
    with _database() as connection:
        row = connection.execute(
            "SELECT id, title FROM sources WHERE content_sha256 = ? ORDER BY id LIMIT 1",
            (digest,),
        ).fetchone()
        if row:
            return dict(row)
        for legacy in connection.execute(
            "SELECT id, title, content FROM sources WHERE content_sha256 = '' ORDER BY id"
        ):
            if content_hash(legacy["content"]) == digest:
                return {"id": legacy["id"], "title": legacy["title"]}
    return None


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
    mode: str = "full",
    failed_sections: int = 0,
    section_outcomes: list[dict[str, Any]] | None = None,
    actor: str = SYSTEM_ACTOR,
) -> tuple[int, list[int]]:
    """Atomically save a run, its drafts, skipped Q&A pairs and ledger updates."""
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
                needs_review_count, created_at, mode, failed_sections
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_count,
                sections_total,
                sections_total - failed_sections,
                len(questions),
                duplicates_removed,
                unverified_count,
                needs_review_count,
                now,
                mode,
                failed_sections,
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
            _audit(
                connection,
                actor,
                "generated",
                question_id=question_ids[-1],
                run_id=run_id,
                details={"review_status": question.get("review_status", "NEEDS_REVIEW")},
            )
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
        for outcome in section_outcomes or []:
            if outcome["status"] not in SECTION_STATUSES:
                raise ValueError(f"Unknown section status: {outcome['status']}")
            connection.execute(
                """
                UPDATE source_sections
                SET status = ?, last_error = ?, last_run_id = ?, updated_at = ?,
                    attempts = attempts + ?
                WHERE id = ?
                """,
                (
                    outcome["status"],
                    (outcome.get("error") or "")[:500],
                    run_id,
                    now,
                    1 if outcome["status"] == "failed" else 0,
                    outcome["ledger_id"],
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


def set_question_status(
    question_id: int, status: str, actor: str = SYSTEM_ACTOR
) -> bool:
    """Approve or reject a pending draft; the change and its actor are audited.

    Approval still requires a verified source and READY_FOR_APPROVAL.
    """
    if status not in {"approved", "rejected"}:
        raise ValueError("Unsupported question status.")
    with _database() as connection:
        verification_clause = (
            " AND source_verified = 1 AND review_status = 'READY_FOR_APPROVAL'"
            if status == "approved"
            else ""
        )
        now = _now()
        approval_fields = ", approved_at = ?, approved_by = ?" if status == "approved" else ""
        parameters: tuple = (status, now)
        if status == "approved":
            parameters += (now, actor)
        cursor = connection.execute(
            "UPDATE questions SET status = ?, updated_at = ?" + approval_fields
            + " WHERE id = ? AND status = 'pending'" + verification_clause,
            parameters + (question_id,),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(connection, actor, status, question_id=question_id)
        return changed


def unapprove_question(question_id: int, actor: str, reason: str = "") -> bool:
    """Move an approved or held question back to pending review (audited)."""
    with _database() as connection:
        row = connection.execute(
            "SELECT status FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        cursor = connection.execute(
            "UPDATE questions SET status = 'pending', approved_at = NULL, "
            "approved_by = '', updated_at = ? "
            "WHERE id = ? AND status IN ('approved', 'held')",
            (_now(), question_id),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(
                connection,
                actor,
                "unapproved",
                question_id=question_id,
                details={"from": row["status"], "reason": reason},
            )
        return changed


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
    actor: str = SYSTEM_ACTOR,
) -> bool:
    """Update a pending draft. Original Q&A text and source links stay unchanged.

    The audit log keeps the text before and after the edit.
    """
    with _database() as connection:
        before = connection.execute(
            "SELECT question, options_json, correct_index, source_reference, "
            "source_quote, review_status FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
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
        changed = cursor.rowcount == 1
        if changed:
            _audit(
                connection,
                actor,
                "edited",
                question_id=question_id,
                details={
                    "before": {
                        "question": before["question"],
                        "options": json.loads(before["options_json"]),
                        "correct_index": before["correct_index"],
                        "source_reference": before["source_reference"],
                        "source_quote": before["source_quote"],
                        "review_status": before["review_status"],
                    },
                    "after": {
                        "question": question,
                        "options": options,
                        "correct_index": correct_index,
                        "source_reference": source_reference,
                        "source_quote": source_quote,
                        "review_status": review_status,
                    },
                },
            )
        return changed


def next_approved_question() -> dict[str, Any] | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM questions WHERE status = 'approved' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return _question_from_row(row) if row else None


def published_question_texts() -> list[tuple[int, str]]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT id, question FROM questions WHERE status IN ('published', 'publishing')"
        ).fetchall()
    return [(int(row["id"]), row["question"]) for row in rows]


def hold_question(question_id: int, reason: str, actor: str = SYSTEM_ACTOR) -> bool:
    """Stop an approved question from being published (audited)."""
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = 'held', last_publish_error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'approved'",
            (reason[:500], _now(), question_id),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(connection, actor, "held", question_id=question_id, details={"reason": reason})
        return changed


def claim_for_publishing(question_id: int, actor: str = SYSTEM_ACTOR) -> bool:
    """Atomically move one approved question to "publishing".

    Only one caller can win the claim, so the same question can never be sent
    twice, even if two publish jobs run at the same moment.
    """
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = 'publishing', publish_claimed_at = ?, "
            "publish_attempts = publish_attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = 'approved'",
            (_now(), _now(), question_id),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(connection, actor, "publish_started", question_id=question_id)
        return changed


def mark_question_published(
    question_id: int, message_id: int, actor: str = SYSTEM_ACTOR
) -> bool:
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = 'published', published_message_id = ?, "
            "last_publish_error = '', updated_at = ? "
            "WHERE id = ? AND status = 'publishing'",
            (message_id, _now(), question_id),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(
                connection,
                actor,
                "published",
                question_id=question_id,
                details={"message_id": message_id},
            )
        return changed


def release_failed_publish(
    question_id: int, error: str, actor: str = SYSTEM_ACTOR
) -> bool:
    """Telegram certainly did not post the poll: return it to the approved queue."""
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = 'approved', last_publish_error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'publishing'",
            (error[:500], _now(), question_id),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(
                connection, actor, "publish_failed", question_id=question_id,
                details={"error": error[:500]},
            )
        return changed


def note_uncertain_publish(question_id: int, error: str, actor: str = SYSTEM_ACTOR) -> None:
    """The send may or may not have reached Telegram; keep it in "publishing"."""
    with _database() as connection:
        connection.execute(
            "UPDATE questions SET last_publish_error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'publishing'",
            (error[:500], _now(), question_id),
        )
        _audit(
            connection, actor, "publish_uncertain", question_id=question_id,
            details={"error": error[:500]},
        )


def resolve_publishing(
    question_id: int,
    outcome: str,
    actor: str,
    message_id: int | None = None,
) -> bool:
    """Administrator decision for a question stuck in "publishing".

    ``outcome`` is "published" (it is in the channel) or "approved" (it is not;
    put it back in the queue).
    """
    if outcome not in {"published", "approved"}:
        raise ValueError("outcome must be 'published' or 'approved'")
    with _database() as connection:
        cursor = connection.execute(
            "UPDATE questions SET status = ?, published_message_id = COALESCE(?, published_message_id), "
            "updated_at = ? WHERE id = ? AND status = 'publishing'",
            (outcome, message_id, _now(), question_id),
        )
        changed = cursor.rowcount == 1
        if changed:
            _audit(
                connection, actor, f"resolved_{outcome}", question_id=question_id,
                details={"message_id": message_id},
            )
        return changed


def count_questions_by_status() -> dict[str, int]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS amount FROM questions GROUP BY status"
        ).fetchall()
    counts = {status: 0 for status in QUESTION_STATUSES}
    for row in rows:
        counts[row["status"]] = row["amount"]
    return counts


# ---------------------------------------------------------------------------
# Audit log, settings and daily runs.
# ---------------------------------------------------------------------------


def add_audit(
    actor: str,
    action: str,
    question_id: int | None = None,
    run_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with _database() as connection:
        _audit(connection, actor, action, question_id, run_id, details)


def list_audit(limit: int = 20, question_id: int | None = None) -> list[dict[str, Any]]:
    with _database() as connection:
        if question_id is None:
            rows = connection.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE question_id = ? ORDER BY id DESC LIMIT ?",
                (question_id, limit),
            ).fetchall()
    entries = []
    for row in rows:
        entry = dict(row)
        entry["details"] = json.loads(entry.pop("details_json") or "{}")
        entries.append(entry)
    return entries


def get_setting(key: str, default: str = "") -> str:
    with _database() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, actor: str = SYSTEM_ACTOR) -> None:
    with _database() as connection:
        connection.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        _audit(connection, actor, "setting_changed", details={"key": key, "value": value})


def start_daily_run(run_date: str, trigger: str) -> bool:
    """Reserve the daily generation for ``run_date``; False if already taken.

    This makes the 09:00 run happen at most once per day, even if the bot's
    own scheduler and Activepieces both fire, or the bot restarts.
    """
    with _database() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO daily_runs (run_date, trigger, status, started_at) "
            "VALUES (?, ?, 'running', ?)",
            (run_date, trigger, _now()),
        )
        taken = cursor.rowcount == 1
        if taken:
            _audit(
                connection, trigger, "daily_generation_started",
                details={"run_date": run_date},
            )
        return taken


def finish_daily_run(
    run_date: str,
    status: str,
    generation_run_id: int | None,
    details: dict[str, Any],
) -> None:
    with _database() as connection:
        connection.execute(
            "UPDATE daily_runs SET status = ?, finished_at = ?, generation_run_id = ?, "
            "details_json = ? WHERE run_date = ?",
            (status, _now(), generation_run_id, json.dumps(details, ensure_ascii=False), run_date),
        )
        _audit(
            connection, SYSTEM_ACTOR, f"daily_generation_{status}",
            run_id=generation_run_id, details={"run_date": run_date, **details},
        )


def get_daily_run(run_date: str) -> dict[str, Any] | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM daily_runs WHERE run_date = ?", (run_date,)
        ).fetchone()
    if not row:
        return None
    entry = dict(row)
    entry["details"] = json.loads(entry.pop("details_json") or "{}")
    return entry


# ---------------------------------------------------------------------------
# Section ledger: which parts of each source have already been used.
# ---------------------------------------------------------------------------


def section_hash(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()


def sync_source_sections(
    source_id: int, sections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Register the current sections of a source and return their ledger rows.

    Sections are identified by offsets and text hash, so re-splitting the same
    text finds the same rows. Existing ledger rows are never reset.
    """
    now = _now()
    keys = []
    with _database() as connection:
        for section in sections:
            digest = section_hash(section["source_excerpt"])
            key = (source_id, section["start"], section["end"], digest)
            keys.append(key)
            connection.execute(
                """
                INSERT OR IGNORE INTO source_sections (
                    source_id, section_number, start_offset, end_offset,
                    section_sha256, section_kind, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    section["section_number"],
                    section["start"],
                    section["end"],
                    digest,
                    section.get("section_kind", "prose"),
                    now,
                ),
            )
        rows = connection.execute(
            "SELECT * FROM source_sections WHERE source_id = ? ORDER BY start_offset, id",
            (source_id,),
        ).fetchall()
    wanted = set(keys)
    return [
        dict(row)
        for row in rows
        if (row["source_id"], row["start_offset"], row["end_offset"], row["section_sha256"])
        in wanted
    ]


def section_is_eligible(row: dict[str, Any]) -> bool:
    return row["status"] == "unused" or (
        row["status"] == "failed" and row["attempts"] < MAX_SECTION_ATTEMPTS
    )


def save_incremental_run(
    source_count: int,
    questions: list[dict[str, Any]],
    duplicates_removed: int,
    skipped_items: list[dict[str, Any]],
    section_outcomes: list[dict[str, Any]],
    actor: str = SYSTEM_ACTOR,
) -> tuple[int, list[int]]:
    """Atomically save drafts, skipped pairs and ledger updates for one run.

    ``section_outcomes`` items: {"ledger_id", "status", "error"}; a "failed"
    outcome increases the attempt counter so the section is retried later.
    """
    failed = sum(item["status"] == "failed" for item in section_outcomes)
    run_id, question_ids = save_generation_collection(
        source_count=source_count,
        sections_total=len(section_outcomes),
        questions=questions,
        duplicates_removed=duplicates_removed,
        skipped_items=skipped_items,
        mode="incremental",
        failed_sections=failed,
        section_outcomes=section_outcomes,
        actor=actor,
    )
    return run_id, question_ids


def coverage_summary() -> list[dict[str, Any]]:
    """Per-source counts of ledger sections by status."""
    with _database() as connection:
        rows = connection.execute(
            """
            SELECT s.id AS source_id, s.title, s.official_title,
                   ss.status, ss.attempts, COUNT(ss.id) AS amount
            FROM sources s
            LEFT JOIN source_sections ss ON ss.source_id = s.id
            GROUP BY s.id, ss.status, ss.attempts >= ?
            ORDER BY s.id
            """,
            (MAX_SECTION_ATTEMPTS,),
        ).fetchall()
    summary: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = summary.setdefault(
            row["source_id"],
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "official_title": row["official_title"],
                "total": 0,
                **{status: 0 for status in SECTION_STATUSES},
                "failed_final": 0,
            },
        )
        if row["status"] is None:
            continue
        entry["total"] += row["amount"]
        entry[row["status"]] += row["amount"]
        if row["status"] == "failed" and row["attempts"] >= MAX_SECTION_ATTEMPTS:
            entry["failed_final"] += row["amount"]
    return list(summary.values())


def list_failed_sections(limit: int = 20) -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute(
            """
            SELECT ss.*, s.title FROM source_sections ss
            JOIN sources s ON s.id = ss.source_id
            WHERE ss.status = 'failed' ORDER BY ss.updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_questions_for_recheck(statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    with _database() as connection:
        rows = connection.execute(
            f"SELECT * FROM questions WHERE status IN ({placeholders}) ORDER BY id",
            statuses,
        ).fetchall()
    return [_question_from_row(row) for row in rows]


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
