# Telegram AI Quiz Bot

A small Python Telegram bot that reads administrator-provided documents or text,
creates Uzbek multiple-choice questions with OpenAI, and sends Telegram Quiz
Polls to a channel. Generated questions stay private and pending until an
administrator approves them.

## What it does

- Reads PDF, DOCX, and TXT files, or plain text sent in the bot chat.
- Creates four-option questions in Uzbek using only the saved source material.
- Requires an exact source quote and citation that can be found in the source.
- Lets the administrator review, edit, approve, or reject every question.
- Stores sources, questions, approvals, and schedule settings in SQLite.
- Publishes only approved questions, one at each configured daily time.
- Uses `Asia/Tashkent` as the default schedule timezone.

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
- `/generate` — create five draft questions from saved sources.
- `/generate 3` — create three draft questions.
- `/questions` — review pending questions.
- `/questions approved` — list approved questions waiting for the schedule.
- `/questions published` — list recently published questions.
- `/schedule` — show and update the daily publishing schedule.
- `/schedule 2 09:00,18:00` — publish one approved quiz at each time, daily.
- `/help` — show help.

Schedule times use 24-hour `HH:MM` format and `Asia/Tashkent` time by default.
The number of times must equal the number of daily quizzes. For example,
`/schedule 1 10:30` sets one quiz per day. If there are no approved questions
when a scheduled time arrives, nothing is published.

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

The source reference and quote must still match an uploaded source. If the
document has no article or section labels, the bot uses extracted page,
paragraph, or line markers rather than inventing a legal citation.

## Files

- `bot.py` — Telegram commands, approval buttons, and scheduled publishing.
- `quiz_generator.py` — PDF/DOCX/TXT extraction and OpenAI question generation.
- `storage.py` — SQLite persistence.
- `requirements.txt` — Python dependencies.

The SQLite database is created automatically next to the bot code as
`quiz_bot.sqlite3`. It is excluded from Git. Keep a backup if you need to
preserve it across workspace changes.
