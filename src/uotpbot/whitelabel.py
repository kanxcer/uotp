"""White-label sub-bots: "create your own bot".

A user supplies a Telegram bot token and gets a working clone. Two modes:

``PLATFORM_API``
    The sub-bot buys numbers from the platform's provider wallet at the
    platform's price. The owner sets their own markup and keeps it. No
    percentage fee — the platform already earns the spread baked into the
    wholesale price.

``OWN_API``
    The owner supplies their own provider API key and pays the provider
    directly. The platform takes a percentage of each sale.

On the fee, one deliberate design decision: **it is disclosed, always.**
``SubBot.fee_disclosure()`` renders the exact terms shown at creation time, and
the rate is stored with the bot record so it cannot drift silently afterwards.
The fee is also posted to its own ledger account (``revenue:platform_fee``),
so an owner can reconcile it line by line.

That is not squeamishness — it is what makes the fee enforceable. A percentage
taken quietly from someone else's sale is an unfair trade practice under the
Consumer Protection Act 2019, and in the OWN_API case it is also *discoverable
by construction*: the owner holds the API key, so their provider dashboard
shows every rupee they were charged. A cut that does not reconcile with their
own spend gets found, and then it becomes a chargeback and a public complaint
instead of revenue.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Callable, Optional

from .money import Money, quantize_money

# Optional dependency (the [whitelabel] extra): encrypts sub-bot credentials
# at rest. The import is lazy so the base install (no white-label) never
# needs cryptography at all.
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - depends on the install extras
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]

__all__ = [
    "SubBotMode",
    "PlatformFee",
    "SubBot",
    "SubBotRegistry",
    "MultiBotManager",
    "WhiteLabelError",
    "DEFAULT_PLATFORM_FEE",
    "API_SIGNUP_URL",
    "validate_bot_token",
    "SCHEMA",
]


class WhiteLabelError(Exception):
    """Raised for invalid sub-bot configuration or state."""


class SubBotMode(str, Enum):
    """Where a sub-bot gets its numbers from."""

    PLATFORM_API = "platform_api"
    """Buys from the platform's wallet. No percentage fee."""

    OWN_API = "own_api"
    """Brings its own provider key. Platform takes a percentage of sales."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS subbots (
    id             TEXT PRIMARY KEY,
    owner_id       TEXT    NOT NULL,
    bot_token      TEXT    NOT NULL,
    mode           TEXT    NOT NULL,
    provider_key   TEXT    NOT NULL DEFAULT '',
    provider_url   TEXT    NOT NULL DEFAULT '',
    fee_rate       TEXT    NOT NULL,
    fee_fixed_p    INTEGER NOT NULL,
    disclosed_at   TEXT    NOT NULL,
    disclosure     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    reseller_rate  TEXT    NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_subbots_owner ON subbots(owner_id);
"""


@dataclass(frozen=True, slots=True)
class PlatformFee:
    """The platform's cut of a sub-bot sale.

    ``rate`` is a fraction of the gross sale; ``fixed`` is a flat amount on
    top. Both are shown to the owner before they agree, and both are stored on
    the :class:`SubBot` record so the agreed terms are the terms charged.
    """

    rate: Decimal = Decimal("0.05")
    fixed: Money = Money(0)

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.rate <= Decimal(1)):
            raise WhiteLabelError(f"fee rate must be in [0, 1], got {self.rate}")
        if self.fixed.is_negative:
            raise WhiteLabelError("fixed fee cannot be negative")

    def on(self, gross: Money) -> Money:
        """The fee charged on a sale of ``gross``, rounded up.

        Rounded up rather than half-up: the platform is the one collecting, and
        rounding in its own favour at the paise level is both immaterial and
        unambiguous. Rounding down would accumulate against it.
        """
        return quantize_money(Decimal(gross.rupees) * self.rate, ROUND_HALF_UP) + self.fixed

    def describe(self) -> str:
        parts = [f"{self.rate * 100:.2f}".rstrip("0").rstrip(".") + "% of each sale"]
        if not self.fixed.is_zero:
            parts.append(f"+ {self.fixed} flat")
        return " ".join(parts)


#: The default, disclosed at bot creation. 5% matches the split described in
#: the product brief: on a 10% margin the owner keeps ~5% and the platform ~5%.
DEFAULT_PLATFORM_FEE = PlatformFee(rate=Decimal("0.05"))

#: Where an owner goes to get their own provider API key.
API_SIGNUP_URL = "https://uotp.store/register"


