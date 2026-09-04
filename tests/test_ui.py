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
    labels = []
    for row in reply.rows:
        for b in row:
            if isinstance(b, tuple) and len(b) == 2:
                labels.append(b[0])
    assert any("Check OTP" in l for l in labels)
    assert any("Resend" in l for l in labels)
    assert any("Cancel" in l for l in labels)
    store.close()


def test_check_otp_works_after_restart_via_db(tmp_path):
    """The 💰 Check OTP (and the co:/rs:/cx: buttons) must survive a redeploy:
    the router re-enters the wait straight from the activenumbers row, not the
    now-empty in-memory _awaiting map."""
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
                           owner_id=OWNER, allowed_users=(OWNER, USER), wallets=store)
    ui = MenuUI(router)
    # Simulate a pre-restart buy: an active row exists and the in-memory _awaiting is empty.
    store.record_active(user_id=USER, slug="telegram", phone="+919000000000",
                        provider_order_id="alloc1", token="tok9", gross=INR(15),
                        valid_until=_time.time() + 600)
    # The mock has no SMS for alloc1, so check_otp refunds -> record_order writes history.
    reply = ui.button(USER, "co:tok9")
    reply.deferred(USER)  # run the off-event-loop wait
    # A terminal outcome: OTP delivered OR refund recorded in orders.
    rows = store.recent_orders(scope="", user_id=USER, limit=5)
    assert len(rows) >= 1
    assert rows[0].status in ("delivered", "refunded")
    store.close()


def test_history_detail_rich_receipt(tmp_path):
    """👁 Details shows a full receipt: exact date/time, status, reason, charged,
    refunded, provider cost and balance-after -- for that customer only."""
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
                       config=EngineConfig(otp_timeout_seconds=1.0))
    store = SqliteWallets(str(tmp_path / "w.db"))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER), wallets=store)
    ui = MenuUI(router)
    oid = store.record_order(user_id=USER, slug="telegram", amount=INR(18),
                             phone="+911111", otp="654321", success=True,
                             profit=INR(5), status="delivered",
                             reason="delivered after 2 attempts", spent=INR(10),
                             balance_after=INR(82), scope="")
    reply = ui.button(USER, f"h:{oid}")
    assert reply.ok
    assert "delivered" in reply.text
    assert "18" in reply.text and "10" in reply.text and "82" in reply.text
    assert "654321" in reply.text
    # Another user cannot view it.
    other = ui.button(OUTSIDER, f"h:{oid}")
    assert other.ok is False
    store.close()


def test_typed_my_numbers_matches_the_button_screen(tmp_path):
    """'my numbers' typed (and /numbers) must open the same screen as 🧾 My
    numbers, not fall through to search and return the menu."""
    import time as _time
    from uotpbot.wallets import SqliteWallets
    catalog = Catalog(
        {"telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                 Decimal("0.94"))},
        (WalletPack("Pro", INR(1000), INR(1150)),),
    )
    ledger = Ledger(); pricer = Pricer(catalog)
    provider = MockProvider({s.slug: catalog.sticker_price(s.slug)
                             for s in catalog.services()}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    store = SqliteWallets(str(tmp_path / "w.db"))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER), wallets=store)
    ui = MenuUI(router)
    store.record_active(user_id=USER, slug="telegram", phone="+919000000000",
                        provider_order_id="p1", token="tok1", gross=INR(15),
                        valid_until=_time.time() + 600)
    for phrase in ("my numbers", "My Numbers", "/numbers", "/my numbers"):
        reply = ui.text(USER, phrase)
        assert "my numbers" in reply.text.lower() or "LIVE" in reply.text \
            or "No live numbers" in reply.text, phrase
        assert reply.text.strip() and "✳️ YCOTP Numbers" != reply.text.strip()
    store.close()

# -- Favourites & Support & admin support-username edit --------------------

