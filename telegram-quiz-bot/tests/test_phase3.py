"""Phase 3 tests: audit log, duplicate-safe publishing, daily generation,
administrator notifications and the Activepieces HTTP hook."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import daily  # noqa: E402
import hook  # noqa: E402
import publishing  # noqa: E402
import storage  # noqa: E402
from notify import AdminNotifier  # noqa: E402
from test_pipeline import CHARS, LAW, FakeClient  # noqa: E402

QUOTE = "Ushbu moddaning asosiy qoidasi raqam 1 bo‘yicha belgilanadi."
TOKEN = "t" * 32


class NetworkError(Exception):
    pass


class TimedOut(NetworkError):
    pass


class BadRequest(Exception):
    pass


class Forbidden(Exception):
    pass


class Recorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, text, key=None):
        self.messages.append(text)


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "phase3.sqlite3"
        storage.initialize_database()
        sleeper = patch("quiz_generator.asyncio.sleep", new=AsyncMock())
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self):
        storage.DB_PATH = self.original
        self.temp_dir.cleanup()

    def add_ready(self, text, approve=True):
        question_id = storage.add_question(
            question=text,
            options=["A", "B", "C", "D"],
            correct_index=0,
            source_title="law.txt",
            source_reference="1-modda",
            source_quote=QUOTE,
            source_ids=[],
            review_status="READY_FOR_APPROVAL",
        )
        if approve:
            self.assertTrue(storage.set_question_status(question_id, "approved", actor="admin:7"))
        return question_id

    def actions(self, question_id):
        return [entry["action"] for entry in reversed(storage.list_audit(50, question_id))]


class AuditLogTests(DatabaseCase):
    def test_approve_reject_edit_unapprove_are_logged_with_actor(self):
        approved = self.add_ready("Birinchi savol matni qanday?")
        rejected = self.add_ready("Ikkinchi savol matni qanday?", approve=False)
        storage.set_question_status(rejected, "rejected", actor="admin:7")
        edited = self.add_ready("Uchinchi savol matni qanday?", approve=False)
        storage.update_question(
            edited,
            question="Uchinchi savol yangi matni qanday?",
            options=["A", "B", "C", "D"],
            correct_index=1,
            source_title="law.txt",
            source_reference="1-modda",
            source_quote=QUOTE,
            source_verified=True,
            source_review_note="",
            review_status="READY_FOR_APPROVAL",
            actor="admin:7",
        )
        storage.unapprove_question(approved, "admin:7", "qayta tekshirish")

        self.assertEqual(self.actions(approved), ["approved", "unapproved"])
        self.assertEqual(self.actions(rejected), ["rejected"])
        edit_entry = storage.list_audit(5, edited)[0]
        self.assertEqual(edit_entry["action"], "edited")
        self.assertEqual(edit_entry["actor"], "admin:7")
        self.assertEqual(edit_entry["details"]["before"]["question"], "Uchinchi savol matni qanday?")
        self.assertEqual(edit_entry["details"]["after"]["correct_index"], 1)
        question = storage.get_question(approved)
        self.assertEqual(question["status"], "pending")
        self.assertEqual(question["approved_by"], "")

    def test_failed_approval_is_not_logged_as_approved(self):
        question_id = storage.add_question(
            question="Tekshirilmagan savol?",
            options=["A", "B", "C", "D"],
            correct_index=0,
            source_title="law.txt",
            source_reference="99-modda",
            source_quote=QUOTE,
            source_ids=[],
            review_status="NEEDS_REVIEW",
        )
        self.assertFalse(storage.set_question_status(question_id, "approved", actor="admin:7"))
        self.assertEqual(self.actions(question_id), [])

    def test_approval_records_who_and_when(self):
        question = storage.get_question(self.add_ready("To‘rtinchi savol matni?"))
        self.assertEqual(question["approved_by"], "admin:7")
        self.assertTrue(question["approved_at"])

    def test_generated_drafts_and_settings_changes_are_logged(self):
        run_id, ids = storage.save_generation_collection(
            1, 1,
            [{
                "question": "Generatsiya qilingan savol?", "options": ["A", "B", "C", "D"],
                "correct_index": 0, "source_title": "law.txt", "source_reference": "1-modda",
                "source_quote": QUOTE, "source_ids": [], "source_verified": True,
                "source_review_note": "", "review_status": "READY_FOR_APPROVAL",
            }],
            0,
            actor="scheduler",
        )
        entry = storage.list_audit(5, ids[0])[0]
        self.assertEqual((entry["action"], entry["actor"], entry["run_id"]), ("generated", "scheduler", run_id))
        daily.set_enabled(True, "admin:7")
        last = storage.list_audit(1)[0]
        self.assertEqual(last["action"], "setting_changed")
        self.assertEqual(last["details"], {"key": daily.AUTO_SETTING, "value": "1"})


class PublishingTests(DatabaseCase):
    async def test_only_approved_questions_are_published(self):
        pending = self.add_ready("Tasdiqlanmagan savol matni?", approve=False)
        sent = []

        async def send(question):
            sent.append(question["id"])
            return 100

        notes = Recorder()
        outcome = await publishing.publish_next(send, notes)
        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(sent, [])
        self.assertEqual(storage.get_question(pending)["status"], "pending")
        self.assertTrue(any("bo‘sh" in text for text in notes.messages))

    async def test_publish_success_is_logged_and_never_repeated(self):
        first = self.add_ready("Birinchi e’lon qilinadigan savol?")
        second = self.add_ready("Ikkinchi e’lon qilinadigan savol boshqacha?")
        sent = []

        async def send(question):
            sent.append(question["id"])
            return 500 + question["id"]

        notes = Recorder()
        self.assertEqual((await publishing.publish_next(send, notes))["question_id"], first)
        self.assertEqual((await publishing.publish_next(send, notes))["question_id"], second)
        self.assertEqual((await publishing.publish_next(send, notes))["status"], "empty")
        self.assertEqual(sent, [first, second])
        self.assertEqual(self.actions(first), ["approved", "publish_started", "published"])
        self.assertEqual(storage.get_question(first)["published_message_id"], 500 + first)
        self.assertTrue(any("navbat endi bo‘sh" in text for text in notes.messages))

    async def test_two_simultaneous_slots_cannot_send_the_same_question(self):
        only = self.add_ready("Yagona tasdiqlangan savol?")
        sent = []

        async def slow_send(question):
            sent.append(question["id"])
            await asyncio.sleep(0.01)
            return 1

        notes = Recorder()
        results = await asyncio.gather(
            publishing.publish_next(slow_send, notes),
            publishing.publish_next(slow_send, notes),
        )
        self.assertEqual(sent, [only])
        self.assertEqual(sorted(item["status"] for item in results), ["empty", "published"])

    async def test_rejected_send_returns_to_queue(self):
        question_id = self.add_ready("Telegram rad etadigan savol?")

        async def send(question):
            raise Forbidden("bot is not an admin")

        notes = Recorder()
        outcome = await publishing.publish_next(send, notes)
        self.assertEqual(outcome["status"], "failed")
        question = storage.get_question(question_id)
        self.assertEqual(question["status"], "approved")
        self.assertEqual(question["publish_attempts"], 1)
        self.assertIn("publish_failed", self.actions(question_id))
        self.assertTrue(any("navbatga qaytarildi" in text for text in notes.messages))

    async def test_uncertain_send_is_never_retried_automatically(self):
        uncertain = self.add_ready("Javobi noma’lum bo‘lgan savol?")
        later = self.add_ready("Keyingi navbatdagi boshqa savol?")
        sent = []

        async def flaky_send(question):
            sent.append(question["id"])
            if question["id"] == uncertain:
                raise TimedOut("timed out")
            return 77

        notes = Recorder()
        self.assertEqual((await publishing.publish_next(flaky_send, notes))["status"], "uncertain")
        self.assertEqual(storage.get_question(uncertain)["status"], "publishing")
        # Next slot publishes the next question, not the uncertain one again.
        result = await publishing.publish_next(flaky_send, notes)
        self.assertEqual(result["question_id"], later)
        self.assertEqual(sent.count(uncertain), 1)
        self.assertTrue(any("/resolve" in text for text in notes.messages))
        # Administrator confirms it did reach the channel.
        self.assertTrue(storage.resolve_publishing(uncertain, "published", "admin:7", 999))
        self.assertEqual(storage.get_question(uncertain)["status"], "published")
        self.assertIn("resolved_published", self.actions(uncertain))

    async def test_question_matching_a_published_one_is_held(self):
        first = self.add_ready("Mehnat shartnomasi qanday shaklda tuziladi?")
        twin = self.add_ready("Mehnat shartnomasi qanday shaklda tuziladi ?")

        async def send(question):
            return 1

        notes = Recorder()
        await publishing.publish_next(send, notes)
        outcome = await publishing.publish_next(send, notes)
        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(storage.get_question(twin)["status"], "held")
        self.assertIn(f"#{first}", storage.get_question(twin)["last_publish_error"])
        self.assertTrue(storage.unapprove_question(twin, "admin:7"))

    def test_error_classification(self):
        self.assertEqual(publishing.classify_publish_error(BadRequest()), "not_sent")
        self.assertEqual(publishing.classify_publish_error(Forbidden()), "not_sent")
        self.assertEqual(publishing.classify_publish_error(TimedOut()), "uncertain")
        self.assertEqual(publishing.classify_publish_error(NetworkError()), "uncertain")
        self.assertEqual(publishing.classify_publish_error(RuntimeError()), "uncertain")


class DailyGenerationTests(DatabaseCase):
    async def run_daily(self, client, trigger="internal", date="2026-11-12", **kwargs):
        return await daily.run_daily_generation(
            client, trigger, date, section_chars=CHARS, **kwargs
        )

    async def test_disabled_by_default_and_makes_no_ai_calls(self):
        storage.add_source("law.txt", LAW)
        client = FakeClient()
        self.assertFalse(daily.automation_settings()["enabled"])
        outcome = await self.run_daily(client)
        self.assertEqual(outcome["status"], "disabled")
        self.assertEqual(client.generation_calls, [])
        self.assertIsNone(storage.get_daily_run("2026-11-12"))

    async def test_runs_once_per_day_even_with_two_triggers(self):
        storage.add_source("law.txt", LAW)
        daily.set_enabled(True, "admin:7")
        first = await self.run_daily(FakeClient(), count=2)
        self.assertEqual(first["status"], "done")
        again = await self.run_daily(FakeClient(), count=2)
        self.assertEqual(again["status"], "already_ran")
        other = await self.run_daily(FakeClient(), trigger="activepieces", count=2)
        self.assertEqual(other["status"], "other_trigger")
        next_day = await self.run_daily(FakeClient(), date="2026-11-13", count=2)
        self.assertEqual(next_day["status"], "done")
        self.assertEqual(len(storage.list_questions("pending")), 4)
        self.assertEqual(storage.get_daily_run("2026-11-12")["details"]["created"], 2)

    async def test_activepieces_trigger_when_configured(self):
        storage.add_source("law.txt", LAW)
        daily.set_enabled(True, "admin:7")
        daily.set_trigger("activepieces", "admin:7")
        self.assertEqual((await self.run_daily(FakeClient(), count=1))["status"], "other_trigger")
        outcome = await self.run_daily(FakeClient(), trigger="activepieces", count=1)
        self.assertEqual(outcome["status"], "done")

    async def test_shortfall_is_reported_never_padded(self):
        storage.add_source("law.txt", LAW)  # five articles -> at most five drafts
        daily.set_enabled(True, "admin:7")
        client = FakeClient()
        outcome = await self.run_daily(client)
        self.assertEqual(outcome["created"], 5)
        self.assertEqual(outcome["shortfall"], 5)
        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(len(storage.list_questions("pending")), 5)
        text = daily.daily_summary_text(outcome)
        self.assertIn("5/10", text)
        self.assertIn("sun’iy", text)
        # Nothing left the next day: reported, still not padded.
        tomorrow = await self.run_daily(client, date="2026-11-13")
        self.assertEqual(tomorrow["status"], "nothing_left")
        self.assertIn("sun’iy", daily.daily_summary_text(tomorrow))

    async def test_drafts_from_daily_run_still_need_approval(self):
        storage.add_source("law.txt", LAW)
        daily.set_enabled(True, "admin:7")
        await self.run_daily(FakeClient(), count=3)
        counts = storage.count_questions_by_status()
        self.assertEqual(counts["pending"], 3)
        self.assertEqual(counts["approved"], 0)

    async def test_failed_generation_is_recorded_and_reported(self):
        daily.set_enabled(True, "admin:7")
        outcome = await self.run_daily(FakeClient())  # no sources uploaded
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(storage.get_daily_run("2026-11-12")["status"], "failed")
        self.assertIn("MUVAFFAQIYATSIZ", daily.daily_summary_text(outcome))

    async def test_manual_run_works_while_disabled_and_does_not_use_the_daily_slot(self):
        storage.add_source("law.txt", LAW)
        outcome = await self.run_daily(FakeClient(), trigger="manual", manual=True, count=1, actor="admin:7")
        self.assertEqual(outcome["created"], 1)
        self.assertIsNone(storage.get_daily_run("2026-11-12"))
        self.assertEqual(storage.list_audit(10)[-1]["action"], "manual_generation_started")

    def test_queue_warnings(self):
        self.assertIn("bo‘sh", daily.queue_warnings(2)[0])
        self.add_ready("Navbatdagi yagona savol matni?")
        self.assertIn("1 ta", daily.queue_warnings(2)[0])
        self.assertEqual(daily.queue_warnings(1), [])


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_alert_is_not_repeated_within_the_window(self):
        sent = []
        now = [0.0]

        async def send(text):
            sent.append(text)

        notifier = AdminNotifier(send, window_seconds=3600, clock=lambda: now[0])
        self.assertTrue(await notifier.notify("Navbat bo‘sh", key="empty"))
        self.assertFalse(await notifier.notify("Navbat bo‘sh", key="empty"))
        now[0] = 4000
        self.assertTrue(await notifier.notify("Navbat bo‘sh", key="empty"))
        self.assertTrue(await notifier.notify("Kalitsiz xabar"))
        self.assertTrue(await notifier.notify("Kalitsiz xabar"))
        self.assertEqual(len(sent), 4)

    async def test_send_failure_never_raises(self):
        async def broken(text):
            raise ConnectionError("down")

        self.assertFalse(await AdminNotifier(broken).notify("test"))


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, method, path, headers=None, outcome=None):
        callback = AsyncMock(return_value=outcome or {"accepted": True})
        status, body = await hook.handle_request(method, path, headers or {}, TOKEN, callback)
        return status, body, callback

    async def test_health_check_reveals_nothing(self):
        status, body, callback = await self.call("GET", "/healthz")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        callback.assert_not_awaited()

    async def test_token_is_required(self):
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": TOKEN}):
            with self.subTest(headers=headers):
                status, _body, callback = await self.call("POST", "/hooks/daily-generation", headers)
                self.assertEqual(status, 401)
                callback.assert_not_awaited()

    async def test_valid_call_is_accepted_or_refused_with_reason(self):
        auth = {"authorization": f"Bearer {TOKEN}"}
        status, body, callback = await self.call("POST", "/hooks/daily-generation", auth)
        self.assertEqual((status, body), (202, {"status": "accepted"}))
        callback.assert_awaited_once()
        status, body, _ = await self.call(
            "POST", "/hooks/daily-generation", auth, {"accepted": False, "reason": "disabled"}
        )
        self.assertEqual((status, body["reason"]), (409, "disabled"))

    async def test_wrong_method_and_unknown_path(self):
        self.assertEqual((await self.call("GET", "/hooks/daily-generation"))[0], 405)
        self.assertEqual((await self.call("GET", "/admin"))[0], 404)

    def test_weak_token_is_refused(self):
        self.assertFalse(hook.token_is_strong("short"))
        with self.assertRaises(RuntimeError):
            asyncio.run(hook.start_hook_server("127.0.0.1", 0, "short", AsyncMock()))

    async def test_real_socket_round_trip(self):
        callback = AsyncMock(return_value={"accepted": True})
        server = await hook.start_hook_server("127.0.0.1", 0, TOKEN, callback)
        port = server.sockets[0].getsockname()[1]
        try:
            async def request(raw):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(raw)
                await writer.drain()
                data = await reader.read()
                writer.close()
                return data.decode()

            ok = await request(
                f"POST /hooks/daily-generation HTTP/1.1\r\nHost: x\r\n"
                f"Authorization: Bearer {TOKEN}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
            )
            self.assertTrue(ok.startswith("HTTP/1.1 202"))
            self.assertEqual(json.loads(ok.split("\r\n\r\n", 1)[1]), {"status": "accepted"})
            denied = await request(
                b"POST /hooks/daily-generation HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
            )
            self.assertTrue(denied.startswith("HTTP/1.1 401"))
            garbage = await request(b"nonsense\r\n\r\n")
            self.assertTrue(garbage.startswith("HTTP/1.1 400"))
            self.assertEqual(callback.await_count, 1)
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
