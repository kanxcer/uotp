"""Customer wallet persistence and its router wiring.

Money-grade behaviour only: survive "restarts" (re-open the same file),
never go negative, keep sub-bot balances under a namespace so a customer of
bot A never spends on bot B.
"""

from __future__ import annotations

import time

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider
from uotpbot.wallets import ScopedWallets, SqliteWallets, WalletError

OWNER = "111"
USER = "222"


def _router(wallets=None):
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                __import__("decimal").Decimal("0.94")),
    })
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"telegram": catalog.sticker_price("telegram")},
                            balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(otp_timeout_seconds=0.5, poll_interval=0.01))
    return CommandRouter(engine, catalog, pricer, ledger,
                         owner_id=OWNER, allowed_users=(OWNER, USER),
                         wallets=wallets), provider


def test_sqlite_wallets_survive_reopen(tmp_path):
    db = str(tmp_path / "w.db")
    store = SqliteWallets(db)
    store.adjust(USER, INR(50))
    store.close()
    reopened = SqliteWallets(db)
    assert reopened.balance(USER).paise == INR(50).paise
    reopened.close()


def test_adjust_is_read_modify_write_and_never_negative(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(50))
    assert store.adjust(USER, Money(-1000)).paise == INR(40).paise
    with pytest.raises(WalletError):
        store.set_balance(USER, Money(-1))
    store.close()


