"""Percentage referral programme: bind once, % of net spend, clawback."""

from __future__ import annotations

from decimal import Decimal

from uotpbot.bot.alerts import ChannelPoster, order_placed_update, purchase_update
from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost, WalletPack, ingest_handler_vocab
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockOutcome, MockProvider
from uotpbot.referral import (
    bind,
    clawback_spend,
    credit_spend,
    get_rate,
    invite_link,
    is_enabled,
    parse_rate,
    parse_start_payload,
    referrer_of,
    set_enabled,
    set_rate,
    stats,
)
from uotpbot.wallets import SqliteWallets

OWNER, USER, FRIEND = "1", "2", "9"


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
                           owner_id=OWNER, allowed_users=(OWNER, USER, FRIEND),
                           wallets=store, platform_bot_username="YCOTP_Bot")
    ui = MenuUI(router)
    return ui, router, provider, ledger


def test_parse_start_payload():
    assert parse_start_payload("/start r_7493927458") == "7493927458"
    assert parse_start_payload("/start@YCOTP_Bot r_99") == "99"
    assert parse_start_payload("/start") == ""
    assert parse_start_payload("/help") == ""
    assert parse_start_payload("r_123") == "123"
    assert parse_start_payload("r555") == "555"
    assert parse_start_payload("/start r_notanid") == ""
    assert parse_start_payload("/start r_") == ""


def test_parse_rate_caps_at_ten_percent():
    assert parse_rate("5") == Decimal("0.05")
    assert parse_rate("5%") == Decimal("0.05")
    assert parse_rate("0.05") == Decimal("0.05")
    assert parse_rate("10") == Decimal("0.10")
    assert parse_rate("11") is None
    assert parse_rate("0") is None


def test_bind_once_no_self():
    store = SqliteWallets(":memory:")
    assert bind(store, USER, FRIEND) is True
    assert referrer_of(store, USER) == FRIEND
    assert bind(store, USER, OWNER) is False  # already bound
    assert referrer_of(store, USER) == FRIEND
    assert bind(store, FRIEND, FRIEND) is False  # self
    assert bind(store, "3", FRIEND, existing=True) is False
    assert referrer_of(store, "3") == ""
    assert bind(store, "abc", FRIEND) is False


def test_stats_unique_and_like_safe():
    store = SqliteWallets(":memory:")
    # Unescaped LIKE 'ref_of:%' would also match this key (`_` = any char).
    store.kv_set("refXof:zzz", FRIEND)
    store.kv_set("referral_rate", "0.05")
    assert bind(store, USER, FRIEND) is True
    n, _ = stats(store, FRIEND)
    assert n == 1
    assert bind(store, USER, FRIEND) is False
    n, _ = stats(store, FRIEND)
    assert n == 1
    scanned = store.kv_scan("ref_of:")
    assert "refXof:zzz" not in scanned
    assert scanned.get(f"ref_of:{USER}") == FRIEND


def test_credit_and_clawback_percent_of_spend():
    store = SqliteWallets(":memory:")
    set_enabled(store, True)
    set_rate(store, Decimal("0.05"))
    bind(store, USER, FRIEND)
    store.adjust(FRIEND, INR(0))  # ensure row
    charge = INR("14.50")  # 1450 paise → 5% = 72 paise (floor)
    payout = credit_spend(store, store, USER, charge, order_key="otp:1")
    assert payout is not None
    assert payout.referrer_id == FRIEND
    assert payout.amount == Money(72)
    assert store.balance(FRIEND) == Money(72)
    # idempotent
    assert credit_spend(store, store, USER, charge, order_key="otp:1") is None
    # full refund claws it back
    back = clawback_spend(store, store, "otp:1", refunded=charge, original=charge)
    assert back is not None
    assert back.amount == Money(72)
    assert store.balance(FRIEND).is_zero
    # second clawback is a no-op
    assert clawback_spend(store, store, "otp:1", refunded=charge, original=charge) is None


def test_disabled_pays_nothing():
    store = SqliteWallets(":memory:")
    bind(store, USER, FRIEND)
    assert not is_enabled(store)
    assert credit_spend(store, store, USER, INR(100), order_key="otp:x") is None


def test_invite_link_and_start_binds():
    store = SqliteWallets(":memory:")
    ui, router, _p, ledger = _rig(store)
    try:
        set_enabled(store, True)
        menu = ui.text(USER, f"/start r_{FRIEND}")
        assert menu.ok
        assert "YC OTP" in menu.text
        assert referrer_of(store, USER) == FRIEND
        assert "rf" in [d for row in menu.rows for _l, d in row]
        card = ui.button(USER, "rf")
        assert "Invite" in card.text
        assert invite_link("YCOTP_Bot", USER) in card.text
        assert "5%" in card.text
        assert stats(store, FRIEND)[0] == 1
        assert "Friends joined: 0" in card.text  # USER was referred, they invited nobody
    finally:
        ledger.close()


def test_first_start_binds_even_if_note_user_ran_first():
    """Live Telegram notes the user before text(); first invite must still bind."""
    store = SqliteWallets(":memory:")
    ui, _r, _p, ledger = _rig(store)
    try:
        set_enabled(store, True)
        ui.note_user(USER)
        ui.text(USER, f"/start r_{FRIEND}")
        assert referrer_of(store, USER) == FRIEND
        n, _ = stats(store, FRIEND)
        assert n == 1
    finally:
        ledger.close()


