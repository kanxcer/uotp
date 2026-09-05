"""Clone bots: extra on our selling price, 5% of that extra, platform UPI."""

from __future__ import annotations

from decimal import Decimal

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.createbot import CB_MARGIN_SUGGESTED, CB_OWN_API, CreateBotFlow
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer, ResellerPricer, Strategy
from uotpbot.provider.mock import MockOutcome, MockProvider
from uotpbot.reseller import (
    clone_price,
    credit_earnings,
    earnings_balance,
    parse_percent,
    pending_withdrawals,
    request_withdraw,
    reseller_split,
    settle_withdraw,
)
from uotpbot.wallets import ScopedWallets, SqliteWallets
from uotpbot.whitelabel import (
    DEFAULT_PLATFORM_FEE,
    SubBot,
    SubBotMode,
    SubBotRegistry,
)

GOOD_TOKEN = "123456789:AAF1234567890abcdefghijklmnopqrstuvw"


def test_blinkit_example_14_50_at_38_percent():
    ours = INR("14.50")
    theirs = clone_price(ours, Decimal("0.38"))
    assert theirs == INR("20.01")
    split = reseller_split(theirs, Decimal("0.38"), Decimal("0.05"))
    assert split.wholesale == INR("14.50")
    assert split.extra == INR("5.51")
    assert split.platform_cut == INR("0.28")
    assert split.owner_share == INR("5.23")
    assert split.extra == split.platform_cut + split.owner_share


def test_parse_percent_accepts_38_and_fraction():
    assert parse_percent("38") == Decimal("0.38")
    assert parse_percent("38%") == Decimal("0.38")
    assert parse_percent("0.38") == Decimal("0.38")
    assert parse_percent("0") is None
    assert parse_percent("nope") is None


def test_earnings_credit_and_withdraw_round_trip():
    store = SqliteWallets(":memory:")
    credit_earnings(store, "owner1", INR("5.23"))
    assert earnings_balance(store, "owner1") == INR("5.23")
    wd = request_withdraw(store, "owner1", INR("5.23"), "me@okaxis")
    assert earnings_balance(store, "owner1").is_zero
    pending = pending_withdrawals(store)
    assert len(pending) == 1 and pending[0]["id"] == wd
    settle_withdraw(store, wd, paid=False)
    assert earnings_balance(store, "owner1") == INR("5.23")


def test_createbot_never_offers_own_api():
    flow = CreateBotFlow(SubBotRegistry(), token_verifier=lambda t, **k: (True, "x"))
    flow.start("u1")
    asked = flow.on_text("u1", GOOD_TOKEN)
    labels = " ".join(
        [l for l, _ in (asked.buttons or [])]
        + [l for row in (asked.rows or []) for l, _ in row]
    ).lower()
    assert "api" not in labels
    stale = flow.on_button("u1", CB_OWN_API)
    assert "not available" in stale.reply.lower()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    shown = flow.on_button("u1", CB_MARGIN_SUGGESTED)
    assert "38%" in shown.reply
    assert "own YC OTP API" in shown.reply