def test_router_credits_and_purchase_flow_through_the_store(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    router, provider = _router(wallets=store)
    router.credit(USER, INR(100))
    assert store.balance(USER).paise == INR(100).paise
    reply = router.handle(USER, "/buy telegram")
    assert "OTP" in reply.text
    spent = INR(100) - store.balance(USER)
    assert spent.paise > 0
    store.close()


def test_scoped_wallets_isolate_bots_sharing_one_store(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    bot_a = ScopedWallets(store, "bta1")
    bot_b = ScopedWallets(store, "btb2")
    bot_a.adjust(USER, INR(50))
    assert bot_b.balance(USER).paise == 0  # bot A's money stays bot A's
    assert bot_a.balance(USER).paise == INR(50).paise
    store.close()


def test_wallets_and_balances_are_mutually_exclusive(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    router, _ = _router(wallets=None)
    with pytest.raises(ValueError):
        CommandRouter(router.engine, router.catalog, router.pricer, Ledger(),
                      balances={}, wallets=store)
    store.close()


def test_ui_balance_reflects_the_store(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    router, _ = _router(wallets=store)
    ui = MenuUI(router)
    store.adjust(USER, INR(42))
    assert "42" in ui.text(USER, "/start").text
    store.close()


def test_active_numbers_persist_and_expire(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    now = time.time()
    store.record_active(user_id=USER, slug="telegram", phone="+919999999999",
                        provider_order_id="p1", token="tok1", gross=INR(15),
                        valid_until=now + 600)
    live = store.active_numbers(user_id=USER, now=now + 100)
    assert len(live) == 1
    assert live[0].phone == "+919999999999"
    assert live[0].token == "tok1"
    assert live[0].seconds_left > 0
    # A number past its expiry no longer shows.
    assert store.active_numbers(user_id=USER, now=now + 700) == []
    # Delivering records the OTP; it stays live until finished.
    store.update_active("p1", otp="123456")
    assert store.active_numbers(user_id=USER, now=now)[0].has_otp
    store.finish_active("p1")
    assert store.active_numbers(user_id=USER, now=now) == []
    store.close()


def test_scoped_active_numbers_are_isolated(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    scoped = ScopedWallets(store, scope="botA")
    scoped.record_active(user_id=USER, slug="telegram", phone="+911111",
                         provider_order_id="a", token="t", gross=INR(10),
                         valid_until=time.time() + 500)
    assert len(store.active_numbers(scope="botA", user_id=USER)) == 1
    assert len(store.active_numbers(scope="botB", user_id=USER)) == 0
    store.close()


def test_scoped_get_active_refuses_a_foreign_token(tmp_path):
    """A wait-token handed to the wrong bot must yield nothing, not someone
    else's live number (user id, phone, OTP) -- a cross-tenant data leak."""
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.record_active(user_id="999", slug="telegram", phone="+91111",
                        provider_order_id="pA", token="tok-X", gross=INR(100),
                        valid_until=time.time() + 600, scope="botA")
    botB = ScopedWallets(store, scope="botB")
    assert botB.get_active("tok-X") is None
    # And botA still sees its own number.
    assert ScopedWallets(store, scope="botA").get_active("tok-X") is not None
    store.close()


def test_scoped_update_and_finish_cannot_touch_foreign_rows(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.record_active(user_id="999", slug="telegram", phone="+91111",
                        provider_order_id="pA", token="tok-X", gross=INR(100),
                        valid_until=time.time() + 600, scope="botA")
    botB = ScopedWallets(store, scope="botB")
    # Writing botA's OTP or deleting botA's number from botB must be no-ops.
    botB.update_active("pA", otp="123456")
    assert store.get_active("tok-X").otp == ""
    botB.finish_active("pA")
    assert store.get_active("tok-X") is not None
    # The owning scope still controls its own row.
    botA = ScopedWallets(store, scope="botA")
    botA.update_active("pA", otp="654321")
    assert store.get_active("tok-X").otp == "654321"
    botA.finish_active("pA")
    assert store.get_active("tok-X") is None
    store.close()


def test_legacy_db_gains_new_columns_on_open(tmp_path):
    """A database created by an older release (no ledger_done / scope columns)
    must be migrated on open, not crash or silently lose the new features."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE refund_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " scope TEXT NOT NULL DEFAULT '', refund_id TEXT NOT NULL,"
        " user_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,"
        " order_token TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
        " last_error TEXT NOT NULL DEFAULT '', created_ts REAL NOT NULL, done_ts REAL,"
        " UNIQUE(scope, order_token))")
    conn.execute(
        "CREATE TABLE activenumbers (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT NOT NULL, slug TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',"
        " provider_order_id TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '',"
        " gross_paise INTEGER NOT NULL, otp TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,"
        " valid_until REAL NOT NULL)")
    conn.execute(
        "INSERT INTO activenumbers(user_id, slug, phone, provider_order_id, token,"
        " gross_paise, ts, valid_until) VALUES ('5','s','9','p9','tokY',10000,0,9e18)")
    conn.commit()
    conn.close()

    store = SqliteWallets(path)
    # New features work on the migrated table.
    rid, inserted = store.write_refund(user_id="u", amount=INR(1), order_token="tL")
    assert inserted and store.get_refund("tL").ledger_done is False
    store.mark_ledger_done("tL")
    assert store.get_refund("tL").ledger_done is True
    # Pre-existing rows survive with sane defaults.
    active = store.get_active("tokY")
    assert active is not None and active.scope == ""
    store.close()


def test_orders_rich_history_fields_and_get_order(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.record_order(user_id=USER, slug="telegram", amount=INR(18), phone="+911111",
                       otp="123456", success=True, profit=INR(5), status="delivered",
                       reason="delivered after 2 attempts", refunded=INR(0),
                       spent=INR(10), balance_after=INR(82))
    store.record_order(user_id=USER, slug="binance", amount=INR(22), success=False,
                       status="refunded", reason="no OTP arrived", refunded=INR(22),
                       spent=INR(10), balance_after=INR(60))
    rows = store.recent_orders(scope="", user_id=USER, limit=10)
    assert len(rows) == 2
    top = rows[0]
    assert top.status == "refunded"
    assert top.refunded.paise == 2200
    assert top.balance_after.paise == 6000
    # get_order retrieves the exact one and enforces ownership.
    delivered = store.get_order(rows[1].id, scope="", user_id=USER)
    assert delivered is not None and delivered.status == "delivered"
    assert delivered.spent.paise == 1000
    assert store.get_order(rows[1].id, scope="", user_id="someone-else") is None
    store.close()


def test_user_directory_unions_seen_orders_topups_and_wallets(tmp_path):
    """All users must list everyone who used the bot, not only wallet rows.

    A /start with ₹0 never wrote a wallets row, which is why the owner panel
    used to show 2 customers while many were live.
    """
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.touch_user("aaa")  # /start only, ₹0
    store.record_order(user_id="bbb", slug="telegram", amount=INR(10), success=True)
    store.adjust("ccc", INR(50))
    store.create_topup("ddd", INR(20))
    ids = set(store.user_ids())
    assert ids >= {"aaa", "bbb", "ccc", "ddd"}
    fs = store.float_stats()
    assert fs["users"] == len(ids)
    assert fs["float"] == INR(50)  # only ccc holds money
    # Empty / whitespace ids are ignored.
    store.touch_user("")
    store.touch_user("   ")
    assert "" not in store.user_ids()
    store.close()


def test_scoped_user_ids_are_isolated_and_unprefixed(tmp_path):
    """Sub-bot customer lists must not leak across bots, and must return the
    bare Telegram id (not ``scope:uid``) so balance_of() prefixes once."""
    store = SqliteWallets(str(tmp_path / "w.db"))
    bot_a = ScopedWallets(store, "bta1")
    bot_b = ScopedWallets(store, "btb2")
    bot_a.touch_user("111")
    bot_a.adjust("111", INR(10))
    bot_b.touch_user("222")
    assert bot_a.user_ids() == ["111"]
    assert bot_b.user_ids() == ["222"]
    assert store.user_ids(scope="bta1") == ["111"]
    # Platform (empty scope) must not treat scoped wallet keys as customers.
    assert "bta1:111" not in store.user_ids()
    assert "111" not in store.user_ids()
    store.close()


def test_list_users_paginates(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    for i in range(45):
        store.touch_user(f"{i:03d}")
    page, total = store.list_users(limit=40, offset=0)
    assert total == 45
    assert len(page) == 40
    page2, total2 = store.list_users(limit=40, offset=40)
    assert total2 == 45
    assert len(page2) == 5
    assert {u for u, _ in page}.isdisjoint({u for u, _ in page2})
    store.close()


def test_start_only_user_appears_in_admin_list_and_broadcast(tmp_path):
    """Tapping /start (no payment) must show up in All users, Total users,
    and the broadcast audience."""
    store = SqliteWallets(str(tmp_path / "w.db"))
    router, _ = _router(wallets=store)
    ui = MenuUI(router)
    ghost = "555"
    ui.text(ghost, "/start")
    assert ghost in store.user_ids()
    fs = store.float_stats()
    assert fs["users"] >= 1
    listed = router.handle(OWNER, "/users")
    assert listed.ok and ghost in listed.text
    assert "₹0.00" in listed.text
    panel = ui.admin_panel(OWNER)
    assert f"Total users: {fs['users']}" in panel.text
    blast = router.handle(OWNER, "/broadcast hi there")
    assert any(t == ghost for t, _ in blast.notify)
    store.close()


def test_cmd_users_paginates_past_the_old_30_cap(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    router, _ = _router(wallets=store)
    ui = MenuUI(router)
    for i in range(45):
        store.adjust(str(10_000 + i), INR(1))
    first = router.handle(OWNER, "/users")
    assert first.ok
    assert "page 1/" in first.text
    assert "Customers (45)" in first.text
    datas = [d for row in first.rows for _l, d in row]
    assert "ax:users:1" in datas
    # All 45 must be reachable; the first page is 40, not a silent [:30].
    # Each id is wrapped in a backtick pair (`uid`).
    assert first.text.count("`") == 80
    second = router.handle(OWNER, "/users 1")
    assert second.ok and "page 2/" in second.text
    assert second.text.count("`") == 10
    via_btn = ui.button(OWNER, "ax:users:1")
    assert via_btn.ok and "page 2/" in via_btn.text
    store.close()
