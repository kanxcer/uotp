"""Durable customer refunds (outbox pattern).

A refund is real money leaving the customer balance and entering the ledger's
sales-refund line, and it must never be lost to a transient DB blip. The wallet
store keeps a durable ``refund_outbox`` row, and this service writes it BEFORE
any money moves, then attempts the credit. If the credit fails the row stays
pending and a background worker (plus a redeploy's startup pass) retries until
it succeeds -- so a customer who was legitimately refunded (confirmed release
at the provider) is never left out of pocket silently.
"""

from __future__ import annotations

import logging
import threading
import time

from .money import Money

__all__ = ["DurableRefund", "refund_once"]

log = logging.getLogger("uotpbot.refund")

#: Stop retrying a refund after this many attempts (owner should reconcile).
MAX_ATTEMPTS = 8
#: Seconds between background retry sweeps.
RETRY_INTERVAL_SECONDS = 30


def refund_once(router, user_id: str, amount: Money, order_token: str,
                reason: str) -> bool:
    """Post the ledger refund line and credit the customer's wallet.

    Both must succeed for a refund to count; either failing leaves the money
    outstanding and is retried by :class:`DurableRefund`. Returns True on
    success.
    """
    # 1. Ledger: the customer refund line, so P&L never understates refunds.
    try:
        router.ledger.record_customer_refund(amount, ref=order_token, memo=reason)
    except Exception as exc:  # noqa: BLE001 - retryable
        log.warning("refund ledger post failed user=%s amount=%s: %s",
                    user_id, amount, exc)
        return False
    # 2. Wallet: give the customer their money back.
    try:
        router.credit(user_id, amount)
    except Exception as exc:  # noqa: BLE001 - retryable
        log.warning("refund wallet credit failed user=%s amount=%s: %s",
                    user_id, amount, exc)
        return False
    return True


class DurableRefund:
    """Refund service with a durable outbox and a background retry worker."""

    def __init__(self, router) -> None:
        self.router = router
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def _store(self):
        return self.router.wallets

    def _write(self, user_id, amount, order_token, reason) -> str:
        store = self._store
        if store is None or not callable(getattr(store, "write_refund", None)):
            return ""
        try:
            return store.write_refund(user_id=user_id, amount=amount,
                                      order_token=order_token, reason=reason)
        except Exception as exc:  # noqa: BLE001 - never block the cancel
            log.critical("refund outbox write FAILED order=%s: %s", order_token, exc)
            return ""

    def _mark(self, refund_id: str, *, done: bool, error: str = "") -> None:
        store = self._store
        if store is None or not refund_id \
                or not callable(getattr(store, "mark_refund_result", None)):
            return
        try:
            store.mark_refund_result(refund_id, done=done, error=error)
        except Exception:  # noqa: BLE001 - retry won't find it; reconcile only
            log.exception("could not mark refund %s", refund_id)

    def request(self, user_id: str, amount: Money, order_token: str,
                reason: str) -> tuple[bool, str]:
        """Refund ``amount`` to ``user_id`` for ``order_token``.

        Writes the outbox first (durable + idempotent: one refund per order),
        then attempts the credit. Returns ``(ok_tried_now, refund_id)``.
        Even when ``ok`` is False the refund WILL be retried.
        """
        refund_id = self._write(user_id, amount, order_token, reason)
        ok = refund_once(self.router, user_id, amount, order_token, reason)
        self._mark(refund_id, done=ok, error="" if ok else "first attempt failed")
        # A `done` row must not be "pending" just because the idempotent insert
        # reused another order's row; if we wrote nothing (already pending) the
        # worker will pick it up. Report what actually happened.
        return ok, refund_id

    def retry_all(self) -> int:
        """Retry every pending refund; returns how many succeeded."""
        store = self._store
        if store is None or not callable(getattr(store, "pending_refunds", None)):
            return 0
        done = 0
        try:
            rows = list(store.pending_refunds(max_attempts=MAX_ATTEMPTS))
        except Exception:  # noqa: BLE001
            return 0
        for row in rows:
            if self._stop.is_set():
                break
            ok = refund_once(self.router, row.user_id, row.amount,
                             row.order_token, row.reason)
            self._mark(row.refund_id, done=ok,
                       error="" if ok else f"retry attempt failed ({row.attempts + 1})")
            if ok:
                done += 1
                log.info("retried refund %s → credited %s to %s",
                         row.refund_id, row.amount, row.user_id)
        return done

    def pending_count(self) -> int:
        store = self._store
        if store is None or not callable(getattr(store, "pending_refunds", None)):
            return 0
        try:
            return len(list(store.pending_refunds(max_attempts=MAX_ATTEMPTS + 1)))
        except Exception:  # noqa: BLE001
            return 0

    # -- background worker ------------------------------------------------
    def start(self) -> None:
        """Start (or restart) the retry worker; also sweeps once immediately."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="refund-retry",
                                        daemon=True)
        self._thread.start()
        log.info("refund retry worker started")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.retry_all()
            except Exception:  # noqa: BLE001 - worker must never die
                log.exception("refund retry sweep failed")
            self._stop.wait(RETRY_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
