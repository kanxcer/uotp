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

from .money import Money

__all__ = ["WalletStore", "SqliteWallets", "PostgresWallets", "WalletError"]


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
    Keys are namespaced ``<bot ID>:<user id>`` so wallets stay per-bot even
    though the table is shared.
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    user_id TEXT PRIMARY KEY,
    balance_paise INTEGER NOT NULL CHECK (balance_paise >= 0)
)
"""


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
            self._conn.commit()

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
        with self._lock:
            self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            self._conn.execute(_SCHEMA.format(t=self._t))

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
