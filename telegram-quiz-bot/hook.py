"""Tiny HTTP endpoint so Activepieces (or a health monitor) can reach the bot.

Disabled unless HOOK_TOKEN is set. Endpoints:
- GET  /healthz                    -> 200 {"status": "ok"}   (no secrets, no data)
- POST /hooks/daily-generation     -> 202 accepted / 409 not run / 401 bad token
  Requires "Authorization: Bearer <HOOK_TOKEN>". The generation itself runs in
  the background; results go to the administrator in Telegram.

Uses only the Python standard library (no new dependency).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 4096
READ_TIMEOUT_SECONDS = 10
MIN_TOKEN_LENGTH = 24

DailyCallback = Callable[[], Awaitable[dict[str, Any]]]


def token_is_strong(token: str) -> bool:
    return len(token) >= MIN_TOKEN_LENGTH


def _response(status: int, payload: dict[str, Any]) -> bytes:
    reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
               404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
               413: "Payload Too Large", 500: "Internal Server Error"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body


async def handle_request(
    method: str,
    path: str,
    headers: dict[str, str],
    token: str,
    on_daily: DailyCallback,
) -> tuple[int, dict[str, Any]]:
    path = path.split("?", 1)[0]
    if path == "/healthz":
        if method != "GET":
            return 405, {"error": "method not allowed"}
        return 200, {"status": "ok"}
    if path == "/hooks/daily-generation":
        if method != "POST":
            return 405, {"error": "method not allowed"}
        supplied = headers.get("authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return 401, {"error": "unauthorized"}
        try:
            outcome = await on_daily()
        except Exception as error:  # never leak details over HTTP
            logger.warning("Daily hook failed: %s", type(error).__name__)
            return 500, {"error": "internal error"}
        if outcome.get("accepted"):
            return 202, {"status": "accepted"}
        return 409, {"status": "not_started", "reason": outcome.get("reason", "")}
    return 404, {"error": "not found"}


async def _serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    token: str,
    on_daily: DailyCallback,
) -> None:
    try:
        raw_head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=READ_TIMEOUT_SECONDS
        )
        if len(raw_head) > MAX_HEADER_BYTES:
            writer.write(_response(413, {"error": "headers too large"}))
            return
        lines = raw_head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            writer.write(_response(400, {"error": "bad request"}))
            return
        method, path, _version = parts
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY_BYTES:
            writer.write(_response(413, {"error": "body too large"}))
            return
        if length:
            await asyncio.wait_for(reader.readexactly(length), timeout=READ_TIMEOUT_SECONDS)
        status, payload = await handle_request(method.upper(), path, headers, token, on_daily)
        writer.write(_response(status, payload))
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
        writer.write(_response(400, {"error": "bad request"}))
    finally:
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()


async def start_hook_server(
    host: str, port: int, token: str, on_daily: DailyCallback
) -> asyncio.AbstractServer:
    if not token_is_strong(token):
        raise RuntimeError(f"HOOK_TOKEN must be at least {MIN_TOKEN_LENGTH} characters.")

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _serve_connection(reader, writer, token, on_daily)

    server = await asyncio.start_server(handler, host, port, limit=MAX_HEADER_BYTES)
    logger.info("Hook endpoint listening on %s:%s", host, port)
    return server