def test_menu_has_favourites_and_support_buttons(rig):
    ui, _router, _provider, _ledger = rig
    reply = ui.main_menu(USER)
    datas_ = datas(reply)
    assert "fav" in datas_, f"menu must have a Favourites button, got {datas_}"
    assert "support" in datas_, f"menu must have a Support button, got {datas_}"
    # Favourites sits on the SAME row as How it works.
    hrow = [r for r in reply.rows if any(d == "h" for _l, d in r)]
    frow = [r for r in reply.rows if any(d == "fav" for _l, d in r)]
    assert hrow and frow and hrow == frow, "Favourites must be parallel with How it works"


def test_favourites_flow_star_toggle_and_card(rig):
    ui, _router, _provider, _ledger = rig
    # No favourites yet.
    reply = ui.favourites_card(USER)
    assert "haven't starred anything" in reply.text
    # Star telegram from its service card.
    card = ui.service_card(USER, "telegram")
    assert "fvt:telegram" in datas(card), "service card needs a star button"
    ui.toggle_favourite(USER, "telegram")
    assert ui.is_favourite(USER, "telegram")
    reply = ui.favourites_card(USER)
    assert "Telegram" in reply.text
    # The menu shows the count on the Favourites button itself.
    m_labels = [l for row in ui.main_menu(USER).rows for l, _d in row]
    assert any("Favourites (1)" in l for l in m_labels), m_labels
    # Unstar via the favourites card "Remove" button.
    assert "fvt:telegram" in datas(reply)
    ui.toggle_favourite(USER, "telegram")
    assert not ui.is_favourite(USER, "telegram")


def test_support_button_shows_configurable_contact(rig):
    ui, _router, _provider, _ledger = rig
    reply = ui.support_card(USER)
    assert "@support" in reply.text


def test_admin_can_edit_support_username_and_it_persists(rig):
    ui, _router, _provider, _ledger = rig
    # Admin panel has the edit button.
    panel = ui.admin_panel(OWNER)
    assert "ax:support" in datas(panel), "admin needs a Support username edit"
    # Owner edits support username; it shows to a customer.
    r = ui.button(OWNER, "ax:support")  # opens the input wizard
    assert "EDIT SUPPORT USERNAME" in r.text
    r2 = ui.text(OWNER, "new_support")
    assert "✅" in r2.text
    assert ui.support_contact == "new_support"
    assert "@new_support" in ui.support_card(USER).text


def test_support_button_hidden_when_no_contact_set(rig):
    ui, _router, _provider, _ledger = rig
    # Fail gracefully when no default and no override.
    ui._support_default = ""
    reply = ui.support_card(USER)
    assert "being set up" in reply.text


# -- 20-minute validity display --------------------------------------------

def test_remaining_time_capped_at_20_minutes_not_provider_30(rig):
    """The provider's clock can report 30-minute validity, but the number is
    only ours for 20. _record_active must cap the persisted valid_until to 20
    minutes so the 'min left' screens never claim 30."""
    import time as _time
    from uotpbot.wallets import SqliteWallets
    from uotpbot.catalog import PROVIDER_VALIDITY_MINUTES
    ui, router, _provider, _ledger = rig
    store = SqliteWallets(":memory:")
    router.wallets = store

    class _Alloc:
        order_id = "alloc_prov"
        def seconds_left(self):  # provider claims 30 minutes
            return 30 * 60

    class _Result:  # simulates a provider result whose allocation is 30-min
        phone = "+919000000000"
        _alloc = _Alloc()

    router._record_active(USER, "telegram", INR(15), _Result(), "tok-t")
    active = store.active_numbers(user_id=USER)
    assert active, "active row should exist"
    left = active[0].seconds_left
    assert left <= PROVIDER_VALIDITY_MINUTES * 60 + 1, \
        f"provider claimed 30 min but UI must cap to 20; got {left/60:.1f} min"
    # And every screen renders <= 20 min, never 30.
    reply = ui.history_card(USER)
    assert "30 min" not in reply.text, f"history must not claim 30 min: {reply.text!r}"
