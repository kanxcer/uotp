"""The ledger must balance, and profit must be derivable, never stored."""

from decimal import Decimal

import pytest

from uotpbot.ledger import (
    CASH, COGS, GATEWAY, GST, OWNER, SALES, TOPUP_FEE, WALLET,
    Account, Ledger, LedgerError, Posting,
)
from uotpbot.money import INR, Money


@pytest.fixture()
def ledger():
    led = Ledger()
    yield led
    led.close()


def test_fresh_ledger_is_empty_and_balanced(ledger):
    ledger.verify()
    assert ledger.trial_balance() == {}
    pnl = ledger.profit_and_loss()
    assert pnl.revenue == Money.zero()
    assert pnl.net_profit == Money.zero()


def test_single_posting_keeps_the_ledger_balanced(ledger):
    ledger.post(WALLET, CASH, INR(100), ref="t1")
    ledger.verify()
    assert ledger.balance(WALLET) == INR(100)
    assert ledger.balance(CASH) == -INR(100)


def test_negative_and_zero_postings_are_refused(ledger):
    with pytest.raises(LedgerError):
        ledger.post(WALLET, CASH, INR(-5), ref="bad")
    with pytest.raises(LedgerError):
        ledger.post(WALLET, CASH, Money.zero(), ref="bad")


def test_self_posting_is_refused(ledger):
    with pytest.raises(LedgerError, match="same account"):
        ledger.post(WALLET, WALLET, INR(5), ref="bad")


def test_unknown_account_is_refused(ledger):
    with pytest.raises(LedgerError, match="unknown account"):
        ledger.post(Account("asset:bitcon"), CASH, INR(5), ref="typo")


def test_balances_respect_normal_direction(ledger):
    # revenue is credit-normal: a credit increases it
    ledger.post(CASH, SALES, INR(100), ref="s1")
    assert ledger.balance(SALES) == INR(100)
    # expense is debit-normal
    ledger.post(GATEWAY, CASH, INR(2), ref="s1")
    assert ledger.balance(GATEWAY) == INR(2)


# ------------------------------------------------------------------ flows
def test_topup_records_bonus_as_owner_capital(ledger):
    ledger.record_topup(INR(1000), INR(1150), Money.zero(), ref="top1", memo="Pro")
    ledger.verify()
    assert ledger.balance(WALLET) == INR(1150)
    assert ledger.balance(CASH) == -INR(1000)
    # The Rs.150 bonus came from the owner, not from nowhere.
    assert ledger.balance(OWNER) == INR(150)
    # Assets = liabilities + equity
    assert ledger.balance(WALLET) + ledger.balance(CASH) == ledger.balance(OWNER)


def test_topup_with_rail_fee_records_the_expense(ledger):
    ledger.record_topup(INR(200), INR(200), INR(4), ref="top2")
    ledger.verify()
    assert ledger.balance(WALLET) == INR(200)
    assert ledger.balance(TOPUP_FEE) == INR(4)
    assert ledger.balance(CASH) == -INR(204)


def test_topup_rejects_crediting_less_than_paid(ledger):
    with pytest.raises(LedgerError, match="less than paid"):
        ledger.record_topup(INR(100), INR(50), Money.zero(), ref="bad")


def test_sale_splits_gst_into_a_liability_not_revenue(ledger):
    ledger.record_sale(INR(118), INR(3), INR(18), ref="s1")
    ledger.verify()
    # Revenue excludes GST but not the PSP fee -- the fee is an expense,
    # which is what makes net_profit = gross_profit - gateway meaningful.
    assert ledger.balance(SALES) == INR(100)
    assert ledger.balance(GST) == INR(18)
    assert ledger.balance(GATEWAY) == INR(3)
    assert ledger.balance(CASH) == INR(118) - INR(3)


def test_gst_never_inflates_reported_profit(ledger):
    """Collecting GST must not show up as profit."""
    ledger.record_sale(INR(118), Money.zero(), INR(18), ref="s1")
    pnl = ledger.profit_and_loss()
    assert pnl.revenue == INR(100)
    assert pnl.net_profit == INR(100)
    assert pnl.gst_collected == INR(18)


def test_sale_cannot_absorb_more_fees_than_the_price(ledger):
    with pytest.raises(LedgerError, match="cannot absorb"):
        ledger.record_sale(INR(10), INR(8), INR(5), ref="bad")
    with pytest.raises(LedgerError, match="cannot absorb"):
        ledger.record_sale(INR(10), INR(20), Money.zero(), ref="bad")


