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
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from ..config import Settings
from .commands import CommandRouter
from .ui import MenuUI, _REPLY_MENU

__all__ = ["TelegramFrontend", "run_bot", "build_from_settings"]

log = logging.getLogger("uotpbot.telegram")


# TWO pools, deliberately separate. This is the difference between "My numbers"
# freezing and staying responsive during a purchase.
#
# 1. _EXECUTOR (fast): menu navigation, button dispatch, plain text/photo
#    screens. These return in milliseconds -- provider calls here are bounded
#    (a cancel/resend setStatus is ~1s). Sized BOT_WORKERS so a flood of taps
#    never queues.
# 2. _SLOW_EXECUTOR (slow): DEFFERED jobs and the background OTP auto-poller.
#    A 💰 Check OTP / ✅ Buy "wait" blocks for the FULL ~5-minute OTP window.
#    If those shared the fast pool, a handful of concurrent orders would
#    occupy every thread and a "My numbers" tap would sit queued behind them
#    until an order refunded -- exactly the reported freeze. On their own pool
#    they can use all its threads without starving navigation.
#
# ``asyncio.to_thread`` would put everything on asyncio's DEFAULT executor
# (min(32, cpu+4) = 6 threads on a small box), so 10+ concurrent orders starve
# it. Both pools are sized from BOT_WORKERS (default 32) because threads mostly
# *sleep* on poll intervals, so a big pool is cheap.
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()
_SLOW_EXECUTOR: Optional[ThreadPoolExecutor] = None
_SLOW_EXECUTOR_LOCK = threading.Lock()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_executor() -> ThreadPoolExecutor:
    """The FAST pool for navigation (menu, buttons, screens). Created lazily."""
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                workers = _worker_count()
                _EXECUTOR = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="uotp-nav"
                )
    return _EXECUTOR


def _get_slow_executor() -> ThreadPoolExecutor:
    """The SLOW pool for multi-minute OTP waits and the auto-poller.

    Kept separate from ``_get_executor`` so long waits only ever occupy this
    pool's threads and can never starve the navigation that keeps the button
    surface alive (the reported freeze: everything sharing one pool).
    """
    global _SLOW_EXECUTOR
    if _SLOW_EXECUTOR is None:
        with _SLOW_EXECUTOR_LOCK:
            if _SLOW_EXECUTOR is None:
                _SLOW_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_worker_count(), thread_name_prefix="uotp-wait"
                )
    return _SLOW_EXECUTOR


def _worker_count() -> int:
    """BOT_WORKERS, floored so a crazy value can't spawn thousands of threads."""
    return max(8, min(_int_env("BOT_WORKERS", 32), 128))


async def _run_offloop(fn, *args):
    """Run ``fn`` on the FAST navigation pool.

    Use for menu/button/screen work that must never queue behind a multi-minute
    OTP wait. Never the 6-thread asyncio default pool.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_executor(), fn, *args)


async def _run_slow(fn, *args):
    """Run a long off-loop callable (an OTP wait / buy) on the SLOW pool.

    These occupy their own threads so concurrent orders wait in parallel and
    navigation (``_run_offloop``) stays free.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_slow_executor(), fn, *args)


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


def _reply_menu_markup(include_admin: bool = False) -> object:
    """The always-visible bottom menu (ReplyKeyboardMarkup).

    Lives at the bottom of the chat so a customer never has to type. Only one
    ``reply_markup`` fits per message, and it cannot be an inline keyboard at
    the same time, so it is only attached to the persistent-menu welcome and
    never to an edit (an edit keeps the inline buttons; Telegram keeps the
    keyboard in the client once sent).

    ``include_admin`` hides the owner-only "⚙️ Admin Panel" key from every
    non-owner (customers must never see an owner screen). The routing still
    re-checks ownership, so even a crafted press goes nowhere.
    """
    if not HAS_TELEGRAM:
        return None
    from telegram import KeyboardButton, ReplyKeyboardMarkup

    rows = [row for row in _REPLY_MENU
            if include_admin or all(not _is_admin_label(label) for label, _cb in row)]
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label, _cb in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap a button or type /help",
    )


