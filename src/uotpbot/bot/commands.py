"""Bot command handling, independent of any chat transport.

Every handler takes plain strings and returns plain text, so the whole
conversation layer is unit-testable without a Telegram connection. The
transport adapter in ``telegram.py`` is a thin shell over this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..catalog import Catalog
from ..economics import EconomicsError
from ..engine import BotEngine
from ..ledger import Ledger
from ..money import Money
from ..pricing import Pricer

__all__ = ["CommandRouter", "HELP_TEXT"]

HELP_TEXT = """UOTP bot

/buy <service>        Buy a number and wait for the OTP
/price <service>      Price and margin for one service
/list [category]      Services you can buy
/wallet               Your balance
/report               Revenue, cost and profit (owner only)
/status               Wallet and ledger health (owner only)
/help                 This message

Payment is collected before a number is bought. If no OTP arrives the order is
refunded automatically."""


@dataclass(slots=True)
class Reply:
    """A handler's response."""

    text: str
    ok: bool = True


class CommandRouter:
    """Maps commands onto the engine. No I/O, no globals, no transport."""

    def __init__(
        self,
        engine: BotEngine,
        catalog: Catalog,
        pricer: Pricer,
        ledger: Ledger,
        *,
        owner_id: str = "",
        allowed_users: tuple[str, ...] = (),
        balances: Optional[dict[str, Money]] = None,
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.pricer = pricer
        self.ledger = ledger
        self.owner_id = owner_id
        self.allowed_users = allowed_users
        #: Customer wallets. A real deployment backs this with the database;
        #: keeping it injected means the router stays testable.
        self.balances: dict[str, Money] = balances if balances is not None else {}

    # -- helpers ---------------------------------------------------------
    def balance_of(self, user_id: str) -> Money:
        return self.balances.get(user_id, Money.zero())

    def credit(self, user_id: str, amount: Money) -> Money:
        """Add funds to a customer wallet (called after a payment confirms)."""
        if amount.is_negative or amount.is_zero:
            raise ValueError("credit amount must be positive")
        self.balances[user_id] = self.balance_of(user_id) + amount
        return self.balances[user_id]

    def _authorised(self, user_id: str) -> bool:
        return not self.allowed_users or user_id in self.allowed_users

    def _is_owner(self, user_id: str) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id

    # -- dispatch --------------------------------------------------------
    def handle(self, user_id: str, text: str) -> Reply:
        """Route one incoming message."""
        text = (text or "").strip()
        if not text.startswith("/"):
            return Reply(HELP_TEXT, ok=False)
        parts = text.split()
        command, args = parts[0][1:].lower(), parts[1:]

        if not self._authorised(user_id):
            return Reply("You are not authorised to use this bot.", ok=False)

        handlers: dict[str, Callable[[str, list[str]], Reply]] = {
            "help": self.cmd_help,
            "start": self.cmd_help,
            "list": self.cmd_list,
            "price": self.cmd_price,
            "buy": self.cmd_buy,
            "wallet": self.cmd_wallet,
            "report": self.cmd_report,
            "status": self.cmd_status,
        }
        handler = handlers.get(command)
        if handler is None:
            return Reply(f"Unknown command /{command}. Try /help.", ok=False)
        try:
            return handler(user_id, args)
        except EconomicsError as exc:
            return Reply(f"Cannot price that right now: {exc}", ok=False)
        except Exception as exc:  # never leak a stack trace into a chat
            return Reply(f"Something went wrong: {type(exc).__name__}: {exc}", ok=False)

    # -- handlers --------------------------------------------------------
    def cmd_help(self, user_id: str, args: list[str]) -> Reply:
        return Reply(HELP_TEXT)

    def cmd_wallet(self, user_id: str, args: list[str]) -> Reply:
        return Reply(f"Balance: {self.balance_of(user_id)}")

    def cmd_list(self, user_id: str, args: list[str]) -> Reply:
        category = args[0].lower() if args else None
        rows = [
            f"{s.name} - {self.pricer.price(s).gross_price}"
            for s in self.catalog.services()
            if category is None or s.category == category
        ]
        if not rows:
            return Reply(f"No services in category {category!r}.", ok=False)
        return Reply("Available services:\n" + "\n".join(rows))

    def cmd_price(self, user_id: str, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /price <service>", ok=False)
        cost = self.catalog.get(args[0])
        advice = self.pricer.price(cost)
        econ = advice.econ
        return Reply(
            f"{cost.name}: {advice.gross_price}\n"
            f"  break-even {advice.break_even}\n"
            f"  margin {econ.gross_margin_ratio:.0%}\n"
            f"  delivered in {econ.delivery.order_success_rate:.0%} of orders"
        )

    def cmd_buy(self, user_id: str, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /buy <service>", ok=False)
        slug = args[0]
        if not self.catalog.has(slug):
            return Reply(f"Unknown service {slug!r}. Try /list.", ok=False)

        price, _ = self.engine.quote(slug)
        balance = self.balance_of(user_id)
        if balance.paise < price.paise:
            short = price - balance
            return Reply(
                f"{slug} costs {price}; your balance is {balance}. "
                f"Top up at least {short} more.",
                ok=False,
            )

        # Debit first: if the purchase then fails, the refund path restores it.
        self.balances[user_id] = balance - price
        result = self.engine.fulfil(user_id, slug)
        if result.success:
            return Reply(
                f"OTP for {slug}: {result.otp}\nNumber: {result.phone}\n"
                f"Charged {price}. Remaining balance {self.balance_of(user_id)}."
            )
        # Failed: put the money back so the customer is never out of pocket.
        self.balances[user_id] = self.balance_of(user_id) + result.refunded
        return Reply(
            f"No OTP arrived for {slug}. Refunded {result.refunded}; "
            f"balance {self.balance_of(user_id)}.",
            ok=False,
        )

    def cmd_report(self, user_id: str, args: list[str]) -> Reply:
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        pnl = self.ledger.profit_and_loss()
        lines = [f"{k.replace('_', ' ')}: {v}" for k, v in pnl.as_dict().items()]
        return Reply("Profit and loss\n" + "\n".join(lines))

    def cmd_status(self, user_id: str, args: list[str]) -> Reply:
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        status = self.engine.status()
        pnl = status["pnl"]
        return Reply(
            f"Provider: {status['provider']}\n"
            f"Provider wallet: {status['provider_wallet']}\n"
            f"Ledger wallet: {status['ledger_wallet']}\n"
            f"Net profit: {pnl['net_profit']}\n"
            f"Revenue: {pnl['revenue']}  COGS: {pnl['cogs']}"
        )
