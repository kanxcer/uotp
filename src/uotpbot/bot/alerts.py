"""Owner alert bridge.

The wallet monitor (P2) runs on its own daemon thread. The Telegram ``Application``
runs its event loop on the poller thread and is only built after startup. This
small holder lets the monitor hand an alert to the app once it is wired, and
degrade to a log line (still visible to the operator) when no bot/app is present.

Not a queue: an alert that cannot be delivered is logged; the monitor also keeps
its own state in memory so the loss of a single message is never fatal.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("uotpbot.alert")


class OwnerAlert:
    """Thread-safe owner notification that bridges to a Telegram app."""

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        self._app = None
        self._warned = False

    def attach(self, app) -> None:
        """Called once the Telegram app is built on the poller thread."""
        self._app = app
        if not self._warned and app is not None:
            log.info("owner alert bridge attached")

    def send(self, text: str) -> None:
        """Deliver ``text`` to the owner, or log it when unavailable."""
        app = self._app
        if app is None or not self.owner_id:
            log.warning("[no-app] owner alert: %s", text.splitlines()[0])
            if not self._warned:
                self._warned = True
                log.warning("Owner alerts will only be logged until the "
                            "Telegram app is wired (they are not dropped).")
            return
        try:
            loop = getattr(app, "loop", None)
            if loop is None:
                log.warning("[no-loop] owner alert: %s", text.splitlines()[0])
                return
            async def _deliver():
                await app.bot.send_message(chat_id=int(self.owner_id), text=text)
            asyncio.run_coroutine_threadsafe(_deliver(), loop)
            log.info("owner alert sent: %s", text.splitlines()[0])
        except Exception as exc:  # noqa: BLE001 - never kill a caller
            log.error("owner alert delivery failed: %s", exc)


class PaymentNotifier:
    """Thread-safe bridge that edits a customer's QR payment message in place.

    The FamGateway webhook and the background sweep run on OTHER threads (the
    HTTP server thread / the sweep daemon). The Telegram ``Application`` runs
    its event loop on the poller thread. This holder lets those paths hand the
    app a request to *edit the QR message* once payment is confirmed -- so the
    customer sees "✅ Payment received" on the very message they just paid on,
    without tapping anything. Degrades to a log line when no app is present
    (sweep-only / tests), never raising into the caller.
    """

    def __init__(self) -> None:
        self._app = None

    def attach(self, app) -> None:
        """Called once the Telegram app is built on the poller thread."""
        self._app = app
        if app is not None:
            log.info("payment notifier bridge attached")

    def edit_order_message(self, chat_id, message_id: int, text: str) -> bool:
        """Edit the QR message in ``chat_id`` to ``text`` (success note).

        The QR is a PHOTO message with a caption + inline keyboard, so we edit
        the CAPTION (which Telegram allows and keeps the buttons); if that
        fails we fall back to editing the message text. Returns True when an
        edit was dispatched.
        """
        app = self._app
        if app is None or chat_id is None or message_id is None:
            log.info("[no-app] edit payment message for order skipped")
            return False
        try:
            loop = getattr(app, "loop", None)
            if loop is None:
                log.warning("[no-loop] edit payment message for order skipped")
                return False
            async def _edit() -> None:
                bot = app.bot
                try:
                    await bot.edit_message_caption(
                        chat_id=int(chat_id), message_id=int(message_id),
                        caption=text[:1024],
                    )
                except Exception as caption_exc:
                    # Not a photo (or caption not editable): edit the text so
                    # the customer still sees the success note.
                    try:
                        await bot.edit_message_text(
                            chat_id=int(chat_id), message_id=int(message_id),
                            text=text,
                        )
                    except Exception as text_exc:
                        raise text_exc from caption_exc

            def _done(fut: "asyncio.Future") -> None:
                # Surface a swallowed failure so the "QR doesn't edit" case is
                # diagnosable in logs instead of dying silently in the future.
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - never kill a caller
                    log.error("payment message edit failed for order in chat %s: %s",
                              chat_id, exc)

            fut = asyncio.run_coroutine_threadsafe(_edit(), loop)
            fut.add_done_callback(_done)
            log.info("payment message edited for order in chat %s", chat_id)
            return True
        except Exception as exc:  # noqa: BLE001 - never kill a caller
            log.error("payment message edit failed: %s", exc)
            return False


def pay_message(store, notifier, order_id: str, money) -> None:
    """Edit a customer's QR message to a success note after a confirmed payment.

    Shared by every credit path (webhook, background sweep, AND the customer's
    own 'Check status' / 'I've paid' tap) so the QR is always updated in place
    the moment the money lands. ``store`` holds the ``fg_msg:<order>`` location
    the bot recorded when it sent the QR; missing value or notifier just skips
    the edit (the credit already happened, and Check status still works).
    Best-effort: never raises into a caller.
    """
    if notifier is None:
        return
    get_ = getattr(store, "kv_get", None)
    if not callable(get_):
        return
    try:
        loc = get_(f"fg_msg:{order_id}")
        if not loc or ":" not in loc:
            return
        chat_id, msg_id = loc.split(":", 1)
        text = (
            "✅ Payment received!\n\n"
            f"{money} added to your balance.\n\n"
            "Tap 💰 Balance to see it, or start buying 🛒"
        )
        notifier.edit_order_message(chat_id, int(msg_id), text)
    except Exception as exc:  # noqa: BLE001 - the credit already happened
        log.warning("could not edit QR message for %s: %s", order_id, exc)
