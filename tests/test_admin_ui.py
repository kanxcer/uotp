"""Admin panel actions: add/deduct/ban/broadcast/users via buttons, owner-gated."""

from __future__ import annotations

from decimal import Decimal

import pytest

from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider
from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI

OWNER, USER = "1", "2"


@pytest.fixture
def rig():
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {"telegram": catalog.sticker_price("telegram")}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER))
    ui = MenuUI(router)
    yield router, ui
    ledger.close()


def test_admin_panel_has_money_and_broadcast_buttons(rig):
    router, ui = rig
    panel = ui.admin_panel(OWNER)
    assert panel.ok
    labels = [lbl for row in panel.rows for lbl, _ in row]
    assert any("Add balance" in lbl for lbl in labels)
    assert any("Deduct" in lbl for lbl in labels)
    assert any("Broadcast" in lbl for lbl in labels)
    assert any(lbl.startswith("👥 All users") for lbl in labels)
    assert not any(lbl == "👥 Customers" or lbl.startswith("👥 Customers") for lbl in labels)
    assert not any("Top-ups" in lbl for lbl in labels)
    assert not any("Payment QR" in lbl for lbl in labels)
    assert any("Ban" in lbl for lbl in labels)


def test_admin_actions_owner_gated(rig):
    router, ui = rig
    # Non-owner pressing an admin action gets Owner only.
    r = ui.button(USER, "ax:users")
    assert "Owner only" in r.text


def test_admin_credit_via_prompt(rig):
    router, ui = rig
    # No prior balance; the prompt credits from scratch.
    start = ui.button(OWNER, "ax:credit")
    assert "ADD BALANCE" in start.text
    # The prompt collects a user+amount on the next message.
    done = ui.text(OWNER, "2 500")
    assert done.ok and "Credited" in done.text
    assert router.balance_of(USER) == INR(500)


def test_admin_debit_via_prompt(rig):
    router, ui = rig
    router.credit(USER, INR(200))
    ui.text(OWNER, "2 30")  # consume any pending wizard
    done = ui.button(OWNER, "ax:debit")
    assert "DEDUCT" in done.text
    result = ui.text(OWNER, "2 30")
    assert result.ok and "Debited" in result.text
    assert router.balance_of(USER) == INR(170)


def test_admin_broadcast_via_prompt(rig):
    router, ui = rig
    router.credit(USER, INR(100))
    ui.button(OWNER, "ax:broadcast")
    r = ui.text(OWNER, "Big sale today!")
    assert r.ok and "customer(s)" in r.text


def test_admin_users_button(rig):
    router, ui = rig
    router.credit(USER, INR(100))
    r = ui.button(OWNER, "ax:users")
    assert r.ok and USER in r.text
    datas = [d for row in (r.rows or ()) for _l, d in row]
    assert any(d.startswith("url:tg://user?id=") or d.startswith("url:https://t.me/")
               for d in datas)
    assert f"ax:up:{USER}" in datas
    profile = ui.button(OWNER, f"ax:up:{USER}")
    assert profile.ok and USER in profile.text
    pdata = [d for row in (profile.rows or ()) for _l, d in row]
    assert any("Open Telegram" in l for row in (profile.rows or ()) for l, _ in row)
    assert any(d.startswith("url:") for d in pdata)


def test_admin_bad_input_reprompts_not_crash(rig):
    router, ui = rig
    ui.button(OWNER, "ax:credit")
    r = ui.text(OWNER, "two fifty")
    assert not r.ok and "Format" in r.text


def test_admin_panel_find_user_and_orders_include_smm(tmp_path):
    from uotpbot.wallets import SqliteWallets

    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {"telegram": catalog.sticker_price("telegram")}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer)
    store = SqliteWallets(str(tmp_path / "w.db"))
    router = CommandRouter(
        engine, catalog, pricer, ledger, owner_id=OWNER, allowed_users=(OWNER, USER),
        wallets=store,
    )
    ui = MenuUI(router)
    try:
        panel = ui.admin_panel(OWNER)
        labels = [lbl for row in panel.rows for lbl, _ in row]
        assert any("Find user" in lbl for lbl in labels)
        store.record_order(user_id=USER, slug="telegram", amount=INR(10), success=True,
                           profit=INR(4))
        store.create_smm_order(
            user_id=USER, service_id="7", service_name="IG Followers",
            quantity=50, charge=INR(19), cost=INR(8),
        )
        orders = ui.button(OWNER, "a:o")
        assert orders.ok
        assert "Social boost" in orders.text or "📣" in orders.text
        assert "IG Followers" in orders.text
        assert "telegram" in orders.text
        listed = router.handle(OWNER, "/orders")
        assert listed.ok and "IG Followers" in listed.text
        store.touch_user(USER, username="payee_one")
        users = ui.button(OWNER, "ax:users")
        assert "payee_one" in users.text or "@payee_one" in users.text
        datas = [d for row in (users.rows or ()) for _l, d in row]
        assert "url:https://t.me/payee_one" in datas
        prompt = ui.button(OWNER, "ax:finduser")
        assert "FIND USER" in prompt.text
        found = ui.text(OWNER, USER)
        assert found.ok and USER in found.text
        assert any("Open Telegram" in l for row in (found.rows or ()) for l, _ in row)
    finally:
        store.close()
        ledger.close()