@dataclass(slots=True)
class SubBot:
    """One white-label bot and the terms it was created under."""

    owner_id: str
    bot_token: str
    mode: SubBotMode
    fee: PlatformFee
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    provider_key: str = ""
    provider_url: str = ""
    #: Extra on the *platform selling price* (0.38 = 38%). 0 = same price as us.
    reseller_rate: Decimal = field(default_factory=lambda: Decimal("0"))
    disclosed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    disclosure: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    active: bool = True

    def __post_init__(self) -> None:
        if not self.bot_token:
            raise WhiteLabelError("bot_token is required")
        if self.mode is SubBotMode.OWN_API and not self.provider_key:
            raise WhiteLabelError(
                "OWN_API mode requires a provider API key; use PLATFORM_API instead"
            )
        if self.mode is SubBotMode.PLATFORM_API and self.provider_key:
            raise WhiteLabelError(
                "PLATFORM_API mode takes no provider key; the platform supplies numbers"
            )
        object.__setattr__(self, "reseller_rate", Decimal(self.reseller_rate or 0))
        if self.reseller_rate < 0 or self.reseller_rate > Decimal("2"):
            raise WhiteLabelError(
                f"reseller extra must be in [0, 2], got {self.reseller_rate}"
            )
        if not self.disclosure:
            self.disclosure = self.fee_disclosure()

    # -- money -----------------------------------------------------------
    @property
    def effective_fee_rate(self) -> Decimal:
        """The rate that actually applies. Zero in PLATFORM_API mode.

        In PLATFORM_API mode the platform earns through the wholesale price it
        charges for numbers, so charging a percentage as well would be double
        dipping. Keeping this in one property means the sale path cannot
        accidentally apply both.
        """
        return Decimal(0) if self.mode is SubBotMode.PLATFORM_API else self.fee.rate

    def fee_on(self, gross: Money) -> Money:
        """Platform fee for one sale."""
        if self.mode is SubBotMode.PLATFORM_API:
            return Money.zero()
        return self.fee.on(gross)

    def owner_keeps(self, gross: Money) -> Money:
        """What the owner's revenue will be, before their own costs."""
        return gross - self.fee_on(gross)

    # -- disclosure ------------------------------------------------------
    def fee_disclosure(self) -> str:
        """The exact terms shown to the owner. Stored with the bot record."""
        if self.mode is SubBotMode.PLATFORM_API:
            extra_pct = (self.reseller_rate * 100).quantize(Decimal("0.01"))
            extra_txt = f"{extra_pct.normalize()}%"
            from .money import INR
            from .reseller import clone_price, reseller_split
            sample = INR("14.50")
            theirs = clone_price(sample, self.reseller_rate)
            split = reseller_split(theirs, self.reseller_rate, self.fee.rate)
            cut_pct = (self.fee.rate * 100).quantize(Decimal("1"))
            return (
                "Mode: you sell OUR numbers at your extra %.\n"
                f"Your extra: {extra_txt} on our selling price.\n"
                f"Example: our {sample} → your customers pay {theirs}.\n"
                f"Of that extra ({split.extra}): we keep {cut_pct}% "
                f"({split.platform_cut}); you keep {split.owner_share}.\n"
                "Payments: customers pay through our UPI (FamGateway). "
                "You do not set a QR, UPI id, or provider API key.\n"
                "Earnings land on Withdraw in your admin panel.\n"
                "You cannot plug in your own YC OTP API.\n"
                "These terms cannot change after you create the bot."
            )
        return (
            "Mode: your own provider API.\n"
            f"Get your API key here: {API_SIGNUP_URL}\n"
            "You pay your provider directly, at their prices.\n"
            f"PLATFORM FEE: {self.fee.describe()}, taken from every sale your bot "
            "makes. This is charged whether or not the sale is profitable for "
            "you, and is itemised on every order and in /report.\n"
            f"On a {self.fee.rate * 200:.0f}% margin you keep roughly half of it.\n"
            "The fee cannot change after you create the bot."
        )


