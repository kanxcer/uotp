"""Customer wallet persistence.

The command router keeps customer balances in a dict, which on a host with
an ephemeral filesystem (Render's free tier restarts daily) means a paying
customer's balance simply vanishes on redeploy. These two stores give the
dict a database underneath, one sqlite, one Postgres, chosen by ``store.py``
exactly like the ledger -- because a wallet that outlives the audit trail,
or dies with it, is not a wallet.

Interface: the MutableMapping minimum the router actually uses
(``balance_of`` -> ``__getitem__``-ish via ``get``; ``credit``/debit ->
``__setitem__``), implemented here as plain methods so persistence failures
raise loudly instead of being swallowed by a dict contract.
"""

from __future__ import annotations


import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .money import Money

__all__ = [
    "WalletStore", "SqliteWallets", "PostgresWallets", "ScopedWallets",
    "WalletError", "Topup", "OrderRow", "ActiveNumber",
]


class WalletError(Exception):
    """Storage problems; never caught, because ignoring them loses money."""


class WalletStore:
    """What the router needs: read a balance, write a balance."""

    def balance(self, user_id: str) -> Money:
        raise NotImplementedError

    def set_balance(self, user_id: str, amount: Money) -> None:
        raise NotImplementedError

    def adjust(self, user_id: str, delta: Money) -> Money:
        """Atomic read-modify-write. Returns the new balance."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - lifecycle nicety
        pass

    def kv_scan(self, prefix: str) -> dict[str, str]:
        """Return {key: value} for every stored key starting with ``prefix``.

        Implementations back a small key/value table (payment settings, order
        mappings). Optional: callers check ``hasattr`` and fall back to scanning
        nothing when absent.
        """
        return {}


class ScopedWallets(WalletStore):
    """One bot's view of a shared wallet store.

    White-label sub-bots share the platform's database, but a customer who
    pays the owner of bot A must not find that balance spendable on bot B.
    Wallets are key-namespaced ``<bot ID>:<user id>``; top-ups and the QR
    carry the bot id in their ``scope`` column / key prefix, so a sub-bot
    owner can never approve (or even see) another bot's payments.
    """

    def __init__(self, inner: WalletStore, scope: str) -> None:
        self._inner = inner
        self._scope = scope

    def _key(self, user_id: str) -> str:
        return f"{self._scope}:{user_id}"

    def balance(self, user_id: str) -> Money:
        return self._inner.balance(self._key(user_id))

    def set_balance(self, user_id: str, amount: Money) -> None:
        self._inner.set_balance(self._key(user_id), amount)

    def adjust(self, user_id: str, delta: Money) -> Money:
        return self._inner.adjust(self._key(user_id), delta)

    # Delegated with the explicit scope column/prefix --------------------
    def create_topup(self, user_id, amount, *, note="", photo_file_id=""):
        return self._inner.create_topup(user_id, amount, note=note,
                                        photo_file_id=photo_file_id, scope=self._scope)

    def get_topup(self, topup_id, **kw):
        return self._inner.get_topup(topup_id, scope=self._scope)

    def user_topups(self, user_id):
        return self._inner.user_topups(user_id, scope=self._scope)

    def pending_topups(self):
        return self._inner.pending_topups(scope=self._scope)

    def decide_topup(self, topup_id, status, *, decided_by):
        return self._inner.decide_topup(topup_id, status, decided_by=decided_by,
                                        scope=self._scope)

    def kv_get(self, key):
        return self._inner.kv_get(f"{self._scope}:{key}")

    def kv_set(self, key, value):
        self._inner.kv_set(f"{self._scope}:{key}", value)

    def kv_scan(self, prefix: str) -> dict[str, str]:
        """Return {key: value} for every key starting with ``prefix`` in scope."""
        return self._inner.kv_scan(f"{self._scope}:{prefix}")

    def record_order(self, *, user_id, slug, amount, phone="", otp="",
                     success=False, profit=Money(0), status="", reason="",
                     refunded=Money(0), spent=Money(0), balance_after=Money(0)):
        self._inner.record_order(user_id=user_id, slug=slug, amount=amount,
                                 phone=phone, otp=otp, success=success,
                                 profit=profit, scope=self._scope, status=status,
                                 reason=reason, refunded=refunded, spent=spent,
                                 balance_after=balance_after)

    def recent_orders(self, *, user_id: str = "", limit: int = 10):
        return self._inner.recent_orders(scope=self._scope, user_id=user_id, limit=limit)

    def get_order(self, order_id: int, *, user_id: str = ""):
        return self._inner.get_order(order_id, scope=self._scope, user_id=user_id)

    # -- active numbers (scoped per bot) -----------------------------------
    def record_active(self, *, user_id, slug, phone, provider_order_id="",
                      token="", gross, otp="", valid_until):
        return self._inner.record_active(user_id=user_id, slug=slug, phone=phone,
                                         provider_order_id=provider_order_id,
                                         token=token, gross=gross, otp=otp,
                                         valid_until=valid_until, scope=self._scope)

    def active_numbers(self, *, user_id: str = "", now=None):
        return self._inner.active_numbers(scope=self._scope, user_id=user_id, now=now)

    def get_active(self, token: str):
        found = self._inner.get_active(token)
        # Scope-guard: a wait-token only admits this bot's own live numbers.
        # (Tokens are random, but "can't guess it" is not a boundary.)
        if found is not None and found.scope != self._scope:
            return None
        return found

    def update_active(self, provider_order_id: str, *, otp: str = ""):
        return self._inner.update_active(provider_order_id, otp=otp,
                                         scope=self._scope)

    def finish_active(self, provider_order_id: str):
        return self._inner.finish_active(provider_order_id, scope=self._scope)

    def write_refund(self, *, user_id, amount, order_token="", reason="", scope="",
                     ledger_done=False):
        # Refunds are recorded at the platform scope; the credit itself stays
        # scoped to this bot via ``adjust``, so a sub-bot never loses a refund.
        return self._inner.write_refund(user_id=user_id, amount=amount,
                                        order_token=order_token, reason=reason,
                                        scope="", ledger_done=ledger_done)

    def mark_ledger_done(self, order_token, *, scope=""):
        # Same platform scope as write_refund: the ledger line is per order.
        return self._inner.mark_ledger_done(order_token, scope="")

    def get_refund(self, order_token, *, scope=""):
        return self._inner.get_refund(order_token, scope="")

    def pending_refunds(self, *, scope="", max_attempts: int = 5):
        return self._inner.pending_refunds(scope="", max_attempts=max_attempts)

    def mark_refund_result(self, refund_id, *, done, error="", scope=""):
        return self._inner.mark_refund_result(refund_id, done=done, error=error, scope="")

    def float_stats(self):
        return self._inner.float_stats(scope=self._scope)

    def user_ids(self):
        return self._inner.user_ids(scope=self._scope)

    def touch_user(self, user_id: str, *, scope: str = "") -> None:
        # Scope always comes from this wrapper: a sub-bot must not write
        # another bot's seen-user row.
        self._inner.touch_user(user_id, scope=self._scope)

    def list_users(self, *, limit: int = 40, offset: int = 0):
        return self._inner.list_users(scope=self._scope, limit=limit, offset=offset)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    user_id TEXT PRIMARY KEY,
    balance_paise INTEGER NOT NULL CHECK (balance_paise >= 0)
)
"""

_TOPUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    {pk}
    scope TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    note TEXT NOT NULL DEFAULT '',
    photo_file_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'declined')),
    created_ts REAL NOT NULL,
    decided_ts REAL,
    decided_by TEXT
)
"""

_ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    {pk}
    scope TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    gross_paise INTEGER NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    otp TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    profit_paise INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    refunded_paise INTEGER NOT NULL DEFAULT 0,
    spent_paise INTEGER NOT NULL DEFAULT 0,
    balance_after_paise INTEGER NOT NULL DEFAULT 0
)
"""

#: Live (not yet expired) numbers a customer is still entitled to see. Unlike
#: ``orders`` (written only once an order is *completed*), a row is created the
#: moment a number is allocated so it stays viewable in "My numbers" even after
#: the customer leaves the buy screen -- and it outlives a redeploy, which
#: matters because an active number is a real, already-charged activation.
_ACTIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    {pk}
    scope TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    provider_order_id TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    gross_paise INTEGER NOT NULL,
    otp TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    valid_until REAL NOT NULL
)
"""

# ``scope`` is also SELECTed on active rows (added by _migrate_orders on old
# tables) so a sub-bot's view can be scope-guarded.
_ACTIVE_SELECT = ("id, user_id, slug, phone, provider_order_id, token, "
                  "gross_paise, otp, ts, valid_until, "
                  "COALESCE(scope, '')")

_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

#: Everyone who has used the bot, including customers who never got a wallet
#: row (a wallet row is only written on credit/debit). Admin "All users",
#: Total users, and broadcast UNION this with wallets/orders/topups.
_SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    scope TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (scope, user_id)
)
"""


def _bare_wallet_uid(stored: str, scope: str) -> str:
    """Strip a ``scope:`` prefix from a wallets.user_id, if present."""
    uid = (stored or "").strip()
    if not uid:
        return ""
    if scope:
        prefix = scope + ":"
        if uid.startswith(prefix):
            return uid[len(prefix):]
    return uid


def _page_users(ids: list[str], seen: dict[str, float], *,
                limit: int, offset: int) -> tuple[list[tuple[str, float]], int]:
    """Sort ``ids`` by last_seen desc and slice one page. Empty last_seen last."""
    decorated = sorted(
        ((float(seen.get(u, 0.0)), u) for u in ids),
        key=lambda t: (t[0], t[1]),
        reverse=True,
    )
    total = len(decorated)
    limit = 40 if int(limit) <= 0 else int(limit)
    offset = max(0, int(offset))
    page = decorated[offset: offset + limit]
    return [(u, ts) for ts, u in page], total

