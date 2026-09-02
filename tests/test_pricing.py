"""Pricing: ladder behaviour, floors, and the Rs.10 provider minimum."""

from decimal import Decimal

import pytest

from uotpbot.catalog import Catalog, ServiceCost, WalletPack, load_catalog
from uotpbot.economics import FeeModel
from uotpbot.money import INR, rate
from uotpbot.pricing import PRICE_LADDER, PriceLadder, Pricer, Strategy


def cat(services=None, packs=()):
    services = {
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
        "whatsapp": ServiceCost("whatsapp", "WhatsApp", "messaging", INR(12),
                                Decimal("0.90"), Decimal("0.06"), Decimal("0.90")),
        "binance": ServiceCost("binance", "Binance", "crypto", INR(22),
                               Decimal("0.74"), Decimal("0.14"), Decimal("0.80")),
    } if services is None else services
    return Catalog(services, packs)


# ------------------------------------------------------------------ ladder
def test_ladder_requires_strictly_increasing_rungs():
    with pytest.raises(ValueError):
        PriceLadder((INR(10), INR(10)))
    with pytest.raises(ValueError):
        PriceLadder((INR(20), INR(10)))
    with pytest.raises(ValueError):
        PriceLadder(())


def test_snap_up_never_rounds_down():
    assert PRICE_LADDER.snap_up(INR("15.01")) == INR(18)
    assert PRICE_LADDER.snap_up(INR(15)) == INR(15)
    assert PRICE_LADDER.snap_up(INR("14.99")) == INR(15)


def test_snap_up_extends_past_the_top_rung():
    assert PRICE_LADDER.snap_up(INR(600)) == INR(600)
    assert PRICE_LADDER.snap_up(INR(501)) == INR(600)


def test_snap_down_and_nearest():
    assert PRICE_LADDER.snap_down(INR("26.00")) == INR(25)
    assert PRICE_LADDER.snap_down(INR(5)) == INR(15)  # nothing below -> floor
    assert PRICE_LADDER.nearest(INR("26.00")) == INR(25)
    assert PRICE_LADDER.nearest(INR("27.00")) == INR(28)


def test_membership():
    assert INR(25) in PRICE_LADDER
    assert INR(26) not in PRICE_LADDER


def test_prices_always_land_on_a_rung():
    pricer = Pricer(cat())
    for advice in pricer.price_book():
        assert advice.gross_price in PRICE_LADDER, advice.gross_price


# ---------------------------------------------------------------- pricing
def test_price_clears_break_even_with_buffer():
    pricer = Pricer(cat(), safety_buffer=rate("0.10"))
    for advice in pricer.price_book():
        assert advice.gross_price > advice.break_even
        assert advice.econ.is_profitable


def test_price_is_never_below_the_ladder_floor():
    cheap = cat({"line": ServiceCost("line", "Line", "messaging", INR(10),
                                     Decimal("0.99"), Decimal("0.01"), Decimal("0.95"))})
    advice = Pricer(cheap).price(cheap.get("line"))
    assert advice.gross_price >= PRICE_LADDER.rungs[0]


def test_higher_risk_services_price_higher():
    pricer = Pricer(cat())
    book = {a.service.slug: a for a in pricer.price_book()}
    # Binance is the most expensive AND the least reliable -> highest price.
    assert book["binance"].gross_price > book["whatsapp"].gross_price
    assert book["whatsapp"].gross_price >= book["telegram"].gross_price


def test_target_margin_strategy_hits_the_target():
    pricer = Pricer(cat(), strategy=Strategy.TARGET_MARGIN, target_margin=rate("0.40"))
    for advice in pricer.price_book():
        # Ladder snapping can only push margin up, never below target.
        assert advice.econ.gross_margin_ratio >= rate("0.40") - Decimal("0.005")


def test_markup_strategy_is_cost_relative():
    low = Pricer(cat(), strategy=Strategy.MARKUP_ON_COST, markup=rate("0.5"))
    high = Pricer(cat(), strategy=Strategy.MARKUP_ON_COST, markup=rate("2.0"))
    for slug in ("telegram", "whatsapp", "binance"):
        assert high.price(cat().get(slug)).gross_price >= low.price(cat().get(slug)).gross_price


