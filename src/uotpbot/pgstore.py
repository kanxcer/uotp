"""PostgreSQL storage backends (Supabase-compatible).

Why this module exists: a sqlite ledger on a host with an ephemeral
filesystem -- Render without a persistent disk, any container platform -- is
silently wiped on every deploy, taking the entire audit trail with it. These
classes are drop-in replacements::

    Ledger          -> PostgresLedger
    SubBotRegistry  -> PostgresRegistry

They share ALL business logic with the sqlite originals: posting shape,
validation, the record_* flows. Only the connection and three primitives are
re-provided (``_execute``, ``_write``, ``_tx``), because money logic that
exists in two places eventually disagrees with itself, and disagreement about
money is the whole problem this codebase exists to prevent.

Requires the ``postgres`` extra:  ``pip install 'uotpbot[postgres]'``.
"""

from __future__ import annotations

import logging
import re
import threading

from .ledger import Ledger, LedgerError
from .whitelabel import SubBotRegistry, WhiteLabelError

__all__ = [
    "PostgresLedger", "PostgresRegistry", "StorageError",
    "ReconnectingConnection",
]

log = logging.getLogger("uotpbot.pgstore")


class StorageError(LedgerError):
    """Raised for Postgres storage setup problems."""


#: A schema name the user gives us lands verbatim in SQL identifiers, so it is
#: restricted to a plain identifier shape -- anything else is a SQL-injection
#: vector with extra steps.
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _import_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise StorageError(
            "PostgreSQL storage needs psycopg: pip install 'uotpbot[postgres]'"
        ) from exc
    return psycopg


def _dsn_keepalive(dsn: str) -> str:
    """Ask libpq to probe idle TCP so a dead Supabase session is noticed.

    Without this, an idle-killed connection hangs until the OS TCP timeout
    (minutes). Every wallet tap then queues behind the lock and the bot
    looks asleep while ``/healthz`` still says the poller is alive.
    """
    if "keepalives=" in (dsn or ""):
        return dsn
    extra = (
        "keepalives=1&keepalives_idle=30&keepalives_interval=10"
        "&keepalives_count=3&connect_timeout=10"
    )
    if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
        return dsn + ("&" if "?" in dsn else "?") + extra
    return (
        dsn.rstrip()
        + " keepalives=1 keepalives_idle=30 keepalives_interval=10"
        + " keepalives_count=3 connect_timeout=10"
    )


def _pg_conn_dead(exc: BaseException) -> bool:
    """True when the session is gone and a reconnect (not a raise) is right.

    Unique/check violations must still raise: those are money-logic errors.
    """
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if any(s in msg for s in (
        "connection is closed",
        "connection already closed",
        "server closed the connection",
        "ssl connection has been closed",
        "terminating connection",
        "could not receive data from server",
        "connection not open",
        "broken pipe",
        "connection reset",
        "admin_shutdown",
        "crash shutdown",
    )):
        return True
    if name in {"operationalerror", "interfaceerror"} and any(
        s in msg for s in ("connection", "ssl", "server", "eof", "timeout")
    ):
        return True
    return False


