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

import re
import threading

from .ledger import Ledger, LedgerError
from .whitelabel import SubBotRegistry, WhiteLabelError

__all__ = ["PostgresLedger", "PostgresRegistry", "StorageError"]


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
    """
    psycopg = _import_psycopg()
    return psycopg.connect(dsn, autocommit=True, prepare_threshold=None)


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
