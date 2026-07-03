"""Thread-safe outbound text notices via the Telegram Bot API.

Discovery and tailoring run in executor threads, so their progress and results
can't go through the bot's event loop. This notifier posts ``sendMessage``
directly with stdlib urllib — safe from any thread, chunked to Telegram's
message limit, and it never raises: a lost notice must not kill the run that
sent it. Documents (the tailored PDF) are NOT sent here; those go through the
bot object on the event loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("cvflow.app.notify")

__all__ = ["TelegramNotifier"]

_MAX_MSG = 4000  # Telegram's hard limit is 4096; leave headroom.


def _urllib_post(url: str, data: dict[str, str]) -> None:
    req = Request(url, data=urlencode(data).encode(), method="POST")  # noqa: S310 (https only)
    with urlopen(req, timeout=30):  # noqa: S310 (https only)
        pass


def _chunks(text: str) -> list[str]:
    """Split into <=4000-char pieces, preferring newline boundaries."""
    out: list[str] = []
    rest = text
    while len(rest) > _MAX_MSG:
        cut = rest.rfind("\n", 0, _MAX_MSG)
        if cut <= 0:
            cut = _MAX_MSG
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


class TelegramNotifier:
    """Callable that pushes a text message to the authorized chat. Never raises."""

    def __init__(
        self,
        bot_token: str,
        chat_id: int,
        *,
        poster: Callable[[str, dict[str, str]], None] = _urllib_post,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._poster = poster

    def __call__(self, message: str) -> None:
        for chunk in _chunks(message):
            try:
                self._poster(self._url, {"chat_id": str(self._chat_id), "text": chunk})
            except Exception as exc:  # noqa: BLE001 — a notice must not crash the caller
                logger.warning("telegram sendMessage failed (%s); message was: %s", exc, chunk)
