"""Retry policy: the decision that stops a bad order from bleeding money."""

from decimal import Decimal

import pytest

from uotpbot.catalog import ServiceCost
from uotpbot.economics import OrderEconomics
from uotpbot.money import INR, Money
from uotpbot.orders import ALLOWED, Order, OrderState, retry_policy


def econ_for(price="12", s="0.90", b="0.05", r="0.90", gross="25", cap=3, **kw):
    cost = ServiceCost("svc", "Svc", "m", INR(price), Decimal(s), Decimal(b), Decimal(r))
    return OrderEconomics.for_service(cost, INR(gross), retry_cap=cap, **kw)


# ------------------------------------------------------------- state machine
def test_happy_path_transitions():
    o = Order(customer_id="c", service="telegram", gross_price=INR(25))
    assert o.state is OrderState.CREATED
    o.transition(OrderState.PAID)
    o.transition(OrderState.PURCHASING)
    o.transition(OrderState.AWAITING_OTP)
    o.transition(OrderState.DELIVERED)
    assert o.state.is_terminal
    assert o.closed_at is not None


def test_illegal_transitions_raise():
    o = Order(customer_id="c", service="x", gross_price=INR(25))
    with pytest.raises(ValueError, match="illegal order transition"):
        o.transition(OrderState.DELIVERED)  # cannot deliver before paying


def test_terminal_states_are_final():
    o = Order(customer_id="c", service="x", gross_price=INR(25))
    o.transition(OrderState.PAID)
    o.transition(OrderState.REFUNDED)
    with pytest.raises(ValueError):
        o.transition(OrderState.DELIVERED)


def test_transition_table_is_complete():
    assert set(ALLOWED) == set(OrderState)


# -------------------------------------------------------------- retry policy
def test_retry_when_the_attempt_is_worth_more_than_it_costs():
    econ = econ_for(s="0.90")  # cheap number, likely to work
    d = retry_policy(econ, attempts_so_far=0, retry_cap=3, spent=Money.zero())
    assert d.retry
    assert d.edge.paise > 0


def test_stop_when_a_retry_costs_more_than_it_can_earn():
    # A Rs.50 number with a 20% success rate against a Rs.25 sale.
    econ = econ_for(price="50", s="0.20", b="0.70", r="0.0", gross="25", cap=5)
    d = retry_policy(econ, attempts_so_far=0, retry_cap=5, spent=Money.zero())
    assert not d.retry
    assert d.edge.paise <= 0


def test_retry_cap_is_respected():
    econ = econ_for()
    d = retry_policy(econ, attempts_so_far=3, retry_cap=3, spent=Money.zero())
    assert not d.retry
    assert "retry cap" in d.reason


def test_sunk_cost_is_ignored():
    """Money already spent must not make another bad retry look attractive."""
    econ = econ_for()
    # Rs.5 of sunk cost, comfortably inside the loss ceiling.
    cold = retry_policy(econ, attempts_so_far=1, retry_cap=5, spent=Money.zero())
    burned = retry_policy(econ, attempts_so_far=1, retry_cap=5, spent=INR(5))
    # Identical decision, because the Rs.5 is gone either way.
    assert cold.retry == burned.retry
    assert cold.edge == burned.edge
    assert cold.marginal_cost == burned.marginal_cost


def test_loss_ceiling_stops_a_runaway_order():
    """Even a +EV retry stops once the order's total spend breaches the ceiling."""
    econ = econ_for(gross="25", s="0.90")
    ceiling = econ.gross_price + econ.gross_margin
    d = retry_policy(econ, attempts_so_far=1, retry_cap=99, spent=ceiling)
    assert not d.retry
    assert "loss ceiling" in d.reason


def test_policy_is_optimal_against_brute_force_dp():
    """The myopic rule must equal the exact optimal-stopping value.

    Solved by backward induction over the remaining attempt budget, which is
    the ground truth. If the cheap ``p*net > c`` test ever diverged from the
    DP, one of them would be wrong -- they agree at every state.
    """
    for s, b, price, gross in [
        ("0.90", "0.05", "12", "25"),
        ("0.30", "0.20", "20", "22"),
        ("0.55", "0.10", "15", "30"),
        ("0.05", "0.60", "40", "20"),
    ]:
        econ = econ_for(price=price, s=s, b=b, r="0.90", gross=gross, cap=6)
        p = econ.delivery.success_rate
        c = Decimal(econ.delivery.net_charge_per_attempt_rate) * Decimal(
            econ.sticker_price.rupees
        ) * econ.wallet_multiplier
        net = Decimal(econ.net_proceeds.rupees)

        # Backward induction: V[k] = value with k attempts left.
        V = Decimal(0)
        for _ in range(6):
            continue_value = -c + p * net + (1 - p) * V
            V = max(Decimal(0), continue_value)
        should_continue = V > 0

        decision = retry_policy(econ, attempts_so_far=0, retry_cap=6, spent=Money.zero())
        assert decision.retry == should_continue, (s, b, price, gross, decision.reason)


def test_marginal_cost_ignores_the_refund_on_burned_numbers():
    """Burned numbers are charged and not refunded; silent ones are refunded."""
    all_burn = econ_for(price="10", s="0.50", b="0.50", r="1.0")
    all_silent = econ_for(price="10", s="0.50", b="0.00", r="1.0")
    d_burn = retry_policy(all_burn, attempts_so_far=0, retry_cap=3, spent=Money.zero())
    d_silent = retry_policy(all_silent, attempts_so_far=0, retry_cap=3, spent=Money.zero())
    # Burned: silent_rate 0, so nothing is ever refunded -> full Rs.10.
    # Silent: silent_rate 0.5 at full refund -> half the cost comes back.
    assert d_burn.marginal_cost == INR(10)
    assert d_silent.marginal_cost == INR(5)
    assert d_silent.marginal_cost < d_burn.marginal_cost


# ------------------------------------------------------------------- money
def test_order_tracks_net_spend():
    o = Order(customer_id="c", service="x", gross_price=INR(25))
    o.record_attempt(INR(12), "p1")
    o.record_attempt(INR(12), "p2")
    assert o.spent == INR(24)
    o.record_recovery(INR(12))
    assert o.net_spend == INR(12)
    assert o.attempts == 2


def test_realized_profit_accounts_for_refund_and_spend():
    o = Order(customer_id="c", service="x", gross_price=INR(25))
    o.record_attempt(INR(12), "p1")
    o.record_attempt(INR(12), "p2")
    o.record_recovery(INR(12))
    # Sale netted Rs.24, customer was refunded Rs.25, net spend Rs.12.
    assert o.realized_profit(INR(24), INR(25)) == -INR(13)


def test_order_serialises():
    o = Order(customer_id="c", service="telegram", gross_price=INR(25))
    d = o.to_dict()
    assert d["service"] == "telegram"
    assert d["gross_price"] == "25.00"
    assert d["state"] == "created"
