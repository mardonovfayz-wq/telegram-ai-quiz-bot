import json
import sqlite3
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from docx import Document
from openai import APITimeoutError, RateLimitError

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import storage
from quiz_generator import (
    chunk_document,
    extract_document,
    generate_questions,
    is_near_duplicate,
    prepare_plain_text,
    _request_json,
    verify_grounding,
)


class FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        prompt = kwargs["messages"][-1]["content"]
        if "alpha.txt" in prompt:
            reference = "1-modda"
            quote = "Rule alpha supports this quiz question."
            question = "What legal rule does the alpha document describe?"
        else:
            reference = "2-modda"
            quote = "Rule bravo supports this quiz question."
            question = "Which condition appears in the bravo provision?"
        response = {
            "questions": [
                {
                    "question": question,
                    "options": ["A", "B", "C", "D"],
                    "correct_index": 0,
                    "source_reference": reference,
                    "source_quote": quote,
                }
            ]
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(response))
                )
            ]
        )


class FakeClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class LargeDocumentTests(unittest.IsolatedAsyncioTestCase):
    def test_large_plain_text_and_txt_are_not_limited_to_45000_chars(self):
        original = ("A long source line with legal information.\n" * 2_000).strip()
        prepared = prepare_plain_text(original)
        extracted = extract_document("large.txt", original.encode("utf-8"))

        self.assertGreater(len(prepared), 45_000)
        self.assertGreater(len(extracted), 45_000)
        self.assertIn("[Line 2000]", extracted)

    def test_large_docx_and_pdf_extraction_are_not_limited_by_text_length(self):
        document = Document()
        for number in range(1_000):
            document.add_paragraph(
                f"{number + 1}-modda. This long legal paragraph supports a test question."
            )
        docx_buffer = BytesIO()
        document.save(docx_buffer)
        extracted_docx = extract_document("large.docx", docx_buffer.getvalue())

        large_page = "A long PDF page with legal provisions. " * 2_000
        fake_page = SimpleNamespace(extract_text=lambda: large_page)
        with patch("quiz_generator.PdfReader") as pdf_reader:
            pdf_reader.return_value.pages = [fake_page]
            extracted_pdf = extract_document("large.pdf", b"%PDF-test")

        self.assertGreater(len(extracted_docx), 45_000)
        self.assertGreater(len(extracted_pdf), 45_000)
        self.assertIn("[Paragraph 1000]", extracted_docx)
        self.assertIn("[Page 1]", extracted_pdf)

    def test_chunking_preserves_every_character_and_article_heading(self):
        source = "\n\n".join(
            f"{number}-modda. {number} moddaning to‘liq huquqiy qoidasi."
            for number in range(1, 35)
        )
        chunks = chunk_document("law.txt", source, source_id=1, max_chars=220)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(item["source_excerpt"] for item in chunks), source)
        joined_context = "\n".join(item["content"] for item in chunks)
        for number in range(1, 35):
            self.assertIn(f"{number}-modda", joined_context)
        self.assertEqual([item["section_number"] for item in chunks], list(range(1, len(chunks) + 1)))

    async def test_every_source_section_gets_a_separate_openai_request(self):
        sources = [
            {
                "id": 1,
                "title": "alpha.txt",
                "content": "[Page 1]\n1-modda. Rule alpha supports this quiz question.",
            },
            {
                "id": 2,
                "title": "bravo.txt",
                "content": "[Page 1]\n2-modda. Rule bravo supports this quiz question.",
            },
        ]
        client = FakeClient()

        result = await generate_questions(
            client,
            sources,
            count=1,
            section_chars=500,
        )

        self.assertEqual(client.completions.calls, 2)
        self.assertEqual(result["sections_total"], 2)
        self.assertEqual(result["sections_processed"], 2)
        self.assertEqual(len(result["questions"]), 2)
        self.assertEqual(
            {item["source_title"] for item in result["questions"]},
            {"alpha.txt", "bravo.txt"},
        )

    async def test_unverified_reference_is_flagged_instead_of_discarded(self):
        source = {
            "id": 1,
            "title": "alpha.txt",
            "content": "[Page 1]\n1-modda. Rule alpha supports this quiz question.",
        }

        class UnverifiedCompletions(FakeCompletions):
            async def create(self, **kwargs):
                self.calls += 1
                response = {
                    "questions": [
                        {
                            "question": "Which rule is stated in the alpha source?",
                            "options": ["A", "B", "C", "D"],
                            "correct_index": 0,
                            "source_reference": "99-modda",
                            "source_quote": "Rule alpha supports this quiz question.",
                        }
                    ]
                }
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(response))
                        )
                    ]
                )

        client = FakeClient()
        client.completions = UnverifiedCompletions()
        client.chat = SimpleNamespace(completions=client.completions)

        result = await generate_questions(
            client, [source], count=1, section_chars=500
        )

        self.assertEqual(len(result["questions"]), 1)
        self.assertFalse(result["questions"][0]["source_verified"])
        self.assertEqual(result["source_unverified"], 1)

    async def test_rate_limits_and_timeouts_are_retried(self):
        response = SimpleNamespace(
            status_code=429,
            headers={"retry-after": "1"},
            request=SimpleNamespace(),
        )
        rate_limit = RateLimitError("rate limited", response=response, body=None)
        timeout = APITimeoutError(request=SimpleNamespace())
        success = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))
            ]
        )
        client = FakeClient()
        client.chat.completions.create = AsyncMock(
            side_effect=[rate_limit, timeout, success]
        )

        with patch("quiz_generator.asyncio.sleep", new_callable=AsyncMock) as sleep:
            payload = await _request_json(
                client, [{"role": "user", "content": "test"}]
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(client.chat.completions.create.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    def test_near_duplicate_question_detection(self):
        original = "Qonunning 3-moddasida qaysi qoida belgilangan?"
        self.assertTrue(
            is_near_duplicate(
                "Qonunning 3-moddasida qaysi qoida belgilanadi?",
                [original],
            )
        )
        self.assertFalse(
            is_near_duplicate(
                "Qonun qachon kuchga kiradi?",
                [original],
            )
        )


class StorageCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        storage.initialize_database()

    def tearDown(self):
        storage.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_generation_collection_is_saved_together_and_requires_verification(self):
        source_id = storage.add_source(
            "law.txt", "[Page 1]\n1-modda. The cited legal rule appears here."
        )
        questions = [
            {
                "question": "What does the first article state?",
                "options": ["A", "B", "C", "D"],
                "correct_index": 0,
                "source_title": "law.txt",
                "source_reference": "99-modda",
                "source_quote": "The cited legal rule appears here.",
                "source_ids": [source_id],
                "source_verified": False,
                "source_review_note": "Reference not found.",
            }
        ]

        run_id, question_ids = storage.save_generation_collection(
            source_count=1,
            sections_total=4,
            questions=questions,
            duplicates_removed=2,
        )
        saved = storage.get_question(question_ids[0])

        self.assertGreater(run_id, 0)
        self.assertEqual(saved["generation_run_id"], run_id)
        self.assertEqual(saved["status"], "pending")
        self.assertFalse(saved["source_verified"])
        self.assertFalse(storage.set_question_status(question_ids[0], "approved"))
        self.assertTrue(storage.set_question_status(question_ids[0], "rejected"))

    def test_pending_questions_can_be_reviewed_in_pages(self):
        for index in range(25):
            storage.add_question(
                question=f"Question number {index}",
                options=["A", "B", "C", "D"],
                correct_index=0,
                source_title="law.txt",
                source_reference="1-modda",
                source_quote="This is a source quote.",
                source_ids=[],
            )
        first_page = storage.list_questions("pending", limit=20, offset=0)
        second_page = storage.list_questions("pending", limit=20, offset=20)
        self.assertEqual(len(first_page), 20)
        self.assertEqual(len(second_page), 5)

    def test_existing_database_gets_review_and_collection_columns(self):
        original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "legacy.sqlite3"
        try:
            with sqlite3.connect(storage.DB_PATH) as connection:
                connection.execute(
                    """
                    CREATE TABLE questions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        question TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        correct_index INTEGER NOT NULL,
                        source_title TEXT NOT NULL,
                        source_reference TEXT NOT NULL,
                        source_quote TEXT NOT NULL,
                        source_ids_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        published_message_id INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO questions (
                        question, options_json, correct_index, source_title,
                        source_reference, source_quote, source_ids_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Legacy question?",
                        '["A","B","C","D"]',
                        0,
                        "law.txt",
                        "1-modda",
                        "A quote from the law.",
                        "[]",
                        "pending",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            storage.initialize_database()
            migrated = storage.get_question(1)
            self.assertTrue(migrated["source_verified"])
            self.assertIsNone(migrated["generation_run_id"])
        finally:
            storage.DB_PATH = original_path

    def test_grounding_reports_unverifiable_citation(self):
        source = {
            "title": "law.txt",
            "content": "[Page 1]\n1-modda. The cited legal rule appears here.",
        }
        valid, note = verify_grounding(
            "law.txt",
            "99-modda",
            "The cited legal rule appears here.",
            [source],
        )
        self.assertFalse(valid)
        self.assertTrue(note)

    def test_page_number_does_not_replace_a_visible_legal_locator(self):
        source = {
            "title": "law.txt",
            "content": "[Page 1]\n1-modda. The cited legal rule appears here.",
        }
        valid, note = verify_grounding(
            "law.txt",
            "[Page 1]",
            "The cited legal rule appears here.",
            [source],
        )
        self.assertFalse(valid)
        self.assertIn("huquqiy modda", note)


if __name__ == "__main__":
    unittest.main()
