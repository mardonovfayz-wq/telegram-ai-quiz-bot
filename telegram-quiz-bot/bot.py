"""Telegram bot entry point.

All credentials and identifiers come from environment variables (Replit
Secrets or the host's environment); none are stored in tracked files.
"""

import asyncio
import logging
import os
import re
from datetime import time
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, PollType
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import storage
from bot_helpers import (
    build_poll_explanation,
    format_metadata,
    format_question as _format_question,
    parse_admin_id,
    parse_channel_id,
    parse_question_edit,
    parse_schedule,
    parse_source_metadata,
    source_display_name,
)
from quiz_generator import (
    MAX_FILE_BYTES,
    POLL_REFERENCE_MAX_CHARS,
    SectionProcessingError,
    check_citation,
    extract_document,
    generate_questions,
    prepare_plain_text,
)


def required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is missing. Add it in Replit Secrets / Environment variables."
        )
    return value


BOT_TOKEN = required_environment("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = required_environment("OPENAI_API_KEY")
ADMIN_ID = parse_admin_id(required_environment("ADMIN_ID"))
CHANNEL_ID = parse_channel_id(required_environment("CHANNEL_ID"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    timeout=45.0,
    max_retries=0,
)

HELP_TEXT = (
    "Telegram AI Quiz Bot buyruqlari:\n"
    "/start — botni boshlash\n"
    "/upload — PDF, DOCX, TXT yoki oddiy matn yuborish rejimi\n"
    "/generate [son] — manbalardan savollar yaratish (standart: 10)\n"
    "/questions [holat] [sahifa] — savollarni ko‘rish\n"
    "/sources — manbalar ro‘yxati va huquqiy hujjat ma’lumotlari\n"
    "/setmeta <ID> — manbaga rasmiy nom, raqam, sana va havola kiritish\n"
    "/schedule — nashr vaqtlarini ko‘rish yoki sozlash\n"
    "/help — yordam\n\n"
    "Yangi savollar avtomatik nashr qilinmaydi: har birini avval tasdiqlang."
)

METADATA_TEMPLATE = (
    "Nomi: O‘zbekiston Respublikasining ... kodeksi\n"
    "Raqami: O‘RQ-000\n"
    "Sana: 01.01.2024\n"
    "Havola: https://lex.uz/docs/...\n\n"
    "Kerakli qatorlarnigina yuboring. Maydonni tozalash uchun qiymat o‘rniga - yozing."
)

# Only private-chat messages (not channel posts, group messages or edits)
# ever reach the command, document and text handlers.
PRIVATE_MESSAGES = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE


def format_question(question: dict) -> str:
    sources = storage.get_sources_by_ids(question.get("source_ids") or [])
    return _format_question(question, sources[0] if sources else None)


async def is_authorized_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    chat = update.effective_chat
    if update.callback_query is None and (chat is None or chat.type != ChatType.PRIVATE):
        # Never answer in channels or groups, not even with an error message.
        return False

    user = update.effective_user
    if user is None or user.id != ADMIN_ID:
        if update.callback_query:
            await update.callback_query.answer(
                "Bu amal faqat administrator uchun.", show_alert=True
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "Bu botni faqat vakolatli administrator boshqarishi mumkin."
            )
        return False

    if chat and chat.type != ChatType.PRIVATE:
        if update.callback_query:
            await update.callback_query.answer(
                "Boshqaruv faqat shaxsiy chatda.", show_alert=True
            )
        return False
    return True


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await is_authorized_admin(update, context):
        await update.effective_message.reply_text(
            "Quiz bot tayyor. Manba yuborish uchun /upload buyrug‘ini bosing.\n\n"
            + HELP_TEXT
        )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await is_authorized_admin(update, context):
        await update.effective_message.reply_text(HELP_TEXT)


async def upload_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    context.user_data["awaiting_upload"] = True
    context.user_data.pop("awaiting_schedule", None)
    context.user_data.pop("editing_question_id", None)
    context.user_data.pop("metadata_source_id", None)
    await update.effective_message.reply_text(
        "PDF, DOCX yoki TXT fayl yuboring. Oddiy matn uchun matnni shu yerga "
        "yuboring. Faqat yuborgan ma’lumotingiz manba sifatida ishlatiladi."
    )


async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    if not context.user_data.get("awaiting_upload"):
        await update.effective_message.reply_text(
            "Avval /upload buyrug‘ini yuboring."
        )
        return

    document = update.effective_message.document
    if not document:
        return
    filename = document.file_name or "manba"
    if not filename.lower().endswith((".pdf", ".docx", ".txt")):
        await update.effective_message.reply_text(
            "Faqat PDF, DOCX yoki TXT fayllar qabul qilinadi."
        )
        return
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await update.effective_message.reply_text("Fayl 20 MB dan katta.")
        return

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        file_bytes = bytes(await telegram_file.download_as_bytearray())
        if len(file_bytes) > MAX_FILE_BYTES:
            raise ValueError("Fayl 20 MB dan katta.")
        content = extract_document(filename, file_bytes)
        source_id = storage.add_source(filename, content)
        context.user_data["awaiting_upload"] = False
        await update.effective_message.reply_text(
            f"Manba saqlandi: {filename} (ID: {source_id}).\n"
            "Savollar yaratish uchun /generate yoki /generate 10 buyrug‘ini yuboring."
        )
    except ValueError as error:
        await update.effective_message.reply_text(str(error))
    except Exception as error:
        logger.warning("Document processing failed: %s", type(error).__name__)
        await update.effective_message.reply_text(
            "Faylni qayta ishlashda xatolik yuz berdi. "
            "Faylni tekshirib, yana yuboring."
        )


async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    message = update.effective_message
    text = message.text or ""

    if context.user_data.get("editing_question_id"):
        await save_edited_question(update, context, text)
        return

    if context.user_data.get("metadata_source_id"):
        await save_source_metadata(update, context, text)
        return

    if context.user_data.get("awaiting_schedule"):
        await set_schedule_from_text(update, context, text)
        return

    if context.user_data.get("awaiting_upload"):
        try:
            content = prepare_plain_text(text)
            title = "Telegram matni"
            source_id = storage.add_source(title, content)
            context.user_data["awaiting_upload"] = False
            await message.reply_text(
                f"Matn manba sifatida saqlandi (ID: {source_id}).\n"
                "Savollar yaratish uchun /generate yoki /generate 10 buyrug‘ini yuboring."
            )
        except ValueError as error:
            await message.reply_text(str(error))
        return

    await message.reply_text("Yordam uchun /help buyrug‘ini yuboring.")


async def sources_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    sources = storage.list_sources()
    if not sources:
        await update.effective_message.reply_text(
            "Hozircha manba yo‘q. /upload orqali yuboring."
        )
        return
    lines = []
    for source in sources[:30]:
        status = "ma’lumot kiritilgan" if source.get("official_title") else "rasmiy ma’lumot yo‘q"
        lines.append(
            f"ID {source['id']}: {source['title']} — "
            f"{source_display_name(source, source['title'])} ({status})"
        )
    note = "\n\n(oxirgi 30 ta manba ko‘rsatildi)" if len(sources) > 30 else ""
    await update.effective_message.reply_text(
        "Manbalar:\n" + "\n".join(lines) + note
        + "\n\nRasmiy ma’lumot kiritish: /setmeta <ID>"
    )


async def setmeta_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Foydalanish: /setmeta <manba ID>. ID ni /sources orqali ko‘ring."
        )
        return
    source = storage.get_source(int(context.args[0]))
    if not source:
        await update.effective_message.reply_text("Bunday ID bilan manba topilmadi.")
        return
    context.user_data["metadata_source_id"] = source["id"]
    context.user_data.pop("awaiting_upload", None)
    context.user_data.pop("awaiting_schedule", None)
    context.user_data.pop("editing_question_id", None)
    await update.effective_message.reply_text(
        f"Manba ID {source['id']}: {source['title']}\n"
        f"Joriy ma’lumotlar:\n{format_metadata(source)}\n\n"
        "Rasmiy hujjat ma’lumotlarini quyidagi shaklda yuboring "
        "(bot ularni o‘zi to‘ldirmaydi):\n\n" + METADATA_TEMPLATE
    )


async def save_source_metadata(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    source_id = context.user_data.get("metadata_source_id")
    try:
        metadata = parse_source_metadata(text)
    except ValueError as error:
        await update.effective_message.reply_text(
            f"{error}\n\nNamuna:\n{METADATA_TEMPLATE}"
        )
        return
    if not storage.update_source_metadata(source_id, **metadata):
        context.user_data.pop("metadata_source_id", None)
        await update.effective_message.reply_text("Manba topilmadi; hech narsa saqlanmadi.")
        return
    context.user_data.pop("metadata_source_id", None)
    source = storage.get_source(source_id)
    await update.effective_message.reply_text(
        f"Saqlandi. Manba ID {source_id}:\n{format_metadata(source)}"
    )


async def generate_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    if len(context.args) > 1:
        await update.effective_message.reply_text("Foydalanish: /generate [son]")
        return
    try:
        count = int(context.args[0]) if context.args else 10
    except ValueError:
        await update.effective_message.reply_text("Savollar sonini raqam bilan kiriting.")
        return

    sources = storage.list_sources()
    progress = await update.effective_message.reply_text(
        "Manbalar bo‘limlarga ajratilmoqda. Hech bir bo‘lim tashlab ketilmaydi."
    )

    async def show_progress(processed: int, total: int, draft_count: int) -> None:
        if processed == 1 or processed % 5 == 0 or processed == total:
            try:
                await progress.edit_text(
                    f"Bo‘limlar: {processed}/{total} qayta ishlandi. "
                    f"Hozircha {draft_count} ta noyob savol tayyorlandi."
                )
            except TelegramError:
                logger.info("Could not update Telegram generation progress.")

    try:
        result = await generate_questions(
            openai_client,
            sources,
            count,
            existing_question_texts=storage.question_texts_for_dedupe(),
            progress_callback=show_progress,
        )
        questions = result["questions"]
        run_id, question_ids = storage.save_generation_collection(
            source_count=len(sources),
            sections_total=result["sections_total"],
            questions=questions,
            duplicates_removed=result["duplicates_removed"],
            skipped_items=result["unsuitable_items"],
        )
        type_names = {
            "question_answer": "savol-javob",
            "normative_legal": "normativ-huquqiy",
            "mixed": "aralash",
            "general": "umumiy",
        }
        type_summary = ", ".join(
            f"{type_names.get(name, name)}: {amount}"
            for name, amount in result["document_type_counts"].items()
        )
        delivery_failures = 0
        for card_number, question_id in enumerate(question_ids, start=1):
            try:
                await send_question_card(
                    update.effective_chat.id, context, question_id
                )
            except Exception as error:
                delivery_failures += 1
                logger.warning(
                    "Could not send review card #%s: %s",
                    question_id,
                    type(error).__name__,
                )
            if card_number % 10 == 0:
                try:
                    await progress.edit_text(
                        f"Bo‘limlar qayta ishlandi: "
                        f"{result['sections_processed']}/{result['sections_total']}. "
                        f"Ko‘rib chiqish kartalari yuborildi: "
                        f"{card_number}/{len(question_ids)}."
                    )
                except TelegramError:
                    logger.info("Could not update card-delivery progress.")
        delivery_note = (
            f"\nYuborilmagan kartalar: {delivery_failures}. "
            "Ularni /questions pending [sahifa] orqali ko‘ring."
            if delivery_failures
            else ""
        )
        unsuitable = result["unsuitable_items"]
        unsuitable_note = ""
        if unsuitable:
            examples = "\n".join(
                f"• {item['source_title']}, {item['source_reference']}: {item['reason']}"
                for item in unsuitable[:5]
            )
            more = f"\n… va yana {len(unsuitable) - 5} ta" if len(unsuitable) > 5 else ""
            unsuitable_note = (
                f"\nQuiz uchun yaroqsiz (o‘zgartirilmay saqlandi) asl savol-javoblar: "
                f"{len(unsuitable)}\n{examples}{more}"
            )
        try:
            await progress.edit_text(
                f"To‘plam #{run_id} SQLite’da saqlandi.\n"
                f"Bo‘limlar qayta ishlandi: "
                f"{result['sections_processed']}/{result['sections_total']}\n"
                f"Hujjat turlari: {type_summary}\n"
                f"Qoralama savollar: {len(questions)}\n"
                f"Yetarli dalil topilmagan bo‘limlar: "
                f"{result['sections_without_questions']}\n"
                f"Takroriy savollar olib tashlandi: {result['duplicates_removed']}\n"
                f"Tasdiqlashga tayyor qoralamalar: "
                f"{result['ready_for_approval']}\n"
                f"Manbasi tekshirilmagan qoralamalar: {result['source_unverified']}\n"
                f"NEEDS_REVIEW holatidagi qoralamalar: {result['needs_review']}"
                f"{unsuitable_note}\n\n"
                "Har bir savol administrator tomonidan tasdiqlanmaguncha "
                f"kanalga chiqmaydi.{delivery_note}"
            )
        except TelegramError:
            logger.info("Could not send the final generation summary.")
    except SectionProcessingError as error:
        await progress.edit_text(
            f"Qayta ishlash to‘xtadi: {error.sections_processed}/"
            f"{error.sections_total} bo‘lim tugallandi; "
            f"{error.section_number}-bo‘limda muammo yuz berdi.\n"
            f"Oldin tayyorlangan savol nomzodlari: {error.drafts_found} "
            "(to‘liq bo‘lmagan to‘plamda saqlanmadi).\n"
            f"Xato: {error.reason}\n"
            f"To‘liq bo‘lmagan to‘plam sifatida savollar saqlanmadi. "
            "Hech bir bo‘lim jimgina tashlab ketilmadi; manbani tekshirib "
            "qayta urinib ko‘ring."
        )
    except ValueError as error:
        await progress.edit_text(str(error))
    except Exception as error:
        logger.warning("Question generation failed: %s", type(error).__name__)
        await progress.edit_text(
            "Savollar yaratilmadi. OpenAI kaliti, internet aloqasi va "
            "manba matnini tekshirib, qayta urinib ko‘ring."
        )


async def send_question_card(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, question_id: int
) -> None:
    question = storage.get_question(question_id)
    if not question:
        return
    buttons = []
    if (
        question["source_verified"]
        and question.get("review_status") == "READY_FOR_APPROVAL"
    ):
        buttons.append(
            InlineKeyboardButton("Tasdiqlash", callback_data=f"approve:{question_id}")
        )
    buttons.extend(
        [
            InlineKeyboardButton("Tahrirlash", callback_data=f"edit:{question_id}"),
            InlineKeyboardButton("Rad etish", callback_data=f"reject:{question_id}"),
        ]
    )
    keyboard = InlineKeyboardMarkup([buttons])
    for attempt in range(4):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_question(question),
                reply_markup=keyboard,
            )
            return
        except RetryAfter as error:
            if attempt == 3:
                raise
            delay = error.retry_after
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            await asyncio.sleep(min(max(float(delay), 1.0), 30.0) + 0.1)


async def questions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    if len(context.args) > 2:
        await update.effective_message.reply_text(
            "Foydalanish: /questions [pending|approved|rejected|published] [sahifa]"
        )
        return
    status = context.args[0].lower() if context.args else "pending"
    allowed = {"pending", "approved", "rejected", "published"}
    if status not in allowed:
        await update.effective_message.reply_text(
            "Holat: pending, approved, rejected yoki published."
        )
        return
    try:
        page = int(context.args[1]) if len(context.args) == 2 else 1
    except ValueError:
        await update.effective_message.reply_text("Sahifa raqamini kiriting.")
        return
    if page < 1:
        await update.effective_message.reply_text("Sahifa raqami 1 yoki undan katta bo‘lsin.")
        return
    questions = storage.list_questions(
        status=status, limit=20, offset=(page - 1) * 20
    )
    if not questions:
        await update.effective_message.reply_text(
            f"'{status}' holatida savollar topilmadi."
        )
        return
    await update.effective_message.reply_text(
        f"{status} holatidagi {len(questions)} ta savol (sahifa {page}):"
    )
    for question in questions:
        if status == "pending":
            await send_question_card(
                update.effective_chat.id, context, question["id"]
            )
        else:
            await update.effective_message.reply_text(format_question(question))


async def question_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not await is_authorized_admin(update, context):
        return
    match = re.fullmatch(r"(approve|edit|reject):(\d+)", query.data or "")
    if not match:
        await query.answer()
        return
    action, raw_id = match.groups()
    question_id = int(raw_id)
    question = storage.get_question(question_id)
    if not question or question["status"] != "pending":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Bu savol endi ko‘rib chiqilmayapti.")
        return

    if action == "approve":
        if (
            not question["source_verified"]
            or question.get("review_status") != "READY_FOR_APPROVAL"
        ):
            await query.answer(
                "Savol tasdiqlashga tayyor emas. Avval manba va mazmunni tahrirlang.",
                show_alert=True,
            )
            return
        await query.answer()
        changed = storage.set_question_status(question_id, "approved")
        if changed:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"#{question_id} tasdiqlandi. U sozlangan navbatdagi vaqtda kanalga chiqadi."
            )
        return
    if action == "reject":
        await query.answer()
        changed = storage.set_question_status(question_id, "rejected")
        if changed:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"#{question_id} rad etildi.")
        return

    await query.answer()
    context.user_data["editing_question_id"] = question_id
    await query.message.reply_text(
        "Savolni quyidagi shaklda yuboring. A–D javoblari va to‘g‘ri javobni "
        "ko‘rsating. Manba va iqtibosni bo‘sh qoldirsangiz, avvalgisi saqlanadi:\n\n"
        "Savol: ...\n"
        "A) ...\n"
        "B) ...\n"
        "C) ...\n"
        "D) ...\n"
        "To‘g‘ri javob: A\n"
        "Manba: fayl nomi | manbadagi aniq havola\n"
        "Iqtibos: manbadan aynan olingan jumla"
    )


