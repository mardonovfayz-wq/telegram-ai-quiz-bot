"""Phase 2 tests: section ledger, incremental and failure-tolerant generation."""

import hashlib
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import pipeline  # noqa: E402
import storage  # noqa: E402
from bot_helpers import parse_generate_args  # noqa: E402
from openai import APITimeoutError  # noqa: E402

# Five short articles; with small sections each article is its own section.
LAW = "\n\n".join(
    f"[Paragraph {number}]\n{number}-modda. Qoida {number}\n\n"
    f"[Paragraph {number}b]\nUshbu moddaning asosiy qoidasi raqam {number} bo‘yicha belgilanadi."
    for number in range(1, 6)
).replace("[Paragraph 1b]", "[Paragraph 11]").replace(
    "[Paragraph 2b]", "[Paragraph 12]"
).replace("[Paragraph 3b]", "[Paragraph 13]").replace(
    "[Paragraph 4b]", "[Paragraph 14]"
).replace("[Paragraph 5b]", "[Paragraph 15]")

# Small sections so every article becomes its own section.
CHARS = 120

QUESTION_TEMPLATES = [
    "Birinchi moddada qaysi raqam belgilangan?",
    "Ikkinchi qoida qanday tartibni nazarda tutadi?",
    "Uchinchi modda nimani tartibga soladi?",
    "To‘rtinchi qoidaga ko‘ra qaysi raqam ko‘rsatilgan?",
    "Beshinchi moddadagi asosiy qoida qaysi?",
]


def _response(payload):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


class FakeClient:
    """Answers generation prompts from the article in the section text.

    ``fail_articles``: article numbers whose generation request raises a
    timeout. ``support``: value returned by the answer-support check.
    """

    def __init__(self, fail_articles=(), support=True, support_error=False):
        self.fail_articles = set(fail_articles)
        self.support = support
        self.support_error = support_error
        self.generation_calls = []
        self.support_calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        if "TO‘G‘RI JAVOB:" in prompt and "supported" in prompt:
            self.support_calls += 1
            if self.support_error:
                raise APITimeoutError(request=SimpleNamespace())
            return _response({"supported": self.support, "reason": "test"})
        article = None
        for number in range(1, 6):
            if f"\n{number}-modda. Qoida {number}" in prompt:
                article = number
        self.generation_calls.append(article)
        if article in self.fail_articles:
            raise APITimeoutError(request=SimpleNamespace())
        if article is None:
            return _response({"questions": []})
        return _response(
            {
                "questions": [
                    {
                        "question": QUESTION_TEMPLATES[article - 1],
                        "options": [f"Raqam {article}", "Boshqa A", "Boshqa B", "Boshqa C"],
                        "correct_index": 0,
                        "source_reference": f"{article}-modda",
                        "source_quote": (
                            f"Ushbu moddaning asosiy qoidasi raqam {article} bo‘yicha belgilanadi."
                        ),
                    }
                ]
            }
        )


class PipelineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "pipeline.sqlite3"
        storage.initialize_database()
        # OpenAI retry back-off would make failure tests slow; skip the waits.
        sleeper = patch("quiz_generator.asyncio.sleep", new=AsyncMock())
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def tearDown(self):
        storage.DB_PATH = self.original
        self.temp_dir.cleanup()

    def ledger(self):
        with sqlite3.connect(storage.DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM source_sections ORDER BY start_offset"
                )
            ]


class LedgerTests(PipelineTestCase):
    def test_sync_is_idempotent_and_never_resets_status(self):
        storage.add_source("law.txt", LAW)
        _sources, first = pipeline.eligible_sections(CHARS)
        self.assertGreaterEqual(len(first), 5)
        rows_before = self.ledger()
        with sqlite3.connect(storage.DB_PATH) as connection:
            connection.execute(
                "UPDATE source_sections SET status = 'used' WHERE id = ?",
                (rows_before[0]["id"],),
            )
        _sources, second = pipeline.eligible_sections(CHARS)
        rows_after = self.ledger()
        self.assertEqual(len(rows_after), len(rows_before))
        self.assertEqual(rows_after[0]["status"], "used")
        self.assertEqual(len(second), len(first) - 1)

    def test_section_rows_link_back_to_exact_source_text(self):
        storage.add_source("law.txt", LAW)
        pipeline.eligible_sections(CHARS)
        rows = self.ledger()
        self.assertEqual(rows[0]["start_offset"], 0)
        self.assertEqual(rows[-1]["end_offset"], len(LAW))
        for row in rows:
            excerpt = LAW[row["start_offset"]:row["end_offset"]]
            self.assertEqual(hashlib.sha256(excerpt.encode()).hexdigest(), row["section_sha256"])