def test_full_order_lifecycle_produces_the_right_profit(ledger):
    """End-to-end: fund wallet, buy a number, sell it, read the P&L."""
    ledger.record_topup(INR(50), INR(50), Money.zero(), ref="fund")
    ledger.record_number_purchase(INR(10), ref="o1")
    ledger.record_sale(INR(20), INR(1), Money.zero(), ref="o1")
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.revenue == INR(20)
    assert pnl.cogs == INR(10)
    assert pnl.gross_profit == INR(10)
    assert pnl.gateway == INR(1)
    assert pnl.net_profit == INR(9)
    assert pnl.net_margin_ratio == Decimal("9") / Decimal("20")


def test_provider_refund_reduces_cogs(ledger):
    ledger.record_topup(INR(50), INR(50), Money.zero(), ref="fund")
    ledger.record_number_purchase(INR(12), ref="o1")
    ledger.record_number_refund(INR(12), ref="o1")
    ledger.verify()
    assert ledger.balance(COGS) == Money.zero()
    assert ledger.balance(WALLET) == INR(50)


def test_customer_refund_reduces_revenue_but_keeps_cogs(ledger):
    """A failed order costs you the number AND the sale. Both must show."""
    ledger.record_topup(INR(50), INR(50), Money.zero(), ref="fund")
    ledger.record_number_purchase(INR(12), ref="o1")
    ledger.record_sale(INR(20), Money.zero(), Money.zero(), ref="o1")
    ledger.record_customer_refund(INR(20), ref="o1")
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.revenue == Money.zero()
    assert pnl.cogs == INR(12)
    assert pnl.net_profit == -INR(12)


def test_corruption_is_detected(ledger):
    ledger.post(WALLET, CASH, INR(100), ref="t")
    # Forge an unbalanced row directly, bypassing the API.
    ledger._conn.execute(
        "INSERT INTO postings (ts, ref, account, debit_p, credit_p, memo) "
        "VALUES ('x','x','asset:wallet',500,0,'forged')"
    )
    ledger._conn.commit()
    with pytest.raises(LedgerError, match="out of balance"):
        ledger.verify()


def test_post_many_writes_every_row_of_the_batch(ledger):
    batch = [Posting("t", "r1", WALLET, CASH, INR(10)),
             Posting("t", "r1", COGS, WALLET, INR(10))]
    ledger.post_many(batch)
    ledger.verify()
    assert ledger.balance(WALLET) == Money.zero()
    assert ledger.balance(COGS) == INR(10)
    assert ledger.balance(CASH) == -INR(10)


def test_post_many_rolls_back_when_the_write_fails(ledger):
    """A half-written batch would leave the ledger unbalanced, so it must not
    be possible: force the insert to blow up and confirm nothing landed.

    ``sqlite3.Connection`` attributes are read-only, so the connection is
    swapped for a proxy rather than monkeypatched in place.
    """
    class ExplodingConn:
        def __init__(self, inner):
            self._inner = inner

        def executemany(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

        # Dunder protocol methods are looked up on the type, so __getattr__
        # never sees them; they have to be forwarded explicitly.
        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    ledger._conn = ExplodingConn(ledger._conn)
    with pytest.raises(RuntimeError):
        ledger.post_many([Posting("t", "r1", WALLET, CASH, INR(10))])
    ledger._conn = ledger._conn._inner

    assert ledger.balance(WALLET) == Money.zero()
    assert ledger.balance(CASH) == Money.zero()
    ledger.verify()


def test_invalid_posting_is_rejected_before_anything_is_written(ledger):
    with pytest.raises(LedgerError, match="same account"):
        Posting("t", "r1", WALLET, WALLET, INR(5))
    assert ledger.trial_balance() == {}


def test_order_history_is_retrievable(ledger):
    ledger.record_number_purchase(INR(10), ref="o1", memo="first")
    ledger.record_sale(INR(20), INR(1), Money.zero(), ref="o1", memo="sold")
    rows = ledger.history(limit=10)
    # purchase = 1 posting; sale with a fee = 2 postings; 2 rows each.
    assert len(rows) == 6
    assert all(r[1] == "o1" for r in rows)


def test_pnl_handles_empty_ledger(ledger):
    pnl = ledger.profit_and_loss()
    assert pnl.gross_margin_ratio == Decimal(0)
    assert pnl.net_margin_ratio == Decimal(0)
    assert pnl.as_dict()["revenue"] == "0.00"


def test_account_kind_parsing():
    assert Account("asset:cash").kind == "asset"
    assert Account("cogs:numbers").is_debit_normal
    assert not Account("revenue:sales").is_debit_normal
    assert not Account("liability:gst").is_debit_normal


def test_persistence_survives_reopen(tmp_path):
    path = tmp_path / "ledger.db"
    led = Ledger(path)
    led.record_topup(INR(500), INR(550), Money.zero(), ref="t")
    led.close()
    again = Ledger(path)
    again.verify()
    assert again.balance(WALLET) == INR(550)
    again.close()
