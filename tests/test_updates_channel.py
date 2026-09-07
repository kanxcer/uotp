"""Public updates channel: owner connects it; successful money events post."""

from __future__ import annotations

from decimal import Decimal

from uotpbot.bot.alerts import (
    ChannelPoster,
    boost_update,
    deposit_update,
    md_to_telegram_html,
    purchase_update,
    withdraw_update,
)
from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockOutcome, MockProvider
from uotpbot.wallets import SqliteWallets

OWNER, USER = "1", "2"


def _rig(store=None):
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {"telegram": catalog.sticker_price("telegram")}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=0.4,
                                           poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           wallets=store, platform_bot_username="YCOTP_Bot")
    ui = MenuUI(router)
    return ui, router, provider, ledger


def test_copy_matches_the_asked_templates():
    d = deposit_update(INR(101), method="FamPay Automatic", bot="YCOTP_Bot")
    assert "New Deposit Success" in d
    assert "₹101" in d
    assert "FamPay Automatic" in d
    assert "Thanks For Deposit" in d
    assert "@YCOTP_Bot" in d
    p = purchase_update("Telegram", INR("15.00"), bot="YCOTP_Bot")
    assert "Number Purchase Successful" in p
    assert "Telegram" in p
    assert USER not in p
    w = withdraw_update(INR(12), bot="YCOTP_Bot")
    assert "Withdrawal Successful" in w
    assert "₹12" in w
    assert "@YCOTP_Bot" in w
    b = boost_update("TikTok Views", INR("15.00"), bot="YCOTP_Bot", order_id="99")
    assert "New Order Success" in b
    assert "TikTok Views" in b
    assert "**Link:** hidden" in b
    assert "**Price:**" in b
    assert "`99`" in b
    assert "Thanks for Purchase @YCOTP_Bot" in b
    assert USER not in b
    assert "http" not in b.lower()
    assert "tiktok.com" not in b.lower()
    d = boost_update("TikTok Views", INR("15.00"), bot="YCOTP_Bot",
                     delivered=True, order_id="99")
    assert "Order Delivered" in d
    assert "**Link:** hidden" in d
    assert USER not in d


def test_admin_panel_has_updates_channel_and_owner_can_set_it():
    ui, _router, _p, ledger = _rig()
    try:
        panel = ui.admin_panel(OWNER)
        datas = [d for row in (panel.rows or ()) for _l, d in row]
        assert "a:uc" in datas
        assert "Updates channel" in panel.text
        assert ui.updates_channel_label() == "off"
        screen = ui.button(OWNER, "a:uc")
        assert "OFF" in screen.text
        prompt = ui.button(OWNER, "ax:uc")
        assert "UPDATES CHANNEL" in prompt.text
        done = ui.text(OWNER, "@ycotp_updates")
        assert done.ok and "ON" in done.text
        assert ui.updates_channel_config()["username"] == "ycotp_updates"
        assert ui.updates_channel_label() == "ON · @ycotp_updates"
        off = ui.button(OWNER, "a:ucoff")
        assert "OFF" in off.text
        assert ui.updates_channel_config() is None
    finally:
        ledger.close()


def test_non_owner_cannot_set_updates_channel():
    ui, _r, _p, ledger = _rig()
    try:
        r = ui.button(USER, "a:uc")
        assert not r.ok
    finally:
        ledger.close()


def test_poster_is_silent_when_channel_is_off():
    store = SqliteWallets(":memory:")
    sent = []
    poster = ChannelPoster(store, bot_username="YCOTP_Bot",
                           send_fn=lambda p: sent.append(p) or (True, ""))
    assert poster.post("hello") is False
    assert sent == []


def test_md_to_telegram_html_bold_and_code():
    html = md_to_telegram_html(
        "📅 **Order Delivered**\n\n**Link:** hidden\n**Order ID:** `99`")
    assert "<b>Order Delivered</b>" in html
    assert "<b>Link:</b> hidden" in html
    assert "<code>99</code>" in html
    assert "**" not in html
    w = md_to_telegram_html(withdraw_update(INR(12), bot="YCOTP_Bot"))
    assert "<b>Withdrawal Successful</b>" in w
    assert "**" not in w


def test_poster_sends_when_channel_is_configured():
    store = SqliteWallets(":memory:")
    store.kv_set("updates_channel",
                 '{"chat": "-10099", "title": "Updates", "username": "u", "link": ""}')
    sent = []
    poster = ChannelPoster(store, bot_username="YCOTP_Bot",
                           send_fn=lambda p: sent.append(p) or (True, ""))
    assert poster.post(deposit_update(INR(101), bot="YCOTP_Bot")) is True
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "-10099"
    assert sent[0]["parse_mode"] == "HTML"
    assert "<b>New Deposit Success</b>" in sent[0]["text"]
    assert "**" not in sent[0]["text"]
    assert "101" in sent[0]["text"]
    assert USER not in sent[0]["text"]


def test_successful_purchase_posts_to_the_channel():
    store = SqliteWallets(":memory:")
    ui, router, provider, ledger = _rig(store)
    sent = []
    router.updates_poster = ChannelPoster(
        store, send_fn=lambda p: sent.append(p) or (True, ""))
    store.kv_set("updates_channel",
                 '{"chat": "-1001", "title": "U", "username": "u", "link": ""}')
    try:
        router.credit(USER, INR(100))
        provider.force_next(MockOutcome("success", otp="111111"))
        reply = router.purchase(USER, "telegram")
        assert reply.ok
        assert sent, "completed purchase must post to the updates channel"
        blob = sent[-1]["text"]
        assert "Purchase Successful" in blob
        assert "Telegram" in blob
        assert "111111" not in blob  # never leak OTP
        assert USER not in blob
    finally:
        ledger.close()


def test_famgateway_button_credit_posts_deposit():
    store = SqliteWallets(":memory:")
    ui, router, _p, ledger = _rig(store)
    sent = []
    router.updates_poster = ChannelPoster(
        store, send_fn=lambda p: sent.append(p) or (True, ""))
    store.kv_set("updates_channel",
                 '{"chat": "-1001", "title": "U", "username": "u", "link": ""}')
    try:
        ui._post_update(deposit_update(INR(101), method="FamPay Automatic",
                                       bot="YCOTP_Bot"))
        assert sent and "New Deposit Success" in sent[0]["text"]
        assert "FamPay Automatic" in sent[0]["text"]
    finally:
        ledger.close()


def test_try_smm_shop_accepts_updates_poster():
    """Boot passes updates_poster into _try_smm_shop; missing kwarg crashes serve."""
    import inspect
    from uotpbot.__main__ import _try_smm_shop
    assert "updates_poster" in inspect.signature(_try_smm_shop).parameters
