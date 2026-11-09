"""Document extraction and source-grounded question generation."""

import json
import asyncio
import bisect
import re
import unicodedata
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, Awaitable, Callable

from docx import Document
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pypdf import PdfReader


MAX_FILE_BYTES = 20 * 1024 * 1024
SECTION_CHARS = 10_000
MAX_QUESTIONS_PER_REQUEST = 20
MAX_API_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 30

_LOCATION_RE = re.compile(r"(?im)^[ \t]*\[(?:Page|Paragraph)\s+\d+\][^\n]*")
_LEGAL_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"\d+\s*[-–—.]?\s*(?:modda|band|qism)\b[^\n]*|"
    r"(?:modda|article|section|статья|§)\s*(?:№\s*)?[\w./-]+[^\n]*|"
    r"(?:\d+|[IVXLCDM]+)\s*[-–—.]?\s*"
    r"(?:bob|bo['’‘]lim|chapter|глава|раздел)\b[^\n]*)"
)
_LEGAL_LOCATOR_RE = re.compile(
    r"(?i)(?:\d+\s*[-–—.]?\s*(?:modda|band|qism|bob|bo['’‘]lim)\b|"
    r"(?:modda|article|section|статья|band|qism|chapter|глава|раздел)"
    r"\s*(?:№\s*)?[\w./-]+|§\s*[\w./-]+)"
)


class SectionProcessingError(RuntimeError):
    """A visible, actionable failure while processing a specific section."""

    def __init__(
        self,
        section_number: int,
        sections_total: int,
        sections_processed: int,
        drafts_found: int,
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.section_number = section_number
        self.sections_total = sections_total
        self.sections_processed = sections_processed
        self.drafts_found = drafts_found
        self.reason = reason


def extract_document(filename: str, file_bytes: bytes) -> str:
    """Extract all available text and retain page/paragraph/line markers."""
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ValueError("Fayl 20 MB dan katta. Kichikroq fayl yuboring.")

    if extension == "pdf":
        try:
            reader = PdfReader(BytesIO(file_bytes))
            pages = []
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    pages.append(f"[Page {page_number}]\n{page_text}")
            content = "\n\n".join(pages)
        except Exception as error:
            raise ValueError("PDF faylni o‘qib bo‘lmadi.") from error
    elif extension == "docx":
        try:
            document = Document(BytesIO(file_bytes))
            paragraphs = [
                paragraph.text.strip()
                for paragraph in document.paragraphs
                if paragraph.text.strip()
            ]
            for table in document.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells)
                    if row_text.strip(" |"):
                        paragraphs.append(row_text)
            content = "\n\n".join(
                f"[Paragraph {number}]\n{text}"
                for number, text in enumerate(paragraphs, start=1)
            )
        except Exception as error:
            raise ValueError("DOCX faylni o‘qib bo‘lmadi.") from error
    elif extension == "txt":
        try:
            text = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode("cp1251")
            except UnicodeDecodeError as error:
                raise ValueError("TXT fayl kodlash formatini o‘qib bo‘lmadi.") from error
        content = "\n".join(
            f"[Line {number}] {line}"
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        )
    else:
        raise ValueError("Faqat PDF, DOCX yoki TXT fayl yuboring.")

    if not content.strip():
        raise ValueError(
            "Faylda o‘qiladigan matn topilmadi. PDF skanerlangan rasm bo‘lishi mumkin."
        )
    return content


def prepare_plain_text(text: str) -> str:
    """Keep all submitted text and add paragraph markers without a size cutoff."""
    if not text.strip():
        raise ValueError("Bo‘sh matnni manba sifatida saqlab bo‘lmaydi.")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    return "\n\n".join(
        f"[Paragraph {number}]\n{paragraph}"
        for number, paragraph in enumerate(paragraphs, start=1)
    )


def _split_ranges(text: str, max_chars: int) -> list[tuple[int, int]]:
    """Split at legal/page/paragraph boundaries, then soft-wrap oversized units."""
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100.")

    boundaries = {0, len(text)}
    for pattern in (_LOCATION_RE, _LEGAL_HEADING_RE):
        boundaries.update(match.start() for match in pattern.finditer(text))
    boundaries.update(
        match.end() for match in re.finditer(r"\n[ \t]*\n+", text)
    )
    ordered = sorted(boundaries)

    ranges: list[tuple[int, int]] = []
    pending_start: int | None = None
    pending_end: int | None = None

    def flush_pending() -> None:
        nonlocal pending_start, pending_end
        if pending_start is not None and pending_end is not None:
            ranges.append((pending_start, pending_end))
        pending_start = None
        pending_end = None

    for unit_start, unit_end in zip(ordered, ordered[1:]):
        if unit_start == unit_end:
            continue
        if unit_end - unit_start <= max_chars:
            if (
                pending_start is not None
                and pending_end is not None
                and unit_end - pending_start > max_chars
            ):
                flush_pending()
            if pending_start is None:
                pending_start = unit_start
            pending_end = unit_end
            continue

        flush_pending()
        position = unit_start
        while position < unit_end:
            hard_end = min(position + max_chars, unit_end)
            cut = hard_end
            if hard_end < unit_end:
                minimum_cut = position + int(max_chars * 0.55)
                newline_cut = text.rfind("\n", minimum_cut, hard_end)
                space_cut = text.rfind(" ", minimum_cut, hard_end)
                soft_cut = max(newline_cut, space_cut)
                if soft_cut >= minimum_cut:
                    cut = soft_cut + 1
            ranges.append((position, cut))
            position = cut

    flush_pending()
    return ranges


