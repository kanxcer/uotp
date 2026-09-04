"""Durable customer refunds (outbox pattern).

A refund is real money leaving the customer balance and entering the ledger's
sales-refund line, and it must never be lost to a transient DB blip. The wallet
store keeps a durable ``refund_outbox`` row, and this service WRITES IT FIRST
(the row's unique ``(scope, order_token)`` constraint is the claim), then
attempts the ledger post and the wallet credit. Each step is individually
idempotent:

* the ledger line is posted at most once per order (tracked by the row's
  ``ledger_done`` flag -- a retry after a wallet-credit blip never re-posts
  the refund line, which used to make reported refunds outrun real money);
* the wallet credit happens only for the path that CLAIMED the row (the
  insert won), so two racing paths (a Cancel and a timeout poll) cannot both
  credit;
* if the credit fails the row stays pending and a background worker (plus a
  redeploy's startup pass) retries until it succeeds -- so a customer who was
  legitimately refunded (confirmed release at the provider) is never left out
  of pocket silently.
"""

from __future__ import annotations

import logging
import threading

from .money import Money

__all__ = ["DurableRefund", "refund_once"]

log = logging.getLogger("uotpbot.refund")

#: Stop retrying a refund after this many attempts (owner should reconcile).
MAX_ATTEMPTS = 8
#: Seconds between background retry sweeps.
RETRY_INTERVAL_SECONDS = 30


def refund_once(router, user_id: str, amount: Money, order_token: str,
                reason: str) -> bool:
    """Idempotently apply one claimed refund: ledger line + wallet credit.

    Both steps are guarded so a partial failure plus retry cannot double-post
    the ledger line or double-credit the wallet:

    1. If the outbox row says the ledger line was already posted, skip it.
    2. Otherwise post it, then persist ``ledger_done`` BEFORE the credit --
       so any later retry (worker or redeploy) will never post it again.
    3. If the row is already ``done`` the credit already happened; skip it.
    4. Otherwise credit the wallet.

    Returns True when the refund is fully applied (it may have been applied
    by an earlier call -- idempotency, not "I did the work").
    """
    store = router.wallets
    row = None
    if store is not None and callable(getattr(store, "get_refund", None)):
        try:
            row = store.get_refund(order_token)
        except Exception:  # noqa: BLE001 - fail open: attempt the steps
            row = None

    # 1 + 2. Ledger line, at most once per order.
    if not (row is not None and row.ledger_done):
        try:
            router.ledger.record_customer_refund(amount, ref=order_token, memo=reason)
        except Exception as exc:  # noqa: BLE001 - retryable
            log.warning("refund ledger post failed user=%s amount=%s: %s",
                        user_id, amount, exc)
            return False
        _mark_ledger_done(store, order_token)
    # 3. Already credited (an earlier attempt or a racing path).
    if row is not None and row.status == "done":
        return True
    # 4. Wallet: give the customer their money back.
    try:
        router.credit(user_id, amount)
    except Exception as exc:  # noqa: BLE001 - retryable
        log.warning("refund wallet credit failed user=%s amount=%s: %s",
                    user_id, amount, exc)
        return False
    # Persist "done" immediately, so no later call -- worker sweep, redeploy,
    # or a racing path that still holds the row -- can credit again. (Dying
    # between the credit and this mark is the documented reconciliation case:
    # the credit is monotonic and visible in the wallet.)
    if row is not None and store is not None \
            and callable(getattr(store, "mark_refund_result", None)):
        try:
            store.mark_refund_result(row.refund_id, done=True)
        except Exception:  # noqa: BLE001 - the caller's mark is the fallback
            log.warning("could not mark refund %s done", row.refund_id)
    return True


def _mark_ledger_done(store, order_token: str) -> None:
    """Persist that the ledger line for ``order_token`` is posted."""
    if store is None or not order_token \
            or not callable(getattr(store, "mark_ledger_done", None)):
        return
    try:
        store.mark_ledger_done(order_token)
    except Exception:  # noqa: BLE001 - the ledger line will re-check on retry
        log.warning("could not persist ledger_done for %s", order_token)


class DurableRefund:
    """Refund service with a durable outbox and a background retry worker."""

    def __init__(self, router) -> None:
        self.router = router
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def _store(self):
        return self.router.wallets

    def _claim(self, user_id, amount, order_token, reason, *,
               ledger_done: bool) -> tuple[str, bool]:
        """Durably claim this order's refund. Returns (refund_id, inserted).

        ``inserted`` is True only for the path that won the claim (the unique
        constraint made the row exist because of this call). The row is
        written BEFORE any money moves, so a crash can never lose the refund;
        ``ledger_done`` records whether the caller already posted the ledger
        line (the engine posts it itself on the timeout path).
        """
        store = self._store
        if store is None or not callable(getattr(store, "write_refund", None)):
            return "", True
        try:
            result = store.write_refund(user_id=user_id, amount=amount,
                                        order_token=order_token, reason=reason,
                                        ledger_done=ledger_done)
        except Exception as exc:  # noqa: BLE001 - never block the refund
            log.critical("refund outbox write FAILED order=%s: %s", order_token, exc)
            return "", True
        if isinstance(result, tuple):
            refund_id, inserted = result
        else:  # a store that predates the claim API: assume claimed
            refund_id, inserted = result, True
        return str(refund_id), bool(inserted)

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
                reason: str, *, ledger_posted: bool = False) -> tuple[bool, str]:
        """Refund ``amount`` to ``user_id`` for ``order_token`` (cancel path).

        Claims the outbox row first (durable + idempotent: one refund per
        order), then applies it idempotently. Returns ``(ok, refund_id)``.

        ``ledger_posted`` is True when the caller KNOWS the ledger line
        already exists (e.g. the engine posted it for this order just before
        a cancel raced in) -- the row is then written with ``ledger_done``
        set so the apply step does not post a second line.

        If the order was ALREADY refunded by a racing path, this does NOT
        credit again and returns ``(False, ...)`` so the caller can tell the
        customer honestly. Even when ``ok`` is False for a transient DB blip,
        the refund WILL still be retried.
        """
        if self.is_processed(order_token):
            log.info("refund request for %s skipped: already processed", order_token)
            return False, ""
        refund_id, inserted = self._claim(user_id, amount, order_token, reason,
                                          ledger_done=ledger_posted)
        if not inserted:
            # A racing path already claimed this order's refund; it owns the
            # credit. Report its state honestly, credit nothing here.
            row = None
            if self._store is not None and callable(getattr(self._store, "get_refund", None)):
                try:
                    row = self._store.get_refund(order_token)
                except Exception:  # noqa: BLE001
                    row = None
            return (row is not None and row.status == "done"), ""
        ok = refund_once(self.router, user_id, amount, order_token, reason)
        self._mark(refund_id, done=ok, error="" if ok else "first attempt failed")
        return ok, refund_id

    def is_processed(self, order_token: str) -> bool:
        """True if this order's customer refund has already been applied (done).

        The single guard that makes a refund happen exactly ONCE per order, even
        when several independent code paths (a manual Check, the background
        auto-poller, a Cancel) resolve the same order concurrently. Without it a
        single order could be refunded several times.
        """
        store = self._store
        if store is None or not order_token \
                or not callable(getattr(store, "get_refund", None)):
            return False
        try:
            row = store.get_refund(order_token)
        except Exception:  # noqa: BLE001 - fail open to allow the credit
            return False
        return row is not None and row.status == "done"

    def apply_timeout_refund(self, order_token: str, user_id: str,
                             amount: Money, reason: str) -> bool:
        """Credit a timeout/auto-refund, guarded so it only happens once.

        The ENGINE already posted the customer-refund ledger line when the OTP
        timed out (that is its job); this claims the outbox row (marked
        ``ledger_done`` so no retry re-posts the line), writes it BEFORE the
        credit, and credits the wallet only if this call won the claim.
        Returns True when this call applied the credit, False when the order
        was already resolved by another path (no double credit).
        """
        if self.is_processed(order_token):
            return False
        refund_id, inserted = self._claim(user_id, amount, order_token, reason,
                                          ledger_done=True)
        if not inserted:
            # The cancel path claimed first (it will credit); do not credit again.
            return False
        try:
            self.router.credit(user_id, amount)
        except Exception as exc:  # noqa: BLE001 - retryable
            log.warning("timeout refund credit failed user=%s amt=%s: %s",
                        user_id, amount, exc)
            return False
        # Record it as done (the ledger line is the engine's; here we only
        # persist that the WALLET credit happened, so nothing re-credits).
        self._mark(refund_id, done=True, error="")
        return True

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
