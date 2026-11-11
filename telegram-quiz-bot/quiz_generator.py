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

_LOCATION_RE = re.compile(r"(?im)^[ \t]*\[(?:Page|Paragraph|Line)\s+\d+\][^\n]*")
_INLINE_LOCATION_PREFIX_RE = re.compile(
    r"(?i)^\s*\[(?:Page|Paragraph|Line)\s+\d+\]\s*"
)
_QA_QUESTION_RE = re.compile(
    r"(?i)^\s*(?:(?:\d+)[.)]\s*)?(?:savol|question|q)\s*(?:#?\s*\d+)?\s*:\s*(.*?)\s*$"
)
_QA_ANSWER_RE = re.compile(
    r"(?i)^\s*(?:(?:\d+)[.)]\s*)?(?:javob|answer)\s*(?:#?\s*\d+)?\s*:\s*(.*?)\s*$"
)
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


def extract_question_answer_pairs(content: str) -> list[dict[str, Any]]:
    """Extract explicitly labeled Q&A pairs without rewriting their wording."""
    pairs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    active_field: str | None = None
    offset = 0
    pending_location_start: int | None = None

    def finish(end: int) -> None:
        nonlocal current, active_field
        if current is None:
            return
        question = " ".join(current["question_parts"]).strip()
        answer = " ".join(current["answer_parts"]).strip()
        if question and answer:
            excerpt = content[current["start"]:end]
            answer_position = excerpt.find(current["answer_parts"][0]) if current["answer_parts"] else -1
            legal = list(_LEGAL_LOCATOR_RE.finditer(excerpt))
            locations = list(_LOCATION_RE.finditer(excerpt))
            before_answer = [match for match in legal if match.start() <= answer_position]
            if before_answer:
                reference = before_answer[-1].group(0).strip()
            else:
                before_answer = [
                    match for match in locations if match.start() <= answer_position
                ]
                reference = (
                    before_answer[-1].group(0).strip()
                    if before_answer
                    else current["question_label"]
                )
            pairs.append(
                {
                    "question": question,
                    "answer": answer,
                    "source_excerpt": excerpt,
                    "source_reference": reference,
                    "start": current["start"],
                    "end": end,
                }
            )
        current = None
        active_field = None

    for raw_line in content.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        label_line = _INLINE_LOCATION_PREFIX_RE.sub("", line, count=1)
        question_match = _QA_QUESTION_RE.match(label_line)
        answer_match = _QA_ANSWER_RE.match(label_line)

        if (
            current is not None
            and active_field == "answer"
            and _LEGAL_HEADING_RE.match(label_line)
        ):
            finish(
                pending_location_start
                if pending_location_start is not None
                else offset
            )
            pending_location_start = None

        if question_match:
            pair_start = (
                pending_location_start
                if pending_location_start is not None
                else offset
            )
            finish(pair_start)
            label = label_line[: label_line.find(":") + 1].strip()
            current = {
                "start": pair_start,
                "question_label": label,
                "question_parts": [question_match.group(1).strip()]
                if question_match.group(1).strip()
                else [],
                "answer_parts": [],
            }
            active_field = "question"
            pending_location_start = None
        elif answer_match and current is not None and active_field == "question":
            current["answer_parts"].append(answer_match.group(1).strip())
            active_field = "answer"
            pending_location_start = None
        elif _LOCATION_RE.match(line):
            pending_location_start = offset
        elif current is not None:
            text_line = line.strip()
            if text_line and active_field == "question":
                current["question_parts"].append(text_line)
            elif text_line and active_field == "answer":
                current["answer_parts"].append(text_line)
        offset += len(raw_line)

    finish(len(content))
    return pairs