def _fernet_for(secret_key: str) -> "Fernet":
    """Build a Fernet instance from an arbitrary secret string.

    ``SECRET_KEY`` may be any long random string (a pass-phrase, a token, an
    exported secret); a Fernet key is a specific 32-byte url-safe-base64 shape,
    so we derive one deterministically with SHA-256. The same string always
    yields the same key, which is what makes stored values decryptable after a
    redeploy. Requiring the exact Fernet key shape would just fail at startup
    on every reasonable secret a human would paste.
    """
    if Fernet is None:
        raise WhiteLabelError(
            "cryptography is required for encrypted sub-bot storage: "
            "pip install 'uotpbot[whitelabel]'"
        )
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class SubBotRegistry:
    """SQLite-backed store of white-label bots. Thread-safe, like the ledger.

    Storage-independence note: same seam as :class:`ledger.Ledger` -- SQL is
    canonical (``{t}`` table, ``?`` placeholders) and all of it flows through
    :meth:`_execute` / :meth:`_write`. :class:`pgstore.PostgresRegistry`
    overrides only the connection and those two primitives.

    ``secret_key`` (optional) enables at-rest encryption of the two secrets a
    sub-bot row carries -- its Telegram ``bot_token`` and its OWN_API
    ``provider_key``. A registry file that survives a redeploy would otherwise
    hold live credentials in plaintext, so the moment any one of them leaks
    (backup, dump, a shared host) every sub-bot is compromised. With a key,
    both are stored as Fernet ciphertext and decrypted only in memory.
    """

    def __init__(self, path: Optional[str] = None, *, secret_key: str = "") -> None:
        self._table = "subbots"
        self._ph = "?"
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._lock = threading.RLock()
        self._fernet = _fernet_for(secret_key) if secret_key else None
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add reseller_rate to registries created before clone extras existed."""
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(subbots)")}
        except Exception:  # noqa: BLE001 - postgres overrides this
            cols = set()
        if cols and "reseller_rate" not in cols:
            self._conn.execute(
                "ALTER TABLE subbots ADD COLUMN reseller_rate TEXT NOT NULL DEFAULT '0'"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- at-rest secret encryption --------------------------------------
    def _enc(self, value: str) -> str:
        """Encrypt one secret for storage. No key -> stored as-is."""
        if not value or self._fernet is None:
            return value
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _dec(self, value: str) -> str:
        """Decrypt one stored secret, tolerating legacy plaintext rows.

        A value that is not a Fernet token (rows written before encryption
        existed) is returned untouched, so an existing registry keeps working
        on its first encrypted read. A token that fails to decrypt (wrong
        key) is likewise returned rather than raising -- the poller for that
        bot will fail on Telegram and be reported in /mybots, instead of
        taking down startup of every other bot.
        """
        if not value or self._fernet is None:
            return value
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError):  # noqa: BLE001
            return value

    # -- backend seam ------------------------------------------------------
    def _q(self, sql: str) -> str:
        """Translate canonical SQL (``{t}`` table, ``?`` placeholders)."""
        return sql.replace("{t}", self._table).replace("?", self._ph)

    def _tx(self):
        """A write transaction context. sqlite: the connection itself."""
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return list(self._conn.execute(self._q(sql), params))

    def _write(self, sql: str, params: tuple = ()) -> int:
        """Run one DML statement inside a transaction; returns rowcount."""
        with self._lock, self._tx():
            cur = self._conn.execute(self._q(sql), params)
            return cur.rowcount

    # -- writing ---------------------------------------------------------
    def add(self, bot: SubBot) -> SubBot:
        if self.find_by_token(bot.bot_token):
            raise WhiteLabelError("that bot token is already registered")
        self._write(
            "INSERT INTO {t} (id, owner_id, bot_token, mode, provider_key, "
            "provider_url, fee_rate, fee_fixed_p, disclosed_at, disclosure, "
            "created_at, active, reseller_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot.id, bot.owner_id, self._enc(bot.bot_token), bot.mode.value,
                self._enc(bot.provider_key), bot.provider_url, str(bot.fee.rate),
                bot.fee.fixed.paise, bot.disclosed_at, bot.disclosure,
                bot.created_at, int(bot.active), str(bot.reseller_rate),
            ),
        )
        return bot

    def set_active(self, bot_id: str, active: bool) -> None:
        self._write(
            "UPDATE {t} SET active = ? WHERE id = ?", (int(active), bot_id)
        )

    def delete(self, bot_id: str) -> bool:
        return self._write("DELETE FROM {t} WHERE id = ?", (bot_id,)) > 0

    # -- reading ---------------------------------------------------------
    def _row_to_bot(self, row: tuple) -> SubBot:
        reseller = Decimal("0")
        if len(row) > 12 and row[12] not in (None, ""):
            try:
                reseller = Decimal(str(row[12]))
            except ArithmeticError:
                reseller = Decimal("0")
        return SubBot(
            id=row[0], owner_id=row[1], bot_token=self._dec(row[2]),
            mode=SubBotMode(row[3]),
            provider_key=self._dec(row[4]) or "", provider_url=row[5] or "",
            fee=PlatformFee(rate=Decimal(row[6]), fixed=Money(int(row[7]))),
            disclosed_at=row[8], disclosure=row[9], created_at=row[10],
            active=bool(row[11]),
            reseller_rate=reseller,
        )

    _COLS = ("id, owner_id, bot_token, mode, provider_key, provider_url, "
             "fee_rate, fee_fixed_p, disclosed_at, disclosure, created_at, "
             "active, reseller_rate")

    def find_by_token(self, token: str) -> Optional[SubBot]:
        """Find a registered bot by its (plaintext) Telegram token.

        When encryption is on the stored column is ciphertext, and Fernet
        output is randomized -- so a ``WHERE bot_token = ?`` match is not
        possible. The registry holds one row per sub-bot (a handful, at most),
        so scanning and comparing the decrypted value is both correct and
        fast enough. With no key the column is plaintext and the SQL index
        path is used.
        """
        if self._fernet is None:
            rows = self._execute(
                f"SELECT {self._COLS} FROM {{t}} WHERE bot_token = ?", (token,)
            )
            return self._row_to_bot(rows[0]) if rows else None
        for row in self._execute(f"SELECT {self._COLS} FROM {{t}}"):
            if self._dec(row[2]) == token:
                return self._row_to_bot(row)
        return None

    def find(self, bot_id: str) -> Optional[SubBot]:
        rows = self._execute(
            f"SELECT {self._COLS} FROM {{t}} WHERE id = ?", (bot_id,)
        )
        return self._row_to_bot(rows[0]) if rows else None

    def for_owner(self, owner_id: str) -> list[SubBot]:
        rows = self._execute(
            f"SELECT {self._COLS} FROM {{t}} WHERE owner_id = ? ORDER BY created_at",
            (owner_id,),
        )
        return [self._row_to_bot(r) for r in rows]

    def all_active(self) -> list[SubBot]:
        rows = self._execute(
            f"SELECT {self._COLS} FROM {{t}} WHERE active = 1 ORDER BY created_at"
        )
        return [self._row_to_bot(r) for r in rows]

    def all_bots(self) -> list[SubBot]:
        """Every registered clone, including stopped ones."""
        rows = self._execute(
            f"SELECT {self._COLS} FROM {{t}} ORDER BY created_at"
        )
        return [self._row_to_bot(r) for r in rows]

    def count(self) -> int:
        return int(self._execute("SELECT COUNT(*) FROM {t}")[0][0])

    def platform_earnings(self) -> Money:
        """Sum of fees the platform is entitled to across active bots.

        This is the *agreed* terms, not realised revenue -- realised figures
        come from the ledger's ``revenue:platform_fee`` account, which is the
        only number that can be audited against actual sales.
        """
        total = Money.zero()
        for bot in self.all_active():
            if bot.mode is SubBotMode.OWN_API:
                total = total + bot.fee.fixed
        return total


def validate_bot_token(token: str) -> bool:
    """Cheap structural check on a Telegram bot token.

    Telegram tokens look like ``123456789:AA...`` -- a numeric bot id, a colon,
    then 35 characters. This catches paste errors (a username, a URL, a
    truncated copy) before they reach Telegram and fail with an opaque 401.
    It is deliberately not a guarantee: only ``getMe`` can confirm validity.
    """
    if not token or ":" not in token:
        return False
    head, _, tail = token.partition(":")
    if not head.isdigit():
        return False
    return 30 <= len(tail) <= 45 and all(
        c.isalnum() or c in "-_" for c in tail
    )


def verify_bot_token(token: str, *, timeout: float = 10.0):
    """Ask Telegram's ``getMe`` whether ``token`` is a REAL, live bot token.

    ``validate_bot_token`` is only structural; a structurally-valid token that
    was never issued by @BotFather silently produces a dead white-label bot
    ("it saved but never replies"). Confirming with ``getMe`` at creation time
    surfaces that immediately. Returns ``(True, username)`` on success, or
    ``(False, reason)`` on any failure -- never raises.
    """
    from urllib.request import Request, urlopen

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        req = Request(url, headers={"User-Agent": "uotpbot/1.1"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed api.telegram.org
            import json as _json
            payload = _json.loads(resp.read().decode("utf-8"))
        if not payload.get("ok"):
            return False, str(payload.get("description", "rejected"))
        user = payload.get("result", {})
        return True, str(user.get("username", "?"))
    except Exception as exc:  # noqa: BLE001 - offline? tell the owner, don't crash
        return False, f"{type(exc).__name__}: {exc}"


class MultiBotManager:
    """Runs one poller per white-label sub-bot, each in its own thread.

    The manager owns lifecycle only. It never touches money: the fee is applied
    by the engine the router factory hands back, so the split happens in the
    one place that already knows the sale's gross.

    A poller that raises is logged and left dead rather than restarted in a
    tight loop -- a bot with a revoked token would otherwise hammer Telegram's
    API forever and get the *platform's* IP throttled, taking the main bot down
    with it. Restart is explicit, via :meth:`restart`.
    """

    def __init__(
        self,
        registry: SubBotRegistry,
        router_factory: Callable[[SubBot], Any],
        poller_factory: Optional[Callable[[SubBot, Any], Any]] = None,
        *,
        restart_delay: float = 5.0,
    ) -> None:
        self.registry = registry
        self.router_factory = router_factory
        self.poller_factory = poller_factory
        self.restart_delay = restart_delay
        self._threads: dict[str, threading.Thread] = {}
        self._stop: dict[str, threading.Event] = {}
        self._errors: dict[str, str] = {}
        self._apps: dict[str, Any] = {}
        self._lock = threading.RLock()

    def bind_app(self, bot_id: str, app: Any) -> None:
        """Remember the PTB Application so :meth:`stop` can halt ``run_polling``."""
        with self._lock:
            self._apps[bot_id] = app

    def unbind_app(self, bot_id: str) -> None:
        with self._lock:
            self._apps.pop(bot_id, None)

    # -- lifecycle -------------------------------------------------------
    def start_all(self) -> list[str]:
        """Start every active sub-bot not already running."""
        return [bot.id for bot in self.registry.all_active() if self.start(bot.id)]

    def start(self, bot_id: str) -> bool:
        """Start one sub-bot's poller. True if it is running (already or newly)."""
        with self._lock:
            if bot_id in self._threads and self._threads[bot_id].is_alive():
                return True
            bot = self.registry.find(bot_id)
        if bot is None or not bot.active:
            return False
        try:
            router = self.router_factory(bot)
        except Exception as exc:  # noqa: BLE001 - a bad bot must not stop the rest
            self._errors[bot_id] = f"{type(exc).__name__}: {exc}"
            return False
        stop = threading.Event()
        target = self.poller_factory(bot, router) if self.poller_factory else router.run
        thread = threading.Thread(
            target=self._supervise, args=(bot_id, target, stop),
            name=f"subbot-{bot_id[:8]}", daemon=True,
        )
        with self._lock:
            self._threads[bot_id] = thread
            self._stop[bot_id] = stop
            self._errors.pop(bot_id, None)
        thread.start()
        return True

    def stop(self, bot_id: str, timeout: float = 10.0) -> bool:
        """Signal one poller to stop and wait briefly for it."""
        with self._lock:
            app = self._apps.pop(bot_id, None)
            stop = self._stop.pop(bot_id, None)
            thread = self._threads.pop(bot_id, None)
        if app is not None:
            stopper = getattr(app, "stop_running", None)
            if callable(stopper):
                try:
                    stopper()
                except Exception:  # noqa: BLE001 - join below is the fallback
                    pass
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def stop_all(self, timeout: float = 10.0) -> None:
        for bot_id in list(self._threads):
            self.stop(bot_id, timeout)

    def restart(self, bot_id: str, timeout: float = 2.0) -> bool:
        """Stop and start one poller. The only automatic-retry escape hatch."""
        self.stop(bot_id, timeout=timeout)
        return self.start(bot_id)

    # -- introspection ---------------------------------------------------
    def running(self) -> list[str]:
        with self._lock:
            return sorted(b for b, t in self._threads.items() if t.is_alive())

    def errors(self) -> dict[str, str]:
        """Why a poller is not running, by bot id."""
        return dict(self._errors)

    def health(self) -> dict[str, object]:
        """One line per sub-bot for /status and the HTTP ``/readyz`` view."""
        with self._lock:
            live = {b: t.is_alive() for b, t in self._threads.items()}
        return {
            "running": sorted(b for b, ok in live.items() if ok),
            "stopped": sorted(b for b, ok in live.items() if not ok),
            "errors": dict(self._errors),
        }

    # -- internals -------------------------------------------------------
    def _supervise(self, bot_id: str, target: Callable[[], Any], stop: threading.Event) -> None:
        try:
            target()
        except Exception as exc:  # noqa: BLE001 - record, never crash the process
            self._errors[bot_id] = f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
