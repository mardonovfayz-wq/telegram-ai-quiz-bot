"""Document extraction and source-grounded question generation."""

import json
import asyncio
import bisect
import random
import re
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

from legal_refs import (
    LEGAL_HEADING_RE,
    MARKER_RE,
    CitationCheck,
    find_inline_references,
    has_article_headings,
    locate_citation,
    normalize,
)


MAX_FILE_BYTES = 20 * 1024 * 1024
SECTION_CHARS = 10_000
MAX_QUESTIONS_PER_REQUEST = 20
MAX_API_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 30
# Telegram quiz-poll limits.
POLL_QUESTION_MAX_CHARS = 300
POLL_OPTION_MAX_CHARS = 100
POLL_REFERENCE_MAX_CHARS = 190

_LOCATION_RE = re.compile(r"(?im)^[ \t]*\[(?:Page|Paragraph|Line)\s+\d+\][^\n]*")
_INLINE_LOCATION_PREFIX_RE = re.compile(
    r"(?i)^\s*\[(?:Page|Paragraph|Line)\s+\d+\]\s*"
)
# Q&A labels in Uzbek Latin, Uzbek Cyrillic, Russian and English, optionally
# numbered: "Savol:", "1-savol:", "Савол 2:", "3. Вопрос:", "Question №4:".
_QA_NUMBER_PREFIX = r"(?:(?:\d+)\s*[.)]\s*|\d+\s*-\s*)?"
_QA_NUMBER_SUFFIX = r"\s*(?:№|#)?\s*(?:\d+)?\s*[:：]\s*(.*?)\s*$"
_QA_QUESTION_RE = re.compile(
    rf"(?i)^\s*{_QA_NUMBER_PREFIX}(?:savol|савол|вопрос|question|q){_QA_NUMBER_SUFFIX}"
)
_QA_ANSWER_RE = re.compile(
    rf"(?i)^\s*{_QA_NUMBER_PREFIX}(?:javob|жавоб|ответ|answer){_QA_NUMBER_SUFFIX}"
)
_LEGAL_HEADING_RE = LEGAL_HEADING_RE

_system_random = random.SystemRandom()


class UnsuitableForPoll(ValueError):
    """An original Q&A pair cannot become a Telegram quiz without rewriting it."""


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
    all_markers = list(MARKER_RE.finditer(content))
    marker_starts = [match.start() for match in all_markers]

    def finish(end: int) -> None:
        nonlocal current, active_field
        if current is None:
            return
        question = " ".join(current["question_parts"]).strip()
        answer = " ".join(current["answer_parts"]).strip()
        if question and answer:
            excerpt = content[current["start"]:end]
            # The location reference is the document marker(s) the pair sits
            # under. Legal articles mentioned inside the answer are the Q&A
            # author's own legal basis and are kept separately, verbatim.
            first_index = bisect.bisect_right(marker_starts, current["start"])
            last_index = bisect.bisect_left(marker_starts, end)
            markers = all_markers[max(first_index - 1, 0):first_index]
            markers += all_markers[first_index:last_index]
            if not markers:
                reference = current["question_label"]
            elif len(markers) == 1:
                reference = markers[0].group(0)
            else:
                reference = f"{markers[0].group(0)}–{markers[-1].group(0)}"
            pairs.append(
                {
                    "question": question,
                    "answer": answer,
                    "source_excerpt": excerpt,
                    "source_reference": reference,
                    "legal_basis": "; ".join(find_inline_references(excerpt)),
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
            not any(pair["start"] <= position < pair["end"] for pair in pairs)
            for position in (
                match.start() for match in _LEGAL_HEADING_RE.finditer(content)
            )
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
    # Only real article headings ("5-modda.", "5-модда.", "Статья 5.") make a
    # document normative; words such as "moddaning" or "band" do not.
    if has_article_headings(content):
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
            # An over-long pair is kept whole; generation flags it as
            # unsuitable for a poll instead of aborting the whole run.
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
                    "legal_basis": pair.get("legal_basis", ""),
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


_normalize = normalize
_SECTION_CONTEXT_RE = re.compile(r"\A\[Section context: [^\n]*\]\n")


def _scope_of_section_text(content: str, section_text: str) -> tuple[int, int] | None:
    excerpt = _SECTION_CONTEXT_RE.sub("", section_text, count=1)
    position = content.find(excerpt) if excerpt else -1
    if position < 0:
        return None
    return position, position + len(excerpt)


def check_citation(
    source_title: str,
    source_reference: str,
    source_quote: str,
    available_sources: list[dict[str, Any]],
    section_text: str | None = None,
    scope: tuple[int, int] | None = None,
) -> CitationCheck:
    """Locate the quote in the source and check the cited article/part/clause.

    The reference must name exactly where the quote is: the enclosing article
    (strict number match, so 1-modda never passes for 21-modda), and the part
    and clause when they are cited. Nothing is auto-corrected; a mismatch is
    reported for administrator review.
    """
    source = next(
        (
            item
            for item in available_sources
            if _normalize(item["title"]) == _normalize(source_title)
        ),
        None,
    )
    if source is None:
        return CitationCheck(
            False, "Manba fayli mos kelmadi; fayl nomini administrator tekshirsin."
        )
    content = source["content"]
    if scope is None and section_text is not None:
        scope = _scope_of_section_text(content, section_text)
        if scope is None and _normalize(source_quote) not in _normalize(section_text):
            return CitationCheck(
                False, "Asoslovchi iqtibos aynan qayta ishlangan bo‘limda topilmadi."
            )
    return locate_citation(content, source_reference, source_quote, scope=scope)


def verify_grounding(
    source_title: str,
    source_reference: str,
    source_quote: str,
    available_sources: list[dict[str, Any]],
    section_text: str | None = None,
) -> tuple[bool, str]:
    """Backward-compatible (verified, note) wrapper around ``check_citation``."""
    result = check_citation(
        source_title,
        source_reference,
        source_quote,
        available_sources,
        section_text=section_text,
    )
    return result.verified, result.note


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
- source_reference iqtibos joylashgan aniq modda (modda/модда/статья),
  kerak bo‘lsa qism (qism/қисм/часть) va band (band/банд/пункт) raqamini
  bo‘limdagi kabi ko‘rsatsin, masalan "5-modda, 2-qism, 3-band",
  "5-модда, 2-қисм" yoki "Статья 5, часть 2, пункт 3". Bot iqtibos
  joylashgan joyni manbadan tekshiradi; mos kelmagan havola rad etiladi.
- Agar iqtibos moddaga tegishli bo‘lsa, modda raqamini albatta kiriting;
  sahifa belgisi modda o‘rnini bosa olmaydi.
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
    reason = qa_poll_unsuitability(section)
    if reason:
        raise UnsuitableForPoll(reason)
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
            "legal_basis": section.get("legal_basis", ""),
            "distractors_grounded": bank_grounded,
        }
    ]


