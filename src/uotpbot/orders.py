"""Order lifecycle and the retry policy.

The most expensive bug in an OTP reseller bot is blind retrying. A customer
pays once; the bot buys number after number hoping one works, and a Rs.12 order
quietly costs Rs.60. The state machine here makes that impossible by deciding
*before* each purchase whether another attempt is still worth buying.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from .economics import OrderEconomics
from .money import Money, quantize_money

__all__ = ["OrderState", "RetryDecision", "Order", "retry_policy"]


class OrderState(str, Enum):
    """Order lifecycle. Transitions are one-way except the refund edge."""

    CREATED = "created"
    PAID = "paid"
    PURCHASING = "purchasing"
    AWAITING_OTP = "awaiting_otp"
    DELIVERED = "delivered"
    FAILED = "failed"
    REFUNDED = "refunded"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.DELIVERED, OrderState.FAILED, OrderState.REFUNDED)


#: Legal transitions. Anything else is a bug and raises.
ALLOWED: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.PAID, OrderState.FAILED}),
    OrderState.PAID: frozenset({OrderState.PURCHASING, OrderState.FAILED,
                                OrderState.REFUNDED}),
    OrderState.PURCHASING: frozenset({OrderState.AWAITING_OTP, OrderState.PURCHASING,
                                      OrderState.FAILED, OrderState.REFUNDED}),
    OrderState.AWAITING_OTP: frozenset({OrderState.PURCHASING, OrderState.DELIVERED,
                                        OrderState.FAILED}),
    OrderState.FAILED: frozenset({OrderState.REFUNDED}),
    OrderState.DELIVERED: frozenset(),
    OrderState.REFUNDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Whether to buy another number, and why."""

    retry: bool
    reason: str
    #: What one more attempt costs, in real rupees.
    marginal_cost: Money
    #: What a success on that attempt is worth, in real rupees.
    expected_value: Money

    @property
    def edge(self) -> Money:
        """Expected gain from trying once more. Negative means stop."""
        return self.expected_value - self.marginal_cost


def retry_policy(
    econ: OrderEconomics,
    *,
    attempts_so_far: int,
    retry_cap: int,
    spent: Money,
) -> RetryDecision:
    """Decide whether to buy one more number.

    The spent money is *sunk* and must be ignored -- that is the whole point.
    The decision compares only:

        cost of one more number   vs   P(it works) x what a delivery earns

    This myopic rule is provably optimal for a stationary process when you may
    stop at any attempt. Letting ``W`` be the value of being about to try::

        W = max(0, -c + p*net + (1-p)*W)   =>   W = net - c/p   (if positive)

    so continuing is right exactly when ``p*net > c``. Note ``c/p`` is the
    expected cost per successful delivery, which is why this rule and
    :attr:`OrderEconomics.expected_contribution` agree at a large retry cap --
    a consistency the test-suite asserts.

    ``spent`` is carried only for reporting and the hard stop; it never makes a
    losing retry look attractive.
    """
    p = econ.delivery.success_rate
    marginal = quantize_money(
        Decimal(econ.sticker_price.rupees)
        * econ.delivery.net_charge_per_attempt_rate
        * econ.wallet_multiplier
    )
    value = quantize_money(Decimal(econ.net_proceeds.rupees) * p)

    if attempts_so_far >= retry_cap:
        return RetryDecision(
            False, f"retry cap of {retry_cap} reached", marginal, value
        )
    if marginal.is_zero and value.is_zero:
        return RetryDecision(False, "no cost and no value; nothing to decide", marginal, value)
    if p <= Decimal(0):
        return RetryDecision(False, "provider success rate is zero", marginal, value)

    # Hard ceiling: never let one order cost more than the price paid for it
    # plus the margin on it. Protects against a mis-estimated success rate
    # turning a Rs.15 order into a Rs.200 hole.
    ceiling = econ.gross_price + econ.gross_margin
    if spent.paise > 0 and spent + marginal > ceiling and ceiling.paise > 0:
        return RetryDecision(
            False,
            f"spend {spent} + {marginal} would breach the {ceiling} loss ceiling",
            marginal,
            value,
        )

    if value.paise <= marginal.paise:
        return RetryDecision(
            False,
            f"one more number costs {marginal} but is worth only {value} "
            f"at p={p:.1%}; stopping is cheaper than continuing",
            marginal,
            value,
        )
    return RetryDecision(
        True,
        f"worth {value} against a {marginal} cost at p={p:.1%}",
        marginal,
        value,
    )


@dataclass(slots=True)
class Order:
    """One customer order, with a full audit trail of money and attempts."""

    customer_id: str
    service: str
    gross_price: Money
    order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    state: OrderState = OrderState.CREATED
    country: str = "in"
    phone: Optional[str] = None
    otp: Optional[str] = None
    provider_order_ids: list[str] = field(default_factory=list)
    attempts: int = 0
    spent: Money = field(default_factory=Money.zero)
    recovered: Money = field(default_factory=Money.zero)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    closed_at: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def transition(self, target: OrderState) -> None:
        if target not in ALLOWED[self.state]:
            raise ValueError(
                f"illegal order transition {self.state.value} -> {target.value} "
                f"(allowed: {sorted(s.value for s in ALLOWED[self.state])})"
            )
        self.state = target
        if target.is_terminal:
            self.closed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def note(self, message: str) -> None:
        self.notes.append(message)

    # -- money -----------------------------------------------------------
    def record_attempt(self, charged: Money, provider_order_id: str) -> None:
        self.attempts += 1
        self.spent = self.spent + charged
        self.provider_order_ids.append(provider_order_id)

    def record_recovery(self, amount: Money) -> None:
        self.recovered = self.recovered + amount

    @property
    def net_spend(self) -> Money:
        """Real cost of this order after provider refunds."""
        return self.spent - self.recovered

    def realized_profit(self, fees_net_proceeds: Money, refunded: Money) -> Money:
        """Actual profit on this order, from the books.

        ``fees_net_proceeds`` is what the sale netted after PSP fees and GST;
        ``refunded`` is what went back to the customer. Subtracting real spend
        gives a figure that can be reconciled against the ledger.
        """
        return fees_net_proceeds - refunded - self.net_spend

    def to_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "service": self.service,
            "country": self.country,
            "state": self.state.value,
            "gross_price": self.gross_price.to_plain(),
            "phone": self.phone,
            "otp": self.otp,
            "attempts": self.attempts,
            "spent": self.spent.to_plain(),
            "recovered": self.recovered.to_plain(),
            "net_spend": self.net_spend.to_plain(),
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }
