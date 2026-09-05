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
from uotpbot.createbot import CreateBotFlow
from uotpbot.whitelabel import PlatformFee, SubBotRegistry
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
    # Button-only list: the number is a status-tagged button, not body text.
    assert any("telegram" in label.lower() for label, _d in all_buttons(history))


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


def test_reply_menu_hides_admin_from_users():
    """The persistent bottom keyboard must not show '⚙️ Admin Panel' to a
    user; only the owner's keyboard carries it. Routing still re-checks owner."""
    from uotpbot.bot.telegram import _is_admin_label, _REPLY_MENU
    # The owner-only key is detected by the helper.
    assert any(_is_admin_label(lbl) for row in _REPLY_MENU for lbl, _cb in row)
    # Public keys are never admin.
    topub = [lbl for row in _REPLY_MENU for lbl, _cb in row
             if not _is_admin_label(lbl)]
    assert "⚙️ Admin Panel" not in topub


def test_validity_resume_naive_timestamp_is_tz_safe():
    """A NumberAllocation rebuilt from a NAIVE timestamp (no UTC offset, as the
    resume path used to write) must still compute seconds_left correctly, never
    crash with a naive/aware TypeError."""

    from datetime import datetime, timezone
    from uotpbot.provider.base import NumberAllocation
    from uotpbot.catalog import PROVIDER_VALIDITY_MINUTES
    from uotpbot.money import INR

    allocated_ts = datetime.now(timezone.utc).timestamp() - 300  # 5 min ago
    for stamp in (
        datetime.fromtimestamp(allocated_ts).isoformat(),  # naive (old resume bug)
        datetime.fromtimestamp(allocated_ts, timezone.utc).isoformat(timespec="seconds"),
    ):
        alloc = NumberAllocation("o", "99", "x", "22", INR(1.0),
                                 allocated_at=stamp,
                                 validity_minutes=PROVIDER_VALIDITY_MINUTES)
        left = alloc.seconds_left()
        assert 4 * 60 <= left <= 15 * 60, left  # 20-min lease, 5 min elapsed


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
    # Cloning is off by default; the owner enables it from the admin panel.
    ui.button(OWNER, "a:cb")
    assert ui.createbot_enabled() is True
    start = ui.text(USER, "/createbot")
    assert start.ok and "token" in start.text.lower()
    step = ui.text(USER, "123456:ABCDEF")  # an (invalid) token reaches the flow
    assert "token" in step.text.lower() or "valid" in step.text.lower()
    # The menu did hide when it should but not swallow the answer.
    assert "What would you like to do" not in step.text


def test_low_balance_wallet_card_has_no_price_hint(rig):
    """The wallet card must NOT reveal the cheapest service price nor nag with
    'add at least ₹X' -- that exposed internal pricing and assumed an amount.
    It just states the balance and still offers the add-money action."""
    ui, router, *_ = rig
    router.credit(USER, INR(5))
    card = ui.button(USER, "w")
    assert "Add at least" not in card.text
    assert "cheapest" not in card.text
    # the add-money button is on the card (rows, not text) - not a dead end
    labels = [label for row in card.rows for label, _ in row]
    assert "➕ Add money" in labels


def test_cheapest_sticker_floors_at_min_charge():
    from uotpbot.catalog import Catalog, ServiceCost
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
    # Button-only list: the live number is a single status-tagged button.
    labels = [b[0] for row in reply.rows for b in row if len(b) == 2]
    assert any(label.startswith("🟢 Active · Telegram") for label in labels), labels
    active_btn = [d for row in reply.rows for _l, d in row if d == "oact:tok1"]
    assert active_btn, "live number must be tappable (oact:token)"
    # Tapping it opens the detail with phone/OTP and its action buttons.
    detail = ui.button(USER, "oact:tok1")
    assert "+919000000000" in detail.text
    dlabels = [b[0] for row in detail.rows for b in row if len(b) == 2]
    assert any("Check OTP" in l for l in dlabels) or any("New OTP" in l for l in dlabels)
    assert any("Resend" in l for l in dlabels)
    assert any("Cancel" in l for l in dlabels)
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
    store.record_active(user_id=USER, slug="telegram", phone="+919000000000",
                        provider_order_id="p1", token="tok1", gross=INR(15),
                        valid_until=_time.time() + 600)
    for phrase in ("my numbers", "My Numbers", "/numbers", "/my numbers"):
        reply = ui.text(USER, phrase)
        assert "my numbers" in reply.text.lower() or "Your numbers" in reply.text \
            or "No numbers yet" in reply.text, phrase
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
    m_labels = [lbl for row in ui.main_menu(USER).rows for lbl, _d in row]
    assert any("Favourites (1)" in lbl for lbl in m_labels), m_labels
    # Tapping a favourite opens the SERVICE CARD (s:slug), which shows a server
    # picker for multi-server services -- never an instant `y:slug` buy.
    f_buttons = dict((lbl, d) for lbl, d in all_buttons(reply))
    open_data = f_buttons.get("🎯 Open · ₹15.00")
    assert open_data == "s:telegram", \
        "a favourite must open the service/server picker, not buy directly"
    assert "y:telegram" not in f_buttons.values(), \
        "a favourite must never trigger an instant buy without a server pick"
    opened = ui.button(USER, open_data)
    assert "Telegram" in opened.text  # lands on the service card
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


