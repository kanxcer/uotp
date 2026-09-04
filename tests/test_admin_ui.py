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
    assert any("Customers" in lbl for lbl in labels)
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


def test_admin_bad_input_reprompts_not_crash(rig):
    router, ui = rig
    ui.button(OWNER, "ax:credit")
    r = ui.text(OWNER, "two fifty")
    assert not r.ok and "Format" in r.text
