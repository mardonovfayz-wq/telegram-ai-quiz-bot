"""Phase 0/1 regression tests: one or more tests for every bug from the audit."""

import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

BOT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BOT_DIR.parent
sys.path.insert(0, str(BOT_DIR))

import storage  # noqa: E402
from bot_helpers import (  # noqa: E402
    POLL_EXPLANATION_MAX_CHARS,
    build_poll_explanation,
    differs_from_original_pair,
    format_question,
    parse_admin_id,
    parse_channel_id,
    parse_source_metadata,
    source_display_name,
)
from legal_refs import (  # noqa: E402
    find_inline_references,
    find_locators,
    locate_citation,
    normalize,
    parse_legal_structure,
    parse_reference,
)
from quiz_generator import (  # noqa: E402
    build_source_sections,
    check_citation,
    classify_document,
    extract_question_answer_pairs,
    generate_questions,
    qa_poll_unsuitability,
    shuffle_options,
    verify_grounding,
)


def _response(payload):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


class ScriptedClient:
    """Fake OpenAI client: returns queued payloads and records every prompt."""

    def __init__(self, handler):
        self.prompts = []
        self._handler = handler
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        self.prompts.append(prompt)
        return _response(self._handler(prompt))


UZ_LAW = (
    "[Paragraph 1]\n1-modda. Umumiy qoidalar\n\n"
    "[Paragraph 2]\nMehnat shartnomasi yozma shaklda tuziladi.\n\n"
    "[Paragraph 3]\nShartnoma quyidagilarni o‘z ichiga oladi:\n\n"
    "[Paragraph 4]\n1) xodimning familiyasi va ismi;\n\n"
    "[Paragraph 5]\n2) ish joyi va lavozimi.\n\n"
    "[Paragraph 6]\nShartnomaning bir nusxasi xodimga beriladi.\n\n"
    "[Paragraph 7]\n2-modda. Sinov muddati\n\n"
    "[Paragraph 8]\nSinov muddati uch oydan oshmasligi kerak.\n\n"
    "[Paragraph 9]\n21-modda. Ish vaqti\n\n"
    "[Paragraph 10]\nIsh vaqti haftasiga qirq soatdan oshmaydi."
)

UZ_CYRILLIC_LAW = (
    "[Line 1] 5-модда. Меҳнат шартномаси\n"
    "[Line 2] Меҳнат шартномаси ёзма шаклда тузилади.\n"
    "[Line 3] Шартнома қуйидагиларни ўз ичига олади:\n"
    "[Line 4] 1) ходимнинг исми;\n"
    "[Line 5] 2) иш жойи ва лавозими.\n"
)

RU_LAW = (
    "[Line 1] Статья 7. Трудовой договор\n"
    "[Line 2] 1. Трудовой договор заключается в письменной форме.\n"
    "[Line 3] 2. Трудовой договор должен содержать:\n"
    "[Line 4] 1) сведения о работнике;\n"
    "[Line 5] 2) место работы.\n"
    "[Line 6] Статья 17. Испытательный срок\n"
    "[Line 7] Испытательный срок не может превышать трех месяцев.\n"
)


