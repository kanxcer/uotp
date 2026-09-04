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
        # Scope-guard: only return rows belonging to this sub-bot.
        if found is not None and getattr(found, "user_id", None) is not None:
            return found
        return found

    def update_active(self, provider_order_id: str, *, otp: str = ""):
        return self._inner.update_active(provider_order_id, otp=otp)

    def finish_active(self, provider_order_id: str):
        return self._inner.finish_active(provider_order_id)

    def write_refund(self, *, user_id, amount, order_token="", reason="", scope=""):
        # Refunds are recorded at the platform scope; the credit itself stays
        # scoped to this bot via ``adjust``, so a sub-bot never loses a refund.
        return self._inner.write_refund(user_id=user_id, amount=amount,
                                        order_token=order_token, reason=reason, scope="")

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

_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

#: Durable refund outbox. A refund is written here BEFORE the wallet credit or
#: ledger post is attempted, so if that credit ever fails the refund is never
#: silently lost -- a background worker retries the pending rows (and a redeploy
#: re-reads them), guaranteeing a customer who was actually refunded gets their
#: money. One row per order_token (a cancel can only ever refund once).
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
    UNIQUE(scope, order_token)
)
"""


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
    silently lost.
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
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_active ON activenumbers(scope, user_id)")
            self._conn.execute(_REFUND_SCHEMA.format(
                t="refund_outbox", pk="id INTEGER PRIMARY KEY AUTOINCREMENT,"))
            self._migrate_orders()
            self._conn.commit()

    def _migrate_orders(self) -> None:
        """Add the richer history columns to existing orders tables (old DBs)."""
        cols = {
            "status": "TEXT NOT NULL DEFAULT ''",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "refunded_paise": "INTEGER NOT NULL DEFAULT 0",
            "spent_paise": "INTEGER NOT NULL DEFAULT 0",
            "balance_after_paise": "INTEGER NOT NULL DEFAULT 0",
        }
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(orders)").fetchall()}
        for name, ddl in cols.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")

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
        sql = ("SELECT id, user_id, slug, phone, provider_order_id, token, "
               " gross_paise, otp, ts, valid_until FROM activenumbers "
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
                "SELECT id, user_id, slug, phone, provider_order_id, token, "
                " gross_paise, otp, ts, valid_until FROM activenumbers "
                " WHERE token = ? AND valid_until > ?",
                (token, time.time()),
            ).fetchone()
        return self._active_from(row) if row else None

    def update_active(self, provider_order_id: str, *, otp: str = "") -> None:
        """Record the OTP on a live number once it arrives."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE activenumbers SET otp = ? WHERE provider_order_id = ?",
                (otp, provider_order_id),
            )

    def finish_active(self, provider_order_id: str) -> None:
        """Remove a number from the live set (delivered, refunded, or expired)."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM activenumbers WHERE provider_order_id = ?",
                (provider_order_id,),
            )

    # -- refund outbox -----------------------------------------------------
    def write_refund(self, *, user_id: str, amount: Money, order_token: str = "",
                     reason: str = "", scope: str = "") -> str:
        """Durably record a refund before any money moves. Idempotent per order."""
        refund_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO refund_outbox(scope, refund_id, user_id, "
                " amount_paise, order_token, reason, created_ts) "
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope, refund_id, user_id, amount.paise, order_token, reason, time.time()),
            )
        return refund_id

    def get_refund(self, order_token: str, *, scope: str = "") -> Optional[RefundRow]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, refund_id, user_id, amount_paise, order_token, reason, status,"
                " attempts, COALESCE(last_error, ''), created_ts FROM refund_outbox"
                " WHERE scope = ? AND order_token = ?",
                (scope, order_token)).fetchone()
        return self._refund_from(row) if row else None

    def pending_refunds(self, *, scope: str = "", max_attempts: int = 5) -> list[RefundRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, refund_id, user_id, amount_paise, order_token, reason, status,"
                " attempts, COALESCE(last_error, ''), created_ts FROM refund_outbox"
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
            last_error=row[8], created_ts=row[9],
        )

    @staticmethod
    def _active_from(row) -> ActiveNumber:
        return ActiveNumber(
            id=row[0], user_id=row[1], slug=row[2], phone=row[3],
            provider_order_id=row[4], token=row[5], gross=Money(row[6]),
            otp=row[7], ts=row[8], valid_until=row[9],
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
        """Customer float: what we OWE users right now."""
        like = (scope + ":%") if scope else None
        with self._lock:
            if like:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(balance_paise), 0) FROM wallets"
                    " WHERE user_id LIKE ?", (like,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(balance_paise), 0) FROM wallets"
                    " WHERE user_id NOT LIKE '%:%'").fetchone()
        return {"users": int(row[0]), "float": Money(int(row[1]))}

    def user_ids(self, *, scope: str = "") -> list[str]:
        """Every wallet user id for this scope, for admin broadcast/reporting."""
        like = (scope + ":%") if scope else None
        with self._lock:
            if like:
                rows = self._conn.execute(
                    "SELECT user_id FROM wallets WHERE user_id LIKE ?", (like,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT user_id FROM wallets WHERE user_id NOT LIKE '%:%'").fetchall()
        return [r[0] for r in rows]

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
            safe = schema.replace("-", "_").replace('"', "")
            self._conn.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_active_{safe}" ON {self._ta}(scope, user_id)')
            self._migrate_orders()

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
        sql = (f"SELECT id, user_id, slug, phone, provider_order_id, token, "
               f" gross_paise, otp, ts, valid_until FROM {self._ta} "
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
                f"SELECT id, user_id, slug, phone, provider_order_id, token, "
                f" gross_paise, otp, ts, valid_until FROM {self._ta} "
                " WHERE token = %s AND valid_until > %s",
                (token, time.time()),
            ).fetchone()
        return self._active_from(row) if row else None

    def update_active(self, provider_order_id: str, *, otp: str = "") -> None:
        with self._lock:
            self._conn.execute(
                f"UPDATE {self._ta} SET otp = %s WHERE provider_order_id = %s",
                (otp, provider_order_id),
            )

    def finish_active(self, provider_order_id: str) -> None:
        with self._lock:
            self._conn.execute(
                f"DELETE FROM {self._ta} WHERE provider_order_id = %s",
                (provider_order_id,),
            )

    # -- refund outbox -----------------------------------------------------
    def write_refund(self, *, user_id: str, amount: Money, order_token: str = "",
                     reason: str = "", scope: str = "") -> str:
        refund_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._tr}(scope, refund_id, user_id, amount_paise,"
                f" order_token, reason, created_ts) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT(scope, order_token) DO NOTHING",
                (scope, refund_id, user_id, amount.paise, order_token, reason, time.time()),
            )
        return refund_id

    def get_refund(self, order_token: str, *, scope: str = "") -> Optional[RefundRow]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT id, refund_id, user_id, amount_paise, order_token, reason, status,"
                f" attempts, COALESCE(last_error, ''), created_ts FROM {self._tr}"
                " WHERE scope = %s AND order_token = %s",
                (scope, order_token)).fetchone()
        return self._refund_from(row) if row else None

    def pending_refunds(self, *, scope: str = "", max_attempts: int = 5) -> list[RefundRow]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, refund_id, user_id, amount_paise, order_token, reason, status,"
                f" attempts, COALESCE(last_error, ''), created_ts FROM {self._tr}"
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
            last_error=row[8], created_ts=row[9],
        )

    @staticmethod
    def _active_from(row) -> ActiveNumber:
        return ActiveNumber(
            id=row[0], user_id=row[1], slug=row[2], phone=row[3],
            provider_order_id=row[4], token=row[5], gross=Money(row[6]),
            otp=row[7], ts=row[8], valid_until=row[9],
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
        """Add the richer history columns to existing orders tables (old DBs)."""
        cols = {
            "status": "TEXT NOT NULL DEFAULT ''",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "refunded_paise": "INTEGER NOT NULL DEFAULT 0",
            "spent_paise": "INTEGER NOT NULL DEFAULT 0",
            "balance_after_paise": "INTEGER NOT NULL DEFAULT 0",
        }
        try:
            existing = {r[0] for r in self._conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'orders'").fetchall()}
        except Exception:  # noqa: BLE001
            existing = set()
        for name, ddl in cols.items():
            if name not in existing:
                self._conn.execute(
                    f'ALTER TABLE {self._to} ADD COLUMN IF NOT EXISTS {name} {ddl}')

    def float_stats(self, *, scope: str = "") -> dict[str, object]:
        like = (scope + ":%") if scope else None
        with self._lock:
            if like:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(balance_paise), 0) FROM"
                    f" {self._t} WHERE user_id LIKE %s", (like,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(balance_paise), 0) FROM"
                    f" {self._t} WHERE user_id NOT LIKE '%:%'").fetchone()
        return {"users": int(row[0]), "float": Money(int(row[1]))}

    def user_ids(self, *, scope: str = "") -> list[str]:
        like = (scope + ":%") if scope else None
        with self._lock:
            if like:
                rows = self._conn.execute(
                    f"SELECT user_id FROM {self._t} WHERE user_id LIKE %s", (like,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT user_id FROM {self._t} WHERE user_id NOT LIKE '%:%'"
                ).fetchall()
        return [r[0] for r in rows]

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
