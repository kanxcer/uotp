"""Bot command handling, independent of any chat transport.

Every handler takes plain strings and returns plain text, so the whole
conversation layer is unit-testable without a Telegram connection. The
transport adapter in ``telegram.py`` is a thin shell over this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..catalog import Catalog
from ..createbot import CreateBotFlow, CreateBotResult
from ..economics import EconomicsError
from ..engine import BotEngine
from ..ledger import Ledger
from ..money import Money
from ..pricing import Pricer
from ..whitelabel import PlatformFee, SubBotRegistry

__all__ = ["CommandRouter", "HELP_TEXT"]

HELP_TEXT = """UOTP bot

/buy <service>        Buy a number and wait for the OTP
/price <service>      Price and margin for one service
/list [category]      Services you can buy
/wallet               Your balance
/report               Revenue, cost and profit (owner only)
/status               Wallet and ledger health (owner only)
/createbot            Launch your own white-label copy of this bot
/mybots               Your white-label bots and their agreed terms
/deletebot <id>       Remove one of your bots
/help                 This message

Payment is collected before a number is bought. If no OTP arrives the order is
refunded automatically."""


@dataclass(slots=True)
class Reply:
    """A handler's response.

    ``buttons`` are ``(label, callback_data)`` pairs the transport renders as
    an inline keyboard, one per row. ``rows`` is the same but pre-arranged
    into rows (the menu grid puts two service buttons side by side); when
    set it wins over ``buttons``. Keeping layout on the reply rather than in
    the transport is what lets the whole conversation be tested without
    Telegram.

    ``deferred`` marks slow work: the transport edits in the placeholder
    ``text`` immediately, then runs this callable off the event loop and
    edits the message again with the returned reply. Purchases use it --
    an OTP wait can run minutes, and blocking the poller for that long
    freezes every other customer.
    """

    text: str
    ok: bool = True
    buttons: tuple[tuple[str, str], ...] = ()
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    deferred: Optional[Callable[[str], "Reply"]] = None


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
        wallets: Optional[object] = None,
        subbots: Optional[SubBotRegistry] = None,
        platform_fee: Optional[PlatformFee] = None,
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.pricer = pricer
        self.ledger = ledger
        self.owner_id = owner_id
        self.allowed_users = allowed_users
        #: Customer wallets. A real deployment passes ``wallets`` (a
        #: WalletStore on Postgres/sqlite) so balances survive redeploys;
        #: ``balances`` stays as the plain-dict fallback for tests.
        if wallets is not None and balances is not None:
            raise ValueError("pass wallets OR balances, not both")
        self.wallets = wallets
        self.balances: dict[str, Money] = balances if balances is not None else {}
        #: White-label sub-bots. ``None`` disables /createbot entirely rather
        #: than silently running it against an in-memory store that loses every
        #: bot on restart.
        self.subbots = subbots
        self._createbot_flow: Optional[CreateBotFlow] = (
            CreateBotFlow(subbots, platform_fee) if subbots is not None else None
        )
        #: Set by the transport once a sub-bot's poller is live, so /createbot
        #: can report that its bot actually started.
        self.on_bot_created: Optional[Callable[[object], None]] = None

    # -- helpers ---------------------------------------------------------
    def balance_of(self, user_id: str) -> Money:
        if self.wallets is not None:
            return self.wallets.balance(user_id)
        return self.balances.get(user_id, Money.zero())

    def credit(self, user_id: str, amount: Money) -> Money:
        """Add funds to a customer wallet (called after a payment confirms)."""
        if amount.is_negative or amount.is_zero:
            raise ValueError("credit amount must be positive")
        if self.wallets is not None:
            return self.wallets.adjust(user_id, amount)
        self.balances[user_id] = self.balance_of(user_id) + amount
        return self.balances[user_id]

    def _debit(self, user_id: str, amount: Money) -> Money:
        """Take funds off a customer wallet for a purchase."""
        if amount.is_negative or amount.is_zero:
            raise ValueError("debit amount must be positive")
        if self.wallets is not None:
            return self.wallets.adjust(user_id, Money(-amount.paise))
        self.balances[user_id] = self.balance_of(user_id) - amount
        return self.balances[user_id]

    def _authorised(self, user_id: str) -> bool:
        return not self.allowed_users or user_id in self.allowed_users

    def _is_owner(self, user_id: str) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id

    # -- dispatch --------------------------------------------------------
    def handle(self, user_id: str, text: str) -> Reply:
        """Route one incoming message."""
        text = (text or "").strip()
        # Plain text can be an answer to an in-progress /createbot: bot
        # tokens, provider API keys and yes/no confirmations never start with
        # a slash. This check MUST run before the slash test below -- without
        # it the whole white-label flow is unreachable through the real bot,
        # while every test that drives the flow directly stays green.
        if (
            not text.startswith("/")
            and self._createbot_flow is not None
            and self._createbot_flow.pending(user_id)
        ):
            if not self._authorised(user_id):
                return Reply("You are not authorised to use this bot.", ok=False)
            return self._createbot_reply(self._createbot_flow.on_text(user_id, text))
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

        # White-label commands. /cancel always works so an owner stuck mid-flow
        # can escape, even where /createbot is disabled.
        if command == "cancel":
            return self.cmd_cancel(user_id, args)
        if self._createbot_flow is None:
            if command in {"createbot", "mybots", "deletebot"}:
                return Reply("White-label bots are not enabled on this bot.", ok=False)
        else:
            handlers["createbot"] = self.cmd_createbot
            handlers["mybots"] = self.cmd_mybots
            handlers["deletebot"] = self.cmd_deletebot

        handler = handlers.get(command)
        if handler is None:
            # An unknown slash command is never fed to the pending flow: a
            # mistyped "/creatbot" must not be swallowed as a bot token.
            return Reply(f"Unknown command /{command}. Try /help.", ok=False)
        try:
            return handler(user_id, args)
        except EconomicsError as exc:
            return Reply(f"Cannot price that right now: {exc}", ok=False)
        except Exception as exc:  # never leak a stack trace into a chat
            return Reply(f"Something went wrong: {type(exc).__name__}: {exc}", ok=False)

    def handle_callback(self, user_id: str, data: str) -> Reply:
        """Route one inline-keyboard press from the transport.

        Callback data can only advance or cancel a pending /createbot -- it
        cannot invoke any other command, so a forged press is inert.
        """
        if not self._authorised(user_id):
            return Reply("You are not authorised to use this bot.", ok=False)
        if self._createbot_flow is None or not self._createbot_flow.pending(user_id):
            return Reply("That menu has expired. Send /createbot to start again.", ok=False)
        return self._createbot_reply(self._createbot_flow.on_button(user_id, data))

    # -- handlers --------------------------------------------------------
    def cmd_help(self, user_id: str, args: list[str]) -> Reply:
        return Reply(HELP_TEXT)

    def cmd_wallet(self, user_id: str, args: list[str]) -> Reply:
        return Reply(f"Balance: {self.balance_of(user_id)}")

    def cmd_list(self, user_id: str, args: list[str]) -> Reply:
        """/list shows the tappable shop grid (it IS the list)."""
        from .ui import MenuUI  # local: avoids an import cycle at module level

        category = args[0].lower() if args else None
        if category is not None and not any(
            s.category == category for s in self.catalog.services()
        ):
            return Reply(f"No services in category {category!r}.", ok=False)
        return MenuUI(self).services_grid(user_id, category)

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
        self._debit(user_id, price)
        result = self.engine.fulfil(user_id, slug)
        if result.success:
            return Reply(
                f"OTP for {slug}: {result.otp}\nNumber: {result.phone}\n"
                f"Charged {price}. Remaining balance {self.balance_of(user_id)}."
            )
        # Failed: put the money back so the customer is never out of pocket.
        if result.refunded.paise > 0:
            self.credit(user_id, result.refunded)
        return Reply(
            f"No OTP arrived for {slug}. Refunded {result.refunded}; "
            f"balance {self.balance_of(user_id)}.",
            ok=False,
        )

    # -- white-label -----------------------------------------------------
    def cmd_createbot(self, user_id: str, args: list[str]) -> Reply:
        assert self._createbot_flow is not None
        if args:
            # `/createbot <token>` is accepted as a shortcut.
            self._createbot_flow.start(user_id)
            return self._createbot_reply(self._createbot_flow.on_text(user_id, args[0]))
        return self._createbot_reply(self._createbot_flow.start(user_id))

    def cmd_cancel(self, user_id: str, args: list[str]) -> Reply:
        if self._createbot_flow and self._createbot_flow.pending(user_id):
            self._createbot_flow.cancel(user_id)
            return Reply("Cancelled. Nothing was created.")
        return Reply("Nothing in progress.", ok=False)

    def cmd_mybots(self, user_id: str, args: list[str]) -> Reply:
        assert self.subbots is not None
        bots = self.subbots.for_owner(user_id)
        if not bots:
            return Reply("You have no bots yet. Send /createbot to make one.", ok=False)
        lines = []
        for b in bots:
            mode = "platform numbers" if b.mode.value == "platform_api" else "your own API"
            state = "running" if b.active else "stopped"
            fee = (
                "no platform fee"
                if b.mode.value == "platform_api"
                else f"platform fee {b.fee.describe()}"
            )
            lines.append(f"`{b.id}` - {mode}, {fee}, {state}")
        return Reply("Your bots:\n" + "\n".join(lines))

    def cmd_deletebot(self, user_id: str, args: list[str]) -> Reply:
        assert self.subbots is not None
        if not args:
            return Reply("Usage: /deletebot <bot id>. See /mybots.", ok=False)
        bot = self.subbots.find(args[0])
        if bot is None or bot.owner_id != user_id:
            return Reply("No bot of yours has that id.", ok=False)
        self.subbots.delete(bot.id)
        return Reply(f"Deleted bot `{bot.id}`.")

    def _createbot_reply(self, result: CreateBotResult) -> Reply:
        if result.created is not None and self.on_bot_created is not None:
            try:
                self.on_bot_created(result.created)
            except Exception as exc:  # the bot record exists; say so honestly
                return Reply(
                    f"{result.reply}\n\nNote: your bot was saved but its poller "
                    f"did not start ({type(exc).__name__}). Use /restart to try again.",
                    ok=False,
                )
        return Reply(result.reply, buttons=tuple(result.buttons))

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
