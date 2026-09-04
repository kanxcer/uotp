"""Durable refund idempotency -- the money-grade regressions.

The bug class this guards: a customer refund is REAL money leaving the
balance and a line in the books, and several independent paths (a manual
Cancel, the background auto-poller, a late Check, a retry worker, a redeploy)
can all resolve the SAME order. Any two of them agreeing to "refund" would
either double-credit the customer (we pay twice for one number) or double-post
the ledger line (reported refunds outrun the money actually returned).

Every test here asserts the exact-once property on the two things that matter:
the wallet credit and the customer-refund ledger line.
"""

from __future__ import annotations

import threading


from uotpbot.bot.commands import CommandRouter
from uotpbot.catalog import Catalog, ServiceCost
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import SALES, Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider
from uotpbot.refund import refund_once
from uotpbot.wallets import SqliteWallets

OWNER = "111"
USER = "222"


def _router():
    from decimal import Decimal

    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    })
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"telegram": catalog.sticker_price("telegram")},
                            balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(otp_timeout_seconds=1.0, poll_interval=0.01))
    wallets = SqliteWallets(":memory:")
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER), wallets=wallets)
    return router, wallets, ledger


def _refund_lines(ledger, ref):
    """Count the customer-refund ledger lines posted under ``ref``.

    A customer refund is the ONE posting that DEBITS revenue:sales, so counting
    those rows is an exact count of how many times the refund was booked.
    """
    n = 0
    for _ts, r, account, debit_p, _credit_p, _memo in ledger.history(500):
        if r == ref and account == str(SALES) and debit_p > 0:
            n += 1
    return n


def test_record_customer_refund_is_idempotent_per_ref():
    """The ledger-level guard: the same ref posts one line, no matter how many
    paths race to post it."""
    router, _, ledger = _router()
    router.ledger.record_customer_refund(INR(50), ref="o1", memo="no OTP")
    router.ledger.record_customer_refund(INR(50), ref="o1", memo="no OTP")
    router.ledger.record_customer_refund(INR(50), ref="o1", memo="no OTP")
    assert _refund_lines(ledger, "o1") == 1
    assert ledger.has_customer_refund("o1") is True
    assert ledger.has_customer_refund("never-posted") is False


def test_refund_once_posts_line_once_and_credits_once():
    """The cancel path: one call posts the line AND credits. A repeat call
    (idempotency) must not do either a second time."""
    router, wallets, ledger = _router()
    wallets.set_balance(USER, INR(60))  # as if the 40 purchase was debited
    amount = INR(40)
    router.refunds._claim(USER, amount, "tok-A", "user_cancelled", ledger_done=False)

    assert refund_once(router, USER, amount, "tok-A", "user_cancelled") is True
    assert refund_once(router, USER, amount, "tok-A", "user_cancelled") is True

    assert _refund_lines(ledger, "tok-A") == 1
    # Exactly one credit: 60 -> 100, not 60 -> 140.
    assert wallets.balance(USER) == INR(100)


def test_retry_after_a_credit_blip_does_not_repost_the_line():
    """The regression: the ledger line posts, the wallet credit fails, and a
    retry (worker/redeploy) completes the credit. The line must NOT be posted
    a second time, or reported refunds exceed the money returned."""
    router, wallets, ledger = _router()
    wallets.set_balance(USER, INR(60))  # as if the 40 purchase was debited
    amount = INR(40)
    router.refunds._claim(USER, amount, "tok-B", "user_cancelled", ledger_done=False)

    # First attempt: credit blows up after the line is posted.
    def boom(uid, amt):
        raise RuntimeError("transient wallet failure")

    real_credit = router.credit
    router.credit = boom
    try:
        assert refund_once(router, USER, amount, "tok-B", "user_cancelled") is False
    finally:
        router.credit = real_credit

    # The line was posted, the money was NOT yet returned.
    assert _refund_lines(ledger, "tok-B") == 1
    assert wallets.balance(USER) == INR(60)

    # Retry: credit succeeds; the line is not re-posted.
    assert refund_once(router, USER, amount, "tok-B", "user_cancelled") is True
    assert _refund_lines(ledger, "tok-B") == 1
    assert wallets.balance(USER) == INR(100)


