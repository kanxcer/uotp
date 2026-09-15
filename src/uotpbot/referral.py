"""Percentage referral programme.

A customer shares ``https://t.me/<bot>?start=r_<their_id>``. The first
``/start`` with that payload binds them to the referrer (once, never self).
When the referee's OTP or social-boost purchase succeeds, the referrer is
credited ``rate × net spend`` (default 5%, hard cap 10%). A later refund
claws the same share back. Bindings and payouts live in the wallet kv
table so they survive a redeploy.

Feature on/off and the rate are *platform* flags (clones cannot set them).
The bind itself is per-bot (a clone's customers refer inside that clone).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from .money import Money
from .reseller import parse_percent

log = logging.getLogger("uotpbot.referral")

__all__ = [
    "DEFAULT_RATE",
    "MAX_RATE",
    "FEATURE_KEY",
    "RATE_KEY",
    "ReferralPayout",
    "is_enabled",
    "set_enabled",
    "get_rate",
    "set_rate",
    "parse_rate",
    "parse_start_payload",
    "bind",
    "referrer_of",
    "invite_link",
    "credit_spend",
    "clawback_spend",
    "stats",
]

DEFAULT_RATE = Decimal("0.05")
MAX_RATE = Decimal("0.10")
MIN_RATE = Decimal("0.01")

FEATURE_KEY = "feature_referral"
RATE_KEY = "referral_rate"
_BIND = "ref_of:{}"
_PAID = "ref_paid:{}"


@dataclass(frozen=True, slots=True)
class ReferralPayout:
    """One credited (or clawed-back) referral share."""

    referrer_id: str
    amount: Money


def _kv_get(store, key: str) -> str:
    get = getattr(store, "kv_get", None) if store is not None else None
    if not callable(get):
        return ""
    try:
        return str(get(key) or "")
    except Exception:  # noqa: BLE001
        return ""


def _kv_set(store, key: str, value: str) -> bool:
    set_ = getattr(store, "kv_set", None) if store is not None else None
    if not callable(set_):
        return False
    try:
        set_(key, value)
        return True
    except Exception:  # noqa: BLE001
        log.debug("referral kv_set %s failed", key, exc_info=True)
        return False


def is_enabled(store) -> bool:
    """True while the owner has the programme ON. Default OFF."""
    v = _kv_get(store, FEATURE_KEY)
    return v == "1"


def set_enabled(store, on: bool) -> bool:
    _kv_set(store, FEATURE_KEY, "1" if on else "0")
    return bool(on)


def parse_rate(text: str) -> Optional[Decimal]:
    """``5``, ``5%``, ``0.05`` → a rate in [1%, 10%]. None if unusable."""
    rate = parse_percent(text)
    if rate is None:
        return None
    if rate < MIN_RATE or rate > MAX_RATE:
        return None
    return rate


def get_rate(store) -> Decimal:
    raw = _kv_get(store, RATE_KEY).strip()
    if not raw:
        return DEFAULT_RATE
    try:
        rate = Decimal(raw)
    except ArithmeticError:
        return DEFAULT_RATE
    if rate < MIN_RATE:
        return DEFAULT_RATE
    if rate > MAX_RATE:
        return MAX_RATE
    return rate


def set_rate(store, rate: Decimal) -> Decimal:
    rate = Decimal(rate)
    if rate < MIN_RATE or rate > MAX_RATE:
        raise ValueError(f"referral rate must be between {MIN_RATE} and {MAX_RATE}")
    _kv_set(store, RATE_KEY, str(rate))
    return rate


def parse_start_payload(text: str) -> str:
    """Extract the referrer uid from ``/start r_<uid>`` / ``r_uid`` / ``r123``.

    Telegram deep-links arrive as ``/start r_7493927458`` (payload after the
    command). ``/start@BotName r_…`` is accepted too. Empty when there is
    no referral payload.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = raw.split()
    payload = parts[-1] if parts else ""
    cmd = parts[0].split("@", 1)[0].lower() if parts else ""
    if cmd in ("/start", "/help") and len(parts) == 1:
        return ""
    if payload.lower().startswith("/start"):
        return ""
    token = payload.strip()
    if token.lower().startswith("r_"):
        uid = token[2:].strip()
        return uid if uid else ""
    # Bare ``r`` + digits (deep-link payloads cannot contain ``?``).
    if len(token) > 1 and token[0] in "rR" and token[1:].isdigit():
        return token[1:]
    return ""


