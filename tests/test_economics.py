"""The economics engine. These are the tests that matter most.

Each one pins a relationship that must hold for the pricing to be trustworthy,
including an independent check that the closed-form expected cost agrees with
the optimal-stopping value of the same process.
"""

from decimal import ROUND_CEILING, Decimal

import pytest

from uotpbot.catalog import ServiceCost
from uotpbot.economics import DeliveryModel, EconomicsError, FeeModel, OrderEconomics
from uotpbot.money import INR, Money, quantize_money, rate


def svc(slug="telegram", price="10", s="0.94", b="0.04", r="0.95") -> ServiceCost:
    return ServiceCost(
        slug=slug, name=slug.title(), category="messaging",
        list_price=INR(price), otp_success_rate=Decimal(s),
        burn_rate=Decimal(b), refund_share=Decimal(r),
    )


# ------------------------------------------------------- delivery model
def test_rates_sum_coherently():
    d = DeliveryModel(Decimal("0.94"), Decimal("0.04"), Decimal("0.95"))
    assert d.silent_rate == Decimal("0.02")
    assert d.failure_rate == Decimal("0.06")
    assert d.success_rate + d.burn_rate + d.silent_rate == Decimal(1)


def test_invalid_probabilities_rejected():
    with pytest.raises(ValueError):
        DeliveryModel(Decimal("1.5"), Decimal("0"), Decimal("0"))
    with pytest.raises(ValueError):
        DeliveryModel(Decimal("0.9"), Decimal("0.2"), Decimal("0"))  # sums > 1
    with pytest.raises(ValueError):
        DeliveryModel(Decimal("0.9"), Decimal("0.05"), Decimal("0"), retry_cap=0)


def test_expected_attempts_matches_harmonic_limit():
    """With a large cap the truncated geometric must approach 1/p."""
    p = Decimal("0.90")
    d = DeliveryModel(p, Decimal("0.05"), Decimal("0.9"), retry_cap=500)
    expected = Decimal(1) / p
    assert abs(d.expected_attempts - expected) < Decimal("1e-9")


def test_expected_attempts_truncation_is_exact_for_cap_one():
    d = DeliveryModel(Decimal("0.7"), Decimal("0.2"), Decimal("0.9"), retry_cap=1)
    assert d.expected_attempts == Decimal(1)
    assert d.order_success_rate == Decimal("0.7")


def test_expected_failed_attempts_never_negative():
    for cap in range(1, 8):
        d = DeliveryModel(Decimal("0.85"), Decimal("0.10"), Decimal("0.9"), retry_cap=cap)
        assert d.expected_failed_attempts >= Decimal(0)


def test_refunds_only_cover_silent_failures():
    d = DeliveryModel(Decimal("0.80"), Decimal("0.15"), Decimal("1.0"))
    # silent = 0.05; with full refund the expected refund is 0.05 per attempt.
    assert d.refund_per_attempt_rate == Decimal("0.050")
    assert d.net_charge_per_attempt_rate == Decimal("0.950")


# ------------------------------------------------- closed form vs optimal
def test_cost_per_delivery_equals_expected_spend_over_success_rate():
    cost = svc()
    econ = OrderEconomics.for_service(cost, INR(25), retry_cap=3)
    expected = quantize_money(
        Decimal(econ.cogs.rupees) / econ.delivery.order_success_rate, ROUND_CEILING
    )
    assert econ.cost_per_successful_delivery == expected


def test_closed_form_matches_optimal_stopping_value():
    """The central correctness check.

    For a stationary process with the option to stop after any attempt, the
    value of continuing is ``net - c/p`` (derived in orders.retry_policy).
    So expected contribution must equal ``net_proceeds - c/p``, where c is the
    net cost of one attempt. If the truncated-geometric machinery in
    economics.py were wrong, this equality would break.
    """
    cost = svc(s="0.94", b="0.04", r="0.95")
    econ = OrderEconomics.for_service(
        cost, INR(25), retry_cap=10_000, wallet_multiplier=Decimal(1)
    )
    c = quantize_money(
        Decimal(cost.list_price.rupees) * econ.delivery.net_charge_per_attempt_rate
    )
    c_per_p = quantize_money(Decimal(c.rupees) / econ.delivery.success_rate, ROUND_CEILING)
    assert econ.cost_per_successful_delivery == c_per_p
    assert econ.expected_contribution == econ.net_proceeds - c_per_p


def test_cost_per_delivery_is_invariant_to_retry_cap():
    """Retrying more does NOT lower your cost per delivered OTP.

    Conditional on eventual success, the attempt count is a plain geometric,
    so E[attempts]/P(success) == 1/p for every cap. The retry cap buys a
    higher *success rate*, never a cheaper delivery. Pricing it as a cost
    lever is a category error; it is a conversion lever.
    """
    cost = svc(s="0.90", b="0.05", r="0.90", price="10")
    costs = [
        OrderEconomics.for_service(cost, INR(25), retry_cap=n).cost_per_successful_delivery
        for n in (1, 2, 3, 5, 10, 100)
    ]
    spread = max(c.paise for c in costs) - min(c.paise for c in costs)
    # Only paise-level rounding noise is permitted.
    assert spread <= 2, f"cost/delivery varied by {spread} paise across caps: {costs}"


