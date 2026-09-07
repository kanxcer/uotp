"""Place, poll and refund social-boost orders.

Customer INR is debited *before* the upstream ``add``. An explicit supplier
error refunds immediately. A timeout / 5xx on ``add`` does **not** refund:
retrying ``add`` could double-order, and we cannot know if the supplier
accepted. Keep the debit, persist pending/unknown, alert the owner, never
retry ``add``. Partial / canceled / refunded statuses credit the undelivered
share once; ``refunded_paise`` is the idempotency lock.

There is no 5-minute auto-refund: delivery is minutes-to-days. The poller
only settles money when the supplier reports a terminal status.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from ..money import Money
from ..reseller import credit_earnings, reseller_split
from .catalog import SmmCatalog, SmmService
from .persist import SmmOrderRow
from .provider import (
    OPEN_STATUSES,
    SmmAmbiguous,
    SmmError,
    SmmStatus,
    SmmUserError,
)

log = logging.getLogger("uotpbot.smm.shop")

__all__ = ["SmmShop", "SmmNotice"]

NotifyFn = Callable[[str, str, str], None]  # scope, user_id, text
AlertFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SmmNotice:
    scope: str
    user_id: str
    text: str


def _friendly_add_error(exc: BaseException) -> str:
    """Customer-safe refusal. Never names the supplier."""
    msg = str(exc or "").lower()
    if any(s in msg for s in ("fund", "not enough", "no enough", "insufficient")):
        return ("This service is temporarily unavailable. "
                "Try a smaller quantity or another option.")
    if "link" in msg:
        return "That link was rejected. Check it and try again."
    if any(s in msg for s in ("quantity", "min", "max")):
        return "Quantity is outside the allowed range for this service."
    if "service" in msg:
        return "That service just left the catalogue. Pick another."
    return "Could not place that order. Nothing was charged. Try another service."


class SmmShop:
    """One shop instance per process; clones share it and pass extra %."""

    def __init__(
        self,
        provider,
        catalog: SmmCatalog,
        wallets,
        *,
        extra_rate: Decimal = Decimal("0"),
        notify: Optional[NotifyFn] = None,
        alert: Optional[AlertFn] = None,
        announce: Optional[Callable[..., None]] = None,
        margin_fee_rate: Decimal = Decimal("0.05"),
    ) -> None:
        self.provider = provider
        self.catalog = catalog
        self.wallets = wallets
        self.extra_rate = Decimal(extra_rate or 0)
        self.notify = notify
        self.alert = alert
        self.announce = announce
        self.margin_fee_rate = Decimal(margin_fee_rate or 0)
        self.last_usd: Optional[Decimal] = None
        self._lock = threading.Lock()

    # -- quoting ---------------------------------------------------------
    def quote(self, svc: SmmService, quantity: int, *, extra_rate: Decimal = Decimal("0")) -> Money:
        extra = extra_rate if extra_rate else self.extra_rate
        return self.catalog.quote(svc, quantity, extra_rate=extra)

    def _store(self):
        inner = getattr(self.wallets, "_inner", None)
        return inner if inner is not None else self.wallets

    def _smm(self):
        store = self.wallets
        smm = getattr(store, "_smm", None)
        if smm is not None:
            return smm
        # Scoped wrapper: inner store.
        inner = getattr(store, "_inner", None)
        return getattr(inner, "_smm", None) if inner is not None else None

    # -- place -----------------------------------------------------------
    def place(
        self,
        user_id: str,
        svc: SmmService,
        quantity: int,
        link: str,
        *,
        extra_rate: Decimal = Decimal("0"),
        clone_owner: str = "",
        scope: str = "",
        wallets=None,
    ) -> SmmOrderRow:
        """Debit, add upstream, persist. Raises :class:`SmmUserError` on refusal."""
        wallets = wallets if wallets is not None else self.wallets
        extra = Decimal(extra_rate or 0)
        link = (link or "").strip()
        if len(link) < 3:
            raise SmmUserError("Send the public link to the profile or post.")
        try:
            sell = self.catalog.quote(svc, quantity, extra_rate=extra)
        except SmmError as exc:
            raise SmmUserError(str(exc)) from exc
        balance = wallets.balance(user_id)
        if balance.paise < sell.paise:
            short = sell - balance
            raise SmmUserError(
                f"That costs {sell}; your balance is {balance}. "
                f"Top up at least {short} more (💰 Wallet → ➕ Add money)."
            )
        usd = svc.cost_usd(quantity)
        try:
            pbal, _cur = self.provider.get_balance()
            self.last_usd = pbal
        except SmmError as exc:
            log.warning("smm provider balance check failed: %s", exc)
            pbal = None
        if pbal is not None and pbal < usd:
            raise SmmUserError(
                "This service is temporarily unavailable. "
                "Try a smaller quantity or another option."
            )
        try:
            try:
                wallets.adjust(
                    user_id, Money(-sell.paise), kind="boost", note=svc.name)
            except TypeError:
                wallets.adjust(user_id, Money(-sell.paise))
        except Exception as exc:  # noqa: BLE001
            raise SmmUserError("Could not debit your wallet. Try again.") from exc
        extra_json = ""
        if clone_owner:
            extra_json = json.dumps({"clone_owner": clone_owner, "extra_rate": str(extra)})
        try:
            pid = self.provider.add_order(svc.service_id, link, quantity)
        except SmmAmbiguous as exc:
            # Timeout / 5xx: keep debit, never retry add, persist unknown.
            log.warning("smm add ambiguous for %s: %s", user_id, exc)
            try:
                oid = wallets.create_smm_order(
                    user_id=user_id,
                    service_id=svc.service_id,
                    service_name=svc.name,
                    platform=svc.platform,
                    category=svc.category,
                    link=link,
                    quantity=int(quantity),
                    charge=sell,
                    cost=self.catalog.cost_inr(svc, quantity),
                    provider_order_id="",
                    status="pending",
                    remains=int(quantity),
                    refillable=svc.refill,
                    cancelable=svc.cancel,
                    extra=extra_json,
                )
                wallets.update_smm_order(oid, last_error=f"ambiguous add: {exc}")
            except Exception:  # noqa: BLE001
                log.exception("smm persist failed after ambiguous add")
            self._ping_owner(
                f"SMM add timed out for user {user_id} service {svc.service_id} "
                f"qty {quantity} charge {sell}. Debit kept, order pending/unknown. "
                f"Check the supplier for an untracked order — do not retry add."
            )
            raise SmmUserError(
                "Order is pending confirmation. You were charged once and "
                "won't be charged again. If it doesn't appear, message the owner."
            ) from exc
        except SmmError as exc:
            self._refund_wallet(wallets, user_id, sell)
            raise SmmUserError(_friendly_add_error(exc)) from exc
        try:
            oid = wallets.create_smm_order(
                user_id=user_id,
                service_id=svc.service_id,
                service_name=svc.name,
                platform=svc.platform,
                category=svc.category,
                link=link,
                quantity=int(quantity),
                charge=sell,
                cost=self.catalog.cost_inr(svc, quantity),
                provider_order_id=str(pid),
                status="pending",
                remains=int(quantity),
                refillable=svc.refill,
                cancelable=svc.cancel,
                extra=extra_json,
            )
        except Exception as exc:  # noqa: BLE001 - we have an upstream order
            log.exception("smm persist failed after add %s", pid)
            self._ping_owner(
                f"SMM order {pid} placed upstream but failed to save "
                f"({type(exc).__name__}). User {user_id} was charged {sell}."
            )
            raise SmmUserError(
                "Order was sent but we couldn't save the receipt. "
                "Contact the owner with this time — don't reorder yet."
            ) from exc
        row = wallets.get_smm_order(oid, user_id=user_id)
        if row is None:
            # Scope-less fallback (tests / unscoped store).
            row = self._store_get(oid)
        self._announce_boost(svc.name, sell, delivered=False)
        return row

    def _store_get(self, oid: int) -> SmmOrderRow:
        smm = self._smm()
        row = smm.get(oid) if smm is not None else None
        if row is None:
            raise SmmUserError("Order vanished after placing. Contact the owner.")
        return row

    @staticmethod
    def _refund_wallet(wallets, user_id: str, amount: Money, *, note: str = "") -> None:
        if amount.is_zero:
            return
        try:
            try:
                wallets.adjust(
                    user_id, amount, kind="boost_refund", note=note)
            except TypeError:
                wallets.adjust(user_id, amount)
        except Exception:  # noqa: BLE001
            log.exception("smm refund-after-fail could not credit %s %s", user_id, amount)

    def _announce_boost(self, service: str, amount: Money, *,
                        delivered: bool = False) -> None:
        """Public updates-channel post. Never includes the customer, OTP, or link."""
        fn = self.announce
        if not callable(fn):
            return
        try:
            fn(service, amount, delivered=delivered)
        except TypeError:
            try:
                fn(service, amount)
            except Exception:  # noqa: BLE001
                log.debug("smm updates announce failed", exc_info=True)
        except Exception:  # noqa: BLE001
            log.debug("smm updates announce failed", exc_info=True)

    def _ping_owner(self, text: str) -> None:
        if self.alert is None:
            log.warning("%s", text)
            return
        try:
            self.alert(text)
        except Exception:  # noqa: BLE001
            log.warning("smm owner alert failed", exc_info=True)

    def _notify(self, scope: str, user_id: str, text: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(scope, user_id, text)
        except Exception:  # noqa: BLE001
            log.warning("smm customer notify failed", exc_info=True)

    # -- live status -----------------------------------------------------
    def refresh(self, order: SmmOrderRow, *, wallets=None) -> SmmOrderRow:
        """Pull upstream status and settle money if the order went terminal."""
        wallets = wallets if wallets is not None else self.wallets
        if not order.provider_order_id:
            return order
        if order.status not in OPEN_STATUSES and order.status != "pending":
            # Still allow a manual refresh of a live-looking row.
            if order.status in {"completed", "partial", "canceled", "refunded", "failed"}:
                try:
                    st = self.provider.get_status(order.provider_order_id)
                except SmmError:
                    return order
                return self._apply(order, st, wallets=wallets)
        try:
            st = self.provider.get_status(order.provider_order_id)
        except SmmError as exc:
            log.warning("smm status failed for %s: %s", order.id, exc)
            return order
        return self._apply(order, st, wallets=wallets)

    def poll_open(self, *, limit: int = 100) -> list[SmmNotice]:
        """One pass over open orders. Returns customer notices to send."""
        smm = self._smm()
        if smm is None:
            return []
        try:
            open_rows = smm.list_open(limit=limit)
        except Exception:  # noqa: BLE001
            log.exception("smm list_open failed")
            return []
        notices: list[SmmNotice] = []
        # Batch by upstream id.
        ids = [r.provider_order_id for r in open_rows if r.provider_order_id]
        statuses: dict[str, SmmStatus] = {}
        if ids:
            try:
                statuses = self.provider.get_statuses(ids)
            except SmmError as exc:
                log.warning("smm batch status failed: %s", exc)
                return []
        platform = self._store()
        for row in open_rows:
            st = statuses.get(row.provider_order_id)
            if st is None:
                continue
            before = row.status
            try:
                updated = self._apply(row, st, wallets=platform)
            except Exception:  # noqa: BLE001 - never kill the poller
                log.exception("smm apply failed for %s", row.id)
                continue
            if updated.status != before and updated.status not in OPEN_STATUSES:
                notices.append(SmmNotice(
                    scope=updated.scope,
                    user_id=updated.user_id,
                    text=self._customer_update(updated),
                ))
                if updated.status == "completed":
                    self._announce_boost(
                        updated.service_name or f"#{updated.service_id}",
                        updated.charge, delivered=True)
        return notices

    def _apply(self, order: SmmOrderRow, st: SmmStatus, *, wallets) -> SmmOrderRow:
        new_status = st.normalized or order.status
        if st.error and new_status == "failed" and order.status in OPEN_STATUSES:
            # "Incorrect order ID" on a live row is a blip, not a refund.
            log.info("smm status error for %s: %s", order.id, st.error)
            return order
        refund_delta = self._refund_due(order, new_status, st.remains)
        new_refunded = order.refunded
        # Always the unscoped store: clone wallets prefix on adjust, and
        # ``smm_orders`` lives on the platform connection.
        platform = getattr(wallets, "_inner", None) or wallets
        if refund_delta.paise > 0:
            # Credit the *scoped* wallet: platform store keys are
            # ``scope:uid`` for clones, bare uid otherwise.
            wallet_uid = f"{order.scope}:{order.user_id}" if order.scope else order.user_id
            try:
                try:
                    platform.adjust(
                        wallet_uid, refund_delta,
                        kind="boost_refund",
                        note=order.service_name or f"#{order.service_id}",
                    )
                except TypeError:
                    platform.adjust(wallet_uid, refund_delta)
            except Exception:  # noqa: BLE001
                log.exception("smm partial refund credit failed for %s", order.id)
                # Don't mark refunded; next poll retries.
                return order
            new_refunded = Money(order.refunded.paise + refund_delta.paise)
            log.info("smm refunded %s on order %s (%s)", refund_delta, order.id, new_status)
        earnings_paid = order.earnings_paid
        if (not earnings_paid) and new_status in {"completed", "partial"}:
            if self._maybe_credit_clone(order, new_refunded, platform):
                earnings_paid = True
        smm = getattr(platform, "_smm", None) or self._smm()
        if smm is not None:
            smm.update(
                order.id,
                status=new_status,
                remains=int(st.remains),
                start_count=int(st.start_count),
                refunded=new_refunded,
                earnings_paid=earnings_paid,
            )
            fresh = smm.get(order.id)
            if fresh is not None:
                return fresh
        return order

    @staticmethod
    def _refund_due(order: SmmOrderRow, new_status: str, remains: int) -> Money:
        """How much MORE to credit now. Never negative, never over-refund."""
        already = order.refunded.paise
        charge = order.charge.paise
        if charge <= 0:
            return Money(0)
        if new_status in {"canceled", "refunded", "failed"}:
            due = charge - already
            return Money(due if due > 0 else 0)
        if new_status == "partial":
            qty = order.quantity or 0
            if qty <= 0:
                return Money(0)
            remains = max(0, min(int(remains), qty))
            # Floor so we never over-refund the customer.
            due_total = (charge * remains) // qty
            delta = due_total - already
            return Money(delta if delta > 0 else 0)
        if new_status == "completed" and remains > 0:
            # Some panels mark completed with leftover remains -- treat as partial.
            return SmmShop._refund_due(order, "partial", remains)
        return Money(0)

    def _maybe_credit_clone(self, order: SmmOrderRow, refunded: Money, wallets) -> bool:
        """Credit clone owner on terminal net. True = done (or nothing to do)."""
        if not order.extra:
            return True
        try:
            data = json.loads(order.extra)
        except Exception:  # noqa: BLE001
            return True
        owner = str((data or {}).get("clone_owner") or "")
        try:
            extra = Decimal(str((data or {}).get("extra_rate") or "0"))
        except Exception:  # noqa: BLE001
            extra = Decimal("0")
        if not owner or extra <= 0:
            return True
        net = Money(max(0, order.charge.paise - refunded.paise))
        if net.is_zero:
            return True
        split = reseller_split(net, extra, self.margin_fee_rate)
        if split.owner_share.is_zero:
            return True
        try:
            credit_earnings(wallets, owner, split.owner_share)
            return True
        except Exception:  # noqa: BLE001
            log.exception("smm clone earnings credit failed for %s", owner)
            return False

    @staticmethod
    def _customer_update(order: SmmOrderRow) -> str:
        name = order.service_name or f"#{order.service_id}"
        if order.status == "completed":
            return (
                f"✅ Social boost delivered\n\n"
                f"{name}\n"
                f"Qty {order.quantity:,} · charged {order.charge}\n"
                f"Tap 📣 Social boost → My boosts for the receipt."
            )
        if order.status == "partial":
            delivered = max(0, order.quantity - order.remains)
            extra = (
                f"\n↩️ {order.refunded} returned for the undelivered part."
                if order.refunded.paise > 0 else ""
            )
            return (
                f"📦 Social boost partially delivered\n\n"
                f"{name}\n"
                f"{delivered:,} of {order.quantity:,} landed.{extra}"
            )
        if order.status in {"canceled", "refunded", "failed"}:
            extra = (
                f" {order.refunded} returned to your wallet."
                if order.refunded.paise > 0 else ""
            )
            return (
                f"♻️ Social boost cancelled\n\n"
                f"{name}.{extra}"
            )
        return f"📣 Social boost update: {name} is now {order.status}."

    def request_refill(self, order: SmmOrderRow) -> str:
        if not order.refillable:
            raise SmmUserError("This service has no refill.")
        if order.status not in {"completed", "partial"}:
            raise SmmUserError("Refill is only available after delivery.")
        try:
            rid = self.provider.refill(order.provider_order_id)
        except SmmError as exc:
            raise SmmUserError("Refill isn't available on this order right now.") from exc
        return rid

    def request_cancel(self, order: SmmOrderRow) -> None:
        if not order.cancelable:
            raise SmmUserError("This service can't be cancelled once placed.")
        if order.status not in OPEN_STATUSES:
            raise SmmUserError("This order is already finished.")
        try:
            self.provider.cancel([order.provider_order_id])
        except SmmError as exc:
            raise SmmUserError(
                "Cancel request didn't go through. We'll keep tracking the order."
            ) from exc

    def start_poller(self, stop: threading.Event, *, interval: float = 45.0) -> threading.Thread:
        def run() -> None:
            while not stop.is_set():
                try:
                    notices = self.poll_open()
                    for n in notices:
                        self._notify(n.scope, n.user_id, n.text)
                except Exception:  # noqa: BLE001 - never kill the thread
                    log.exception("smm poller pass failed")
                if interval <= 0:
                    return
                stop.wait(interval)

        thread = threading.Thread(target=run, name="smm-poll", daemon=True)
        thread.start()
        log.info("smm poller started (every %ss)", interval)
        return thread
