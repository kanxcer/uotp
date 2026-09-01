"""Unit economics.

This module answers one question with no hand-waving: **what does one customer
order actually earn, and what does it cost to break even?**

The central mistake this code exists to prevent is pricing off the *sticker
price of a single number*. The real cost of one delivered OTP is much higher
than the sticker price, because:

1.  A number can arrive already burned (rejected by the target service) -- you
    paid for it, you get nothing, and you buy another.
2.  A number can be live but never receive the OTP -- you paid, and at best you
    get a partial wallet refund.
3.  Each of those retries is a fresh charge, so the cost of one *successful*
    delivery is the sticker price inflated by 1/p(success).
4.  Wallet top-up bonuses and payment-rail fees scale the whole thing.
5.  On the way in, the payment gateway, GST and disputes take a cut of revenue.

Get any one of those wrong and a service that looks 50% margin at the sticker
price is actually loss-making. :class:`OrderEconomics` composes all five.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Optional

from .catalog import ServiceCost
from .money import (
    Money,
    Rate,
    quantize_money,
    rate,
)

__all__ = [
    "DeliveryModel",
    "FeeModel",
    "OrderEconomics",
    "PricingAdvice",
    "EconomicsError",
]

_ONE = Decimal(1)
_ZERO = Decimal(0)


class EconomicsError(Exception):
    """Raised when an economics request has no finite answer."""


def _prob(value: object, name: str) -> Decimal:
    d = Decimal(value)  # type: ignore[arg-type]
    if not (_ZERO <= d <= _ONE):
        raise ValueError(f"{name} must be in [0, 1], got {value!r}")
    return d


@dataclass(frozen=True, slots=True)
class DeliveryModel:
    """Stochastic model of turning wallet credit into one usable OTP.

    Each *attempt* is one purchased number. It resolves to exactly one of::

        success  (prob ``success_rate``)  usable OTP arrived
        burned   (prob ``burn_rate``)     number already registered / rejected
        silent   (prob ``1 - s - b``)     number live, no OTP ever arrived

    Only ``silent`` attempts are refundable, and only for ``refund_share`` of
    the charge -- burned numbers were delivered as promised from the
    provider's point of view, so they are almost never refunded.

    ``retry_cap`` is how many numbers you will buy for one customer order
    before giving up. Capping matters: an uncapped retry loop turns a
    loss-making order into an unbounded one.
    """

    success_rate: Decimal
    burn_rate: Decimal
    refund_share: Decimal
    retry_cap: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "success_rate", _prob(self.success_rate, "success_rate"))
        object.__setattr__(self, "burn_rate", _prob(self.burn_rate, "burn_rate"))
        object.__setattr__(self, "refund_share", _prob(self.refund_share, "refund_share"))
        if self.success_rate + self.burn_rate > _ONE:
            raise ValueError(
                f"success_rate + burn_rate must be <= 1, got "
                f"{self.success_rate} + {self.burn_rate}"
            )
        if not isinstance(self.retry_cap, int) or isinstance(self.retry_cap, bool):
            raise TypeError("retry_cap must be an int")
        if self.retry_cap < 1:
            raise ValueError(f"retry_cap must be >= 1, got {self.retry_cap}")

    @classmethod
    def from_service(cls, cost: ServiceCost, retry_cap: int = 3) -> "DeliveryModel":
        return cls(cost.otp_success_rate, cost.burn_rate, cost.refund_share, retry_cap)

    # -- per-attempt -----------------------------------------------------
    @property
    def failure_rate(self) -> Decimal:
        """P(an attempt does not yield a usable OTP)."""
        return _ONE - self.success_rate

    @property
    def silent_rate(self) -> Decimal:
        """P(live number, no OTP) -- the only refundable failure mode."""
        return _ONE - self.success_rate - self.burn_rate

    @property
    def refund_per_attempt_rate(self) -> Decimal:
        """Expected refund per attempt, as a fraction of one sticker price."""
        return self.silent_rate * self.refund_share

    @property
    def net_charge_per_attempt_rate(self) -> Decimal:
        """Expected *net* charge per attempt, as a fraction of sticker price."""
        return _ONE - self.refund_per_attempt_rate

    # -- per-order (truncated geometric) ---------------------------------
    @property
    def order_success_rate(self) -> Decimal:
        """P(at least one attempt succeeds within ``retry_cap``)."""
        return _ONE - self.failure_rate**self.retry_cap

    @property
    def expected_attempts(self) -> Decimal:
        """Mean number of numbers bought per customer order.

        Truncated geometric: you stop at the first success or at the cap.
        ``sum k*p*q^(k-1) + N*q^N`` -- the last term is the capped tail where
        every attempt failed and you still paid for all N.
        """
        p, q, n = self.success_rate, self.failure_rate, self.retry_cap
        total = _ZERO
        # Iterate q^(k-1) multiplicatively rather than using ``q ** (k-1)``:
        # Decimal defines 0 ** 0 as InvalidOperation, so a service with a
        # 100% success rate (q == 0) would otherwise crash on the first term.
        qpow = _ONE
        for k in range(1, n + 1):
            total += Decimal(k) * p * qpow
            qpow *= q
        # qpow is now q**n: the capped tail, where every attempt failed.
        return total + Decimal(n) * qpow

    @property
    def expected_failed_attempts(self) -> Decimal:
        """Mean wasted numbers per order (attempts minus the one success)."""
        return self.expected_attempts - self.order_success_rate

    def expected_net_spend_rate(self, sticker: Money) -> Decimal:
        """Expected net wallet spend per *initiated* order, in rupees.

        Counts every attempt including the ones on orders that ultimately
        fail, net of the refunds you actually recover.
        """
        return Decimal(sticker.rupees) * self.expected_attempts * self.net_charge_per_attempt_rate


@dataclass(frozen=True, slots=True)
class FeeModel:
    """Everything that takes a cut between the customer and your bank.

    ``gateway_rate`` / ``gateway_fixed``
        The PSP's charge on a sale of gross size G: ``G*rate + fixed``.
        Razorpay-style UPI is near-zero, cards/netbanking ~2%, and most
        rails add a flat rupee or two.
    ``fee_gst_rate``
        GST on the gateway's own fee (18% in India). Modelled as a cost
        because a small operator generally cannot bank on input tax credit.
    ``gst_rate``
        Output GST on the sale itself, 18% for a registered digital service.
        Zero if you are under the registration threshold.
    ``gst_inclusive``
        Whether your advertised price already contains GST. This flips where
        the tax comes out of and changes break-even by ~15%.
    ``chargeback_rate``
        Fraction of delivered orders the customer disputes anyway. You refund
        the sale but the number is already consumed, so this is pure loss.
    """

    gateway_rate: Decimal = Decimal("0.02")
    gateway_fixed: Money = Money(0)
    fee_gst_rate: Decimal = Decimal("0.18")
    gst_rate: Decimal = Decimal("0.00")
    gst_inclusive: bool = True
    chargeback_rate: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        for name in ("gateway_rate", "fee_gst_rate", "gst_rate", "chargeback_rate"):
            object.__setattr__(self, name, _prob(getattr(self, name), name))
        if self.gateway_fixed.is_negative:
            raise ValueError("gateway_fixed cannot be negative")

    @property
    def revenue_factor(self) -> Decimal:
        """Fraction of the *quoted* price that is your revenue.

        Tax-inclusive: you quoted ``gross``, GST is inside it, so revenue is
        ``gross/(1+g)``. Tax-exclusive: ``gross`` was already the pre-tax
        figure, so all of it is revenue and GST is added on top.
        """
        return _ONE / (_ONE + self.gst_rate) if self.gst_inclusive else _ONE

    @property
    def collection_multiple(self) -> Decimal:
        """How much the customer actually pays, per rupee of quoted price.

        The PSP charges on what is *collected*, which under an exclusive price
        includes the GST -- so you pay gateway fees on tax that was never
        yours. Missing this understates fees by ~2% x 18% on every sale.
        """
        return _ONE if self.gst_inclusive else _ONE + self.gst_rate

    @property
    def keep_ratio(self) -> Decimal:
        """Fraction of the quoted price that survives GST and gateway fees.

        This is the slope of ``net_proceeds`` in the quoted price, and it is
        what makes both break-even formulas solvable in closed form.
        """
        return self.revenue_factor - self.collection_multiple * self.gateway_rate * (
            _ONE + self.fee_gst_rate
        )

    @property
    def fixed_cost(self) -> Decimal:
        """Price-independent part of the fee, in rupees."""
        return Decimal(self.gateway_fixed.rupees) * (_ONE + self.fee_gst_rate)

    def collected(self, gross: Money) -> Money:
        """Total the customer is charged for a quoted price of ``gross``."""
        if self.gst_inclusive:
            return gross
        return gross.scale(self.collection_multiple, ROUND_HALF_UP)

    def gross_fee(self, gross: Money) -> Money:
        """Total PSP charge on a quoted price of ``gross``, incl. GST on the fee."""
        variable = self.collected(gross).scale(self.gateway_rate, ROUND_HALF_UP)
        return (variable + self.gateway_fixed).scale(_ONE + self.fee_gst_rate, ROUND_CEILING)

    def output_gst(self, gross: Money) -> Money:
        """GST you must remit on a sale of ``gross``."""
        if self.gst_rate == _ZERO:
            return Money.zero()
        if self.gst_inclusive:
            return quantize_money(
                Decimal(gross.rupees) - Decimal(gross.rupees) * self.revenue_factor,
                ROUND_HALF_UP,
            )
        return gross.scale(self.gst_rate, ROUND_HALF_UP)

    def net_proceeds(self, gross: Money) -> Money:
        """What actually reaches you from a quoted price of ``gross``.

        ``revenue - gateway fee (incl. its GST)``. Revenue is derived from the
        *quoted* price, not the collected amount: under an exclusive price the
        customer pays gross + GST, but that GST was never the business's money.
        """
        revenue = quantize_money(
            Decimal(gross.rupees) * self.revenue_factor, ROUND_HALF_UP
        )
        return revenue - self.gross_fee(gross)


@dataclass(frozen=True, slots=True)
class OrderEconomics:
    """Complete cost/revenue picture for one customer order.

    Build via :meth:`for_service`; every field is a settled ``Money`` or an
    exact ``Decimal``, so downstream reports never inherit float drift.
    """

    service: ServiceCost
    delivery: DeliveryModel
    fees: FeeModel
    sticker_price: Money
    gross_price: Money
    #: Real rupees per rupee of wallet credit (< 1 when a bonus pack applies).
    wallet_multiplier: Decimal

    # -- costs -----------------------------------------------------------
    @property
    def expected_attempts(self) -> Decimal:
        return self.delivery.expected_attempts

    @property
    def expected_net_spend(self) -> Money:
        """Expected wallet spend per initiated order, in wallet credit."""
        return quantize_money(
            self.delivery.expected_net_spend_rate(self.sticker_price), ROUND_CEILING
        )

    @property
    def cogs(self) -> Money:
        """Expected **real money** cost per initiated order.

        This is the number to put in a P&L: it includes retry waste and is
        converted from wallet credit into rupees actually spent.
        """
        return self.expected_net_spend.scale(self.wallet_multiplier, ROUND_CEILING)

    @property
    def cost_per_successful_delivery(self) -> Money:
        """COGS spread over only the orders that succeed.

        Failed orders are refunded to the customer and earn nothing, so their
        cost has to be carried by the ones that worked. Pricing off
        ``cogs`` alone understates true unit cost by 1/P(success).
        """
        p = self.delivery.order_success_rate
        if p == _ZERO:
            raise EconomicsError("order can never succeed; no finite cost per delivery")
        return quantize_money(Decimal(self.cogs.rupees) / p, ROUND_CEILING)

    # -- revenue ---------------------------------------------------------
    @property
    def net_proceeds(self) -> Money:
        """Revenue landing with you on a delivered, undisputed order."""
        return self.fees.net_proceeds(self.gross_price)

    @property
    def gateway_cost(self) -> Money:
        return self.fees.gross_fee(self.gross_price)

    @property
    def gst_payable(self) -> Money:
        return self.fees.output_gst(self.gross_price)

    @property
    def chargeback_loss(self) -> Money:
        """Expected loss from disputes, per initiated order.

        On a chargeback you return the whole gross price but keep the consumed
        number, so the loss is the full gross, not just the margin.
        """
        if self.fees.chargeback_rate == _ZERO:
            return Money.zero()
        return quantize_money(
            Decimal(self.gross_price.rupees)
            * self.delivery.order_success_rate
            * self.fees.chargeback_rate,
            ROUND_CEILING,
        )

    # -- margin ----------------------------------------------------------
    @property
    def gross_margin(self) -> Money:
        """Net proceeds minus cost of a successful delivery."""
        return self.net_proceeds - self.cost_per_successful_delivery

    @property
    def gross_margin_ratio(self) -> Rate:
        if self.gross_price.is_zero:
            return rate(0)
        return rate(Decimal(self.gross_margin.paise) / Decimal(self.gross_price.paise))

    @property
    def expected_contribution(self) -> Money:
        """True expected profit per **initiated** order.

        This is the honest bottom line, and it is the field that catches the
        classic trap: a service can show a healthy ``gross_margin`` per
        successful delivery while losing money overall, because too many
        orders fail and get refunded.
        """
        p = self.delivery.order_success_rate
        delivered = self.net_proceeds - self.chargeback_loss
        expected_revenue = quantize_money(Decimal(delivered.rupees) * p, ROUND_HALF_UP)
        return expected_revenue - self.cogs

    @property
    def contribution_per_delivery(self) -> Money:
        """Expected profit restated per successful delivery."""
        p = self.delivery.order_success_rate
        if p == _ZERO:
            raise EconomicsError("order can never succeed")
        return quantize_money(Decimal(self.expected_contribution.rupees) / p, ROUND_HALF_UP)

    @property
    def is_profitable(self) -> bool:
        return self.expected_contribution.paise > 0

    @property
    def expected_revenue_per_order(self) -> Money:
        p = self.delivery.order_success_rate
        return quantize_money(
            Decimal((self.net_proceeds - self.chargeback_loss).rupees) * p, ROUND_HALF_UP
        )

    # -- break-even ------------------------------------------------------
    def break_even_gross(self) -> Money:
        """Cheapest gross price at which this order stops losing money.

        Solves ``net_proceeds(G) == cost_per_successful_delivery(G)`` in closed
        form. Both sides are linear in G once cost is held fixed, so the
        solution is exact rather than searched.
        """
        cogs = self.cost_per_successful_delivery
        keep = self.fees.keep_ratio
        if keep <= _ZERO:
            raise EconomicsError(
                f"gateway + GST consume everything: only {keep:.1%} of each rupee "
                "survives, so no price can ever break even. Lower the gateway "
                "rate, or this service cannot be sold."
            )
        gross_rupees = (Decimal(cogs.rupees) + self.fees.fixed_cost) / keep
        return quantize_money(gross_rupees, ROUND_CEILING)

    def break_even_success_rate(self) -> Decimal:
        """Per-attempt success rate at which profit hits zero, price held.

        Found by bisection: profit is monotonically increasing in the success
        rate, so there is a single crossing. Answers "how bad can delivery get
        before this service stops paying?".
        """
        base = self.delivery
        lo, hi = Decimal("0.001"), _ONE
        zero = Money.zero()
        if _contribution_at(base, self, lo) < zero and _contribution_at(base, self, hi) < zero:
            raise EconomicsError("unprofitable at every success rate; raise the price")

        def profit(p: Decimal) -> Money:
            return _contribution_at(base, self, p)

        if profit(lo) >= zero:
            return lo
        for _ in range(80):  # bisection to ~1e-24, far finer than we report
            mid = (lo + hi) / 2
            if profit(mid) < zero:
                lo = mid
            else:
                hi = mid
        return hi.quantize(Decimal(1).scaleb(-4), rounding=ROUND_HALF_UP)

    def required_gross_for_margin(self, target: Rate) -> Money:
        """Gross price needed to hit a target margin *ratio* on the sale.

        Solves ``A - (fixed + cost)/G == target`` for G, where ``A`` is the
        share of the gross that survives GST and gateway fees.
        """
        a = self.fees.keep_ratio
        slack = a - Decimal(target)
        if slack <= _ZERO:
            raise EconomicsError(
                f"target margin {Decimal(target):.1%} exceeds the maximum achievable "
                f"{a:.1%} after GST and gateway fees"
            )
        return quantize_money(
            (self.fees.fixed_cost + Decimal(self.cost_per_successful_delivery.rupees)) / slack,
            ROUND_CEILING,
        )

    # -- factory ---------------------------------------------------------
    @classmethod
    def for_service(
        cls,
        cost: ServiceCost,
        gross_price: Money,
        *,
        fees: Optional[FeeModel] = None,
        wallet_multiplier: Decimal = _ONE,
        retry_cap: int = 3,
        sticker_price: Optional[Money] = None,
        min_charge: Optional[Money] = None,
    ) -> "OrderEconomics":
        """Assemble the full picture for one service at one price."""
        sticker = sticker_price if sticker_price is not None else cost.list_price
        if min_charge is not None:
            sticker = max(sticker, min_charge, key=lambda m: m.paise)
        if gross_price.is_negative:
            raise ValueError("gross_price cannot be negative")
        return cls(
            service=cost,
            delivery=DeliveryModel.from_service(cost, retry_cap),
            fees=fees or FeeModel(),
            sticker_price=sticker,
            gross_price=gross_price,
            wallet_multiplier=Decimal(wallet_multiplier),
        )

    # -- reporting -------------------------------------------------------
    def summary(self) -> dict[str, object]:
        return {
            "service": self.service.slug,
            "sticker_price": self.sticker_price.to_plain(),
            "gross_price": self.gross_price.to_plain(),
            "expected_attempts": f"{self.expected_attempts:.4f}",
            "order_success_rate": f"{self.delivery.order_success_rate:.4f}",
            "cogs": self.cogs.to_plain(),
            "cost_per_delivery": self.cost_per_successful_delivery.to_plain(),
            "net_proceeds": self.net_proceeds.to_plain(),
            "gross_margin": self.gross_margin.to_plain(),
            "gross_margin_ratio": f"{self.gross_margin_ratio:.2%}",
            "expected_contribution": self.expected_contribution.to_plain(),
            "profitable": self.is_profitable,
        }


def _contribution_at(base: DeliveryModel, econ: OrderEconomics, success_rate: Decimal) -> Money:
    """Expected contribution with the success rate overridden (for bisection)."""
    burn = min(base.burn_rate, _ONE - success_rate)
    delivery = DeliveryModel(success_rate, burn, base.refund_share, base.retry_cap)
    replaced = OrderEconomics(
        service=econ.service,
        delivery=delivery,
        fees=econ.fees,
        sticker_price=econ.sticker_price,
        gross_price=econ.gross_price,
        wallet_multiplier=econ.wallet_multiplier,
    )
    return replaced.expected_contribution


@dataclass(frozen=True, slots=True)
class PricingAdvice:
    """A recommended price plus the reasoning behind it."""

    service: ServiceCost
    gross_price: Money
    break_even: Money
    econ: OrderEconomics
    reason: str

    @property
    def headroom(self) -> Money:
        """How far the price sits above break-even."""
        return self.gross_price - self.break_even

    @property
    def headroom_ratio(self) -> Rate:
        if self.break_even.is_zero:
            return rate(0)
        return rate(Decimal(self.headroom.paise) / Decimal(self.break_even.paise))