def chunk_document(
    title: str,
    content: str,
    source_id: int | None = None,
    max_chars: int = SECTION_CHARS,
) -> list[dict[str, Any]]:
    """Return complete, ordered processing sections with legal context retained."""
    if not content.strip():
        raise ValueError(f"{title}: manbada qayta ishlanadigan matn yo‘q.")
    ranges = _split_ranges(content, max_chars)

    location_markers = list(_LOCATION_RE.finditer(content))
    legal_headings = list(_LEGAL_HEADING_RE.finditer(content))
    location_positions = [item.start() for item in location_markers]
    heading_positions = [item.start() for item in legal_headings]

    def previous_text(matches: list[re.Match[str]], positions: list[int], start: int):
        index = bisect.bisect_right(positions, start) - 1
        return matches[index].group(0).strip() if index >= 0 else ""

    sections: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(ranges, start=1):
        excerpt = content[start:end]
        context_parts = [
            previous_text(location_markers, location_positions, start),
            previous_text(legal_headings, heading_positions, start),
        ]
        context = " | ".join(part for part in context_parts if part)
        prompt_text = (
            f"[Section context: {context}]\n{excerpt}" if context else excerpt
        )
        sections.append(
            {
                "source_id": source_id,
                "source_title": title,
                "section_number": index,
                "start": start,
                "end": end,
                "source_excerpt": excerpt,
                "content": prompt_text,
            }
        )

    if "".join(section["source_excerpt"] for section in sections) != content:
        raise RuntimeError("Section splitting failed its full-text coverage check.")
    return sections


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = normalized.replace("’", "'").replace("‘", "'").replace("`", "'")
    return " ".join(normalized.split())


def verify_grounding(
    source_title: str,
    source_reference: str,
    source_quote: str,
    available_sources: list[dict[str, Any]],
    section_text: str | None = None,
) -> tuple[bool, str]:
    """Check the exact source locator and evidence; report uncertainty for review."""
    source = next(
        (
            item
            for item in available_sources
            if _normalize(item["title"]) == _normalize(source_title)
        ),
        None,
    )
    if source is None:
        return False, "Manba fayli mos kelmadi; fayl nomini administrator tekshirsin."

    source_text = _normalize(source["content"])
    if not source_reference.strip() or _normalize(source_reference) not in source_text:
        return False, "Manba havolasi fayl matnida aniq topilmadi."
    if section_text is not None and _normalize(source_reference) not in _normalize(section_text):
        return False, "Manba havolasi aynan qayta ishlangan bo‘limda topilmadi."
    citation_scope = section_text if section_text is not None else source["content"]
    normalized_scope = _normalize(citation_scope)
    normalized_reference = _normalize(source_reference)
    legal_locators = [
        _normalize(match.group(0))
        for match in _LEGAL_LOCATOR_RE.finditer(normalized_scope)
    ]
    if legal_locators and not any(
        locator in normalized_reference for locator in legal_locators
    ):
        return (
            False,
            "Bo‘limda huquqiy modda yoki band bor, lekin u havolada ko‘rsatilmagan.",
        )
    quote_position = normalized_scope.find(_normalize(source_quote))
    locators_before_quote = [
        _normalize(match.group(0))
        for match in _LEGAL_LOCATOR_RE.finditer(normalized_scope)
        if match.start() <= quote_position
    ]
    if (
        locators_before_quote
        and locators_before_quote[-1] not in normalized_reference
    ):
        return (
            False,
            "Iqtibosning bevosita huquqiy modda yoki band havolasi ko‘rsatilmagan.",
        )
    if len(source_quote.strip()) < 12 or _normalize(source_quote) not in source_text:
        return False, "Asoslovchi iqtibos manbada aniq topilmadi."
    if section_text is not None and _normalize(source_quote) not in _normalize(section_text):
        return False, "Asoslovchi iqtibos aynan qayta ishlangan bo‘limda topilmadi."
    return True, ""


