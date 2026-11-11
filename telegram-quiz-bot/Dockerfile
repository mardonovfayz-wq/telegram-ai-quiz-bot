# Telegram AI Quiz Bot — container image for a self-hosted server.
# Secrets are NOT baked in: pass them as environment variables at runtime.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUIZ_BOT_DB_PATH=/data/quiz_bot.sqlite3 \
    TIMEZONE=Asia/Tashkent \
    HOOK_HOST=0.0.0.0 \
    HOOK_PORT=8080

WORKDIR /app

RUN useradd --create-home --uid 10001 quizbot \
    && mkdir -p /data \
    && chown quizbot:quizbot /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=quizbot:quizbot . .

USER quizbot
VOLUME ["/data"]
EXPOSE 8080

# Health check only when the HTTP hook is enabled (HOOK_TOKEN set).
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0) if not os.getenv('HOOK_TOKEN') else urllib.request.urlopen('http://127.0.0.1:' + os.getenv('HOOK_PORT','8080') + '/healthz', timeout=5)"

CMD ["python", "bot.py"]
