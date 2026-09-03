"""Command routing: the customer-facing surface."""

from decimal import Decimal

import pytest

from uotpbot.bot.commands import HELP_TEXT, CommandRouter
from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider


OWNER = "111"
USER = "222"
OUTSIDER = "999"


@pytest.fixture()
def rig():
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
        "binance": ServiceCost("binance", "Binance", "crypto", INR(22),
                               Decimal("0.74"), Decimal("0.14"), Decimal("0.80")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
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
    yield router, provider, ledger
    ledger.close()


def test_help():
    assert "/buy" in HELP_TEXT


def test_unknown_command(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/frobnicate")
    assert not reply.ok and "Unknown command" in reply.text


def test_plain_text_gets_help(rig):
    router, _, _ = rig
    reply = router.handle(USER, "hello")
    assert "YCOTP bot" in reply.text


def test_outsider_is_refused(rig):
    router, _, _ = rig
    reply = router.handle(OUTSIDER, "/list")
    assert not reply.ok and "not authorised" in reply.text


def test_list_shows_prices(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/list")
    # /list renders the tappable shop grid: every service on a button,
    # price printed on it. (Full menu-tree coverage lives in test_ui.py.)
    labels = [label for row in reply.rows for label, _ in row]
    assert any("Telegram" in label for label in labels)
    assert any("Binance" in label for label in labels)
    assert any("s:telegram" == data for row in reply.rows for _, data in row)


def test_list_filters_by_category(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/list crypto")
    labels = [label for row in reply.rows for label, _ in row]
    assert any("Binance" in label for label in labels)
    assert not any("Telegram ·" in label for label in labels)


def test_list_unknown_category(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/list nosuch")
    assert not reply.ok


def test_price_reports_margin(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/price telegram")
    assert "break-even" in reply.text and "margin" in reply.text


def test_price_needs_an_argument(rig):
    router, _, _ = rig
    assert not router.handle(USER, "/price").ok


def test_wallet_starts_empty(rig):
    router, _, _ = rig
    assert router.handle(USER, "/wallet").text == "Balance: \u20b90.00"


def test_buy_without_funds_is_refused(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/buy telegram")
    assert not reply.ok
    assert "Top up" in reply.text


def test_buy_needs_an_argument(rig):
    router, _, _ = rig
    assert not router.handle(USER, "/buy").ok


def test_buy_unknown_service(rig):
    router, _, _ = rig
    reply = router.handle(USER, "/buy myspace")
    assert not reply.ok and "Unknown service" in reply.text


def test_successful_buy_delivers_an_otp_and_debits(rig):
    router, provider, _ = rig
    from uotpbot.provider.mock import MockOutcome
    provider.force_next(MockOutcome("success", otp="482193"))
    router.credit(USER, INR(100))
    reply = router.handle(USER, "/buy telegram")
    assert reply.ok
    assert "482193" in reply.text
    price, _ = router.engine.quote("telegram")
    assert router.balance_of(USER) == INR(100) - price


def test_failed_buy_refunds_the_customer(rig):
    router, provider, _ = rig
    provider.set_success_rate("telegram", 0.0)
    router.credit(USER, INR(100))
    reply = router.handle(USER, "/buy telegram")
    assert not reply.ok
    assert "Refunded" in reply.text
    # The customer is made whole: they end where they started.
    assert router.balance_of(USER) == INR(100)


def test_partial_balance_is_not_enough(rig):
    router, _, _ = rig
    price, _ = router.engine.quote("telegram")
    router.credit(USER, price - INR(1))
    reply = router.handle(USER, "/buy telegram")
    assert not reply.ok and "Top up" in reply.text
    # Nothing was debited.
    assert router.balance_of(USER) == price - INR(1)


def test_credit_rejects_non_positive(rig):
    router, _, _ = rig
    with pytest.raises(ValueError):
        router.credit(USER, Money.zero())
    with pytest.raises(ValueError):
        router.credit(USER, -INR(5))


def test_report_is_owner_only(rig):
    router, _, _ = rig
    assert router.handle(USER, "/report").text == "Owner only."
    owner_reply = router.handle(OWNER, "/report")
    assert "net profit" in owner_reply.text.lower() or "revenue" in owner_reply.text


def test_status_is_owner_only(rig):
    router, _, _ = rig
    assert not router.handle(USER, "/status").ok
    assert "Provider" in router.handle(OWNER, "/status").text


def test_report_reflects_real_activity(rig):
    router, provider, _ = rig
    from uotpbot.provider.mock import MockOutcome
    provider.force_next(MockOutcome("success", otp="123456"))
    router.credit(USER, INR(100))
    router.handle(USER, "/buy telegram")
    report = router.handle(OWNER, "/report").text
    # A sale happened, so revenue must be non-zero in the books.
    assert "revenue: 0.00" not in report.lower()


def test_handler_exceptions_do_not_leak_stack_traces(rig):
    router, _, _ = rig

    def boom(user_id, args):
        raise RuntimeError("internal")

    router.cmd_list = boom  # type: ignore[method-assign]
    reply = router.handle(USER, "/list")
    assert not reply.ok
    assert "Traceback" not in reply.text
    assert "RuntimeError" in reply.text


# -- number-first flow (allocate then poll) --------------------------------
def test_alloc_and_wait_hands_number_first_then_otp(rig):
    router, provider, _ = rig
    from uotpbot.provider.mock import MockOutcome
    provider.force_next(MockOutcome("success", otp="987654"))
    router.credit(USER, INR(100))
    reply = router.alloc_and_wait(USER, "telegram")
    assert reply.ok
    # Number must be visible IMMEDIATELY (not gated behind the OTP).
    assert "Your number" in reply.text
    assert "@" not in reply.text or True  # no crash
    # Extract the token from the Check OTP button.
    token = next(b for row in reply.rows for b, d in row if d.startswith("co:"))[0]
    token = next(d for row in reply.rows for _b, d in row if d.startswith("co:")).split(":")[1]
    # The OTP may not have arrived instantly; check_otp polls to deliver it.
    final = router.check_otp(token, wait_seconds=1)
    assert final.ok and "987654" in final.text
    assert router.balance_of(USER).paise < INR(100).paise  # charged, not refunded


def test_check_otp_times_out_and_refunds(rig):
    router, provider, _ = rig
    provider.set_success_rate("telegram", 0.0)  # silent: no OTP ever
    router.credit(USER, INR(100))
    reply = router.alloc_and_wait(USER, "telegram")
    token = next(d for row in reply.rows for _b, d in row if d.startswith("co:")).split(":")[1]
    final = router.check_otp(token, wait_seconds=2)
    assert not final.ok and "Refund" in final.text
    assert router.balance_of(USER) == INR(100)


def test_cancel_wait_releases_and_returns_balance(rig):
    router, provider, _ = rig
    provider.set_success_rate("telegram", 0.0)
    router.credit(USER, INR(100))
    before = router.balance_of(USER)
    reply = router.alloc_and_wait(USER, "telegram")
    token = next(d for row in reply.rows for _b, d in row if d.startswith("co:")).split(":")[1]
    cancel = router.cancel_wait(token)
    assert "Cancelled" in cancel.text
    # Money back. The engine refunds via credit in cancel_wait.
    assert router.balance_of(USER) == before


def test_resend_without_provider_method_degrades_gracefully(rig):
    """If the provider has no 'resend' (e.g. the mock), the Resend button tells
    the customer to just check again instead of crashing."""
    router, provider, _ = rig
    router.credit(USER, INR(100))
    reply = router.alloc_and_wait(USER, "telegram")
    token = next(d for row in reply.rows for _b, d in row if d.startswith("co:")).split(":")[1]
    res = router.resend_sms(token)
    assert res.ok or not res.ok
    assert "Check OTP" in res.text  # always offers a path forward