def qa_poll_unsuitability(section: dict[str, Any]) -> str:
    """Why an original Q&A pair cannot be a Telegram quiz unchanged ('' if it can).

    The original wording is never shortened or rewritten to fit Telegram's
    limits; such pairs are reported to the administrator instead.
    """
    question = section.get("original_question", "")
    answer = section.get("original_answer", "")
    if not question or not answer:
        return "Asl savol yoki javob bo‘sh."
    if len(question) > POLL_QUESTION_MAX_CHARS:
        return (
            f"Asl savol {len(question)} belgi; Telegram quiz savoli "
            f"{POLL_QUESTION_MAX_CHARS} belgidan oshmasligi kerak."
        )
    if len(answer) > POLL_OPTION_MAX_CHARS:
        return (
            f"Asl javob {len(answer)} belgi; Telegram quiz varianti "
            f"{POLL_OPTION_MAX_CHARS} belgidan oshmasligi kerak."
        )
    return ""


def shuffle_options(
    options: list[str],
    correct_index: int,
    rng: random.Random | None = None,
) -> tuple[list[str], int]:
    """Randomise option order and return the new index of the same correct answer."""
    rng = rng or _system_random
    correct_text = options[correct_index]
    order = list(range(len(options)))
    rng.shuffle(order)
    shuffled = [options[index] for index in order]
    new_index = order.index(correct_index)
    if shuffled[new_index] != correct_text:  # defensive; must never happen
        raise RuntimeError("Answer shuffling lost the correct option.")
    return shuffled, new_index


def _finalize_candidate(
    section: dict[str, Any],
    candidate: dict[str, Any],
    source_record: dict[str, Any],
    rng: random.Random | None,
) -> dict[str, Any]:
    """Verify the citation, shuffle options and attach source links/review state."""
    citation = check_citation(
        section["source_title"],
        candidate["source_reference"],
        candidate["source_quote"],
        [source_record],
        scope=(section["start"], section["end"]),
    )
    verified, review_note = citation.verified, citation.note
    if verified and citation.canonical_reference:
        # Store the location derived from the document itself.
        candidate["source_reference"] = citation.canonical_reference
    candidate["options"], candidate["correct_index"] = shuffle_options(
        candidate["options"], candidate["correct_index"], rng
    )
    if len(candidate["source_reference"]) > POLL_REFERENCE_MAX_CHARS:
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
            "legal_basis": section.get("legal_basis", ""),
            "section_start": section["start"],
            "section_end": section["end"],
            "quote_start": citation.quote_start,
            "quote_end": citation.quote_end,
            "legal_article": citation.article or "",
            "legal_part": citation.part or "",
            "legal_clause": citation.clause or "",
            "review_status": (
                "NEEDS_REVIEW" if needs_review else "READY_FOR_APPROVAL"
            ),
        }
    )
    return candidate