def test_higher_retry_cap_raises_expected_contribution_per_order():
    """What the cap *does* buy: more orders end delivered, so more revenue lands."""
    cost = svc(s="0.90", b="0.05", r="0.90", price="10")
    contribs = [
        OrderEconomics.for_service(cost, INR(25), retry_cap=n).expected_contribution.paise
        for n in (1, 2, 3, 5, 10)
    ]
    assert contribs == sorted(contribs)
    assert contribs[-1] > contribs[0]


def test_unprofitable_service_does_not_improve_with_retries():
    """When net < cost/delivery, extra retries multiply the loss, not the profit."""
    cost = svc(s="0.50", b="0.30", r="0.0", price="10")
    econ = OrderEconomics.for_service(cost, INR(12), retry_cap=1)
    assert not econ.is_profitable
    more = OrderEconomics.for_service(cost, INR(12), retry_cap=6)
    assert more.expected_contribution < econ.expected_contribution


# ------------------------------------------------------------- the Rs.2 trap
def test_pricing_off_the_advertised_two_rupee_price_loses_money():
    """The specific error this package exists to prevent.

    uotp.in advertises "from Rs.2". The real charge for Telegram is Rs.10.
    A bot that believes Rs.2 and sells at Rs.5 loses money on every order.
    """
    real = svc(slug="telegram", price="10", s="0.94", b="0.04")
    naive_sticker = INR(2)
    sell = INR(5)

    honest = OrderEconomics.for_service(real, sell, sticker_price=INR(10))
    naive = OrderEconomics.for_service(real, sell, sticker_price=naive_sticker)

    assert honest.expected_contribution.is_negative
    assert naive.expected_contribution.paise > 0  # looks fine on paper
    # The gap between the two models is the size of the mistake.
    assert (naive.expected_contribution - honest.expected_contribution).paise > 700


def test_provider_floor_is_applied_by_for_service():
    cost = svc(price="2")  # pretend a Rs.2 sticker
    econ = OrderEconomics.for_service(cost, INR(20), min_charge=INR(10))
    assert econ.sticker_price == INR(10)


# ------------------------------------------------------------------ wallet
def test_bonus_pack_reduces_real_cost():
    cost = svc(price="10")
    plain = OrderEconomics.for_service(cost, INR(25), wallet_multiplier=Decimal(1))
    # Rs.1000 -> Rs.1150 credit: 1000/1150 = 0.869565
    bonus = OrderEconomics.for_service(
        cost, INR(25), wallet_multiplier=Decimal("0.869565217391304")
    )
    assert bonus.cogs < plain.cogs
    saving = plain.cogs - bonus.cogs
    assert saving.paise > 0
    # ~13% off a ~Rs.10.7 cost is roughly Rs.1.3
    assert INR(1) <= saving <= INR(2)


def test_multiplier_of_one_is_identity():
    cost = svc()
    assert OrderEconomics.for_service(cost, INR(25)).cogs == OrderEconomics.for_service(
        cost, INR(25), wallet_multiplier=Decimal(1)
    ).cogs


# --------------------------------------------------------------------- fees
def test_gateway_fee_includes_its_own_gst():
    f = FeeModel(gateway_rate=Decimal("0.02"), gateway_fixed=INR(2), fee_gst_rate=Decimal("0.18"))
    fee = f.gross_fee(INR(100))
    # (2 + 2) * 1.18 = 4.72
    assert fee == INR("4.72")


def test_gst_inclusive_splits_revenue_and_liability():
    f = FeeModel(gateway_rate=Decimal(0), gateway_fixed=Money(0),
                 fee_gst_rate=Decimal(0), gst_rate=Decimal("0.18"), gst_inclusive=True)
    gross = INR(118)
    assert f.output_gst(gross) == INR(18)
    assert f.net_proceeds(gross) == INR(100)


def test_gst_exclusive_keeps_the_whole_price_as_revenue():
    f = FeeModel(gateway_rate=Decimal(0), gateway_fixed=Money(0), fee_gst_rate=Decimal(0),
                 gst_rate=Decimal("0.18"), gst_inclusive=False)
    gross = INR(100)
    assert f.output_gst(gross) == INR(18)
    assert f.net_proceeds(gross) == INR(100)


def test_chargebacks_cost_the_full_gross():
    f = FeeModel(gateway_rate=Decimal(0), gateway_fixed=Money(0), fee_gst_rate=Decimal(0),
                 chargeback_rate=Decimal("0.10"))
    econ = OrderEconomics.for_service(svc(), INR(25), fees=f)
    # 10% of delivered orders, each losing the whole Rs.25
    expected = quantize_money(
        Decimal(25) * econ.delivery.order_success_rate * Decimal("0.1"), ROUND_CEILING
    )
    assert econ.chargeback_loss == expected


