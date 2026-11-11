# Telegram AI Quiz Bot

A small Python Telegram bot that reads administrator-provided documents or text,
creates Uzbek multiple-choice questions with OpenAI, and sends Telegram Quiz
Polls to a channel. Generated questions stay private and pending until an
administrator approves them.

## What it does

- Reads PDF, DOCX, and TXT files, or plain text sent in the bot chat.
- Splits long documents into ordered sections while retaining page, paragraph,
  heading, article, and paragraph-number context. There is no 45,000-character
  text limit.
- Sends every section to OpenAI separately. It checks complete text coverage and
  reports the number of sections processed.
- Distinguishes explicitly labeled question–answer documents from normative
  legal text and general material. Q&A labels are recognised in Uzbek Latin
  (`Savol:` / `Javob:`), Uzbek Cyrillic (`Савол:` / `Жавоб:`), Russian
  (`Вопрос:` / `Ответ:`) and English. Original Q&A wording and answers are
  retained; normative text is chunked around legal headings and source markers.
- Reads the article → part → clause structure of normative text in Uzbek Latin
  (`modda`, `qism`, `band`), Uzbek Cyrillic (`модда`, `қисм`, `банд`) and
  Russian (`статья`, `часть`, `пункт`). A citation is accepted only if the
  supporting quote really sits in the cited article (and part/clause, when
  cited). Numbers are matched strictly, so `1-modda` never matches `21-modda`,
  and ordinary words such as `moddaning` or `band bo‘lgan` are not citations.
- Stores, for every draft, the exact section offsets and quote offsets in the
  uploaded source, plus the located article, part and clause.
- Randomises the position of the correct answer in every quiz.
- Original Q&A pairs too long for a Telegram poll (question over 300 or answer
  over 100 characters) are never shortened: they are reported as unsuitable and
  stored unchanged, and generation continues with the other sections.
- Creates four-option questions in Uzbek using only the current section. The
  exact source excerpt, document type, citation, and any original Q&A pair are
  stored with each draft in SQLite.
- Checks questions for exact and near duplicates against the batch and recent
  saved questions.
- Marks uncertain source, mixed-document, or ungrounded-distractor drafts as
  `NEEDS_REVIEW`. They cannot be approved until an administrator checks and edits
  them; source evidence is never automatically rewritten.
- Lets the administrator review, edit, approve, or reject every question.
- Stores sources, generation collections, questions, approvals, and schedule
  settings in SQLite.
- Publishes only approved questions, one at each configured daily time.
- Uses `Asia/Tashkent` as the default schedule timezone.
- Retries OpenAI rate limits, connection errors, and timeouts. If any section
  still fails, the bot reports its progress and does not save an incomplete run.
- If an AI response has a malformed structure, the bot makes one format-only
  repair request. If that also fails, the run stops; it never guesses or repairs
  legal citations automatically.

Telegram file downloads are limited to 20 MB per file. The removed 45,000
character limit applied to extracted text, not the raw Telegram file size.

## Run it in Replit

1. Open this project in Replit. The bot uses the `Telegram Quiz Bot` workflow.
   Start that workflow to run the bot; keep it running while you want the bot
   to receive messages and publish scheduled polls.
2. In **Tools → Secrets**, add all four values (none of them is stored in
   tracked files such as `.replit`):
   - `TELEGRAM_BOT_TOKEN` — create a bot with Telegram's `@BotFather`.
   - `OPENAI_API_KEY` — an OpenAI API key.
   - `ADMIN_ID` — your numeric Telegram user ID. `@userinfobot` can show it.
   - `CHANNEL_ID` — the channel username, such as `@mychannel`, or its numeric
     channel ID, usually beginning with `-100`.
   Optional: `QUIZ_BOT_DB_PATH` — full path of the SQLite file on a persistent
   disk (default: `telegram-quiz-bot/quiz_bot.sqlite3`).
4. Add the bot to the destination channel as an administrator with permission
   to post messages.
