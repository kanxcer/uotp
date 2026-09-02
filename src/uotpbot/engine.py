"""Order fulfilment engine.

This is the only place money actually moves. Every rupee that leaves the
wallet, comes back as a refund, or arrives from a customer is posted to the
ledger here -- there is no balance variable anywhere that is updated without a
matching double entry, so reported profit can always be rebuilt from the books.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

from .catalog import Catalog
from .economics import EconomicsError, FeeModel, OrderEconomics
from .ledger import COGS, OWNER, WALLET, Ledger
from .money import Money
from .orders import Order, OrderState, RetryDecision, retry_policy
from .pricing import Pricer
from .provider.base import (
    AuthError,
    Balance,
    InsufficientBalance,
    NumberUnavailable,
    Provider,
    ProviderError,
    PurchaseTimedOut,
)

__all__ = ["EngineConfig", "EngineError", "FulfilResult", "BotEngine"]


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Runtime knobs."""

    retry_cap: int = 3
    #: How long to wait for the OTP. UOTP refunds if nothing arrives inside
    #: its 5-minute window, so waiting longer than that forfeits the refund.
    otp_timeout_seconds: float = 290.0
    poll_interval: float = 3.0
    #: Refund the customer automatically when no OTP arrives.
    auto_refund: bool = True
    #: Top the provider wallet up to this multiple of the order price when it
    #: runs dry, so a single order is never blocked by a Rs.2 shortfall.
    topup_headroom: Decimal = Decimal("5")
    #: Country code in the provider's vocabulary. On uotp.store's dashboard
    #: API India is code "22" (dial 91); the old sms-activate-style "in"
    #: returns BAD_COUNTRY through the legacy handler.
    default_country: str = "22"


@dataclass(slots=True)
class FulfilResult:
    """What came back from an attempt to fulfil an order."""

    order: Order
    success: bool
    otp: Optional[str] = None
    phone: Optional[str] = None
    refunded: Money = Money(0)
    decision: Optional[RetryDecision] = None
    message: str = ""

    @property
    def profit(self) -> Money:
        """Real profit, after the customer refund and net number spend."""
        proceeds = self._proceeds
        return proceeds - self.refunded - self.order.net_spend

    _proceeds: Money = field(default=Money(0), repr=False)

    def summary(self) -> dict[str, object]:
        return {
            **self.order.to_dict(),
            "success": self.success,
            "otp": self.otp,
            "refunded": self.refunded.to_plain(),
            "profit": self.profit.to_plain(),
            "message": self.message,
        }


class EngineError(EconomicsError):
    """An engine invariant was violated.

    Subclasses :class:`EconomicsError` so the command layer's existing
    ``except EconomicsError`` turns it into a chat message rather than a stack
    trace. These are configuration faults, not normal order failures, so the
    caller sees the reason verbatim.
    """