class ArticleBoundaryTests(unittest.TestCase):
    """Audit bug: '1-modda' matched inside '21-modda' (substring matching)."""

    def test_1_modda_never_matches_inside_21_or_11_modda(self):
        numbers = [item.number for item in find_locators("21-modda, 11-modda va 111-modda")]
        self.assertEqual(numbers, ["21", "11", "111"])
        self.assertNotIn("1", numbers)

    def test_quote_under_article_1_cannot_be_cited_as_article_21(self):
        result = locate_citation(
            UZ_LAW, "21-modda", "Mehnat shartnomasi yozma shaklda tuziladi."
        )
        self.assertFalse(result.verified)
        self.assertIn("1-modda", result.note)

    def test_quote_under_article_21_cannot_be_cited_as_article_1(self):
        result = locate_citation(UZ_LAW, "1-modda", "Ish vaqti haftasiga qirq soatdan oshmaydi.")
        self.assertFalse(result.verified)
        ok = locate_citation(UZ_LAW, "21-modda", "Ish vaqti haftasiga qirq soatdan oshmaydi.")
        self.assertTrue(ok.verified)
        self.assertEqual(ok.canonical_reference, "21-modda")

    def test_the_exact_audit_probe_is_now_rejected(self):
        section = (
            "[Page 1]\n1-modda. Mehnat shartnomasi yozma shaklda tuziladi.\n"
            "21-modda. Boshqa qoida."
        )
        verified, note = verify_grounding(
            "law.txt",
            "21-modda",
            "Mehnat shartnomasi yozma shaklda tuziladi.",
            [{"title": "law.txt", "content": section}],
            section_text=section,
        )
        self.assertFalse(verified)
        self.assertTrue(note)

    def test_russian_article_numbers_are_strict_too(self):
        self.assertFalse(
            locate_citation(RU_LAW, "Статья 17", "заключается в письменной форме").verified
        )
        self.assertFalse(
            locate_citation(RU_LAW, "Статья 1", "Испытательный срок не может превышать").verified
        )
        self.assertTrue(
            locate_citation(RU_LAW, "Статья 17", "Испытательный срок не может превышать").verified
        )


class LegalTermsVersusOrdinaryWordsTests(unittest.TestCase):
    """Audit bug: 'moddaning', 'band bo‘lgan', 'section about' were legal locators."""

    def test_ordinary_words_are_not_locators(self):
        text = (
            "Ushbu moddaning talablari. Xodim band bo‘lgan vaqtda dam oladi. "
            "This section about tax. Bandlik oshdi. Qismlarga bo‘lindi. "
            "Пункт назначения изменился. Часть работы выполнена. Статья в журнале."
        )
        self.assertEqual(find_locators(text), [])

    def test_plain_prose_is_not_classified_as_normative(self):
        self.assertEqual(
            classify_document("[Line 1] Xodim ish bilan band bo‘lgan vaqtda dam oladi."),
            "general",
        )
        self.assertEqual(
            classify_document("[Line 1] Ushbu moddaning mazmuni sharhlanadi."),
            "general",
        )

    def test_incidental_word_does_not_block_a_page_reference(self):
        text = "[Line 1] Ushbu moddaning talablari bajarilishi shart."
        verified, note = verify_grounding(
            "t.txt",
            "[Line 1]",
            "Ushbu moddaning talablari bajarilishi shart.",
            [{"title": "t.txt", "content": text}],
            section_text=text,
        )
        self.assertTrue(verified, note)

    def test_real_headings_in_three_scripts_are_normative(self):
        for text in (
            "[Page 1]\n3-modda. Huquqiy norma matni.",
            "[Page 1]\n3-модда. Ҳуқуқий норма матни.",
            "[Page 1]\nСтатья 3. Правовая норма.",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_document(text), "normative_legal")

    def test_prose_mention_with_suffix_is_not_a_heading(self):
        structure = parse_legal_structure(
            "[Line 1] 5-moddasida ko‘rsatilgan tartibda.\n[Line 2] Boshqa matn."
        )
        self.assertFalse(structure.has_articles)


class ScriptAndTerminologyTests(unittest.TestCase):
    def test_locators_in_uzbek_latin_cyrillic_and_russian(self):
        cases = {
            "5-moddasining 2-qismi 3-bandi": [("article", "5"), ("part", "2"), ("clause", "3")],
            "5-модда 2-қисм 3-банд": [("article", "5"), ("part", "2"), ("clause", "3")],
            "пункт 3 части 2 статьи 5": [("clause", "3"), ("part", "2"), ("article", "5")],
            "п. 3 ч. 2 ст. 5": [("clause", "3"), ("part", "2"), ("article", "5")],
            "25¹-modda": [("article", "25¹")],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    [(item.kind, item.number) for item in find_locators(text)], expected
                )

    def test_superscript_article_number_stays_distinct(self):
        parsed = parse_reference("25¹-modda")
        self.assertNotEqual(parsed.articles, parse_reference("251-modda").articles)

    def test_apostrophe_variants_normalise_identically(self):
        self.assertEqual(normalize("o‘z ichiga"), normalize("oʻz ichiga"))
        self.assertEqual(normalize("o'z  ichiga"), normalize("O’Z ICHIGA"))


