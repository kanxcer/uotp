"""Configuration.

Settings come from the environment, optionally seeded from a ``.env`` file.
Nothing secret has a default: a missing API key or bot token fails loudly at
startup rather than half-working at the first order.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .economics import FeeModel
from .engine import EngineConfig
from .money import INR
from .provider.uotp import ResponseShape, UotpConfig

__all__ = ["Settings", "ConfigError", "load_env_file", "from_environment"]


class ConfigError(Exception):
    """Raised for missing or unusable configuration."""


def load_env_file(path: Optional[Path | str] = None) -> dict[str, str]:
    """Read a ``KEY=value`` file into the environment without overwriting it.

    Deliberately minimal rather than pulling in python-dotenv: comments, blank
    lines, optional quotes and ``export`` prefixes are the whole format.
    """
    target = Path(path) if path else Path(".env")
    if not target.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def _get(name: str, default: Optional[str] = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"{name} is required but not set")
    return value or ""


def _decimal(name: str, default: str) -> Decimal:
    raw = _get(name, default)
    try:
        return Decimal(raw)
    except ArithmeticError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid decimal") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _get(name, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(slots=True)
class Settings:
    """Everything the bot needs to start."""

    uotp: UotpConfig
    fees: FeeModel
    engine: EngineConfig
    telegram_token: str = ""
    allowed_users: tuple[str, ...] = ()
    owner_id: str = ""
    ledger_path: str = "ledger.db"
    prices_path: Optional[str] = None

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token)

    def require_telegram(self) -> str:
        if not self.telegram_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
        return self.telegram_token


def from_environment(env_file: Optional[Path | str] = None) -> Settings:
    """Build :class:`Settings` from the environment.

    The UOTP API key is required: the bot's entire cost model depends on
    talking to the provider, and silently defaulting to an unauthenticated
    client would fail on the first order with a confusing 401.
    """
    load_env_file(env_file)

    shape = ResponseShape(
        balance=_get("UOTP_FIELD_BALANCE", "balance"),
        order_id=_get("UOTP_FIELD_ORDER_ID", "id"),
        phone=_get("UOTP_FIELD_PHONE", "number"),
        charged=_get("UOTP_FIELD_PRICE", "price"),
        sms_list=_get("UOTP_FIELD_SMS_LIST", "messages"),
        sms_text=_get("UOTP_FIELD_SMS_TEXT", "text"),
        sms_sender=_get("UOTP_FIELD_SMS_SENDER", "sender"),
        sms_time=_get("UOTP_FIELD_SMS_TIME", "created_at"),
        prices=_get("UOTP_FIELD_PRICES", "prices"),
    )
    uotp = UotpConfig(
        base_url=_get("UOTP_BASE_URL", "https://uotp.store"),
        api_key=_get("UOTP_API_KEY", required=True),
        auth_header=_get("UOTP_AUTH_HEADER", "Authorization"),
        auth_scheme=_get("UOTP_AUTH_SCHEME", "Bearer"),
        balance_path=_get("UOTP_BALANCE_PATH", "/api/v1/balance"),
        prices_path=_get("UOTP_PRICES_PATH", "/api/v1/prices"),
        buy_path=_get("UOTP_BUY_PATH", "/api/v1/number"),
        sms_path=_get("UOTP_SMS_PATH", "/api/v1/sms"),
        cancel_path=_get("UOTP_CANCEL_PATH", "/api/v1/cancel"),
        timeout=float(_get("UOTP_TIMEOUT", "20")),
        shape=shape,
    )

    fees = FeeModel(
        gateway_rate=_decimal("FEE_GATEWAY_RATE", "0.02"),
        gateway_fixed=INR(_get("FEE_GATEWAY_FIXED", "0")),
        fee_gst_rate=_decimal("FEE_GATEWAY_GST", "0.18"),
        gst_rate=_decimal("FEE_GST_RATE", "0"),
        gst_inclusive=_bool("FEE_GST_INCLUSIVE", True),
        chargeback_rate=_decimal("FEE_CHARGEBACK_RATE", "0"),
    )
    engine = EngineConfig(
        retry_cap=int(_get("ENGINE_RETRY_CAP", "3")),
        otp_timeout_seconds=float(_get("ENGINE_OTP_TIMEOUT", "290")),
        poll_interval=float(_get("ENGINE_POLL_INTERVAL", "3")),
        auto_refund=_bool("ENGINE_AUTO_REFUND", True),
        topup_headroom=_decimal("ENGINE_TOPUP_HEADROOM", "5"),
        default_country=_get("ENGINE_DEFAULT_COUNTRY", "in"),
    )
    allowed = tuple(
        u.strip() for u in _get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()
    )
    return Settings(
        uotp=uotp,
        fees=fees,
        engine=engine,
        telegram_token=_get("TELEGRAM_BOT_TOKEN", ""),
        allowed_users=allowed,
        owner_id=_get("TELEGRAM_OWNER_ID", ""),
        ledger_path=_get("LEDGER_PATH", "ledger.db"),
        prices_path=_get("PRICES_PATH") or None,
    )
