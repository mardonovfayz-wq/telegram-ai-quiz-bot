"""Telegram bot entry point.

All credentials and identifiers come from environment variables (Replit
Secrets or the host's environment); none are stored in tracked files.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, time
from types import SimpleNamespace
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

import daily
import pipeline
import publishing
import storage
from hook import start_hook_server
from notify import AdminNotifier
from bot_helpers import (
    build_poll_explanation,
    format_metadata,
    format_question as _format_question,
    parse_admin_id,
    parse_channel_id,
    parse_generate_args,
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
# Daily draft generation time (local TIMEZONE). The generation itself is off
# until the administrator runs /autogen on.
DAILY_GENERATION_TIME = os.getenv("DAILY_GENERATION_TIME", "09:00").strip()
# Optional HTTP hook for Activepieces; disabled unless HOOK_TOKEN is set.
HOOK_TOKEN = os.getenv("HOOK_TOKEN", "").strip()
HOOK_HOST = os.getenv("HOOK_HOST", "127.0.0.1").strip()
HOOK_PORT = int(os.getenv("HOOK_PORT", "8080") or 8080)

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
    "/generate [son] — keyingi ishlatilmagan bo‘limlardan savollar (standart: 10)\n"
    "/generate full [son] — barcha bo‘limlarni qaytadan ko‘rib chiqish (eski usul)\n"
    "/coverage — har bir manbaning qaysi bo‘limlari ishlatilgani\n"
    "/recheck — kutayotgan va tasdiqlangan savollar havolalarini qayta tekshirish (hech narsa o‘zgarmaydi)\n"
    "/questions [holat] [sahifa] — savollarni ko‘rish\n"
    "/sources — manbalar ro‘yxati va huquqiy hujjat ma’lumotlari\n"
    "/setmeta <ID> — manbaga rasmiy nom, raqam, sana va havola kiritish\n"
    "/schedule — nashr vaqtlarini ko‘rish yoki sozlash\n"
    "/autogen — kunlik 09:00 generatsiya: holat, on, off, run, trigger\n"
    "/queue — nashr navbati holati\n"
    "/audit [son] yoki /audit #ID — audit jurnali\n"
    "/unapprove <ID> — tasdiqni bekor qilib, qayta ko‘rib chiqishga qaytarish\n"
    "/resolve <ID> published|approved — holati noma’lum nashrni hal qilish\n"
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
        existing = storage.find_source_by_content(content)
        if existing:
            context.user_data["awaiting_upload"] = False
            await update.effective_message.reply_text(
                f"Bu matn allaqachon yuklangan: {existing['title']} "
                f"(ID: {existing['id']}). Qayta saqlanmadi."
            )
            return
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
            existing = storage.find_source_by_content(content)
            if existing:
                context.user_data["awaiting_upload"] = False
                await message.reply_text(
                    f"Bu matn allaqachon yuklangan (ID: {existing['id']}). "
                    "Qayta saqlanmadi."
                )
                return
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


TELEGRAM_TEXT_LIMIT = 4000


def _fit_message(text: str) -> str:
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    return text[: TELEGRAM_TEXT_LIMIT - 40].rstrip() + "\n… (xabar qisqartirildi)"


async def generate_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Runs in the background (block=False) so other commands keep working."""
    if not await is_authorized_admin(update, context):
        return
    try:
        mode, count = parse_generate_args(context.args)
    except ValueError as error:
        await update.effective_message.reply_text(str(error))
        return
    lock = context.application.bot_data.setdefault("generation_lock", asyncio.Lock())
    if lock.locked():
        await update.effective_message.reply_text(
            "Generatsiya allaqachon ishlayapti. U tugagach qayta urinib ko‘ring."
        )
        return
    async with lock:
        if mode == "full":
            await _run_full_generation(update, context, count)
        else:
            await _run_incremental_generation(update, context, count)


async def _send_review_cards(
    update: Update, context: ContextTypes.DEFAULT_TYPE, question_ids: list[int]
) -> int:
    failures = 0
    for question_id in question_ids:
        try:
            await send_question_card(update.effective_chat.id, context, question_id)
        except Exception as error:
            failures += 1
            logger.warning(
                "Could not send review card #%s: %s", question_id, type(error).__name__
            )
    return failures


