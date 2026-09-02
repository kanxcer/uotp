"""Crawl the whole button tree and press everything, like a customer who
taps randomly -- no tap may crash, hang, or answer with the 'stale' fallback
when reached through the tree itself.

Also pins the transport contract: callback payloads fit Telegram's 64-byte
limit and rows never exceed the 8-button row cap.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, CatalogError, ServerOption, ServiceCost, load_catalog
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider

OWNER = "1"
USER = "2"
STALE = "went stale"


def make_rig(*, with_servers=True):
    fixed = {
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10), Decimal("0.94")),
        "google": ServiceCost("google", "Google", "tech", INR(12), Decimal("0.80")),
        "binance": ServiceCost("binance", "Binance", "crypto", INR(22), Decimal("0.74")),
    }
    servers = {}
    if with_servers:
        servers = {
            "google": [
                ServerOption("11", INR(12), 400, name="Server 11"),
                ServerOption("12", INR(15), 900, name="Server 12", is_best=True),
            ],
        }
    catalog = Catalog(fixed, servers=servers)
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
                            balance=INR(9999), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(otp_timeout_seconds=0.3, poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER))
    return MenuUI(router), router, provider


def walk(ui, user):
    seen, queue = set(), deque(["m"])
    replies = []
    while queue:
        data = queue.popleft()
        if data in seen:
            continue
        seen.add(data)
        reply = ui.button(user, data)
        replies.append((data, reply))
        assert reply.text, f"empty reply for tap {data!r}"
        for row in reply.rows:
            assert len(row) <= 8, f"row too long at {data!r}"
            for label, cb in row:
                assert label, f"empty label at {data!r}"
                assert len(cb.encode()) <= 64, f"callback >64B: {cb!r}"
                # Never auto-run deferred purchases in a crawl; queue everything else.
                if cb not in ("nop",) and not cb.startswith("y:") and cb not in seen:
                    queue.append(cb)
    return replies


def test_crawl_from_main_menu_presses_everything_safely():
    ui, _, _ = make_rig()
    replies = walk(ui, USER)
    for data, reply in replies:
        # A reachable button may legitimately say ok=False (out of stock etc.)
        # but it must never be the unknown-tap fallback.
        assert STALE not in reply.text, f"reachable tap {data!r} hit the stale fallback"
    datas = {d for d, _ in replies}
    assert "l" in datas and any(d.startswith("s:") for d in datas)
    assert "w" in datas and "o" in datas and "h" in datas


def test_owner_crawl_reaches_admin_screens():
    ui, _, _ = make_rig()
    replies = walk(ui, OWNER)
    datas = {d for d, _ in replies}
    for expected in ("a", "a:t", "a:o", "a:qr"):
        assert expected in datas


def test_server_picker_lists_each_server_with_its_price():
    ui, _, _ = make_rig()
    card = ui.button(USER, "s:google")
    labels = [label for row in card.rows for label, _ in row]
    datas = [d for row in card.rows for _, d in row]
    assert any("Server 11" in label for label in labels)
    assert any("⭐ Server 12" in label for label in labels)
    assert "y:google:11" in datas and "y:google:12" in datas
    flat = ui.button(USER, "s:telegram")
    assert "y:telegram" in [d for row in flat.rows for _, d in row]


def test_buy_from_a_server_charges_that_servers_price():
    ui, router, _ = make_rig()
    router.credit(USER, INR(500))
    picked = ui.button(USER, "y:google:12")
    assert picked.deferred is not None
    final = picked.deferred(USER)
    assert final.ok and "OTP" in final.text
    advice_12 = ui.pricer.price(router.catalog.get("google").with_overrides(list_price=INR(15)))
    advice_11 = ui.pricer.price(router.catalog.get("google").with_overrides(list_price=INR(12)))
    if advice_12.gross_price.paise != advice_11.gross_price.paise:
        assert f"{advice_12.gross_price}" in final.text


def test_server_param_reaches_the_provider_call():
    catalog = Catalog({
        "google": ServiceCost("google", "Google", "tech", INR(12), Decimal("0.80")),
    }, servers={"google": [ServerOption("12", INR(15), 9)]})

    class Spy(MockProvider):
        def __init__(self, prices, **kw):
            super().__init__(prices, **kw)
            self.seen_server = ""

        def buy_number(self, service, country="in", *, idempotency_key=None, server=""):
            self.seen_server = server
            return super().buy_number(service, country,
                                      idempotency_key=idempotency_key, server=server)

    provider = Spy({"google": INR(15)}, balance=INR(9999), seed=5)
    engine = BotEngine(catalog, provider, Ledger(), Pricer(catalog),
                       config=EngineConfig(otp_timeout_seconds=0.3, poll_interval=0.01))
    router = CommandRouter(engine, catalog, Pricer(catalog), Ledger(),
                           owner_id=OWNER, allowed_users=(USER,))
    router.credit(USER, INR(500))
    router.purchase(USER, "google", server="12")
    assert provider.seen_server == "12"


def test_unknown_server_tap_is_a_safe_no_sale():
    ui, router, _ = make_rig()
    router.credit(USER, INR(500))
    before = router.balance_of(USER)
    picked = ui.button(USER, "y:google:999")
    assert not picked.ok
    assert picked.deferred is None
    assert router.balance_of(USER).paise == before.paise  # nothing moved


def test_target_margin_is_actually_hit_per_quote():
    """Pricing math: at the deployed 10% target, every quote clears it."""
    from uotpbot.money import rate

    catalog = load_catalog()
    pricer = Pricer(catalog, target_margin=rate("0.10"))
    for svc in [catalog.get(s) for s in ("google", "facebook", "swiggy", "irctc")]:
        advice = pricer.price(svc)
        assert advice.econ.gross_margin_ratio >= rate("0.095"), (
            f"{svc.slug}: priced at {advice.gross_price} yields margin "
            f"{advice.econ.gross_margin_ratio}, below target"
        )
        assert advice.econ.is_profitable


def test_servers_csv_roundtrip():
    from uotpbot.catalog import load_servers_csv

    servers = load_servers_csv(
        "slug,server_id,name,price_inr,stock,score,is_best\n"
        "google,11,Server 11,12.00,400,59.36,\n"
        "google,12,Server 12,15.00,900,61.20,1\n"
    )
    google = servers["google"]
    assert [o.server_id for o in google] == ["11", "12"]
    assert google[1].is_best
    cat = Catalog(
        {"google": ServiceCost("google", "Google", "tech", INR(12), Decimal("0.8"))},
        servers=servers,
    )
    assert cat.server_option("google", "12").price.paise == 1500
    with pytest.raises(CatalogError):
        load_servers_csv("no,header\n1,2\n")