def classify_document(content: str) -> str:
    """Separate explicit Q&A material from legal and general prose."""
    pairs = extract_question_answer_pairs(content)
    if pairs:
        covered_characters = sum(
            max(0, pair["end"] - pair["start"]) for pair in pairs
        )
        legal_outside_pairs = any(
            not any(pair["start"] <= match.start() < pair["end"] for pair in pairs)
            for match in _LEGAL_LOCATOR_RE.finditer(content)
        )
        coverage = covered_characters / max(len(content), 1)
        if legal_outside_pairs or coverage < 0.60:
            return "mixed"
        return "question_answer"
    has_partial_qa_labels = any(
        _QA_QUESTION_RE.match(
            _INLINE_LOCATION_PREFIX_RE.sub("", line.rstrip("\r\n"), count=1)
        )
        or _QA_ANSWER_RE.match(
            _INLINE_LOCATION_PREFIX_RE.sub("", line.rstrip("\r\n"), count=1)
        )
        for line in content.splitlines(keepends=True)
    )
    if has_partial_qa_labels:
        return "mixed"
    if _LEGAL_LOCATOR_RE.search(content):
        return "normative_legal"
    return "general"


def build_source_sections(
    title: str,
    content: str,
    source_id: int | None = None,
    max_chars: int = SECTION_CHARS,
) -> tuple[str, list[dict[str, Any]]]:
    """Build ordered Q&A or prose sections and verify exact full-text coverage."""
    document_type = classify_document(content)
    pairs = extract_question_answer_pairs(content)
    if not pairs:
        sections = chunk_document(
            title, content, source_id=source_id, max_chars=max_chars
        )
        for section in sections:
            section["document_type"] = document_type
            section["section_kind"] = "prose"
        return document_type, sections

    pair_sections: list[dict[str, Any]] = []
    ranges: list[tuple[int, int, dict[str, Any] | None]] = []
    cursor = 0
    for pair in pairs:
        if pair["start"] > cursor:
            ranges.append((cursor, pair["start"], None))
        ranges.append((pair["start"], pair["end"], pair))
        cursor = pair["end"]
    if cursor < len(content):
        ranges.append((cursor, len(content), None))

    section_number = 0
    for start, end, pair in ranges:
        excerpt = content[start:end]
        if pair is not None:
            if len(excerpt) > max_chars:
                raise ValueError(
                    f"{title}: Q&A jufti {max_chars} belgilik bo‘limdan uzun. "
                    "Faylni kichikroq qismlarga ajrating."
                )
            section_number += 1
            pair_sections.append(
                {
                    "source_id": source_id,
                    "source_title": title,
                    "section_number": section_number,
                    "start": start,
                    "end": end,
                    "source_excerpt": excerpt,
                    "content": excerpt,
                    "document_type": document_type,
                    "section_kind": "question_answer",
                    "original_question": pair["question"],
                    "original_answer": pair["answer"],
                    "source_reference": pair["source_reference"],
                }
            )
            continue

        if not excerpt:
            continue
        residual_type = classify_document(excerpt)
        for residual in chunk_document(
            title,
            excerpt,
            source_id=source_id,
            max_chars=max_chars,
        ):
            section_number += 1
            local_start = residual["start"]
            local_end = residual["end"]
            residual.update(
                {
                    "section_number": section_number,
                    "start": start + local_start,
                    "end": start + local_end,
                    "document_type": (
                        "mixed" if document_type == "mixed" else residual_type
                    ),
                    "section_kind": "prose",
                }
            )
            pair_sections.append(residual)

    pair_sections.sort(key=lambda section: section["start"])
    if "".join(section["source_excerpt"] for section in pair_sections) != content:
        raise RuntimeError("Q&A section splitting failed its full-text coverage check.")
    return document_type, pair_sections


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


