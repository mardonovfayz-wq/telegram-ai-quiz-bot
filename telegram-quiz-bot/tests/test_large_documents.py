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
    SectionProcessingError,
    build_source_sections,
    classify_document,
    chunk_document,
    extract_document,
    extract_question_answer_pairs,
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

    def test_question_answer_documents_keep_original_pairs_and_full_coverage(self):
        source_text = (
            "[Paragraph 1]\n"
            "Savol: Birinchi savol nima?\n"
            "Javob: Birinchi aniq javob.\n\n"
            "[Paragraph 2]\n"
            "Savol: Ikkinchi savol nima?\n"
            "Javob: Ikkinchi aniq javob."
        )

        pairs = extract_question_answer_pairs(source_text)
        document_type, sections = build_source_sections(
            "qa.txt", source_text, source_id=9
        )

        self.assertEqual(document_type, "question_answer")
        self.assertEqual(classify_document(source_text), "question_answer")
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["question"], "Birinchi savol nima?")
        self.assertEqual(pairs[0]["answer"], "Birinchi aniq javob.")
        self.assertEqual("".join(section["source_excerpt"] for section in sections), source_text)
        self.assertEqual(sections[0]["source_reference"], "[Paragraph 1]")

    def test_normative_legal_documents_are_classified_separately(self):
        self.assertEqual(
            classify_document("[Page 1]\n3-modda. Huquqiy norma matni."),
            "normative_legal",
        )

    def test_mixed_qa_and_legal_text_keeps_both_sections(self):
        source_text = (
            "[Paragraph 1]\nSavol: Qonun qachon qabul qilindi?\n"
            "Javob: 2020-yilda.\n"
            "3-modda. Alohida normativ qoida."
        )
        document_type, sections = build_source_sections("mixed.txt", source_text)
        self.assertEqual(document_type, "mixed")
        self.assertEqual(
            "".join(section["source_excerpt"] for section in sections), source_text
        )
        self.assertEqual(
            [section["section_kind"] for section in sections],
            ["question_answer", "prose"],
        )

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
        self.assertTrue(all(item["source_excerpt"] for item in result["questions"]))

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
        self.assertEqual(result["questions"][0]["review_status"], "NEEDS_REVIEW")

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

    async def test_qa_generation_preserves_source_question_and_answer(self):
        class QaCompletions:
            def __init__(self):
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                prompt = kwargs["messages"][-1]["content"]
                answer = prompt.split("ASL TO‘G‘RI JAVOB: ", 1)[1].splitlines()[0]
                bank = json.loads(
                    prompt.split("JAVOBLAR BANKI:\n", 1)[1].splitlines()[0]
                )
                distractors = [item for item in bank if item != answer][:3]
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({"distractors": distractors})
                            )
                        )
                    ]
                )

        pairs = [
            ("Suv qaynash nuqtasi qanchaga teng?", "100 daraja"),
            ("Yer qaysi yulduz atrofida aylanadi?", "Quyosh"),
            ("O‘zbekistonning poytaxti qaysi?", "Toshkent"),
            ("Bir haftada necha kun bor?", "Yetti kun"),
        ]
        qa_text = "\n\n".join(
            f"[Paragraph {index}]\nSavol: {question}\nJavob: {answer}"
            for index, (question, answer) in enumerate(pairs, start=1)
        )
        client = FakeClient()
        client.completions = QaCompletions()
        client.chat = SimpleNamespace(completions=client.completions)

        result = await generate_questions(
            client,
            [{"id": 9, "title": "qa.txt", "content": qa_text}],
            count=10,
        )

        self.assertEqual(client.completions.calls, 4)
        self.assertEqual(len(result["questions"]), 4)
        first = result["questions"][0]
        self.assertEqual(first["question"], "Suv qaynash nuqtasi qanchaga teng?")
        self.assertEqual(first["options"][first["correct_index"]], "100 daraja")
        self.assertEqual(first["original_question"], first["question"])
        self.assertEqual(first["original_answer"], first["options"][first["correct_index"]])
        self.assertIn(first["original_answer"], first["source_quote"])
        self.assertIn("Savol: Suv qaynash nuqtasi qanchaga teng?", first["source_excerpt"])
        self.assertEqual(first["review_status"], "READY_FOR_APPROVAL")

    async def test_ungrounded_qa_distractors_require_review(self):
        class UnsupportedDistractors:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "distractors": [
                                            "Uydirma javob bir",
                                            "Uydirma javob ikki",
                                            "Uydirma javob uch",
                                        ]
                                    }
                                )
                            )
                        )
                    ]
                )

        client = FakeClient()
        client.chat = SimpleNamespace(
            completions=UnsupportedDistractors()
        )
        result = await generate_questions(
            client,
            [
                {
                    "id": 15,
                    "title": "single-qa.txt",
                    "content": (
                        "[Paragraph 1]\nSavol: Asosiy javob nima?\n"
                        "Javob: Manbada aniq javob."
                    ),
                }
            ],
            count=1,
        )

        question = result["questions"][0]
        self.assertTrue(question["source_verified"])
        self.assertEqual(question["review_status"], "NEEDS_REVIEW")
        self.assertIn("distractor", question["source_review_note"])

    async def test_invalid_ai_shape_gets_one_safe_repair_attempt(self):
        class RepairCompletions:
            def __init__(self):
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    result = {
                        "questions": [
                            {
                                "question": "What rule is stated?",
                                "options": ["A", "B"],
                                "correct_index": 0,
                                "source_reference": "1-modda",
                                "source_quote": "The exact source rule is stated here.",
                            }
                        ]
                    }
                else:
                    result = {
                        "questions": [
                            {
                                "question": "What rule is stated?",
                                "options": ["A", "B", "C", "D"],
                                "correct_index": 0,
                                "source_reference": "1-modda",
                                "source_quote": "The exact source rule is stated here.",
                            }
                        ]
                    }
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(result))
                        )
                    ]
                )

        client = FakeClient()
        client.completions = RepairCompletions()
        client.chat = SimpleNamespace(completions=client.completions)
        result = await generate_questions(
            client,
            [
                {
                    "id": 1,
                    "title": "law.txt",
                    "content": "[Page 1]\n1-modda. The exact source rule is stated here.",
                }
            ],
            count=1,
        )

        self.assertEqual(client.completions.calls, 2)
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["review_status"], "READY_FOR_APPROVAL")

    async def test_second_invalid_ai_shape_stops_without_saving_partial_results(self):
        class AlwaysInvalid:
            def __init__(self):
                self.calls = 0

            async def create(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "questions": [
                                            {
                                                "question": "Question?",
                                                "options": ["A", "B"],
                                                "correct_index": 0,
                                                "source_reference": "1-modda",
                                                "source_quote": "A sufficiently long source quote.",
                                            }
                                        ]
                                    }
                                )
                            )
                        )
                    ]
                )

        client = FakeClient()
        client.completions = AlwaysInvalid()
        client.chat = SimpleNamespace(completions=client.completions)
        with self.assertRaises(SectionProcessingError) as raised:
            await generate_questions(
                client,
                [
                    {
                        "id": 1,
                        "title": "law.txt",
                        "content": "[Page 1]\n1-modda. A sufficiently long source quote.",
                    }
                ],
                count=1,
            )

        self.assertEqual(client.completions.calls, 2)
        self.assertEqual(raised.exception.sections_processed, 0)
        self.assertEqual(raised.exception.sections_total, 1)

    async def test_section_error_stops_processing_and_reports_completed_sections(self):
        sources = [
            {"id": 1, "title": "first.txt", "content": "[Page 1]\n1-modda. First."},
            {"id": 2, "title": "second.txt", "content": "[Page 1]\n2-modda. Second."},
            {"id": 3, "title": "third.txt", "content": "[Page 1]\n3-modda. Third."},
        ]

        async def fail_on_second_section(client, section, count, answer_bank=None):
            if section["source_title"] == "second.txt":
                raise APITimeoutError(request=SimpleNamespace())
            return []

        with patch(
            "quiz_generator._generate_for_section",
            side_effect=fail_on_second_section,
        ):
            with self.assertRaises(SectionProcessingError) as raised:
                await generate_questions(FakeClient(), sources, count=1)

        self.assertEqual(raised.exception.section_number, 2)
        self.assertEqual(raised.exception.sections_processed, 1)
        self.assertEqual(raised.exception.sections_total, 3)

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
                "source_excerpt": "[Page 1]\n1-modda. The cited legal rule appears here.",
                "document_type": "normative_legal",
                "review_status": "NEEDS_REVIEW",
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
        self.assertEqual(saved["review_status"], "NEEDS_REVIEW")
        self.assertEqual(
            saved["source_excerpt"],
            "[Page 1]\n1-modda. The cited legal rule appears here.",
        )
        self.assertFalse(storage.set_question_status(question_ids[0], "approved"))
        self.assertTrue(storage.set_question_status(question_ids[0], "rejected"))

    def test_approval_requires_ready_review_state_even_when_source_matches(self):
        question_id = storage.add_question(
            question="Which rule is stated?",
            options=["A", "B", "C", "D"],
            correct_index=0,
            source_title="law.txt",
            source_reference="1-modda",
            source_quote="The source quote.",
            source_ids=[],
            source_verified=True,
            review_status="NEEDS_REVIEW",
        )
        self.assertFalse(storage.set_question_status(question_id, "approved"))

        ready_id = storage.add_question(
            question="Which rule is stated?",
            options=["A", "B", "C", "D"],
            correct_index=0,
            source_title="law.txt",
            source_reference="1-modda",
            source_quote="The source quote.",
            source_ids=[],
            source_verified=True,
            review_status="READY_FOR_APPROVAL",
        )
        self.assertTrue(storage.set_question_status(ready_id, "approved"))

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
                    CREATE TABLE generation_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_count INTEGER NOT NULL,
                        sections_total INTEGER NOT NULL,
                        sections_processed INTEGER NOT NULL,
                        question_count INTEGER NOT NULL,
                        duplicates_removed INTEGER NOT NULL DEFAULT 0,
                        unverified_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
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
            self.assertEqual(migrated["review_status"], "NEEDS_REVIEW")
            self.assertEqual(migrated["source_excerpt"], "")
            with sqlite3.connect(storage.DB_PATH) as connection:
                run_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(generation_runs)"
                    )
                }
            self.assertIn("needs_review_count", run_columns)
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