async def _run_incremental_generation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, count: int
) -> None:
    progress = await update.effective_message.reply_text(
        f"Keyingi ishlatilmagan bo‘limlardan {count} ta savol tayyorlanmoqda…"
    )

    async def show_progress(processed: int, total: int, draft_count: int) -> None:
        try:
            await progress.edit_text(
                f"Bo‘limlar: {processed} ta ko‘rildi (navbatda {total} ta). "
                f"Tayyor qoralamalar: {draft_count}/{count}."
            )
        except TelegramError:
            logger.info("Could not update Telegram generation progress.")

    try:
        result = await pipeline.run_incremental_generation(
            openai_client, count, progress_callback=show_progress
        )
    except ValueError as error:
        await progress.edit_text(str(error))
        return
    except Exception as error:
        logger.warning("Incremental generation failed: %s", type(error).__name__)
        await progress.edit_text(
            "Savollar yaratilmadi. Manba matni yoki tarmoqni tekshirib, qayta urinib ko‘ring."
        )
        return
    if result["nothing_left"]:
        await progress.edit_text(
            "Barcha bo‘limlar allaqachon ishlatilgan. Yangi manba yuklang "
            "yoki /coverage orqali holatni ko‘ring."
        )
        return

    delivery_failures = await _send_review_cards(update, context, result["question_ids"])
    lines = [
        f"To‘plam #{result['run_id']} saqlandi (bosqichma-bosqich rejim).",
        f"Qoralamalar: {len(result['questions'])}/{count}",
        f"Ko‘rilgan bo‘limlar: {result['sections_processed']}",
        f"Tasdiqlashga tayyor: {result['ready_for_approval']}",
        f"NEEDS_REVIEW: {result['needs_review']}",
        f"Takroriy savollar olib tashlandi: {result['duplicates_removed']}",
        f"Hali ishlatilmagan bo‘limlar: {result['sections_remaining']}",
    ]
    if result["unsuitable_count"]:
        lines.append(
            "Quiz uchun yaroqsiz (o‘zgartirilmay saqlangan) asl savol-javoblar: "
            f"{result['unsuitable_count']}"
        )
    if result["sections_failed"]:
        lines.append(
            f"Xato bergan bo‘limlar: {result['sections_failed']} "
            "(keyingi /generate da qayta urinib ko‘riladi). "
            f"Sabab: {result['failed_reasons'][0]}"
        )
    if result["stopped_early"]:
        lines.append(
            "Ketma-ket xatolar sababli jarayon erta to‘xtadi; tayyor qoralamalar saqlandi."
        )
    if delivery_failures:
        lines.append(
            f"Yuborilmagan kartalar: {delivery_failures}. /questions pending orqali ko‘ring."
        )
    lines.append(
        "\nHar bir savol administrator tasdiqlamaguncha kanalga chiqmaydi."
    )
    try:
        await progress.edit_text(_fit_message("\n".join(lines)))
    except TelegramError:
        logger.info("Could not send the final generation summary.")


async def coverage_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    try:
        pipeline.eligible_sections()  # registers sections of new sources
    except Exception as error:
        logger.warning("Coverage sync failed: %s", type(error).__name__)
    summary = storage.coverage_summary()
    if not summary:
        await update.effective_message.reply_text("Hozircha manba yo‘q.")
        return
    lines = []
    for item in summary[:20]:
        name = item["official_title"] or item["title"]
        lines.append(
            f"ID {item['source_id']}: {name}\n"
            f"  jami {item['total']} · ishlatilgan {item['used']} · "
            f"savolsiz {item['empty']} · yaroqsiz Q&A {item['unsuitable']} · "
            f"xato {item['failed']} (shundan yakuniy {item['failed_final']}) · "
            f"navbatda {item['unused']}"
        )
    note = "\n(birinchi 20 ta manba ko‘rsatildi)" if len(summary) > 20 else ""
    await update.effective_message.reply_text(
        _fit_message("Manbalar qamrovi:\n" + "\n".join(lines) + note)
    )


