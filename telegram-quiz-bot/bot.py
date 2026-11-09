"""Telegram bot entry point. Configure all credentials with Replit Secrets."""

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
from quiz_generator import (
    MAX_FILE_BYTES,
    SectionProcessingError,
    extract_document,
    generate_questions,
    prepare_plain_text,
    verify_grounding,
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
ADMIN_ID = int(required_environment("ADMIN_ID"))
CHANNEL_ID = required_environment("CHANNEL_ID")
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
    "/generate [son] — manbalardan savollar yaratish (standart: 5)\n"
    "/questions [holat] [sahifa] — savollarni ko‘rish\n"
    "/schedule — nashr vaqtlarini ko‘rish yoki sozlash\n"
    "/help — yordam\n\n"
    "Yangi savollar avtomatik nashr qilinmaydi: har birini avval tasdiqlang."
)


async def is_authorized_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
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

    chat = update.effective_chat
    if chat and chat.type != ChatType.PRIVATE:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Boshqaruv buyruqlarini bot bilan shaxsiy chatda yuboring."
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
            "Savollar yaratish uchun /generate yoki /generate 5 buyrug‘ini yuboring."
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
                "Savollar yaratish uchun /generate yoki /generate 5 buyrug‘ini yuboring."
            )
        except ValueError as error:
            await message.reply_text(str(error))
        return

    await message.reply_text("Yordam uchun /help buyrug‘ini yuboring.")


async def generate_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await is_authorized_admin(update, context):
        return
    if len(context.args) > 1:
        await update.effective_message.reply_text("Foydalanish: /generate [son]")
        return
    try:
        count = int(context.args[0]) if context.args else 5
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
            "Ularni /questions pending orqali ko‘ring."
            if delivery_failures
            else ""
        )
        try:
            await progress.edit_text(
                f"To‘plam #{run_id} SQLite’da saqlandi.\n"
                f"Bo‘limlar qayta ishlandi: "
                f"{result['sections_processed']}/{result['sections_total']}\n"
                f"Qoralama savollar: {len(questions)}\n"
                f"Yetarli dalil topilmagan bo‘limlar: "
                f"{result['sections_without_questions']}\n"
                f"Takroriy savollar olib tashlandi: {result['duplicates_removed']}\n"
                f"Manbasi tekshirilmagan qoralamalar: {result['source_unverified']}\n\n"
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


def format_question(question: dict) -> str:
    letters = ("A", "B", "C", "D")
    options = "\n".join(
        f"{letters[index]}) {option}" for index, option in enumerate(question["options"])
    )
    correct = letters[question["correct_index"]]
    verification_note = ""
    if not question["source_verified"]:
        verification_note = (
            "\n\nOGOHLANTIRISH: MANBA TEKSHIRILMAGAN\n"
            f"{question['source_review_note'] or 'Havola va iqtibosni tekshiring.'}"
        )
    return (
        f"#{question['id']} · Holat: {question['status']}\n\n"
        f"{question['question']}\n\n{options}\n\n"
        f"To‘g‘ri javob: {correct}) {question['options'][question['correct_index']]}\n"
        f"Manba: {question['source_title']}, {question['source_reference']}\n"
        f"Asos: {question['source_quote']}{verification_note}"
    )


async def send_question_card(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, question_id: int
) -> None:
    question = storage.get_question(question_id)
    if not question:
        return
    buttons = []
    if question["source_verified"]:
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
        if not question["source_verified"]:
            await query.answer(
                "Manba hali tekshirilmagan. Avval havola va iqtibosni tahrirlang.",
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

    letters = ("A", "B", "C", "D")
    question = fields.get("question", "").strip()
    choices = [options.get(letter, "") for letter in letters]
    correct_letter = fields.get("correct", "").strip().upper()
    if not question or len(question) > 300:
        raise ValueError("Savol matnini kiriting (300 belgigacha).")
    if any(not choice or len(choice) > 100 for choice in choices):
        raise ValueError("A), B), C), D) javoblarining barchasini kiriting (100 belgigacha).")
    if len({choice.casefold() for choice in choices}) != 4:
        raise ValueError("To‘rtta javob varianti bir-biridan farq qilishi kerak.")
    if correct_letter not in letters:
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
        "correct_index": letters.index(correct_letter),
        "source_title": source_title,
        "source_reference": source_reference,
        "source_quote": source_quote,
    }


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
        verified, review_note = verify_grounding(
            edited["source_title"],
            edited["source_reference"],
            edited["source_quote"],
            sources,
        )
        if len(edited["source_reference"]) > 190:
            verified = False
            review_note = "Havola juda uzun; qisqaroq aniq huquqiy havolani kiriting."
        edited["source_verified"] = verified
        edited["source_review_note"] = review_note
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
    explanation = f"Manba: {question['source_title']}, {question['source_reference']}"
    if len(explanation) > 200:
        explanation = f"Manba: {question['source_reference']}"
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
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Xatolik yuz berdi. Birozdan so‘ng qayta urinib ko‘ring."
            )
        except Exception:
            pass


def main() -> None:
    storage.initialize_database()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(startup)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CommandHandler("generate", generate_command))
    application.add_handler(CommandHandler("questions", questions_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(
        CallbackQueryHandler(
            question_action, pattern=r"^(approve|edit|reject):\d+$"
        )
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_error_handler(error_handler)
    logger.info("Starting the Telegram Quiz Bot.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