def validate_grounding(
    source_title: str,
    source_reference: str,
    source_quote: str,
    available_sources: list[dict[str, Any]],
) -> None:
    """Strict edit-time validator retained for callers that need an exception."""
    valid, note = verify_grounding(
        source_title, source_reference, source_quote, available_sources
    )
    if not valid:
        raise ValueError(note)


def _question_key(text: str) -> str:
    normalized = _normalize(text)
    return " ".join(re.findall(r"\w+", normalized, flags=re.UNICODE))


def is_near_duplicate(question: str, existing_questions: list[str]) -> bool:
    """Detect exact and near-identical question wording."""
    key = _question_key(question)
    if not key:
        return False
    new_tokens = set(key.split())
    for existing in existing_questions:
        old_key = _question_key(existing)
        if not old_key:
            continue
        if key == old_key:
            return True
        if SequenceMatcher(None, key, old_key).ratio() >= 0.90:
            return True
        old_tokens = set(old_key.split())
        union = new_tokens | old_tokens
        if union and len(new_tokens & old_tokens) / len(union) >= 0.86:
            length_ratio = min(len(key), len(old_key)) / max(len(key), len(old_key))
            if length_ratio >= 0.72:
                return True
    return False


async def _request_json(
    client: AsyncOpenAI, messages: list[dict[str, str]]
) -> dict[str, Any]:
    retryable = (
        RateLimitError,
        APITimeoutError,
        APIConnectionError,
        InternalServerError,
    )
    for attempt in range(MAX_API_ATTEMPTS):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
                timeout=45.0,
            )
            raw_content = response.choices[0].message.content
            if not raw_content:
                raise ValueError("AI bo‘sh javob qaytardi.")
            payload = json.loads(raw_content)
            if not isinstance(payload, dict):
                raise ValueError("AI javobi JSON obyekt emas.")
            return payload
        except retryable as error:
            if attempt + 1 >= MAX_API_ATTEMPTS:
                raise
            delay = min(2**attempt, MAX_RETRY_DELAY_SECONDS)
            response = getattr(error, "response", None)
            retry_after = getattr(response, "headers", {}).get("retry-after")
            if retry_after:
                try:
                    delay = min(
                        max(float(retry_after), 1),
                        MAX_RETRY_DELAY_SECONDS,
                    )
                except (TypeError, ValueError):
                    pass
            await asyncio.sleep(delay)
    raise RuntimeError("OpenAI request retries ended unexpectedly.")