def _is_admin_label(label: str) -> bool:
    """True for the owner-only admin menu key."""
    return "Admin Panel" in label or label.lower() in ("⚙️ admin panel", "admin panel")


def _is_button_pair(x) -> bool:
    """True when ``x`` is a single ``(label, callback_data)`` pair."""
    return (
        isinstance(x, (tuple, list))
        and len(x) == 2
        and all(isinstance(v, str) for v in x)
    )


def _normalize_rows(rows) -> list[list[tuple[str, str]]]:
    """Coerce a Reply's ``rows`` into a list of rows, each a list of
    ``(label, callback_data)`` pairs.

    The UI contract says ``rows`` is a tuple of *rows*, each row being a
    tuple of button pairs. Some replies (e.g. the 🆘 Support card) pass a
    FLAT tuple of pairs by accident, and the 🧾 My numbers history grid used
    to double-nest a single button row. Both break ``for label, data in row``
    and -- because the transport swallows the exception -- blanked the screen,
    leaving only the "OK" popup. Normalising here makes any such shape render
    instead of silently failing, so a single malformed row can never hide an
    entire screen.
    """
    rows = [r for r in (rows or ()) if r]
    if not rows:
        return []

    def pairs_of(row) -> list[tuple[str, str]]:
        # A bare ``(label, data)`` pair is a lone-button row.
        if _is_button_pair(row):
            return [tuple(row)]
        # Otherwise a row is a sequence of button pairs; keep the valid ones.
        return [tuple(it) for it in row if _is_button_pair(it)]

    # If every top-level element is itself a single pair, the author passed a
    # FLAT tuple of buttons ("Buy", "My numbers", "Menu") instead of rows-of-
    # rows. Bundle them all into one row rather than reading each button as a
    # whole row (which would try to unpack a string into two vars).
    if all(_is_button_pair(r) for r in rows):
        return [list(rows)]
    return [pairs_of(r) for r in rows]


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
    rows = _normalize_rows(getattr(reply, "rows", ()) or ())
    if rows:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in rows
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
        famgateway_api_key: str = "",
        famgateway_base_url: str = "https://famgateway.in",
        public_url: str = "",
    ) -> None:
        self.router = router
        self.ui = ui or MenuUI(
            router, support_contact=support_contact, pay_upi_id=pay_upi_id,
            famgateway_api_key=famgateway_api_key,
            famgateway_base_url=famgateway_base_url,
            public_url=public_url,
        )
        # /buy (typed) and button buys must agree on maintenance mode:
        # the flag lives in the UI's store; the router just consults it.
        self.router.maintenance_fn = self.ui.maintenance_on
        #: Per-token single-flight guards for the background OTP auto-poller.
        self._auto_polling: set[str] = set()
        self._auto_done: set[str] = set()
        self._auto_extra: set[str] = set()  # tokens already listening for more OTPs

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
        reply = await _run_offloop(self.ui.text, user_id, message.text)
        deferred = getattr(reply, "deferred", None)
        if deferred is None:
            await self._deliver_reply(message, reply)
            await self._send_notifications(context, reply)
            return
        # A slow step (e.g. creating a FamGateway payment order): send the
        # placeholder now, do the work off the event loop, then deliver the
        # outcome. If the outcome carries a photo (a QR image) we send a fresh
        # photo message, otherwise we edit the placeholder in place.
        await self._deliver_reply(message, reply)
        try:
            final = await _run_slow(deferred, user_id)
        except Exception:  # never leave the customer staring at a spinner
            log.exception("deferred step failed")
            await self._safe_edit_text(
                message,
                "⚠️ That step could not be completed. Nothing was charged. "
                "Please try again or contact support.",
            )
            return
        await self._deliver_reply(message, final)
        await self._send_notifications(context, final)

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
            # Answer with NO text so no toast popup appears. Telegram still
            # requires answering the callback (else the button spinner hangs),
            # but an empty answer suppresses the "OK"/"Rejected" toast that
            # users found noisy on every navigation tap.
            await query.answer()
            return

        # ``ui.button`` handles token buttons (💰 Check OTP / 🔁 Resend / ♻️
        # Cancel) by calling the provider SYNCHRONOUSLY (setStatus). Running it
        # off the event loop keeps that network call from blocking every other
        # customer's tap. A thrown error must never silently drop the screen --
        # the transport always edits to a real fallback instead.
        try:
            reply = await _run_offloop(self.ui.button, user_id, data)
        except Exception:  # noqa: BLE001 - never freeze a tap
            log.exception("button handler failed")
            await query.answer()
            if message is not None:
                await self._safe_edit_text(
                    message,
                    "⚠️ Something went wrong opening that screen. Please "
                    "tap again — nothing was charged.",
                )
            return
        # No-toast answer: the screen (edited below) is the feedback, not a
        # popup. An empty answer suppresses the "OK"/"Hmm" toast entirely.
        await query.answer()
        if message is None:
            return
        deferred = getattr(reply, "deferred", None)
        if deferred is None:
            await self._safe_edit(message, reply)
            await self._send_notifications(context, reply)
            return
        # Purchase (or any slow step): placeholder now, outcome when done.
        # Runs on the SLOW pool so a long OTP wait never starves navigation.
        await self._safe_edit(message, reply)
        try:
            final = await _run_slow(deferred, user_id)
        except Exception as exc:  # never leave the customer staring at a spinner
            log.exception("deferred step failed")
            kind = type(exc).__name__
            await self._safe_edit_text(
                message,
                f"⚠️ Order could not be completed ({kind}). "
                "Please do not retry yet — your payment and any supplier "
                "activation are being reconciled. Contact support if your "
                "balance does not update shortly.",
            )
            return
        await self._safe_edit(message, final)
        # Auto-deliver the OTP in the background once the SMS lands, so the
        # customer does not have to keep tapping 💰 Check OTP. Only ever edits
        # with a genuine terminal outcome; disabled per-config if not wanted.
        engine_cfg = getattr(self.router.engine, "config", None)
        if engine_cfg is not None and getattr(engine_cfg, "auto_poll_otp", False):
            token = self._wait_token(final)
            if token:
                self._schedule_auto_poll(message, token)

    def _wait_token(self, reply) -> Optional[str]:
        """The co:<token> from a reply, or None (no active number in it)."""
        for row in getattr(reply, "rows", ()) or ():
            for _label, data in row:
                if isinstance(data, str) and data.startswith("co:"):
                    return data.split(":", 1)[1]
        return None

    def _schedule_auto_poll(self, message: Any, token: str) -> None:
        """Schedule one background OTP poller per active order (single-flight)."""
        if token in self._auto_polling or token in self._auto_done:
            return
        self._auto_polling.add(token)
        asyncio.get_running_loop().create_task(self._auto_poll(message, token))

    async def _auto_poll(self, message: Any, token: str) -> None:
        """Wait (off the event loop) for the OTP and DELIVER it as a fresh
        message (never an edit).

        This is the permanent fix for "My numbers stops working after a
        purchase". The previous implementation edited the ORIGINAL buy message
        with the outcome; if the customer had tapped '🧾 My numbers' (or any
        button) on that same message in the meantime, the background delivery
        silently REWROTE the message they were navigating, clobbering the
        screen and leaving its buttons dead. Delivering the code as a NEW
        message means no background task ever touches a message the customer is
        looking at, so navigation is always stable.
        """
        try:
            _terminal, _reply = await _run_slow(self.router.poll_once, token)
            stored = self.router.terminal_reply(token)
            if stored is not None:
                # Fresh message, never an edit. The buy screen keeps its number
                # + 💰 Check buttons; the code arrives as its own message.
                try:
                    await message.reply_text(stored.text, reply_markup=_reply_markup(stored))
                except Exception:  # noqa: BLE001 - still don't crash the poller
                    log.warning("could not deliver OTP message for token %s", token)
                # The first OTP is delivered; keep listening for ADDITIONAL
                # codes on the same number during its validity window (multi-OTP).
                remaining = await _run_slow(self.router.active_valid_remaining, token)
                if remaining is not None and remaining > 0:
                    self._schedule_auto_extra(message, token)
            self._auto_done.add(token)
        except Exception as exc:  # noqa: BLE001 - never crash the poller
            log.exception("auto OTP poll failed: %s", exc)
        finally:
            self._auto_polling.discard(token)

    def _schedule_auto_extra(self, message: Any, token: str) -> None:
        """Start one background listener for extra OTPs on this number."""
        if token in self._auto_extra:
            return
        self._auto_extra.add(token)
        asyncio.get_running_loop().create_task(self._auto_extra_listener(message, token))

    async def _auto_extra_listener(self, message: Any, token: str) -> None:
        """Keep checking for newer OTPs until the number expires.

        Sends a fresh message only when a genuinely-new code arrives, so it
        never edits (and can never clobber) the OTP the manual tap already
        showed. Stops when the number is no longer valid.
        """
        try:
            while True:
                remaining = await _run_slow(self.router.active_valid_remaining, token)
                if remaining is None or remaining <= 0:
                    return
                reply = await _run_slow(self.router.poll_new_otp, token)
                if reply.ok and "New OTP" in reply.text:
                    try:
                        await message.reply_text(reply.text, reply_markup=_reply_markup(reply))
                    except Exception:  # noqa: BLE001
                        log.warning("could not deliver an extra OTP to %s", token)
                    return  # one extra delivered; user can tap Check for more
                await asyncio.sleep(25)
        except Exception as exc:  # noqa: BLE001 - never crash
            log.exception("extra OTP listener failed: %s", exc)
        finally:
            self._auto_extra.discard(token)

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
        reply = await _run_offloop(self.ui.photo, user_id, file_id)
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
        """Text reply, or a photo message when the reply carries one (QR).

        A reply that opts into the persistent bottom menu carries the
        ReplyKeyboardMarkup instead of inline buttons (the two cannot share a
        message); every other reply keeps its inline keyboard.
        """
        photo = getattr(reply, "photo", None)
        photo_url = getattr(reply, "photo_url", None)
        if photo or photo_url:
            if photo:
                sent = await message.reply_photo(
                    photo=photo, caption=reply.text[:1024],
                    reply_markup=_reply_markup(reply),
                )
            else:
                sent = await message.reply_photo(
                    photo=photo_url, caption=reply.text[:1024],
                    reply_markup=_reply_markup(reply),
                )
            # Remember where the FamGateway QR landed so the payment
            # webhook/sweep can edit THIS message to a success note as soon as
            # the money is confirmed -- no tap needed from the customer.
            self._remember_fg_message(sent, reply)
            return
        is_owner = False
        if getattr(reply, "persistent_menu", False):
            user = getattr(message, "from_user", None)
            uid = str(getattr(user, "id", "")) if user else ""
            is_owner = bool(uid) and self.router._is_owner(uid)
        markup = _reply_menu_markup(include_admin=is_owner) \
            if getattr(reply, "persistent_menu", False) else _reply_markup(reply)
        await message.reply_text(reply.text, reply_markup=markup)

    async def _send_notifications(self, context: Any, reply) -> None:
        """Deliver any (chat_id, text) notifications a reply carries."""
        if context is None:
            return
        for chat_id, text in getattr(reply, "notify", ()) or ():
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=text)
            except Exception:
                log.warning("notification to %s failed", chat_id, exc_info=True)

    def _remember_fg_message(self, sent, reply) -> None:
        """Persist the QR message identity for an order (best-effort).

        ``sent`` is the :class:`Message` returned by ``reply_photo``; it carries
        the chat id and message id that let the payment notifier edit the QR in
        place on confirmation. Mocks / non-Telegram transports simply no-op.
        """
        try:
            order_id = self.ui._fg_order_id(reply)
            if not order_id:
                return
            chat_id = getattr(getattr(sent, "chat", None), "id", None)
            message_id = getattr(sent, "message_id", None)
            if chat_id is None or message_id is None:
                return
            self.ui.remember_fg_message(order_id, chat_id, message_id)
        except Exception:  # noqa: BLE001 - a nicety, never break delivery
            pass

    @staticmethod
    def _is_photo_message(message: Any) -> bool:
        """True when ``message`` is a Telegram PHOTO (the QR image screen).

        Real PTB Message objects expose ``photo`` as a list of
        :class:`PhotoSize` (empty/None for text messages), so only a non-empty
        list counts -- a bare truthiness test would misfire on mocks.
        """
        if message is None:
            return False
        try:
            photos = getattr(message, "photo", None)
            return isinstance(photos, (list, tuple)) and bool(photos)
        except Exception:  # noqa: BLE001
            return False

    async def _safe_edit(self, message: Any, reply) -> None:
        """Update a message with a reply's text + keyboard.

        The payment QR screen (and any screen a fresh photo message carries) is
        a PHOTO message: Telegram cannot ``edit_text`` a photo into a text
        reply, so the button tap used to silently do nothing ("buttons not
        responding"). Detect a photo message and reply with a fresh text
        message instead -- the QR stays, and the result (or the Menu) appears
        as a new message. Everything else edits in place.
        """
        markup = _reply_markup(reply)
        if self._is_photo_message(message):
            await message.reply_text(reply.text, reply_markup=markup)
            return
        try:
            await message.edit_text(reply.text, reply_markup=markup)
        except Exception:  # pragma: no cover - "message is not modified"
            pass

    async def _safe_edit_text(self, message: Any, text: str) -> None:
        if self._is_photo_message(message):
            await message.reply_text(text)
            return
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
        famgateway_api_key=getattr(settings, "famgateway_api_key", ""),
        famgateway_base_url=getattr(settings, "famgateway_base_url",
                                    "https://famgateway.in"),
        public_url=getattr(settings, "public_url", ""),
    )
    # Durable refunds: boot the retry worker so any refund left pending by an
    # earlier crash/redeploy is credited now (idempotent; safe on rebuilds).
    frontend.router.start_refund_worker()
    # ``concurrent_updates`` is load-bearing for responsiveness.
    #
    # By default python-telegram-bot processes updates SEQUENTIALLY: it awaits
    # one handler to complete before it starts the next. A single long-running
    # handler -- a ``/buy`` text command, or a 💰 Check OTP tap, either of which
    # can wait on the provider for the full ~5-minute OTP window -- would
    # therefore queue every other update behind it, so Back / My numbers / the
    # whole button grid froze until that order refunded. Running each update in
    # its own task keeps the button surface live during a wait.
    app = (
        Application.builder()
        .token(settings.require_telegram())
        .post_init(_post_init)
        .concurrent_updates(True)
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


def run_bot(settings: Settings, router_factory: Any,
            *, owner_alert: Any = None, payment_notifier: Any = None) -> None:  # pragma: no cover
    """Start long-polling. Blocking; meant for a real deployment."""
    app = build_from_settings(settings, router_factory)
    # Phase-1: hand the alert bridge to the wallet monitor thread so it can
    # reach the owner through this app's event loop.
    if owner_alert is not None:
        owner_alert.attach(app)
    # Phase-2: hand the payment notifier to the app so the FamGateway
    # webhook/sweep can edit a customer's QR message the moment payment lands.
    if payment_notifier is not None:
        payment_notifier.attach(app)
    log.info("bot starting")
    _start_polling(app)