def test_fixed_spread_gives_flat_absolute_profit():
    pricer = Pricer(cat(), strategy=Strategy.FIXED_SPREAD, fixed_spread=INR(10))
    book = pricer.price_book()
    assert len(book) == 3
    assert all(a.gross_price >= a.break_even for a in book)


def test_safety_buffer_raises_prices():
    tight = Pricer(cat(), safety_buffer=rate("0.0"))
    loose = Pricer(cat(), safety_buffer=rate("0.50"))
    for slug in ("telegram", "binance"):
        assert loose.price(cat().get(slug)).gross_price >= tight.price(cat().get(slug)).gross_price


def test_worse_gateway_economics_raise_break_even():
    cheap = Pricer(cat(), fees=FeeModel(gateway_rate=Decimal("0.00")))
    dear = Pricer(cat(), fees=FeeModel(gateway_rate=Decimal("0.05"), gateway_fixed=INR(3)))
    assert dear.price(cat().get("telegram")).break_even > cheap.price(cat().get("telegram")).break_even


def test_gst_registration_raises_break_even():
    unregistered = Pricer(cat(), fees=FeeModel(gst_rate=Decimal(0)))
    registered = Pricer(cat(), fees=FeeModel(gst_rate=Decimal("0.18"), gst_inclusive=True))
    assert registered.price(cat().get("telegram")).break_even > \
        unregistered.price(cat().get("telegram")).break_even


# --------------------------------------------------------------- portfolio
def test_portfolio_summary_counts_correctly():
    summary = Pricer(cat()).portfolio_summary()
    assert summary["services"] == 3
    assert summary["profitable"] + summary["loss_making"] == 3


def test_loss_makers_are_identified():
    # A service that cannot possibly work: expensive and almost never delivers.
    hopeless = cat({
        "dead": ServiceCost("dead", "Dead", "x", INR(50), Decimal("0.05"),
                            Decimal("0.90"), Decimal("0.0")),
    })
    pricer = Pricer(hopeless)
    losses = pricer.loss_makers()
    assert len(losses) == 1
    assert losses[0].service.slug == "dead"
    # Its break-even sits above the top rung: it cannot be sold at a sane price.
    assert losses[0].break_even > PRICE_LADDER.rungs[-1]
    assert pricer.is_unviable(losses[0])


def test_bonus_pack_lowers_every_break_even():
    plain = Pricer(cat())
    bonus = Pricer(cat(packs=(WalletPack("Pro", INR(1000), INR(1150)),)))
    for slug in ("telegram", "whatsapp", "binance"):
        assert bonus.price(cat().get(slug)).break_even < plain.price(cat().get(slug)).break_even


def test_unknown_service_raises_a_helpful_error():
    pricer = Pricer(cat())
    from uotpbot.catalog import CatalogError
    with pytest.raises(CatalogError, match="unknown service"):
        pricer.price_book(["instagram"])


def test_empty_catalog_summary():
    assert Pricer(cat({})).portfolio_summary() == {"services": 0}


# --------------------------------------------------------- bundled data file
def test_bundled_catalog_loads_with_real_prices():
    catalog = load_catalog()
    assert len(catalog) >= 30
    # The real floor on uotp.in is Rs.10, not the advertised Rs.2.
    assert catalog.cheapest_price() == INR(10)
    assert catalog.get("telegram").list_price == INR(10)
    assert catalog.get("whatsapp").list_price == INR(12)
    assert catalog.get("google").list_price == INR(15)
    assert catalog.get("binance").list_price == INR(22)


def test_no_service_is_priced_at_the_advertised_two_rupees():
    """The whole reason this catalogue exists."""
    catalog = load_catalog()
    for service in catalog.services():
        assert service.list_price >= INR(10), f"{service.slug} priced below the Rs.10 floor"


def test_sticker_price_applies_the_minimum_charge():
    catalog = load_catalog()
    for service in catalog.services():
        assert catalog.sticker_price(service.slug) >= catalog.min_charge


def test_best_pack_is_the_pro_pack():
    catalog = load_catalog()
    best = catalog.best_pack()
    assert best is not None and best.label == "Pro"
    # 1000/1150 = 0.869565...: every purchase is ~13% cheaper in real money.
    assert best.multiplier < Decimal("0.87")


def test_slug_lookup_is_forgiving():
    catalog = load_catalog()
    assert catalog.get("WhatsApp").slug == "whatsapp"
    assert catalog.get(" TWITTER ").slug == "twitter"
    assert "whatsapp" in catalog