def _clone_rig():
    catalog = Catalog({
        "blinkit": ServiceCost(
            "blinkit", "Blinkit", "food", INR(10),
            Decimal("0.94"), Decimal("0.04"), Decimal("0.95"),
        ),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    inner = Pricer(catalog, strategy=Strategy.EXACT_MARKUP, markup=Decimal("0.45"))
    pricer = ResellerPricer(inner, Decimal("0.38"))
    provider = MockProvider({"blinkit": INR(10)}, balance=INR(5000), seed=3)
    engine = BotEngine(
        catalog, provider, ledger, pricer,
        config=EngineConfig(retry_cap=1, otp_timeout_seconds=0.4, poll_interval=0.01),
    )
    store = SqliteWallets(":memory:")
    router = CommandRouter(
        engine, catalog, pricer, ledger, owner_id="clone-owner",
        wallets=ScopedWallets(store, "clone1"),
        is_clone=True, reseller_rate=Decimal("0.38"),
        clone_bot_id="clone1", platform_wallets=store,
        platform_owner_id="platform-owner", margin_fee_rate=Decimal("0.05"),
        clone_bot_token="123:AAFclonexxxxxxxxxxxxxxxxxxxxxxxxxxx",
    )
    ui = MenuUI(router, famgateway_api_key="fg-key-xxxxxx")
    return router, ui, store, provider, ledger, pricer


def test_reseller_pricer_multiplies_our_shelf_price():
    _, _, _, _, ledger, pricer = _clone_rig()
    try:
        cost = pricer.catalog.get("blinkit")
        # inner exact markup: 10 * 1.45 = 14.50; 38% extra → 20.01
        assert pricer.price(cost).gross_price == INR("20.01")
    finally:
        ledger.close()


def test_otp_success_credits_95_percent_of_extra():
    router, _, store, provider, ledger, _ = _clone_rig()
    try:
        router.credit("cust", INR(50))
        provider.force_next(MockOutcome("success", otp="111111"))
        reply = router.purchase("cust", "blinkit")
        assert reply.ok
        assert earnings_balance(store, "clone-owner") == INR("5.23")
    finally:
        ledger.close()


def test_failed_order_does_not_credit_earnings():
    router, _, store, provider, ledger, _ = _clone_rig()
    try:
        router.credit("cust", INR(50))
        provider.force_next(MockOutcome("unavailable"))
        reply = router.purchase("cust", "blinkit")
        assert not reply.ok
        assert earnings_balance(store, "clone-owner").is_zero
    finally:
        ledger.close()


def test_clone_admin_hides_platform_tools():
    router, ui, _, _, ledger, _ = _clone_rig()
    try:
        panel = ui.admin_panel("clone-owner")
        blob = panel.text + " ".join(
            lbl for row in (panel.rows or ()) for lbl, _ in row
        )
        assert "Withdraw" in blob
        assert "Add balance" not in blob
        assert "Deduct" not in blob
        assert "Clone-bot" not in blob
        assert "Top-ups" not in blob
        assert "Payment QR" not in blob
        assert "FamGateway" not in blob
        assert "Users may use bot" not in blob
        datas = {d for row in (panel.rows or ()) for _l, d in row}
        assert "a:on" not in datas
        assert "a:cb" not in datas
        blocked = ui.button("clone-owner", "ax:credit")
        assert not blocked.ok
        blocked2 = ui.button("clone-owner", "a:cb")
        assert not blocked2.ok
        blocked3 = ui.button("clone-owner", "ax:fg")
        assert not blocked3.ok
        blocked4 = ui.button("clone-owner", "a:on")
        assert not blocked4.ok
    finally:
        ledger.close()


def test_clone_topup_without_platform_key_refuses_screenshot():
    catalog = Catalog({
        "x": ServiceCost("x", "X", "other", INR(10), Decimal("1"), Decimal("0"), Decimal("1")),
    }, (WalletPack("P", INR(100), INR(100)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    engine = BotEngine(catalog, MockProvider({}, balance=INR(0)), ledger, pricer)
    store = SqliteWallets(":memory:")
    router = CommandRouter(
        engine, catalog, pricer, ledger, owner_id="clone-owner",
        wallets=ScopedWallets(store, "c1"),
        is_clone=True, reseller_rate=Decimal("0.38"),
        clone_bot_id="c1", platform_wallets=store,
    )
    ui = MenuUI(router)  # no FamGateway key
    try:
        card = ui.topup_card("cust")
        assert not card.ok
        assert "platform" in card.text.lower()
    finally:
        ledger.close()


def test_fg_order_is_written_unscoped_with_clone_scope():
    """Clone payments must be visible to the platform webhook."""
    router, ui, store, _, ledger, _ = _clone_rig()
    try:
        class _Order:
            order_id = "ord-clone-1"
            payable_amount = Decimal("20")
            qr_url = "https://example/qr.png"

        class _Client:
            def create_order(self, amount, webhook_url=""):
                return _Order()

        ui._fg_client = _Client()
        reply = ui._begin_fg_topup("cust99", INR(20))
        final = reply.deferred("cust99")
        assert "ord-clone-1" in final.text
        assert store.kv_get("fg_order:ord-clone-1") == "cust99"
        assert store.kv_get("fg_scope:ord-clone-1") == "clone1"
        assert store.kv_get("fg_token:ord-clone-1")
        # Scoped view must NOT have been the only write.
        scoped = ScopedWallets(store, "clone1")
        assert scoped.kv_get("fg_order:ord-clone-1") in (None, "")
    finally:
        ledger.close()

def _labels(reply):
    out = []
    for row in (reply.rows or ()):
        out.extend(row)
    if reply.buttons:
        out.extend(reply.buttons)
    return out


def test_clone_run_your_own_bot_follows_platform_switch():
    """Main Clone-bot ON also shows the button on clones; clones cannot toggle it."""
    catalog = Catalog({
        "blinkit": ServiceCost(
            "blinkit", "Blinkit", "food", INR(10),
            Decimal("0.94"), Decimal("0.04"), Decimal("0.95"),
        ),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"blinkit": INR(10)}, balance=INR(5000), seed=3)
    engine = BotEngine(catalog, provider, ledger, pricer)
    store = SqliteWallets(":memory:")
    registry = SubBotRegistry()
    router = CommandRouter(
        engine, catalog, pricer, ledger, owner_id="clone-owner",
        wallets=ScopedWallets(store, "clone1"),
        is_clone=True, reseller_rate=Decimal("0.38"),
        clone_bot_id="clone1", platform_wallets=store,
        platform_owner_id="platform-owner", margin_fee_rate=Decimal("0.05"),
        subbots=registry, platform_fee=DEFAULT_PLATFORM_FEE,
    )
    ui = MenuUI(router)
    try:
        assert ui.createbot_enabled() is False
        assert ("🤖 Run your own bot", "cb") not in _labels(ui.main_menu("cust"))
        store.kv_set("feature_createbot", "1")
        assert ui.createbot_enabled() is True
        assert ("🤖 Run your own bot", "cb") in _labels(ui.main_menu("cust"))
        landing = ui.button("cust", "cb")
        labs = " ".join(l for l, _ in _labels(landing))
        assert "My bots" in labs
        assert "Create" in labs or "add bot" in labs.lower()
        blocked = ui.button("clone-owner", "a:cb")
        assert not blocked.ok
        assert ui.createbot_enabled() is True
        assert store.kv_get("feature_createbot") == "1"
        assert ui._toggle_createbot() is True
        assert store.kv_get("feature_createbot") == "1"
        panel = ui.admin_panel("clone-owner")
        blob = panel.text + " ".join(l for l, _ in _labels(panel))
        datas = {d for _l, d in _labels(panel)}
        assert "Users may use bot" not in blob
        assert "Clone-bot" not in blob
        assert "a:on" not in datas
        assert "a:cb" not in datas
        store.kv_set("feature_createbot", "0")
        assert ui.createbot_enabled() is False
        assert ("🤖 Run your own bot", "cb") not in _labels(ui.main_menu("cust"))
        off = ui.text("cust", "/createbot")
        assert not off.ok
        assert "turned OFF" in off.text
    finally:
        ledger.close()

