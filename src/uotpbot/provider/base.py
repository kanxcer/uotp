"""Provider abstraction.

The bot never talks to UOTP directly; it talks to this interface. That keeps
the money code testable offline and means a second provider can be added (or
UOTP's endpoints changed) without touching pricing or the ledger.

UOTP's API is not publicly documented, so the concrete adapter in
``uotp.py`` takes its endpoint paths, auth header style and response field
names from configuration rather than hard-coding guesses. Point it at the real
endpoints from your account and map the JSON keys; no code change needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..catalog import PROVIDER_VALIDITY_MINUTES
from ..money import Money

__all__ = [
    "ProviderError",
    "InsufficientBalance",
    "ServiceUnavailable",
    "NumberUnavailable",
    "PurchaseTimedOut",
    "AuthError",
    "Balance",
    "NumberAllocation",
    "SmsMessage",
    "OtpResult",
    "Provider",
]


class ProviderError(Exception):
    """Base class for provider failures."""


class AuthError(ProviderError):
    """Rejected credentials. Do not retry -- fix the key."""


class InsufficientBalance(ProviderError):
    """Wallet cannot cover the purchase. Carry the shortfall in ``shortfall``."""

    def __init__(self, message: str, *, shortfall: Money = Money(0)) -> None:
        super().__init__(message)
        self.shortfall = shortfall


class ServiceUnavailable(ProviderError):
    """Provider has no stock for this service/country right now. Retryable."""


class NumberUnavailable(ServiceUnavailable):
    """Specifically: no free number in the requested pool."""


class PurchaseTimedOut(ProviderError):
    """The request may or may not have completed.

    This is the dangerous case: the number may already be allocated and
    charged. Callers MUST reconcile by idempotency key before retrying, or
    they will silently buy two numbers and pay twice.
    """


@dataclass(frozen=True, slots=True)
class Balance:
    """Wallet state as the provider reports it."""

    credit: Money
    currency: str = "INR"
    as_of: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def can_afford(self, amount: Money) -> bool:
        return self.credit.paise >= amount.paise

    def shortfall_for(self, amount: Money) -> Money:
        """How much more is needed to afford ``amount`` (zero if enough)."""
        if self.can_afford(amount):
            return Money.zero()
        return amount - self.credit


@dataclass(frozen=True, slots=True)
class SmsMessage:
    """One SMS that landed on a rented number."""

    sender: str
    text: str
    received_at: str

    def extract_otp(self, *, min_len: int = 4, max_len: int = 8) -> Optional[str]:
        """Pull the OTP out of the message body.

        Takes the first standalone digit run of plausible OTP length. Anchoring
        on word boundaries avoids grabbing the last four of a phone number or
        the "2024" out of a date. Longer runs are preferred because a 6-digit
        code contains a 4-digit substring and picking the short one would be
        wrong.
        """
        import re

        best: Optional[str] = None
        for match in re.finditer(r"(?<!\d)(\d{%d,%d})(?!\d)" % (min_len, max_len), self.text):
            code = match.group(1)
            if best is None or len(code) > len(best):
                best = code
            if len(best) == max_len:
                break
        return best


@dataclass(frozen=True, slots=True)
class OtpResult:
    """Outcome of waiting for an OTP on an allocated number."""

    code: Optional[str]
    message: Optional[SmsMessage]
    attempts: int
    elapsed_seconds: float
    #: True when we stopped waiting because the number expired, not because we
    #: found a code. Drives the refund decision.
    timed_out: bool

    @property
    def success(self) -> bool:
        return self.code is not None


@dataclass(frozen=True, slots=True)
class NumberAllocation:
    """A purchased number and its lease."""

    order_id: str
    phone: str
    service: str
    country: str
    charged: Money
    allocated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    validity_minutes: int = PROVIDER_VALIDITY_MINUTES

    @property
    def expires_at(self) -> datetime:
        return datetime.fromisoformat(self.allocated_at) + timedelta(minutes=self.validity_minutes)

    def seconds_left(self, now: Optional[datetime] = None) -> float:
        return (self.expires_at - (now or datetime.now(timezone.utc))).total_seconds()

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return self.seconds_left(now) <= 0


@runtime_checkable
class Provider(Protocol):
    """What the bot needs from a virtual-number supplier."""

    name: str

    def get_balance(self) -> Balance: ...

    def get_prices(self) -> Mapping[str, Money]:
        """Live sticker price per service slug, as the provider will charge."""
        ...

    def buy_number(
        self, service: str, country: str = "in", *,
        idempotency_key: Optional[str] = None, server: str = "",
    ) -> NumberAllocation: ...

    def get_sms(self, order_id: str) -> Sequence[SmsMessage]: ...

    def wait_for_otp(
        self,
        allocation: NumberAllocation,
        *,
        timeout_seconds: float = 300.0,
        poll_interval: float = 3.0,
        expect: Optional[str] = None,
        adaptive: bool = False,
    ) -> OtpResult: ...

    def cancel(self, order_id: str) -> Money:
        """Release a number and return the amount credited back."""
        ...