# ---------------------------------------------------------------- break-even
def test_break_even_price_actually_breaks_even():
    """Solving for break-even and then pricing at it must yield ~zero profit."""
    cost = svc(s="0.88", b="0.07", r="0.85", price="14")
    econ = OrderEconomics.for_service(cost, INR(30), retry_cap=3)
    be = econ.break_even_gross()
    at_be = OrderEconomics.for_service(cost, be, retry_cap=3)
    # Rounding to paise means it can land a hair either side of zero, but it
    # must be within one rupee and must not be a real loss.
    assert at_be.expected_contribution.paise >= -100
    assert abs(at_be.expected_contribution.paise) <= 100
    # And one paise below break-even must actually lose.
    below = OrderEconomics.for_service(cost, be - Money(1), retry_cap=3)
    assert below.expected_contribution.paise < at_be.expected_contribution.paise


def test_break_even_rises_with_worse_delivery():
    good = OrderEconomics.for_service(svc(s="0.95", b="0.03"), INR(25)).break_even_gross()
    bad = OrderEconomics.for_service(svc(s="0.60", b="0.25"), INR(25)).break_even_gross()
    assert bad > good


def test_break_even_impossible_when_fees_eat_everything():
    f = FeeModel(gateway_rate=Decimal("1.0"), gateway_fixed=Money(0), fee_gst_rate=Decimal(0))
    econ = OrderEconomics.for_service(svc(), INR(25), fees=f)
    with pytest.raises(EconomicsError, match="no price can ever break even"):
        econ.break_even_gross()


def test_gst_plus_gateway_can_also_make_break_even_impossible():
    # 18% GST inside the price leaves 84.7%; a 90% gateway rate pushes keep < 0.
    f = FeeModel(gateway_rate=Decimal("0.90"), gst_rate=Decimal("0.18"), gst_inclusive=True)
    econ = OrderEconomics.for_service(svc(), INR(25), fees=f)
    assert econ.fees.keep_ratio < Decimal(0)
    with pytest.raises(EconomicsError):
        econ.break_even_gross()


def test_required_margin_is_achievable():
    cost = svc(s="0.90", b="0.05", price="12")
    target = rate("0.40")
    probe = OrderEconomics.for_service(cost, Money(0), retry_cap=3)
    gross = probe.required_gross_for_margin(target)
    achieved = OrderEconomics.for_service(cost, gross, retry_cap=3)
    # Ceiling rounding means we land at or above the target, never below.
    assert achieved.gross_margin_ratio >= target
    assert achieved.gross_margin_ratio < target + Decimal("0.02")


def test_impossible_margin_target_raises():
    probe = OrderEconomics.for_service(svc(), Money(0))
    with pytest.raises(EconomicsError, match="exceeds the maximum"):
        probe.required_gross_for_margin(rate("0.99"))


def test_break_even_success_rate_is_consistent():
    """Pricing at the break-even success rate must give ~zero profit."""
    cost = svc(s="0.90", b="0.05", r="0.90", price="12")
    econ = OrderEconomics.for_service(cost, INR(22), retry_cap=3)
    p_be = econ.break_even_success_rate()
    assert Decimal(0) < p_be < Decimal(1)
    # Just below the break-even rate loses money; just above it earns.
    worse = cost.with_overrides(
        otp_success_rate=max(p_be - Decimal("0.05"), Decimal("0.01")),
        burn_rate=Decimal("0.01"),
    )
    better = cost.with_overrides(
        otp_success_rate=min(p_be + Decimal("0.05"), Decimal("0.98")),
        burn_rate=Decimal("0.01"),
    )
    assert OrderEconomics.for_service(worse, INR(22), retry_cap=3).expected_contribution.paise < 0
    assert OrderEconomics.for_service(better, INR(22), retry_cap=3).expected_contribution.paise > 0


def test_zero_success_rate_has_no_finite_cost():
    cost = ServiceCost(slug="dead", name="Dead", category="x", list_price=INR(10),
                       otp_success_rate=Decimal(0), burn_rate=Decimal("1.0"))
    econ = OrderEconomics.for_service(cost, INR(25), retry_cap=2)
    with pytest.raises(EconomicsError):
        econ.cost_per_successful_delivery


def test_summary_is_json_safe():
    econ = OrderEconomics.for_service(svc(), INR(25))
    summary = econ.summary()
    import json
    json.dumps(summary)
    assert summary["service"] == "telegram"
    assert summary["profitable"] is True


def test_perfect_success_rate_does_not_crash():
    """q == 0 exercises Decimal's 0**0, which raises unless handled."""
    cost = ServiceCost(slug="perfect", name="Perfect", category="m", list_price=INR(10),
                       otp_success_rate=Decimal(1), burn_rate=Decimal(0),
                       refund_share=Decimal("0.9"))
    econ = OrderEconomics.for_service(cost, INR(25), retry_cap=3)
    assert econ.expected_attempts == Decimal(1)
    assert econ.delivery.order_success_rate == Decimal(1)
    # One attempt, no failures, no refunds: cost is exactly the sticker price.
    assert econ.cost_per_successful_delivery == INR(10)
