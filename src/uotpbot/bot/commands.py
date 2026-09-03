"""Bot command handling, independent of any chat transport.

Every handler takes plain strings and returns plain text, so the whole
conversation layer is unit-testable without a Telegram connection. The
transport adapter in ``telegram.py`` is a thin shell over this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import time
import uuid
from typing import Optional

from ..catalog import Catalog
from ..createbot import CreateBotFlow, CreateBotResult
from ..economics import EconomicsError
from ..engine import BotEngine
from ..ledger import Ledger
from ..money import Money
from ..pricing import Pricer
from ..whitelabel import PlatformFee, SubBotRegistry

__all__ = ["CommandRouter", "HELP_TEXT"]

HELP_TEXT = """YCOTP bot

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

    ``photo`` is a Telegram file_id the transport sends WITH this reply as
    a photo message (payment QR codes; Telegram cannot edit a text message
    into a photo, so the transport sends a fresh message).
    ``notify`` is a list of ``(chat_id, text)`` the transport additionally
    delivers as fresh messages -- used to alert the owner about payments.
    ``forward_photo`` asks the transport to also forward the photo the
    customer just sent to the owner (payment screenshots).
    """

    text: str
    ok: bool = True
    buttons: tuple[tuple[str, str], ...] = ()
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    deferred: Optional[Callable[[str], "Reply"]] = None
    photo: Optional[str] = None
    notify: tuple[tuple[str, str], ...] = ()
    forward_photo: bool = False


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
        maintenance_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.engine = engine
        # Owner panel sets this to pause buying during provider incidents:
        # returns True while purchases/maintenance are in effect.
        self.maintenance_fn = maintenance_fn
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
        #: Number-first wait: token -> (customer_id, slug, engine result). An
        #: order is placed here once a number is allocated and cleared when the
        #: OTP is delivered or the wait is abandoned. Kept in memory: the wait
        #: only spans a single order's lifetime, never a redeploy.
        self._awaiting: dict[str, tuple[str, str, object]] = {}

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
        return self.purchase(user_id, args[0])

    def purchase(self, user_id: str, slug: str, *, server: str = "") -> Reply:
        """THE money path -- /buy and every ✅ Buy tap both end up here.

        Single entry point on purpose: wallet guard, debit, fulfilment,
        refund and the durable order record must never be able to disagree,
        so there is exactly one implementation to test.
        """
        if not self.catalog.has(slug):
            return Reply(f"Unknown service {slug!r}. Try /list.", ok=False)

        if (self.maintenance_fn and self.maintenance_fn()
                and not self._is_owner(user_id)):
            return Reply(
                "🛠 Maintenance in progress — purchases are paused for a few "
                "minutes. Your balance is safe; please try again shortly.",
                ok=False,
            )

        serve = getattr(self.engine.provider, "can_serve", None)
        if callable(serve) and not serve(slug) and not self._is_owner(user_id):
            return Reply(
                f"😔 {self.catalog.get(slug).name} numbers are temporarily "
                "unavailable from our supplier. Nothing was charged — pick "
                "another service, or try again later.",
                ok=False,
            )

        price, _ = self.engine.quote(slug, server=server or None)
        balance = self.balance_of(user_id)
        if balance.paise < price.paise:
            short = price - balance
            return Reply(
                f"That costs {price}; your balance is {balance}. "
                f"Top up at least {short} more (💰 Balance → ➕ Add money).",
                ok=False,
            )

        # Debit first: if the purchase then fails, the refund path restores it.
        self._debit(user_id, price)
        result = self.engine.fulfil(user_id, slug, gross_price=price,
                                    server=server or None)
        self._record_order(user_id, slug, price, result)
        if result.success:
            return Reply(
                f"✅ OTP for {self.catalog.get(slug).name}: {result.otp}\n"
                f"📱 Number: {result.phone}\n\n"
                f"Charged {price} · Balance {self.balance_of(user_id)}"
            )
        # Failed: put the money back so the customer is never out of pocket.
        if result.refunded.paise > 0:
            self.credit(user_id, result.refunded)
        reason = getattr(result, "message", "") or ""
        detail = f"\n\nWhy: {reason}" if reason else ""
        return Reply(
            f"⚠️ Couldn't deliver {slug}. Refunded {result.refunded} in full; "
            f"balance {self.balance_of(user_id)}.{detail}",
            ok=False,
        )

    def alloc_and_wait(
        self, user_id: str, slug: str, *, server: str = ""
    ) -> Reply:
        """Validate, debit, allocate a number, and start waiting for its OTP.

        Returns a reply that (a) shows the number immediately so the customer
        can request the OTP on the target service, and (b) carries an
        ``await_token`` so the transport can re-invoke :meth:`check_otp` on
        demand. If the number cannot be allocated the customer is refunded.
        """
        if not self.catalog.has(slug):
            return Reply(f"Unknown service {slug!r}. Try /list.", ok=False)

        if (self.maintenance_fn and self.maintenance_fn()
                and not self._is_owner(user_id)):
            return Reply(
                "🛠 Maintenance in progress — purchases are paused for a few "
                "minutes. Your balance is safe; please try again shortly.",
                ok=False,
            )

        serve = getattr(self.engine.provider, "can_serve", None)
        if callable(serve) and not serve(slug) and not self._is_owner(user_id):
            return Reply(
                f"😔 {self.catalog.get(slug).name} numbers are temporarily "
                "unavailable from our supplier. Nothing was charged — pick "
                "another service, or try again later.",
                ok=False,
            )

        price, _ = self.engine.quote(slug, server=server or None)
        balance = self.balance_of(user_id)
        if balance.paise < price.paise:
            short = price - balance
            return Reply(
                f"That costs {price}; your balance is {balance}. "
                f"Top up at least {short} more (💰 Balance → ➕ Add money).",
                ok=False,
            )

        self._debit(user_id, price)
        result = self.engine.allocate_number(user_id, slug, gross_price=price,
                                             server=server or None)
        if not result.success and result.order.state.value == "awaiting_otp" \
                and result.phone:
            # Number allocated -> hand it over NOW; the OTP arrives separately.
            token = uuid.uuid4().hex[:12]
            self._awaiting[token] = (user_id, slug, result)
            self._record_active(user_id, slug, price, result, token)
            return self.await_reply(token, result)
        # Allocation failed (already refunded by the engine's _fail).
        self._record_order(user_id, slug, price, result)
        # Allocation failed (already refunded by the engine's _fail).
        if result.refunded.paise > 0:
            self.credit(user_id, result.refunded)
        reason = getattr(result, "message", "") or ""
        detail = f"\n\nWhy: {reason}" if reason else ""
        return Reply(
            f"⚠️ Couldn't get a number for {slug}. Refunded {result.refunded} in "
            f"full; balance {self.balance_of(user_id)}.{detail}",
            ok=False,
        )

    def await_reply(self, token: str, result) -> Reply:
        """Build the 'number is yours; waiting for OTP' reply + a Check button."""
        return Reply(
            f"📱 {self.catalog.get(result.order.service).name}\n\n"
            f"🎯 Your number: `{result.phone}`\n"
            f"⏳ Waiting for the OTP to arrive (up to ~{int(self.engine.config.otp_timeout_seconds // 60)} min)…\n\n"
            "Enter this number on the service to request the code, then tap "
            "💰 Check OTP below. Auto-refund if it never lands.",
            rows=(
                ((f"💰 Check OTP", f"co:{token}"),),
                ((f"🔁 Resend SMS", f"rs:{token}"), (f"♻️ Cancel", f"cx:{token}")),
                (("🧾 My numbers", "o"), ("🏠 Menu", "m")),
            ),
        )

    def resend_sms(self, token: str) -> Reply:
        """🔁 Resend: ask the provider to re-send the code (setStatus=3)."""
        provider_order_id = self._provider_order_id(token)
        if not provider_order_id:
            return Reply("That order is no longer waiting. See 🧾 My numbers.",
                         ok=False, rows=((("🧾 My numbers", "o"),),))
        ok = getattr(self.engine.provider, "resend", None)
        if callable(ok) and ok(provider_order_id):
            return Reply(
                "✅ Resent. Give it a moment, then tap 💰 Check OTP.",
                rows=((("💰 Check OTP", f"co:{token}"),),
                      (("🏠 Menu", "m"),)),
            )
        return Reply(
            "Couldn't resend right now — the provider is briefly unavailable. "
            "Tap 💰 Check OTP in a moment instead.",
            ok=False,
            rows=((("💰 Check OTP", f"co:{token}"),), (("🏠 Menu", "m"),)),
        )

    def _provider_order_id(self, token: str) -> str:
        """The provider order id for a wait-token, from memory or the DB.

        Handles the in-memory fast path and the after-restart case in one place.
        """
        entry = self._awaiting.get(token)
        if entry is not None:
            alloc = getattr(entry[2], "_alloc", None)
            return getattr(alloc, "order_id", "") if alloc else ""
        active = self._active_by_token(token)
        return getattr(active, "provider_order_id", "") if active else ""

    def cancel_wait(self, token: str) -> Reply:
        """♻️ Cancel: release the number so the activation stops costing.

        Only refund the customer when the provider actually confirmed the
        release. If the provider refuses (the live handler returns
        EARLY_CANCEL_DENIED for a fresh activation -- it must sit out its OTP
        window), we tell the customer the truth rather than silently refunding
        a number that is still live and spendable on our wallet.
        """
        entry = self._awaiting.get(token)
        user_id = slug = None
        result = None
        provider_id = ""
        if entry is not None:
            user_id, slug, result = entry
            alloc = getattr(result, "_alloc", None)
            provider_id = getattr(alloc, "order_id", "") if alloc else ""
        else:
            active = self._active_by_token(token)
            if active is None:
                return Reply("That order has finished. See 🧾 My numbers.",
                             ok=False, rows=((("🧾 My numbers", "o"),),))
            user_id, slug, provider_id = active.user_id, active.slug, active.provider_order_id
        released = True
        refused = ""
        if provider_id:
            # Use cancel_strict so a provider refusal (EARLY_CANCEL_DENIED)
            # propagates instead of being swallowed by the best-effort
            # ``cancel`` (which returns Money(0) on both success and failure).
            strict = getattr(self.engine.provider, "cancel_strict", None)
            try:
                if callable(strict):
                    strict(provider_id)
                else:
                    self.engine.provider.cancel(provider_id)
            except Exception as exc:  # noqa: BLE001 - surface provider refusal
                released = False
                refused = str(exc)
        if not released:
            return Reply(
                "♻️ The provider won't release this number yet — a fresh "
                "activation must wait out its window before it can be cancelled "
                "or refunded. Tap 💰 Check OTP; if no code arrives it auto-refunds "
                "at the deadline."
                + (f"\n\n({refused})" if refused else ""),
                ok=False,
                rows=((("💰 Check OTP", f"co:{token}"),), (("🏠 Menu", "m"),)),
            )
        self._awaiting.pop(token, None)
        # Release confirmed -> remove from the active set too.
        finish = getattr(self.wallets, "finish_active", None)
        if callable(finish) and provider_id:
            try:
                finish(provider_id)
            except Exception:  # noqa: BLE001
                pass
        gross = result.order.gross_price if result is not None and result.order else Money(0)
        self.credit(user_id, gross)
        name = self.catalog.get(slug).name if self.catalog.has(slug) else slug
        return Reply(
            f"♻️ Cancelled — {name} number released and {gross} "
            f"returned to your balance ({self.balance_of(user_id)}).",
            rows=((("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
        )

    def check_otp(self, token: str, *, wait_seconds: int = 0) -> Reply:
        """Re-check a waiting order for its OTP; deliver it when it arrives.

        ``wait_seconds`` lets a button press block a short while before giving
        up, so tapping 'Check' actually waits for a code that is seconds away
        rather than replying instantly with nothing. The transport runs this
        off the event loop (deferred), so a longer wait is fine.
        """
        entry = self._awaiting.get(token)
        if entry is not None:
            user_id, slug, result = entry
            wait = wait_seconds if wait_seconds > 0 else self.engine.config.otp_timeout_seconds
            result = self.engine.poll_otp(result, timeout_seconds=wait)
            return self._after_check(token, user_id, slug, result)
        # Restart-safe: re-enter the wait straight from the DB row.
        active = self._active_by_token(token)
        if active is None:
            return Reply(
                "That order has finished or is no longer waiting. "
                "See 🧾 My numbers.",
                ok=False, rows=((("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
            )
        wait = wait_seconds if wait_seconds > 0 else self.engine.config.otp_timeout_seconds
        result = self.engine.resume_otp(
            provider_order_id=active.provider_order_id,
            customer_id=active.user_id, service=active.slug,
            gross=active.gross, phone=active.phone, allocated_ts=active.ts,
            timeout_seconds=wait,
        )
        return self._after_check(token, active.user_id, active.slug, result)

    def _after_check(self, token: str, user_id: str, slug: str, result) -> Reply:
        """Shared tail of check_otp: deliver the OTP, refund on timeout, or wait."""
        if result.success and result.otp:
            self._awaiting.pop(token, None)
            self._record_order(user_id, slug, result.order.gross_price, result)
            return Reply(
                f"✅ OTP for {self.catalog.get(slug).name}: `{result.otp}`\n"
                f"📱 Number: {result.phone}\n\n"
                f"Charged {result.order.gross_price} · Balance {self.balance_of(user_id)}",
                rows=(
                    (("🔁 Another", f"s:{slug}"), ("🧾 My numbers", "o")),
                    (("🏠 Menu", "m"),),
                ),
            )
        # Still waiting (or timed out to a refund).  If poll_otp refunded, reflect it.
        if result.refunded.paise > 0:
            self.credit(user_id, result.refunded)
        if not result.success:
            # Terminal (timed out / refunded) -- clear it and the active number.
            self._awaiting.pop(token, None)
            self._record_order(user_id, slug, result.order.gross_price, result)
            return Reply(
                f"⚠️ No OTP arrived for {slug}. Refunded {result.refunded} in "
                f"full; balance {self.balance_of(user_id)}.",
                ok=False,
                rows=((("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
            )
        return self.await_reply(token, result)

    def _active_by_token(self, token: str) -> Optional[object]:
        """Look up a live number by its wait-token.

        Works whether or not the in-memory ``_awaiting`` entry is still there
        (so Check OTP / Resend / Cancel survive a redeploy).
        """
        store = self.wallets
        fn = getattr(store, "get_active", None)
        if not callable(fn):
            return None
        try:
            return fn(token)
        except Exception:  # noqa: BLE001
            return None

    def active_owner(self, token: str) -> Optional[str]:
        """The user_id that owns a wait-token, from memory or the DB.

        Lets the UI authorise Check OTP / Resend / Cancel against the right
        customer even after a redeploy.
        """
        entry = self._awaiting.get(token)
        if entry is not None:
            return entry[0]
        active = self._active_by_token(token)
        return getattr(active, "user_id", None) if active else None

    def _record_active(self, user_id: str, slug: str, price: Money, result,
                       token: str) -> None:
        """Persist the just-allocated number so it shows in 'My numbers' and
        survives leaving the buy screen / a redeploy."""
        store = self.wallets
        rec = getattr(store, "record_active", None)
        if not callable(rec):
            return
        alloc = getattr(result, "_alloc", None)
        provider_id = getattr(alloc, "order_id", "") if alloc else ""
        # Absolute epoch expiry so the row stays viewable until the number is
        # actually dead, surviving the message/navigation and a redeploy.
        import datetime
        if alloc is not None:
            seconds_left = alloc.seconds_left()
            valid_until = time.time() + max(seconds_left, 0)
        else:
            valid_until = time.time() + 20 * 60  # provider validity default
        try:
            rec(user_id=user_id, slug=slug, phone=result.phone or "",
                provider_order_id=provider_id, token=token, gross=price,
                valid_until=valid_until)
        except Exception:  # noqa: BLE001 - a display cache must not block delivery
            pass

    def _record_order(self, user_id: str, slug: str, price: Money, result) -> None:
        """Persist a durable order row for history + per-order profit.

        Display cache ONLY -- the ledger stays the source of truth for money;
        this exists so /metrics, history and the owner panel can answer
        "what happened per order" without parsing journal lines.
        """
        store = self.wallets
        rec = getattr(store, "record_order", None)
        if not callable(rec):
            return
        # Rich history fields: status/reason, what was refunded, what the
        # provider charged, and the customer's balance right after.
        status = "delivered" if result.success else (
            "refunded" if result.refunded.paise > 0 else "failed"
        )
        reason = result.message or ""
        alloc = getattr(result, "_alloc", None)
        spent = alloc.charged if alloc is not None else Money(0)
        try:
            rec(user_id=user_id, slug=slug, amount=price, phone=result.phone or "",
                otp=result.otp or "", success=result.success, profit=result.profit,
                status=status, reason=reason, refunded=result.refunded,
                spent=spent, balance_after=self.balance_of(user_id))
        except Exception:  # never let the receipt printer block a delivery
            pass
        # The order is now terminal (delivered or refunded): remove it from the
        # live/active set so it no longer shows as an active number.
        finish = getattr(store, "finish_active", None)
        alloc = getattr(result, "_alloc", None)
        provider_id = getattr(alloc, "order_id", "") if alloc else ""
        if callable(finish) and provider_id:
            try:
                finish(provider_id)
            except Exception:  # noqa: BLE001 - display cache, non-fatal
                pass

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