5. Start the workflow. In a private chat with the bot, send `/start`.

Do not paste real tokens into source files or commit them. The `.env.example`
file contains placeholders only.

## Admin commands

- `/start` — start and show the command list.
- `/upload` — enter source-upload mode, then send a PDF, DOCX, TXT, or plain text.
- `/generate` — create up to ten drafts from the next **unused** sections.
- `/generate 3` — the same, up to three drafts.
- `/generate full 10` — the previous behaviour: re-process every section of
  every source.
- `/coverage` — per source: sections used, without questions, unsuitable Q&A,
  failed (and failed for good after 3 attempts) and still waiting.
- `/recheck` — re-check the citations of pending and approved questions with
  the current rules. Read-only: it only reports, nothing is changed.
- `/questions` — review the first page of pending questions.
- `/questions pending 2` — review the second page of pending questions.
- `/questions approved` — list approved questions waiting for the schedule.
- `/questions published 2` — list the second page of published questions.
- `/sources` — list uploaded sources with their legal-act metadata status.
- `/setmeta 3` — enter the official title, number, adoption date and source URL
  of source 3. These values are typed by the administrator; the bot never
  fills them in itself. They appear on review cards and in the poll explanation.
- `/schedule` — show and update the daily publishing schedule.
- `/schedule 2 09:00,18:00` — publish one approved quiz at each time, daily.
- `/autogen` — daily 09:00 (Asia/Tashkent) draft generation: `status`, `on`,
  `off`, `run` (manual test run now), `trigger internal|activepieces`.
  **Off by default.** It creates up to 10 drafts once per day and never pads
  the number; drafts still need approval.
- `/queue` — counts per status, stuck publications and queue warnings.
- `/audit [n]` or `/audit #ID` — audit log of generate/approve/edit/reject/
  unapprove/publish events with who and when.
- `/unapprove <ID> [reason]` — send an approved or held question back to review.
- `/resolve <ID> published [message_id]` or `/resolve <ID> approved` — settle a
  publication whose result was unknown (see below).
- `/help` — show help.

Schedule times use 24-hour `HH:MM` format and `Asia/Tashkent` time by default.
The number of times must equal the number of daily quizzes. For example,
`/schedule 1 10:30` sets one quiz per day. If there are no approved questions
when a scheduled time arrives, nothing is published.

### How `/generate` chooses material

The bot keeps a ledger of every section of every source. `/generate N` walks
through sections that have not been used yet, in document order (oldest source
first), and stops once it has N drafts; the next run continues where it
stopped, so the same material is not used twice. Uploading exactly the same
text again is refused.

If a section fails (for example an OpenAI timeout), the drafts already made
are still saved, the section is marked as failed and retried on a later run
(at most 3 attempts in total). After 3 failures in a row the run stops early
and keeps what it has. Generation runs in the background, so other commands
keep working; only one generation can run at a time.

Every new draft gets a second, independent AI check: does the quote alone
support the marked answer? If not, or if the check itself fails, the draft is
marked NEEDS_REVIEW. This check can only make a draft stricter; it never
approves or changes anything. Q&A drafts skip it because the source states the
question and answer itself. Nothing is generated automatically on a schedule.

### `/generate full`

The requested question count is a target. If the document has more sections than
the target, the bot still checks every section and may create more questions so
relevant later sections are not ignored. Sections with no supported quiz question
are counted and reported rather than silently omitted. Generation is on-demand;
`/generate` defaults to ten drafts, while `/schedule` continues to control only
the publication of questions that an administrator has approved.

## Safe publishing and alerts

A scheduled slot claims one approved question atomically, sends it, then marks
it published, so the same quiz cannot be posted twice. If Telegram refuses the
send, the question returns to the queue. If the result is unknown (timeout,
network error), it stays in `publishing`, is never resent automatically, and
the administrator is asked to check the channel and run `/resolve`. An approved
question that matches an already published one is put on `held`.