async def recheck_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    report = pipeline.recheck_questions()
    header = (
        f"Qayta tekshiruv (bazada hech narsa o‘zgartirilmadi):\n"
        f"Tekshirildi: {report['checked']} · mos: {report['ok']} · "
        f"muammoli: {len(report['problems'])}"
    )
    if not report["problems"]:
        await update.effective_message.reply_text(header)
        return
    details = "\n".join(
        f"#{item['id']} ({item['status']}) {item['reference']}: {item['note']}"
        for item in report["problems"][:15]
    )
    more = (
        f"\n… va yana {len(report['problems']) - 15} ta"
        if len(report["problems"]) > 15
        else ""
    )
    await update.effective_message.reply_text(
        _fit_message(f"{header}\n\n{details}{more}\n\n"
        "Muammoli savollarni ko‘rib chiqing; tasdiqlanganlarini nashrdan oldin "
        "tekshirish tavsiya etiladi.")
    )


async def _run_full_generation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, count: int
) -> None:
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
            "Foydalanish: /questions [pending|approved|rejected|publishing|published|held] [sahifa]"
        )
        return
    status = context.args[0].lower() if context.args else "pending"
    allowed = set(storage.QUESTION_STATUSES)
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
        changed = storage.set_question_status(
            question_id, "approved", actor=admin_actor(update)
        )
        if changed:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"#{question_id} tasdiqlandi. U sozlangan navbatdagi vaqtda kanalga chiqadi."
            )
        return
    if action == "reject":
        await query.answer()
        changed = storage.set_question_status(
            question_id, "rejected", actor=admin_actor(update)
        )
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
        updated = storage.update_question(
            question_id, **edited, actor=admin_actor(update)
        )
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


def admin_actor(update: Update) -> str:
    user = update.effective_user
    return f"admin:{user.id}" if user else "admin"


def today_string() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def get_notifier(application: Application) -> AdminNotifier:
    notifier = application.bot_data.get("notifier")
    if notifier is None:

        async def send(text: str) -> None:
            await application.bot.send_message(chat_id=ADMIN_ID, text=text)

        notifier = AdminNotifier(send)
        application.bot_data["notifier"] = notifier
    return notifier


def generation_lock(application: Application) -> asyncio.Lock:
    return application.bot_data.setdefault("generation_lock", asyncio.Lock())


