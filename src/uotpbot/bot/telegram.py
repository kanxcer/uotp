"""Telegram transport.

A deliberately thin shell: it authenticates the update, hands the text to
:class:`CommandRouter`, and sends the reply. All logic lives in ``commands.py``
so it can be tested without a network.

``python-telegram-bot`` is an optional dependency. Importing this module
without it raises a clear error instead of an ImportError from deep inside.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from .commands import CommandRouter

__all__ = ["TelegramFrontend", "run_bot", "build_from_settings"]

log = logging.getLogger("uotpbot.telegram")

try:  # pragma: no cover - exercised only when the dependency is installed
    from telegram import Update
    from telegram.ext import (  # noqa: F401  -- presence is the availability probe
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    HAS_TELEGRAM = True
except ImportError:  # pragma: no cover
    HAS_TELEGRAM = False


class TelegramFrontend:
    """Bridges python-telegram-bot updates to the command router."""

    def __init__(self, router: CommandRouter) -> None:
        self.router = router

    async def on_message(self, update: Any, context: Any = None) -> None:
        """Handle any incoming message and reply with the router's answer."""
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is None or not getattr(message, "text", None):
            return
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", "")) if user else ""
        if not user_id:
            return
        reply = self.router.handle(user_id, message.text)
        await message.reply_text(reply.text)


def build_from_settings(settings: Settings, router_factory: Any) -> Any:
    """Construct the PTB application from settings.

    ``router_factory`` is a zero-argument callable returning a
    :class:`CommandRouter`; passing it in keeps this module free of database
    and provider wiring, which lives in ``__main__``.
    """
    if not HAS_TELEGRAM:
        raise RuntimeError(
            "python-telegram-bot is not installed. Run: pip install 'uotpbot[telegram]'"
        )
    frontend = TelegramFrontend(router_factory())
    app = Application.builder().token(settings.require_telegram()).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, frontend.on_message))
    app.add_handler(MessageHandler(filters.COMMAND, frontend.on_message))
    return app


def run_bot(settings: Settings, router_factory: Any) -> None:  # pragma: no cover
    """Start long-polling. Blocking; meant for a real deployment."""
    app = build_from_settings(settings, router_factory)
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
