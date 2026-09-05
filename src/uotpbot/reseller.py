"""Clone-bot extra: price on the platform selling price, then split the extra.

A clone never plugs in its own UOTP key. Customers pay the platform's
FamGateway. The clone owner picks an extra % on *our* shelf price::

    our Blinkit  ₹14.50
    extra        38%
    clone price  ₹20.01
    extra rupees ₹5.51
    platform cut 5% of extra  → ₹0.28
    owner keeps  ₹5.23  (credited as withdrawable earnings)

Rounding: extra is priced CEILING (never under-charge the customer);
the 5% cut is HALF_UP; the owner gets whatever paise remain so the
three parts still sum to the extra.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .money import ROUND_CEILING, ROUND_HALF_UP, Money, quantize_money

__all__ = [
    "DEFAULT_RESELLER_RATE",
    "MARGIN_FEE_RATE",
    "ResellerSplit",
    "clone_price",
    "reseller_split",
    "parse_percent",
    "earnings_balance",
    "credit_earnings",
    "debit_earnings",
    "request_withdraw",
    "pending_withdrawals",
    "get_withdraw",
    "settle_withdraw",
]

#: Suggested extra on our selling price (38% → ₹14.50 becomes ₹20.01).
DEFAULT_RESELLER_RATE = Decimal("0.38")
#: Platform cut of the clone owner's extra, not of gross.
MARGIN_FEE_RATE = Decimal("0.05")

_EARN = "earn:{}"
_WD = "wdpend:{}"


@dataclass(frozen=True, slots=True)
class ResellerSplit:
    """One successful clone sale, broken into who keeps what."""

    wholesale: Money
    clone_price: Money
    extra: Money
    platform_cut: Money
    owner_share: Money


def clone_price(wholesale: Money, extra_rate: Decimal) -> Money:
    """Shelf price on the clone: our price × (1 + extra), rounded up."""
    if extra_rate <= 0:
        return wholesale
    return quantize_money(
        Decimal(wholesale.rupees) * (Decimal(1) + Decimal(extra_rate)),
        ROUND_CEILING,
    )


def reseller_split(
    gross: Money,
    extra_rate: Decimal,
    fee_rate: Decimal = MARGIN_FEE_RATE,
) -> ResellerSplit:
    """Invert a clone sale into wholesale / extra / 5% cut / owner share."""
    extra_rate = Decimal(extra_rate)
    fee_rate = Decimal(fee_rate)
    if extra_rate <= 0 or gross.is_zero:
        return ResellerSplit(gross, gross, Money.zero(), Money.zero(), Money.zero())
    wholesale = quantize_money(
        Decimal(gross.rupees) / (Decimal(1) + extra_rate), ROUND_HALF_UP
    )
    extra = gross - wholesale
    if extra.paise <= 0:
        return ResellerSplit(gross, gross, Money.zero(), Money.zero(), Money.zero())
    cut = extra.scale(fee_rate, ROUND_HALF_UP)
    if cut.paise > extra.paise:
        cut = extra
    owner = extra - cut
    return ResellerSplit(wholesale, gross, extra, cut, owner)


def parse_percent(text: str) -> Optional[Decimal]:
    """``38``, ``38%``, ``0.38`` → ``Decimal('0.38')``. None if unusable.

    Values in (0, 1] are fractions; values in (1, 200] are percents.
    """
    raw = (text or "").strip().lower()
    for tok in ("percent", "%", "pct"):
        raw = raw.replace(tok, "")
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except ArithmeticError:
        return None
    if value <= 0:
        return None
    if value <= Decimal("1"):
        rate = value
    elif value <= Decimal("200"):
        rate = value / Decimal(100)
    else:
        return None
    if rate < Decimal("0.01") or rate > Decimal("2"):
        return None
    return rate


def earnings_balance(store, owner_id: str) -> Money:
    get = getattr(store, "kv_get", None)
    if not callable(get) or not owner_id:
        return Money.zero()
    try:
        raw = get(_EARN.format(owner_id)) or "0"
        return Money(int(raw))
    except Exception:  # noqa: BLE001
        return Money.zero()


def credit_earnings(store, owner_id: str, amount: Money) -> Money:
    """Add ``amount`` to the clone owner's withdrawable earnings."""
    if amount.is_negative or amount.is_zero or not owner_id:
        return earnings_balance(store, owner_id)
    set_ = getattr(store, "kv_set", None)
    if not callable(set_):
        return Money.zero()
    new = earnings_balance(store, owner_id) + amount
    set_(_EARN.format(owner_id), str(new.paise))
    return new


def debit_earnings(store, owner_id: str, amount: Money) -> Money:
    """Take ``amount`` off earnings. Raises ValueError if it would go negative."""
    if amount.is_negative or amount.is_zero:
        raise ValueError("debit amount must be positive")
    cur = earnings_balance(store, owner_id)
    if cur.paise < amount.paise:
        raise ValueError("insufficient earnings")
    set_ = getattr(store, "kv_set", None)
    if not callable(set_):
        raise ValueError("no earnings store")
    new = cur - amount
    set_(_EARN.format(owner_id), str(new.paise))
    return new


def request_withdraw(store, owner_id: str, amount: Money, upi: str) -> str:
    """Freeze ``amount`` from earnings and open a pending payout request."""
    upi = (upi or "").strip()
    if not upi or "@" not in upi:
        raise ValueError("send a UPI id, e.g. name@okaxis")
    debit_earnings(store, owner_id, amount)
    set_ = getattr(store, "kv_set", None)
    wd_id = uuid.uuid4().hex[:12]
    payload = {
        "id": wd_id,
        "owner_id": owner_id,
        "amount_paise": amount.paise,
        "upi": upi,
        "status": "pending",
        "ts": time.time(),
    }
    set_(_WD.format(wd_id), json.dumps(payload))
    return wd_id


def _read_wd(store, wd_id: str) -> Optional[dict]:
    get = getattr(store, "kv_get", None)
    if not callable(get):
        return None
    raw = get(_WD.format(wd_id)) or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def get_withdraw(store, wd_id: str) -> Optional[dict]:
    return _read_wd(store, wd_id)


def pending_withdrawals(store) -> list[dict]:
    scan = getattr(store, "kv_scan", None)
    if not callable(scan):
        return []
    out: list[dict] = []
    try:
        rows = scan("wdpend:")
    except Exception:  # noqa: BLE001
        return []
    for raw in rows.values():
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data.get("status") == "pending":
            out.append(data)
    out.sort(key=lambda d: float(d.get("ts") or 0), reverse=True)
    return out


def settle_withdraw(store, wd_id: str, *, paid: bool) -> Optional[dict]:
    """Mark a request paid, or decline and return the money to earnings."""
    data = _read_wd(store, wd_id)
    if data is None or data.get("status") != "pending":
        return None
    set_ = getattr(store, "kv_set", None)
    if not callable(set_):
        return None
    if paid:
        data["status"] = "paid"
    else:
        data["status"] = "declined"
        try:
            credit_earnings(
                store, str(data.get("owner_id") or ""),
                Money(int(data.get("amount_paise") or 0)),
            )
        except Exception:  # noqa: BLE001
            pass
    data["decided_ts"] = time.time()
    set_(_WD.format(wd_id), json.dumps(data))
    return data
