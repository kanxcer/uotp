"""Cached, grouped catalogue + INR quoting for social-boost services.

v1 sells only PerfectPanel ``Default`` type (a public link + a quantity).
Custom comments, subscriptions, mentions and web-traffic extras need more
fields than a Telegram flow can collect safely, so they stay off the shelf.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional, Sequence

from ..money import ROUND_CEILING, Money, quantize_money
from ..reseller import clone_price
from .provider import SmmError, SmmProvider

log = logging.getLogger("uotpbot.smm.catalog")

__all__ = [
    "SmmService",
    "SmmCatalog",
    "detect_platform",
    "platform_label",
    "cat_id",
    "PLATFORMS",
]


#: (slug, button label, match needles). Order is specificity: Instagram
#: before a generic "gram". Twitter needles include "x (twitter)" so a
#: bare letter X never hijacks "Max" / "Next".
PLATFORMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("instagram", "📸 Instagram", ("instagram",)),
    ("tiktok", "🎵 TikTok", ("tiktok", "tik tok")),
    ("youtube", "▶️ YouTube", ("youtube",)),
    ("telegram", "✈️ Telegram", ("telegram",)),
    ("facebook", "👥 Facebook", ("facebook",)),
    ("twitter", "🐦 Twitter / X", (
        "twitter", "x (twitter)", "(twitter)", "x twitter", "❖ x ",
    )),
    ("spotify", "🎧 Spotify", ("spotify",)),
    ("snapchat", "👻 Snapchat", ("snapchat",)),
    ("twitch", "🎮 Twitch", ("twitch",)),
    ("linkedin", "💼 LinkedIn", ("linkedin",)),
    ("threads", "🧵 Threads", ("threads",)),
    ("discord", "🎮 Discord", ("discord",)),
    ("whatsapp", "🟢 WhatsApp", ("whatsapp",)),
    ("pinterest", "📌 Pinterest", ("pinterest",)),
    ("reddit", "🤖 Reddit", ("reddit",)),
    ("website", "🌐 Website", ("website traffic", "web traffic", "website ")),
    ("google", "🔍 Google", ("google map", "google maps", "google ")),
)

_PLATFORM_LABEL = {slug: label for slug, label, _needles in PLATFORMS}
_PLATFORM_LABEL["other"] = "📦 Other"


def platform_label(slug: str) -> str:
    return _PLATFORM_LABEL.get(slug, slug)


def detect_platform(name: str, category: str = "") -> str:
    """Best-effort platform for a panel row. Never raises."""
    hay = f"{name} {category}".lower()
    for slug, _label, needles in PLATFORMS:
        if any(n in hay for n in needles):
            return slug
    return "other"


def cat_id(category: str) -> str:
    """Stable 8-char id that fits in a Telegram callback."""
    return hashlib.sha1((category or "").encode("utf-8")).hexdigest()[:8]


def _as_decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, ValueError, ArithmeticError):
        return Decimal(default)


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class SmmService:
    """One Default-type service the shop will sell."""

    service_id: str
    name: str
    category: str
    rate_usd: Decimal
    min_qty: int
    max_qty: int
    refill: bool = False
    cancel: bool = False
    platform: str = "other"
    type: str = "Default"

    def cost_usd(self, quantity: int) -> Decimal:
        """Upstream charge in USD for ``quantity`` units (rate is per 1000)."""
        if quantity <= 0:
            return Decimal("0")
        return (self.rate_usd * Decimal(quantity)) / Decimal(1000)

    @property
    def cat_key(self) -> str:
        return cat_id(self.category)


def parse_service(row: object) -> Optional[SmmService]:
    """Interpret one panel ``services`` row. None = skip (wrong type / junk)."""
    if not isinstance(row, dict):
        return None
    kind = str(row.get("type") or "Default").strip()
    if kind.lower() != "default":
        return None
    sid = str(row.get("service") or "").strip()
    if not sid:
        return None
    name = str(row.get("name") or "").strip() or f"Service {sid}"
    category = str(row.get("category") or "Other").strip() or "Other"
    rate = _as_decimal(row.get("rate"))
    if rate <= 0:
        return None
    min_qty = max(1, _as_int(row.get("min"), 1))
    max_qty = max(min_qty, _as_int(row.get("max"), min_qty))
    return SmmService(
        service_id=sid,
        name=name,
        category=category,
        rate_usd=rate,
        min_qty=min_qty,
        max_qty=max_qty,
        refill=_as_bool(row.get("refill")),
        cancel=_as_bool(row.get("cancel")),
        platform=detect_platform(name, category),
        type=kind,
    )


@dataclass
class _Snap:
    by_id: dict[str, SmmService] = field(default_factory=dict)
    platforms: list[tuple[str, str, int]] = field(default_factory=list)
    plat_cats: dict[str, list[str]] = field(default_factory=dict)
    cat_name: dict[str, str] = field(default_factory=dict)
    cat_platform: dict[str, str] = field(default_factory=dict)
    cat_services: dict[str, list[SmmService]] = field(default_factory=dict)
    fetched_at: float = 0.0


class SmmCatalog:
    """Thread-safe snapshot of the sellable Default-type catalogue."""

    def __init__(
        self,
        provider: SmmProvider,
        *,
        usd_inr: Decimal = Decimal("95"),
        markup: Decimal = Decimal("0.45"),
        min_charge: Money = Money(100),
        cache_seconds: float = 1800.0,
        services: Optional[Sequence[SmmService]] = None,
    ) -> None:
        if usd_inr <= 0:
            raise SmmError("usd_inr must be positive")
        if markup < 0:
            raise SmmError("markup cannot be negative")
        self.provider = provider
        self.usd_inr = Decimal(usd_inr)
        self.markup = Decimal(markup)
        self.min_charge = min_charge
        self.cache_seconds = float(cache_seconds)
        self._lock = threading.RLock()
        self._snap = _Snap()
        if services:
            self._install(list(services))

    @property
    def ready(self) -> bool:
        return bool(self._snap.by_id)

    def stale(self) -> bool:
        if not self.ready:
            return True
        if self.cache_seconds <= 0:
            return False
        return (time.time() - self._snap.fetched_at) > self.cache_seconds

    def refresh(self) -> int:
        """Pull the live list. Returns how many Default-type services we kept."""
        raw = list(self.provider.list_services())
        parsed = [s for s in (parse_service(r) for r in raw) if s is not None]
        self._install(parsed)
        log.info("smm catalogue: %d sellable of %d upstream", len(parsed), len(raw))
        return len(parsed)

    def ensure(self) -> None:
        """Refresh if empty or past TTL. Swallows provider errors on a warm cache."""
        if not self.stale():
            return
        try:
            self.refresh()
        except SmmError:
            if self.ready:
                log.warning("smm catalogue refresh failed; keeping previous snapshot",
                            exc_info=True)
                return
            raise

    def get(self, service_id: str) -> Optional[SmmService]:
        return self._snap.by_id.get(str(service_id))

    def platforms(self) -> list[tuple[str, str, int]]:
        return list(self._snap.platforms)

    def categories(self, platform: str) -> list[tuple[str, str, int]]:
        """``(cat_id, name, count)`` for one platform."""
        out: list[tuple[str, str, int]] = []
        for cid in self._snap.plat_cats.get(platform, ()):
            name = self._snap.cat_name.get(cid, cid)
            n = len(self._snap.cat_services.get(cid, ()))
            out.append((cid, name, n))
        return out

    def services_in(self, cat_key: str) -> list[SmmService]:
        return list(self._snap.cat_services.get(cat_key, ()))

    def cat_name(self, cat_key: str) -> str:
        return self._snap.cat_name.get(cat_key, "")

    def cat_platform(self, cat_key: str) -> str:
        return self._snap.cat_platform.get(cat_key, "other")

    def quote(
        self, svc: SmmService, quantity: int, *, extra_rate: Decimal = Decimal("0"),
    ) -> Money:
        """Customer INR price for ``quantity``. Re-quoted at confirm time."""
        if quantity < svc.min_qty or quantity > svc.max_qty:
            raise SmmError(
                f"quantity {quantity} is outside {svc.min_qty}–{svc.max_qty}"
            )
        usd = svc.cost_usd(quantity)
        cost = quantize_money(usd * self.usd_inr, ROUND_CEILING)
        sell = cost.scale(Decimal(1) + self.markup, ROUND_CEILING)
        if sell.paise < self.min_charge.paise:
            sell = self.min_charge
        extra = Decimal(extra_rate or 0)
        if extra > 0:
            sell = clone_price(sell, extra)
        return sell

    def cost_inr(self, svc: SmmService, quantity: int) -> Money:
        """Our expected INR cost (USD × FX), ceiling so we never under-provision."""
        return quantize_money(svc.cost_usd(quantity) * self.usd_inr, ROUND_CEILING)

    def _install(self, services: Iterable[SmmService]) -> None:
        by_id: dict[str, SmmService] = {}
        cat_services: dict[str, list[SmmService]] = {}
        cat_name: dict[str, str] = {}
        cat_platform: dict[str, str] = {}
        plat_cats: dict[str, list[str]] = {}
        plat_count: dict[str, int] = {}
        for svc in services:
            by_id[svc.service_id] = svc
            cat_services.setdefault(svc.cat_key, []).append(svc)
            cat_name[svc.cat_key] = svc.category
            cat_platform[svc.cat_key] = svc.platform
            plat_count[svc.platform] = plat_count.get(svc.platform, 0) + 1
        for cid, rows in cat_services.items():
            rows.sort(key=lambda s: (s.rate_usd, s.name.lower(), s.service_id))
            plat = cat_platform[cid]
            plat_cats.setdefault(plat, []).append(cid)
        for plat, cids in plat_cats.items():
            cids.sort(key=lambda c: cat_name.get(c, "").lower())
        platforms: list[tuple[str, str, int]] = []
        for slug, label, _n in PLATFORMS:
            n = plat_count.get(slug, 0)
            if n:
                platforms.append((slug, label, n))
        other_n = plat_count.get("other", 0)
        if other_n:
            platforms.append(("other", platform_label("other"), other_n))
        with self._lock:
            self._snap = _Snap(
                by_id=by_id,
                platforms=platforms,
                plat_cats=plat_cats,
                cat_name=cat_name,
                cat_platform=cat_platform,
                cat_services=cat_services,
                fetched_at=time.time(),
            )
