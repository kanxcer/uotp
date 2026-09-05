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


def _telegram_http(app, method: str, payload: dict) -> tuple[bool, str]:
    """Call the Telegram Bot HTTP API synchronously (no event loop needed).

    python-telegram-bot >= 21 does not expose ``Application.loop``. The
    FamGateway webhook and the sweep run on OTHER threads, so they cannot
    ``await app.bot.*``. Posting to ``api.telegram.org`` works from any
    thread and is how we actually edit the QR / ping the owner in production.
    """
    token = getattr(getattr(app, "bot", None), "token", None)
    if not token:
        return False, "no-token"
    import json
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode() or "{}")
            if body.get("ok"):
                return True, ""
            return False, str(body.get("description", body))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
            desc = str(body.get("description", exc))
        except Exception:
            desc = str(exc)
        return False, desc
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


class OwnerAlert:
    """Thread-safe owner notification that bridges to a Telegram app."""

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        self._app = None
        self._loop = None
        self._warned = False

    def attach(self, app) -> None:
        """Called once the Telegram app is built on the poller thread."""
        self._app = app
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if not self._warned and app is not None:
            log.info("owner alert bridge attached")

    def set_loop(self, loop) -> None:
        """Capture the poller loop (called from PTB post_init, when it exists)."""
        self._loop = loop

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
            loop = self._loop
            if loop is None or not getattr(loop, "is_running", lambda: False)():
                # PTB 21 Application has no .loop. Fall back to the Bot HTTP API
                # so a monitor thread can still reach the owner.
                if _telegram_http(
                    app, "sendMessage",
                    {"chat_id": int(self.owner_id), "text": text},
                )[0]:
                    log.info("owner alert sent: %s", text.splitlines()[0])
                    return
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
    HTTP server thread / the sweep daemon). python-telegram-bot >= 21 does
    not expose ``Application.loop``, so we edit via the Bot HTTP API -- that
    works from any thread and does not need the poller's event loop. Degrades
    to a log line when no app/token is present, never raising into the caller.
    """

    def __init__(self) -> None:
        self._app = None
        self._loop = None

    def attach(self, app) -> None:
        """Called once the Telegram app is built on the poller thread."""
        self._app = app
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if app is not None:
            log.info("payment notifier bridge attached")

    def set_loop(self, loop) -> None:
        """Capture the poller loop (called from PTB post_init, when it exists)."""
        self._loop = loop

    def edit_order_message(self, chat_id, message_id: int, text: str) -> bool:
        """Edit the QR message in ``chat_id`` to ``text`` (success note).

        The QR is a PHOTO message with a caption + inline keyboard, so we edit
        the CAPTION (which Telegram allows and keeps the buttons); if that
        fails we fall back to editing the message text. Returns True when an
        edit succeeded.
        """
        app = self._app
        if app is None or chat_id is None or message_id is None:
            log.info("[no-app] edit payment message for order skipped")
            return False
        try:
            # HTTP is the production path: webhook/sweep/button-credit all run
            # off the poller thread, and PTB 21 has no Application.loop.
            # Replace the stale "✅ I've paid / Check status" row with a
            # Balance / Menu pair so the QR becomes a finished receipt.
            done_markup = {
                "inline_keyboard": [
                    [{"text": "💰 Balance", "callback_data": "w"},
                     {"text": "🏠 Menu", "callback_data": "m"}],
                ]
            }
            payload = {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "caption": text[:1024],
                "reply_markup": done_markup,
            }
            ok, err = _telegram_http(app, "editMessageCaption", payload)
            if ok or "message is not modified" in err.lower():
                log.info("payment message caption edited in chat %s", chat_id)
                return True
            log.info("caption edit failed (%s); trying editMessageText", err)
            ok2, err2 = _telegram_http(app, "editMessageText", {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "text": text[:4096],
                "reply_markup": done_markup,
            })
            if ok2 or "message is not modified" in err2.lower():
                log.info("payment message text edited in chat %s", chat_id)
                return True
            log.error("payment message edit failed for order in chat %s: "
                      "caption=%s text=%s", chat_id, err, err2)
            return False
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
