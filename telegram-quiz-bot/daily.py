"""Daily 09:00 draft generation (no Telegram import).

Off by default. When enabled, it creates up to 10 drafts once per day from the
next unused sections. It never pads the number: if fewer good drafts can be
made, it reports the shortfall. Drafts still need administrator approval.
"""

from __future__ import annotations

import random
from typing import Any

import pipeline
import storage
from quiz_generator import SECTION_CHARS, describe_section_error

DAILY_TARGET = 10
AUTO_SETTING = "auto_generation_enabled"
TRIGGER_SETTING = "daily_trigger"
TRIGGERS = ("internal", "activepieces")


def automation_settings() -> dict[str, Any]:
    trigger = storage.get_setting(TRIGGER_SETTING, "internal")
    return {
        "enabled": storage.get_setting(AUTO_SETTING, "0") == "1",
        "trigger": trigger if trigger in TRIGGERS else "internal",
    }


def set_enabled(enabled: bool, actor: str) -> None:
    storage.set_setting(AUTO_SETTING, "1" if enabled else "0", actor=actor)


def set_trigger(trigger: str, actor: str) -> None:
    if trigger not in TRIGGERS:
        raise ValueError("Trigger: internal yoki activepieces.")
    storage.set_setting(TRIGGER_SETTING, trigger, actor=actor)


async def run_daily_generation(
    client: Any,
    trigger: str,
    run_date: str,
    count: int = DAILY_TARGET,
    manual: bool = False,
    actor: str | None = None,
    rng: random.Random | None = None,
    section_chars: int = SECTION_CHARS,
) -> dict[str, Any]:
    """Run the daily generation, or explain why it did not run.

    ``trigger`` is "internal" (bot scheduler), "activepieces" (HTTP hook) or
    "manual" (/autogen run). Scheduled triggers run only when enabled, only
    for the configured trigger, and at most once per ``run_date``.
    """
    actor = actor or trigger
    if not manual:
        settings = automation_settings()
        if not settings["enabled"]:
            return {"status": "disabled"}
        if trigger != settings["trigger"]:
            return {"status": "other_trigger", "configured": settings["trigger"]}
        if not storage.start_daily_run(run_date, trigger):
            return {"status": "already_ran", "run": storage.get_daily_run(run_date)}
    else:
        storage.add_audit(actor, "manual_generation_started", details={"count": count})

    try:
        result = await pipeline.run_incremental_generation(
            client, count, rng=rng, section_chars=section_chars, actor=actor
        )
    except Exception as error:
        reason = (
            str(error) if isinstance(error, ValueError) else describe_section_error(error)
        )
        if not manual:
            storage.finish_daily_run(run_date, "failed", None, {"error": reason[:300]})
        return {"status": "failed", "error": reason}

    made = len(result["questions"])
    shortfall = max(count - made, 0)
    status = "done" if not shortfall and not result.get("sections_failed") else "partial"
    if result.get("nothing_left"):
        status = "nothing_left"
    details = {
        "requested": count,
        "created": made,
        "ready": result.get("ready_for_approval", 0),
        "needs_review": result.get("needs_review", 0),
        "shortfall": shortfall,
        "failed_sections": result.get("sections_failed", 0),
    }
    if not manual:
        storage.finish_daily_run(run_date, status, result.get("run_id"), details)
    return {"status": status, "result": result, **details}


def daily_summary_text(outcome: dict[str, Any], heading: str = "Kunlik generatsiya") -> str:
    status = outcome["status"]
    if status == "disabled":
        return f"{heading}: avtomatik generatsiya o‘chiq."
    if status == "other_trigger":
        return f"{heading}: bu ishga tushirish manbai sozlanmagan ({outcome['configured']})."
    if status == "already_ran":
        return f"{heading}: bugun allaqachon bajarilgan; qayta ishga tushirilmadi."
    if status == "failed":
        return (
            f"{heading} MUVAFFAQIYATSIZ: {outcome['error']}\n"
            "Hech qanday qoralama saqlanmadi. Keyinroq /autogen run bilan qayta urinib ko‘ring."
        )
    if status == "nothing_left":
        return (
            f"{heading}: barcha manba bo‘limlari ishlatib bo‘lingan. Yangi manba yuklang "
            "(/upload). Savollar sun’iy to‘ldirilmadi."
        )
    lines = [
        f"{heading}: {outcome['created']}/{outcome['requested']} ta qoralama tayyorlandi "
        f"(tasdiqlashga tayyor: {outcome['ready']}, NEEDS_REVIEW: {outcome['needs_review']}).",
    ]
    if outcome["shortfall"]:
        lines.append(
            f"{outcome['shortfall']} ta yetishmadi: sifatli manba yetarli bo‘lmadi. "
            "Savollar soni sun’iy ravishda to‘ldirilmadi."
        )
    if outcome["failed_sections"]:
        lines.append(
            f"Xato bergan bo‘limlar: {outcome['failed_sections']} (keyingi safar qayta urinib ko‘riladi)."
        )
    result = outcome.get("result") or {}
    if result.get("stopped_early"):
        lines.append("Ketma-ket xatolar sababli jarayon erta to‘xtadi.")
    lines.append("Har bir savol faqat sizning tasdig‘ingizdan keyin kanalga chiqadi.")
    return "\n".join(lines)


def queue_warnings(daily_slots: int) -> list[str]:
    """Warnings about the publishing queue for the administrator."""
    counts = storage.count_questions_by_status()
    warnings = []
    if daily_slots and counts["approved"] == 0:
        warnings.append("Nashr navbati bo‘sh: tasdiqlangan savol yo‘q.")
    elif daily_slots and counts["approved"] < daily_slots:
        warnings.append(
            f"Navbatda {counts['approved']} ta tasdiqlangan savol bor, kunlik jadval esa "
            f"{daily_slots} ta. Ko‘proq savolni tasdiqlang."
        )
    if counts["publishing"]:
        warnings.append(
            f"{counts['publishing']} ta savolning nashr holati noma’lum; /queue va /resolve."
        )
    if counts["held"]:
        warnings.append(f"{counts['held']} ta savol takroriy deb to‘xtatilgan (held).")
    return warnings