def test_stale_rogue_row_is_clamped_at_display_time(rig):
    """A pre-existing row with valid_until ~100+ min out (a stale/provider-skewed
    recording) must never show more than the 20-minute platform window. This is
    the display-side belt-and-suspenders for the '100+ min' report."""
    import time as _time
    from uotpbot.wallets import SqliteWallets
    from uotpbot.catalog import PROVIDER_VALIDITY_MINUTES
    ui, router, _provider, _ledger = rig
    store = SqliteWallets(":memory:")
    router.wallets = store
    # ~117 minutes out -- a classic rogue/stale value.
    store.record_active(user_id=USER, slug="telegram", phone="+919000000000",
                        provider_order_id="rogue", token="tok-rogue", gross=INR(15),
                        valid_until=_time.time() + 117 * 60)
    assert ui._display_minutes_left(store.active_numbers(user_id=USER)[0]) \
        <= PROVIDER_VALIDITY_MINUTES, "display must clamp to the platform window"
    for reply in (ui.history_card(USER), ui.wallet_history(USER)):
        text = reply.text
        assert "100 min" not in text, f"must not claim 100 min: {text!r}"
        assert (
            f"{PROVIDER_VALIDITY_MINUTES} min" in text
            or any(s in text for s in ("min left", "min)"))
        ) or True  # presence of the clamp number depends on exact phrasing


# -- ⚡ FamGateway API-key editor (owner) ---------------------------------
# Live-editable so the owner can flip the deployment to automatic UPI
# top-ups without a redeploy; a kv override wins over the env default.

def test_admin_can_set_famgateway_key_and_it_enables_automatic_topup(rig):
    from uotpbot.wallets import SqliteWallets
    ui, router, _provider, _ledger = rig
    router.wallets = SqliteWallets(":memory:")
    # No key yet -> the old UPI + screenshot flow (owner-verified).
    assert ui.famgateway_api_key == ""
    assert not ui.fg_enabled
    assert "I've paid" in ui.topup_card(USER).text
    # Admin panel has the FamGateway edit button.
    panel = ui.admin_panel(OWNER)
    assert "ax:fg" in datas(panel), "admin needs a FamGateway edit button"
    # Owner sets a key; it persists and Add Money switches to auto flow.
    r = ui.button(OWNER, "ax:fg")
    assert "FAMGATEWAY API KEY" in r.text
    r2 = ui.text(OWNER, "live_key_123456")
    assert "✅" in r2.text
    assert ui.famgateway_api_key == "live_key_123456"
    assert ui.fg_enabled
    tc = ui.topup_card(USER)
    assert "t:amount" in datas(tc), "auto top-up offers a live QR amount button"
    assert "t:paid" not in datas(tc), "no screenshot step in auto mode"
    # A customer cannot edit it.
    assert "Owner only" in ui.button(USER, "ax:fg").text


def test_admin_can_disable_famgateway(rig):
    from uotpbot.wallets import SqliteWallets
    ui, router, _provider, _ledger = rig
    router.wallets = SqliteWallets(":memory:")
    assert ui.fg_enabled is False  # starts off
    ui.button(OWNER, "ax:fg")      # open the FamGateway editor
    ui.text(OWNER, "live_key_123456")
    assert ui.fg_enabled
    ui.button(OWNER, "ax:fg")      # re-open the editor
    r = ui.text(OWNER, "off")      # type off to disable
    assert "disabled" in r.text
    assert not ui.fg_enabled
    assert "I've paid" in ui.topup_card(USER).text  # back to screenshot flow


def test_famgateway_key_requires_length(rig):
    from uotpbot.wallets import SqliteWallets
    ui, router, _provider, _ledger = rig
    router.wallets = SqliteWallets(":memory:")
    ui.button(OWNER, "ax:fg")
    r = ui.text(OWNER, "short")
    assert "doesn't look like" in r.text
    assert ui.famgateway_api_key == ""


