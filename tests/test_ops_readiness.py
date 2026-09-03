"""Provider vocabulary + live-diagnosed request shape, and the owner's
maintenance switch + YCOTP rebrand.

Every getNumber request requires a numeric ``operator`` id (verified against
the live endpoint: names/any/0/1 -> BAD_OPERATOR, 2..10 pass validation) and
a numeric country id (22 = India; "in" -> BAD_COUNTRY). These tests pin both,
plus the operator walk: one tap may try several pools before concluding
"no stock", but MUST stop the moment a timeout makes the charge ambiguous.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServerOption, ServiceCost
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.base import PurchaseTimedOut
from uotpbot.provider.mock import MockProvider
from uotpbot.provider.uotp import UotpConfig, UotpProvider

OWNER, USER = "1", "2"


# ------------------------------------------------------------- uotp provider
def _fake_opener(*bodies):
    from tests.test_provider import FakeOpener

    return FakeOpener(*bodies)


def _provider(*bodies, **cfg):
    opener = _fake_opener(*bodies)
    return UotpProvider(UotpConfig(api_key="TESTKEY", **cfg), opener=opener), opener


def test_get_number_carries_operator_and_numeric_country():
    """The exact two defects that made a live bigbasket order fail instantly."""
    p, opener = _provider("ACCESS_NUMBER:98765:+919876543210")
    p.buy_number("bigbasket", "in", server="11")
    url = opener.calls[0]["url"]
    assert "service=bigbasket" in url
    assert "country=22" in url            # was 'in' -> BAD_COUNTRY
    assert "operator=" in url             # missing entirely -> BAD_OPERATOR
    assert "server=11" in url


def test_operator_walk_concludes_no_stock_only_after_the_list():
    p, opener = _provider("ERROR_NO_NUMBERS", "ERROR_NO_NUMBERS",
                          "ACCESS_NUMBER:9:+9191",
                          service_map={"x": "google"})  # harvest: ops 3,4,7,2,8
    alloc = p.buy_number("x")
    assert alloc.order_id == "9"
    ops = [c["url"].split("operator=")[1][:1] for c in opener.calls]
    assert ops == ["3", "4", "7"]          # cheapest-first order from the harvest


def test_bad_operator_ids_are_skipped_to_a_valid_one():
    p, opener = _provider("BAD_OPERATOR", "ACCESS_NUMBER:7:+91", operator="1")
    alloc = p.buy_number("bigbasket")
    assert alloc.order_id == "7"
    assert len(opener.calls) == 2


def test_ambiguous_timeout_stops_the_walk_immediately():
    """A timed-out getNumber may already be charged: never buy twice."""
    import urllib.error

    p, opener = _provider(urllib.error.URLError("boom"),
                          "ACCESS_NUMBER:7:+91", max_retries=0)
    with pytest.raises(PurchaseTimedOut):
        p.buy_number("bigbasket")
    assert len(opener.calls) == 1           # ONE attempt, not one per operator


def test_vocabailability_gate_mirrors_the_live_vocabulary():
    p, _ = _provider("ACCESS_NUMBER:1:+91")
    assert p.can_serve("google")            # harvested: handler knows it
    assert not p.can_serve("telegram")      # handler_api has no telegram rail
    # provider vocabulary resolves its odd case shifts: instagram -> Instagram
    alloc = p.buy_number("instagram")
    assert alloc.order_id == "1"
    assert "service=Instagram" in _last_url(p)


def _last_url(p: UotpProvider) -> str:
    return p._opener.calls[-1]["url"]


def test_unmapped_service_still_tries_the_raw_slug():
    p, opener = _provider("ACCESS_NUMBER:5:+91")
    p.buy_number("telegraph")               # present on the site, absent here
    assert "service=telegraph" in opener.calls[0]["url"]


# ------------------------------------------------------- maintenance + brand
def _rig(sqlite_wallets=None):
    catalog = Catalog(
        {"google": ServiceCost("google", "Google", "tech", INR(10), Decimal("0.80"))},
        servers={"google": [ServerOption("11", INR(10), 100)]},
    )
    engine = BotEngine(
        catalog, MockProvider({"google": INR(10)}, balance=INR(9999), seed=7),
        Ledger(), Pricer(catalog),
        config=EngineConfig(otp_timeout_seconds=0.3, poll_interval=0.02),
    )
    router = CommandRouter(engine, catalog, Pricer(catalog), Ledger(),
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           wallets=sqlite_wallets)
    ui = MenuUI(router)
    router.maintenance_fn = ui.maintenance_on     # same wiring as telegram.py
    return ui, router


def test_maintenance_toggle_blocks_customer_buys_only(tmp_path):
    ui, router = _rig()
    router.credit(USER, INR(500))

    panel = ui.button(OWNER, "a")
    assert "🛠 Toggle maintenance" in [b for row in panel.rows for b, _ in row]

    flip = ui.button(OWNER, "a:mm")
    assert "Maintenance ON" in flip.text and ui.maintenance_on()

    blocked = ui.button(USER, "y:google:11")
    assert not blocked.ok and blocked.deferred is None
    assert "Maintenance" in router.handle(USER, "/buy google").text

    menu = ui.button(USER, "m")
    assert "Maintenance in progress" in menu.text        # banner tells customers why

    owner_buy = ui.button(OWNER, "y:google:11")          # owner always can (testing)
    assert owner_buy.deferred is not None

    back = ui.button(OWNER, "a:mm")
    assert "Maintenance OFF" in back.text and not ui.maintenance_on()
    again = ui.button(USER, "y:google:11")
    assert again.deferred is not None


def test_maintenance_flag_persists_in_the_wallet_kv(tmp_path):
    from uotpbot.wallets import SqliteWallets

    store = SqliteWallets(tmp_path / "w.db")
    try:
        ui, router = _rig(store)
        ui.button(OWNER, "a:mm")
        # a NEW ui instance over the same store must see the same flag
        ui2 = MenuUI(router)
        assert ui2.maintenance_on()
        assert store.kv_get("maintenance") == "1"
    finally:
        store.close()


def test_customer_cant_flip_maintenance():
    ui, _ = _rig()
    reply = ui.button(USER, "a:mm")
    assert not reply.ok and not ui.maintenance_on()


def test_user_facing_copy_says_ycotp():
    ui, _ = _rig()
    texts = [ui.button(USER, "m").text, ui.button(USER, "h").text]
    joined = "\n".join(texts)
    assert "YCOTP" in joined
    assert "UOTP" not in joined


def test_unavailable_service_via_card_is_a_clean_no(tmp_path):
    """Provider says it cannot serve the slug -> the card explains, no charge."""
    from uotpbot.provider.base import Provider

    class CappedMock(MockProvider, Provider):
        def can_serve(self, slug):        # noqa: D102 - provider contract
            return False

    catalog = Catalog({"cloudkitchen": ServiceCost(
        "cloudkitchen", "CloudKitchen", "food", INR(10), Decimal("0.8"))})
    engine = BotEngine(
        catalog, CappedMock({"cloudkitchen": INR(10)}, balance=INR(9999), seed=3),
        Ledger(), Pricer(catalog),
        config=EngineConfig(otp_timeout_seconds=0.2, poll_interval=0.02),
    )
    router = CommandRouter(engine, catalog, Pricer(catalog), Ledger(),
                           owner_id=OWNER, allowed_users=(OWNER, USER))
    ui = MenuUI(router)
    card = ui.button(USER, "s:cloudkitchen")
    assert not card.ok and "temporarily unavailable" in card.text
    buy = router.purchase(USER, "cloudkitchen")
    assert not buy.ok and "temporarily unavailable" in buy.text


# -------------------------------------------------- live-observed bare tokens
def test_bare_no_numbers_and_no_balance_are_recognized_and_walk_continues():
    """Regression: uotp.store's handler returns BARE NO_NUMBERS / NO_BALANCE
    (no 'ERROR_' prefix) on getNumber. They were not in ERROR_TOKENS, so they
    parsed as a *success* status; the operator walk then broke on the FIRST
    empty pool and raised "expected ACCESS_NUMBER but got NO_NUMBERS" instead
    of trying the next operator -- a live bigbasket buy failed even though a
    later operator (8) had stock.

    This replays the exact live sequence and asserts the walk reaches operator
    8 and returns the number.
    """
    p, opener = _provider(
        "NO_NUMBERS", "NO_BALANCE", "NO_BALANCE",
        "NO_NUMBERS", "NO_NUMBERS",
        "ACCESS_NUMBER:6a991a5e295d9b7e0600e25c:919334803395",
    )
    alloc = p.buy_number("bigbasket", "22")
    assert alloc.order_id == "6a991a5e295d9b7e0600e25c"
    assert alloc.phone == "919334803395"
    ops = []
    for c in opener.calls:
        # each call is action=getNumber&...operator=N
        q = c["url"].split("?")[1].split("&")
        ops.append(dict(kv.split("=", 1) for kv in q).get("operator"))
    assert "8" in ops          # it reached the operator that had stock
    assert "3" in ops          # and it walked past the empty pools


def test_all_bare_no_stock_reports_clean_number_unavailable():
    """If every operator pool is empty, the customer gets a clean no-stock
    (NumberUnavailable) message rather than a confusing parse error.
    Six operators in the bigbasket walk -> six NO_NUMBERS responses."""
    from uotpbot.provider.base import NumberUnavailable
    p, _ = _provider("NO_NUMBERS", "NO_NUMBERS", "NO_NUMBERS",
                     "NO_NUMBERS", "NO_NUMBERS", "NO_NUMBERS")
    with pytest.raises(NumberUnavailable):
        p.buy_number("bigbasket", "22")
