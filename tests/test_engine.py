"""End-to-end fulfilment: provider + ledger + pricing working together."""

from decimal import Decimal

import pytest

from uotpbot.catalog import Catalog, ServiceCost, WalletPack, load_catalog
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import CASH, COGS, OWNER, SALES, WALLET, Ledger
from uotpbot.money import INR, Money
from uotpbot.orders import OrderState
from uotpbot.pricing import Pricer
from uotpbot.provider.base import AuthError, NumberUnavailable
from uotpbot.provider.mock import MockProvider, MockOutcome


@pytest.fixture()
def stack():
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
        balance=INR(5000), seed=42,
    )
    engine = BotEngine(
        catalog, provider, ledger, pricer,
        config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0, poll_interval=0.01),
    )
    yield engine, ledger, provider, catalog
    ledger.close()


def test_successful_order_books_a_profit(stack):
    engine, ledger, provider, _ = stack
    provider.force_next(MockOutcome("success", otp="482193"))
    result = engine.fulfil("cust1", "telegram")
    assert result.success
    assert result.otp == "482193"
    assert result.order.state is OrderState.DELIVERED
    assert result.profit.paise > 0
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.revenue.paise > 0
    assert pnl.cogs == INR(10)


def test_failed_order_refunds_the_customer(stack):
    engine, ledger, provider, _ = stack
    provider.set_success_rate("telegram", 0.0)
    result = engine.fulfil("cust2", "telegram")
    assert not result.success
    assert result.refunded == result.order.gross_price
    assert result.order.state is OrderState.REFUNDED
    ledger.verify()
    # Revenue is fully reversed; the number cost stays on the books.
    assert ledger.balance(SALES) == Money.zero()


def test_ledger_balances_across_many_orders(stack):
    engine, ledger, provider, _ = stack
    delivered = 0
    for i in range(25):
        provider.force_next(
            MockOutcome("success", otp=f"{100000 + i}") if i % 4 else MockOutcome("silent")
        )
        r = engine.fulfil(f"c{i}", "telegram")
        delivered += bool(r.success)
    ledger.verify()
    assert delivered > 0
    pnl = ledger.profit_and_loss()
    assert pnl.revenue.paise >= 0


def test_realized_profit_matches_the_ledger(stack):
    """The engine's per-order profit must reconcile with the books."""
    engine, ledger, provider, _ = stack
    provider.force_next(MockOutcome("success", otp="111111"))
    result = engine.fulfil("cust3", "telegram")
    ledger.verify()
    pnl = ledger.profit_and_loss()
    # Single order, so ledger net profit equals the order's profit.
    assert pnl.net_profit == result.profit


def test_refund_on_failure_is_a_real_loss(stack):
    """A failed order costs the number and the sale. Both must appear."""
    engine, ledger, provider, _ = stack
    provider.set_success_rate("binance", 0.0)
    result = engine.fulfil("cust4", "binance")
    assert not result.success
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.net_profit.is_negative


def test_quote_is_available_before_purchase(stack):
    engine, _, _, _ = stack
    price, econ = engine.quote("telegram")
    assert price.paise > 0
    assert econ.is_profitable


def test_unknown_service_raises(stack):
    engine, _, _, _ = stack
    from uotpbot.catalog import CatalogError
    with pytest.raises(CatalogError):
        engine.fulfil("c", "myspace")


def test_engine_stops_when_out_of_stock(stack):
    engine, ledger, provider, _ = stack

    class NoStock(MockProvider):
        def buy_number(self, service, country="in", *, idempotency_key=None):
            raise NumberUnavailable("none left")

    engine.provider = NoStock({})
    result = engine.fulfil("c", "telegram")
    assert not result.success
    assert any("no stock" in n for n in result.order.notes)
    ledger.verify()


def test_engine_survives_an_auth_failure_on_balance(stack):
    engine, ledger, provider, _ = stack

    class BadAuth(MockProvider):
        def get_balance(self):
            raise AuthError("rejected")

    engine.provider = BadAuth({})
    result = engine.fulfil("c", "telegram")
    assert not result.success
    assert "could not verify wallet balance" in result.message


def test_topup_is_recorded_when_the_wallet_is_empty(stack):
    engine, ledger, provider, _ = stack
    provider.set_balance(Money.zero())
    provider.force_next(MockOutcome("success", otp="654321"))
    engine.fulfil("c", "telegram")
    ledger.verify()
    assert ledger.balance(WALLET).paise > 0


def test_status_reports_wallet_and_pnl(stack):
    engine, _, provider, _ = stack
    provider.force_next(MockOutcome("success", otp="123123"))
    engine.fulfil("c", "telegram")
    status = engine.status()
    assert status["provider"] == "mock"
    assert "pnl" in status
    assert status["ledger_wallet"] != "0.00"


def test_bundled_catalog_runs_end_to_end():
    """Smoke test against the real price file, not a fixture."""
    catalog = load_catalog()
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(10_000), seed=7,
    )
    engine = BotEngine(
        catalog, provider, ledger, pricer,
        config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0, poll_interval=0.01),
    )
    results = [engine.fulfil(f"c{i}", slug) for i, slug in
               enumerate(["telegram", "whatsapp", "google", "binance"])]
    ledger.verify()
    assert any(r.success for r in results)
    ledger.close()


def test_opening_balance_is_recorded_as_owner_capital(stack):
    """Without this the ledger wallet goes negative and profit is wrong."""
    engine, ledger, provider, _ = stack
    provider.set_balance(INR(5000))
    gap = engine.open_books()
    assert gap == INR(5000)
    ledger.verify()
    assert ledger.balance(WALLET) == INR(5000)
    assert ledger.balance(OWNER) == INR(5000)
    # Owner capital, not revenue: profit must be unaffected.
    assert ledger.profit_and_loss().net_profit == Money.zero()


def test_open_books_is_idempotent(stack):
    engine, ledger, provider, _ = stack
    provider.set_balance(INR(2000))
    engine.open_books()
    assert engine.open_books() == Money.zero()
    ledger.verify()
    assert ledger.balance(WALLET) == INR(2000)


def test_unrecorded_provider_spend_is_reconciled(stack):
    """Books ahead of the provider means spend happened outside the bot."""
    engine, ledger, provider, _ = stack
    ledger.post(WALLET, OWNER, INR(100), ref="seed")
    provider.set_balance(INR(40))
    engine.open_books()
    ledger.verify()
    assert ledger.balance(WALLET) == INR(40)
    assert ledger.balance(COGS) == INR(60)


def test_wallet_never_goes_negative_on_a_real_order(stack):
    engine, ledger, provider, _ = stack
    provider.set_balance(INR(500))
    provider.force_next(MockOutcome("success", otp="999888"))
    engine.fulfil("c", "telegram")
    ledger.verify()
    assert ledger.balance(WALLET).paise >= 0
    pnl = ledger.profit_and_loss()
    # Accounting equation: assets = equity + retained earnings. Omitting the
    # profit term is the classic way this check gets written wrong.
    assets = ledger.balance(WALLET) + ledger.balance(CASH)
    assert assets == ledger.balance(OWNER) + pnl.net_profit