async def save_edited_question(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    question_id = context.user_data.get("editing_question_id")
    old_question = storage.get_question(question_id)
    if not old_question or old_question["status"] != "pending":
        context.user_data.pop("editing_question_id", None)
        await update.effective_message.reply_text(
            "Bu savol endi tahrirlanmaydi."
        )
        return
    try:
        edited = parse_question_edit(text, old_question)
        sources = storage.get_sources_by_ids(old_question["source_ids"])
        has_scope = (
            old_question.get("section_start") is not None
            and old_question.get("section_end") is not None
        )
        citation = check_citation(
            edited["source_title"],
            edited["source_reference"],
            edited["source_quote"],
            sources,
            section_text=None
            if has_scope
            else (old_question.get("source_excerpt") or None),
            scope=(old_question["section_start"], old_question["section_end"])
            if has_scope
            else None,
        )
        verified, review_note = citation.verified, citation.note
        if verified and citation.canonical_reference:
            edited["source_reference"] = citation.canonical_reference
        if len(edited["source_reference"]) > POLL_REFERENCE_MAX_CHARS:
            verified = False
            review_note = "Havola juda uzun; qisqaroq aniq huquqiy havolani kiriting."
        edited["quote_start"] = citation.quote_start
        edited["quote_end"] = citation.quote_end
        edited["legal_article"] = citation.article or ""
        edited["legal_part"] = citation.part or ""
        edited["legal_clause"] = citation.clause or ""
        edited["source_verified"] = verified
        edited["source_review_note"] = review_note
        edited["review_status"] = (
            "READY_FOR_APPROVAL" if verified else "NEEDS_REVIEW"
        )
        updated = storage.update_question(question_id, **edited)
        if not updated:
            raise ValueError("Savol holati o‘zgargan; tahrir saqlanmadi.")
        context.user_data.pop("editing_question_id", None)
        message = (
            "Tahrir saqlandi. Manba tekshirildi; savol hali tasdiqlanmagan:"
            if verified
            else "Tahrir saqlandi, ammo manba hali tekshirilmagan. "
            "Ushbu savol tasdiqlanmaydi. Havola yoki iqtibosni tuzating:"
        )
        await update.effective_message.reply_text(message)
        await send_question_card(
            update.effective_chat.id, context, question_id
        )
    except ValueError as error:
        await update.effective_message.reply_text(
            f"{error}\n\nSavolni yuqoridagi shaklda qayta yuboring."
        )


def apply_schedule(application: Application, daily_times: list[str]) -> None:
    for job in application.job_queue.get_jobs_by_name("quiz-publish"):
        job.schedule_removal()
    for time_text in daily_times:
        hour, minute = map(int, time_text.split(":"))
        application.job_queue.run_daily(
            publish_approved_question,
            time=time(hour=hour, minute=minute, tzinfo=TIMEZONE),
            name="quiz-publish",
        )


async def set_schedule_from_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    try:
        daily_times = parse_schedule(text)
        storage.save_schedule(daily_times)
        apply_schedule(context.application, daily_times)
        context.user_data["awaiting_schedule"] = False
        await update.effective_message.reply_text(
            f"Jadval saqlandi: kuniga {len(daily_times)} ta savol, "
            f"{', '.join(daily_times)} ({TIMEZONE.key}). "
            "Har bir vaqtda bittadan tasdiqlangan savol nashr qilinadi."
        )
    except ValueError as error:
        await update.effective_message.reply_text(
            f"{error}\nMasalan: 2 09:00,18:00"
        )


async def schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    if context.args:
        await set_schedule_from_text(
            update, context, " ".join(context.args)
        )
        return

    times = storage.get_schedule()
    current = (
        f"Joriy jadval: kuniga {len(times)} ta, vaqtlar: {', '.join(times)} "
        f"({TIMEZONE.key})."
        if times
        else "Hozircha nashr jadvali o‘rnatilmagan."
    )
    context.user_data["awaiting_schedule"] = True
    context.user_data.pop("awaiting_upload", None)
    context.user_data.pop("editing_question_id", None)
    context.user_data.pop("metadata_source_id", None)
    await update.effective_message.reply_text(
        f"{current}\nYangi jadvalni yuboring: kunlik_son HH:MM,HH:MM\n"
        "Masalan: 2 09:00,18:00\n"
        "Har bir vaqtga bittadan savol to‘g‘ri keladi. Vaqtlar Asia/Tashkent bo‘yicha."
    )


async def publish_approved_question(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    question = storage.next_approved_question()
    if not question:
        logger.info("No approved questions are waiting to be published.")
        return
    sources = storage.get_sources_by_ids(question.get("source_ids") or [])
    explanation = build_poll_explanation(question, sources[0] if sources else None)
    try:
        poll_message = await context.bot.send_poll(
            chat_id=CHANNEL_ID,
            question=question["question"],
            options=question["options"],
            type=PollType.QUIZ,
            correct_option_id=question["correct_index"],
            explanation=explanation,
            is_anonymous=True,
        )
        storage.mark_question_published(question["id"], poll_message.message_id)
        logger.info("Published approved question #%s.", question["id"])
    except Exception as error:
        logger.warning(
            "Could not publish approved question #%s: %s",
            question["id"],
            type(error).__name__,
        )


async def startup(application: Application) -> None:
    times = storage.get_schedule()
    if times:
        apply_schedule(application, times)
        logger.info("Loaded %s daily publication times.", len(times))
    else:
        logger.info("No publication schedule is set. Use /schedule in the bot.")


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    error_name = type(context.error).__name__ if context.error else "UnknownError"
    logger.error("Unhandled bot error (%s).", error_name)
    if (
        isinstance(update, Update)
        and update.effective_message
        and update.effective_chat
        and update.effective_chat.type == ChatType.PRIVATE
    ):
        try:
            await update.effective_message.reply_text(
                "Xatolik yuz berdi. Birozdan so‘ng qayta urinib ko‘ring."
            )
        except Exception:
            pass


COMMANDS = {
    "start": start_command,
    "help": help_command,
    "upload": upload_command,
    "generate": generate_command,
    "questions": questions_command,
    "sources": sources_command,
    "setmeta": setmeta_command,
    "schedule": schedule_command,
}
# Updates the bot asks Telegram for: no channel posts, edits or group events.
ALLOWED_UPDATES = [Update.MESSAGE, Update.CALLBACK_QUERY]


def register_handlers(application: Application) -> None:
    for name, callback in COMMANDS.items():
        application.add_handler(
            CommandHandler(name, callback, filters=PRIVATE_MESSAGES)
        )
    application.add_handler(
        CallbackQueryHandler(
            question_action, pattern=r"^(approve|edit|reject):\d+$"
        )
    )
    application.add_handler(
        MessageHandler(PRIVATE_MESSAGES & filters.Document.ALL, handle_document)
    )
    application.add_handler(
        MessageHandler(
            PRIVATE_MESSAGES & filters.TEXT & ~filters.COMMAND, handle_text
        )
    )
    application.add_error_handler(error_handler)


def main() -> None:
    storage.initialize_database()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(startup)
        .build()
    )
    register_handlers(application)
    logger.info("Starting the Telegram Quiz Bot.")
    application.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
