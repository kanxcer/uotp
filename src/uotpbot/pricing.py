"""Pricing: turning a break-even number into a price customers will pay.

Break-even tells you the floor. This module picks the actual shelf price,
respecting three constraints that a naive "cost x 1.5" ignores:

* it must clear break-even **plus** a safety buffer, because the success rate
  in the cost model is an estimate and real delivery is worse on bad days;
* it must land on a clean price point -- Rs.14.37 converts worse than Rs.15
  and earns less;
* it must not undercut the ladder below the point where the gateway's fixed
  fee eats the margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import Enum
from typing import Iterable, Optional, Sequence

from .catalog import Catalog, ServiceCost
from .economics import FeeModel, OrderEconomics, PricingAdvice
from .money import INR, PAISE_PER_RUPEE, Money, Rate, quantize_money, rate

__all__ = ["Strategy", "PriceLadder", "Pricer", "PRICE_LADDER", "DEFAULT_LADDER"]

_ONE = Decimal(1)

#: Indian consumer price points. Empirically these convert far better than
#: arbitrary figures, and each rung is a natural anchor. The ladder starts at
#: Rs.15 because the provider floor is Rs.10 -- there is no room below that.
DEFAULT_LADDER: tuple[Money, ...] = (
    INR(15), INR(18), INR(20), INR(22), INR(25), INR(28), INR(30),
    INR(35), INR(39), INR(40), INR(45), INR(49), INR(55), INR(59),
    INR(69), INR(79), INR(89), INR(99), INR(119), INR(129), INR(149),
    INR(179), INR(199), INR(249), INR(299), INR(399), INR(499),
)


class Strategy(str, Enum):
    """How the target price is derived before it is snapped to the ladder."""

    TARGET_MARGIN = "target_margin"
    """Price so that margin/gross equals a target ratio (e.g. 35%)."""

    MARKUP_ON_COST = "markup_on_cost"
    """Price = cost x (1 + markup). Simple, but ignores fees."""

    FIXED_SPREAD = "fixed_spread"
    """Price = cost + a fixed rupee amount. Flat absolute profit."""

    PEER_MULTIPLE = "peer_multiple"
    """Price = provider sticker x a multiple. Rough but predictable."""

    EXACT_MARKUP = "exact_markup"
    """Price = the service's actual per-number cost x (1 + markup), exactly.

    This is the owner's 45% rule: a Rs.10 service sells at Rs.14.50, a Rs.20
    server at Rs.29.00. No break-even floor and no snapping to a round rung --
    the price IS the exact figure. The base is ``list_price``, the provider's
    per-number charge for that service / server (NOT the amortized
    ``cost_per_successful_delivery``, which a high retry rate inflates). So the
    price* scale is bounded by the stated base, and a per-server quote uses
    that server's own ``list_price``.
    """


@dataclass(frozen=True, slots=True)
class PriceLadder:
    """An ordered set of allowed shelf prices."""

    rungs: tuple[Money, ...]

    def __post_init__(self) -> None:
        if not self.rungs:
            raise ValueError("PriceLadder needs at least one rung")
        if any(r.is_negative for r in self.rungs):
            raise ValueError("price rungs cannot be negative")
        if any(a.paise >= b.paise for a, b in zip(self.rungs, self.rungs[1:])):
            raise ValueError("price rungs must be strictly increasing")

    def snap_up(self, amount: Money) -> Money:
        """Smallest rung at or above ``amount``.

        Always rounds *up*: rounding a price down to a rung can push it under
        break-even, and losing money to hit a round number is a bad trade.
        """
        for rung in self.rungs:
            if rung.paise >= amount.paise:
                return rung
        # Above the ladder, round up to the next whole hundred rather than
        # refusing: a premium service still needs a price, and a round
        # hundred is the natural anchor at that level.
        step = 100 * PAISE_PER_RUPEE
        hundreds = -(-amount.paise // step)  # ceiling division
        return Money(hundreds * step)

    def snap_down(self, amount: Money) -> Money:
        """Largest rung at or below ``amount`` (for discount display)."""
        below = [r for r in self.rungs if r.paise <= amount.paise]
        return below[-1] if below else self.rungs[0]

    def nearest(self, amount: Money) -> Money:
        """Closest rung, ties going up."""
        best = min(self.rungs, key=lambda r: (abs(r.paise - amount.paise), -r.paise))
        return best

    def __contains__(self, amount: object) -> bool:
        return isinstance(amount, Money) and any(r.paise == amount.paise for r in self.rungs)


PRICE_LADDER = PriceLadder(DEFAULT_LADDER)


class Pricer:
    """Produces :class:`PricingAdvice` for every service in a catalogue."""

    def __init__(
        self,
        catalog: Catalog,
        *,
        fees: Optional[FeeModel] = None,
        ladder: PriceLadder = PRICE_LADDER,
        strategy: Strategy = Strategy.TARGET_MARGIN,
        target_margin: Rate = rate("0.35"),
        markup: Rate = rate("0.60"),
        fixed_spread: Money = INR(10),
        peer_multiple: Rate = rate("1.8"),
        retry_cap: int = 3,
        #: Extra buffer above break-even, as a fraction of break-even. Guards
        #: against the success rate being optimistic -- which it usually is.
        safety_buffer: Rate = rate("0.10"),
    ) -> None:
        self.catalog = catalog
        self.fees = fees or FeeModel()
        self.ladder = ladder
        self.strategy = strategy
        self.target_margin = target_margin
        self.markup = markup
        self.fixed_spread = fixed_spread
        self.peer_multiple = peer_multiple
        self.retry_cap = retry_cap
        self.safety_buffer = safety_buffer
        self._multiplier = catalog.effective_multiplier(catalog.best_pack())

    # -- core ------------------------------------------------------------
    def raw_target(self, cost: ServiceCost, sticker: Money) -> Money:
        """Un-snapped price from the configured strategy."""
        if self.strategy is Strategy.TARGET_MARGIN:
            probe = OrderEconomics.for_service(
                cost, Money(0), fees=self.fees, wallet_multiplier=self._multiplier,
                retry_cap=self.retry_cap, sticker_price=sticker,
            )
            return probe.required_gross_for_margin(self.target_margin)
        if self.strategy is Strategy.MARKUP_ON_COST:
            probe = OrderEconomics.for_service(
                cost, Money(0), fees=self.fees, wallet_multiplier=self._multiplier,
                retry_cap=self.retry_cap, sticker_price=sticker,
            )
            base = Decimal(probe.cost_per_successful_delivery.rupees)
            return quantize_money(base * (_ONE + self.markup), ROUND_CEILING)
        if self.strategy is Strategy.FIXED_SPREAD:
            probe = OrderEconomics.for_service(
                cost, Money(0), fees=self.fees, wallet_multiplier=self._multiplier,
                retry_cap=self.retry_cap, sticker_price=sticker,
            )
            return probe.cost_per_successful_delivery + self.fixed_spread
        if self.strategy is Strategy.PEER_MULTIPLE:
            return quantize_money(Decimal(sticker.rupees) * self.peer_multiple, ROUND_CEILING)
        if self.strategy is Strategy.EXACT_MARKUP:
            # Base = the service's actual per-number charge (list_price), NOT
            # the amortized cost_per_successful_delivery which a high retry
            # rate inflates. Exact figure, no ladding/snapping.
            return quantize_money(
                Decimal(cost.list_price.rupees) * (_ONE + self.markup), ROUND_CEILING
            )
        raise ValueError(f"unknown strategy {self.strategy!r}")

    def price(self, cost: ServiceCost, *, sticker: Optional[Money] = None) -> PricingAdvice:
        """Recommend a shelf price for one service.

        ``sticker`` overrides the default cost anchor (the catalogue minimum):
        when a customer hand-picks a dearer SIM-bank server, the quote must
        clear THAT server's price, not the cheapest one -- otherwise a tapped
        ₹16.80 server priced off a ₹10.00 anchor sells at a loss.
        """
        if sticker is None:
            sticker = self.catalog.sticker_price(cost.slug)
        probe = OrderEconomics.for_service(
            cost, Money(0), fees=self.fees, wallet_multiplier=self._multiplier,
            retry_cap=self.retry_cap, sticker_price=sticker,
        )
        break_even = probe.break_even_gross()

        # The floor is break-even plus buffer, and never below the ladder.
        buffered = quantize_money(
            Decimal(break_even.rupees) * (_ONE + self.safety_buffer), ROUND_CEILING
        )
        ladder_floor = self.ladder.rungs[0]
        target = self.raw_target(cost, sticker)

        if self.strategy is Strategy.EXACT_MARKUP:
            # Owner's exact markup rule: the price IS cost x (1+markup), to the
            # paise, on every service and server. No break-even floor and no
            # snapping to a round rung -- the owner decides the margin, not the
            # ladder. (break_even/econ are still computed for reporting.)
            gross = target
            reason = (
                f"strategy={self.strategy.value}: {cost.list_price} x "
                f"{_ONE + self.markup} = {gross}"
            )
            econ = OrderEconomics.for_service(
                cost, gross, fees=self.fees, wallet_multiplier=self._multiplier,
                retry_cap=self.retry_cap, sticker_price=sticker,
            )
            return PricingAdvice(
                service=cost, gross_price=gross, break_even=break_even,
                econ=econ, reason=reason,
            )

        floor = max(buffered, ladder_floor, key=lambda m: m.paise)
        chosen_raw = max(target, floor, key=lambda m: m.paise)
        gross = self.ladder.snap_up(chosen_raw)

        # Name the constraint that actually bound, because a price book that
        # misreports why a price is what it is gets edited wrongly later.
        if gross.paise <= break_even.paise:
            reason = (
                f"break-even is {break_even}; even {gross} loses money -- "
                "raise the price tier or drop this service"
            )
        elif gross.paise == ladder_floor.paise and ladder_floor.paise >= target.paise:
            reason = (
                f"ladder minimum {ladder_floor} binds (break-even {break_even}, "
                f"strategy target {target})"
            )
        elif buffered.paise > target.paise and buffered.paise >= ladder_floor.paise:
            reason = (
                f"break-even {break_even} + {self.safety_buffer:.0%} buffer = {buffered} "
                f"binds, above strategy target {target}"
            )
        else:
            reason = f"strategy={self.strategy.value} gave {target}, snapped up to {gross}"

        econ = OrderEconomics.for_service(
            cost, gross, fees=self.fees, wallet_multiplier=self._multiplier,
            retry_cap=self.retry_cap, sticker_price=sticker,
        )
        return PricingAdvice(
            service=cost, gross_price=gross, break_even=break_even, econ=econ, reason=reason
        )

    def price_book(self, slugs: Optional[Iterable[str]] = None) -> list[PricingAdvice]:
        """Price every service (or a subset), cheapest sticker first."""
        items = (
            [self.catalog.get(s) for s in slugs]
            if slugs is not None
            else list(self.catalog.services())
        )
        return sorted(
            (self.price(c) for c in items), key=lambda a: a.service.list_price.paise
        )

    # -- diagnostics -----------------------------------------------------
    def is_unviable(self, advice: PricingAdvice) -> bool:
        """True when a service cannot be sold at a sane price.

        ``price()`` always floors the shelf price at break-even plus buffer,
        so a returned price is *arithmetically* profitable by construction --
        which makes "is it profitable?" useless as a filter. The question that
        matters is whether break-even sits inside the ladder at all. A service
        whose break-even is above the top rung would have to be sold at a
        price no customer will pay, which is the same thing as being
        unsellable.
        """
        if not advice.econ.is_profitable:
            return True
        return advice.break_even.paise > self.ladder.rungs[-1].paise

    def loss_makers(self, price_book: Optional[Sequence[PricingAdvice]] = None) -> list[PricingAdvice]:
        """Services that cannot be priced inside the ladder. Do not sell these."""
        book = list(price_book if price_book is not None else self.price_book())
        return [a for a in book if self.is_unviable(a)]

    def portfolio_summary(self, book: Optional[Sequence[PricingAdvice]] = None) -> dict[str, object]:
        book = list(book if book is not None else self.price_book())
        if not book:
            return {"services": 0}
        viable = [a for a in book if not self.is_unviable(a)]
        contributions = [a.econ.expected_contribution.paise for a in book]
        avg_margin = (
            sum((a.econ.gross_margin_ratio for a in viable), rate(0)) / len(viable)
            if viable
            else rate(0)
        )
        return {
            "services": len(book),
            "profitable": len(viable),
            "loss_making": len(book) - len(viable),
            "avg_margin_ratio": f"{avg_margin:.2%}",
            "min_expected_contribution": Money(min(contributions)).to_plain(),
            "max_expected_contribution": Money(max(contributions)).to_plain(),
            "mean_expected_contribution": Money(sum(contributions) // len(contributions)).to_plain(),
            "wallet_multiplier": f"{self._multiplier:.6f}",
            "best_pack": self.catalog.best_pack().label if self.catalog.best_pack() else None,
        }
