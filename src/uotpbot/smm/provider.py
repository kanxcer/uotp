"""Provider-agnostic types for the social-boost shop.

Money the customer sees is always INR (:class:`~uotpbot.money.Money`).
Upstream panels quote USD-per-1000; that stays a ``Decimal`` so we never
accidentally treat dollars as rupees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

__all__ = [
    "SmmError",
    "SmmAuthError",
    "SmmProviderError",
    "SmmAmbiguous",
    "SmmUserError",
    "SmmStatus",
    "SmmProvider",
    "normalize_status",
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
]


class SmmError(Exception):
    """Base class for social-boost failures."""


class SmmAuthError(SmmError):
    """Rejected credentials. Do not retry -- fix the key."""


class SmmProviderError(SmmError):
    """Upstream refused the call with a known error (safe to surface / refund)."""


class SmmAmbiguous(SmmError):
    """The request may or may not have completed.

    Same shape as the OTP :class:`~uotpbot.provider.base.PurchaseTimedOut`:
    callers must NOT retry ``add`` blindly or they can double-order.
    """


class SmmUserError(SmmError):
    """A customer-safe refusal (short balance, bad qty, bad link)."""


#: Statuses that still need polling.
OPEN_STATUSES = frozenset({"pending", "in_progress"})
#: Statuses that will not change again (money is settled after one pass).
TERMINAL_STATUSES = frozenset({
    "completed", "partial", "canceled", "refunded", "failed",
})


def normalize_status(raw: str) -> str:
    """Map PerfectPanel-family status strings onto our small vocabulary."""
    token = (raw or "").strip().lower().replace("_", " ").replace("-", " ")
    token = " ".join(token.split())
    if token in {"cancelled", "canceled", "cancel"}:
        return "canceled"
    if token in {"in progress", "inprogress", "processing", "awaiting", "working"}:
        return "in_progress"
    if token in {"completed", "complete", "done"}:
        return "completed"
    if token in {"partial", "partially completed"}:
        return "partial"
    if token in {"refunded", "refund"}:
        return "refunded"
    if token in {"pending", "waiting"}:
        return "pending"
    if token in {"fail", "failed", "error", "rejected"}:
        return "failed"
    return token or "pending"


@dataclass(frozen=True, slots=True)
class SmmStatus:
    """One upstream order's live state."""

    order_id: str
    status: str
    charge: Decimal = Decimal("0")
    remains: int = 0
    start_count: int = 0
    currency: str = "USD"
    error: str = ""

    @property
    def normalized(self) -> str:
        return normalize_status(self.status)

    @property
    def is_open(self) -> bool:
        return self.normalized in OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.normalized in TERMINAL_STATUSES


@runtime_checkable
class SmmProvider(Protocol):
    """What the shop needs from a social-boost supplier."""

    name: str

    def get_balance(self) -> tuple[Decimal, str]:
        """``(amount, currency)`` as the panel reports it (usually USD)."""
        ...

    def list_services(self) -> Sequence[Mapping[str, object]]:
        """Raw catalogue rows. The catalog layer interprets them."""
        ...

    def add_order(self, service_id: str, link: str, quantity: int) -> str:
        """Place a Default-type order. Returns the upstream order id.

        Must not be retried by the client on timeout -- that is
        :class:`SmmAmbiguous`.
        """
        ...

    def get_status(self, order_id: str) -> SmmStatus: ...

    def get_statuses(self, order_ids: Sequence[str]) -> dict[str, SmmStatus]:
        """Batch status; missing ids are omitted or carry ``error``."""
        ...

    def refill(self, order_id: str) -> str:
        """Request a refill. Returns a refill id."""
        ...

    def cancel(self, order_ids: Sequence[str]) -> Mapping[str, object]:
        """Request cancellation. Not guaranteed; caller re-polls status."""
        ...