class IncrementalGenerationTests(PipelineTestCase):
    async def test_consecutive_runs_use_new_sections_until_exhausted(self):
        storage.add_source("law.txt", LAW)
        client = FakeClient()
        first = await pipeline.run_incremental_generation(client, 2, rng=random.Random(1), section_chars=CHARS)
        self.assertEqual(len(first["questions"]), 2)
        first_articles = [q["legal_article"] for q in first["questions"]]

        second = await pipeline.run_incremental_generation(client, 2, rng=random.Random(2), section_chars=CHARS)
        second_articles = [q["legal_article"] for q in second["questions"]]
        self.assertEqual(len(second_articles), 2)
        self.assertFalse(set(first_articles) & set(second_articles))

        third = await pipeline.run_incremental_generation(client, 5, rng=random.Random(3), section_chars=CHARS)
        self.assertEqual(len(third["questions"]), 1)
        self.assertEqual(third["sections_remaining"], 0)

        fourth = await pipeline.run_incremental_generation(client, 5, section_chars=CHARS)
        self.assertTrue(fourth["nothing_left"])
        all_articles = sorted(first_articles + second_articles + [third["questions"][0]["legal_article"]])
        self.assertEqual(all_articles, ["1", "2", "3", "4", "5"])

    async def test_drafts_are_saved_pending_and_ready_when_supported(self):
        storage.add_source("law.txt", LAW)
        result = await pipeline.run_incremental_generation(FakeClient(), 1, section_chars=CHARS)
        saved = storage.get_question(result["question_ids"][0])
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(saved["review_status"], "READY_FOR_APPROVAL")
        self.assertEqual(saved["source_reference"], "1-modda")

    async def test_failed_section_does_not_discard_other_drafts_and_is_retried(self):
        storage.add_source("law.txt", LAW)
        client = FakeClient(fail_articles={2})
        result = await pipeline.run_incremental_generation(client, 3, section_chars=CHARS)
        self.assertEqual(sorted(q["legal_article"] for q in result["questions"]), ["1", "3", "4"])
        self.assertEqual(result["sections_failed"], 1)
        self.assertFalse(result["stopped_early"])
        failed = [row for row in self.ledger() if row["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["attempts"], 1)
        self.assertIn("tarmoq", failed[0]["last_error"])
        # Saved drafts survive in the database.
        self.assertEqual(len(storage.list_questions("pending")), 3)

        # Next run retries the failed section once the problem is gone.
        retry = await pipeline.run_incremental_generation(FakeClient(), 2, section_chars=CHARS)
        self.assertIn("2", [q["legal_article"] for q in retry["questions"]])

    async def test_section_failing_three_times_stops_being_retried(self):
        storage.add_source("law.txt", LAW)
        for _ in range(3):
            await pipeline.run_incremental_generation(FakeClient(fail_articles={1}), 1, section_chars=CHARS)
        failed = [row for row in self.ledger() if row["status"] == "failed"]
        self.assertEqual(failed[0]["attempts"], 3)
        _sources, eligible = pipeline.eligible_sections(CHARS)
        self.assertNotIn(failed[0]["id"], [section["ledger_id"] for section in eligible])
        summary = storage.coverage_summary()[0]
        self.assertEqual(summary["failed_final"], 1)

    async def test_outage_stops_early_but_keeps_finished_drafts(self):
        storage.add_source("law.txt", LAW)
        client = FakeClient(fail_articles={2, 3, 4})
        result = await pipeline.run_incremental_generation(client, 5, section_chars=CHARS)
        self.assertTrue(result["stopped_early"])
        self.assertEqual([q["legal_article"] for q in result["questions"]], ["1"])
        self.assertEqual(len(storage.list_questions("pending")), 1)
        self.assertNotIn(5, client.generation_calls)

    async def test_unsuitable_qa_pair_is_recorded_in_ledger(self):
        long_answer = "Juda uzun javob matni " * 10
        storage.add_source(
            "qa.txt",
            f"[Paragraph 1]\nSavol: Uzun javobli savol?\nJavob: {long_answer}",
        )
        result = await pipeline.run_incremental_generation(FakeClient(), 1, section_chars=CHARS)
        self.assertEqual(result["unsuitable_count"], 1)
        statuses = {row["status"] for row in self.ledger()}
        self.assertIn("unsuitable", statuses)
        self.assertEqual(
            storage.list_skipped_items(result["run_id"])[0]["original_answer"],
            long_answer.strip(),
        )


class AnswerSupportTests(PipelineTestCase):
    async def test_unsupported_answer_is_downgraded_to_needs_review(self):
        storage.add_source("law.txt", LAW)
        result = await pipeline.run_incremental_generation(FakeClient(support=False), 1, section_chars=CHARS)
        question = result["questions"][0]
        self.assertEqual(question["review_status"], "NEEDS_REVIEW")
        self.assertIn("AI tekshiruvi", question["source_review_note"])
        self.assertFalse(storage.set_question_status(result["question_ids"][0], "approved"))

    async def test_failed_support_check_never_blocks_but_requires_review(self):
        storage.add_source("law.txt", LAW)
        result = await pipeline.run_incremental_generation(FakeClient(support_error=True), 1, section_chars=CHARS)
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["review_status"], "NEEDS_REVIEW")
        self.assertIn("bajarilmadi", result["questions"][0]["source_review_note"])

    async def test_support_check_never_upgrades_an_unverified_citation(self):
        storage.add_source("law.txt", LAW)

        class WrongArticle(FakeClient):
            async def _create(self, **kwargs):
                response = await super()._create(**kwargs)
                payload = json.loads(response.choices[0].message.content)
                for item in payload.get("questions", []):
                    item["source_reference"] = "21-modda"
                return _response(payload)

        client = WrongArticle(support=True)
        result = await pipeline.run_incremental_generation(client, 1, section_chars=CHARS)
        self.assertEqual(result["questions"][0]["review_status"], "NEEDS_REVIEW")
        self.assertEqual(client.support_calls, 0)

    async def test_qa_questions_skip_the_support_check(self):
        storage.add_source(
            "qa.txt",
            "[Paragraph 1]\nSavol: Sinov muddati qancha?\nJavob: Uch oy\n\n"
            "[Paragraph 2]\nSavol: Ish vaqti qancha?\nJavob: Qirq soat\n\n"
            "[Paragraph 3]\nSavol: Ta’til necha kun?\nJavob: O‘n besh kun\n\n"
            "[Paragraph 4]\nSavol: Ish haqi qachon to‘lanadi?\nJavob: Oyiga ikki marta",
        )

        class QaClient(FakeClient):
            async def _create(self, **kwargs):
                prompt = kwargs["messages"][-1]["content"]
                if "JAVOBLAR BANKI" in prompt:
                    answer = prompt.split("ASL TO‘G‘RI JAVOB: ", 1)[1].splitlines()[0]
                    bank = json.loads(prompt.split("JAVOBLAR BANKI:\n", 1)[1].splitlines()[0])
                    return _response({"distractors": [item for item in bank if item != answer][:3]})
                return await super()._create(**kwargs)

        client = QaClient(support=False)
        result = await pipeline.run_incremental_generation(client, 1, section_chars=CHARS)
        self.assertEqual(result["questions"][0]["review_status"], "READY_FOR_APPROVAL")
        self.assertEqual(client.support_calls, 0)


class DuplicateSourceTests(PipelineTestCase):
    def test_same_text_is_detected_for_new_and_legacy_rows(self):
        new_id = storage.add_source("law.txt", LAW)
        self.assertEqual(storage.find_source_by_content(LAW)["id"], new_id)
        self.assertIsNone(storage.find_source_by_content(LAW + " o‘zgargan"))
        with sqlite3.connect(storage.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO sources (title, content, created_at) VALUES (?, ?, ?)",
                ("old.txt", "Eski yuklangan matn", "2026-01-01"),
            )
        legacy = storage.find_source_by_content("Eski yuklangan matn")
        self.assertEqual(legacy["title"], "old.txt")
        with sqlite3.connect(storage.DB_PATH) as connection:
            stored_hash = connection.execute(
                "SELECT content_sha256 FROM sources WHERE title = 'old.txt'"
            ).fetchone()[0]
        self.assertEqual(stored_hash, "")  # legacy row left untouched


class RecheckTests(PipelineTestCase):
    def test_recheck_finds_old_wrong_citations_without_changing_the_database(self):
        source_id = storage.add_source("law.txt", LAW)
        quote = "Ushbu moddaning asosiy qoidasi raqam 1 bo‘yicha belgilanadi."
        good = storage.add_question(
            question="To‘g‘ri havolali savol?",
            options=["A", "B", "C", "D"],
            correct_index=0,
            source_title="law.txt",
            source_reference="1-modda",
            source_quote=quote,
            source_ids=[source_id],
            review_status="READY_FOR_APPROVAL",
        )
        bad = storage.add_question(
            question="Eski xato havolali savol?",
            options=["A", "B", "C", "D"],
            correct_index=0,
            source_title="law.txt",
            source_reference="21-modda",
            source_quote=quote,
            source_ids=[source_id],
            review_status="READY_FOR_APPROVAL",
        )
        storage.set_question_status(bad, "approved")
        before = hashlib.sha256(storage.DB_PATH.read_bytes()).hexdigest()
        report = pipeline.recheck_questions()
        after = hashlib.sha256(storage.DB_PATH.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["ok"], 1)
        self.assertEqual([item["id"] for item in report["problems"]], [bad])
        self.assertEqual(report["problems"][0]["status"], "approved")
        self.assertNotIn(good, [item["id"] for item in report["problems"]])


class GenerateArgumentTests(unittest.TestCase):
    def test_modes_and_counts(self):
        self.assertEqual(parse_generate_args([]), ("incremental", 10))
        self.assertEqual(parse_generate_args(["5"]), ("incremental", 5))
        self.assertEqual(parse_generate_args(["full"]), ("full", 10))
        self.assertEqual(parse_generate_args(["FULL", "3"]), ("full", 3))
        for bad in (["0"], ["21"], ["x"], ["full", "1", "2"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_generate_args(bad)


if __name__ == "__main__":
    unittest.main()