def referrer_of(store, user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    return _kv_get(store, _BIND.format(uid)).strip()


def bind(store, user_id: str, referrer_id: str) -> bool:
    """Bind ``user_id`` to ``referrer_id`` once. False if skipped.

    Never overwrites an existing bind. Never binds self. Empty ids no-op.
    """
    uid = str(user_id or "").strip()
    ref = str(referrer_id or "").strip()
    if not uid or not ref or uid == ref:
        return False
    if referrer_of(store, uid):
        return False
    return _kv_set(store, _BIND.format(uid), ref)


def invite_link(bot_username: str, user_id: str) -> str:
    """``https://t.me/<bot>?start=r_<uid>``, or empty when the handle is unknown."""
    handle = (bot_username or "").strip().lstrip("@")
    uid = str(user_id or "").strip()
    if not handle or not uid:
        return ""
    return f"https://t.me/{handle}?start=r_{uid}"


def _share_paise(charge: Money, rate: Decimal) -> int:
    if charge is None or charge.paise <= 0 or rate <= 0:
        return 0
    return int((Decimal(charge.paise) * Decimal(rate)).to_integral_value(ROUND_DOWN))


def credit_spend(flag_store, wallet_store, user_id: str, charge: Money,
                 *, order_key: str) -> Optional[ReferralPayout]:
    """Credit the referrer ``rate × charge`` once per ``order_key``.

    No-op when the programme is off, the user has no referrer, the share
    rounds to 0 paise, or this order was already paid. Never raises into a
    purchase path.
    """
    key = str(order_key or "").strip()
    uid = str(user_id or "").strip()
    if not key or not uid or charge is None or charge.paise <= 0:
        return None
    if not is_enabled(flag_store):
        return None
    if _kv_get(wallet_store, _PAID.format(key)):
        return None
    ref = referrer_of(wallet_store, uid)
    if not ref or ref == uid:
        return None
    paise = _share_paise(charge, get_rate(flag_store))
    if paise < 1:
        return None
    amount = Money(paise)
    adjust = getattr(wallet_store, "adjust", None)
    if not callable(adjust):
        return None
    try:
        try:
            adjust(ref, amount, kind="referral",
                   note=f"referral · {uid}")
        except TypeError:
            adjust(ref, amount)
    except Exception:  # noqa: BLE001 - never roll back the customer's buy
        log.exception("referral credit failed for %s on %s", ref, key)
        return None
    _kv_set(wallet_store, _PAID.format(key), json.dumps({
        "referrer": ref, "paise": paise, "charge_paise": charge.paise,
    }))
    touch = getattr(wallet_store, "touch_user", None)
    if callable(touch):
        try:
            touch(ref)
        except Exception:  # noqa: BLE001
            pass
    return ReferralPayout(ref, amount)


def clawback_spend(flag_store, wallet_store, order_key: str,
                   *, refunded: Money, original: Money) -> Optional[ReferralPayout]:
    """Take back the referral share in proportion to ``refunded / original``.

    Idempotent per remaining paid paise (the stored ``paise`` shrinks). A
    referrer who already spent the reward is debited only up to their
    current balance — we never drive a wallet negative.
    """
    key = str(order_key or "").strip()
    if not key or refunded is None or refunded.paise <= 0:
        return None
    raw = _kv_get(wallet_store, _PAID.format(key))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    ref = str(data.get("referrer") or "").strip()
    paid = int(data.get("paise") or 0)
    charge_paise = int(data.get("charge_paise") or 0)
    if not ref or paid <= 0:
        return None
    orig = original.paise if original is not None and original.paise > 0 else charge_paise
    if orig <= 0:
        return None
    due = min(paid, (paid * refunded.paise) // orig)
    if due < 1:
        return None
    adjust = getattr(wallet_store, "adjust", None)
    if not callable(adjust):
        return None
    take = due
    bal_fn = getattr(wallet_store, "balance", None)
    if callable(bal_fn):
        try:
            have = int(bal_fn(ref).paise)
            take = min(take, max(0, have))
        except Exception:  # noqa: BLE001
            take = due
    if take < 1:
        data["paise"] = 0
        _kv_set(wallet_store, _PAID.format(key), json.dumps(data))
        return None
    try:
        try:
            adjust(ref, Money(-take), kind="referral",
                   note="referral clawback")
        except TypeError:
            adjust(ref, Money(-take))
    except Exception:  # noqa: BLE001
        log.exception("referral clawback failed for %s on %s", ref, key)
        return None
    data["paise"] = max(0, paid - take)
    _kv_set(wallet_store, _PAID.format(key), json.dumps(data))
    return ReferralPayout(ref, Money(take))


def stats(store, user_id: str) -> tuple[int, Money]:
    """``(referred_count, lifetime_credits)`` for ``user_id``.

    Count is how many users have this id as their referrer. Earnings sum
    *positive* ``kind=referral`` wallet lines (clawbacks are the negatives).
    """
    uid = str(user_id or "").strip()
    if not uid:
        return 0, Money.zero()
    n = 0
    scan = getattr(store, "kv_scan", None)
    if callable(scan):
        try:
            rows = scan("ref_of:")
        except Exception:  # noqa: BLE001
            rows = {}
        for raw in (rows or {}).values():
            if str(raw or "").strip() == uid:
                n += 1
    earned = 0
    lister = getattr(store, "list_wallet_tx", None)
    if callable(lister):
        offset = 0
        page = 50
        seen = 0
        while True:
            try:
                txs, total = lister(uid, limit=page, offset=offset)
            except TypeError:
                try:
                    txs, total = lister(uid, limit=page)
                except Exception:  # noqa: BLE001
                    break
                # No offset support: one page is all we get.
                total = len(txs or [])
            except Exception:  # noqa: BLE001
                break
            txs = txs or []
            for tx in txs:
                if getattr(tx, "kind", "") == "referral":
                    delta = getattr(tx, "delta", None)
                    paise = int(getattr(delta, "paise", 0) or 0)
                    if paise > 0:
                        earned += paise
            seen += len(txs)
            if not txs or seen >= int(total or 0) or len(txs) < page:
                break
            offset += len(txs)
            if offset > 10_000:
                break
    return n, Money(earned)