def test_request_claims_exactly_once_under_a_race():
    """Two racing paths (a Cancel and a timeout poll) resolve the same order.
    Exactly one may credit -- the unique (scope, order_token) claim is what
    decides, so the winner is irrelevant to the money."""
    for _ in range(15):
        router, wallets, ledger = _router()
        wallets.set_balance(USER, INR(60))  # as if the 40 purchase was debited
        amount = INR(40)
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def worker():
            barrier.wait()
            ok, _ = router.refunds.request(USER, amount, "tok-C", "user_cancelled")
            results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _refund_lines(ledger, "tok-C") == 1
        assert wallets.balance(USER) == INR(100)  # credited exactly once
        # Exactly one caller reports it applied the credit.
        assert sorted(results) == [False, True]


def test_timeout_refund_credits_exactly_once_across_pollers():
    """The engine already booked the line on timeout; several pollers racing to
    credit it must result in one wallet credit only."""
    for _ in range(10):
        router, wallets, ledger = _router()
        wallets.set_balance(USER, INR(70))  # as if the 30 purchase was debited
        amount = INR(30)
        barrier = threading.Barrier(3)
        outcomes: list[bool] = []

        def worker():
            barrier.wait()
            outcomes.append(router.refunds.apply_timeout_refund(
                "tok-D", USER, amount, "no OTP"))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # apply_timeout_refund does not post the line itself (the engine did);
        # it only claims + credits. The credit happens exactly once.
        assert wallets.balance(USER) == INR(100)
        assert outcomes.count(True) == 1


def test_pending_and_retry_all_recover_a_stalled_refund():
    """A refund whose credit failed stays pending and is recovered by the
    background retry sweep -- and not double-applied on the way."""
    router, wallets, ledger = _router()
    wallets.set_balance(USER, INR(75))  # as if the 25 purchase was debited
    amount = INR(25)
    router.refunds._claim(USER, amount, "tok-E", "user_cancelled", ledger_done=False)

    def boom(uid, amt):
        raise RuntimeError("still down")

    real_credit = router.credit
    router.credit = boom
    try:
        refund_once(router, USER, amount, "tok-E", "user_cancelled")
    finally:
        router.credit = real_credit

    assert router.refunds.pending_count() == 1
    recovered = router.refunds.retry_all()
    assert recovered == 1
    assert router.refunds.pending_count() == 0
    assert _refund_lines(ledger, "tok-E") == 1
    assert wallets.balance(USER) == INR(100)
    # A second sweep finds nothing to do.
    assert router.refunds.retry_all() == 0
    assert wallets.balance(USER) == INR(100)


def test_cancel_after_restart_refunds_the_real_gross():
    """A restart wipes the in-memory order; only the persisted active row
    remains. A cancel then must refund the amount the purchase actually
    charged (the row's gross) -- not zero. Regression: a customer who
    cancelled after a redeploy got their number released and no money back."""
    router, wallets, ledger = _router()
    price, _ = router.engine.quote("telegram")
    wallets.set_balance(USER, INR(100))
    reply = router.alloc_and_wait(USER, "telegram")
    assert reply.ok
    assert wallets.balance(USER) == INR(100) - price
    token = next(d for row in reply.rows for _b, d in row
                 if d.startswith("co:")).split(":")[1]
    # Simulate a restart: the in-memory wait entry is gone, the DB row stays.
    router._awaiting.clear()
    cancel = router.cancel_wait(token)
    assert "Cancelled" in cancel.text, cancel.text
    row = wallets.get_refund(token)
    assert row is not None
    assert row.amount == price
    # The refund lands (immediately or via the sweep) and the customer is
    # back to their full balance -- exactly one credit.
    if router.refunds.pending_count():
        assert router.refunds.retry_all() >= 1
    assert wallets.balance(USER) == INR(100)
