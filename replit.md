# Telegram AI Quiz Bot

A beginner-friendly Python Telegram bot that creates source-grounded Uzbek quiz questions, requires administrator approval, and publishes approved quizzes to a Telegram channel.

## Run & Operate

- Start the `Telegram Quiz Bot` workflow to run `python3 telegram-quiz-bot/bot.py`.
- Set `TELEGRAM_BOT_TOKEN` and `OPENAI_API_KEY` as Replit Secrets.
- Set `ADMIN_ID` and `CHANNEL_ID` as Replit environment variables.
- The bot creates and uses `telegram-quiz-bot/quiz_bot.sqlite3`.

## Stack

- Python 3.11
- Telegram: `python-telegram-bot`
- AI: OpenAI Python SDK
- Persistence: SQLite
- Document extraction: pypdf and python-docx

## Where things live

- `telegram-quiz-bot/bot.py` — commands, approvals, and scheduled channel polls
- `telegram-quiz-bot/quiz_generator.py` — file reading and source-checked AI generation
- `telegram-quiz-bot/storage.py` — SQLite operations
- `telegram-quiz-bot/README.md` — setup and use instructions

## Architecture decisions

- Generated questions stay pending until an administrator approves them.
- Approval queues a question for the next configured schedule time; it does not publish immediately.
- The bot verifies that each generated citation and supporting quote appear in the selected uploaded source.

## Product

Admins upload PDF, DOCX, TXT, or plain text, review generated Uzbek multiple-choice drafts, approve them, and configure daily Telegram Quiz Poll publication times.

## User preferences

- Keep the bot project beginner-friendly and store its credentials outside source code.

## Gotchas

- Add the bot as a channel administrator with permission to post polls.
- Schedule times use `Asia/Tashkent` unless the `TIMEZONE` environment variable overrides it.
- A scheduled time with no approved question does not publish anything.

## Pointers

- See `telegram-quiz-bot/README.md` for configuration and administrator commands.
