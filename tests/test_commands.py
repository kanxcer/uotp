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
    assert "UOTP bot" in reply.text


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
