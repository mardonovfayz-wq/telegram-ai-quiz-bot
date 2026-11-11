"""Generation workflows that combine the section ledger with AI generation.

No Telegram imports, so everything here is testable offline.
"""

from __future__ import annotations

import random
from typing import Any, Awaitable, Callable

import storage
from quiz_generator import (
    SECTION_CHARS,
    build_source_sections,
    check_citation,
    generate_incremental,
)


def eligible_sections(
    section_chars: int = SECTION_CHARS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sources (oldest first) and their not-yet-used sections in document order."""
    sources = sorted(storage.list_sources(), key=lambda item: item["id"])
    selected: list[dict[str, Any]] = []
    for source in sources:
        _document_type, sections = build_source_sections(
            source["title"],
            source["content"],
            source_id=source["id"],
            max_chars=section_chars,
        )
        rows = storage.sync_source_sections(source["id"], sections)
        by_key = {
            (row["start_offset"], row["end_offset"], row["section_sha256"]): row
            for row in rows
        }
        for section in sections:
            row = by_key[
                (
                    section["start"],
                    section["end"],
                    storage.section_hash(section["source_excerpt"]),
                )
            ]
            if storage.section_is_eligible(row):
                section["ledger_id"] = row["id"]
                section["ledger_status"] = row["status"]
                selected.append(section)
    return sources, selected


async def run_incremental_generation(
    client: Any,
    count: int,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    rng: random.Random | None = None,
    verify_answers: bool = True,
    section_chars: int = SECTION_CHARS,
) -> dict[str, Any]:
    """Create up to ``count`` drafts from the next unused sections and save them.

    Drafts are always saved as pending; nothing is approved or published here.
    """
    sources, sections = eligible_sections(section_chars)
    if not sources:
        raise ValueError("Avval /upload orqali kamida bitta manba yuboring.")
    if not sections:
        return {
            "nothing_left": True,
            "questions": [],
            "question_ids": [],
            "run_id": None,
        }
    result = await generate_incremental(
        client,
        sources,
        sections,
        count,
        existing_question_texts=storage.question_texts_for_dedupe(),
        progress_callback=progress_callback,
        rng=rng,
        verify_answers=verify_answers,
    )
    run_id, question_ids = storage.save_incremental_run(
        source_count=len(sources),
        questions=result["questions"],
        duplicates_removed=result["duplicates_removed"],
        skipped_items=result["unsuitable_items"],
        section_outcomes=result["section_outcomes"],
    )
    finished = sum(
        item["status"] != "failed" for item in result["section_outcomes"]
    )
    result.update(
        {
            "nothing_left": False,
            "run_id": run_id,
            "question_ids": question_ids,
            "sections_remaining": len(sections) - finished,
        }
    )
    return result


def recheck_questions(
    statuses: tuple[str, ...] = ("pending", "approved"),
) -> dict[str, Any]:
    """Re-check stored citations with the current rules. Read-only.

    Nothing is changed in the database; the result is only a report.
    """
    questions = storage.list_questions_for_recheck(statuses)
    source_cache: dict[int, dict[str, Any] | None] = {}
    problems: list[dict[str, Any]] = []
    for question in questions:
        sources = []
        for source_id in question.get("source_ids") or []:
            if source_id not in source_cache:
                source_cache[source_id] = storage.get_source(source_id)
            if source_cache[source_id]:
                sources.append(source_cache[source_id])
        if not sources:
            problems.append(
                {
                    "id": question["id"],
                    "status": question["status"],
                    "reference": question["source_reference"],
                    "note": "Manba bazada topilmadi.",
                }
            )
            continue
        has_scope = (
            question.get("section_start") is not None
            and question.get("section_end") is not None
        )
        result = check_citation(
            question["source_title"],
            question["source_reference"],
            question["source_quote"],
            sources,
            section_text=None if has_scope else (question.get("source_excerpt") or None),
            scope=(question["section_start"], question["section_end"]) if has_scope else None,
        )
        if not result.verified:
            problems.append(
                {
                    "id": question["id"],
                    "status": question["status"],
                    "reference": question["source_reference"],
                    "note": result.note,
                }
            )
    return {
        "checked": len(questions),
        "ok": len(questions) - len(problems),
        "problems": problems,
    }
