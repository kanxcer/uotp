"""Telegram transport.

A deliberately thin shell: authentication of the update, rendering
:class:`uotpbot.bot.commands.Reply` into inline keyboards, and deferring
slow work (purchases wait on an OTP for minutes) off the event loop so one
customer's order never freezes another's menu.

All conversation logic lives in ``commands.py`` (commands + white-label
flow) and ``ui.py`` (the guided button interface), so it is unit-testable
without a network.

``python-telegram-bot`` is an optional dependency. Importing this module
without it raises a clear error instead of an ImportError from deep inside.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..config import Settings
from .commands import CommandRouter
from .ui import MenuUI

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
    """Turn a Reply's buttons into an inline keyboard.

    ``rows`` (pre-arranged grid built by the menu UI) wins; otherwise the
    flat ``buttons`` render one per row, because labels like
    "My own API (5% of each sale)" are too long to sit side by side.
    ``None`` means no keyboard, which is what python-telegram-bot expects
    for a plain message.
    """
    if not HAS_TELEGRAM:
        return None
    rows = getattr(reply, "rows", ()) or ()
    if rows:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in rows
            if row
        ])
    buttons = getattr(reply, "buttons", ()) or ()
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data)] for label, data in buttons
    ])


class TelegramFrontend:
    """Bridges python-telegram-bot updates to the UI and command router."""

    def __init__(
        self,
        router: CommandRouter,
        *,
        ui: Optional[MenuUI] = None,
        support_contact: str = "",
        pay_upi_id: str = "",
    ) -> None:
        self.router = router
        self.ui = ui or MenuUI(
            router, support_contact=support_contact, pay_upi_id=pay_upi_id
        )

    async def on_message(self, update: Any, context: Any = None) -> None:
        """Handle any incoming message and reply with the UI's answer.

        Runs off the event loop: typed /buy can wait on a provider for
        minutes, and the ledger/Postgres backends are explicitly built for
        cross-thread use.
        """
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if message is None or not getattr(message, "text", None):
            return
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", "")) if user else ""
        if not user_id:
            return
        reply = await asyncio.to_thread(self.ui.text, user_id, message.text)
        await self._deliver_reply(message, reply)
        await self._send_notifications(context, reply)

    async def on_callback(self, update: Any, context: Any = None) -> None:
        """Handle an inline-keyboard press.

        ``cb:*`` data belongs to the /createbot state machine and goes to
        the router; everything else is menu navigation or a purchase. The
        message is edited in place so an answered menu cannot be pressed
        twice into a stale step, and a purchase edits twice: "working on
        it" immediately, the outcome when it lands.
        """
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        user = getattr(query, "from_user", None)
        user_id = str(getattr(user, "id", "")) if user else ""
        if not user_id:
            await query.answer()
            return
        data = query.data or ""
        message = getattr(query, "message", None)
        if data.startswith("cb:"):
            reply = self.router.handle_callback(user_id, data)
            if message is not None:
                await self._safe_edit(message, reply)
            await query.answer("OK" if reply.ok else "Rejected")
            return

        reply = self.ui.button(user_id, data)
        await query.answer("OK" if reply.ok else "Hmm")
        if message is None:
            return
        deferred = getattr(reply, "deferred", None)
        if deferred is None:
            await self._safe_edit(message, reply)
            await self._send_notifications(context, reply)
            return
        # Purchase (or any slow step): placeholder now, outcome when done.
        await self._safe_edit(message, reply)
        try:
            final = await asyncio.to_thread(deferred, user_id)
        except Exception as exc:  # never leave the customer staring at a spinner
            log.exception("deferred step failed")
            kind = type(exc).__name__
            await self._safe_edit_text(
                message,
                f"⚠️ Something failed mid-order ({kind}). "
                "If money was deducted it is already back in your balance. "
                "Please try again.",
            )
            return
        await self._safe_edit(message, final)

    async def on_photo(self, update: Any, context: Any = None) -> None:
        """A photo arrives: payment screenshot, owner's payment QR, or noise.

        Payment flows are the only consumer, so a random photo gets a polite
        pointer to ➕ Add money. When a top-up is created the screenshot is
        forwarded to the owner so approval is one glance, not a guessing game.
        """
        message = getattr(update, "message", None)
        photos = getattr(message, "photo", None) if message else None
        if not message or not photos:
            return
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", "")) if user else ""
        if not user_id:
            return
        file_id = photos[-1].file_id  # largest size Telegram offered
        reply = await asyncio.to_thread(self.ui.photo, user_id, file_id)
        await self._deliver_reply(message, reply)
        if reply.forward_photo and self.router.owner_id and context is not None:
            caption = reply.notify[0][1] if reply.notify else "Payment screenshot"
            try:
                await context.bot.send_photo(
                    chat_id=int(self.router.owner_id), photo=file_id,
                    caption=caption[:1024],
                )
            except Exception:  # owner blocked the bot? notify text still went
                log.warning("could not forward payment screenshot to owner")
        await self._send_notifications(context, reply)

    # -- send/edit plumbing -----------------------------------------------
    async def _deliver_reply(self, message: Any, reply) -> None:
        """Text reply, or a photo message when the reply carries one (QR)."""
        photo = getattr(reply, "photo", None)
        if photo:
            await message.reply_photo(
                photo=photo, caption=reply.text[:1024],
                reply_markup=_reply_markup(reply),
            )
            return
        await message.reply_text(reply.text, reply_markup=_reply_markup(reply))

    async def _send_notifications(self, context: Any, reply) -> None:
        """Deliver any (chat_id, text) notifications a reply carries."""
        if context is None:
            return
        for chat_id, text in getattr(reply, "notify", ()) or ():
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=text)
            except Exception:
                log.warning("notification to %s failed", chat_id, exc_info=True)

    async def _safe_edit(self, message: Any, reply) -> None:
        try:
            await message.edit_text(reply.text, reply_markup=_reply_markup(reply))
        except Exception:  # pragma: no cover - "message is not modified"
            pass

    async def _safe_edit_text(self, message: Any, text: str) -> None:
        try:
            await message.edit_text(text)
        except Exception:  # pragma: no cover
            pass


async def _post_init(app: Any) -> None:  # pragma: no cover - network call
    """Register Telegram's built-in command menu next to the text box."""
    try:
        await app.bot.set_my_commands([
            ("start", "🏠 Open the menu"),
            ("buy", "🛒 Buy a number"),
            ("list", "📋 All services"),
            ("wallet", "💰 Balance & add money"),
            ("help", "❓ How it works"),
        ])
        log.info("telegram command menu registered")
    except Exception:
        log.warning("could not set the command menu", exc_info=True)


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
    frontend = TelegramFrontend(
        router_factory(),
        support_contact=getattr(settings, "support_contact", ""),
        pay_upi_id=getattr(settings, "pay_upi_id", ""),
    )
    app = (
        Application.builder()
        .token(settings.require_telegram())
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CallbackQueryHandler(frontend.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, frontend.on_message))
    app.add_handler(MessageHandler(filters.COMMAND, frontend.on_message))
    app.add_handler(MessageHandler(filters.PHOTO, frontend.on_photo))  # payments/QR
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