async def publish_approved_question(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Scheduled slot: publish one approved question, never the same one twice."""
    lock = context.application.bot_data.setdefault("publish_lock", asyncio.Lock())
    notifier = get_notifier(context.application)

    async def send_poll(question: dict) -> int:
        sources = storage.get_sources_by_ids(question.get("source_ids") or [])
        explanation = build_poll_explanation(question, sources[0] if sources else None)
        message = await context.bot.send_poll(
            chat_id=CHANNEL_ID,
            question=question["question"],
            options=question["options"],
            type=PollType.QUIZ,
            correct_option_id=question["correct_index"],
            explanation=explanation,
            is_anonymous=True,
        )
        return message.message_id

    async with lock:
        outcome = await publishing.publish_next(
            send_poll, notifier.notify, actor="scheduler", today=today_string()
        )
    logger.info("Publish slot finished: %s", outcome["status"])


async def run_daily(
    application: Application, trigger: str, manual: bool = False, actor: str | None = None
) -> dict:
    """Daily generation (scheduled, Activepieces or manual) with notifications."""
    lock = generation_lock(application)
    notifier = get_notifier(application)
    if lock.locked():
        if manual or trigger == "activepieces":
            await notifier.notify("Generatsiya allaqachon ishlayapti; yangi ishga tushirilmadi.")
        return {"status": "busy"}
    async with lock:
        outcome = await daily.run_daily_generation(
            openai_client, trigger, today_string(), manual=manual, actor=actor
        )
    if outcome["status"] in {"disabled", "other_trigger"} and not manual:
        logger.info("Daily generation skipped: %s", outcome["status"])
        return outcome
    result = outcome.get("result") or {}
    if result.get("question_ids"):
        holder = SimpleNamespace(bot=application.bot)
        for question_id in result["question_ids"]:
            try:
                await send_question_card(ADMIN_ID, holder, question_id)
            except Exception as error:
                logger.warning("Could not send review card: %s", type(error).__name__)
    heading = "Qo‘lda ishga tushirilgan generatsiya" if manual else "Kunlik generatsiya (09:00)"
    await notifier.notify(daily.daily_summary_text(outcome, heading))
    for warning in daily.queue_warnings(len(storage.get_schedule())):
        await notifier.notify(warning, key=f"queue:{warning[:40]}:{today_string()}")
    return outcome


async def daily_generation_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_daily(context.application, "internal")


def _parse_hhmm(text: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in text.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(text)
    return hour, minute


async def autogen_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    actor = admin_actor(update)
    action = context.args[0].lower() if context.args else "status"
    if action == "on":
        daily.set_enabled(True, actor)
    elif action == "off":
        daily.set_enabled(False, actor)
    elif action == "trigger":
        try:
            daily.set_trigger(context.args[1].lower() if len(context.args) > 1 else "", actor)
        except ValueError as error:
            await update.effective_message.reply_text(str(error))
            return
    elif action == "run":
        context.application.create_task(
            run_daily(context.application, "manual", manual=True, actor=actor)
        )
        await update.effective_message.reply_text(
            "10 ta qoralama tayyorlash boshlandi (qo‘lda sinov). Natija shu chatga keladi; "
            "hech narsa avtomatik e’lon qilinmaydi."
        )
        return
    elif action != "status":
        await update.effective_message.reply_text(
            "Foydalanish: /autogen [status|on|off|run|trigger internal|trigger activepieces]"
        )
        return
    settings = daily.automation_settings()
    today = storage.get_daily_run(today_string())
    today_line = (
        f"Bugungi ishga tushirish: {today['status']} ({today['trigger']})"
        if today
        else "Bugun hali ishga tushirilmagan."
    )
    await update.effective_message.reply_text(
        f"Kunlik generatsiya: {'YOQILGAN' if settings['enabled'] else 'o‘chiq'}\n"
        f"Vaqt: har kuni {DAILY_GENERATION_TIME} ({TIMEZONE.key}), {daily.DAILY_TARGET} ta qoralama\n"
        f"Ishga tushiruvchi: {settings['trigger']} "
        f"({'bot ichidagi jadval' if settings['trigger'] == 'internal' else 'Activepieces HTTP hook'})\n"
        f"HTTP hook: {'sozlangan' if HOOK_TOKEN else 'o‘chiq (HOOK_TOKEN yo‘q)'}\n"
        f"{today_line}\n\n"
        "Qoralamalar faqat sizning tasdig‘ingizdan keyin kanalga chiqadi."
    )


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized_admin(update, context):
        return
    counts = storage.count_questions_by_status()
    lines = [f"{status}: {amount}" for status, amount in counts.items()]
    stuck = storage.list_questions("publishing", limit=10)
    if stuck:
        lines.append(
            "Holati noma’lum: "
            + ", ".join(f"#{item['id']} ({item['last_publish_error'] or '—'})" for item in stuck)
        )
    warnings = daily.queue_warnings(len(storage.get_schedule()))
    text = "Savollar holati:\n" + "\n".join(lines)
    if warnings:
        text += "\n\nOgohlantirishlar:\n" + "\n".join(f"• {item}" for item in warnings)
    await update.effective_message.reply_text(_fit_message(text))


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized_admin(update, context):
        return
    question_id = None
    limit = 15
    if context.args:
        argument = context.args[0]
        if argument.startswith("#") and argument[1:].isdigit():
            question_id = int(argument[1:])
            limit = 30
        elif argument.isdigit():
            limit = min(int(argument), 50)
        else:
            await update.effective_message.reply_text("Foydalanish: /audit [son] yoki /audit #ID")
            return
    entries = storage.list_audit(limit=limit, question_id=question_id)
    if not entries:
        await update.effective_message.reply_text("Audit jurnalida yozuv yo‘q.")
        return
    lines = []
    for entry in entries:
        target = f" #{entry['question_id']}" if entry["question_id"] else ""
        extra = ""
        details = entry["details"]
        if details.get("message_id"):
            extra = f" (xabar {details['message_id']})"
        elif details.get("reason"):
            extra = f" ({str(details['reason'])[:80]})"
        elif details.get("key"):
            extra = f" ({details['key']}={details.get('value')})"
        lines.append(f"{entry['created_at'][:19]} · {entry['actor']} · {entry['action']}{target}{extra}")
    await update.effective_message.reply_text(_fit_message("Audit jurnali:\n" + "\n".join(lines)))


async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized_admin(update, context):
        return
    if len(context.args) < 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Foydalanish: /unapprove <ID> [sabab]")
        return
    question_id = int(context.args[0])
    reason = " ".join(context.args[1:])
    if storage.unapprove_question(question_id, admin_actor(update), reason):
        await update.effective_message.reply_text(
            f"#{question_id} qayta ko‘rib chiqishga (pending) qaytarildi."
        )
        await send_question_card(update.effective_chat.id, context, question_id)
    else:
        await update.effective_message.reply_text(
            "Faqat 'approved' yoki 'held' holatidagi savolni qaytarish mumkin."
        )


async def resolve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized_admin(update, context):
        return
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or args[1] not in {"published", "approved"}:
        await update.effective_message.reply_text(
            "Foydalanish: /resolve <ID> published [xabar_ID] yoki /resolve <ID> approved\n"
            "published — savol kanalda chiqqan; approved — chiqmagan, navbatga qaytarish."
        )
        return
    message_id = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
    if storage.resolve_publishing(int(args[0]), args[1], admin_actor(update), message_id):
        await update.effective_message.reply_text(f"#{args[0]} holati: {args[1]}.")
    else:
        await update.effective_message.reply_text(
            "Bu savol 'publishing' holatida emas; hech narsa o‘zgarmadi."
        )


async def _handle_daily_hook() -> dict:
    """Called by the HTTP hook (Activepieces). Starts the run in the background."""
    application = _APPLICATION_REF["app"]
    settings = daily.automation_settings()
    if not settings["enabled"]:
        return {"accepted": False, "reason": "disabled"}
    if settings["trigger"] != "activepieces":
        return {"accepted": False, "reason": "trigger_is_internal"}
    if storage.get_daily_run(today_string()):
        return {"accepted": False, "reason": "already_ran_today"}
    if generation_lock(application).locked():
        return {"accepted": False, "reason": "busy"}
    application.create_task(run_daily(application, "activepieces"))
    return {"accepted": True}


_APPLICATION_REF: dict = {}


async def startup(application: Application) -> None:
    _APPLICATION_REF["app"] = application
    times = storage.get_schedule()
    if times:
        apply_schedule(application, times)
        logger.info("Loaded %s daily publication times.", len(times))
    else:
        logger.info("No publication schedule is set. Use /schedule in the bot.")
    hour, minute = _parse_hhmm(DAILY_GENERATION_TIME)
    application.job_queue.run_daily(
        daily_generation_job,
        time=time(hour=hour, minute=minute, tzinfo=TIMEZONE),
        name="daily-generation",
    )
    settings = daily.automation_settings()
    logger.info(
        "Daily generation at %s: %s (trigger %s).",
        DAILY_GENERATION_TIME,
        "enabled" if settings["enabled"] else "disabled",
        settings["trigger"],
    )
    if HOOK_TOKEN:
        application.bot_data["hook_server"] = await start_hook_server(
            HOOK_HOST, HOOK_PORT, HOOK_TOKEN, _handle_daily_hook
        )


async def shutdown(application: Application) -> None:
    server = application.bot_data.get("hook_server")
    if server is not None:
        server.close()
        await server.wait_closed()


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    error_name = type(context.error).__name__ if context.error else "UnknownError"
    logger.error("Unhandled bot error (%s).", error_name)
    await get_notifier(context.application).notify(
        f"Botda xatolik: {error_name}. Tafsilotlar server logida.",
        key=f"error:{error_name}",
    )
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
    "coverage": coverage_command,
    "recheck": recheck_command,
    "questions": questions_command,
    "sources": sources_command,
    "setmeta": setmeta_command,
    "schedule": schedule_command,
    "autogen": autogen_command,
    "queue": queue_command,
    "audit": audit_command,
    "unapprove": unapprove_command,
    "resolve": resolve_command,
}
# Updates the bot asks Telegram for: no channel posts, edits or group events.
ALLOWED_UPDATES = [Update.MESSAGE, Update.CALLBACK_QUERY]


def register_handlers(application: Application) -> None:
    for name, callback in COMMANDS.items():
        application.add_handler(
            CommandHandler(
                name,
                callback,
                filters=PRIVATE_MESSAGES,
                # Generation runs in the background so the bot stays responsive.
                block=name != "generate",
            )
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
        .post_shutdown(shutdown)
        .build()
    )
    register_handlers(application)
    logger.info("Starting the Telegram Quiz Bot.")
    application.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
