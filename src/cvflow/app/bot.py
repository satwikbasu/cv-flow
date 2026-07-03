"""The python-telegram-bot application: handler registration only.

Every text message and slash command funnels through the one forgiving-parser
handler; button taps go to the callback handler. Auth (a single allowed user
id) is checked inside the handlers, so an unknown sender gets silence, not an
error.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from cvflow.app.buttons import on_button
from cvflow.app.commands import Services, on_text

__all__ = ["build_application"]


def build_application(
    bot_token: str,
    services: Services,
    *,
    post_init: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
) -> Application:  # type: ignore[type-arg]
    builder = Application.builder().token(bot_token)
    if post_init is not None:
        builder = builder.post_init(post_init)
    application = builder.build()
    application.bot_data["services"] = services
    application.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, on_text))
    application.add_handler(CallbackQueryHandler(on_button))
    return application
