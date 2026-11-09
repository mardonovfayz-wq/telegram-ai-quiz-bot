"""Document extraction and source-grounded question generation."""

import json
import re
import unicodedata
from io import BytesIO
from typing import Any

from docx import Document
from openai import AsyncOpenAI
from pypdf import PdfReader


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_CHARS = 45_000
MAX_QUESTIONS_PER_REQUEST = 20


def extract_document(filename: str, file_bytes: bytes) -> str:
    """Extract text and add simple page, paragraph, or line markers."""
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
    if len(content) > MAX_SOURCE_CHARS:
        raise ValueError(
            "Fayldagi matn juda uzun. Iltimos, 45 000 belgidan qisqaroq manba yuboring."
        )
    return content


def prepare_plain_text(text: str) -> str:
    if not text.strip():
        raise ValueError("Bo‘sh matnni manba sifatida saqlab bo‘lmaydi.")
    if len(text) > MAX_SOURCE_CHARS:
        raise ValueError("Matn 45 000 belgidan uzun. Qisqaroq matn yuboring.")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    content = "\n\n".join(
        f"[Paragraph {number}]\n{paragraph}"
        for number, paragraph in enumerate(paragraphs, start=1)
    )
    if len(content) > MAX_SOURCE_CHARS:
        raise ValueError("Matn belgilari soni juda katta. Qisqaroq matn yuboring.")
    return content


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = normalized.replace("’", "'").replace("‘", "'").replace("`", "'")
    return " ".join(normalized.split())


def validate_grounding(
    source_title: str,
    source_reference: str,
    source_quote: str,
    available_sources: list[dict[str, Any]],
) -> None:
    """Require the citation and supporting quote to exist in the named source."""
    source = next(
        (
            item
            for item in available_sources
            if _normalize(item["title"]) == _normalize(source_title)
        ),
        None,
    )
    if source is None:
        raise ValueError("Savoldagi manba nomi yuborilgan fayllarga mos emas.")
    source_text = _normalize(source["content"])
    if not source_reference.strip() or _normalize(source_reference) not in source_text:
        raise ValueError(
            "Manba havolasi yuklangan matndan topilmadi; savol tasdiqlashga qo‘yilmadi."
        )
    if len(source_quote.strip()) < 12 or _normalize(source_quote) not in source_text:
        raise ValueError(
            "Savolni asoslovchi aniq iqtibos manbada topilmadi; savol saqlanmadi."
        )


async def generate_questions(
    client: AsyncOpenAI,
    sources: list[dict[str, Any]],
    count: int,
) -> list[dict[str, Any]]:
    if not sources:
        raise ValueError("Avval /upload orqali kamida bitta manba yuboring.")
    if not 1 <= count <= MAX_QUESTIONS_PER_REQUEST:
        raise ValueError("Bir so‘rovda 1 dan 20 tagacha savol so‘rash mumkin.")

    source_text = "\n\n".join(
        f"=== SOURCE FILE: {source['title']} ===\n{source['content']}"
        for source in sources
    )
    if len(source_text) > MAX_SOURCE_CHARS:
        raise ValueError(
            "Saqlangan manbalar jami 45 000 belgidan oshdi. "
            "Iltimos, keraksiz uzun fayllarni olib tashlang yoki qisqaroq matn yuboring."
        )

    prompt = f"""
Siz manbaga sodiq quiz tuzuvchisiz. Savollarni faqat quyida berilgan matndan tuzing.
Qo‘shimcha bilim, taxmin yoki manbada yo‘q faktlardan foydalanmang.

Har bir savol uchun:
- Uzbek tilida, lotin yozuvida savol tuzing.
- Aynan 4 ta turli javob varianti bering.
- correct_index 0 dan boshlanadigan yagona to‘g‘ri javob indeksidir.
- source_title manba sarlavhalaridan biriga aynan mos bo‘lsin.
- source_reference manbaning ichida aynan uchraydigan modda, band, bo‘lim,
  sahifa, paragraf yoki satr belgisidan olingan qisqa havola bo‘lsin.
- Agar manbada modda/band nomi bo‘lmasa, mavjud [Page N], [Paragraph N]
  yoki [Line N] belgisidan foydalaning. Huquqiy raqamni uydirmang.
- source_quote manbadan olingan, savol va javobni bevosita asoslaydigan
  kamida 12 belgili aniq iqtibos bo‘lsin. Uni tarjima yoki qayta yozmang.
- Yuborilgan matn savolga yetarli dalil bermasa, bu savolni yaratmang.

Faqat quyidagi JSON shaklida javob bering:
{{"questions":[{{"question":"...","options":["...","...","...","..."],
"correct_index":0,"source_title":"...","source_reference":"...",
"source_quote":"..."}}]}}
Aniq {count} ta savol qaytaring.

YUKLANGAN MANBALAR:
{source_text}
"""

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Manbaga qat’iy rioya qiling. Har qanday shubhada savol "
                    "to‘qimang. Faqat talab qilingan JSONni qaytaring."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw_content = response.choices[0].message.content
    if not raw_content:
        raise ValueError("AI bo‘sh javob qaytardi. Qayta urinib ko‘ring.")
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        raise ValueError("AI javobini savollar ro‘yxati sifatida o‘qib bo‘lmadi.") from error

    items = payload.get("questions")
    if not isinstance(items, list) or len(items) != count:
        raise ValueError(
            "AI so‘ralgan miqdorda savol yarata olmadi. Boshqa miqdorni so‘rab ko‘ring."
        )

    generated: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("AI javobida savol formati noto‘g‘ri.")
        question = str(item.get("question", "")).strip()
        options = item.get("options")
        source_title = str(item.get("source_title", "")).strip()
        source_reference = str(item.get("source_reference", "")).strip()
        source_quote = str(item.get("source_quote", "")).strip()
        correct_index = item.get("correct_index")

        if (
            not question
            or len(question) > 300
            or not isinstance(options, list)
            or len(options) != 4
            or any(not isinstance(option, str) or not option.strip() for option in options)
            or any(len(option.strip()) > 100 for option in options)
            or len({_normalize(option) for option in options}) != 4
            or not isinstance(correct_index, int)
            or isinstance(correct_index, bool)
            or not 0 <= correct_index < 4
            or not source_title
            or not source_reference
            or not source_quote
        ):
            raise ValueError(
                "AI qaytargan savollardan biri format talablariga mos kelmadi. "
                "Hech bir savol saqlanmadi."
            )

        validate_grounding(source_title, source_reference, source_quote, sources)
        if len(f"Manba: {source_title}, {source_reference}") > 200:
            raise ValueError(
                "Manba havolasi Telegram izohi uchun juda uzun. "
                "Qisqaroq fayl nomi yoki havoladan foydalaning."
            )
        generated.append(
            {
                "question": question,
                "options": [option.strip() for option in options],
                "correct_index": correct_index,
                "source_title": source_title,
                "source_reference": source_reference,
                "source_quote": source_quote,
            }
        )
    return generated
