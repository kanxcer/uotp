"""Deterministic mock provider.

Used for tests, offline development and the Monte Carlo validation in
``cli.py simulate``. It is deterministic by default (seeded), because a
flaky random provider makes economics tests impossible to assert on.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

from ..catalog import PROVIDER_VALIDITY_MINUTES
from ..money import INR, Money
from .base import (
    Balance,
    InsufficientBalance,
    NumberAllocation,
    NumberUnavailable,
    OtpResult,
    SmsMessage,
)

__all__ = ["MockProvider", "MockOutcome"]


@dataclass(frozen=True, slots=True)
class MockOutcome:
    """Forced result for the next ``buy_number`` call, for test scenarios."""

    kind: str  # "success" | "burned" | "silent" | "unavailable"
    otp: Optional[str] = None
    sender: str = "OTP"


class MockProvider:
    """In-memory provider that behaves like the real one, minus the network."""

    name = "mock"

    def __init__(
        self,
        prices: Mapping[str, Money],
        *,
        balance: Money = INR(1000),
        success_rates: Optional[Mapping[str, float]] = None,
        seed: int = 1234,
        validity_minutes: int = PROVIDER_VALIDITY_MINUTES,
    ) -> None:
        self._prices = {k: v for k, v in prices.items()}
        self._balance = balance
        self._rates = dict(success_rates or {})
        self._rng = random.Random(seed)
        self._validity = validity_minutes
        # A FIFO queue, not a single slot: tests routinely need to script a
        # whole sequence ("fail, fail, succeed") to exercise the retry path.
        self._forced: deque[MockOutcome] = deque()
        self._counter = 0
        self.purchases: list[NumberAllocation] = []
        self.cancellations: list[str] = []

    # -- test hooks ------------------------------------------------------
    def force_next(self, outcome: MockOutcome) -> None:
        """Queue one scripted outcome, consumed by the next SMS lookup."""
        self._forced.append(outcome)

    def force_sequence(self, outcomes: Sequence[MockOutcome]) -> None:
        """Queue several outcomes at once."""
        self._forced.extend(outcomes)

    def set_balance(self, amount: Money) -> None:
        self._balance = amount

    def set_success_rate(self, service: str, rate: float) -> None:
        """Pin a service's delivery probability (0.0 = always fails)."""
        self._rates[service] = rate

    # -- Provider implementation ----------------------------------------
    def get_balance(self) -> Balance:
        return Balance(credit=self._balance)

    def get_prices(self) -> Mapping[str, Money]:
        return dict(self._prices)

    def buy_number(
        self, service: str, country: str = "in", *,
        idempotency_key: Optional[str] = None, server: str = "",
    ) -> NumberAllocation:
        # Idempotency: same key returns the same allocation, no second charge.
        if idempotency_key:
            for existing in self.purchases:
                if existing.order_id == f"mock-{idempotency_key}":
                    return existing
        if service not in self._prices:
            raise NumberUnavailable(f"mock has no price for {service!r}")
        price = self._prices[service]
        if self._balance.paise < price.paise:
            raise InsufficientBalance(
                f"need {price}, have {self._balance}",
                shortfall=price - self._balance,
            )
        self._balance = self._balance - price
        self._counter += 1
        order_id = f"mock-{idempotency_key}" if idempotency_key else f"mock-{self._counter}"
        alloc = NumberAllocation(
            order_id=order_id,
            phone=f"+919{self._rng.randint(10**8, 10**9 - 1)}",
            service=service,
            country=country,
            charged=price,
            validity_minutes=self._validity,
        )
        self.purchases.append(alloc)
        return alloc

    def _resolve(self, service: str) -> MockOutcome:
        if self._forced:
            return self._forced.popleft()
        p = self._rates.get(service, 0.9)
        return MockOutcome("success", otp=f"{self._rng.randint(100000, 999999)}") \
            if self._rng.random() < p else MockOutcome("silent")

    def get_sms(self, order_id: str) -> Sequence[SmsMessage]:
        alloc = next((a for a in self.purchases if a.order_id == order_id), None)
        if alloc is None:
            return []
        outcome = self._resolve(alloc.service)
        if outcome.kind != "success" or not outcome.otp:
            return []
        return [
            SmsMessage(
                sender=outcome.sender,
                text=f"Your verification code is {outcome.otp}. Valid 10 minutes.",
                received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        ]

    def wait_for_otp(
        self,
        allocation: NumberAllocation,
        *,
        timeout_seconds: float = 300.0,
        poll_interval: float = 3.0,
        expect: Optional[str] = None,
        adaptive: bool = False,
    ) -> OtpResult:
        for msg in self.get_sms(allocation.order_id):
            code = msg.extract_otp()
            if code:
                return OtpResult(code, msg, 1, 0.4, False)
        return OtpResult(None, None, 1, timeout_seconds, True)

    def cancel(self, order_id: str) -> Money:
        alloc = next((a for a in self.purchases if a.order_id == order_id), None)
        if alloc is None:
            return Money(0)
        self._balance = self._balance + alloc.charged
        self.cancellations.append(order_id)
        return alloc.charged
