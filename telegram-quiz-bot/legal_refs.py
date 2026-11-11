"""Strict legal-reference parsing for Uzbek (Latin and Cyrillic) and Russian text.

This module never invents a citation. It only:
- recognises numbered legal locators that are written in the text
  (``5-modda``, ``5-модда``, ``Статья 5``, ``2-qism``, ``часть 2``, ``3-band``,
  ``пункт 3``),
- reads the article -> part -> clause structure of a normative document, and
- checks that a quote really sits at the cited place in the source.

Matching uses strict number boundaries, so ``1-modda`` never matches inside
``21-modda`` or ``11-modda``, and ordinary words such as ``moddaning``,
``band bo'lgan`` or ``section about`` are not treated as legal locators.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

# Apostrophe variants used in Uzbek Latin script (o‘, g‘, ʻ, ʼ ...).
APOSTROPHES = "'’‘`ʻʼ"
_APOSTROPHE_TRANSLATION = {ord(char): "'" for char in APOSTROPHES}
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

# A provision number: 5, 25, or 25¹ (superscript numbering used in Uzbek codes).
_NUM = r"\d+(?:[¹²³⁰-⁹]+)?"

# Uzbek terms are written after the number with a hyphen: 5-modda, 2-қисм.
_UZ_TERMS = {
    "article": ("modda", "модда"),
    "part": ("qism", "қисм", "кисм"),
    "clause": ("band", "банд"),
}
# Russian / English terms are written before the number: Статья 5, пункт 3.
_PREFIX_TERMS = {
    "article": (
        r"стать(?:я|и|е|ю|ей)",
        r"ст\.",
        r"article",
        r"art\.",
    ),
    "part": (r"част(?:ь|и|ью|ей)", r"ч\.", r"part"),
    "clause": (r"пункт(?:а|е|ом|у|ы)?", r"п\.", r"clause"),
}

_LETTER_OR_APOSTROPHE = rf"(?:[^\W\d_]|[{APOSTROPHES}])"

_UZ_LOCATOR_RE = re.compile(
    rf"(?<![\w.])(?P<num>{_NUM})\s*[-–—]\s*"
    rf"(?P<term>{'|'.join(term for terms in _UZ_TERMS.values() for term in terms)})"
    rf"(?P<suffix>{_LETTER_OR_APOSTROPHE}*)",
    re.IGNORECASE,
)
_PREFIX_LOCATOR_RE = re.compile(
    rf"(?<![\w])(?P<term>{'|'.join(term for terms in _PREFIX_TERMS.values() for term in terms)})"
    rf"\s*(?:№\s*)?(?P<num>{_NUM})(?![\d\w])",
    re.IGNORECASE,
)
MARKER_RE = re.compile(r"\[(?:Page|Paragraph|Line)\s+\d+\]")
_LINE_MARKER_PREFIX_RE = re.compile(r"^[ \t]*\[(?:Page|Paragraph|Line)\s+\d+\][ \t]*")

# Article headings must start a line and carry no grammatical suffix:
# "5-modda. Title" is a heading, "5-moddasida ko'rsatilgan" is prose.
_ARTICLE_HEADING_RE = re.compile(
    rf"^[ \t]*(?:\[(?:Page|Paragraph|Line)\s+\d+\][ \t]*)?(?:"
    rf"(?P<uz_num>{_NUM})\s*[-–—]\s*(?P<uz_term>modda|модда)(?!{_LETTER_OR_APOSTROPHE})"
    rf"|(?P<ru_term>статья|article)\s+(?:№\s*)?(?P<ru_num>{_NUM})(?=[.:]|[ \t]*$)"
    rf")",
    re.IGNORECASE | re.MULTILINE,
)
_CHAPTER_HEADING_RE = re.compile(
    rf"^[ \t]*(?:\[(?:Page|Paragraph|Line)\s+\d+\][ \t]*)?(?:"
    rf"(?:\d+|[IVXLCDM]+)\s*[-–—.]?\s*(?:bob|боб|bo[{APOSTROPHES}]lim|бўлим)"
    rf"(?!{_LETTER_OR_APOSTROPHE})"
    rf"|(?:глава|раздел|chapter|section|§)\s*(?:\d+|[IVXLCDM]+)(?![\w])"
    rf")",
    re.IGNORECASE | re.MULTILINE,
)
# Any heading that opens a new legal unit; used for section boundaries.
LEGAL_HEADING_RE = re.compile(
    rf"(?:{_ARTICLE_HEADING_RE.pattern})|(?:{_CHAPTER_HEADING_RE.pattern})",
    re.IGNORECASE | re.MULTILINE,
)

_EXPLICIT_PART_RE = re.compile(r"^\s*(\d+)\.\s+\S")
_CLAUSE_RE = re.compile(r"^\s*(\d+)\)\s*")
_SUBCLAUSE_RE = re.compile(r"^\s*([a-zа-яёўқғҳ])\)\s*", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*[-–—•]\s+")
_SENTENCE_END_RE = re.compile(r"[.!?…][»\"”'’)\]]*\s*$")

_CANONICAL_TERMS = {
    "uz_latn": {"article": "modda", "part": "qism", "clause": "band"},
    "uz_cyrl": {"article": "модда", "part": "қисм", "clause": "банд"},
}


def _kind_of_uz_term(term: str) -> str:
    lowered = term.lower()
    for kind, terms in _UZ_TERMS.items():
        if lowered in terms:
            return kind
    raise ValueError(term)


def _kind_of_prefix_term(term: str) -> str:
    lowered = term.lower()
    for kind, patterns in _PREFIX_TERMS.items():
        if any(re.fullmatch(pattern, lowered) for pattern in patterns):
            return kind
    raise ValueError(term)


def _script_of(term: str) -> str:
    lowered = term.lower()
    if lowered in {"modda", "qism", "band"}:
        return "uz_latn"
    if lowered in {"модда", "қисм", "кисм", "банд"}:
        return "uz_cyrl"
    if re.search(r"[а-яё]", lowered):
        return "ru"
    return "en"


def number_key(number: str) -> str:
    """Comparable form of a provision number; 25¹ stays distinct from 251."""
    digits = []
    for char in number.strip():
        if char.isascii() and char.isdigit():
            digits.append(char)
        else:
            digits.append("^" + char.translate(_SUPERSCRIPT_DIGITS))
    return "".join(digits)


# ---------------------------------------------------------------------------
# Normalisation with an index map back to the raw text.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def normalize_with_map(text: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    """Normalise like ``normalize`` and map every output char to raw offsets.

    Returns ``(normalized, starts, ends)`` where ``starts[i]``/``ends[i]`` are
    the raw-text span that produced normalised character ``i``.
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending_space: tuple[int, int] | None = None
    index = 0
    length = len(text)
    while index < length:
        unit_start = index
        index += 1
        while index < length and unicodedata.combining(text[index]):
            index += 1
        unit = text[unit_start:index]
        if unit.isspace():
            if out and pending_space is None:
                pending_space = (unit_start, index)
            continue
        produced = (
            unicodedata.normalize("NFKC", unit).lower().translate(_APOSTROPHE_TRANSLATION)
        )
        if not produced:
            continue
        if pending_space is not None:
            out.append(" ")
            starts.append(pending_space[0])
            ends.append(pending_space[1])
            pending_space = None
        for char in produced:
            if char.isspace():
                if out and out[-1] != " ":
                    out.append(" ")
                    starts.append(unit_start)
                    ends.append(index)
                continue
            out.append(char)
            starts.append(unit_start)
            ends.append(index)
    while out and out[-1] == " ":
        out.pop()
        starts.pop()
        ends.pop()
    return "".join(out), tuple(starts), tuple(ends)


