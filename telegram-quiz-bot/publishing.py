"""Duplicate-safe publishing of approved questions (no Telegram import).

Life cycle: approved -> publishing -> published.
- A question is claimed atomically before sending, so it can be sent once only.
- If Telegram certainly rejected the send, it returns to "approved".
- If the result is unknown (timeout, network error), it stays in "publishing"
  and is never retried automatically; the administrator checks the channel and
  runs /resolve. This prevents a second copy of the same quiz.
- An approved question that duplicates an already published one is "held".
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import storage
from quiz_generator import is_near_duplicate

# Telegram errors after which the poll was certainly not posted.
_NOT_SENT_ERRORS = {"BadRequest", "Forbidden", "ChatMigrated", "RetryAfter", "InvalidToken"}
MAX_HOLDS_PER_SLOT = 5


def classify_publish_error(error: BaseException) -> str:
    """'not_sent' when Telegram refused the request, otherwise 'uncertain'."""
    names = {cls.__name__ for cls in type(error).__mro__}
    if names & {"TimedOut", "NetworkError"}:
        return "uncertain"
    if names & _NOT_SENT_ERRORS:
        return "not_sent"
    return "uncertain"


async def publish_next(
    send_poll: Callable[[dict[str, Any]], Awaitable[int]],
    notify: Callable[[str, str | None], Awaitable[Any]],
    actor: str = "scheduler",
    today: str = "",
) -> dict[str, Any]:
    """Publish the oldest approved question once. Returns what happened."""
    stuck = storage.list_questions("publishing", limit=5)
    if stuck:
        ids = ", ".join(f"#{item['id']}" for item in stuck)
        await notify(
            f"Diqqat: {ids} nashr holati noma’lum (publishing). Kanalni tekshiring va "
            "/resolve <ID> published yoki /resolve <ID> approved yuboring.",
            f"stuck:{ids}",
        )

    published = storage.published_question_texts()
    published_texts = [text for _id, text in published]
    for _ in range(MAX_HOLDS_PER_SLOT):
        question = storage.next_approved_question()
        if question is None:
            await notify(
                "Nashr navbati bo‘sh: tasdiqlangan savol yo‘q, bu vaqtda hech narsa "
                "e’lon qilinmadi.",
                f"queue-empty:{today}",
            )
            return {"status": "empty"}
        if is_near_duplicate(question["question"], published_texts):
            twin = next(
                (qid for qid, text in published if is_near_duplicate(question["question"], [text])),
                None,
            )
            reason = f"Avval e’lon qilingan #{twin} savolga juda o‘xshash."
            storage.hold_question(question["id"], reason, actor=actor)
            await notify(
                f"#{question['id']} e’lon qilinmadi: {reason} U 'held' holatiga o‘tkazildi; "
                "kerak bo‘lsa /unapprove orqali qayta ko‘rib chiqing.",
                f"held:{question['id']}",
            )
            continue
        if not storage.claim_for_publishing(question["id"], actor=actor):
            continue  # another job claimed it first
        try:
            message_id = await send_poll(question)
        except Exception as error:
            kind = classify_publish_error(error)
            error_text = type(error).__name__
            if kind == "not_sent":
                storage.release_failed_publish(question["id"], error_text, actor=actor)
                await notify(
                    f"#{question['id']} e’lon qilinmadi (Telegram rad etdi: {error_text}). "
                    "Savol navbatga qaytarildi. Bot kanalda admin ekanini tekshiring.",
                    f"publish-failed:{error_text}",
                )
                return {"status": "failed", "question_id": question["id"], "error": error_text}
            storage.note_uncertain_publish(question["id"], error_text, actor=actor)
            await notify(
                f"#{question['id']} yuborilishi noaniq ({error_text}). Takroriy post "
                "bo‘lmasligi uchun avtomatik qayta yuborilmaydi. Kanalni tekshiring: "
                f"chiqqan bo‘lsa /resolve {question['id']} published, chiqmagan bo‘lsa "
                f"/resolve {question['id']} approved.",
                None,
            )
            return {"status": "uncertain", "question_id": question["id"], "error": error_text}
        storage.mark_question_published(question["id"], message_id, actor=actor)
        remaining = storage.count_questions_by_status()["approved"]
        if remaining == 0:
            await notify(
                "Navbatdagi oxirgi tasdiqlangan savol e’lon qilindi; navbat endi bo‘sh.",
                f"queue-drained:{today}",
            )
        return {"status": "published", "question_id": question["id"], "message_id": message_id}
    return {"status": "held_only"}