def test_existing_user_cannot_bind_via_later_invite():
    store = SqliteWallets(":memory:")
    ui, _r, _p, ledger = _rig(store)
    try:
        set_enabled(store, True)
        ui.text(USER, "/start")
        assert referrer_of(store, USER) == ""
        ui.note_user(USER)
        ui.text(USER, f"/start r_{FRIEND}")
        assert referrer_of(store, USER) == ""
        n, _ = stats(store, FRIEND)
        assert n == 0
    finally:
        ledger.close()


def test_restart_same_invite_does_not_recount():
    store = SqliteWallets(":memory:")
    ui, _r, _p, ledger = _rig(store)
    try:
        set_enabled(store, True)
        ui.text(USER, f"/start r_{FRIEND}")
        assert referrer_of(store, USER) == FRIEND
        assert stats(store, FRIEND)[0] == 1
        ui.note_user(USER)
        ui.text(USER, f"/start r_{FRIEND}")
        ui.note_user(USER)
        ui.text(USER, f"/start r_{FRIEND}")
        assert referrer_of(store, USER) == FRIEND
        assert stats(store, FRIEND)[0] == 1
        card = ui.button(FRIEND, "rf")
        assert "Friends joined: 1" in card.text
    finally:
        ledger.close()


def test_admin_toggle_and_rate():
    store = SqliteWallets(":memory:")
    ui, _r, _p, ledger = _rig(store)
    try:
        panel = ui.admin_panel(OWNER)
        datas = [d for row in (panel.rows or ()) for _l, d in row]
        assert "a:ref" in datas
        assert "a:upost" in datas
        assert "Referral: off" in panel.text
        screen = ui.button(OWNER, "a:ref")
        assert "OFF" in screen.text
        on = ui.button(OWNER, "a:reftog")
        assert on.ok and "ON" in on.text
        assert is_enabled(store)
        prompt = ui.button(OWNER, "ax:refrate")
        assert "PERCENT" in prompt.text
        done = ui.text(OWNER, "8")
        assert done.ok
        assert get_rate(store) == Decimal("0.08")
        # cap
        ui.button(OWNER, "ax:refrate")
        bad = ui.text(OWNER, "15")
        assert not bad.ok
        assert get_rate(store) == Decimal("0.08")
        # clones / non-owners
        assert not ui.button(USER, "a:ref").ok
    finally:
        ledger.close()


def test_purchase_credits_referrer_and_notifies():
    store = SqliteWallets(":memory:")
    ui, router, provider, ledger = _rig(store)
    try:
        set_enabled(store, True)
        bind(store, USER, FRIEND)
        router.credit(USER, INR(100))
        provider.force_next(MockOutcome("success", otp="111111"))
        reply = router.purchase(USER, "telegram")
        assert reply.ok
        assert reply.notify
        assert reply.notify[0][0] == FRIEND
        assert "Referral" in reply.notify[0][1]
        assert store.balance(FRIEND).paise > 0
        n, earned = stats(store, FRIEND)
        assert n == 1
        assert earned.paise > 0
    finally:
        ledger.close()


def test_auto_post_toggle_only_gates_fakes():
    store = SqliteWallets(":memory:")
    store.kv_set("updates_channel",
                 '{"chat": "-10099", "title": "U", "username": "u", "link": ""}')
    sent = []
    poster = ChannelPoster(store, bot_username="YCOTP_Bot",
                           send_fn=lambda p: sent.append(p) or (True, ""))
    assert poster.auto_post_on() is False
    assert poster.post("real event") is True  # real sales still post
    assert poster.post_fake("fake") is False
    store.kv_set("feature_updates", "1")
    assert poster.auto_post_on() is True
    assert poster.post_fake("fake") is True
    store.kv_set("feature_updates", "0")
    assert poster.auto_post_on() is False
    assert poster.post_fake("fake2") is False
    assert poster.post("real 2") is True
    assert len(sent) == 3


def test_ordered_and_delivered_copy():
    placed = order_placed_update("Telegram", INR("15.00"), bot="YCOTP_Bot")
    assert "New Order Success" in placed
    assert "Telegram" in placed
    d = purchase_update("Telegram", INR("15.00"), bot="YCOTP_Bot", delivered=True)
    assert "Order Delivered" in d
    assert "111111" not in d


def test_apply_live_prices_adds_missing_without_changing_markup():
    cat = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.90")),
    })
    stats = cat.apply_live_prices({
        "telegram": INR(12),
        "newapp": INR(8),
    })
    assert stats["updated"] == 1
    assert stats["added"] == 1
    assert cat.get("telegram").list_price == INR(12)
    assert cat.has("newapp")
    assert cat.get("newapp").list_price == INR(8)


def test_ingest_vocab_skips_existing(tmp_path):
    cat = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.90")),
    })
    vocab = tmp_path / "v.json"
    vocab.write_text(
        '{"map": {"telegram": "telegram", "brandnew": "brandnew"},'
        ' "op_prices": {"telegram": {"3": 99}, "brandnew": {"3": 7.5}}}',
        encoding="utf-8",
    )
    n = ingest_handler_vocab(cat, vocab)
    assert n == 1
    assert cat.get("telegram").list_price == INR(10)  # unchanged
    assert cat.has("brandnew")