#: Durable refund outbox. A refund is written here BEFORE the wallet credit or
#: ledger post is attempted, so if that credit ever fails the refund is never
#: silently lost -- a background worker retries the pending rows (and a redeploy
#: re-reads them), guaranteeing a customer who was actually refunded gets their
#: money. One row per order_token (a cancel can only ever refund once): the
#: UNIQUE constraint makes the INSERT the claim, so racing paths cannot both
#: credit. ``ledger_done`` records that the customer-refund LEDGER line for
#: this order is already posted, so a retry after a wallet-credit blip never
#: posts a second line (which would make reported refunds outrun real money).
_REFUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    {pk}
    scope TEXT NOT NULL DEFAULT '',
    refund_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    order_token TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL,
    done_ts REAL,
    ledger_done INTEGER NOT NULL DEFAULT 0 CHECK (ledger_done IN (0, 1)),
    UNIQUE(scope, order_token)
)
"""

_REFUND_SELECT = ("id, refund_id, user_id, amount_paise, order_token, reason,"
                  " status, attempts, COALESCE(last_error, ''), created_ts,"
                  " COALESCE(ledger_done, 0)")


@dataclass(frozen=True, slots=True)
class OrderRow:
    """One customer order, for history and the owner's per-order P&L view.

    Display cache only: the double-entry ledger remains the source of truth
    for money. This table answers "what did each order earn" without mining
    journal lines.
    """

    id: int
    user_id: str
    slug: str
    gross: Money
    phone: str
    otp: str
    success: bool
    profit: Money
    ts: float
    status: str = ""
    reason: str = ""
    refunded: Money = field(default_factory=Money.zero)
    spent: Money = field(default_factory=Money.zero)
    balance_after: Money = field(default_factory=Money.zero)

    @property
    def profit_ratio(self) -> Optional[float]:
        if self.gross.paise <= 0:
            return None
        return round(self.profit.paise / self.gross.paise, 3)


def _order_from_row(row):
    """Build an OrderRow from an orders-table row (any column order on the right)."""
    # Columns: id, user_id, slug, gross_paise, phone, otp, success, profit_paise,
    #          ts, status, reason, refunded_paise, spent_paise, balance_after_paise
    pad = list(row) + [0] * (14 - len(row))
    return OrderRow(
        id=pad[0], user_id=pad[1], slug=pad[2], gross=Money(pad[3]),
        phone=pad[4] or "", otp=pad[5] or "", success=bool(pad[6]),
        profit=Money(pad[7]), ts=float(pad[8]),
        status=pad[9] or "", reason=pad[10] or "",
        refunded=Money(pad[11]), spent=Money(pad[12]), balance_after=Money(pad[13]),
    )


@dataclass(frozen=True, slots=True)
class Topup:
    """One customer payment awaiting (or past) owner review."""

    id: int
    user_id: str
    amount: Money
    note: str = ""
    photo_file_id: str = ""
    status: str = "pending"
    created_ts: float = 0.0
    decided_by: str = ""


@dataclass(frozen=True, slots=True)
class ActiveNumber:
    """One live number a customer can still see (not yet expired).

    ``token`` links back to the in-memory wait so the UI can offer Check OTP /
    Resend / Cancel; it is empty after a redeploy (the number is still shown,
    just without live actions).
    """

    id: int
    user_id: str
    slug: str
    phone: str
    provider_order_id: str
    token: str
    gross: Money
    otp: str
    ts: float
    valid_until: float
    #: Which sub-bot (or the platform, "") this number belongs to. Surfaced so
    #: a scoped view can refuse rows from another bot instead of leaking them.
    scope: str = ""

    @property
    def seconds_left(self) -> float:
        return max(self.valid_until - time.time(), 0.0)

    @property
    def has_otp(self) -> bool:
        return bool(self.otp)


@dataclass(frozen=True, slots=True)
class RefundRow:
    """One durable refund-outbox row awaiting (or past) credit.

    A refund is written here before any money moves; if the credit or ledger
    post fails the row stays 'pending' and a retry worker (or a redeploy)
    re-attempts until it succeeds, so a confirmed customer refund is never
    silently lost. ``ledger_done`` says the customer-refund ledger line is
    already posted, so retries never post it a second time.
    """

    id: int
    refund_id: str
    user_id: str
    amount: Money
    order_token: str
    reason: str
    status: str
    attempts: int
    last_error: str
    created_ts: float
    ledger_done: bool = False


class SqliteWallets(WalletStore):
    """File-backed store for local/dev runs.

    ``check_same_thread=False`` + a lock, same pattern as ``ledger.py``:
    the Telegram transport deliberately runs handlers off the event loop
    thread, so every access must be thread-safe.
    """

    def __init__(self, path: str = "wallets.db") -> None:
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute(_SCHEMA.format(t="wallets"))
            self._conn.execute(
                _TOPUPS_SCHEMA.format(t="topups", pk="id INTEGER PRIMARY KEY AUTOINCREMENT,")
            )
            self._conn.execute(_KV_SCHEMA.format(t="kv"))
            self._conn.execute(
                _ORDERS_SCHEMA.format(t="orders", pk="id INTEGER PRIMARY KEY AUTOINCREMENT,")
            )
            self._conn.execute(_ACTIVE_SCHEMA.format(
                t="activenumbers", pk="id INTEGER PRIMARY KEY AUTOINCREMENT,"))
            self._conn.execute(_REFUND_SCHEMA.format(
                t="refund_outbox", pk="id INTEGER PRIMARY KEY AUTOINCREMENT,"))
            self._conn.execute(_SEEN_SCHEMA.format(t="seen_users"))
            # BEFORE the index: a legacy database gains the scope column here.
            self._migrate_orders()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_active ON activenumbers(scope, user_id)")
            self._conn.commit()

    def _migrate_orders(self) -> None:
        """Add newer columns to existing tables (old DBs).

        Columns are only added when absent, so a fresh database (created with
        the current DDL) skips this entirely.
        """
        migrations = {
            "orders": {
                "status": "TEXT NOT NULL DEFAULT ''",
                "reason": "TEXT NOT NULL DEFAULT ''",
                "refunded_paise": "INTEGER NOT NULL DEFAULT 0",
                "spent_paise": "INTEGER NOT NULL DEFAULT 0",
                "balance_after_paise": "INTEGER NOT NULL DEFAULT 0",
            },
            "refund_outbox": {
                # Refund idempotency: whether the ledger line is already posted.
                "ledger_done": "INTEGER NOT NULL DEFAULT 0",
            },
            "activenumbers": {
                # Sub-bot scope isolation for live numbers.
                "scope": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, cols in migrations.items():
            existing = {r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in cols.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # -- active numbers ----------------------------------------------------
    def record_active(self, *, user_id, slug, phone, provider_order_id="",
                      token="", gross, otp="", valid_until, scope="") -> int:
        """Persist a live number so it stays viewable until it expires."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO activenumbers(scope, user_id, slug, phone, "
                " provider_order_id, token, gross_paise, otp, ts, valid_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope, user_id, slug, phone, provider_order_id, token,
                 gross.paise, otp, time.time(), valid_until),
            )
            return int(cur.lastrowid or 0)

    def active_numbers(self, *, scope: str = "", user_id: str = "",
                       now: Optional[float] = None) -> list[ActiveNumber]:
        """Every LIVE number for a user (ts <= now < valid_until), newest first."""
        now = time.time() if now is None else now
        sql = (f"SELECT {_ACTIVE_SELECT} FROM activenumbers "
               " WHERE scope = ? AND valid_until > ?")
        params: list = [scope, now]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._active_from(r) for r in rows]

    def get_active(self, token: str) -> Optional[ActiveNumber]:
        """The live number identified by its wait-token, or None.

        Used to re-enter Check OTP / Resend / Cancel after a restart, when the
        in-memory wait entry is gone but the row is still live in the DB.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ACTIVE_SELECT} FROM activenumbers "
                " WHERE token = ? AND valid_until > ?",
                (token, time.time()),
            ).fetchone()
        return self._active_from(row) if row else None

    def update_active(self, provider_order_id: str, *, otp: str = "",
                      scope: str = "") -> None:
        """Record the OTP on a live number once it arrives (this scope only)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE activenumbers SET otp = ? "
                "WHERE provider_order_id = ? AND scope = ?",
                (otp, provider_order_id, scope),
            )

    def finish_active(self, provider_order_id: str, *, scope: str = "") -> None:
        """Remove a number from the live set (delivered, refunded, or expired)."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM activenumbers WHERE provider_order_id = ? AND scope = ?",
                (provider_order_id, scope),
            )

    # -- refund outbox -----------------------------------------------------
    def write_refund(self, *, user_id: str, amount: Money, order_token: str = "",
                     reason: str = "", scope: str = "",
                     ledger_done: bool = False) -> tuple[str, bool]:
        """Durably claim + record a refund before any money moves.

        Returns ``(refund_id, inserted)``: ``inserted`` is True only when THIS
        call created the row (the unique ``(scope, order_token)`` constraint
        made the insert the claim). Callers that do not insert did not win the
        claim and must not credit.
        """
        refund_id = uuid.uuid4().hex
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO refund_outbox(scope, refund_id, user_id, "
                " amount_paise, order_token, reason, created_ts, ledger_done) "
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (scope, refund_id, user_id, amount.paise, order_token, reason,
                 time.time(), 1 if ledger_done else 0),
            )
            inserted = (cur.rowcount or 0) == 1
        return refund_id, inserted

    def mark_ledger_done(self, order_token: str, *, scope: str = "") -> None:
        """Persist that this order's customer-refund ledger line is posted."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE refund_outbox SET ledger_done = 1 "
                "WHERE scope = ? AND order_token = ?",
                (scope, order_token),
            )

    def get_refund(self, order_token: str, *, scope: str = "") -> Optional[RefundRow]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_REFUND_SELECT} FROM refund_outbox"
                " WHERE scope = ? AND order_token = ?",
                (scope, order_token)).fetchone()
        return self._refund_from(row) if row else None

    def pending_refunds(self, *, scope: str = "", max_attempts: int = 5) -> list[RefundRow]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_REFUND_SELECT} FROM refund_outbox"
                " WHERE scope = ? AND status = 'pending' AND attempts < ? ORDER BY id",
                (scope, max_attempts)).fetchall()
        return [self._refund_from(r) for r in rows]

    def mark_refund_result(self, refund_id: str, *, done: bool, error: str = "",
                           scope: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE refund_outbox SET status = ?, attempts = attempts + 1,"
                " last_error = ?, done_ts = ? WHERE scope = ? AND refund_id = ?",
                ("done" if done else "pending", error,
                 time.time() if done else None, scope, refund_id),
            )

    @staticmethod
    def _refund_from(row) -> RefundRow:
        return RefundRow(
            id=row[0], refund_id=row[1], user_id=row[2], amount=Money(row[3]),
            order_token=row[4], reason=row[5], status=row[6], attempts=row[7],
            last_error=row[8], created_ts=row[9], ledger_done=bool(row[10]),
        )

    @staticmethod
    def _active_from(row) -> ActiveNumber:
        return ActiveNumber(
            id=row[0], user_id=row[1], slug=row[2], phone=row[3],
            provider_order_id=row[4], token=row[5], gross=Money(row[6]),
            otp=row[7], ts=row[8], valid_until=row[9], scope=row[10],
        )

    # -- payment top-ups ---------------------------------------------------
    def create_topup(
        self, user_id: str, amount: Money, *, note: str = "",
        photo_file_id: str = "", scope: str = "",
    ) -> int:
        if amount.is_negative or amount.is_zero:
            raise WalletError("top-up amount must be positive")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO topups(scope, user_id, amount_paise, note, photo_file_id, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (scope, user_id, amount.paise, note, photo_file_id, time.time()),
            )
            return int(cur.lastrowid or 0)

    def get_topup(self, topup_id: int, *, scope: str = "") -> Optional[Topup]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, amount_paise, note, photo_file_id, status, created_ts,"
                " COALESCE(decided_by, '') FROM topups WHERE id = ? AND scope = ?",
                (topup_id, scope),
            ).fetchone()
        return self._topup_from(row) if row else None

    def user_topups(self, user_id: str, *, scope: str = "") -> list[Topup]:
        """Every top-up a user submitted (any status), newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, user_id, amount_paise, note, photo_file_id, status, created_ts,"
                " COALESCE(decided_by, '') FROM topups"
                " WHERE scope = ? AND user_id = ? ORDER BY id DESC",
                (scope, user_id),
            ).fetchall()
        return [self._topup_from(r) for r in rows]

    def pending_topups(self, *, scope: str = "") -> list[Topup]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, user_id, amount_paise, note, photo_file_id, status, created_ts,"
                " COALESCE(decided_by, '') FROM topups"
                " WHERE scope = ? AND status = 'pending' ORDER BY id DESC",
                (scope,),
            ).fetchall()
        return [self._topup_from(r) for r in rows]

    def decide_topup(self, topup_id: int, status: str, *, decided_by: str,
                     scope: str = "") -> bool:
        """Move pending -> approved/declined. Returns False if already decided."""
        if status not in ("approved", "declined"):
            raise WalletError(f"bad topup status {status!r}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE topups SET status = ?, decided_ts = ?, decided_by = ?"
                " WHERE id = ? AND scope = ? AND status = 'pending'",
                (status, time.time(), decided_by, topup_id, scope),
            )
            return cur.rowcount == 1

    @staticmethod
    def _topup_from(row) -> Topup:
        return Topup(id=row[0], user_id=row[1], amount=Money(row[2]), note=row[3],
                     photo_file_id=row[4], status=row[5], created_ts=row[6],
                     decided_by=row[7])

    # -- small key/value (payment QR, etc.) --------------------------------
    def kv_get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def kv_scan(self, prefix: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        return {k: v for k, v in rows}

    # -- orders + float stats ----------------------------------------------
    def record_order(self, *, user_id: str, slug: str, amount: Money,
                     phone: str = "", otp: str = "", success: bool = False,
                     profit: Money = Money(0), scope: str = "", status: str = "",
                     reason: str = "", refunded: Money = Money(0),
                     spent: Money = Money(0), balance_after: Money = Money(0),
                     ) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO orders(scope, user_id, slug, gross_paise, phone, otp,"
                " success, profit_paise, ts, status, reason, refunded_paise,"
                " spent_paise, balance_after_paise)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope, user_id, slug, amount.paise, phone, otp,
                 1 if success else 0, profit.paise, time.time(), status, reason,
                 refunded.paise, spent.paise, balance_after.paise),
            )
            return int(cur.lastrowid or 0)

    def recent_orders(self, *, scope: str = "", user_id: str = "", limit: int = 10
                      ) -> list[OrderRow]:
        sql = ("SELECT id, user_id, slug, gross_paise, phone, otp, success,"
               " profit_paise, ts, COALESCE(status, ''), COALESCE(reason, ''),"
               " COALESCE(refunded_paise, 0), COALESCE(spent_paise, 0),"
               " COALESCE(balance_after_paise, 0) FROM orders WHERE scope = ?")
        params: list = [scope]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_order_from_row(r) for r in rows]

    def get_order(self, order_id: int, *, scope: str = "", user_id: str = ""
                  ) -> Optional[OrderRow]:
        sql = ("SELECT id, user_id, slug, gross_paise, phone, otp, success,"
               " profit_paise, ts, COALESCE(status, ''), COALESCE(reason, ''),"
               " COALESCE(refunded_paise, 0), COALESCE(spent_paise, 0),"
               " COALESCE(balance_after_paise, 0) FROM orders"
               " WHERE id = ? AND scope = ?")
        params: list = [order_id, scope]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return _order_from_row(row) if row else None

    def float_stats(self, *, scope: str = "") -> dict[str, object]:
        """Customer float: what we OWE users right now.

        ``users`` is everyone who has used the bot (seen + wallets + orders +
        topups + live numbers), not merely wallet-row count -- a /start with
        ₹0 never created a wallets row, which is why the owner panel used to
        report 2 customers while dozens were live.
        ``float`` is still the SUM of wallet balances.
        """
        users = len(self.user_ids(scope=scope))
        like = (scope + ":%") if scope else None
        with self._lock:
            if like:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(balance_paise), 0) FROM wallets"
                    " WHERE user_id LIKE ?", (like,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(balance_paise), 0) FROM wallets"
                    " WHERE user_id NOT LIKE '%:%'").fetchone()
        return {"users": users, "float": Money(int(row[0] if row else 0))}

    def touch_user(self, user_id: str, *, scope: str = "") -> None:
        """Record that ``user_id`` just used this bot (idempotent upsert)."""
        uid = (user_id or "").strip()
        if not uid:
            return
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO seen_users(scope, user_id, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope, user_id) DO UPDATE SET last_seen = excluded.last_seen",
                (scope, uid, now, now),
            )

    def user_ids(self, *, scope: str = "") -> list[str]:
        """Every customer id for this scope (seen, wallets, orders, topups, live)."""
        ids: set[str] = set()
        with self._lock:
            for (uid,) in self._conn.execute(
                "SELECT user_id FROM seen_users WHERE scope = ?", (scope,),
            ):
                if uid:
                    ids.add(str(uid))
            if scope:
                prefix = scope + ":"
                for (uid,) in self._conn.execute(
                    "SELECT user_id FROM wallets WHERE user_id LIKE ?",
                    (prefix + "%",),
                ):
                    bare = _bare_wallet_uid(uid, scope)
                    if bare:
                        ids.add(bare)
            else:
                for (uid,) in self._conn.execute(
                    "SELECT user_id FROM wallets WHERE user_id NOT LIKE '%:%'"
                ):
                    if uid:
                        ids.add(str(uid))
            for table in ("orders", "topups", "activenumbers"):
                for (uid,) in self._conn.execute(
                    f"SELECT DISTINCT user_id FROM {table} WHERE scope = ?",
                    (scope,),
                ):
                    if uid:
                        ids.add(str(uid))
        return sorted(ids)

    def list_users(self, *, scope: str = "", limit: int = 40, offset: int = 0
                   ) -> tuple[list[tuple[str, float]], int]:
        """One page of ``(user_id, last_seen)`` plus the total customer count."""
        ids = self.user_ids(scope=scope)
        seen: dict[str, float] = {}
        with self._lock:
            for uid, ts in self._conn.execute(
                "SELECT user_id, last_seen FROM seen_users WHERE scope = ?",
                (scope,),
            ):
                if uid:
                    seen[str(uid)] = float(ts or 0)
        return _page_users(ids, seen, limit=limit, offset=offset)

    def balance(self, user_id: str) -> Money:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance_paise FROM wallets WHERE user_id = ?", (user_id,)
            ).fetchone()
        return Money(row[0]) if row else Money.zero()

    def set_balance(self, user_id: str, amount: Money) -> None:
        if amount.is_negative:
            raise WalletError(f"balance cannot go negative for {user_id}")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO wallets(user_id, balance_paise) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance_paise = excluded.balance_paise",
                (user_id, amount.paise),
            )

    def adjust(self, user_id: str, delta: Money) -> Money:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT balance_paise FROM wallets WHERE user_id = ?", (user_id,)
            ).fetchone()
            new = Money(row[0] if row else 0) + delta
            if new.is_negative:
                raise WalletError(f"balance cannot go negative for {user_id}")
            self._conn.execute(
                "INSERT INTO wallets(user_id, balance_paise) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance_paise = excluded.balance_paise",
                (user_id, new.paise),
            )
        return new

    def close(self) -> None:
        self._conn.close()


class PostgresWallets(WalletStore):
    """Postgres-backed store for production; shares the ledger's database.

    One shared connection serialised by a lock, and ``prepare_threshold=None``
    like ``pgstore`` -- pgBouncer transaction pooling makes server-side
    prepared statements intermittently vanish.
    """

    def __init__(self, dsn: str, *, schema: str = "uotp") -> None:
        try:
            import psycopg  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise WalletError(
                "PostgreSQL wallets need psycopg: pip install 'uotpbot[postgres]'"
            ) from exc
        if not schema.isidentifier():
            raise WalletError(f"unsafe schema name {schema!r}")
        self._lock = threading.RLock()
        self._conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)
        self._integrity = psycopg.errors.IntegrityError
        self._t = f"{schema}.wallets"
        self._tt = f"{schema}.topups"
        self._tk = f"{schema}.kv"
        with self._lock:
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            self._conn.execute(_SCHEMA.format(t=self._t))
            self._conn.execute(_TOPUPS_SCHEMA.format(
                t=self._tt, pk="id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"))
            self._conn.execute(_KV_SCHEMA.format(t=self._tk))
            self._to = f"{schema}.orders"
            self._conn.execute(_ORDERS_SCHEMA.format(
                t=self._to, pk="id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"))
            self._ta = f"{schema}.activenumbers"
            self._conn.execute(_ACTIVE_SCHEMA.format(
                t=self._ta, pk="id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"))
            self._tr = f"{schema}.refund_outbox"
            self._conn.execute(_REFUND_SCHEMA.format(
                t=self._tr, pk="id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"))
            self._ts = f"{schema}.seen_users"
            self._conn.execute(_SEEN_SCHEMA.format(t=self._ts))
            # BEFORE the index: a legacy database gains the scope column here.
            self._migrate_orders()
            safe = schema.replace("-", "_").replace('"', "")
            self._conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_active_{safe}" ON {self._ta}(scope, user_id)')

    # -- active numbers ----------------------------------------------------
    def record_active(self, *, user_id, slug, phone, provider_order_id="",
                      token="", gross, otp="", valid_until, scope="") -> int:
        with self._lock:
            row = self._conn.execute(
                f"INSERT INTO {self._ta}(scope, user_id, slug, phone, "
                f" provider_order_id, token, gross_paise, otp, ts, valid_until) "
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (scope, user_id, slug, phone, provider_order_id, token,
                 gross.paise, otp, time.time(), valid_until),
            ).fetchone()
        return int(row[0])

    def active_numbers(self, *, scope: str = "", user_id: str = "",
                       now: Optional[float] = None) -> list[ActiveNumber]:
        now = time.time() if now is None else now
        sql = (f"SELECT {_ACTIVE_SELECT} FROM {self._ta} "
               " WHERE scope = %s AND valid_until > %s")
        params: list = [scope, now]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._active_from(r) for r in rows]

    def get_active(self, token: str) -> Optional[ActiveNumber]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_ACTIVE_SELECT} FROM {self._ta} "
                " WHERE token = %s AND valid_until > %s",
                (token, time.time()),
            ).fetchone()
        return self._active_from(row) if row else None

    def update_active(self, provider_order_id: str, *, otp: str = "",
                      scope: str = "") -> None:
        with self._lock:
            self._conn.execute(
                f"UPDATE {self._ta} SET otp = %s WHERE provider_order_id = %s AND scope = %s",
                (otp, provider_order_id, scope),
            )

    def finish_active(self, provider_order_id: str, *, scope: str = "") -> None:
        with self._lock:
            self._conn.execute(
                f"DELETE FROM {self._ta} WHERE provider_order_id = %s AND scope = %s",
                (provider_order_id, scope),
            )

    # -- refund outbox -----------------------------------------------------
    def write_refund(self, *, user_id: str, amount: Money, order_token: str = "",
                     reason: str = "", scope: str = "",
                     ledger_done: bool = False) -> tuple[str, bool]:
        """Claim + record a refund. See :meth:`SqliteWallets.write_refund`.

        ``ON CONFLICT DO NOTHING`` makes the insert the claim; the command
        result says whether this call won it.
        """
        refund_id = uuid.uuid4().hex
        with self._lock:
            inserted = bool(self._conn.execute(
                f"INSERT INTO {self._tr}(scope, refund_id, user_id, amount_paise,"
                f" order_token, reason, created_ts, ledger_done) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT(scope, order_token) DO NOTHING",
                (scope, refund_id, user_id, amount.paise, order_token, reason,
                 time.time(), 1 if ledger_done else 0),
            ).rowcount == 1)
        return refund_id, inserted

    def mark_ledger_done(self, order_token: str, *, scope: str = "") -> None:
        with self._lock:
            self._conn.execute(
                f"UPDATE {self._tr} SET ledger_done = 1 "
                "WHERE scope = %s AND order_token = %s",
                (scope, order_token),
            )

    def get_refund(self, order_token: str, *, scope: str = "") -> Optional[RefundRow]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_REFUND_SELECT} FROM {self._tr}"
                " WHERE scope = %s AND order_token = %s",
                (scope, order_token)).fetchone()
        return self._refund_from(row) if row else None

    def pending_refunds(self, *, scope: str = "", max_attempts: int = 5) -> list[RefundRow]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_REFUND_SELECT} FROM {self._tr}"
                " WHERE scope = %s AND status = 'pending' AND attempts < %s ORDER BY id",
                (scope, max_attempts)).fetchall()
        return [self._refund_from(r) for r in rows]

    def mark_refund_result(self, refund_id: str, *, done: bool, error: str = "",
                           scope: str = "") -> None:
        with self._lock:
            self._conn.execute(
                f"UPDATE {self._tr} SET status = %s, attempts = attempts + 1,"
                f" last_error = %s, done_ts = %s WHERE scope = %s AND refund_id = %s",
                ("done" if done else "pending", error,
                 time.time() if done else None, scope, refund_id),
            )

    @staticmethod
    def _refund_from(row) -> RefundRow:
        return RefundRow(
            id=row[0], refund_id=row[1], user_id=row[2], amount=Money(row[3]),
            order_token=row[4], reason=row[5], status=row[6], attempts=row[7],
            last_error=row[8], created_ts=row[9], ledger_done=bool(row[10]),
        )

    @staticmethod
    def _active_from(row) -> ActiveNumber:
        return ActiveNumber(
            id=row[0], user_id=row[1], slug=row[2], phone=row[3],
            provider_order_id=row[4], token=row[5], gross=Money(row[6]),
            otp=row[7], ts=row[8], valid_until=row[9], scope=row[10],
        )

    # -- payment top-ups ---------------------------------------------------
    def create_topup(
        self, user_id: str, amount: Money, *, note: str = "",
        photo_file_id: str = "", scope: str = "",
    ) -> int:
        if amount.is_negative or amount.is_zero:
            raise WalletError("top-up amount must be positive")
        with self._lock:
            row = self._conn.execute(
                f"INSERT INTO {self._tt}(scope, user_id, amount_paise, note, photo_file_id,"
                f" created_ts) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (scope, user_id, amount.paise, note, photo_file_id, time.time()),
            ).fetchone()
        return int(row[0])

    def get_topup(self, topup_id: int, *, scope: str = "") -> Optional[Topup]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT id, user_id, amount_paise, note, photo_file_id, status, created_ts,"
                f" COALESCE(decided_by, '') FROM {self._tt} WHERE id = %s AND scope = %s",
                (topup_id, scope),
            ).fetchone()
        return self._topup_from(row) if row else None

    def user_topups(self, user_id: str, *, scope: str = "") -> list[Topup]:
        """Every top-up a user submitted (any status), newest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, user_id, amount_paise, note, photo_file_id, status, created_ts,"
                f" COALESCE(decided_by, '') FROM {self._tt}"
                " WHERE scope = %s AND user_id = %s ORDER BY id DESC",
                (scope, user_id),
            ).fetchall()
        return [self._topup_from(r) for r in rows]

    def pending_topups(self, *, scope: str = "") -> list[Topup]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, user_id, amount_paise, note, photo_file_id, status, created_ts,"
                f" COALESCE(decided_by, '') FROM {self._tt}"
                " WHERE scope = %s AND status = 'pending' ORDER BY id DESC",
                (scope,),
            ).fetchall()
        return [self._topup_from(r) for r in rows]

    def decide_topup(self, topup_id: int, status: str, *, decided_by: str,
                     scope: str = "") -> bool:
        if status not in ("approved", "declined"):
            raise WalletError(f"bad topup status {status!r}")
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {self._tt} SET status = %s, decided_ts = %s, decided_by = %s"
                " WHERE id = %s AND scope = %s AND status = 'pending'",
                (status, time.time(), decided_by, topup_id, scope),
            )
            return cur.rowcount == 1

    @staticmethod
    def _topup_from(row) -> Topup:
        return Topup(id=row[0], user_id=row[1], amount=Money(row[2]), note=row[3],
                     photo_file_id=row[4], status=row[5], created_ts=row[6],
                     decided_by=row[7])

    # -- small key/value ---------------------------------------------------
    def kv_get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT value FROM {self._tk} WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._tk}(key, value) VALUES(%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    def kv_scan(self, prefix: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT key, value FROM {self._tk} WHERE key LIKE %s",
                (f"{prefix}%",),
            ).fetchall()
        return {k: v for k, v in rows}

    # -- orders + float stats ----------------------------------------------
    def record_order(self, *, user_id: str, slug: str, amount: Money,
                     phone: str = "", otp: str = "", success: bool = False,
                     profit: Money = Money(0), scope: str = "", status: str = "",
                     reason: str = "", refunded: Money = Money(0),
                     spent: Money = Money(0), balance_after: Money = Money(0),
                     ) -> None:
        with self._lock:
            row = self._conn.execute(
                f"INSERT INTO {self._to}(scope, user_id, slug, gross_paise, phone, otp,"
                f" success, profit_paise, ts, status, reason, refunded_paise,"
                f" spent_paise, balance_after_paise)"
                f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING id",
                # explicit int: psycopg binds Python bool as boolean, which the
                # INTEGER CHECK column rejects (sqlite coerces booleans silently)
                (scope, user_id, slug, amount.paise, phone, otp,
                 1 if success else 0, profit.paise, time.time(), status, reason,
                 refunded.paise, spent.paise, balance_after.paise),
            ).fetchone()
            return int(row[0])

    def recent_orders(self, *, scope: str = "", user_id: str = "", limit: int = 10
                      ) -> list[OrderRow]:
        sql = (f"SELECT id, user_id, slug, gross_paise, phone, otp, success,"
               f" profit_paise, ts, COALESCE(status, ''), COALESCE(reason, ''),"
               f" COALESCE(refunded_paise, 0), COALESCE(spent_paise, 0),"
               f" COALESCE(balance_after_paise, 0) FROM {self._to} WHERE scope = %s")
        params: list = [scope]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_order_from_row(r) for r in rows]

    def get_order(self, order_id: int, *, scope: str = "", user_id: str = ""
                  ) -> Optional[OrderRow]:
        sql = (f"SELECT id, user_id, slug, gross_paise, phone, otp, success,"
               f" profit_paise, ts, COALESCE(status, ''), COALESCE(reason, ''),"
               f" COALESCE(refunded_paise, 0), COALESCE(spent_paise, 0),"
               f" COALESCE(balance_after_paise, 0) FROM {self._to}"
               " WHERE id = %s AND scope = %s")
        params: list = [order_id, scope]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return _order_from_row(row) if row else None

    def _migrate_orders(self) -> None:
        """Add newer columns to existing tables (old DBs).

        ``ADD COLUMN IF NOT EXISTS`` keeps this idempotent on fresh schemas.
        """
        migrations = {
            self._to: {
                "status": "TEXT NOT NULL DEFAULT ''",
                "reason": "TEXT NOT NULL DEFAULT ''",
                "refunded_paise": "INTEGER NOT NULL DEFAULT 0",
                "spent_paise": "INTEGER NOT NULL DEFAULT 0",
                "balance_after_paise": "INTEGER NOT NULL DEFAULT 0",
            },
            self._tr: {
                # Refund idempotency: whether the ledger line is already posted.
                "ledger_done": "INTEGER NOT NULL DEFAULT 0",
            },
            self._ta: {
                # Sub-bot scope isolation for live numbers.
                "scope": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, cols in migrations.items():
            for name, ddl in cols.items():
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}")
                except Exception:  # noqa: BLE001 - never block startup on drift checks
                    pass

    def float_stats(self, *, scope: str = "") -> dict[str, object]:
        """Customer float: users = everyone who used the bot; float = wallet SUM."""
        users = len(self.user_ids(scope=scope))
        like = (scope + ":%") if scope else None
        with self._lock:
            if like:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(balance_paise), 0) FROM"
                    f" {self._t} WHERE user_id LIKE %s", (like,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(balance_paise), 0) FROM"
                    f" {self._t} WHERE user_id NOT LIKE '%:%'").fetchone()
        return {"users": users, "float": Money(int(row[0] if row else 0))}

    def touch_user(self, user_id: str, *, scope: str = "") -> None:
        uid = (user_id or "").strip()
        if not uid:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._ts}(scope, user_id, first_seen, last_seen) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(scope, user_id) DO UPDATE SET last_seen = EXCLUDED.last_seen",
                (scope, uid, now, now),
            )

    def user_ids(self, *, scope: str = "") -> list[str]:
        ids: set[str] = set()
        with self._lock:
            for (uid,) in self._conn.execute(
                f"SELECT user_id FROM {self._ts} WHERE scope = %s", (scope,),
            ):
                if uid:
                    ids.add(str(uid))
            if scope:
                prefix = scope + ":"
                for (uid,) in self._conn.execute(
                    f"SELECT user_id FROM {self._t} WHERE user_id LIKE %s",
                    (prefix + "%",),
                ):
                    bare = _bare_wallet_uid(uid, scope)
                    if bare:
                        ids.add(bare)
            else:
                for (uid,) in self._conn.execute(
                    f"SELECT user_id FROM {self._t} WHERE user_id NOT LIKE '%:%'"
                ):
                    if uid:
                        ids.add(str(uid))
            for table in (self._to, self._tt, self._ta):
                for (uid,) in self._conn.execute(
                    f"SELECT DISTINCT user_id FROM {table} WHERE scope = %s",
                    (scope,),
                ):
                    if uid:
                        ids.add(str(uid))
        return sorted(ids)

    def list_users(self, *, scope: str = "", limit: int = 40, offset: int = 0
                   ) -> tuple[list[tuple[str, float]], int]:
        ids = self.user_ids(scope=scope)
        seen: dict[str, float] = {}
        with self._lock:
            for uid, ts in self._conn.execute(
                f"SELECT user_id, last_seen FROM {self._ts} WHERE scope = %s",
                (scope,),
            ):
                if uid:
                    seen[str(uid)] = float(ts or 0)
        return _page_users(ids, seen, limit=limit, offset=offset)

    def balance(self, user_id: str) -> Money:
        with self._lock:
            row = self._conn.execute(
                f"SELECT balance_paise FROM {self._t} WHERE user_id = %s", (user_id,)
            ).fetchone()
        return Money(row[0]) if row else Money.zero()

    def set_balance(self, user_id: str, amount: Money) -> None:
        if amount.is_negative:
            raise WalletError(f"balance cannot go negative for {user_id}")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._t}(user_id, balance_paise) VALUES(%s, %s) "
                "ON CONFLICT(user_id) DO UPDATE SET balance_paise = EXCLUDED.balance_paise",
                (user_id, amount.paise),
            )

    def adjust(self, user_id: str, delta: Money) -> Money:
        # Two steps in one transaction, NOT a naive INSERT..ON CONFLICT..DO
        # UPDATE with ``delta`` as the candidate balance: Postgres validates
        # CHECK constraints on the INSERT candidate /before/ conflict
        # resolution, so a debit candidate of -1500 fails the >=0 CHECK even
        # when the existing row would update to a healthy 48500 (it broke a
        # real purchase live; SQLite evaluates the CHECK on the final stored
        # row instead, which is why the sqlite tests never caught it).
        #
        #   1. Upsert a zero row (candidate 0 is always CHECK-safe).
        #   2. UPDATE the row to the new balance; the CHECK guards the FINAL
        #      value, so a genuine overdraft still dies loudly at the DB.
        with self._lock:
            try:
                with self._conn.transaction():
                    self._conn.execute(
                        f"INSERT INTO {self._t}(user_id, balance_paise) VALUES(%s, 0) "
                        "ON CONFLICT(user_id) DO NOTHING",
                        (user_id,),
                    )
                    row = self._conn.execute(
                        f"UPDATE {self._t} SET balance_paise = {self._t}.balance_paise + %s "
                        "WHERE user_id = %s RETURNING balance_paise",
                        (delta.paise, user_id),
                    ).fetchone()
            except self._integrity as exc:
                # The final-value CHECK fired: an overdraft, not a bug.
                raise WalletError(f"balance cannot go negative for {user_id}") from exc
        new = Money(row[0])
        if new.is_negative:
            raise WalletError(f"balance cannot go negative for {user_id}")
        return new

    def close(self) -> None:
        self._conn.close()


class MappingAdapter(dict):
    """Dict-shaped view over a store so existing code that treats wallets as
    ``dict[str, Money]`` keeps working while balances live in a database.

    Only the operations the router performs are implemented; anything else
    raises TypeError on purpose rather than reading a stale copy.
    """

    def __init__(self, store: WalletStore) -> None:
        super().__init__()
        self._store = store

    def get(self, user_id, default=None):  # noqa: D102 - dict contract
        return self.balance(user_id) if user_id is not None else default

    def __getitem__(self, user_id):
        return self._store.balance(user_id)

    def __setitem__(self, user_id, amount):
        self._store.set_balance(user_id, amount)

    def balance(self, user_id) -> Money:
        return self._store.balance(user_id)

    def adjust(self, user_id, delta: Money) -> Money:
        return self._store.adjust(user_id, delta)
