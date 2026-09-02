"""Provider cost catalogue.

The numbers in ``src/uotpbot/data/uotp_prices.csv`` were read off uotp.in/services on
2026-09-01. That page is the *real* price list. The homepage's "from Rs.2"
figure and its "Telegram Rs.2 / WhatsApp Rs.5 / Google Rs.8" table do not
survive contact with the checkout -- see ``docs/RESEARCH.md``.

Everything below is therefore denominated in what a purchase actually costs,
floored at the provider minimum charge, never at the advertised teaser price.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

from .money import INR, Money

__all__ = [
    "ServiceCost",
    "Catalog",
    "CatalogError",
    "load_catalog",
    "default_catalog",
    "PROVIDER_MIN_CHARGE",
    "PROVIDER_VALIDITY_MINUTES",
    "PROVIDER_REFUND_WINDOW_MINUTES",
]

_ONE = Decimal(1)

#: Cheapest thing the provider will charge for one number, in INR. The old
#: uotp.in price list floored at Rs.10; the current uotp.store inventory
#: (dashboard live export, 2026-09-02) genuinely charges less for some --
#: e.g. JustDial at Rs.2.79. Keep the floor conservative so pricing never
#: assumes the "from Rs.2" marketing, which no live server matches anyway.
PROVIDER_MIN_CHARGE = INR(2)

#: Each number stays live this long and can receive multiple OTPs for the same
#: service inside the window.
PROVIDER_VALIDITY_MINUTES = 20

#: Provider refunds to wallet if no OTP arrives inside this window.
PROVIDER_REFUND_WINDOW_MINUTES = 5


class CatalogError(Exception):
    """Raised when the cost catalogue is missing a service or is malformed."""


@dataclass(frozen=True, slots=True)
class ServiceCost:
    """What one number for ``service`` actually costs the reseller.

    ``list_price`` is the sticker price the provider charges per number.
    ``otp_success_rate`` is the probability that a purchased number actually
    yields a usable OTP -- the provider does not guarantee delivery (uotp.in
    ToS 5: "We do not guarantee 100% success rate for OTP delivery"), so this
    is a real cost driver, not a rounding detail.
    """

    slug: str
    name: str
    category: str
    list_price: Money
    otp_success_rate: Decimal
    #: Probability a freshly allocated number is already registered / rejected
    #: by the target service ("burned"). Charged, usually not refundable.
    burn_rate: Decimal = Decimal("0.05")
    #: Fraction of a failed charge the provider actually credits back.
    refund_share: Decimal = Decimal("0.90")
    #: Provider SIM-bank server id that quotes this price (uotp.store model:
    #: price/stock live per server). Empty means "let the provider choose".
    server: str = ""

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("ServiceCost.slug must be non-empty")
        if self.list_price.is_negative:
            raise ValueError(f"list_price cannot be negative for {self.slug}")
        for field in ("otp_success_rate", "burn_rate", "refund_share"):
            value = getattr(self, field)
            if not (Decimal(0) <= value <= Decimal(1)):
                raise ValueError(f"{field} for {self.slug} must be in [0, 1], got {value}")
        if self.otp_success_rate + self.burn_rate > Decimal(1):
            raise ValueError(
                f"{self.slug}: otp_success_rate + burn_rate exceeds 1 "
                f"({self.otp_success_rate} + {self.burn_rate})"
            )

    def with_overrides(self, **changes: object) -> "ServiceCost":
        """Return a copy with fields replaced (frozen dataclass helper)."""
        return replace(self, **changes)  # type: ignore[arg-type]

    @property
    def display(self) -> str:
        return f"{self.name} ({self.list_price})"


@dataclass(frozen=True, slots=True)
class WalletPack:
    """A credit top-up offer.

    ``pay`` is what leaves your bank; ``credit`` is what lands in the wallet.
    When ``credit > pay`` the bonus is a direct discount on *every* downstream
    purchase, which materially changes break-even -- see
    :meth:`Catalog.effective_multiplier`.
    """

    label: str
    pay: Money
    credit: Money
    #: Payment-rail fee on the top-up itself (UPI 0, cards ~2%).
    rail_fee_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.pay.is_negative or self.credit.is_negative:
            raise ValueError("WalletPack amounts cannot be negative")
        if self.pay.is_zero:
            raise ValueError("WalletPack.pay cannot be zero")
        if not (Decimal(0) <= self.rail_fee_rate <= Decimal(1)):
            raise ValueError(f"rail_fee_rate must be in [0, 1], got {self.rail_fee_rate}")

    @property
    def bonus(self) -> Money:
        """Free credit granted by the pack."""
        return self.credit - self.pay

    @property
    def multiplier(self) -> Decimal:
        """Rupees of real money per rupee of spendable wallet credit.

        ``< 1`` means the pack is a discount: the Rs.1000 pack pays 1000 for
        1150 of credit, so every purchase costs 1000/1150 = 0.8696x its
        sticker price in real money.

        Computed in rupee-space, not money-space: a multiplier is a ratio, and
        ``Money`` deliberately refuses to be multiplied by one.
        """
        paid = Decimal(self.pay.rupees) * (_ONE + self.rail_fee_rate)
        credit = Decimal(self.credit.rupees)
        if credit == 0:
            raise ValueError(f"pack {self.label!r} grants no credit")
        return paid / credit


class Catalog:
    """Cost lookup table plus the wallet economics that sit on top of it."""

    def __init__(
        self,
        services: Mapping[str, ServiceCost],
        packs: Sequence[WalletPack] = (),
        *,
        min_charge: Money = PROVIDER_MIN_CHARGE,
        fallback_success_rate: Decimal = Decimal("0.90"),
    ) -> None:
        self._services: dict[str, ServiceCost] = {}
        for slug, cost in services.items():
            self._services[self._norm(slug)] = cost
        self._packs: list[WalletPack] = sorted(packs, key=lambda p: p.credit.paise)
        self.min_charge = min_charge
        self.fallback_success_rate = fallback_success_rate

    # -- loading ---------------------------------------------------------
    @classmethod
    def from_csv(cls, text: str, packs: Sequence[WalletPack] = (), **kw: object) -> "Catalog":
        """Parse ``slug,name,category,list_price_inr[,success_rate,burn_rate,refund_share]``."""
        services: dict[str, ServiceCost] = {}
        # Drop comment and blank lines first: the header row must be the real
        # header, not a leading "# slug,name,..." documentation line.
        body = "\n".join(
            line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
        reader = csv.DictReader(io.StringIO(body))
        required = {"slug", "name", "category", "list_price_inr"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            missing = required - set(reader.fieldnames or [])
            raise CatalogError(f"cost CSV is missing columns: {sorted(missing)}")
        for lineno, row in enumerate(reader, start=2):
            slug = (row.get("slug") or "").strip()
            if not slug or slug.startswith("#"):
                continue
            try:
                services[slug] = ServiceCost(
                    slug=slug,
                    name=(row.get("name") or slug).strip(),
                    category=(row.get("category") or "other").strip(),
                    list_price=INR((row["list_price_inr"] or "").strip()),
                    otp_success_rate=Decimal((row.get("success_rate") or "").strip() or "0.90"),
                    burn_rate=Decimal((row.get("burn_rate") or "").strip() or "0.05"),
                    refund_share=Decimal((row.get("refund_share") or "").strip() or "0.90"),
                    server=(row.get("server") or "").strip(),
                )
            except (ValueError, ArithmeticError) as exc:
                raise CatalogError(f"{slug}: line {lineno}: {exc}") from exc
        return cls(services, packs, **kw)  # type: ignore[arg-type]

    # -- lookup ----------------------------------------------------------
    @staticmethod
    def _norm(slug: str) -> str:
        return slug.strip().lower().replace(" ", "-").replace("/", "-")

    def get(self, slug: str) -> ServiceCost:
        key = self._norm(slug)
        try:
            return self._services[key]
        except KeyError:
            raise CatalogError(
                f"unknown service {slug!r}; known services: {', '.join(sorted(self.slugs()))}"
            ) from None

    def has(self, slug: str) -> bool:
        return self._norm(slug) in self._services

    def __contains__(self, slug: object) -> bool:
        return isinstance(slug, str) and self.has(slug)

    def slugs(self) -> Iterator[str]:
        return iter(sorted(self._services))

    def services(self) -> Iterator[ServiceCost]:
        return (self._services[s] for s in sorted(self._services))

    def __len__(self) -> int:
        return len(self._services)

    @property
    def packs(self) -> list[WalletPack]:
        return list(self._packs)

    def cheapest_price(self) -> Optional[Money]:
        """Lowest sticker price in the catalogue."""
        if not self._services:
            return None
        return min((s.list_price for s in self._services.values()), key=lambda m: m.paise)

    def effective_multiplier(self, pack: Optional[WalletPack] = None) -> Decimal:
        """Real-money cost per rupee of wallet credit.

        With no pack (or the smallest, bonus-free pack) this is ``1 + fee``.
        Choosing the biggest pack is a pure margin lever and this method makes
        that lever explicit rather than implicit.
        """
        if pack is None:
            pack = self.best_pack()
        if pack is None:
            return Decimal(1)
        return pack.multiplier

    def best_pack(self) -> Optional[WalletPack]:
        """The pack with the lowest real-money cost per credit rupee."""
        if not self._packs:
            return None
        return min(self._packs, key=lambda p: (p.multiplier, -p.pay.paise))

    def sticker_price(self, slug: str) -> Money:
        """Sticker price floored at the provider minimum charge."""
        cost = self.get(slug)
        return max(cost.list_price, self.min_charge, key=lambda m: m.paise)


#: The bundled price book lives INSIDE the package. A previous layout pointed
#: at ``<repo>/data`` via ``parent.parent.parent``, which only exists in a
#: source checkout -- the built wheel never carried it (the old
#: ``package-data`` rule referenced a path outside the package, which
#: setuptools silently drops, so every clean install crashed at startup with
#: "cost catalogue not found").
_DATA_DIR = Path(__file__).resolve().parent / "data"

#: uotp.in credit packs as advertised on the homepage.
UOTP_PACKS: tuple[WalletPack, ...] = (
    WalletPack("Starter", INR(50), INR(50), rail_fee_rate=Decimal("0")),
    WalletPack("Popular", INR(200), INR(220), rail_fee_rate=Decimal("0")),
    WalletPack("Pro", INR(1000), INR(1150), rail_fee_rate=Decimal("0")),
)

_DEFAULT_CACHE: Optional[Catalog] = None


def load_catalog(path: Optional[Path] = None, packs: Sequence[WalletPack] = UOTP_PACKS) -> Catalog:
    """Load the bundled cost CSV, or an override path."""
    target = Path(path) if path else _DATA_DIR / "uotp_prices.csv"
    if not target.exists():
        raise CatalogError(f"cost catalogue not found at {target}")
    return Catalog.from_csv(target.read_text(encoding="utf-8"), packs)


def default_catalog() -> Catalog:
    """Cached view of the bundled catalogue."""
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = load_catalog()
    return _DEFAULT_CACHE