class ArticlePartClauseTests(unittest.TestCase):
    def test_uzbek_latin_hierarchy(self):
        structure = parse_legal_structure(UZ_LAW)
        self.assertTrue(structure.has_articles)
        clause_2 = locate_citation(UZ_LAW, "1-modda, 2-qism, 2-band", "2) ish joyi va lavozimi.")
        self.assertTrue(clause_2.verified, clause_2.note)
        self.assertEqual((clause_2.article, clause_2.part, clause_2.clause), ("1", "2", "2"))
        self.assertEqual(clause_2.canonical_reference, "1-modda, 2-qism, 2-band")

        part_3 = locate_citation(
            UZ_LAW, "1-modda", "Shartnomaning bir nusxasi xodimga beriladi."
        )
        self.assertTrue(part_3.verified)
        self.assertEqual(part_3.canonical_reference, "1-modda, 3-qism")
        self.assertIsNone(part_3.clause)

    def test_wrong_part_or_clause_is_rejected(self):
        self.assertFalse(
            locate_citation(UZ_LAW, "1-modda, 1-qism", "2) ish joyi va lavozimi.").verified
        )
        self.assertFalse(
            locate_citation(UZ_LAW, "1-modda, 2-qism, 1-band", "2) ish joyi va lavozimi.").verified
        )

    def test_uzbek_cyrillic_hierarchy(self):
        result = locate_citation(
            UZ_CYRILLIC_LAW, "5-модда, 2-қисм, 2-банд", "2) иш жойи ва лавозими."
        )
        self.assertTrue(result.verified, result.note)
        self.assertEqual(result.canonical_reference, "5-модда, 2-қисм, 2-банд")

    def test_russian_explicit_parts_and_clauses(self):
        result = locate_citation(RU_LAW, "пункт 2 части 2 статьи 7", "2) место работы.")
        self.assertTrue(result.verified, result.note)
        self.assertEqual(result.canonical_reference, "Статья 7, часть 2, пункт 2")
        part_1 = locate_citation(RU_LAW, "ч. 1 ст. 7", "заключается в письменной форме")
        self.assertTrue(part_1.verified)
        self.assertEqual(part_1.canonical_reference, "Статья 7, часть 1")
        self.assertFalse(
            locate_citation(RU_LAW, "ч. 2 ст. 7", "заключается в письменной форме").verified
        )

    def test_pdf_layout_lines_never_produce_guessed_part_numbers(self):
        pdf_text = (
            "[Page 1]\n4-modda. Ta’til\nXodimga har yili mehnat ta’tili\n"
            "beriladi. Ta’til muddati o‘n besh kundan kam bo‘lmaydi.\n"
            "Qo‘shimcha ta’til alohida beriladi."
        )
        result = locate_citation(pdf_text, "4-modda", "Qo‘shimcha ta’til alohida beriladi.")
        self.assertTrue(result.verified)
        self.assertIsNone(result.part)
        self.assertEqual(result.canonical_reference, "4-modda")
        cited_part = locate_citation(pdf_text, "4-modda, 2-qism", "Qo‘shimcha ta’til alohida beriladi.")
        self.assertFalse(cited_part.verified)

    def test_quote_spanning_two_articles_is_rejected(self):
        result = locate_citation(
            UZ_LAW,
            "1-modda",
            "Shartnomaning bir nusxasi xodimga beriladi. [Paragraph 7] 2-modda. Sinov muddati",
        )
        self.assertFalse(result.verified)

    def test_reference_without_article_is_rejected_inside_an_article(self):
        result = locate_citation(
            UZ_LAW, "[Paragraph 8]", "Sinov muddati uch oydan oshmasligi kerak."
        )
        self.assertFalse(result.verified)
        self.assertIn("2-modda", result.note)

    def test_wrong_page_marker_and_invented_words_are_rejected(self):
        text = "[Page 1]\nKirish qismi matni bu yerda.\n[Page 2]\nIkkinchi sahifa matni shu."
        self.assertTrue(locate_citation(text, "[Page 2]", "Ikkinchi sahifa matni shu.").verified)
        self.assertFalse(locate_citation(text, "[Page 1]", "Ikkinchi sahifa matni shu.").verified)
        self.assertFalse(
            locate_citation(text, "Soliq kodeksi, [Page 2]", "Ikkinchi sahifa matni shu.").verified
        )

    def test_reference_with_two_articles_is_rejected(self):
        self.assertFalse(
            locate_citation(UZ_LAW, "1-modda, 2-modda", "Mehnat shartnomasi yozma shaklda tuziladi.").verified
        )


