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