def _validate_question(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("AI javobida savol formati noto‘g‘ri.")
    question = str(item.get("question", "")).strip()
    options = item.get("options")
    correct_index = item.get("correct_index")
    source_reference = str(item.get("source_reference", "")).strip()
    source_quote = str(item.get("source_quote", "")).strip()
    if (
        not question
        or len(question) > 300
        or not isinstance(options, list)
        or len(options) != 4
        or any(
            not isinstance(option, str) or not option.strip() or len(option.strip()) > 100
            for option in options
        )
        or len({_normalize(option) for option in options}) != 4
        or not isinstance(correct_index, int)
        or isinstance(correct_index, bool)
        or not 0 <= correct_index < 4
    ):
        raise ValueError("Savol yoki javob variantlari formatga mos emas.")
    if len(source_reference) > 1_000 or len(source_quote) > 800:
        raise ValueError("Manba havolasi yoki iqtibos juda uzun; bo‘limni tekshiring.")
    return {
        "question": question,
        "options": [option.strip() for option in options],
        "correct_index": correct_index,
        "source_reference": source_reference,
        "source_quote": source_quote,
    }


async def _generate_for_section(
    client: AsyncOpenAI,
    section: dict[str, Any],
    count: int,
) -> list[dict[str, Any]]:
    prompt = f"""
Quyidagi BIR bo‘limdan, faqat undagi aniq faktlarga asoslanib, 0 dan {count} tagacha
takrorlanmaydigan test savoli tuzing. Boshqa bo‘limlardan yoki tashqi bilimlardan
foydalanmang. Agar bo‘lim faqat sarlavha, takroriy matn yoki savolga yetarli
dalil bermasa, bo‘sh ro‘yxat qaytaring.

Har bir savol:
- Uzbek tilida, lotin yozuvida bo‘lsin.
- Aynan 4 ta turli variant va yagona correct_index (0–3) qaytarsin.
- source_reference sifatida bo‘lim ichida aynan ko‘rinadigan modda, band, qism,
  sarlavha yoki [Page N]/[Paragraph N]/[Line N] belgisini ko‘chirsin.
- Agar bo‘limda modda, band yoki qism ko‘rsatilgan bo‘lsa, uni sahifa
  belgisidan ustun qo‘ying va havolaga kiriting.
- Manbada aniq huquqiy havola topilmasa, source_reference sifatida bo‘sh
  qiymat qaytaring; raqam yoki qonun moddasini aslo uydirmang.
- source_quote savolni asoslaydigan, manbadan aynan ko‘chirilgan kamida
  12 va ko‘pi bilan 800 belgili matn bo‘lsin. Iqtibosni tarjima yoki qayta yozmang.

Faqat JSON:
{{"questions":[{{"question":"...","options":["...","...","...","..."],
"correct_index":0,"source_reference":"...","source_quote":"..."}}]}}

Fayl nomi: {section["source_title"]}
Bo‘lim: {section["section_number"]}
MATN:
{section["content"]}
"""
    payload = await _request_json(
        client,
        [
            {
                "role": "system",
                "content": (
                    "Huquqiy aniqlik birinchi o‘rinda. Manbada ko‘rinmagan "
                    "modda, band, raqam yoki faktni hech qachon uydirmang."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    items = payload.get("questions")
    if not isinstance(items, list) or len(items) > count:
        raise ValueError("AI bo‘lim uchun noto‘g‘ri savollar ro‘yxatini qaytardi.")
    return [_validate_question(item) for item in items]


async def generate_questions(
    client: AsyncOpenAI,
    sources: list[dict[str, Any]],
    count: int,
    existing_question_texts: list[str] | None = None,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    section_chars: int = SECTION_CHARS,
) -> dict[str, Any]:
    """Process every source section independently and return one deduplicated set."""
    if not sources:
        raise ValueError("Avval /upload orqali kamida bitta manba yuboring.")
    if not 1 <= count <= MAX_QUESTIONS_PER_REQUEST:
        raise ValueError("Bir so‘rovda 1 dan 20 tagacha savol so‘rash mumkin.")

    sections: list[dict[str, Any]] = []
    for source in sources:
        sections.extend(
            chunk_document(
                source["title"],
                source["content"],
                source_id=source["id"],
                max_chars=section_chars,
            )
        )
    if not sections:
        raise ValueError("Manbalarda qayta ishlanadigan bo‘lim topilmadi.")

    total_sections = len(sections)
    if total_sections > count:
        quotas = [1] * total_sections
    else:
        base, remainder = divmod(count, total_sections)
        quotas = [
            base + (1 if index < remainder else 0)
            for index in range(total_sections)
        ]

    seen_questions = list(existing_question_texts or [])
    new_questions: list[dict[str, Any]] = []
    duplicate_count = 0
    empty_section_count = 0
    processed_sections = 0

    for index, section in enumerate(sections, start=1):
        try:
            candidates = await _generate_for_section(
                client, section, quotas[index - 1]
            )
            source_record = {
                "id": section["source_id"],
                "title": section["source_title"],
                "content": next(
                    item["content"]
                    for item in sources
                    if item["id"] == section["source_id"]
                ),
            }
            if not candidates:
                empty_section_count += 1
            for candidate in candidates:
                verified, review_note = verify_grounding(
                    section["source_title"],
                    candidate["source_reference"],
                    candidate["source_quote"],
                    [source_record],
                    section_text=section["content"],
                )
                if len(candidate["source_reference"]) > 190:
                    verified = False
                    review_note = (
                        "Havola Telegram quiz izohiga sig‘maydi; "
                        "administrator aniqroq manba belgisini tekshirsin."
                    )
                candidate.update(
                    {
                        "source_id": section["source_id"],
                        "source_ids": [section["source_id"]],
                        "source_title": section["source_title"],
                        "source_verified": verified,
                        "source_review_note": review_note,
                    }
                )
                if is_near_duplicate(candidate["question"], seen_questions):
                    duplicate_count += 1
                    continue
                new_questions.append(candidate)
                seen_questions.append(candidate["question"])
            processed_sections += 1
        except Exception as error:
            if isinstance(error, SectionProcessingError):
                raise
            reason = (
                "OpenAI rate limitasi, vaqt tugashi yoki tarmoq xatosi."
                if isinstance(
                    error,
                    (
                        RateLimitError,
                        APITimeoutError,
                        APIConnectionError,
                        InternalServerError,
                    ),
                )
                else f"Bo‘lim javobi tekshiruvdan o‘tmadi ({type(error).__name__})."
            )
            raise SectionProcessingError(
                section_number=index,
                sections_total=total_sections,
                sections_processed=processed_sections,
                drafts_found=len(new_questions),
                reason=reason,
            ) from error

        if progress_callback:
            await progress_callback(
                processed_sections, total_sections, len(new_questions)
            )

    return {
        "questions": new_questions,
        "sections_total": total_sections,
        "sections_processed": processed_sections,
        "sections_without_questions": empty_section_count,
        "duplicates_removed": duplicate_count,
        "source_unverified": sum(
            not question["source_verified"] for question in new_questions
        ),
        "requested_count": count,
    }
