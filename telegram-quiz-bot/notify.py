"""Administrator notifications with duplicate suppression (no Telegram import)."""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)
DEFAULT_WINDOW_SECONDS = 60 * 60


class AdminNotifier:
    """Sends short alerts to the administrator.

    The same ``key`` is sent at most once per ``window_seconds`` so a repeating
    problem (for example every publish slot finding an empty queue) does not
    flood the chat. A failure to send is logged and never raises.
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._window = window_seconds
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    async def notify(self, text: str, key: str | None = None) -> bool:
        if key is not None:
            last = self._last_sent.get(key)
            now = self._clock()
            if last is not None and now - last < self._window:
                return False
            self._last_sent[key] = now
        try:
            await self._send(text[:4000])
            return True
        except Exception as error:  # notifications must never break the bot
            logger.warning("Could not notify the administrator: %s", type(error).__name__)
            return False