def normalize(text: str) -> str:
    """Case-, apostrophe-, Unicode- and whitespace-insensitive comparison form."""
    return normalize_with_map(text)[0]


# ---------------------------------------------------------------------------
# Locators written inside text (references, Q&A answers, AI output).
# ---------------------------------------------------------------------------


@dataclass
class Locator:
    kind: str  # "article" | "part" | "clause"
    number: str
    script: str
    text: str
    start: int
    end: int


def find_locators(text: str) -> list[Locator]:
    """Every numbered article/part/clause locator written in ``text``."""
    found: list[Locator] = []
    for match in _UZ_LOCATOR_RE.finditer(text):
        found.append(
            Locator(
                kind=_kind_of_uz_term(match.group("term")),
                number=match.group("num"),
                script=_script_of(match.group("term")),
                text=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )
    for match in _PREFIX_LOCATOR_RE.finditer(text):
        if any(item.start <= match.start() < item.end for item in found):
            continue
        found.append(
            Locator(
                kind=_kind_of_prefix_term(match.group("term")),
                number=match.group("num"),
                script=_script_of(match.group("term")),
                text=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )
    found.sort(key=lambda item: item.start)
    return found


@dataclass
class ParsedReference:
    articles: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    clauses: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    leftover: str = ""

    @property
    def empty(self) -> bool:
        return not (self.articles or self.parts or self.clauses or self.markers)


def parse_reference(reference: str) -> ParsedReference:
    """Split a citation into article/part/clause numbers and location markers."""
    parsed = ParsedReference()
    spans: list[tuple[int, int]] = []
    for locator in find_locators(reference):
        target = {
            "article": parsed.articles,
            "part": parsed.parts,
            "clause": parsed.clauses,
        }[locator.kind]
        key = number_key(locator.number)
        if key not in target:
            target.append(key)
        spans.append((locator.start, locator.end))
    for match in MARKER_RE.finditer(reference):
        if match.group(0) not in parsed.markers:
            parsed.markers.append(match.group(0))
        spans.append((match.start(), match.end()))
    leftover_chars = list(reference)
    for start, end in spans:
        for position in range(start, end):
            leftover_chars[position] = " "
    parsed.leftover = "".join(leftover_chars)
    return parsed


def find_inline_references(text: str) -> list[str]:
    """Exact locator strings written in ``text``, in order, without duplicates."""
    seen: list[str] = []
    for locator in find_locators(text):
        cleaned = locator.text.strip()
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


# ---------------------------------------------------------------------------
# Article -> part -> clause structure of a normative document.
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start: int
    end: int
    article: str | None = None
    article_label: str | None = None
    script: str | None = None
    part: str | None = None
    part_explicit: bool = False
    clause: str | None = None
    is_heading: bool = False
    article_start: int | None = None


@dataclass
class LegalStructure:
    segments: list[Segment]
    part_counts: dict[int, int | None]  # article heading offset -> part count
    has_articles: bool
    paragraph_mode: bool
    _starts: list[int] = field(default_factory=list)

    def locate(self, position: int) -> Segment | None:
        index = bisect.bisect_right(self._starts, position) - 1
        if index < 0:
            return None
        segment = self.segments[index]
        return segment if segment.start <= position < segment.end else None


def _logical_lines(text: str):
    """Yield (line_start, content_start, content) for non-empty, non-marker lines."""
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        prefix = _LINE_MARKER_PREFIX_RE.match(line)
        content_start = offset + (prefix.end() if prefix else 0)
        content = line[prefix.end():] if prefix else line
        if content.strip():
            yield offset, content_start, content
        offset += len(raw_line)


@lru_cache(maxsize=16)
def parse_legal_structure(text: str) -> LegalStructure:
    """Read article -> part -> clause structure from extracted document text.

    Part numbers come only from explicit "1." numbering, or (for DOCX, TXT and
    pasted text, where each line is a real paragraph) from paragraph order.
    In PDF text (``[Page N]`` markers), line breaks are layout wraps, so
    unnumbered parts are reported as unknown instead of being guessed.
    """
    paragraph_mode = "[Page " not in text
    headings = []
    for match in _ARTICLE_HEADING_RE.finditer(text):
        term = match.group("uz_term") or match.group("ru_term")
        number = match.group("uz_num") or match.group("ru_num")
        headings.append((match.start(), number, term, match.group(0).strip()))
    chapter_starts = [match.start() for match in _CHAPTER_HEADING_RE.finditer(text)]

    segments: list[Segment] = []
    part_counts: dict[int, int | None] = {}
    lines = list(_logical_lines(text))
    line_starts = [item[0] for item in lines]

    for heading_index, (heading_start, number, term, label) in enumerate(headings):
        # The article runs until the next article or chapter heading.
        candidates = [
            position
            for position in chapter_starts
            if position > heading_start
        ]
        if heading_index + 1 < len(headings):
            candidates.append(headings[heading_index + 1][0])
        article_end = min(candidates) if candidates else len(text)
        line_index = bisect.bisect_left(line_starts, heading_start)
        article_lines = []
        while line_index < len(lines) and lines[line_index][0] < article_end:
            article_lines.append(lines[line_index])
            line_index += 1
        if not article_lines:
            continue

        script = _script_of(term)
        number = number.strip()
        heading_line = article_lines[0]
        body = article_lines[1:]
        has_explicit_parts = any(_EXPLICIT_PART_RE.match(item[2]) for item in body)
        first_body_start = body[0][0] if body else article_end
        segments.append(
            Segment(
                start=heading_start,
                end=first_body_start,
                article=number,
                article_label=label,
                script=script,
                is_heading=True,
                article_start=heading_start,
            )
        )

        part: str | None = None
        part_explicit = False
        clause: str | None = None
        part_number = 0
        explicit_numbers: set[str] = set()
        parts_known = has_explicit_parts or paragraph_mode
        previous_content = heading_line[2]
        for position, (line_start, _content_start, content) in enumerate(body):
            line_end = body[position + 1][0] if position + 1 < len(body) else article_end
            explicit = _EXPLICIT_PART_RE.match(content)
            clause_match = _CLAUSE_RE.match(content)
            sentence_break = _SENTENCE_END_RE.search(previous_content) is not None
            if explicit:
                part = explicit.group(1)
                explicit_numbers.add(part)
                part_explicit = True
                clause = None
            elif clause_match:
                clause = clause_match.group(1)
            elif _SUBCLAUSE_RE.match(content) or _LIST_ITEM_RE.match(content):
                pass  # belongs to the current part/clause
            elif has_explicit_parts or not paragraph_mode:
                # Continuation text; a finished sentence ends any clause list.
                if sentence_break:
                    clause = None
            elif part is None or sentence_break:
                part_number += 1
                part = str(part_number)
                part_explicit = False
                clause = None
            previous_content = content
            segments.append(
                Segment(
                    start=line_start,
                    end=line_end,
                    article=number,
                    article_label=label,
                    script=script,
                    part=part if parts_known else None,
                    part_explicit=part_explicit,
                    clause=clause,
                    article_start=heading_start,
                )
            )
        if has_explicit_parts:
            part_counts[heading_start] = len(explicit_numbers)
        elif paragraph_mode:
            part_counts[heading_start] = part_number
        else:
            part_counts[heading_start] = None

    segments.sort(key=lambda item: item.start)
    structure = LegalStructure(
        segments=segments,
        part_counts=part_counts,
        has_articles=bool(headings),
        paragraph_mode=paragraph_mode,
    )
    structure._starts = [segment.start for segment in segments]
    return structure


def has_article_headings(text: str) -> bool:
    return _ARTICLE_HEADING_RE.search(text) is not None


def article_heading_positions(text: str) -> list[int]:
    return [match.start() for match in _ARTICLE_HEADING_RE.finditer(text)]


def _article_part_count(structure: LegalStructure, segment: Segment) -> int | None:
    return structure.part_counts.get(segment.article_start)


def format_reference(
    script: str | None,
    article: str | None,
    part: str | None = None,
    clause: str | None = None,
) -> str:
    """Canonical citation text built only from located numbers."""
    if not article:
        return ""
    if script in _CANONICAL_TERMS:
        terms = _CANONICAL_TERMS[script]
        pieces = [f"{article}-{terms['article']}"]
        if part:
            pieces.append(f"{part}-{terms['part']}")
        if clause:
            pieces.append(f"{clause}-{terms['clause']}")
        return ", ".join(pieces)
    if script == "en":
        pieces = [f"Article {article}"]
        if part:
            pieces.append(f"part {part}")
        if clause:
            pieces.append(f"clause {clause}")
        return ", ".join(pieces)
    pieces = [f"Статья {article}"]
    if part:
        pieces.append(f"часть {part}")
    if clause:
        pieces.append(f"пункт {clause}")
    return ", ".join(pieces)


# ---------------------------------------------------------------------------
# Citation verification.
# ---------------------------------------------------------------------------

MIN_QUOTE_CHARS = 12


@dataclass
class CitationCheck:
    verified: bool
    note: str = ""
    article: str | None = None
    part: str | None = None
    clause: str | None = None
    markers: list[str] = field(default_factory=list)
    canonical_reference: str = ""
    quote_start: int | None = None
    quote_end: int | None = None


def _markers_for_span(text: str, start: int, end: int) -> list[str]:
    markers = list(MARKER_RE.finditer(text))
    located: list[str] = []
    before = [match for match in markers if match.start() <= start]
    if before:
        located.append(before[-1].group(0))
    for match in markers:
        if start < match.start() < end and match.group(0) not in located:
            located.append(match.group(0))
    return located


def _check_occurrence(
    source_content: str,
    structure: LegalStructure,
    parsed: ParsedReference,
    raw_start: int,
    raw_end: int,
) -> CitationCheck:
    start_segment = structure.locate(raw_start)
    end_segment = structure.locate(max(raw_start, raw_end - 1))
    article = start_segment.article if start_segment else None
    end_article = end_segment.article if end_segment else None
    located_markers = _markers_for_span(source_content, raw_start, raw_end)
    result = CitationCheck(
        verified=False,
        article=article,
        part=start_segment.part if start_segment else None,
        clause=start_segment.clause if start_segment else None,
        markers=located_markers,
        quote_start=raw_start,
        quote_end=raw_end,
    )
    if article != end_article:
        result.note = "Iqtibos bir nechta moddani qamrab olgan; bitta modda ichidan iqtibos tanlang."
        return result
    if parsed.articles:
        if article is None:
            result.note = "Iqtibos hech bir modda ichida emas, lekin havolada modda ko‘rsatilgan."
            return result
        if parsed.articles[0] != number_key(article):
            result.note = (
                f"Iqtibos {start_segment.article_label} ichida joylashgan; havoladagi "
                "modda raqami mos kelmadi."
            )
            return result
    elif article is not None:
        result.note = (
            f"Iqtibos {start_segment.article_label} ichida joylashgan, lekin havolada "
            "huquqiy modda ko‘rsatilmagan."
        )
        return result
    if parsed.parts:
        if start_segment is None or start_segment.part is None:
            result.note = "Qism raqamini manbadan aniq aniqlab bo‘lmadi; administrator tekshirsin."
            return result
        if parsed.parts[0] != number_key(start_segment.part):
            result.note = "Havoladagi qism (часть) raqami iqtibos joylashgan qismga mos emas."
            return result
    if parsed.clauses:
        if start_segment is None or start_segment.clause is None:
            result.note = "Iqtibos raqamlangan band (пункт) ichida emas."
            return result
        if parsed.clauses[0] != number_key(start_segment.clause):
            result.note = "Havoladagi band (пункт) raqami iqtibos joylashgan bandga mos emas."
            return result
    for marker in parsed.markers:
        if marker not in located_markers:
            result.note = f"{marker} belgisi iqtibos joylashgan joyga mos emas."
            return result

    if article is not None:
        part_count = _article_part_count(structure, start_segment)
        include_part = start_segment.part is not None and (
            start_segment.part_explicit
            or bool(parsed.parts)
            or (part_count is not None and part_count > 1)
        )
        include_clause = start_segment.clause is not None and (
            bool(parsed.clauses) or include_part or part_count == 1
        )
        result.canonical_reference = format_reference(
            start_segment.script,
            article,
            start_segment.part if include_part else None,
            start_segment.clause if include_clause else None,
        )
    else:
        if located_markers:
            result.canonical_reference = (
                located_markers[0]
                if len(located_markers) == 1
                else f"{located_markers[0]}–{located_markers[-1]}"
            )
    result.verified = True
    return result


def locate_citation(
    source_content: str,
    reference: str,
    quote: str,
    scope: tuple[int, int] | None = None,
) -> CitationCheck:
    """Verify that ``quote`` is in the source at the place ``reference`` names.

    ``scope`` is the raw (start, end) range of the processed section; the quote
    must be found inside it when given.
    """
    quote = (quote or "").strip()
    reference = (reference or "").strip()
    if len(quote) < MIN_QUOTE_CHARS:
        return CitationCheck(False, "Asoslovchi iqtibos juda qisqa yoki bo‘sh.")
    if not reference:
        return CitationCheck(False, "Manba havolasi bo‘sh.")

    parsed = parse_reference(reference)
    if parsed.empty:
        return CitationCheck(
            False,
            "Havolada modda/qism/band yoki [Page/Paragraph/Line] belgisi topilmadi.",
        )
    if len(parsed.articles) > 1 or len(parsed.parts) > 1 or len(parsed.clauses) > 1:
        return CitationCheck(
            False, "Havolada bir nechta modda, qism yoki band bor; bitta aniq havola kiriting."
        )

    normalized_source, starts, ends = normalize_with_map(source_content)
    leftover_words = re.findall(r"[^\W\d_]{3,}", normalize(parsed.leftover))
    if any(word not in normalized_source for word in leftover_words):
        return CitationCheck(False, "Havoladagi matn manbada topilmadi.")

    normalized_quote = normalize(quote)
    occurrences: list[tuple[int, int]] = []
    search_from = 0
    while True:
        position = normalized_source.find(normalized_quote, search_from)
        if position < 0:
            break
        occurrences.append(
            (starts[position], ends[position + len(normalized_quote) - 1])
        )
        search_from = position + 1
    if not occurrences:
        return CitationCheck(False, "Asoslovchi iqtibos manbada aniq topilmadi.")
    if scope is not None:
        occurrences = [
            (start, end) for start, end in occurrences if scope[0] <= start and end <= scope[1]
        ]
        if not occurrences:
            return CitationCheck(
                False, "Asoslovchi iqtibos aynan qayta ishlangan bo‘limda topilmadi."
            )

    structure = parse_legal_structure(source_content)
    first_failure: CitationCheck | None = None
    for raw_start, raw_end in occurrences:
        result = _check_occurrence(source_content, structure, parsed, raw_start, raw_end)
        if result.verified:
            return result
        first_failure = first_failure or result
    return first_failure  # type: ignore[return-value]
