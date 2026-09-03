"""Independent validation of the closed-form economics.

``monte_carlo`` simulates orders without using ``OrderEconomics`` at all. If
the analytic expectation in economics.py is correct, the two must agree within
Monte Carlo noise. This is the strongest check in the suite: it validates the
math against a second, independently written implementation.
"""

from decimal import Decimal

import pytest

from uotpbot.catalog import ServiceCost, load_catalog
from uotpbot.cli import monte_carlo
from uotpbot.economics import FeeModel
from uotpbot.money import INR


def svc(slug, price, s, b, r="0.90"):
    return ServiceCost(slug, slug.title(), "m", INR(price), Decimal(s), Decimal(b), Decimal(r))


@pytest.mark.parametrize(
    "cost,gross",
    [
        (svc("telegram", 10, "0.94", "0.04", "0.95"), INR(20)),
        (svc("whatsapp", 12, "0.90", "0.06", "0.90"), INR(25)),
        (svc("google", 15, "0.78", "0.12", "0.80"), INR(35)),
        (svc("binance", 22, "0.74", "0.14", "0.80"), INR(50)),
        (svc("bad", 20, "0.35", "0.30", "0.50"), INR(30)),
    ],
)
def test_modelled_cost_matches_simulation(cost, gross):
    result = monte_carlo(cost, gross, orders=60_000, retry_cap=3, seed=11)
    observed = Decimal(result["observed_cost_per_delivery"])
    modelled = Decimal(result["modelled_cost_per_delivery"])
    # 60k samples gives a standard error well under 1% of the mean.
    assert abs(observed - modelled) / modelled < Decimal("0.02"), (
        f"{cost.slug}: observed {observed} vs modelled {modelled}"
    )


@pytest.mark.parametrize("cost,gross", [
    (svc("telegram", 10, "0.94", "0.04", "0.95"), INR(20)),
    (svc("binance", 22, "0.74", "0.14", "0.80"), INR(50)),
])
def test_modelled_success_rate_matches_simulation(cost, gross):
    result = monte_carlo(cost, gross, orders=60_000, retry_cap=3, seed=13)
    observed = Decimal(result["observed_success_rate"])
    modelled = Decimal(result["modelled_success_rate"])
    assert abs(observed - modelled) < Decimal("0.01")


@pytest.mark.parametrize("cost,gross", [
    (svc("telegram", 10, "0.94", "0.04", "0.95"), INR(20)),
    (svc("whatsapp", 12, "0.90", "0.06", "0.90"), INR(25)),
])
def test_modelled_profit_matches_simulation(cost, gross):
    result = monte_carlo(cost, gross, orders=60_000, retry_cap=3, seed=17)
    observed = Decimal(result["observed_profit_per_delivery"])
    modelled = Decimal(result["modelled_profit_per_delivery"])
    assert abs(observed - modelled) < Decimal("0.50")


def test_simulation_sees_more_retries_raise_success_rate():
    cost = svc("telegram", 10, "0.90", "0.05")
    caps = [
        monte_carlo(cost, INR(25), orders=20_000, retry_cap=n, seed=5)["observed_success_rate"]
        for n in (1, 2, 3, 5)
    ]
    assert caps == sorted(caps)


def test_simulation_is_deterministic_for_a_seed():
    cost = svc("telegram", 10, "0.90", "0.05")
    a = monte_carlo(cost, INR(25), orders=5_000, seed=99)
    b = monte_carlo(cost, INR(25), orders=5_000, seed=99)
    assert a == b


def test_unprofitable_price_shows_a_loss_in_simulation():
    """Selling a Rs.22 Binance number at Rs.25 must lose money, in both models."""
    cost = svc("binance", 22, "0.74", "0.14", "0.80")
    result = monte_carlo(cost, INR(25), orders=40_000, retry_cap=3, seed=3)
    assert Decimal(result["observed_profit_per_delivery"]) < 0
    assert Decimal(result["modelled_profit_per_delivery"]) < 0


def test_profitable_price_shows_a_profit_in_simulation():
    cost = svc("telegram", 10, "0.94", "0.04", "0.95")
    result = monte_carlo(cost, INR(30), orders=40_000, retry_cap=3, seed=4)
    assert Decimal(result["observed_profit_per_delivery"]) > 0


def test_wallet_multiplier_reduces_simulated_cost():
    cost = svc("telegram", 10, "0.94", "0.04", "0.95")
    plain = monte_carlo(cost, INR(25), orders=40_000, seed=8,
                        wallet_multiplier=Decimal(1))
    bonus = monte_carlo(cost, INR(25), orders=40_000, seed=8,
                        wallet_multiplier=Decimal("0.869565217391304"))
    assert Decimal(bonus["observed_cost_per_delivery"]) < Decimal(
        plain["observed_cost_per_delivery"]
    )


def test_every_bundled_service_is_consistent():
    """Full catalogue: model and simulation must agree everywhere."""
    catalog = load_catalog()
    fees = FeeModel()
    for cost in catalog.services():
        gross = catalog.sticker_price(cost.slug) * 2 + INR(5)
        result = monte_carlo(cost, gross, orders=8_000, retry_cap=3, seed=21, fees=fees)
        observed = Decimal(result["observed_cost_per_delivery"])
        modelled = Decimal(result["modelled_cost_per_delivery"])
        # Smaller sample per service, so allow wider tolerance.
        assert abs(observed - modelled) / modelled < Decimal("0.06"), (
            f"{cost.slug}: observed {observed} vs modelled {modelled}"
        )


def test_cli_simulate_does_not_crash_on_tolerance_branch():
    """Regression: cmd_simulate's 2%-tolerance check multiplied a Decimal by a
    float (``... * 0.02``), which raises TypeError. The pure ``monte_carlo``
    tests never exercised the CLI wrapper, so CI stayed green while
    ``uotpbot simulate`` crashed on every run. This pins that the command
    completes (returning 0 for agreement or 1 for a mismatch) instead of
    raising.
    """
    import argparse

    from uotpbot.cli import cmd_simulate

    args = argparse.Namespace(
        service="google", orders=4000, retry_cap=3, seed=7,
        prices=None,
        gateway_rate="0.02", gateway_fixed=0, fee_gst="0.18",
        gst_rate="0", gst_exclusive=False, chargeback_rate="0",
        strategy="target_margin", target_margin="0.35", safety_buffer="0",
    )
    rc = cmd_simulate(args)
    assert rc in (0, 1)  # completes; the crash we guarded raises instead