class ReconnectingConnection:
    """One logical connection that reopens itself after Supabase idle-kills it.

    Ledger, wallets and the SMM store all keep a single long-lived session.
    Live 2026-09-08: that session closed, ``smm list_open`` logged
    ``the connection is closed`` every 30s, Telegram ``getUpdates`` stayed
    200, and every customer tap that hit Postgres looked like a dead bot.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._lock = threading.RLock()
        self._conn = factory()

    def _live(self):
        conn = self._conn
        try:
            closed = bool(getattr(conn, "closed", False))
        except Exception:  # noqa: BLE001
            closed = True
        if conn is None or closed:
            self._reconnect()
        return self._conn

    def _reconnect(self) -> None:
        old = self._conn
        try:
            if old is not None:
                old.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = self._factory()

    def _call(self, method: str, *args, **kwargs):
        with self._lock:
            conn = self._live()
            try:
                return getattr(conn, method)(*args, **kwargs)
            except Exception as exc:
                if not _pg_conn_dead(exc):
                    raise
                log.warning(
                    "postgres connection lost (%s); reconnecting",
                    type(exc).__name__,
                )
                self._reconnect()
                return getattr(self._conn, method)(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self._call("execute", *args, **kwargs)

    def cursor(self, *args, **kwargs):
        inner = self._call("cursor", *args, **kwargs)
        return _ReconnectingCursor(self, inner)

    def transaction(self, *args, **kwargs):
        return self._call("transaction", *args, **kwargs)

    def close(self) -> None:
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def closed(self) -> bool:
        try:
            return bool(getattr(self._conn, "closed", False))
        except Exception:  # noqa: BLE001
            return True


class _ReconnectingCursor:
    """Cursor whose execute/executemany retry once on a dead session."""

    def __init__(self, owner: ReconnectingConnection, cursor) -> None:
        self._owner = owner
        self._cur = cursor

    def _retry(self, method: str, *args, **kwargs):
        try:
            return getattr(self._cur, method)(*args, **kwargs)
        except Exception as exc:
            if not _pg_conn_dead(exc):
                raise
            log.warning(
                "postgres cursor lost (%s); reconnecting",
                type(exc).__name__,
            )
            with self._owner._lock:
                self._owner._reconnect()
                self._cur = self._owner._live().cursor()
                return getattr(self._cur, method)(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self._retry("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._retry("executemany", *args, **kwargs)

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    @property
    def rowcount(self):
        return getattr(self._cur, "rowcount", 0)

    def __enter__(self):
        enter = getattr(self._cur, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *args):
        exit_ = getattr(self._cur, "__exit__", None)
        if callable(exit_):
            return exit_(*args)
        return False


def _connect_pg(dsn: str):
    """One shared connection, serialised by the caller's lock.

    ``prepare_threshold=None`` disables psycopg's automatic server-side
    prepared statements. pgBouncer in *transaction* pooling mode (Supabase's
    6543 pooler) reclaims the server connection between transactions, so a
    prepared statement can simply not exist next time -- intermittent,
    load-dependent query failures of the worst kind. At this codebase's query
    volume preparing is unmeasurable anyway.

    The connection runs with ``autocommit=True``: every read is its own
    atomic statement, and writes opt into an explicit ``transaction()`` block
    via ``_tx()`` -- same shape as the sqlite backend's ``with conn:``.

    The object returned is a :class:`ReconnectingConnection`: Supabase idle
    timeouts close the session, and the next query opens a new one instead
    of failing every customer tap until a redeploy.
    """
    psycopg = _import_psycopg()

    def factory():
        return psycopg.connect(
            _dsn_keepalive(dsn),
            autocommit=True,
            prepare_threshold=None,
        )

    return ReconnectingConnection(factory)


LEDGER_DDL = """\
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {t} (
    id         BIGSERIAL PRIMARY KEY,
    ts         TEXT   NOT NULL,
    ref        TEXT   NOT NULL,
    account    TEXT   NOT NULL,
    debit_p    BIGINT NOT NULL DEFAULT 0,
    credit_p   BIGINT NOT NULL DEFAULT 0,
    memo       TEXT   NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_postings_ref     ON {t} (ref);
CREATE INDEX IF NOT EXISTS idx_postings_account ON {t} (account);
CREATE INDEX IF NOT EXISTS idx_postings_ts      ON {t} (ts);
"""

REGISTRY_DDL = """\
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {t} (
    id             TEXT   PRIMARY KEY,
    owner_id       TEXT   NOT NULL,
    bot_token      TEXT   NOT NULL,
    mode           TEXT   NOT NULL,
    provider_key   TEXT   NOT NULL DEFAULT '',
    provider_url   TEXT   NOT NULL DEFAULT '',
    fee_rate       TEXT   NOT NULL,
    fee_fixed_p    BIGINT NOT NULL,
    disclosed_at   TEXT   NOT NULL,
    disclosure     TEXT   NOT NULL,
    created_at     TEXT   NOT NULL,
    active         BIGINT NOT NULL DEFAULT 1,
    reseller_rate  TEXT   NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_subbots_owner ON {t} (owner_id);
"""


def _bootstrap(conn, ddl: str) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            for stmt in ddl.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)


class PostgresLedger(Ledger):
    """The double-entry ledger on Postgres instead of SQLite.

    Identical posting semantics; the schema lives in a dedicated schema
    (default ``uotp``) so it stays cleanly separated from anything else in the
    project and from anything PostgREST might expose on ``public``.
    """

    def __init__(self, dsn: str, *, schema: str = "uotp") -> None:
        if not _IDENT.match(schema):
            raise StorageError(f"unsafe schema name {schema!r}")
        self._table = f'"{schema}"."postings"'
        self._ph = "%s"
        self._lock = threading.RLock()
        self._conn = _connect_pg(dsn)
        with self._lock:
            _bootstrap(self._conn, LEDGER_DDL.format(s=f'"{schema}"', t=self._table))

    def _tx(self):
        return self._conn.transaction()

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(self._q(sql), params)
                return cur.fetchall()

    def _write(self, rows: list[tuple]) -> None:
        # The whole batch lands or none of it does -- the same atomicity the
        # sqlite backend's `with conn:` gives, and the property
        # post_many's callers rely on.
        with self._lock, self._tx():
            with self._conn.cursor() as cur:
                cur.executemany(self._q(self._INSERT_SQL), rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def drop_schema(self, schema: str) -> None:
        """Drop a schema entirely. For tests, never for production paths."""
        if not _IDENT.match(schema):
            raise StorageError(f"unsafe schema name {schema!r}")
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class PostgresRegistry(SubBotRegistry):
    """The white-label sub-bot registry on Postgres instead of SQLite.

    Lives in the same schema as the ledger by default: the registry and the
    ledger must survive or vanish TOGETHER, because a registry that outlives
    the ledger keeps charging owners fees nobody can account for.
    """

    def __init__(self, dsn: str, *, schema: str = "uotp", secret_key: str = "") -> None:
        if not _IDENT.match(schema):
            raise WhiteLabelError(f"unsafe schema name {schema!r}")
        self._table = f'"{schema}"."subbots"'
        self._ph = "%s"
        self._lock = threading.RLock()
        from .whitelabel import _fernet_for
        self._fernet = _fernet_for(secret_key) if secret_key else None
        self._conn = _connect_pg(dsn)
        with self._lock:
            _bootstrap(self._conn, REGISTRY_DDL.format(s=f'"{schema}"', t=self._table))
            try:
                self._conn.execute(
                    f"ALTER TABLE {self._table} "
                    "ADD COLUMN IF NOT EXISTS reseller_rate TEXT NOT NULL DEFAULT '0'"
                )
            except Exception:  # noqa: BLE001 - column already there
                pass

    def _tx(self):
        return self._conn.transaction()

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(self._q(sql), params)
                return cur.fetchall()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._tx():
            with self._conn.cursor() as cur:
                cur.execute(self._q(sql), params)
                return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def drop_schema(self, schema: str) -> None:
        if not _IDENT.match(schema):
            raise WhiteLabelError(f"unsafe schema name {schema!r}")
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# Convenience factories for callers that want them.
def make_postgres_ledger(dsn: str, *, schema: str = "uotp") -> PostgresLedger:
    return PostgresLedger(dsn, schema=schema)


def make_postgres_registry(dsn: str, *, schema: str = "uotp") -> PostgresRegistry:
    return PostgresRegistry(dsn, schema=schema)