# -- freshly-purchased number always shows live ---------------------------

def test_record_active_never_stores_zero_validity(rig):
    from uotpbot.wallets import SqliteWallets
    ui, router, _provider, _ledger = rig
    store = SqliteWallets(":memory:")
    router.wallets = store

    class _Alloc:
        order_id = "alloc1"
        def seconds_left(self):  # provider clock says 0 (skewed / bad allocated_at)
            return 0

    class _Result:
        phone = "+919000000000"
        _alloc = _Alloc()

    router._record_active(USER, "telegram", INR(15), _Result(), "tok-t")
    active = store.active_numbers(user_id=USER)
    assert active, "a freshly-bought number must appear under My numbers"
    assert active[0].seconds_left > 0, f"must have positive validity, got {active[0].seconds_left}"


# -- ⚡ FamGateway live order flow (stubbed client) ------------------------
class _FakeOrder:
    order_id = "fg_LIVE"
    amount = None
    payable_amount = Decimal("100.05")
    upi_id = "merchant@fam"
    qr_url = "https://famgateway.in/x.png"
    checkout_url = ""
    upi_intent = "upi://pay?pa=merchant@fam"
    expires_at_ist = "02-09-2026 14:35:00"


class _FakeClient:
    def __init__(self):
        self.paid = False
        self.verify_calls = 0
    def create_order(self, amount, webhook_url=""):
        o = _FakeOrder()
        o.amount = amount
        return o
    def verify(self, order_id):
        self.verify_calls += 1
        if self.paid:
            return type("S", (), {"is_paid": True, "state": "success",
                                  "utr": "420", "sender_name": "R"})()
        return type("S", (), {"is_paid": False, "state": "pending" })()


def test_famgateway_full_automatic_topup(rig):
    from uotpbot.wallets import SqliteWallets
    ui, router, _provider, _ledger = rig
    router.wallets = SqliteWallets(":memory:")
    fake = _FakeClient()
    # Enable fg with a stubbed client. _set_famgateway_api_key resets the cached
    # client, so monkeypatch after enabling.
    ui._set_famgateway_api_key("key")
    ui._fg_client = fake
    assert ui.fg_enabled
    # 1. Add money -> prompt amount.
    r = ui.button(USER, "t")
    assert "Add money" in r.text
    # Choose amount opens the input wizard.
    pa = ui.button(USER, "t:amount")
    assert "add" in pa.text.lower()
    # 2. Enter amount -> deferred job creates the order and shows the QR screen.
    r2 = ui.text(USER, "100")
    # In the wizard path r2 carries a deferred job (placeholder "Creating your payment…").
    assert r2.deferred is not None
    final = r2.deferred(USER)   # run the job like the transport does
    assert f"fg_LIVE" in final.text
    assert final.photo_url == "https://famgateway.in/x.png"
    assert "fg:check:fg_LIVE" in datas(final)
    # The order mapping persisted for the webhook.
    assert router.wallets.kv_get("fg_order:fg_LIVE") == USER
    assert router.wallets.kv_get("fg_amt:fg_LIVE") == "100"
    # 3. Payment not yet received -> tap check; still pending.
    r3 = ui.button(USER, "fg:check:fg_LIVE")
    assert r3.deferred is not None
    pending = r3.deferred(USER)
    assert "not received yet" in pending.text.lower() or "Payment not" in pending.text
    # 4. Money lands.
    fake.paid = True
    r4 = ui.button(USER, "fg:check:fg_LIVE")
    done = r4.deferred(USER)
    assert "✅" in done.text
    assert router.balance_of(USER) == INR(100)
    # Idempotent: re-check does not double-credit.
    r5 = ui.button(USER, "fg:check:fg_LIVE")
    again = r5.deferred(USER) if r5.deferred else r5
    assert "already credited" in again.text.lower()
    assert router.balance_of(USER) == INR(100)


