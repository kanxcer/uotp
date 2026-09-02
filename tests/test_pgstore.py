"""Postgres backend tests.

Two layers:

* Fake-connection tests (always run): verify the SQL the backends emit --
  placeholder translation, schema-qualified tables, atomic batches -- without
  needing a database.
* Live conformance tests (opt-in via UOTP_TEST_PG_DSN): run the same battery
  against a real Postgres (Supabase here), in a throwaway schema that is
  dropped afterwards, proving the backends actually work against the same
  engine production will use.
"""

from __future__ import annotations

import os
import secrets

import pytest

from uotpbot.ledger import (
    CASH, COGS, GST, SALES, WALLET, Ledger,
)
from uotpbot.money import INR, Money
from uotpbot.pgstore import PostgresLedger, PostgresRegistry, StorageError
from uotpbot.whitelabel import (
    DEFAULT_PLATFORM_FEE, SubBot, SubBotMode, SubBotRegistry, WhiteLabelError,
)


# --------------------------------------------------------------------------
# fake psycopg connection
# --------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.conn.sql.append(sql)
        self.conn.params.append(params)
        self.conn.in_tx.append(self.conn.tx_open)
        # SELECTs return a single numeric row so balance()/count() work.
        self._rows = [[0, 0]] if "SUM(" in sql else [[0]] if "COUNT" in sql else []
        self.rowcount = 1
        return self

    def executemany(self, sql, rows):
        self.conn.sql.append(sql)
        self.conn.in_tx.append(self.conn.tx_open)
        self.conn.executemany_calls.append(list(rows))

    def fetchall(self):
        return self._rows


class FakeTx:
    def __init__(self, conn) -> None:
        self.conn = conn

    def __enter__(self):
        self.conn.tx_open = True
        return self.conn

    def __exit__(self, *a):
        self.conn.tx_open = False
        return False


class FakeConn:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[tuple] = []
        self.in_tx: list[bool] = []
        self.executemany_calls: list[list[tuple]] = []
        self.tx_open = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTx(self)

    def close(self):
        self.closed = True


@pytest.fixture
def pg(monkeypatch):
    """Route pgstore's connections at a FakeConn and return the ledger."""
    import uotpbot.pgstore as pgstore

    conn = FakeConn()
    monkeypatch.setattr(pgstore, "_connect_pg", lambda dsn: conn)
    return conn


# --------------------------------------------------------------------------
# fake-connection unit tests
# --------------------------------------------------------------------------
def test_ledger_uses_schema_qualified_table_and_percent_placeholders(pg):
    ledger = PostgresLedger("postgres://fake", schema="uotp")
    ddl = "\n".join(pg.sql)
    assert 'CREATE SCHEMA IF NOT EXISTS "uotp"' in ddl
    assert '"uotp"."postings"' in ddl

    ledger.post(CASH, SALES, INR(10), ref="r1", memo="m")
    insert = pg.sql[-1]
    assert '"uotp"."postings"' in insert
    assert "%s" in insert and "?" not in insert
    # one posting expands to two rows (debit + credit), batched atomically
    assert len(pg.executemany_calls[-1]) == 2
    assert pg.tx_open is False  # transaction closed afterwards


def test_ledger_batch_is_inside_a_transaction(pg):
    ledger = PostgresLedger("postgres://fake", schema="uotp")
    ledger.post(CASH, SALES, INR(10), ref="r1")
    # the INSERT ran with the transaction open
    insert_idx = max(i for i, s in enumerate(pg.sql) if "INSERT" in s)
    assert pg.in_tx[insert_idx] is True


def test_registry_uses_schema_qualified_table(pg):
    registry = PostgresRegistry("postgres://fake", schema="uotp")
    assert any('"uotp"."subbots"' in s for s in pg.sql)
    bot = SubBot(owner_id="9", bot_token="11:x" * 12, mode=SubBotMode.PLATFORM_API,
                 fee=DEFAULT_PLATFORM_FEE)
    registry.add(bot)
    insert = [s for s in pg.sql if "INSERT" in s][-1]
    assert '"uotp"."subbots"' in insert and "%s" in insert


def test_unsafe_schema_names_are_rejected(pg):
    with pytest.raises(StorageError):
        PostgresLedger("postgres://fake", schema='public; DROP TABLE x')
    with pytest.raises(WhiteLabelError):
        PostgresRegistry("postgres://fake", schema="a-b")


# --------------------------------------------------------------------------
# conformance battery: run the same behaviour on both backends
# --------------------------------------------------------------------------
def _exercise_ledger(ledger: Ledger) -> None:
    """The app-visible ledger surface, end to end."""
    ledger.record_topup(INR(1000), INR(1150), Money.zero(), ref="top1")
    ledger.record_number_purchase(INR(10), ref="ord1", memo="telegram")
    assert ledger.balance(WALLET) == INR(1000) + INR(150) - INR(10)
    assert ledger.balance(COGS) == INR(10)

    ledger.record_sale(INR(15), INR(0.36), Money.zero(), ref="ord2")
    # split sale: platform fee lands in its own account
    ledger.record_sale_split(INR(100), INR(2), Money.zero(), INR(5), ref="ord3")
    assert ledger.platform_revenue() == INR(5)
    assert ledger.balance(SALES) == INR(15) - Money(0) + (INR(100) - INR(5))

    ledger.record_customer_refund(INR(15), ref="ord2")
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.platform_fee == INR(5)
    assert ledger.balance(GST) == Money.zero()
    # audit trail for one order is intact
    refs = {p.ref for p in ledger.ledger_for("ord1")}
    assert refs == {"ord1"}
    assert ledger.history(5)


def _exercise_registry(registry: SubBotRegistry) -> None:
    bot = SubBot(owner_id="42", bot_token="55:" + "a" * 35,
                 mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE)
    registry.add(bot)
    assert registry.find(bot.id).owner_id == "42"
    assert registry.find_by_token(bot.bot_token) is not None
    assert registry.count() == 1
    own = SubBot(owner_id="42", bot_token="66:" + "b" * 35,
                 mode=SubBotMode.OWN_API, provider_key="k", fee=DEFAULT_PLATFORM_FEE)
    registry.add(own)
    assert registry.count() == 2
    assert len(registry.for_owner("42")) == 2
    registry.set_active(own.id, False)
    assert len(registry.all_active()) == 1
    # duplicate tokens are rejected
    with pytest.raises(WhiteLabelError):
        registry.add(SubBot(owner_id="1", bot_token=bot.bot_token,
                            mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    assert registry.delete(own.id) is True
    assert registry.delete(own.id) is False


def test_conformance_sqlite(tmp_path):
    _exercise_ledger(Ledger(tmp_path / "l.db"))
    _exercise_registry(SubBotRegistry(str(tmp_path / "r.db")))


PG_DSN = os.environ.get("UOTP_TEST_PG_DSN", "")
SCHEMA = "uotp_itest_" + secrets.token_hex(3)


@pytest.mark.skipif(not PG_DSN, reason="set UOTP_TEST_PG_DSN to run against real Postgres")
def test_conformance_postgres():
    ledger = PostgresLedger(PG_DSN, schema=SCHEMA)
    try:
        _exercise_ledger(ledger)
        registry = PostgresRegistry(PG_DSN, schema=SCHEMA)
        try:
            _exercise_registry(registry)
        finally:
            registry.close()
        ledger.verify()
    finally:
        ledger.drop_schema(SCHEMA)
        ledger.close()