async def _request_with_safe_repair(
    client: AsyncOpenAI,
    messages: list[dict[str, str]],
    validator: Callable[[dict[str, Any]], Any],
) -> Any:
    """Retry one malformed AI result with format guidance, never repair evidence."""
    try:
        payload = await _request_json(client, messages)
        return validator(payload)
    except ValueError as first_error:
        repair_messages = messages + [
            {
                "role": "user",
                "content": (
                    "SAFE FORMAT REPAIR: Previous output failed structural validation: "
                    f"{str(first_error)[:240]}. Return a corrected JSON object only. "
                    "Do not invent, replace, or guess facts, citations, source quotes, "
                    "questions, or answers. If the required result cannot be produced "
                    "from the supplied source, return an empty questions or distractors list."
                ),
            }
        ]
        try:
            repaired = await _request_json(client, repair_messages)
            return validator(repaired)
        except ValueError as second_error:
            raise ValueError(
                "AI output failed validation after one safe format-repair attempt."
            ) from second_error


async def _generate_for_section(
    client: AsyncOpenAI,
    section: dict[str, Any],
    count: int,
    answer_bank: list[str] | None = None,
) -> list[dict[str, Any]]:
    if section.get("section_kind") == "question_answer":
        return await _generate_from_question_answer(
            client, section, answer_bank or []
        )

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
- Huquqiy lokator bo‘lmasa, mavjud [Page N], [Paragraph N] yoki [Line N]
  belgisini ishlating. Manbada bunday belgi ham bo‘lmasa, bo‘sh qaytaring.
  Raqam yoki qonun moddasini aslo uydirmang.
- source_quote savolni asoslaydigan, manbadan aynan ko‘chirilgan kamida
  12 va ko‘pi bilan 800 belgili matn bo‘lsin. Iqtibosni tarjima yoki qayta yozmang.

Faqat JSON:
{{"questions":[{{"question":"...","options":["...","...","...","..."],
"correct_index":0,"source_reference":"...","source_quote":"..."}}]}}

Fayl nomi: {section["source_title"]}
Bo‘lim: {section["section_number"]}
    Hujjat turi: {section.get("document_type", "unknown")}
MATN:
{section["content"]}
"""
    messages = [
        {
            "role": "system",
            "content": (
                "Huquqiy aniqlik birinchi o‘rinda. Manbada ko‘rinmagan "
                "modda, band, raqam yoki faktni hech qachon uydirmang."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("questions")
        if not isinstance(items, list) or len(items) > count:
            raise ValueError("AI bo‘lim uchun noto‘g‘ri savollar ro‘yxatini qaytardi.")
        return [_validate_question(item) for item in items]

    return await _request_with_safe_repair(client, messages, validate)


async def _generate_from_question_answer(
    client: AsyncOpenAI,
    section: dict[str, Any],
    answer_bank: list[str],
) -> list[dict[str, Any]]:
    original_question = section["original_question"]
    original_answer = section["original_answer"]
    if (
        not original_question
        or len(original_question) > 300
        or not original_answer
        or len(original_answer) > 100
    ):
        raise ValueError(
            "Asl savol 300 yoki javob 100 belgidan oshdi; xavfsiz quiz varianti tuzilmadi."
        )
    prompt = f"""
Quyidagi savol-javob juftligidan BIR ta Telegram quiz uchun 3 ta distractor yarating.
Savol matnini va to‘g‘ri javobni manbadan aynan olinganidek saqlang.
Faqat distractorlar yaratiladi. Tashqi bilim yoki yangi qonun/fakt ishlatmang.
Mumkin bo‘lsa, boshqa original javoblardan aynan bittasini tanlab foydalaning.
Agar javoblar bankida 3 ta mos variant bo‘lmasa, manbadan faqat administrator
tekshirishi kerak bo‘lgan distractor taklif qiling.
Distractorlar bir-biridan va to‘g‘ri javobdan farqli bo‘lsin.

JSON шакли: {{"distractors":["...","...","..."]}}

