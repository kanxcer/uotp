"""The guided button interface.

Covers the menu tree a customer taps through -- browse, service card,
confirm, outcome -- and proves one thing above all: every tap either
renders a menu or defers onto the *same* money path /buy already uses.
A stale or forged tap can never spend.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider

OWNER = "111"
USER = "222"
OUTSIDER = "999"


@pytest.fixture()
def rig():
    catalog = Catalog(
        {
            "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                    Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
            "binance": ServiceCost("binance", "Binance", "crypto", INR(22),
                                   Decimal("0.74"), Decimal("0.14"), Decimal("0.80")),
            "uber": ServiceCost("uber", "Uber", "transport", INR(14),
                                Decimal("0.89"), Decimal("0.07"), Decimal("0.90")),
        },
        (WalletPack("Pro", INR(1000), INR(1150)),),
    )
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(5000), seed=5,
    )
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER))
    ui = MenuUI(router, support_contact="@support")
    yield ui, router, provider, ledger


def all_buttons(reply):
    out = []
    for row in reply.rows:
        out.extend(row)
    out.extend(reply.buttons)
    return out


def datas(reply):
    return [d for _label, d in all_buttons(reply)]


# -- entry & navigation -----------------------------------------------------

def test_start_opens_menu_with_balance_and_core_buttons(rig):
    ui, router, *_ = rig
    reply = ui.text(USER, "/start")
    assert reply.ok
    assert "balance" in reply.text.lower()
    menu = datas(reply)
    for expected in ("l", "w", "o", "h"):
        assert expected in menu
    # No white-label on this bot -> no "run your own bot" entry.
    assert "cb" not in menu
    # USER is not the owner -> no admin entry.
    assert "a" not in menu
    admin = ui.button(OWNER, "m")
    assert "a" in datas(admin)


def test_plain_text_gets_the_menu_not_a_help_wall(rig):
    ui, *_ = rig
    reply = ui.text(USER, "hello there")
    assert reply.ok
    assert "l" in datas(reply)


def test_unknown_and_stale_taps_fall_back_to_menu(rig):
    ui, *_ = rig
    for data in ("", "x", "c"):
        reply = ui.button(USER, data)
        assert not reply.ok
        assert "m" in datas(reply)
    gone = ui.button(USER, "s:")  # empty slug reads as "left the catalogue"
    assert not gone.ok and "l" in datas(gone)
    # A forged buy tap for a service that does not exist cannot spend.
    reply = ui.button(USER, "y:does-not-exist")
    assert not reply.ok
    assert reply.deferred is None


def test_unauthorised_user_gets_nothing(rig):
    ui, *_ = rig
    for tap in ("m", "l", "y:telegram"):
        assert not ui.button(OUTSIDER, tap).ok


# -- browsing ---------------------------------------------------------------

def test_grid_shows_every_service_with_price_on_the_button(rig):
    ui, router, *_ = rig
    reply = ui.button(USER, "l")
    labels = [label for label, _ in all_buttons(reply)]
    for name in ("Telegram", "Binance", "Uber"):
        assert any(name in label for label in labels)
    # The price is printed on the button itself.
    assert any("₹" in label or "Rs" in label for label in labels)
    # Category chips exist because >1 category.
    assert any(d.startswith("c:") for d in datas(reply))
    # Rows are at most 2 buttons wide.
    assert all(len(row) <= 2 or row[0][1].startswith("c:") for row in reply.rows)


def test_category_chip_filters(rig):
    ui, *_ = rig
    reply = ui.button(USER, "c:crypto")
    labels = [label for label, _ in all_buttons(reply)]
    assert any("Binance" in label for label in labels)
    assert not any("Uber ·" in label for label in labels)


def test_service_card_has_buy_button_and_refund_policy(rig):
    ui, *_ = rig
    card = ui.button(USER, "s:telegram")
    assert card.ok
    assert "Telegram" in card.text
    assert "refund" in card.text.lower()
    assert f"y:{'telegram'}" in datas(card)
    assert card.deferred is None


def test_service_card_warns_when_balance_is_short(rig):
    ui, *_ = rig
    card = ui.button(USER, "s:telegram")
    assert "⚠️" in card.text  # balance is zero


# -- purchase ---------------------------------------------------------------

def test_buy_tap_replies_instantly_and_defers_the_work(rig):
    ui, router, provider, ledger = rig
    router.credit(USER, INR(100))
    reply = ui.button(USER, "y:telegram")
    assert reply.ok and reply.deferred is not None
    assert "⏳" in reply.text
    final = reply.deferred(USER)  # what the transport runs off-loop
    assert "OTP" in final.text
    # Charged the quoted price, wallet updated, ledger booked.
    assert router.balance_of(USER).paise < INR(100).paise
    assert final.rows, "outcome must offer what-to-do-next buttons"


def test_failed_buy_refunds_via_the_same_command_path(rig):
    ui, router, provider, ledger = rig  # noqa: F841
    router.credit(USER, INR(100))
    before = router.balance_of(USER)
    provider.force_sequence([])  # no stock: purchase fails immediately
    from uotpbot.provider.base import NumberUnavailable

    def fail_buy(*a, **k):
        raise NumberUnavailable("no stock")

    provider.buy_number = fail_buy  # type: ignore[method-assign]
    final = ui.button(USER, "y:telegram").deferred(USER)
    assert not final.ok
    assert "Refund" in final.text or "refund" in final.text
    assert router.balance_of(USER).paise == before.paise


def test_insufficient_balance_never_reaches_the_provider(rig):
    ui, router, provider, _ = rig
    final = ui.button(USER, "y:telegram").deferred(USER)
    assert not final.ok
    assert "Top up" in final.text
    assert provider.get_balance().credit.paise == INR(5000).paise


# -- history & wallet --------------------------------------------------------

def test_history_records_purchases_this_session(rig):
    ui, router, *_ = rig
    router.credit(USER, INR(100))
    ui.button(USER, "y:telegram").deferred(USER)
    history = ui.button(USER, "o")
    assert history.ok
    assert "session" in history.text.lower()
    assert "telegram" in history.text.lower() or "OTP" in history.text


def test_wallet_card_and_topup_disabled_fallback(rig):
    ui, router, *_ = rig
    router.credit(USER, INR(25))
    reply = ui.button(USER, "w")
    assert "₹25" in reply.text
    assert "t" in datas(reply)  # ➕ Add money button present
    # No wallet store behind this test rig -> top-ups explain the fallback.
    blocked = ui.button(USER, "t")
    assert not blocked.ok
    assert "@support" in blocked.text
    ui_default = MenuUI(router)
    assert "the bot owner" in ui_default.button(USER, "t").text


# -- owner ------------------------------------------------------------------

def test_owner_panel_shows_money_state(rig):
    ui, *_ = rig
    reply = ui.button(OWNER, "a")
    assert reply.ok
    assert "wallet" in reply.text.lower()
    # Non-owner gets rejected, never the numbers.
    assert not ui.button(USER, "a").ok


# -- text funnel --------------------------------------------------------------

def test_slash_commands_still_work_for_power_users(rig):
    ui, router, *_ = rig
    assert "Balance" in ui.text(USER, "/wallet").text
    router.credit(USER, INR(100))
    assert "OTP" in ui.text(USER, "/buy telegram").text
    assert "Unknown command" in ui.text(USER, "/nope").text


def test_createbot_conversation_is_not_hijacked_by_the_menu(rig):
    """While /createbot waits for a token, plain text must reach its flow."""
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    })
    from uotpbot.whitelabel import PlatformFee, SubBotRegistry

    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"telegram": INR(10)}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer)
    registry = SubBotRegistry()
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           subbots=registry, platform_fee=PlatformFee())
    ui = MenuUI(router)
    start = ui.text(USER, "/createbot")
    assert start.ok and "token" in start.text.lower()
    step = ui.text(USER, "123456:ABCDEF")  # an (invalid) token reaches the flow
    assert "token" in step.text.lower() or "valid" in step.text.lower()
    # The menu did hide when it should but not swallow the answer.
    assert "What would you like to do" not in step.text


def test_low_balance_wallet_card_shows_add_money_nudge(rig):
    """A wallet balance below the cheapest floored sticker gets a clear
    'add at least ₹X' nudge on the wallet card, so a near-empty balance is
    never a silent dead end."""
    ui, router, *_ = rig
    router.credit(USER, INR(5))
    card = ui.button(USER, "w")
    assert "Add at least" in card.text
    # the add-money button is on the card (rows, not text)
    labels = [label for row in card.rows for label, _ in row]
    assert "➕ Add money" in labels


def test_cheapest_sticker_floors_at_min_charge():
    from uotpbot.catalog import Catalog, ServiceCost, PROVIDER_MIN_CHARGE
    from uotpbot.money import INR
    from decimal import Decimal
    cat = Catalog({"cheap": ServiceCost("cheap", "Cheap", "x", INR(1), Decimal("0.9"))},
                  min_charge=INR(2))
    assert cat.cheapest_sticker() == INR(2)  # 1.00 floored up to min_charge 2.00


def test_my_numbers_shows_active_number_and_check_otp(tmp_path):
    """A bought number stays visible in 'My numbers' even after leaving the buy
    screen: the live set is read from the wallet store and rendered on top."""
    import time as _time
    from uotpbot.wallets import SqliteWallets
    catalog = Catalog(
        {"telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                 Decimal("0.94"))},
        (WalletPack("Pro", INR(1000), INR(1150)),),
    )
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({s.slug: catalog.sticker_price(s.slug)
                             for s in catalog.services()}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    store = SqliteWallets(str(tmp_path / "w.db"))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           wallets=store)
    ui = MenuUI(router)
    store.record_active(user_id=USER, slug="telegram", phone="+919000000000",
                        provider_order_id="p1", token="tok1", gross=INR(15),
                        valid_until=_time.time() + 600)
    reply = ui.button(USER, "o")  # 🧾 My numbers
    assert "LIVE" in reply.text
    assert "+919000000000" in reply.text
    labels = [label for row in reply.rows for label, _ in row]
    assert any("Check OTP" in l for l in labels)
    store.close()
