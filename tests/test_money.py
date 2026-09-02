"""Money must be exact. These tests exist to catch float contamination."""

from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

import pytest

from uotpbot.money import INR, Money, Rate, quantize_money, rate, split_amount


def test_paise_is_the_only_storage():
    assert INR("12.50").paise == 1250
    assert INR(10).paise == 1000
    assert INR("0.01").paise == 1


def test_rupee_view_is_exact_two_places():
    assert INR("12.50").rupees == Decimal("12.50")
    assert Money(1).rupees == Decimal("0.01")
    assert Money(-1250).rupees == Decimal("-12.50")


def test_addition_is_exact_where_floats_are_not():
    # 0.1 + 0.2 == 0.30000000000000004 in float. Here it is exactly 0.30.
    total = INR("0.10") + INR("0.20")
    assert total.paise == 30
    assert total.rupees == Decimal("0.30")


def test_hundred_thousand_small_amounts_do_not_drift():
    total = Money.zero()
    for _ in range(100_000):
        total = total + INR("0.01")
    assert total == INR(1000)


def test_multiplication_by_int_is_exact():
    assert INR("12.34") * 3 == Money(3702)


def test_multiplication_by_ratio_is_refused():
    with pytest.raises(TypeError):
        INR(10) * Decimal("0.5")  # type: ignore[operator]


def test_scale_rounds_explicitly():
    # 10.00 * 0.025 = 0.25 paise-of-a-rupee... i.e. 25.0 paise exactly here;
    # use a value that genuinely needs a rounding decision.
    assert INR(1).scale(Decimal("0.005"), ROUND_CEILING) == Money(1)   # 0.5p -> 1p
    assert INR(1).scale(Decimal("0.004"), ROUND_DOWN) == Money(0)      # 0.4p -> 0p
    assert INR(10).scale(Decimal("0.125")) == Money(125)               # 12.5p -> 13p half-up


def test_money_only_combines_with_money():
    with pytest.raises(TypeError):
        INR(10) + 5  # type: ignore[operator]
    with pytest.raises(TypeError):
        INR(10) - Decimal("1.5")  # type: ignore[operator]


def test_division_money_by_money_gives_a_ratio():
    assert INR(15) / INR(50) == Rate("0.3")


def test_division_money_by_int_gives_rupees():
    assert INR(10) / 3 == Decimal("3.333333333333")


def test_bool_is_not_money():
    with pytest.raises(TypeError):
        Money(True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        INR(True)  # type: ignore[arg-type]


def test_subpaise_input_is_rejected_not_truncated():
    with pytest.raises(ValueError, match="whole number of paise"):
        INR("10.005")


def test_nan_and_infinity_are_rejected():
    for bad in ("nan", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            INR(bad)


def test_float_input_routes_through_repr():
    # 12.1 as a float is 12.0999999...; repr() gives '12.1' so this is exact.
    assert INR(12.1).paise == 1210


def test_rate_from_float_is_exact():
    assert rate(0.02) == Decimal("0.02")
    assert str(rate(0.02)) == "0.02"


def test_formatting_uses_indian_grouping():
    assert str(INR("1234.50")) == "\u20b91,234.50"
    assert str(INR(100000)) == "\u20b91,00,000.00"
    assert str(INR(10000000)) == "\u20b91,00,00,000.00"
    assert str(INR("999.99")) == "\u20b9999.99"
    assert str(INR("-5.00")) == "-\u20b95.00"
    assert str(Money.zero()) == "\u20b90.00"


def test_to_plain_is_locale_free():
    assert INR(100000).to_plain() == "100000.00"


def test_split_amount_sums_exactly():
    total = INR("10.00")
    for weights in ([1, 1, 1], [1, 1, 1, 1, 1, 1, 1], [3, 1], [1, 2, 3, 4]):
        parts = split_amount(total, weights)
        assert sum(parts, Money.zero()) == total, f"split lost paise for {weights}"


def test_split_amount_is_proportional():
    parts = split_amount(INR(100), [1, 3])
    assert parts == [INR(25), INR(75)]


def test_split_amount_handles_negatives():
    parts = split_amount(-INR("10.00"), [1, 1, 1])
    assert sum(parts, Money.zero()) == -INR("10.00")


def test_split_amount_rejects_zero_weights():
    with pytest.raises(ValueError):
        split_amount(INR(10), [0, 0])


def test_quantize_money_is_idempotent():
    m = INR("12.34")
    assert quantize_money(m) is m or quantize_money(m) == m


def test_zero_and_negative_helpers():
    assert Money.zero().is_zero
    assert not Money.zero().is_negative
    assert INR(-1).is_negative
    assert not INR(1).is_zero
