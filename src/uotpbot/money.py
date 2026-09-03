"""Exact money arithmetic.

Every rupee value in this codebase is stored as an ``int`` count of **paise**
(1 INR = 100 paise). Binary floats are never used to hold currency: 0.1 + 0.2
!= 0.3 in IEEE-754, and a bot that rounds a float at the wrong moment silently
loses or invents money.

Two distinct kinds of quantity live here and are deliberately kept apart:

``Money``
    A *settled* amount -- something that was actually charged, credited or
    paid. Backed by ``int`` paise. Exact. Comparisons are exact.

``Rate``  (plain ``Decimal``)
    A *ratio* -- a success probability, a markup, a fee percentage. Ratios are
    not money, they are multiplied by money, so they carry extra precision
    (``RATE_PRECISION``) and are only quantised to paise at the moment they
    become a real charge.

The rule this module enforces: **quantise once, at the boundary**, using an
explicit rounding mode. Never ``round()`` a float, never let a Decimal drift
into a Money without naming how it rounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Union

__all__ = [
    "PAISE_PER_RUPEE",
    "Money",
    "INR",
    "Rate",
    "rate",
    "quantize_money",
    "split_amount",
    "ROUND_HALF_UP",
    "ROUND_DOWN",
    "ROUND_CEILING",
    "ROUND_FLOOR",
]

PAISE_PER_RUPEE = 100
"""Paise in one rupee. Integer, so rupee<->paise conversion is exact."""

#: Precision used for ratios and intermediate expected-value maths. 12 places
#: is far below one paise on any realistic amount, so rounding error cannot
#: accumulate into a visible rupee.
RATE_PRECISION = 12


class Rate(Decimal):
    """A dimensionless ratio (probability, markup, fee fraction).

    Subclassing ``Decimal`` is only for documentation and type-checking value:
    a function signature saying ``fee_rate: Rate`` cannot accidentally be
    passed a :class:`Money`.
    """

    __slots__ = ()


def rate(value: Union[int, str, Decimal, float]) -> Rate:
    """Build a :class:`Rate`.

    ``float`` input is accepted for ergonomics (``rate(0.02)``) but is routed
    through ``str`` first so that ``0.02`` becomes exactly ``0.02`` rather than
    the 0.0200000000000000004163... a float constructor would produce.
    """
    if isinstance(value, float):
        return Rate(str(value))
    return Rate(value)


def _to_paise(value: Union[int, str, Decimal, "Money"]) -> int:
    """Coerce a rupee-denominated value to an exact integer paise count."""
    if isinstance(value, Money):
        return value.paise
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise TypeError("bool is not a valid money amount")
    if isinstance(value, int):
        return value * PAISE_PER_RUPEE
    if isinstance(value, float):
        # Route through repr() so 12.5 -> "12.5" exactly, not a binary artefact.
        value = Decimal(repr(value))
    if isinstance(value, str):
        value = Decimal(value.strip().replace(",", "").lstrip("₹"))
    if not isinstance(value, Decimal):
        raise TypeError(f"cannot interpret {value!r} as money")
    if not value.is_finite():
        raise ValueError(f"money must be finite, got {value!r}")
    paise = value * PAISE_PER_RUPEE
    if paise != paise.to_integral_value():
        # Sub-paise input is a caller bug: silently truncating here is how
        # ledgers drift. Force the caller to choose a rounding mode.
        raise ValueError(
            f"{value!r} INR is not a whole number of paise; "
            "quantise explicitly before constructing Money"
        )
    return int(paise)


def quantize_money(
    value: Union[Decimal, "Rate", Money],
    rounding: str = ROUND_HALF_UP,
) -> "Money":
    """Turn a sub-paise precise amount into a settled :class:`Money`.

    This is the *only* sanctioned way to cross from ratio-space into
    money-space. The rounding mode must be named at the call site so that the
    direction of rounding is a visible business decision, not an accident:

    * costs  -> ``ROUND_CEILING`` (never under-provision your cost)
    * prices -> ``ROUND_HALF_UP`` or a price ladder
    * refunds/credits to customers -> ``ROUND_DOWN`` (never over-refund)
    """
    if isinstance(value, Money):
        return value
    paise = Decimal(value) * PAISE_PER_RUPEE
    return Money(int(paise.quantize(Decimal(1), rounding=rounding)))


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """An exact amount of Indian rupees, stored as integer paise."""

    paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.paise, int) or isinstance(self.paise, bool):
            raise TypeError(f"Money.paise must be int, got {type(self.paise).__name__}")

    # -- construction ----------------------------------------------------
    @classmethod
    def zero(cls) -> "Money":
        return cls(0)

    @classmethod
    def from_paise(cls, paise: int) -> "Money":
        return cls(int(paise))

    @classmethod
    def from_rupees(cls, rupees: Union[int, str, Decimal]) -> "Money":
        """Build from a rupee amount, which may carry paise (``"12.50"``)."""
        return cls(_to_paise(rupees))

    # -- views -----------------------------------------------------------
    @property
    def rupees(self) -> Decimal:
        """Exact rupee value as a ``Decimal`` with 2 decimal places."""
        return (Decimal(self.paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))

    @property
    def is_negative(self) -> bool:
        return self.paise < 0

    @property
    def is_zero(self) -> bool:
        return self.paise == 0

    # -- arithmetic ------------------------------------------------------
    def __add__(self, other: "Money") -> "Money":
        _check_money(other, "+")
        return Money(self.paise + other.paise)

    def __sub__(self, other: "Money") -> "Money":
        _check_money(other, "-")
        return Money(self.paise - other.paise)

    def __neg__(self) -> "Money":
        return Money(-self.paise)

    def if_zero(self, fallback: "Money") -> "Money":
        """``fallback`` when this is zero, else ``self``.

        Deliberately not the ``x or y`` idiom: ``Money`` is a dataclass, so a
        ``Money(0)`` instance is *truthy* even though its value is zero, and
        ``Money(0) or sticker`` silently yields ``Money(0)`` instead of the
        fallback. This is how a number the provider did not quote a price for
        (``alloc.charged`` stays ``Money(0)`` on a live ``ACCESS_NUMBER`` that
        carries no price) ends up posted to the ledger as a zero amount -- which
        then trips the ``refusing to record a zero posting`` guard and aborts a
        successfully-purchased activation. Use this so the caller's intent is
        explicit and safe at zero.
        """
        if self.paise == 0:
            return fallback
        return self

    def __mul__(self, factor: Union[int, Decimal, Rate]) -> "Money":
        """Multiply by an exact integer, or by a ratio with explicit rounding.

        Multiplying by a ratio necessarily leaves money-space, so it returns a
        ``Decimal`` of rupees and forces the caller through
        :func:`quantize_money`. That friction is the point: it is impossible to
        accidentally produce a half-paise Money.
        """
        if isinstance(factor, int) and not isinstance(factor, bool):
            return Money(self.paise * factor)
        raise TypeError(
            "Money * ratio leaves money-space; use (money * rate) and pass the "
            "result to quantize_money() with an explicit rounding mode, or use "
            "Money.scale()"
        )

    __rmul__ = __mul__

    def scale(self, factor: Union[Decimal, Rate, int], rounding: str = ROUND_HALF_UP) -> "Money":
        """Multiply by a ratio and settle to paise, rounding explicitly."""
        if isinstance(factor, int) and not isinstance(factor, bool):
            return Money(self.paise * factor)
        with localcontext() as ctx:
            ctx.prec = 40
            paise = Decimal(self.paise) * Decimal(factor)
        return Money(int(paise.quantize(Decimal(1), rounding=rounding)))

    def __truediv__(self, other: Union[int, Decimal, Rate, "Money"]) -> Union[Rate, Decimal]:
        """``Money / int`` -> rupees ``Decimal``; ``Money / Money`` -> ``Rate``.

        ``Money / int`` returns a Decimal rather than a Money because dividing
        ₹10 three ways is not three whole Money values.
        """
        if isinstance(other, Money):
            if other.paise == 0:
                raise ZeroDivisionError("cannot divide by zero Money")
            return Rate(Decimal(self.paise) / Decimal(other.paise))
        if isinstance(other, int) and not isinstance(other, bool):
            if other == 0:
                raise ZeroDivisionError("division by zero")
            with localcontext() as ctx:
                ctx.prec = 40
                return (Decimal(self.paise) / Decimal(other * PAISE_PER_RUPEE)).quantize(
                    Decimal(1).scaleb(-RATE_PRECISION)
                )
        if isinstance(other, (Decimal, Rate)):
            if other == 0:
                raise ZeroDivisionError("division by zero")
            return quantize_money(Decimal(self.paise) / Decimal(other)).rupees
        raise TypeError(f"cannot divide Money by {type(other).__name__}")

    def __floordiv__(self, other: Union[int, "Money"]) -> Union[int, "Money"]:
        if isinstance(other, Money):
            if other.paise == 0:
                raise ZeroDivisionError("division by zero")
            return self.paise // other.paise
        if isinstance(other, int) and not isinstance(other, bool):
            if other == 0:
                raise ZeroDivisionError("division by zero")
            return Money(self.paise // other)
        raise TypeError(f"cannot floor-divide Money by {type(other).__name__}")

    def __mod__(self, other: Union[int, "Money"]) -> "Money":
        if isinstance(other, Money):
            return Money(self.paise % other.paise)
        if isinstance(other, int) and not isinstance(other, bool):
            return Money(self.paise % (other * PAISE_PER_RUPEE))
        raise TypeError(f"cannot take Money modulo {type(other).__name__}")

    # -- formatting ------------------------------------------------------
    def __str__(self) -> str:
        sign = "-" if self.paise < 0 else ""
        rupees = abs(self.paise) // PAISE_PER_RUPEE
        paise = abs(self.paise) % PAISE_PER_RUPEE
        # Indian digit grouping: last three, then pairs (1,00,000.00).
        head = f"{rupees:,}"
        if rupees >= 1000:
            s = str(rupees)
            last3, rest = s[-3:], s[:-3]
            grouped = ""
            while len(rest) > 2:
                grouped = "," + rest[-2:] + grouped
                rest = rest[:-2]
            head = rest + grouped + "," + last3
        return f"{sign}\u20b9{head}.{paise:02d}"

    def __repr__(self) -> str:
        return f"Money({self.paise})  # {self}"

    def to_plain(self) -> str:
        """Unambiguous, locale-independent form for logs, CSV and APIs."""
        return f"{self.rupees:.2f}"


def INR(rupees: Union[int, str, Decimal, Money]) -> Money:
    """Concise constructor: ``INR("12.50")`` -> ``Money(1250)``."""
    if isinstance(rupees, Money):
        return rupees
    return Money.from_rupees(rupees)


def _check_money(other: object, op: str) -> None:
    if not isinstance(other, Money):
        raise TypeError(
            f"unsupported operand type for {op}: {type(other).__name__}. "
            "Money only combines with Money -- convert ratios via scale()."
        )


def split_amount(total: Money, weights: list[Union[int, Decimal, Rate]]) -> list[Money]:
    """Split ``total`` across ``weights`` so the parts sum *exactly* to total.

    Naive proportional splitting loses or gains paise to rounding, which makes
    a ledger fail to balance. This allocates the rounded parts, then hands the
    leftover paise to the largest-weight shares (largest remainder method).

    Used for allocating a gateway fee across line items, splitting a payout,
    or dividing a bundle price across the numbers in it.
    """
    if not weights:
        if total.is_zero:
            return []
        raise ValueError("cannot split a non-zero amount across zero weights")

    w = [Decimal(x) for x in weights]
    denom = sum(w)
    if denom == 0:
        raise ValueError("weights must sum to a non-zero value")

    exact = [Decimal(total.paise) * wi / denom for wi in w]
    floors = [int(e.to_integral_value(rounding=ROUND_DOWN)) for e in exact]
    # Handle negative totals symmetrically so the invariant always holds.
    remainder = total.paise - sum(floors)
    if remainder:
        order = sorted(
            range(len(exact)),
            key=lambda i: (exact[i] - floors[i]),
            reverse=(remainder > 0),
        )
        step = 1 if remainder > 0 else -1
        for i in range(abs(remainder)):
            floors[order[i % len(order)]] += step
    parts = [Money(p) for p in floors]
    assert sum(parts, Money.zero()) == total, "split_amount lost or gained paise"
    return parts
