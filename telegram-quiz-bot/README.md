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
  legal text and general material. Original Q&A wording and answers are retained;
  normative text is chunked around legal headings and source markers.
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
2. In **Tools → Secrets**, add:
   - `TELEGRAM_BOT_TOKEN` — create a bot with Telegram's `@BotFather`.
   - `OPENAI_API_KEY` — an OpenAI API key.
3. In Replit's environment variables, set:
   - `ADMIN_ID` — your numeric Telegram user ID. `@userinfobot` can show it.
   - `CHANNEL_ID` — the channel username, such as `@mychannel`, or its numeric
     channel ID, usually beginning with `-100`.
4. Add the bot to the destination channel as an administrator with permission
   to post messages.
5. Start the workflow. In a private chat with the bot, send `/start`.

Do not paste real tokens into source files or commit them. The `.env.example`
file contains placeholders only.

## Admin commands

- `/start` — start and show the command list.
- `/upload` — enter source-upload mode, then send a PDF, DOCX, TXT, or plain text.
- `/generate` — process every saved section; request a target of ten questions.
- `/generate 3` — process every saved section; request a target of three questions.
- `/questions` — review the first page of pending questions.
- `/questions pending 2` — review the second page of pending questions.
- `/questions approved` — list approved questions waiting for the schedule.
- `/questions published 2` — list the second page of published questions.
- `/schedule` — show and update the daily publishing schedule.
- `/schedule 2 09:00,18:00` — publish one approved quiz at each time, daily.
- `/help` — show help.

Schedule times use 24-hour `HH:MM` format and `Asia/Tashkent` time by default.
The number of times must equal the number of daily quizzes. For example,
`/schedule 1 10:30` sets one quiz per day. If there are no approved questions
when a scheduled time arrives, nothing is published.

The requested question count is a target. If the document has more sections than
the target, the bot still checks every section and may create more questions so
relevant later sections are not ignored. Sections with no supported quiz question
are counted and reported rather than silently omitted. Generation is on-demand;
`/generate` defaults to ten drafts, while `/schedule` continues to control only
the publication of questions that an administrator has approved.

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

The source reference and quote must still match the uploaded excerpt. If the
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
- `tests/test_large_documents.py` — offline chunking, Q&A preservation, citation,
  retry/repair, deduplication, migration, and approval-gate tests.
- `requirements.txt` — Python dependencies.

The SQLite database is created automatically next to the bot code as
`quiz_bot.sqlite3`. It is excluded from Git. Keep a backup if you need to
preserve it across workspace changes.

## Run the offline checks

From the Replit Shell, run:

```bash
python3 -m unittest discover -s telegram-quiz-bot/tests -v
```

These checks use a fake OpenAI response; they do not make paid API calls or
publish messages to Telegram.