class BotEngine:
    """Buys numbers, waits for OTPs, and keeps the books honest."""

    def __init__(
        self,
        catalog: Catalog,
        provider: Provider,
        ledger: Ledger,
        pricer: Pricer,
        *,
        fees: Optional[FeeModel] = None,
        config: Optional[EngineConfig] = None,
        platform_fee_fn: Optional[Callable[[Money], Money]] = None,
        fee_disclosure: str = "",
    ) -> None:
        self.catalog = catalog
        self.provider = provider
        self.ledger = ledger
        self.pricer = pricer
        self.fees = fees or pricer.fees
        self.config = config or EngineConfig()
        #: A white-label engine takes a cut of each sale. Setting this routes
        #: sales through ``record_sale_split`` so the cut lands in
        #: ``revenue:platform_fee`` instead of being folded into revenue.
        self.platform_fee_fn = platform_fee_fn
        #: The terms the owner agreed to, surfaced by /price and /help.
        self.fee_disclosure = fee_disclosure
        self._multiplier = catalog.effective_multiplier(catalog.best_pack())
        self._books_opened = False

    # -- white-label fees ------------------------------------------------
    def platform_fee(self, gross: Money) -> Money:
        """What the platform takes from a sale of ``gross``.

        Zero when this engine is not white-labelled. Called in exactly one
        place per sale, so the figure posted to the ledger and the figure
        deducted from proceeds cannot disagree.
        """
        if self.platform_fee_fn is None:
            return Money.zero()
        fee = self.platform_fee_fn(gross)
        if fee.is_negative:
            raise EngineError(f"platform fee cannot be negative (got {fee})")
        if fee.paise > gross.paise:
            raise EngineError(f"platform fee {fee} exceeds the sale {gross}")
        return fee

    # -- pricing ---------------------------------------------------------
    def quote(self, service: str) -> tuple[Money, OrderEconomics]:
        """Price and full economics for one service, right now."""
        cost = self.catalog.get(service)
        advice = self.pricer.price(cost)
        econ = OrderEconomics.for_service(
            cost,
            advice.gross_price,
            fees=self.fees,
            wallet_multiplier=self._multiplier,
            retry_cap=self.config.retry_cap,
            sticker_price=self.catalog.sticker_price(cost.slug),
        )
        return advice.gross_price, econ

    # -- wallet ----------------------------------------------------------
    def open_books(self, *, ref: str = "opening") -> Money:
        """Reconcile the ledger wallet with the provider's real balance.

        The provider wallet usually already holds credit when the bot starts.
        If that opening balance is never posted, every purchase drives the
        ledger wallet negative and reported profit is wrong by exactly that
        amount. Recording it as owner capital makes the books agree with the
        provider from the first order.

        Safe to call repeatedly: it only posts the difference.
        """
        provider_credit = self.provider.get_balance().credit
        recorded = self.ledger.balance(WALLET)
        gap = provider_credit - recorded
        if gap.paise > 0:
            self.ledger.post(
                WALLET, OWNER, gap, ref=ref, memo="opening wallet balance (owner capital)"
            )
            self._books_opened = True
        elif gap.paise < 0:
            # The provider holds less than the books say: spend happened
            # outside this bot. Surface it rather than papering over it.
            self.ledger.post(
                COGS, WALLET, -gap, ref=ref, memo="unrecorded provider spend reconciled"
            )
            self._books_opened = True
        else:
            self._books_opened = True
        return gap

    def ensure_funds(self, needed: Money, *, ref: str) -> Balance:
        """Top up the provider wallet if it cannot cover ``needed``."""
        if not self._books_opened:
            self.open_books(ref=ref)
        balance = self.provider.get_balance()
        if balance.can_afford(needed):
            return balance
        target = max(
            needed.scale(self.config.topup_headroom),
            self.catalog.min_charge,
            key=lambda m: m.paise,
        )
        shortfall = target - balance.credit
        pack = self._pick_pack(shortfall)
        rail = pack.pay.scale(pack.rail_fee_rate)
        # Real money leaves the bank and lands as wallet credit.
        self.ledger.record_topup(pack.pay, pack.credit, rail, ref=ref, memo=pack.label)
        self._apply_topup(pack.pay, pack.credit)
        return self.provider.get_balance()

    def _pick_pack(self, shortfall: Money):
        """Smallest pack that covers the shortfall, else the best-value one."""
        packs = self.catalog.packs
        if not packs:
            raise ProviderError("no wallet packs configured; cannot top up")
        covering = [p for p in packs if p.credit.paise >= shortfall.paise]
        return min(covering, key=lambda p: p.pay.paise) if covering else packs[-1]

    def _apply_topup(self, paid: Money, credited: Money) -> None:
        """Reflect a top-up on the provider side.

        The mock provider models its own balance; a real HTTP provider holds
        the wallet server-side and this is a no-op. Guarded so a provider
        without ``set_balance`` simply works.
        """
        setter = getattr(self.provider, "set_balance", None)
        if callable(setter):
            current = self.provider.get_balance()
            setter(current.credit + credited)

    # -- fulfilment ------------------------------------------------------
    def fulfil(
        self,
        customer_id: str,
        service: str,
        *,
        country: Optional[str] = None,
        gross_price: Optional[Money] = None,
    ) -> FulfilResult:
        """Run one customer order end to end.

        Assumes the customer has already paid ``gross_price`` (the bot's
        payment step lives in the transport layer). Posts the sale, buys
        numbers until the OTP arrives or retrying stops being worthwhile, then
        refunds automatically if it failed.
        """
        cost = self.catalog.get(service)
        price, econ = self.quote(service)
        gross = gross_price if gross_price is not None else price
        order = Order(
            customer_id=customer_id,
            service=service,
            gross_price=gross,
            country=country or self.config.default_country,
        )
        ref = order.order_id

        gateway_fee = self.fees.gross_fee(gross)
        gst = self.fees.output_gst(gross)
        proceeds = self.fees.net_proceeds(gross) - self.platform_fee(gross)
        if self.platform_fee_fn is None:
            self.ledger.record_sale(gross, gateway_fee, gst, ref=ref, memo=f"{service} order")
        else:
            self.ledger.record_sale_split(
                gross, gateway_fee, gst, self.platform_fee(gross),
                ref=ref, memo=f"{service} order (white-label)",
            )
        order.transition(OrderState.PAID)

        result = FulfilResult(order=order, success=False, _proceeds=proceeds)

        sticker = self.catalog.sticker_price(cost.slug)
        try:
            self.ensure_funds(sticker * (self.config.retry_cap or 1), ref=ref)
        except (AuthError, ProviderError) as exc:
            order.note(f"balance check failed: {exc}")
            return self._fail(result, order, f"could not verify wallet balance: {exc}")

        decision: Optional[RetryDecision] = None
        while True:
            decision = retry_policy(
                econ,
                attempts_so_far=order.attempts,
                retry_cap=self.config.retry_cap,
                spent=order.spent,
            )
            result.decision = decision
            if not decision.retry:
                order.note(f"stopped: {decision.reason}")
                break

            order.transition(OrderState.PURCHASING)
            try:
                alloc = self.provider.buy_number(
                    service, order.country,
                    idempotency_key=f"{ref}-{order.attempts + 1}",
                    server=getattr(cost, "server", ""),
                )
            except InsufficientBalance as exc:
                order.note(f"wallet short by {exc.shortfall}")
                break
            except NumberUnavailable as exc:
                order.note(f"no stock: {exc}")
                break
            except PurchaseTimedOut as exc:
                # Ambiguous: the number may exist and be charged. Do not retry
                # blindly -- that is how a bot pays twice for one order.
                order.note(f"ambiguous purchase, not retrying: {exc}")
                break
            except (AuthError, ProviderError) as exc:
                order.note(f"purchase error: {exc}")
                break

            order.record_attempt(alloc.charged or sticker, alloc.order_id)
            order.phone = alloc.phone
            self.ledger.record_number_purchase(
                alloc.charged or sticker, ref=ref, memo=f"{service} attempt {order.attempts}"
            )
            order.transition(OrderState.AWAITING_OTP)

            otp = self.provider.wait_for_otp(
                alloc,
                timeout_seconds=self.config.otp_timeout_seconds,
                poll_interval=self.config.poll_interval,
            )
            if otp.success and otp.code:
                order.otp = otp.code
                order.transition(OrderState.DELIVERED)
                result.success = True
                result.otp = otp.code
                result.phone = alloc.phone
                result.message = (
                    f"delivered after {order.attempts} attempt(s), "
                    f"net spend {order.net_spend}"
                )
                return result

            # Failed attempt: try to recover the charge before retrying.
            try:
                recovered = self.provider.cancel(alloc.order_id)
            except ProviderError:
                recovered = Money(0)
            if recovered.paise > 0:
                order.record_recovery(recovered)
                self.ledger.record_number_refund(
                    recovered, ref=ref, memo=f"provider refund attempt {order.attempts}"
                )
            order.transition(OrderState.PURCHASING)

        return self._fail(result, order, order.notes[-1] if order.notes else "no OTP delivered")

    def _fail(self, result: FulfilResult, order: Order, message: str) -> FulfilResult:
        """Close an order as failed, refunding the customer if configured."""
        if not order.state.is_terminal:
            order.transition(OrderState.FAILED)
        result.message = message
        if self.config.auto_refund:
            refund = order.gross_price
            self.ledger.record_customer_refund(refund, ref=order.order_id, memo="no OTP")
            result.refunded = refund
            if order.state is OrderState.FAILED:
                order.transition(OrderState.REFUNDED)
        return result

    # -- reporting -------------------------------------------------------
    def status(self) -> dict[str, object]:
        """Wallet, P&L and ledger integrity in one call."""
        pnl = self.ledger.profit_and_loss()
        try:
            balance = self.provider.get_balance()
            provider_credit = balance.credit.to_plain()
        except ProviderError as exc:
            provider_credit = f"unavailable: {exc}"
        return {
            "provider": getattr(self.provider, "name", "unknown"),
            "provider_wallet": provider_credit,
            "ledger_wallet": self.ledger.balance(WALLET).to_plain(),
            "pnl": pnl.as_dict(),
        }
