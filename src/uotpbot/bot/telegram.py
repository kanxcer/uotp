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
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (  # noqa: F401  -- presence is the availability probe
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    HAS_TELEGRAM = True
except ImportError:  # pragma: no cover
    HAS_TELEGRAM = False


def _reply_markup(reply) -> object:
    """Turn a Reply's (label, callback_data) pairs into an inline keyboard.

    One button per row: labels like "My own API (5% of each sale)" are too
    long to sit side by side. Returns None when there are no buttons, which
    is what python-telegram-bot expects for a plain message.
    """
    if not HAS_TELEGRAM or not getattr(reply, "buttons", ()):
        return None
    rows = [
        [InlineKeyboardButton(label, callback_data=data)]
        for label, data in reply.buttons
    ]
    return InlineKeyboardMarkup(rows)


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
        await message.reply_text(reply.text, reply_markup=_reply_markup(reply))

    async def on_callback(self, update: Any, context: Any = None) -> None:
        """Handle an inline-keyboard press: advance or cancel a pending flow.

        The message is edited in place so the answered menu cannot be pressed
        twice into a stale step.
        """
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        user = getattr(query, "from_user", None)
        user_id = str(getattr(user, "id", "")) if user else ""
        if not user_id:
            await query.answer()
            return
        reply = self.router.handle_callback(user_id, query.data or "")
        message = getattr(query, "message", None)
        if message is not None:
            try:
                await message.edit_text(reply.text, reply_markup=_reply_markup(reply))
            except Exception:  # pragma: no cover - "message is not modified"
                pass
        await query.answer("OK" if reply.ok else "Rejected")


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
    app.add_handler(CallbackQueryHandler(frontend.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, frontend.on_message))
    app.add_handler(MessageHandler(filters.COMMAND, frontend.on_message))
    return app


def _start_polling(app: Any) -> None:
    """Run the polling loop in whatever thread called us.

    ``stop_signals=()`` is load-bearing: by default python-telegram-bot
    registers UNIX signal handlers, which are only legal in the main thread --
    in the background thread we run polling in, that raises ``RuntimeError:
    set_wakeup_fd only works in main thread`` and the poller dies at startup
    while the (health-gated) HTTP server keeps serving. Empty tuple registers
    no handlers; shutdown is the process's SIGTERM, handled by the HTTP layer.
    """
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=())


def run_bot(settings: Settings, router_factory: Any) -> None:  # pragma: no cover
    """Start long-polling. Blocking; meant for a real deployment."""
    app = build_from_settings(settings, router_factory)
    log.info("bot starting")
    _start_polling(app)