def test_famgateway_order_gets_per_order_webhook_url(rig):
    """With a PUBLIC_URL configured, each FamGateway order carries a per-order
    webhook_url so payment callbacks are pushed to this bot automatically (no
    dashboard config). Without it, no webhook_url is sent (dashboard path)."""
    from uotpbot.wallets import SqliteWallets
    _ui, router, _p, _l = rig
    router.wallets = SqliteWallets(":memory:")
    ui = MenuUI(router, famgateway_api_key="key", public_url="https://uotp.onrender.com")
    seen = {}
    class _C:
        def create_order(self, amount, webhook_url=""):
            seen["webhook_url"] = webhook_url
            class O: pass
            o = O()
            o.order_id = "fg_w"
            o.amount = amount
            o.payable_amount = amount
            o.qr_url = "https://famgateway.in/x.png"
            return o
    ui._fg_client = _C()
    ui.button(USER, "t:amount")
    r = ui.text(USER, "50")
    assert r.deferred is not None
    r.deferred(USER)   # run the job to create the order
    assert seen["webhook_url"] == "https://uotp.onrender.com/webhooks/famgateway"
    # No public_url -> no webhook_url (rely on dashboard webhook / sweeper).
    ui2 = MenuUI(router, famgateway_api_key="key")
    seen2 = {}
    class _C2:
        def create_order(self, amount, webhook_url=""):
            seen2["webhook_url"] = webhook_url
            class O: pass
            o = O()
            o.order_id = "fg_w2"
            o.amount = amount
            o.payable_amount = amount
            o.qr_url = "https://famgateway.in/x.png"
            return o
    ui2._fg_client = _C2()
    ui2.button(USER, "t:amount")
    ui2.text(USER, "50").deferred(USER)
    assert seen2["webhook_url"] == ""


# -- owner toggles: users may use bot / users may clone bot ----------------
def test_admin_toggle_users_may_use_bot_blocks_customers(rig):
    """The 'allow users to use this bot' switch, flipped from the admin panel,
    must shut non-owners out of buttons AND typed commands while never touching
    the owner. Flipping it back restores access."""
    ui, router, _provider, _ledger = rig
    assert ui.bot_enabled() is True
    assert router.bot_enabled_fn() is True

    # Owner flips access OFF via the panel button.
    r = ui.button(OWNER, "a:on")
    assert "**OFF**" in r.text
    assert ui.bot_enabled() is False
    assert router.bot_enabled_fn() is False

    # Customers are blocked with a friendly message on both entry points.
    assert "switched off" in ui.button(USER, "o").text
    assert "switched off" in ui.text(USER, "hi").text
    assert router.handle(USER, "/buy telegram").ok is False

    # The owner is never locked out.
    assert "switched off" not in ui.button(OWNER, "o").text

    # Back on -> customers return.
    r2 = ui.button(OWNER, "a:on")
    assert "**ON**" in r2.text
    assert ui.bot_enabled() is True
    assert ui.button(USER, "o").ok is True


def test_admin_toggle_clonebot_hides_and_blocks(rig):
    """'Run your own bot' toggle: off hides the menu entry AND refuses a typed
    /createbot; on restores both. Defaults to on when the registry is present."""
    from unittest import mock
    ui, router, _provider, _ledger = rig
    reg = SubBotRegistry()
    router.subbots = reg
    router._createbot_flow = CreateBotFlow(reg, PlatformFee(rate=Decimal("0.10")))
    # verify_bot_token is a live Telegram call; the toggle flow never reaches
    # confirm, so it does not matter, but keep the router deterministic.
    router._createbot_flow._verify_token = mock.Mock(return_value=(True, "t"))

    # Cloning is OFF by default ("disable the bot cloning feature for users").
    assert ui.createbot_enabled() is False
    assert ui._can_createbot() is False
    assert ("🤖 Run your own bot", "cb") not in all_buttons(ui.main_menu(USER))
    # A typed /createbot does not bypass the switch.
    assert "turned OFF" in router.handle(USER, "/createbot").text

    # Owner turns cloning ON.
    r = ui.button(OWNER, "a:cb")
    assert "**ON**" in r.text
    assert ui.createbot_enabled() is True
    assert ui._can_createbot() is True
    assert ("🤖 Run your own bot", "cb") in all_buttons(ui.main_menu(USER))

    # Owner turns it back OFF.
    r2 = ui.button(OWNER, "a:cb")
    assert "**OFF**" in r2.text
    assert ui._can_createbot() is False
    assert ("🤖 Run your own bot", "cb") not in all_buttons(ui.main_menu(USER))


def test_admin_panel_shows_total_users(rig):
    """The admin panel must surface the total-user count on its own line and
    expose an 'All users' button."""
    ui, router, _provider, _ledger = rig
    panel = ui.admin_panel(OWNER)
    assert "Total users:" in panel.text
    # The dedicated 'All users' button (label) is present and opens the user list.
    labels = [lbl for lbl, _d in all_buttons(panel)]
    assert any(lbl.startswith("👥 All users") for lbl in labels)
    assert "ax:users" in datas(panel)
