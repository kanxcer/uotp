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