The administrator gets a Telegram message for: unhandled errors, failed or
partial daily generation (with the shortfall), an empty or low publishing
queue, refused or uncertain sends, and held duplicates. Repeated alerts are
suppressed for an hour.

Running on your own server, the optional Activepieces hook and health check are
described in `deploy/README.md`.

## Privacy of the control chat

The bot only reacts to messages in a private chat with the administrator.
Channel posts, group messages and edited messages never reach its handlers, and
it never replies inside a channel or group.

## Question review

Each draft has **Tasdiqlash** (approve), **Tahrirlash** (edit), and
**Rad etish** (reject) buttons. Approval only puts the question in the
publishing queue; it does not publish it immediately. Telegram's Quiz Poll
marks the correct answer, and the poll explanation includes the source citation.

To edit, press **Tahrirlash** and send the requested format in the private chat:

```text
Savol: Savol matni
A) Birinchi javob
B) Ikkinchi javob
C) Uchinchi javob
D) To‘rtinchi javob
To‘g‘ri javob: B
Manba: fayl_nomi.pdf | 3-modda
Iqtibos: manbadan aynan olingan jumla
```

The source reference and quote must still match the uploaded excerpt. Write the
reference as the article (and part/clause if you want them), for example
`3-modda`, `3-modda, 2-qism, 1-band`, `3-модда, 2-қисм` or
`Статья 3, часть 2, пункт 1`. In PDF text, line breaks are page layout, so the
bot cannot count unnumbered parts; cite only the article there unless the parts
are numbered. If the
document has no article or section labels, the bot uses extracted page,
paragraph, or line markers rather than inventing a legal citation. If a reference,
quote, document classification, or Q&A distractor cannot be verified, the draft
is labeled **NEEDS_REVIEW** and its approval button is disabled. Edit it to match
the source before approving it.

Q&A extraction recognizes explicit labels such as `Savol:` / `Javob:` or
`Question:` / `Answer:`. Those original question and answer strings are retained
in SQLite, and the answer stays the correct option. Distractors not found in the
document's answer bank are flagged for administrator review.

## Files

- `bot.py` — Telegram commands, approval buttons, and scheduled publishing.
- `quiz_generator.py` — PDF/DOCX/TXT extraction and OpenAI question generation.
- `storage.py` — SQLite persistence.
- `legal_refs.py` — strict legal locators, article/part/clause structure and
  citation checks.
- `bot_helpers.py` — parsing and formatting used by the bot (testable offline).
- `pipeline.py` — incremental generation with the section ledger, and the
  read-only citation re-check.
- `tests/test_pipeline.py` — ledger, incremental runs, failure tolerance,
  answer-support check, duplicate uploads and the read-only re-check.
- `tests/test_large_documents.py` — offline chunking, Q&A preservation, citation,
  retry/repair, deduplication, migration, and approval-gate tests.
- `tests/test_legal_accuracy.py` — strict article matching, three-script legal
  terms and Q&A labels, long Q&A answers, answer randomisation, excerpt
  preservation, metadata, migration backups and configuration safety.
- `tests/test_bot_handlers.py` — private-chat-only handler tests (need
  `python-telegram-bot`; they run in GitHub Actions).
- `requirements.txt` — Python dependencies.

The SQLite database is created automatically next to the bot code as
`quiz_bot.sqlite3` (or at `QUIZ_BOT_DB_PATH`). It is excluded from Git. Before
the bot adds new columns to an existing database, it saves a full copy next to
it as `quiz_bot.backup-<UTC time>.sqlite3`; schema updates only add columns and
tables and never change existing rows. Keep your own backup as well.

## Run the offline checks

From the Replit Shell, run:

```bash
python3 -m unittest discover -s telegram-quiz-bot/tests -v
```

These checks use a fake OpenAI response; they do not make paid API calls or
publish messages to Telegram. GitHub Actions (`.github/workflows/tests.yml`)
runs the same suite on every push and pull request, and fails if a database,
`.env` file or upload folder is ever committed. It uses no secrets and deploys
nothing.
