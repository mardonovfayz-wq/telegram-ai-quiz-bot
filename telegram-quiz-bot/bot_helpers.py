"""Pure helpers for the Telegram bot (no Telegram or OpenAI imports).

Keeping parsing and formatting here makes them testable offline.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from legal_refs import normalize

POLL_EXPLANATION_MAX_CHARS = 200
LETTERS = ("A", "B", "C", "D")

_CHANNEL_ID_RE = re.compile(r"^(?:@[A-Za-z][A-Za-z0-9_]{3,31}|-100\d{5,})$")


def parse_admin_id(raw: str) -> int:
    value = raw.strip()
    if not re.fullmatch(r"\d{1,15}", value):
        raise RuntimeError(
            "ADMIN_ID must be your numeric Telegram user ID (digits only)."
        )
    return int(value)


def parse_channel_id(raw: str) -> str:
    value = raw.strip()
    if not _CHANNEL_ID_RE.fullmatch(value):
        raise RuntimeError(
            "CHANNEL_ID must be a channel username such as @mychannel "
            "or a numeric channel ID beginning with -100."
        )
    return value


# ---------------------------------------------------------------------------
# Question editing.
# ---------------------------------------------------------------------------


def parse_question_edit(text: str, old_question: dict) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fields: dict[str, str] = {}
    options: dict[str, str] = {}
    for line in lines:
        field_match = re.match(r"^([^:]+):\s*(.*)$", line)
        if field_match:
            key = field_match.group(1).strip().lower().replace("‘", "'").replace("’", "'")
            value = field_match.group(2).strip()
            if key in {"savol", "question"}:
                fields["question"] = value
            elif key in {"to'g'ri javob", "tog'ri javob", "correct answer"}:
                fields["correct"] = value.upper()
            elif key == "manba":
                fields["source"] = value
            elif key in {"iqtibos", "quote"}:
                fields["quote"] = value
        option_match = re.match(r"^([A-D])[\).]\s*(.+)$", line, re.IGNORECASE)
        if option_match:
            options[option_match.group(1).upper()] = option_match.group(2).strip()

    question = fields.get("question", "").strip()
    choices = [options.get(letter, "") for letter in LETTERS]
    correct_letter = fields.get("correct", "").strip().upper()
    if not question or len(question) > 300:
        raise ValueError("Savol matnini kiriting (300 belgigacha).")
    if any(not choice or len(choice) > 100 for choice in choices):
        raise ValueError("A), B), C), D) javoblarining barchasini kiriting (100 belgigacha).")
    if len({choice.casefold() for choice in choices}) != 4:
        raise ValueError("To‘rtta javob varianti bir-biridan farq qilishi kerak.")
    if correct_letter not in LETTERS:
        raise ValueError("To‘g‘ri javob A, B, C yoki D bo‘lishi kerak.")

    source_title = old_question["source_title"]
    source_reference = old_question["source_reference"]
    if fields.get("source"):
        if "|" not in fields["source"]:
            raise ValueError("Manbani 'fayl nomi | aniq havola' shaklida kiriting.")
        source_title, source_reference = [
            part.strip() for part in fields["source"].split("|", 1)
        ]
    source_quote = fields.get("quote", old_question["source_quote"]).strip()
    if not source_title:
        raise ValueError("Manba faylining nomini kiriting.")
    return {
        "question": question,
        "options": choices,
        "correct_index": LETTERS.index(correct_letter),
        "source_title": source_title,
        "source_reference": source_reference,
        "source_quote": source_quote,
    }


def differs_from_original_pair(question: dict) -> bool:
    """True when an edited Q&A draft no longer matches its preserved original."""
    original_question = question.get("original_question") or ""
    original_answer = question.get("original_answer") or ""
    if not original_question:
        return False
    options = question.get("options") or []
    index = question.get("correct_index")
    current_answer = options[index] if isinstance(index, int) and 0 <= index < len(options) else ""
    return normalize(question.get("question", "")) != normalize(original_question) or normalize(
        current_answer
    ) != normalize(original_answer)


GENERATE_USAGE = "Foydalanish: /generate [son] yoki /generate full [son]"


def parse_generate_args(args: list[str]) -> tuple[str, int]:
    """('incremental' | 'full', count) from /generate arguments."""
    mode = "incremental"
    values = list(args)
    if values and values[0].lower() == "full":
        mode = "full"
        values = values[1:]
    if len(values) > 1:
        raise ValueError(GENERATE_USAGE)
    try:
        count = int(values[0]) if values else 10
    except ValueError as error:
        raise ValueError("Savollar sonini raqam bilan kiriting.") from error
    if not 1 <= count <= 20:
        raise ValueError("Bir so‘rovda 1 dan 20 tagacha savol so‘rash mumkin.")
    return mode, count


# ---------------------------------------------------------------------------
# Publishing schedule.
# ---------------------------------------------------------------------------


def parse_schedule(text: str) -> list[str]:
    parts = text.strip().split()
    if len(parts) != 2:
        raise ValueError("Shakl: 2 09:00,18:00")
    try:
        daily_count = int(parts[0])
    except ValueError as error:
        raise ValueError("Kunlik savollar sonini raqam bilan kiriting.") from error
    times = [item.strip() for item in parts[1].split(",") if item.strip()]
    if not 1 <= daily_count <= 10:
        raise ValueError("Kuniga 1 dan 10 tagacha savol rejalashtirish mumkin.")
    if len(times) != daily_count:
        raise ValueError(
            "Nashr vaqtlari soni kunlik savollar soniga teng bo‘lishi kerak."
        )
    if len(set(times)) != len(times):
        raise ValueError("Nashr vaqtlari takrorlanmasligi kerak.")
    for item in times:
        if not re.fullmatch(r"\d{2}:\d{2}", item):
            raise ValueError("Vaqtni 24 soatlik HH:MM shaklida kiriting.")
        hour, minute = map(int, item.split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"Noto‘g‘ri vaqt: {item}")
    return sorted(times)


# ---------------------------------------------------------------------------
# Legal-act metadata (always entered by the administrator).
# ---------------------------------------------------------------------------

_METADATA_KEYS = {
    "official_title": {"nomi", "номи", "название", "title", "rasmiy nomi"},
    "document_number": {"raqami", "рақами", "номер", "number", "raqam"},
    "adoption_date": {
        "sana",
        "сана",
        "дата",
        "date",
        "qabul qilingan sana",
        "қабул қилинган сана",
        "дата принятия",
    },
    "source_url": {"havola", "ҳавола", "ссылка", "url", "link"},
}
_CLEAR_VALUES = {"-", "—"}


def parse_source_metadata(text: str) -> dict[str, str]:
    """Parse 'Nomi: ... / Raqami: ... / Sana: ... / Havola: ...' lines.

    A value of "-" clears that field. Dates are stored as YYYY-MM-DD; links
    must be https URLs. Nothing is looked up or filled in automatically.
    """
    metadata: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:]+):\s*(.*)$", line)
        if not match:
            raise ValueError(f"Qatorni tushunib bo‘lmadi: {line[:60]}")
        key = " ".join(match.group(1).strip().lower().replace("’", "'").replace("‘", "'").split())
        value = match.group(2).strip()
        field = next(
            (name for name, aliases in _METADATA_KEYS.items() if key in aliases), None
        )
        if field is None:
            raise ValueError(
                f"Noma’lum maydon: {match.group(1).strip()}. "
                "Nomi, Raqami, Sana yoki Havola ishlating."
            )
        if value in _CLEAR_VALUES:
            metadata[field] = ""
            continue
        if not value:
            raise ValueError(f"{match.group(1).strip()} qiymati bo‘sh.")
        if field == "official_title" and len(value) > 300:
            raise ValueError("Rasmiy nom 300 belgidan oshmasin.")
        if field == "document_number" and len(value) > 100:
            raise ValueError("Hujjat raqami 100 belgidan oshmasin.")
        if field == "adoption_date":
            value = _parse_date(value)
        if field == "source_url":
            value = _validate_url(value)
        metadata[field] = value
    if not metadata:
        raise ValueError("Kamida bitta maydonni kiriting.")
    return metadata


def _parse_date(value: str) -> str:
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Sanani KK.OO.YYYY yoki YYYY-OO-KK shaklida kiriting.")


def _validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or " " in value or len(value) > 500:
        raise ValueError("Havola https:// bilan boshlanuvchi to‘liq manzil bo‘lsin.")
    return value


def source_display_name(source: dict[str, Any] | None, fallback_title: str) -> str:
    """Official act name with number and date, or the uploaded file name."""
    if not source or not source.get("official_title"):
        return fallback_title
    details = [
        f"№ {source['document_number']}" if source.get("document_number") else "",
        _display_date(source.get("adoption_date", "")),
    ]
    details = [item for item in details if item]
    if details:
        return f"{source['official_title']} ({', '.join(details)})"
    return source["official_title"]


def _display_date(iso_date: str) -> str:
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


def build_poll_explanation(question: dict, source: dict[str, Any] | None) -> str:
    """Poll explanation (Telegram limit 200 chars) built only from stored data."""
    reference = question.get("source_reference", "")
    candidates = [
        f"Manba: {source_display_name(source, question['source_title'])}, {reference}",
        f"Manba: {source.get('official_title')}, {reference}"
        if source and source.get("official_title")
        else "",
        f"Manba: {question['source_title']}, {reference}",
        f"Manba: {reference}",
    ]
    basis = question.get("legal_basis", "")
    for candidate in candidates:
        if not candidate or len(candidate) > POLL_EXPLANATION_MAX_CHARS:
            continue
        with_basis = f"{candidate}. Asos: {basis}" if basis else candidate
        if len(with_basis) <= POLL_EXPLANATION_MAX_CHARS:
            return with_basis
        return candidate
    return f"Manba: {reference}"[:POLL_EXPLANATION_MAX_CHARS]


def format_metadata(source: dict[str, Any]) -> str:
    return (
        f"Rasmiy nomi: {source.get('official_title') or '—'}\n"
        f"Raqami: {source.get('document_number') or '—'}\n"
        f"Qabul qilingan sana: {_display_date(source.get('adoption_date', '')) or '—'}\n"
        f"Havola: {source.get('source_url') or '—'}"
    )


# ---------------------------------------------------------------------------
# Review card.
# ---------------------------------------------------------------------------


def format_question(question: dict, source: dict[str, Any] | None = None) -> str:
    options = "\n".join(
        f"{LETTERS[index]}) {option}" for index, option in enumerate(question["options"])
    )
    correct = LETTERS[question["correct_index"]]
    verification_note = ""
    review_status = question.get("review_status", "NEEDS_REVIEW")
    if review_status == "NEEDS_REVIEW":
        verification_note = (
            "\n\nNEEDS_REVIEW: administrator tekshiruvi shart.\n"
            f"{question.get('source_review_note') or 'Manbani tekshiring.'}"
        )
    source_excerpt = question.get("source_excerpt", "")
    excerpt_preview = source_excerpt[:500]
    if len(source_excerpt) > 500:
        excerpt_preview += "… [to‘liq parcha SQLite’da saqlangan]"
    source_quote = question.get("source_quote", "")
    quote_preview = source_quote[:500]
    if len(source_quote) > 500:
        quote_preview += "… [to‘liq iqtibos SQLite’da saqlangan]"
    excerpt_block = ""
    if source_excerpt and source_excerpt.strip() != source_quote.strip():
        excerpt_block = f"\nManba parchasi: {excerpt_preview}"
    original_pair = ""
    if question.get("original_question"):
        original_pair = (
            "\nAsl savol-javob saqlangan:\n"
            f"Savol: {question['original_question']}\n"
            f"Javob: {question['original_answer']}\n"
        )
        if differs_from_original_pair(question):
            original_pair += (
                "Diqqat: tahrirlangan savol yoki javob asl matndan farq qiladi; "
                "ma’nosi o‘zgarmaganini tekshiring.\n"
            )
    legal_basis = ""
    if question.get("legal_basis"):
        basis_text = question["legal_basis"]
        if len(basis_text) > 300:
            basis_text = basis_text[:300] + "…"
        legal_basis = f"\nManbadagi huquqiy asos (aynan): {basis_text}"
    file_line = ""
    link_line = ""
    if source and source.get("official_title"):
        file_line = f"\nFayl: {question['source_title']}"
    if source and source.get("source_url"):
        link_line = f"\nHavola: {source['source_url']}"
    return (
        f"#{question['id']} · Holat: {question['status']} · "
        f"{review_status}\n"
        f"Hujjat turi: {question.get('document_type', 'unknown')}\n\n"
        f"{question['question']}\n\n{options}\n\n"
        f"To‘g‘ri javob: {correct}) {question['options'][question['correct_index']]}\n"
        f"Manba: {source_display_name(source, question['source_title'])}, "
        f"{question['source_reference']}{file_line}{link_line}{legal_basis}\n"
        f"Asos: {quote_preview}{excerpt_block}{original_pair}{verification_note}"
    )