class QuestionAnswerLabelTests(unittest.TestCase):
    def test_cyrillic_savol_javob(self):
        text = (
            "[Paragraph 1]\nСавол: Меҳнат шартномаси қандай тузилади?\n"
            "Жавоб: Ёзма шаклда.\n\n"
            "[Paragraph 2]\n2-савол: Синов муддати қанча?\nЖавоб: Уч ойгача."
        )
        pairs = extract_question_answer_pairs(text)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["question"], "Меҳнат шартномаси қандай тузилади?")
        self.assertEqual(pairs[0]["answer"], "Ёзма шаклда.")
        self.assertEqual(pairs[1]["question"], "Синов муддати қанча?")
        self.assertEqual(classify_document(text), "question_answer")

    def test_russian_vopros_otvet(self):
        text = (
            "[Line 1] Вопрос: Как заключается трудовой договор?\n"
            "[Line 2] Ответ: В письменной форме.\n"
            "[Line 3] 2. Вопрос №2: Каков испытательный срок?\n"
            "[Line 4] Ответ: До трех месяцев."
        )
        pairs = extract_question_answer_pairs(text)
        self.assertEqual(
            [(pair["question"], pair["answer"]) for pair in pairs],
            [
                ("Как заключается трудовой договор?", "В письменной форме."),
                ("Каков испытательный срок?", "До трех месяцев."),
            ],
        )
        self.assertEqual(pairs[0]["source_reference"], "[Line 1]–[Line 2]")

    def test_legal_basis_inside_an_answer_is_kept_verbatim_not_as_location(self):
        text = (
            "[Paragraph 1]\nSavol: Sinov muddati qancha?\n"
            "Javob: Mehnat kodeksining 112-moddasiga ko‘ra uch oygacha."
        )
        pair = extract_question_answer_pairs(text)[0]
        self.assertEqual(pair["source_reference"], "[Paragraph 1]")
        self.assertEqual(pair["legal_basis"], "112-moddasiga")
        self.assertEqual(find_inline_references(text), ["112-moddasiga"])


class LongAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Audit bug: one answer over 100 chars aborted the whole run."""

    async def test_long_answer_is_flagged_and_generation_continues(self):
        long_answer = (
            "Mehnat shartnomasi yozma shaklda, ikki nusxada tuziladi va har bir nusxa "
            "taraflar tomonidan imzolanadi hamda saqlanadi."
        )
        self.assertGreater(len(long_answer), 100)
        text = (
            "[Paragraph 1]\nSavol: Qisqa savol nima?\nJavob: Qisqa javob.\n\n"
            f"[Paragraph 2]\nSavol: Shartnoma qanday tuziladi?\nJavob: {long_answer}\n\n"
            "[Paragraph 3]\nSavol: Uchinchi savol nima?\nJavob: Uchinchi javob."
        )
        client = ScriptedClient(lambda prompt: {"distractors": ["X javob", "Y javob", "Z javob"]})
        result = await generate_questions(
            client,
            [{"id": 3, "title": "qa.txt", "content": text}],
            count=10,
            rng=random.Random(1),
        )
        self.assertEqual(result["sections_processed"], result["sections_total"])
        self.assertEqual(len(result["questions"]), 2)
        self.assertEqual(result["unsuitable_count"], 1)
        item = result["unsuitable_items"][0]
        self.assertEqual(item["original_answer"], long_answer)  # unchanged
        self.assertEqual(item["original_question"], "Shartnoma qanday tuziladi?")
        self.assertEqual(item["source_reference"], "[Paragraph 2]")
        self.assertIn("100", item["reason"])
        # No AI call is made for the unsuitable pair.
        self.assertEqual(len(client.prompts), 2)
        self.assertFalse(any(long_answer in prompt for prompt in client.prompts))

    def test_unsuitability_reasons(self):
        self.assertEqual(
            qa_poll_unsuitability({"original_question": "Q?", "original_answer": "A"}), ""
        )
        self.assertIn(
            "300",
            qa_poll_unsuitability({"original_question": "Q" * 301, "original_answer": "A"}),
        )

    def test_very_long_pair_no_longer_raises_while_splitting(self):
        long_answer = "Uzun javob matni. " * 1_000
        text = f"[Paragraph 1]\nSavol: Uzun savol?\nJavob: {long_answer}"
        _type, sections = build_source_sections("qa.txt", text, max_chars=500)
        self.assertEqual("".join(item["source_excerpt"] for item in sections), text)

    def test_skipped_items_are_stored_with_the_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = storage.DB_PATH
            storage.DB_PATH = Path(temp_dir) / "t.sqlite3"
            try:
                storage.initialize_database()
                run_id, _ = storage.save_generation_collection(
                    source_count=1,
                    sections_total=1,
                    questions=[],
                    duplicates_removed=0,
                    skipped_items=[
                        {
                            "source_id": 1,
                            "source_title": "qa.txt",
                            "source_reference": "[Paragraph 2]",
                            "original_question": "Savol?",
                            "original_answer": "Juda uzun javob",
                            "reason": "Asl javob 120 belgi",
                        }
                    ],
                )
                stored = storage.list_skipped_items(run_id)
                self.assertEqual(stored[0]["original_answer"], "Juda uzun javob")
                self.assertEqual(stored[0]["source_reference"], "[Paragraph 2]")
            finally:
                storage.DB_PATH = original


class AnswerRandomisationTests(unittest.IsolatedAsyncioTestCase):
    """Audit bug: the correct Q&A answer was always option A."""

    def test_shuffle_keeps_the_correct_answer(self):
        rng = random.Random(7)
        positions = Counter()
        for _ in range(400):
            options, index = shuffle_options(["right", "w1", "w2", "w3"], 0, rng)
            self.assertEqual(options[index], "right")
            self.assertEqual(sorted(options), ["right", "w1", "w2", "w3"])
            positions[index] += 1
        self.assertEqual(set(positions), {0, 1, 2, 3})
        self.assertTrue(all(count > 60 for count in positions.values()), positions)

    async def test_qa_and_prose_questions_are_shuffled(self):
        pairs = [
            ("Mehnat shartnomasi qanday shaklda tuziladi?", "Yozma shaklda"),
            ("Sinov muddati eng ko‘pi bilan qancha?", "Uch oy"),
            ("Haftalik ish vaqti chegarasi qancha?", "Qirq soat"),
            ("Yillik asosiy ta’til necha kun?", "O‘n besh kun"),
            ("Ish haqi qaysi valyutada to‘lanadi?", "So‘mda"),
            ("Shartnoma nusxasi kimga beriladi?", "Xodimga"),
            ("Tungi vaqt soat nechadan boshlanadi?", "Yigirma ikkidan"),
            ("Voyaga yetmaganlar uchun qisqartirilgan vaqt bormi?", "Ha, bor"),
        ]
        qa_text = "\n\n".join(
            f"[Paragraph {index}]\nSavol: {question}\nJavob: {answer}"
            for index, (question, answer) in enumerate(pairs, start=1)
        )

        def handler(prompt):
            answer = prompt.split("ASL TO‘G‘RI JAVOB: ", 1)[1].splitlines()[0]
            bank = json.loads(prompt.split("JAVOBLAR BANKI:\n", 1)[1].splitlines()[0])
            return {"distractors": [item for item in bank if item != answer][:3]}

        result = await generate_questions(
            ScriptedClient(handler),
            [{"id": 1, "title": "qa.txt", "content": qa_text}],
            count=8,
            rng=random.Random(3),
        )
        self.assertEqual(len(result["questions"]), 8)
        indexes = {question["correct_index"] for question in result["questions"]}
        self.assertGreater(len(indexes), 1)
        for question in result["questions"]:
            self.assertEqual(
                question["options"][question["correct_index"]], question["original_answer"]
            )
            self.assertEqual(question["question"], question["original_question"])


class SourcePreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_excerpt_offsets_and_location_link_back_to_the_source(self):
        source = {"id": 5, "title": "law.txt", "content": UZ_LAW}

        def handler(prompt):
            return {
                "questions": [
                    {
                        "question": "Mehnat shartnomasi qanday shaklda tuziladi?",
                        "options": ["Yozma", "Og‘zaki", "Elektron", "Notarial"],
                        "correct_index": 0,
                        "source_reference": "1-modda",
                        "source_quote": "Mehnat shartnomasi yozma shaklda tuziladi.",
                    }
                ]
                if "Mehnat shartnomasi yozma" in prompt
                else []
            }

        result = await generate_questions(
            ScriptedClient(handler), [source], count=1, section_chars=200, rng=random.Random(0)
        )
        question = result["questions"][0]
        self.assertEqual(question["review_status"], "READY_FOR_APPROVAL")
        self.assertEqual(question["source_reference"], "1-modda, 1-qism")
        self.assertEqual(
            UZ_LAW[question["section_start"]:question["section_end"]],
            question["source_excerpt"],
        )
        self.assertEqual(
            UZ_LAW[question["quote_start"]:question["quote_end"]],
            "Mehnat shartnomasi yozma shaklda tuziladi.",
        )
        self.assertEqual(
            (question["legal_article"], question["legal_part"]), ("1", "1")
        )
        self.assertEqual(question["options"][question["correct_index"]], "Yozma")

        with tempfile.TemporaryDirectory() as temp_dir:
            original = storage.DB_PATH
            storage.DB_PATH = Path(temp_dir) / "t.sqlite3"
            try:
                storage.initialize_database()
                source_id = storage.add_source("law.txt", UZ_LAW)
                question["source_ids"] = [source_id]
                _run, ids = storage.save_generation_collection(1, 1, [question], 0)
                saved = storage.get_question(ids[0])
                stored_source = storage.get_source(source_id)
                self.assertEqual(stored_source["content"], UZ_LAW)
                self.assertEqual(
                    stored_source["content"][saved["section_start"]:saved["section_end"]],
                    saved["source_excerpt"],
                )
                self.assertEqual(saved["legal_article"], "1")
                self.assertEqual(saved["source_quote"], question["source_quote"])
            finally:
                storage.DB_PATH = original

    async def test_ai_citing_wrong_article_is_flagged_not_corrected(self):
        def handler(prompt):
            return {
                "questions": [
                    {
                        "question": "Ish vaqti qancha?",
                        "options": ["40 soat", "36 soat", "48 soat", "30 soat"],
                        "correct_index": 0,
                        "source_reference": "1-modda",
                        "source_quote": "Ish vaqti haftasiga qirq soatdan oshmaydi.",
                    }
                ]
                if "qirq soatdan" in prompt
                else []
            }

        result = await generate_questions(
            ScriptedClient(handler),
            [{"id": 1, "title": "law.txt", "content": UZ_LAW}],
            count=1,
            section_chars=200,
        )
        question = result["questions"][0]
        self.assertFalse(question["source_verified"])
        self.assertEqual(question["review_status"], "NEEDS_REVIEW")
        self.assertEqual(question["source_reference"], "1-modda")  # never rewritten

    def test_check_citation_respects_section_scope(self):
        source = {"title": "law.txt", "content": UZ_LAW}
        start = UZ_LAW.index("[Paragraph 9]")
        outside = check_citation(
            "law.txt",
            "1-modda",
            "Mehnat shartnomasi yozma shaklda tuziladi.",
            [source],
            scope=(start, len(UZ_LAW)),
        )
        self.assertFalse(outside.verified)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "meta.sqlite3"
        storage.initialize_database()

    def tearDown(self):
        storage.DB_PATH = self.original
        self.temp_dir.cleanup()

    def test_parse_metadata_in_three_languages(self):
        parsed = parse_source_metadata(
            "Nomi: O‘zbekiston Respublikasining Mehnat kodeksi\n"
            "Raqami: O‘RQ-798\nSana: 28.10.2022\nHavola: https://lex.uz/docs/6257288"
        )
        self.assertEqual(parsed["adoption_date"], "2022-10-28")
        self.assertEqual(parsed["document_number"], "O‘RQ-798")
        self.assertEqual(
            parse_source_metadata("Название: Трудовой кодекс\nДата: 2022-10-28"),
            {"official_title": "Трудовой кодекс", "adoption_date": "2022-10-28"},
        )
        self.assertEqual(parse_source_metadata("Номи: Кодекс"), {"official_title": "Кодекс"})
        self.assertEqual(parse_source_metadata("Havola: -"), {"source_url": ""})

    def test_invalid_metadata_is_rejected(self):
        for text in (
            "Sana: 31.02.2022",
            "Havola: http://lex.uz/docs/1",
            "Havola: lex.uz",
            "Muallif: Kimdir",
            "",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_source_metadata(text)

    def test_metadata_update_never_changes_document_text(self):
        source_id = storage.add_source("kodeks.docx", UZ_LAW)
        self.assertTrue(
            storage.update_source_metadata(
                source_id, official_title="Mehnat kodeksi", document_number="O‘RQ-798"
            )
        )
        source = storage.get_source(source_id)
        self.assertEqual(source["content"], UZ_LAW)
        self.assertEqual(source["official_title"], "Mehnat kodeksi")
        with self.assertRaises(ValueError):
            storage.update_source_metadata(source_id, content="changed")

    def test_display_and_poll_explanation_use_admin_metadata_within_limits(self):
        source = {
            "official_title": "O‘zbekiston Respublikasining Mehnat kodeksi",
            "document_number": "O‘RQ-798",
            "adoption_date": "2022-10-28",
            "source_url": "https://lex.uz/docs/6257288",
        }
        self.assertEqual(
            source_display_name(source, "kodeks.docx"),
            "O‘zbekiston Respublikasining Mehnat kodeksi (№ O‘RQ-798, 28.10.2022)",
        )
        question = {
            "source_title": "kodeks.docx",
            "source_reference": "1-modda, 1-qism",
            "legal_basis": "",
        }
        explanation = build_poll_explanation(question, source)
        self.assertIn("Mehnat kodeksi", explanation)
        self.assertIn("1-modda, 1-qism", explanation)
        self.assertLessEqual(len(explanation), POLL_EXPLANATION_MAX_CHARS)
        long_source = dict(source, official_title="Juda uzun nom " * 30)
        self.assertLessEqual(
            len(build_poll_explanation(question, long_source)), POLL_EXPLANATION_MAX_CHARS
        )
        self.assertEqual(
            build_poll_explanation(question, None), "Manba: kodeks.docx, 1-modda, 1-qism"
        )

    def test_review_card_shows_metadata_and_original_pair_warning(self):
        question = {
            "id": 1,
            "status": "pending",
            "review_status": "READY_FOR_APPROVAL",
            "question": "Tahrirlangan savol?",
            "options": ["A", "B", "C", "D"],
            "correct_index": 1,
            "source_title": "qa.txt",
            "source_reference": "[Paragraph 1]",
            "source_quote": "Savol: Asl savol? Javob: B",
            "original_question": "Asl savol?",
            "original_answer": "B",
            "legal_basis": "5-moddasi",
        }
        self.assertTrue(differs_from_original_pair(question))
        card = format_question(question, {"official_title": "Qonun", "source_url": "https://lex.uz/x"})
        self.assertIn("Qonun", card)
        self.assertIn("https://lex.uz/x", card)
        self.assertIn("asl matndan farq qiladi", card)
        self.assertIn("5-moddasi", card)


class MigrationSafetyTests(unittest.TestCase):
    def test_legacy_database_is_backed_up_and_rows_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = storage.DB_PATH
            storage.DB_PATH = Path(temp_dir) / "legacy.sqlite3"
            try:
                with sqlite3.connect(storage.DB_PATH) as connection:
                    connection.execute(
                        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "title TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO sources (title, content, created_at) VALUES (?, ?, ?)",
                        ("old.txt", "Eski matn", "2026-01-01"),
                    )
                storage.initialize_database()
                backups = list(Path(temp_dir).glob("legacy.backup-*.sqlite3"))
                self.assertEqual(len(backups), 1)
                with sqlite3.connect(backups[0]) as backup:
                    self.assertEqual(
                        backup.execute("SELECT content FROM sources").fetchone()[0],
                        "Eski matn",
                    )
                source = storage.get_source(1)
                self.assertEqual(source["content"], "Eski matn")
                self.assertEqual(source["official_title"], "")
                storage.initialize_database()  # already current: no second backup
                self.assertEqual(len(list(Path(temp_dir).glob("legacy.backup-*"))), 1)
            finally:
                storage.DB_PATH = original

    def test_new_database_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = storage.DB_PATH
            storage.DB_PATH = Path(temp_dir) / "fresh.sqlite3"
            try:
                storage.initialize_database()
                self.assertEqual(list(Path(temp_dir).glob("*.backup-*")), [])
            finally:
                storage.DB_PATH = original


class ConfigurationSafetyTests(unittest.TestCase):
    """Phase 0: identifiers come from the environment, not tracked files."""

    def test_replit_file_has_no_admin_or_channel_values(self):
        replit = (REPO_DIR / ".replit").read_text(encoding="utf-8")
        for line in replit.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotRegex(stripped, r"^(ADMIN_ID|CHANNEL_ID)\s*=")

    def test_env_example_contains_placeholders_only(self):
        example = (BOT_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertNotRegex(example, r"\d{8,}:[A-Za-z0-9_-]{30,}")
        self.assertNotRegex(example, r"sk-[A-Za-z0-9]{20,}")

    def test_identifier_validation(self):
        self.assertEqual(parse_admin_id(" 12345 "), 12345)
        self.assertEqual(parse_channel_id("@my_channel"), "@my_channel")
        self.assertEqual(parse_channel_id("-1001234567890"), "-1001234567890")
        for bad in ("abc", "12.5", ""):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                parse_admin_id(bad)
        for bad in ("mychannel", "@a", "12345"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                parse_channel_id(bad)

    def test_database_path_can_point_to_a_persistent_disk(self):
        script = (
            "import storage, sys; sys.stdout.write(str(storage.DB_PATH))"
        )
        environment = dict(os.environ, QUIZ_BOT_DB_PATH="/data/quiz.sqlite3")
        output = subprocess.run(
            [sys.executable, "-c", script],
            cwd=BOT_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(output, "/data/quiz.sqlite3")

    @unittest.skipUnless(shutil.which("git") and (REPO_DIR / ".git").exists(), "git checkout needed")
    def test_secrets_databases_and_uploads_are_ignored_by_git(self):
        paths = [
            ".env",
            "telegram-quiz-bot/.env",
            "telegram-quiz-bot/quiz_bot.sqlite3",
            "telegram-quiz-bot/quiz_bot.sqlite3-wal",
            "telegram-quiz-bot/quiz_bot.backup-20260101T000000Z.sqlite3",
            "uploads/law.pdf",
            "telegram-quiz-bot/temp/x.txt",
        ]
        for path in paths:
            with self.subTest(path=path):
                completed = subprocess.run(
                    ["git", "check-ignore", "-q", "--no-index", path], cwd=REPO_DIR
                )
                self.assertEqual(completed.returncode, 0, f"{path} is not ignored")
        example = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "telegram-quiz-bot/.env.example"],
            cwd=REPO_DIR,
        )
        self.assertNotEqual(example.returncode, 0)


if __name__ == "__main__":
    unittest.main()
