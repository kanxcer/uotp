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
from dataclasses import dataclass
from typing import Optional

from .money import Money

__all__ = [
    "WalletStore", "SqliteWallets", "PostgresWallets", "ScopedWallets",
    "WalletError", "Topup", "OrderRow",
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
                     success=False, profit=Money(0)):
        self._inner.record_order(user_id=user_id, slug=slug, amount=amount,
                                 phone=phone, otp=otp, success=success,
                                 profit=profit, scope=self._scope)

    def recent_orders(self, *, user_id: str = "", limit: int = 10):
        return self._inner.recent_orders(scope=self._scope, user_id=user_id, limit=limit)

    def float_stats(self):
        return self._inner.float_stats(scope=self._scope)


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
    ts REAL NOT NULL
)
"""

_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

    @property
    def profit_ratio(self) -> Optional[float]:
        if self.gross.paise <= 0:
            return None
        return round(self.profit.paise / self.gross.paise, 3)


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
            self._conn.commit()

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
                     profit: Money = Money(0), scope: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO orders(scope, user_id, slug, gross_paise, phone, otp,"
                " success, profit_paise, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope, user_id, slug, amount.paise, phone, otp,
                 1 if success else 0, profit.paise, time.time()),
            )

    def recent_orders(self, *, scope: str = "", user_id: str = "", limit: int = 10
                      ) -> list[OrderRow]:
        sql = ("SELECT id, user_id, slug, gross_paise, phone, otp, success,"
               " profit_paise, ts FROM orders WHERE scope = ?")
        params: list = [scope]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [OrderRow(r[0], r[1], r[2], Money(r[3]), r[4], r[5], bool(r[6]),
                         Money(r[7]), r[8]) for r in rows]

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
                     profit: Money = Money(0), scope: str = "") -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._to}(scope, user_id, slug, gross_paise, phone, otp,"
                f" success, profit_paise, ts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (scope, user_id, slug, amount.paise, phone, otp,
                 success, profit.paise, time.time()),
            )

    def recent_orders(self, *, scope: str = "", user_id: str = "", limit: int = 10
                      ) -> list[OrderRow]:
        sql = (f"SELECT id, user_id, slug, gross_paise, phone, otp, success,"
               f" profit_paise, ts FROM {self._to} WHERE scope = %s")
        params: list = [scope]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [OrderRow(r[0], r[1], r[2], Money(r[3]), r[4], r[5], bool(r[6]),
                         Money(r[7]), r[8]) for r in rows]

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
        # Single atomic statement: no lost updates even if two purchases land
        # at the same moment. The CHECK constraint itself rejects an overdraft
        # (a debit that would take the balance negative fails loudly as an
        # IntegrityError instead of quietly flooring at zero).
        with self._lock:
            row = self._conn.execute(
                f"INSERT INTO {self._t}(user_id, balance_paise) VALUES(%s, %s) "
                "ON CONFLICT(user_id) DO UPDATE SET balance_paise = "
                f"{self._t}.balance_paise + %s "
                "RETURNING balance_paise",
                (user_id, delta.paise, delta.paise),
            ).fetchone()
        new = Money(row[0])
        if new.is_negative:  # pragma: no cover - the CHECK got there first
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