ASL SAVOL: {original_question}
ASL TO‘G‘RI JAVOB: {original_answer}
ASL JUFTLIK MATNI:
{section["source_excerpt"]}
JAVOBLAR BANKI:
{json.dumps(answer_bank, ensure_ascii=False)}
"""
    messages = [
        {
            "role": "system",
            "content": (
                "Manbadagi savol va javobni aynan saqlang. Dalil yoki havolani "
                "to‘qimang. Javoblar bankidan foydalaning; noaniqlik bo‘lsa, "
                "formatni bajaring va natija keyin NEEDS_REVIEW bo‘ladi."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    def validate(payload: dict[str, Any]) -> list[str]:
        distractors = payload.get("distractors")
        if (
            not isinstance(distractors, list)
            or len(distractors) != 3
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > 100
                for item in distractors
            )
        ):
            raise ValueError("AI uchta yaroqli distractor qaytarmadi.")
        cleaned = [item.strip() for item in distractors]
        normalized = {_normalize(item) for item in cleaned}
        if (
            len(normalized) != 3
            or _normalize(original_answer) in normalized
        ):
            raise ValueError("Asl savol-javob yoki distractorlar mos emas.")
        return cleaned

    distractors = await _request_with_safe_repair(client, messages, validate)
    normalized_bank = {_normalize(answer) for answer in answer_bank}
    bank_grounded = all(_normalize(item) in normalized_bank for item in distractors)
    return [
        {
            "question": original_question,
            "options": [original_answer, *distractors],
            "correct_index": 0,
            "source_reference": section["source_reference"],
            "source_quote": section["source_excerpt"].strip(),
            "source_excerpt": section["source_excerpt"],
            "document_type": section.get("document_type", "question_answer"),
            "original_question": original_question,
            "original_answer": original_answer,
            "distractors_grounded": bank_grounded,
        }
    ]


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
    document_types: dict[int, str] = {}
    answer_banks: dict[int, list[str]] = {}
    for source in sources:
        document_type, source_sections = build_source_sections(
            source["title"],
            source["content"],
            source_id=source["id"],
            max_chars=section_chars,
        )
        document_types[source["id"]] = document_type
        answer_banks[source["id"]] = [
            pair["answer"]
            for pair in extract_question_answer_pairs(source["content"])
        ]
        sections.extend(source_sections)
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
                client,
                section,
                quotas[index - 1],
                answer_banks.get(section["source_id"], []),
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
                document_type = section.get("document_type", "unknown")
                needs_review = not verified
                if document_type in {"mixed", "unknown"}:
                    needs_review = True
                    review_note = (
                        review_note
                        or "Hujjat turi aralash yoki noma’lum; administrator mazmunni tekshirsin."
                    )
                if not candidate.get("distractors_grounded", True):
                    needs_review = True
                    review_note = (
                        review_note
                        or "Q/A distractorlari javoblar bankida topilmadi; administrator tekshirsin."
                    )
                candidate.update(
                    {
                        "source_id": section["source_id"],
                        "source_ids": [section["source_id"]],
                        "source_title": section["source_title"],
                        "source_verified": verified,
                        "source_review_note": review_note if needs_review else "",
                        "source_excerpt": section["source_excerpt"],
                        "document_type": document_type,
                        "original_question": section.get("original_question", ""),
                        "original_answer": section.get("original_answer", ""),
                        "review_status": (
                            "NEEDS_REVIEW" if needs_review else "READY_FOR_APPROVAL"
                        ),
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
                else (
                    "Bo‘lim javobi tekshiruvdan o‘tmadi: "
                    f"{str(error)[:200]}"
                )
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

    document_type_counts: dict[str, int] = {}
    for document_type in document_types.values():
        document_type_counts[document_type] = (
            document_type_counts.get(document_type, 0) + 1
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
        "needs_review": sum(
            question["review_status"] == "NEEDS_REVIEW"
            for question in new_questions
        ),
        "ready_for_approval": sum(
            question["review_status"] == "READY_FOR_APPROVAL"
            for question in new_questions
        ),
        "document_types": {
            source_id: document_type
            for source_id, document_type in document_types.items()
        },
        "document_type_counts": document_type_counts,
        "requested_count": count,
    }