def _unsuitable_item(section: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "source_id": section["source_id"],
        "source_title": section["source_title"],
        "section_number": section["section_number"],
        "section_start": section["start"],
        "section_end": section["end"],
        "source_reference": section.get("source_reference", ""),
        "original_question": section.get("original_question", ""),
        "original_answer": section.get("original_answer", ""),
        "reason": reason,
    }


async def generate_questions(
    client: AsyncOpenAI,
    sources: list[dict[str, Any]],
    count: int,
    existing_question_texts: list[str] | None = None,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    section_chars: int = SECTION_CHARS,
    rng: random.Random | None = None,
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
        # Only answers that fit in a poll option can serve as distractors.
        answer_banks[source["id"]] = [
            pair["answer"]
            for pair in extract_question_answer_pairs(source["content"])
            if len(pair["answer"]) <= POLL_OPTION_MAX_CHARS
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
    unsuitable_items: list[dict[str, Any]] = []
    duplicate_count = 0
    empty_section_count = 0
    processed_sections = 0
    content_by_source = {item["id"]: item["content"] for item in sources}

    for index, section in enumerate(sections, start=1):
        try:
            if section.get("section_kind") == "question_answer":
                reason = qa_poll_unsuitability(section)
                if reason:
                    # Report it; never shorten or rewrite the original pair.
                    unsuitable_items.append(_unsuitable_item(section, reason))
                    processed_sections += 1
                    if progress_callback:
                        await progress_callback(
                            processed_sections, total_sections, len(new_questions)
                        )
                    continue
            candidates = await _generate_for_section(
                client,
                section,
                quotas[index - 1],
                answer_banks.get(section["source_id"], []),
            )
            source_record = {
                "id": section["source_id"],
                "title": section["source_title"],
                "content": content_by_source[section["source_id"]],
            }
            if not candidates:
                empty_section_count += 1
            for candidate in candidates:
                candidate = _finalize_candidate(section, candidate, source_record, rng)
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
        "unsuitable_items": unsuitable_items,
        "unsuitable_count": len(unsuitable_items),
    }


# ---------------------------------------------------------------------------
# Phase 2: answer-support check and incremental, failure-tolerant generation.
# ---------------------------------------------------------------------------

MAX_QUESTIONS_PER_SECTION = 3
MAX_CONSECUTIVE_SECTION_FAILURES = 3
_NETWORK_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


def describe_section_error(error: Exception) -> str:
    if isinstance(error, _NETWORK_ERRORS):
        return "OpenAI rate limitasi, vaqt tugashi yoki tarmoq xatosi."
    return f"Bo‘lim javobi tekshiruvdan o‘tmadi: {str(error)[:200]}"


async def check_answer_support(
    client: AsyncOpenAI, question: dict[str, Any]
) -> tuple[bool, str]:
    """Second, independent AI check: does the quote alone support the answer?

    It can only move a draft to NEEDS_REVIEW; it never approves anything and
    never changes the question, options, quote or citation.
    """
    letters = ("A", "B", "C", "D")
    options = "\n".join(
        f"{letters[index]}) {option}" for index, option in enumerate(question["options"])
    )
    prompt = f"""
Faqat quyidagi IQTIBOSga tayanib tekshiring (tashqi bilimdan foydalanmang):
1) Belgilangan to‘g‘ri javob iqtibosdan bevosita kelib chiqadimi?
2) Qolgan variantlar iqtibosga ko‘ra noto‘g‘rimi?
Ikkalasi ham "ha" bo‘lsa supported=true, aks holda false. Shubha bo‘lsa false.

Faqat JSON: {{"supported": true, "reason": "qisqa izoh"}}

SAVOL: {question["question"]}
VARIANTLAR:
{options}
TO‘G‘RI JAVOB: {letters[question["correct_index"]]}
IQTIBOS:
{question["source_quote"]}
"""
    messages = [
        {
            "role": "system",
            "content": (
                "Siz huquqiy test savollarini tekshiruvchisiz. Faqat berilgan "
                "iqtibosga tayaning va shubhali holatda supported=false qaytaring."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    def validate(payload: dict[str, Any]) -> tuple[bool, str]:
        supported = payload.get("supported")
        if not isinstance(supported, bool):
            raise ValueError("supported maydoni true yoki false bo‘lishi kerak.")
        return supported, str(payload.get("reason", ""))[:300]

    return await _request_with_safe_repair(client, messages, validate)


async def _apply_answer_support_check(
    client: AsyncOpenAI, candidate: dict[str, Any]
) -> None:
    if candidate["review_status"] != "READY_FOR_APPROVAL":
        return
    if candidate.get("original_question"):
        # The source itself states this question and answer.
        return
    try:
        supported, reason = await check_answer_support(client, candidate)
    except Exception as error:  # the check must never stop generation
        supported = False
        reason = f"Javob tekshiruvi bajarilmadi ({type(error).__name__})."
    if not supported:
        candidate["review_status"] = "NEEDS_REVIEW"
        candidate["source_review_note"] = (
            "AI tekshiruvi: to‘g‘ri javob iqtibos bilan to‘liq asoslanmagan bo‘lishi "
            f"mumkin. {reason}".strip()
        )


async def generate_incremental(
    client: AsyncOpenAI,
    sources: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    count: int,
    existing_question_texts: list[str] | None = None,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    rng: random.Random | None = None,
    verify_answers: bool = True,
    max_per_section: int = MAX_QUESTIONS_PER_SECTION,
    max_consecutive_failures: int = MAX_CONSECUTIVE_SECTION_FAILURES,
) -> dict[str, Any]:
    """Process the given (not yet used) sections in order until ``count`` drafts.

    Unlike ``generate_questions``, a failing section does not discard the
    run: it is reported as "failed" (to be retried later) and the next section
    is processed. Only after several failures in a row (usually a network or
    OpenAI outage) does the run stop early, still keeping finished drafts.
    Each section dict must carry ``ledger_id``.
    """
    if not 1 <= count <= MAX_QUESTIONS_PER_REQUEST:
        raise ValueError("Bir so‘rovda 1 dan 20 tagacha savol so‘rash mumkin.")
    content_by_source = {item["id"]: item["content"] for item in sources}
    answer_banks = {
        item["id"]: [
            pair["answer"]
            for pair in extract_question_answer_pairs(item["content"])
            if len(pair["answer"]) <= POLL_OPTION_MAX_CHARS
        ]
        for item in sources
    }
    seen_questions = list(existing_question_texts or [])
    new_questions: list[dict[str, Any]] = []
    unsuitable_items: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    duplicate_count = 0
    consecutive_failures = 0
    stopped_early = False

    for section in sections:
        if len(new_questions) >= count:
            break
        outcome = {"ledger_id": section["ledger_id"], "status": "empty", "error": ""}
        if section.get("section_kind") == "question_answer":
            reason = qa_poll_unsuitability(section)
            if reason:
                unsuitable_items.append(_unsuitable_item(section, reason))
                outcome["status"] = "unsuitable"
                outcome["error"] = reason
                outcomes.append(outcome)
                continue
        try:
            candidates = await _generate_for_section(
                client,
                section,
                min(count - len(new_questions), max_per_section),
                answer_banks.get(section["source_id"], []),
            )
        except Exception as error:
            outcome["status"] = "failed"
            outcome["error"] = describe_section_error(error)
            outcomes.append(outcome)
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                stopped_early = True
                break
            continue
        consecutive_failures = 0
        source_record = {
            "id": section["source_id"],
            "title": section["source_title"],
            "content": content_by_source[section["source_id"]],
        }
        kept = 0
        for candidate in candidates:
            candidate = _finalize_candidate(section, candidate, source_record, rng)
            if is_near_duplicate(candidate["question"], seen_questions):
                duplicate_count += 1
                continue
            if verify_answers:
                await _apply_answer_support_check(client, candidate)
            new_questions.append(candidate)
            seen_questions.append(candidate["question"])
            kept += 1
        outcome["status"] = "used" if kept else "empty"
        outcomes.append(outcome)
        if progress_callback:
            await progress_callback(len(outcomes), len(sections), len(new_questions))

    return {
        "questions": new_questions,
        "section_outcomes": outcomes,
        "sections_processed": len(outcomes),
        "sections_failed": sum(item["status"] == "failed" for item in outcomes),
        "failed_reasons": [item["error"] for item in outcomes if item["status"] == "failed"],
        "sections_empty": sum(item["status"] == "empty" for item in outcomes),
        "sections_available": len(sections),
        "stopped_early": stopped_early,
        "duplicates_removed": duplicate_count,
        "unsuitable_items": unsuitable_items,
        "unsuitable_count": len(unsuitable_items),
        "needs_review": sum(q["review_status"] == "NEEDS_REVIEW" for q in new_questions),
        "ready_for_approval": sum(
            q["review_status"] == "READY_FOR_APPROVAL" for q in new_questions
        ),
        "source_unverified": sum(not q["source_verified"] for q in new_questions),
        "requested_count": count,
    }
