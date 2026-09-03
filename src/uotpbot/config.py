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
from .provider.uotp import UotpConfig

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


def _parse_service_map(raw: str) -> dict[str, str]:
    """Parse ``local=provider,local=provider`` into a slug translation table.

    The provider rejected our slug with BAD_SERVICE, so a mapping is needed
    once the real vocabulary is known. Empty means pass slugs through.
    """
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        local, _, provider = pair.partition("=")
        local, provider = local.strip(), provider.strip()
        if local and provider:
            out[local] = provider
    return out


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
    #: Customer wallet balances. Must share the ledger's durability story:
    #: on ephemeral free-tier hosting only DATABASE_URL survives a deploy.
    wallets_path: str = "wallets.db"
    #: Postgres connection string. When set, BOTH the ledger and the sub-bot
    #: registry live there instead of sqlite -- they must share one durability
    #: story, because a registry that outlives the ledger keeps charging fees
    #: against books that no longer exist. On Render's free tier (no disk)
    #: this is the only configuration whose audit trail survives a deploy.
    database_url: str = ""
    #: Schema the app's tables live in. A dedicated schema keeps the ledger
    #: off PostgREST's default surface and cleanly nameable in backups.
    database_schema: str = "uotp"
    prices_path: Optional[str] = None
    #: Where white-label sub-bot records live. Must be on the same persistent
    #: disk as the ledger: a sub-bot registry that survives a redeploy but a
    #: ledger that does not would charge fees it cannot account for.
    subbots_path: str = "subbots.db"
    #: Opt-in. Off by default so an existing deployment does not start
    #: spawning pollers for bots nobody asked it to run.
    whitelabel_enabled: bool = False
    #: The platform's cut of an OWN_API sub-bot sale, as a fraction of gross.
    #: Whatever is set here is what owners are shown at creation time and what
    #: is recorded against their bot -- it is not a rate the platform can
    #: quietly change after the fact, because the agreed figure is stored per
    #: sub-bot in the registry.
    platform_fee_rate: Decimal = Decimal("0.05")
    #: Your margin over true cost when pricing services (target-margin
    #: strategy). 0.10 = you keep ~10% of each sale after every cost. Deliberately
    #: an env var: this is a business decision, not a code constant.
    pricing_target_margin: Decimal = Decimal("0.35")
    #: Who customers contact to add money to their wallet (e.g. "@you" or a
    #: link). Shown on the balance screen; empty falls back to "the bot owner".
    support_contact: str = ""
    #: The UPI VPA customers pay into (e.g. "you@okaxis"). Shown on the
    #: Add Money screen. The visual QR is set by the owner from inside the bot
    #: (📊 Owner panel → 🖼 Payment QR) so it survives redeploys.
    pay_upi_id: str = ""

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

    uotp = UotpConfig(
        base_url=_get(
            "UOTP_BASE_URL", "https://uotp.store/api/stubs/handler_api.php"
        ),
        api_key=_get("UOTP_API_KEY", required=True),
        key_param=_get("UOTP_KEY_PARAM", "api_key"),
        action_param=_get("UOTP_ACTION_PARAM", "action"),
        action_balance=_get("UOTP_ACTION_BALANCE", "getBalance"),
        action_prices=_get("UOTP_ACTION_PRICES", "getPrices"),
        action_get_number=_get("UOTP_ACTION_GET_NUMBER", "getNumber"),
        action_get_status=_get("UOTP_ACTION_GET_STATUS", "getStatus"),
        action_set_status=_get("UOTP_ACTION_SET_STATUS", "setStatus"),
        action_active=_get("UOTP_ACTION_ACTIVE", "getActiveActivations"),
        balance_prefix=_get("UOTP_PREFIX_BALANCE", "ACCESS_BALANCE"),
        number_prefix=_get("UOTP_PREFIX_NUMBER", "ACCESS_NUMBER"),
        cancel_prefix=_get("UOTP_PREFIX_CANCEL", "ACCESS_CANCEL"),
        ok_prefix=_get("UOTP_PREFIX_OK", "STATUS_OK"),
        wait_prefix=_get("UOTP_PREFIX_WAIT", "STATUS_WAIT_CODE"),
        resend_prefix=_get("UOTP_PREFIX_RESEND", "STATUS_WAIT_RESEND"),
        canceled_prefix=_get("UOTP_PREFIX_CANCELED", "STATUS_CANCEL"),
        status_complete=_get("UOTP_STATUS_COMPLETE", "6"),
        status_cancel=_get("UOTP_STATUS_CANCEL", "8"),
        status_resend=_get("UOTP_STATUS_RESEND", "3"),
        prices_country=_get("UOTP_PRICES_COUNTRY", "0"),
        # Numeric operator id, verified live: "any"/"0" are rejected with
        # BAD_OPERATOR, ids >= 2 pass validation (the provider then answered
        # NO_CONNECTION -- its own backend, not our parameters).
        prices_operator=_get("UOTP_PRICES_OPERATOR", "2"),
        service_map=_parse_service_map(_get("UOTP_SERVICE_MAP", "")),
        balance_divisor=_decimal("UOTP_BALANCE_DIVISOR", "1"),
        timeout=float(_get("UOTP_TIMEOUT", "30")),
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
        default_country=_get("ENGINE_DEFAULT_COUNTRY", "22")  # uotp.store handler_api: India,
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
        wallets_path=_get("WALLETS_PATH", "wallets.db"),
        database_url=_get("DATABASE_URL", ""),
        database_schema=_get("DATABASE_SCHEMA", "uotp"),
        prices_path=_get("PRICES_PATH") or None,
        subbots_path=_get("SUBBOTS_PATH", "subbots.db"),
        whitelabel_enabled=_bool("WHITELABEL_ENABLED", False),
        platform_fee_rate=_fee_rate(_get("PLATFORM_FEE_RATE", "0.05")),
        pricing_target_margin=_fraction("PRICING_TARGET_MARGIN", "0.35"),
        support_contact=_get("SUPPORT_CONTACT", ""),
        pay_upi_id=_get("PAY_UPI_ID", ""),
    )


def _fraction(name: str, default: str) -> Decimal:
    """Parse a (0, 1) fraction env var; reject nonsense at startup."""
    raw = _get(name, default)
    try:
        value = Decimal(raw)
    except ArithmeticError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid decimal") from exc
    if not (Decimal(0) < value < Decimal(1)):
        raise ConfigError(
            f"{name}={raw} must be a fraction strictly between 0 and 1 "
            "(e.g. 0.10 for a 10% margin)"
        )
    return value


def _fee_rate(raw: str) -> Decimal:
    """Validate PLATFORM_FEE_RATE at startup rather than on first sale.

    A rate outside [0, 1] is rejected here as a ConfigError. Letting it reach
    ``PlatformFee`` would raise a WhiteLabelError from inside the /createbot
    handler, after an owner had already been shown terms and pasted a token.
    """
    try:
        rate = Decimal(raw)
    except ArithmeticError as exc:
        raise ConfigError(f"PLATFORM_FEE_RATE={raw!r} is not a valid decimal") from exc
    if not (Decimal(0) <= rate <= Decimal(1)):
        raise ConfigError(
            f"PLATFORM_FEE_RATE={raw} must be a fraction of gross in [0, 1] "
            "(e.g. 0.05 for 5%)"
        )
    return rate
