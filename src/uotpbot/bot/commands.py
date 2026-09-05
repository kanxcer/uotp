"""Bot command handling, independent of any chat transport.

Every handler takes plain strings and returns plain text, so the whole
conversation layer is unit-testable without a Telegram connection. The
transport adapter in ``telegram.py`` is a thin shell over this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import logging
import threading
import time
import uuid

from ..catalog import Catalog
from ..cancel_tracker import CancelTracker
from ..createbot import CB_HUB_ADD, CB_HUB_MINE, CreateBotFlow, CreateBotResult
from ..economics import EconomicsError
from ..engine import BotEngine
from ..ledger import Ledger
from ..money import INR, Money
from ..orders import OrderState
from ..pricing import Pricer
from ..ratelimit import RateLimitConfig, RateLimiter
from ..refund import DurableRefund
from ..tz import format_ts
from ..whitelabel import PlatformFee, SubBotRegistry

log = logging.getLogger("uotpbot.commands")

__all__ = ["CommandRouter", "HELP_TEXT"]

#: Page size for the owner "All users" list. Telegram messages cap at 4096
#: chars; ~40 ids with balance + last-seen stays comfortably under that.
_USERS_PAGE = 40

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
    #: A URL to a photo the transport fetches and sends WITH this reply (used
    #: for FamGateway's hosted payment-QR image, which is a URL, not a
    #: Telegram file_id). The transport sends a fresh photo message, same as
    #: ``photo``.
    photo_url: Optional[str] = None
    notify: tuple[tuple[str, str], ...] = ()
    forward_photo: bool = False
    #: Ask the transport to also show a persistent bottom menu
    #: (ReplyKeyboardMarkup) so the core actions are one tap away without
    #: typing. Only meaningful for main-menu/welcome-style replies.
    persistent_menu: bool = False


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
        subbot_manager: Optional[object] = None,
        platform_fee: Optional[PlatformFee] = None,
        bot_token_verifier: Optional[Callable[[str], tuple[bool, str]]] = None,
        maintenance_fn: Optional[Callable[[], bool]] = None,
        rate_limit: Optional[RateLimitConfig] = None,
        payment_notifier: Optional[object] = None,
        is_clone: bool = False,
        reseller_rate=None,
        clone_bot_id: str = "",
        platform_wallets: Optional[object] = None,
        platform_owner_id: str = "",
        margin_fee_rate=None,
        clone_bot_token: str = "",
        platform_bot_username: str = "",
        bot_profile_applier=None,
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
        #: Explicitly banned user ids. Kept SEPARATE from ``allowed_users``:
        #: an allowlist means "these may buy" (empty = anyone), so toggling
        #: bans on it made banning one user lock out everyone else. Banned
        #: users are refused in every mode; the owner can never be banned.
        self._banned: set[str] = set()
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
        #: Bridge that edits a customer's QR message to a success note when a
        #: payment is confirmed. Wired from the serve layer; the UI's credit
        #: path (Check status / I've paid tap) uses it so the QR is updated no
        #: matter which path credited the order.
        self.payment_notifier = payment_notifier
        #: Clone (white-label) bot: sells our numbers at an extra %; no own API.
        from decimal import Decimal as _Dec
        self.is_clone = bool(is_clone)
        self.reseller_rate = _Dec(reseller_rate or 0)
        self.clone_bot_id = clone_bot_id or ""
        self.platform_wallets = platform_wallets
        self.platform_owner_id = platform_owner_id or ""
        self.margin_fee_rate = _Dec(margin_fee_rate if margin_fee_rate is not None else "0.05")
        self.clone_bot_token = clone_bot_token or ""
        #: The live MultiBotManager that runs one poller thread per sub-bot.
        #: Exposes ``running()`` / ``errors()`` so /mybots can report whether a
        #: white-label bot is ACTUALLY polling (a saved bot can still be dead if
        #: its token is invalid or the poller crashed) rather than only what the
        #: registry row says. Optional: absent in tests / non-serve deployments.
        self.subbot_manager = subbot_manager
        #: Live hook the UI sets so /createbot agrees with the "Run your own
        #: bot" button about whether cloning is enabled. ``None`` means
        #: enabled (the historical behaviour when no UI owns the flag).
        self.createbot_enabled_fn: Optional[Callable[[], bool]] = None
        self.platform_bot_username = (platform_bot_username or "").lstrip("@")
        self._createbot_flow: Optional[CreateBotFlow] = (
            CreateBotFlow(
                subbots, platform_fee,
                token_verifier=bot_token_verifier,
                platform_username=self.platform_bot_username,
                profile_applier=bot_profile_applier,
            )
            if subbots is not None else None
        )
        #: Set by the transport once a sub-bot's poller is live, so /createbot
        #: can report that its bot actually started.
        self.on_bot_created: Optional[Callable[[object], None]] = None
        #: Number-first wait: token -> (customer_id, slug, engine result). An
        #: order is placed here once a number is allocated and cleared when the
        #: OTP is delivered or the wait is abandoned. Kept in memory: the wait
        #: only spans a single order's lifetime, never a redeploy.
        self._awaiting: dict[str, tuple[str, str, object]] = {}
        #: token -> final Reply for a genuinely-resolved (delivered/refunded)
        #: order, so a background auto-poller only ever edits with a real
        #: outcome and never overwrites an OTP a manual tap already delivered.
        self._terminal: dict[str, object] = {}
        #: Per-token single-flight: only ONE poller (background auto-poll OR a
        #: manual 💰 Check OTP tap) may wait on an order at a time. Without this,
        #: every Check tap stacked another ~5-minute OTP-window wait on its own
        #: thread, so "My numbers" appeared frozen until the number was cancelled
        #: or expired. A duplicate tap now returns instantly and never blocks.
        self._poll_guard = threading.Lock()
        self._polling_tokens: set[str] = set()
        #: Per-user buy rate limiter (anti-spam / anti-burn). Floats on the
        #: wallet store's KV so the window survives a redeploy.
        self._rate_limiter = RateLimiter(
            rate_limit or RateLimitConfig(), store=wallets,
        )
        #: Owner's copy so the transport can exempt its own account.
        for _uid in (owner_id,) if owner_id else ():
            self._rate_limiter.set_user_override(_uid)
        #: EARLY_CANCEL_DENIED hidden-cost tracker (feed burn rate + owner report).
        self._cancel_tracker = CancelTracker()
        #: Durable, retried customer refunds (outbox). No refund is ever lost.
        self.refunds = DurableRefund(self)

    # -- helpers ---------------------------------------------------------
    def start_refund_worker(self) -> None:
        """Boot the durable-refund retry worker (also sweeps pending rows)."""
        if getattr(self, "refunds", None) is not None:
            self.refunds.start()

    def refund_pending_count(self) -> int:
        """Outstanding refunds awaiting credit (owner can see on /metrics)."""
        if getattr(self, "refunds", None) is not None:
            return self.refunds.pending_count()
        return 0

    def phase1_snapshot(self) -> dict:
        """Subsystem stats for /metrics (Phase 1: P3 rate limiter, P4 tracker)."""
        try:
            cancel = self._cancel_tracker.summary()
            rate = self._rate_limiter.stats()
        except Exception as exc:  # noqa: BLE001 - metrics must never break
            return {"phase1_error": str(exc)}
        return {
            "cancel_denied": cancel,
            "rate_limiter": rate,
        }
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
        # Ban is a hard NO in every mode (allowlist or anyone), independent of
        # the allowlist so banning one user can never affect the rest.
        if user_id in self._banned:
            return False
        # The owner is never shut out, even if the access switch is off or the
        # allowlist doesn't list them explicitly (they administer the bot).
        if self._is_owner(user_id):
            return True
        # Owner kill-switch ("allow users to use this bot"): when off, only the
        # owner is authorised. The hook is set by the UI; None = enabled.
        fn = getattr(self, "bot_enabled_fn", None)
        if callable(fn) and not fn():
            return False
        return not self.allowed_users or user_id in self.allowed_users

    def _is_owner(self, user_id: str) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id

    @staticmethod
    def _remaining_otp_minutes(alloc) -> Optional[int]:
        """Whole minutes left before this number auto-resolves (refund/deadline).

        Works from a live ``NumberAllocation`` (its ``seconds_left`` is the
        remaining validity) or a persisted ``ActiveNumber``. Returns None when
        no timing is available so callers can fall back to a generic note.
        """
        try:
            secs = float(getattr(alloc, "seconds_left", lambda: 0)())
        except Exception:  # noqa: BLE001
            return None
        if secs <= 0:
            return 0
        return int(secs // 60)

    @staticmethod
    def _minute_text(mins: Optional[int]) -> str:
        if mins is None:
            return ""
        return f"~{mins} min" if mins >= 1 else "less than a minute"

    @staticmethod
    def _exact_duration(seconds_float: float) -> str:
        """Human-exact duration, e.g. ``1 minute 10 seconds`` / ``10 seconds`` /
        ``2 minutes``. Used for the cancel-cooldown countdown so a customer
        knows precisely when they can cancel."""
        secs = max(0, int(round(seconds_float)))
        mins, rem = divmod(secs, 60)
        parts = []
        if mins:
            parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
        if rem:
            parts.append(f"{rem} second{'s' if rem != 1 else ''}")
        if not parts:
            return "0 seconds"
        return " ".join(parts)

    def _otp_window_minutes(self, alloc) -> Optional[int]:
        """Whole minutes left in the OTP window (when the number auto-resolves).

        The number stays valid for several minutes after the OTP window, but the
        auto-refund happens when no code arrives by the OTP deadline, so the
        countdown the customer cares about (\"when can I cancel / when does it
        refund\") is the OTP window remaining, not the full validity.
        """
        if alloc is None:
            return None
        try:
            from datetime import datetime
            allocated = getattr(alloc, "allocated_at", None)
            if isinstance(allocated, str) and allocated:
                ts = datetime.fromisoformat(allocated).timestamp()
            else:
                ts = getattr(alloc, "ts", None)
            if ts is None:
                return None
            import time
            elapsed = time.time() - float(ts)
            window = self.engine.config.otp_timeout_seconds
            remaining = window - elapsed
            if remaining <= 0:
                return 0
            return int(remaining // 60)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _alloc_epoch(alloc) -> Optional[float]:
        """The absolute allocation time (epoch seconds) of a number, however
        its source is shaped: a live NumberAllocation carries an ISO
        ``allocated_at``; a persisted ActiveNumber carries ``ts``."""
        try:
            raw = getattr(alloc, "allocated_at", None)
            if isinstance(raw, str) and raw:
                from datetime import datetime
                return datetime.fromisoformat(raw).timestamp()
        except Exception:  # noqa: BLE001
            pass
        try:
            ts = getattr(alloc, "ts", None)
            if ts is not None:
                return float(ts)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _cancel_cooldown_left(self, alloc) -> Optional[float]:
        """Seconds remaining before this number is cancellable (0 = now).

        Computed from the actual allocation timestamp + the provider's cooldown
        (``EngineConfig.cancel_cooldown_seconds``), NOT from the OTP window or a
        parsed error string. Returns None when no timing information exists.
        """
        epoch = self._alloc_epoch(alloc)
        if epoch is None:
            return None
        eligible = epoch + self.engine.config.cancel_cooldown_seconds
        return max(0.0, eligible - time.time())

    @staticmethod
    def _retryable(reason: str) -> bool:
        """Whether a failed buy is worth a one-tap retry.

        No-stock / per-operator rejections are transient: a Retry makes a fresh
        allocation without re-navigating. Administrative failures (maintenance,
        auth, config) are NOT retryable and the customer should not be nudged to
        hammer them.
        """
        low = (reason or "").lower()
        return any(
            frag in low for frag in
            ("no stock", "bad_service", "bad_operator", "no number", "no_numbers")
        )

    @staticmethod
    def _retry_row(slug: str, server: str = "") -> tuple[tuple[str, str], ...]:
        """The Retry inline button (row) for a refunded buy."""
        return (("🔄 Retry", f"ry:{slug}:{server}"),)

    # -- dispatch --------------------------------------------------------
    def handle(self, user_id: str, text: str) -> Reply:
        """Route one incoming message."""
        text = (text or "").strip()
        # Owner kill-switch ("allow users to use this bot"): shut non-owners out
        # of the command surface too, with the same friendly message as the UI.
        if not self._is_owner(user_id):
            fn = getattr(self, "bot_enabled_fn", None)
            if callable(fn) and not fn():
                return Reply(
                    "🔒 This bot is temporarily switched off by the owner. "
                    "\n\nYour balance and any active numbers are safe. Please "
                    "try again later.",
                    ok=False,
                )
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
            # Owner/admin commands.
            "metrics": self.cmd_metrics,
            "admin": self.cmd_admin,
            "sunkcost": self.cmd_sunkcost,
            "provider": self.cmd_provider,
            "credit": self.cmd_credit,
            "debit": self.cmd_debit,
            "orders": self.cmd_orders,
            "users": self.cmd_users,
            "ban": lambda uid, a: self.cmd_ban(uid, a, ban=True),
            "unban": lambda uid, a: self.cmd_ban(uid, a, ban=False),
            "maintenance": self.cmd_maintenance,
            "broadcast": self.cmd_broadcast,
            "setmargin": self.cmd_setmargin,
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

        Hub buttons (My bots / Create) work without a pending session. Other
        ``cb:*`` data can only advance or cancel a pending /createbot -- it
        cannot invoke any other command, so a forged press is inert.
        """
        if not self._authorised(user_id):
            return Reply("You are not authorised to use this bot.", ok=False)
        if self._createbot_flow is None:
            return Reply("That menu has expired. Send /createbot to start again.", ok=False)
        if data == CB_HUB_ADD:
            fn = getattr(self, "createbot_enabled_fn", None)
            if callable(fn) and not fn():
                return Reply(
                    "🤖 'Run your own bot' is currently turned OFF by the owner. "
                    "You can't create a clone right now.",
                    ok=False,
                )
            return self._createbot_reply(self._createbot_flow.start(user_id))
        if data == CB_HUB_MINE:
            return self.cmd_mybots(user_id, [])
        if not self._createbot_flow.pending(user_id):
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

    def purchase(self, user_id: str, slug: str, *, server: str = "",
                 retry: bool = False) -> Reply:
        """THE money path -- /buy and every ✅ Buy tap both end up here.

        Single entry point on purpose: wallet guard, debit, fulfilment,
        refund and the durable order record must never be able to disagree,
        so there is exactly one implementation to test.
        """
        if not self.catalog.has(slug):
            return Reply(f"Unknown service {slug!r}. Try /list.", ok=False)

        # Phase-1 per-user buy throttle: a spammy user burns provider calls and
        # credit for nothing. Owner operators are exempt. A Retry-button press
        # is excluded: the failed attempt was refunded, so it is a second
        # chance, not a spam pattern.
        if not retry:
            allowed, why = self._rate_limiter.check(user_id)
            if not allowed:
                return Reply(why, ok=False)
            self._rate_limiter.record(user_id)

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
        # (Exactly ONE rate-limit slot per buy — the check+record above already
        # spent it. Recording again here halved the effective limit for typed
        # /buy while button buys used one slot, so identical behaviour hit two
        # different throttles.)
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
        rows = ()
        # A no-stock / operator rejection is transient: offer a one-tap Retry
        # rather than making the customer re-navigate to the service.
        if self._retryable(reason):
            rows = (self._retry_row(slug, server), (("🏠 Menu", "m"),))
        return Reply(
            f"⚠️ Couldn't deliver {slug}. Refunded {result.refunded} in full; "
            f"balance {self.balance_of(user_id)}.{detail}",
            ok=False,
            rows=rows,
        )

    def alloc_and_wait(
        self, user_id: str, slug: str, *, server: str = "", retry: bool = False
    ) -> Reply:
        """Validate, debit, allocate a number, and start waiting for its OTP.

        Returns a reply that (a) shows the number immediately so the customer
        can request the OTP on the target service, and (b) carries an
        ``await_token`` so the transport can re-invoke :meth:`check_otp` on
        demand. If the number cannot be allocated the customer is refunded.

        ``retry=True`` marks a Retry-button press on a refunded no-stock/operator
        failure, so the per-user buy throttle is not applied to the customer's
        second chance (the failed attempt already spent a window slot and was
        refunded).
        """
        if not self.catalog.has(slug):
            return Reply(f"Unknown service {slug!r}. Try /list.", ok=False)

        # Phase-1 per-user buy throttle: a spammy user burns provider calls and
        # credit for nothing. Owner/white-label operators are exempt.
        if not retry:
            allowed, why = self._rate_limiter.check(user_id)
            if not allowed:
                return Reply(why, ok=False)
            self._rate_limiter.record(user_id)

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
                (("💰 Check OTP", f"co:{token}"),),
                (("🔁 Resend SMS", f"rs:{token}"), ("♻️ Cancel", f"cx:{token}")),
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
        #: Gross from the persisted active row, used when the in-memory order
        #: is gone (after a redeploy) -- a cancel must refund the same money
        #: the purchase took, and the row is the only place that survives.
        db_gross: Optional[Money] = None
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
            db_gross = active.gross
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
            except Exception as exc:  # noqa: BLE001 - provider refusal
                released = False
                refused = str(exc)
                # Logged for diagnosis, never shown to the customer (the
                # cooldown timer below tells them everything they need).
                log.debug("cancel refused for %s: %s", provider_id, refused)
        if not released:
            # Phase-4: this refusal leaves a live, chargeable number. Record the
            # sunk cost so it feeds the burn model and the owner's /metrics.
            cost = 0
            if result is not None and result.order is not None:
                cost = result.order.gross_price.paise
            elif db_gross is not None:
                cost = db_gross.paise
            self._cancel_tracker.record_denied(
                service=slug or "", server=provider_id.split("|")[-1] if provider_id else "",
                order_id=provider_id, cost_paise=cost,
            )
            alloc = getattr(result, "_alloc", None) if result is not None else None
            if alloc is None:
                active = self._active_by_token(token)
                alloc = active
            # Show the REAL cancel cooldown (2 min from allocation), not the OTP
            # window -- the provider locks cancellation for a fixed cooldown even
            # though the window is ~5 min, so "~4 min" was simply wrong.
            cooldown = self._cancel_cooldown_left(alloc)
            if cooldown is None:
                timer = (
                    "\n\n⏳ You can tap ♻️ Cancel again shortly, or tap 💰 Check "
                    "OTP and it refunds automatically if no code arrives."
                )
            elif cooldown <= 0:
                timer = (
                    "\n\n♻️ Cancel is unlocked now — tap ♻️ Cancel again to "
                    "release it and get refunded."
                )
            else:
                cool_s = int(round(self.engine.config.cancel_cooldown_seconds))
                cool_min = max(1, round(cool_s / 60)) if cool_s >= 60 else 0
                cooldown_desc = (
                    f"{cool_min} minutes" if cool_min else f"{cool_s} seconds"
                )
                wait = self._exact_duration(cooldown)
                timer = (
                    f"\n\n⏳ A number must sit out {cooldown_desc} after it's "
                    f"allocated before it can be released.\n\n"
                    f"♻️ You can cancel in {wait}\n\n"
                    "Try ♻️ Cancel again after that, or tap 💰 Check OTP — "
                    "it refunds automatically if no code arrives."
                )
            # The raw provider error (e.g. EARLY_CANCEL_DENIED) is internal and
            # pointless to the customer; the cooldown timer above already says
            # exactly what to do. Never leak the provider response into chat.
            return Reply(
                "♻️ This number can't be cancelled yet." + timer,
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
        gross = (result.order.gross_price
                 if result is not None and result.order is not None
                 else (db_gross or Money(0)))
        # Idempotent: if a concurrent poll already refunded this order, don't
        # credit again -- tell the truth instead.
        already = self.refunds.is_processed(token)
        # Mark the in-flight order REFUNDED so a concurrent engine poll that is
        # still waiting on this number does NOT post a second customer-refund
        # ledger line when it finally times out.
        order = getattr(result, "order", None)
        if order is not None and not already:
            try:
                if not order.state.is_terminal:
                    order.transition(OrderState.FAILED)
                if order.state is not OrderState.REFUNDED:
                    order.transition(OrderState.REFUNDED)
            except Exception:  # noqa: BLE001 - best effort
                pass
        # GUARANTEED refund: post the ledger line + wallet credit through the
        # durable outbox, so a confirmed release can never leave the customer
        # silently out of pocket (and the books stay accurate).
        #
        # If the ENGINE already posted this order's customer-refund ledger line
        # (a timeout race posted it under the order id) tell the outbox so the
        # router does not post a SECOND line under the wait-token: one order,
        # one refund line, always.
        ledger_posted = False
        if order is not None and getattr(order, "order_id", ""):
            try:
                ledger_posted = bool(
                    self.ledger.has_customer_refund(order.order_id))
            except Exception:  # noqa: BLE001 - worst case: the ledger-level
                ledger_posted = False  # guard still catches the duplicate
        ok, _ = self.refunds.request(user_id, gross, token,
                                     reason="user_cancelled",
                                     ledger_posted=ledger_posted)
        if not already:
            self._record_cancel(user_id, slug, gross)
        name = self.catalog.get(slug).name if self.catalog.has(slug) else slug
        if ok:
            return Reply(
                f"♻️ Cancelled — {name} number released and {gross} "
                f"returned to your balance ({self.balance_of(user_id)}).",
                rows=((("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
            )
        return Reply(
            f"♻️ Cancel confirmed — the number was released, and {gross} is on "
            "its way back to your balance. It will appear shortly (a refund is "
            "being retried automatically).",
            rows=((("💰 Wallet", "w"), ("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
        )

    def check_otp(self, token: str, *, wait_seconds: int = 0) -> Reply:
        """Re-check a waiting order for its OTP; deliver it when it arrives.

        ``wait_seconds`` lets a button press block a short while before giving
        up, so tapping 'Check' actually waits for a code that is seconds away
        rather than replying instantly with nothing. The transport runs this
        off the event loop (deferred), so a longer wait is fine.
        """
        _terminal, reply = self.poll_once(token, wait_seconds=wait_seconds)
        return reply

    def poll_once(self, token: str, *, wait_seconds: int = 0):
        """One bounded poll of a waiting order.

        Returns ``(terminal, reply)`` where ``terminal`` is True when the order
        has reached a FINAL state (OTP delivered, or refunded/timed out) and
        False when it is still waiting for the SMS. The transport uses this to
        auto-deliver the code in the background, so the customer does not have
        to keep tapping 💰 Check OTP; a button tap just calls the same thing
        via :meth:`check_otp`. ``wait_seconds`` defaults to the full OTP window
        (it must never be set short here, or we would refund a number that is
        merely seconds away from delivering).

        Single-flight: only ONE poll at a time per order. The background
        auto-poller and a manual Check tap both come through here; without a
        guard each would stack its own ~5-minute OTP-window wait on its own
        thread, so the button surface sat behind a second poll and "My numbers"
        appeared frozen until the number was cancelled or expired. A duplicate
        tap now returns instantly and never blocks.
        """
        with self._poll_guard:
            if token in self._polling_tokens:
                return False, Reply(
                    "⏳ Already checking this number — the code will be sent "
                    "here as soon as it arrives. You don't need to tap again.",
                    ok=False,
                    rows=((( "🧾 My numbers", "o"), ("🏠 Menu", "m")),),
                )
            self._polling_tokens.add(token)
        try:
            return self._poll_once_inner(token, wait_seconds)
        finally:
            with self._poll_guard:
                self._polling_tokens.discard(token)

    def _poll_once_inner(self, token: str, wait_seconds: int):
        entry = self._awaiting.get(token)
        if entry is not None:
            user_id, slug, result = entry
            wait = wait_seconds if wait_seconds > 0 else self.engine.config.otp_timeout_seconds
            result = self.engine.poll_otp(result, timeout_seconds=wait)
            reply = self._after_check(token, user_id, slug, result)
            return (result.success or result.refunded.paise > 0), reply
        # Restart-safe: re-enter the wait straight from the DB row.
        active = self._active_by_token(token)
        if active is None:
            return True, Reply(
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
        reply = self._after_check(token, active.user_id, active.slug, result)
        return (result.success or result.refunded.paise > 0), reply

    def poll_new_otp(self, token: str) -> Reply:
        """💰 Check new OTP: poll a DELIVERED number for an additional code.

        A number is valid ~20 minutes and can legitimately receive several
        codes (the service "resends", or the customer logs in twice). The first
        token is delivered by the normal flow; this keeps the number alive and
        returns any *newer* code the provider now has, without re-delivering
        the already-shown one.
        """
        active = self._active_by_token(token)
        if active is None or not active.provider_order_id:
            return Reply("That number has finished or is no longer waiting. "
                         "See 🧾 My numbers.", ok=False,
                         rows=((("🧾 My numbers", "o"),),))
        name = self.catalog.get(active.slug).name if self.catalog.has(active.slug) \
            else active.slug
        mins = int(round(active.seconds_left / 60)) if active.seconds_left else 0
        get_sms = getattr(self.engine.provider, "get_sms", None)
        if not callable(get_sms):
            return Reply(
                f"💰 Check new OTP isn't available for {name} right now.",
                ok=False, rows=((("🧾 My numbers", "o"),),))
        try:
            msgs = list(get_sms(active.provider_order_id))
            codes = [m.extract_otp() for m in msgs if m.extract_otp()]
        except Exception:  # noqa: BLE001 - provider hiccup, retryable
            return Reply(
                "Couldn't reach the provider right now. Tap 💰 Check new OTP "
                "again in a moment — your number is still valid.",
                ok=False,
                rows=((("💰 Check new OTP", f"nx:{token}"),), (("🏠 Menu", "m"),)),
            )
        code = (codes[-1] if codes else "").strip()
        last = (active.otp or "").strip()
        if code and code != last:
            upd = getattr(self.wallets, "update_active", None)
            if callable(upd):
                try:
                    upd(active.provider_order_id, otp=code)
                except Exception:  # noqa: BLE001 - display cache, non-fatal
                    pass
            return Reply(
                f"📬 *New OTP for {name}*: `{code}`\n📱 {active.phone}\n\n"
                f"⏳ Number valid ~{max(1, mins)} min — you can request another "
                "code on the service and tap 💰 Check new OTP again.",
                rows=((("💰 Check new OTP", f"nx:{token}"),),
                      (("🧾 My numbers", "o"), ("🏠 Menu", "m"))),
            )
        return Reply(
            f"⏳ No new code yet for {name}. The number is valid "
            f"~{max(1, mins)} min.\n\nRequest the code on the target app, then "
            "tap 💰 Check new OTP — a second code lands here on the same number.",
            rows=((("💰 Check new OTP", f"nx:{token}"),), (("🏠 Menu", "m"),)),
        )

    def terminal_reply(self, token: str) -> Optional[object]:
        """The genuine final Reply for a token resolved this process, or None."""
        return self._terminal.get(token)

    def _after_check(self, token: str, user_id: str, slug: str, result) -> Reply:
        """Shared tail of check_otp: deliver the OTP, refund on timeout, or wait."""
        if result.success and result.otp:
            self._awaiting.pop(token, None)
            # Keep the number in the live set (storing this OTP) so the customer
            # can receive MORE codes on it during its validity window (Fix #2).
            self._record_order(user_id, slug, result.order.gross_price, result,
                               keep_active=True)
            from ..catalog import PROVIDER_VALIDITY_MINUTES
            left = self._remaining_otp_minutes(getattr(result, "_alloc", None))
            if left is None or left < 5:
                left = PROVIDER_VALIDITY_MINUTES
            left = min(int(left), PROVIDER_VALIDITY_MINUTES)
            valid = f"\n⏳ Number valid for ~{left} min — you can receive more OTPs on it."
            reply = Reply(
                f"✅ OTP for {self.catalog.get(slug).name}: `{result.otp}`\n"
                f"📱 Number: {result.phone}\n\n"
                f"Charged {result.order.gross_price} · Balance {self.balance_of(user_id)}"
                f"{valid}",
                rows=(
                    (("🔁 Another", f"s:{slug}"), ("🧾 My numbers", "o")),
                    (("🏠 Menu", "m"),),
                ),
            )
            self._terminal[token] = reply
            return reply
        # Still waiting (or timed out to a refund). If poll_otp refunded, credit
        # the customer's wallet -- but ONLY once per order. Several concurrent
        # pollers (a manual 💰 Check OTP, the background auto-poller, a late
        # Check after a Cancel) can resolve the SAME order, and each used to
        # refund it again, so a single order could be credited three times.
        # The idempotent outbox guard makes the wallet credit happen exactly once.
        if not result.success:
            # Capture the "already processed" state BEFORE we apply: applying the
            # refund marks the outbox done, so a post-check would always look done.
            already_resolved = self.refunds.is_processed(token)
            if result.refunded.paise > 0 and not already_resolved:
                self.refunds.apply_timeout_refund(
                    token, user_id, result.refunded, reason="no OTP"
                )
            if already_resolved:
                # A Cancel or an earlier poll already refunded this order: never
                # credit or record it again. Return the genuine terminal reply if
                # we have one, else a benign "already done" message.
                term = self._terminal.get(token)
                if term is not None:
                    return term
                return Reply(
                    "✔️ This order is already resolved and refunded — no further "
                    "charge was made. See 🧾 My numbers.",
                    ok=False,
                    rows=((("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
                )
            # Terminal (timed out / refunded) -- clear it and the active number.
            self._awaiting.pop(token, None)
            self._record_order(user_id, slug, result.order.gross_price, result)
            reply = Reply(
                f"⚠️ No OTP arrived for {slug}. Refunded {result.refunded} in "
                f"full; balance {self.balance_of(user_id)}.",
                ok=False,
                rows=((("🧾 My numbers", "o"), ("🏠 Menu", "m")),),
            )
            self._terminal[token] = reply
            return reply
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

    def active_valid_remaining(self, token: str) -> Optional[float]:
        """Seconds the number is still valid, or -1 / None when it's gone.

        Lets the background auto-poller know when to stop listening for the
        extra OTPs a number can still receive during its validity window.
        """
        try:
            active = self._active_by_token(token)
            if active is None:
                return -1.0
        except Exception:  # noqa: BLE001
            return -1.0
        return float(active.seconds_left)

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
        # Clamp to the REAL provider validity: the provider's clock / reported
        # validity can skew high and made the UI claim "30 min" when the number
        # is only yours for 20 min, which confused customers.
        #
        # Floor to a real validity window: a freshly-allocated number must show
        # as LIVE in "My numbers" for at least the OTP window, whatever the
        # provider's allocation clock says. If ``seconds_left`` computes to 0
        # or negative (a provider skew / misparsed allocated_at), we still grant
        # the platform validity window -- otherwise the number would be recorded
        # with valid_until in the past and filtered straight out of "My numbers"
        # (the persistent "bought it but it never appears" bug).
        from ..catalog import PROVIDER_VALIDITY_MINUTES
        max_valid = PROVIDER_VALIDITY_MINUTES * 60
        # Always grant the platform lease from OUR clock (20 min). Provider
        # seconds_left can be ~1 min (clock skew) or ~100 min (misparsed
        # allocated_at); neither is the product lease.
        valid_until = time.time() + max_valid
        try:
            rec(user_id=user_id, slug=slug, phone=result.phone or "",
                provider_order_id=provider_id, token=token, gross=price,
                valid_until=valid_until)
        except Exception:  # noqa: BLE001 - a display cache must not block delivery
            pass

    def _credit_clone_owner(self, clone_gross: Money) -> None:
        """Credit the clone owner 95% of their extra (5% stays with us)."""
        if not getattr(self, "is_clone", False):
            return
        store = getattr(self, "platform_wallets", None) or self.wallets
        if store is None:
            return
        from ..reseller import credit_earnings, reseller_split
        split = reseller_split(
            clone_gross,
            getattr(self, "reseller_rate", 0) or 0,
            getattr(self, "margin_fee_rate", None) or 0,
        )
        if split.owner_share.is_zero:
            return
        try:
            credit_earnings(store, self.owner_id, split.owner_share)
        except Exception:  # noqa: BLE001 - never block a customer delivery
            log.exception("clone earnings credit failed for %s", self.owner_id)

    def _record_order(self, user_id: str, slug: str, price: Money, result,
                      *, keep_active: bool = False) -> None:
        """Persist a durable order row for history + per-order profit.

        Display cache ONLY -- the ledger stays the source of truth for money;
        this exists so /metrics, history and the owner panel can answer
        "what happened per order" without parsing journal lines.
        ``keep_active`` leaves the number in the live set so the customer can
        keep receiving MORE OTPs on it during its validity window (Fix: a
        number is valid ~20 min and can get several codes).
        """
        store = self.wallets
        rec = getattr(store, "record_order", None)
        if result.success:
            self._credit_clone_owner(price)
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
        # live/active set so it no longer shows as an active number -- unless the
        # customer still wants to receive more OTPs on it, in which case we keep
        # it alive and remember the code it already got.
        finish = getattr(store, "finish_active", None)
        alloc = getattr(result, "_alloc", None)
        provider_id = getattr(alloc, "order_id", "") if alloc else ""
        if keep_active:
            upd = getattr(store, "update_active", None)
            if callable(upd) and provider_id and result.otp:
                try:
                    upd(provider_id, otp=result.otp)
                except Exception:  # noqa: BLE001 - display cache, non-fatal
                    pass
            return
        if callable(finish) and provider_id:
            try:
                finish(provider_id)
            except Exception:  # noqa: BLE001 - display cache, non-fatal
                pass

    def _record_cancel(self, user_id: str, slug: str, gross: Money) -> None:
        """Persist a cancelled order so it appears in My numbers history.

        Display cache (like ``_record_order``); the ledger stays the source of
        truth for the money, which the durable refund already posted.
        """
        store = self.wallets
        rec = getattr(store, "record_order", None)
        if not callable(rec):
            return
        try:
            rec(user_id=user_id, slug=slug, amount=gross, success=False,
                status="cancelled", reason="cancelled by user", refunded=gross,
                balance_after=self.balance_of(user_id))
        except Exception:  # noqa: BLE001 - display cache, non-fatal
            pass

    # -- white-label -----------------------------------------------------
    def cmd_createbot(self, user_id: str, args: list[str]) -> Reply:
        assert self._createbot_flow is not None
        # Owner can flip "Run your own bot" off from the admin panel; the UI
        # hides the button, and the command must refuse too so a typed
        # /createbot can't bypass it.
        fn = getattr(self, "createbot_enabled_fn", None)
        if callable(fn) and not fn():
            return Reply(
                "🤖 'Run your own bot' is currently turned OFF by the owner. "
                "You can't create a clone right now.\n\n"
                "Existing clones keep working. Ask the owner to turn the "
                "feature back on to create more.",
                ok=False,
            )
        if args:
            # `/createbot <token>` is accepted as a shortcut past the hub.
            self._createbot_flow.start(user_id)
            return self._createbot_reply(self._createbot_flow.on_text(user_id, args[0]))
        return self._createbot_reply(self._createbot_flow.hub())

    def cmd_cancel(self, user_id: str, args: list[str]) -> Reply:
        if self._createbot_flow and self._createbot_flow.pending(user_id):
            self._createbot_flow.cancel(user_id)
            return Reply("Cancelled. Nothing was created.")
        return Reply("Nothing in progress.", ok=False)

    def cmd_mybots(self, user_id: str, args: list[str]) -> Reply:
        assert self.subbots is not None
        add_row = ((("➕ Create / add bot", CB_HUB_ADD),),)
        bots = self.subbots.for_owner(user_id)
        if not bots:
            return Reply(
                "You have no bots yet. Tap ➕ Create / add bot to make one.",
                ok=False,
                rows=add_row,
            )
        # Live poller health from the running manager, so a bot that is saved in
        # the registry but whose poller crashed / token is invalid shows as DOWN
        # with a reason instead of looking fine.
        running = set()
        errors: dict[str, str] = {}
        manager = self.subbot_manager
        if manager is not None:
            try:
                running = set(getattr(manager, "running", lambda: [])())
                errors = dict(getattr(manager, "errors", lambda: {})())
            except Exception:  # noqa: BLE001 - never break /mybots
                running, errors = set(), {}
        lines = []
        for b in bots:
            mode = "platform numbers" if b.mode.value == "platform_api" else "your own API"
            if b.active and b.id in running:
                token = "🟢"
                state = "live & polling"
            elif b.active:
                token = "🔴"
                state = "saved but NOT polling"
            else:
                token = "⚪"
                state = "stopped"
            err = errors.get(b.id)
            if err:
                state += f" — {err}"
            extra = getattr(b, "reseller_rate", None)
            if b.mode.value == "platform_api":
                if extra:
                    fee = f"extra {extra:.0%} on our prices · we keep 5% of extra"
                else:
                    fee = "platform numbers, no extra set"
            else:
                fee = f"platform fee {b.fee.describe()}"
            lines.append(f"{token} `{b.id}` - {mode}, {fee}, {state}")
        tip = (
            "\n\nA bot that is saved but not polling usually means the token "
            "isn't a real BotFather token (or lost access). Delete it with "
            "/deletebot and /createbot again with a fresh token from "
            "@BotFather → /newbot."
            if any("saved but NOT polling" in l for l in lines)
            else "\n\nManage with /stopbot and /deletebot."
        )
        return Reply(
            "Your bots:\n" + "\n".join(lines) + tip,
            rows=add_row,
        )

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
        return Reply(
            result.reply,
            buttons=tuple(result.buttons),
            rows=tuple(result.rows) if getattr(result, "rows", None) else (),
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

    # -- admin commands (owner-only) -------------------------------------
    def cmd_metrics(self, user_id: str, args: list[str]) -> Reply:
        """📊 Operational snapshot: users, float, orders, P&L, supplier wallet."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        status = self.engine.status()
        pnl = status["pnl"]
        fs = None
        store = self.wallets
        fn = getattr(store, "float_stats", None) if store is not None else None
        if callable(fn):
            try:
                fs = fn()
            except Exception:  # noqa: BLE001
                fs = None
        uids = []
        ufn = getattr(store, "user_ids", None) if store is not None else None
        if callable(ufn):
            try:
                uids = list(ufn())
            except Exception:  # noqa: BLE001
                uids = []
        order_lines = []
        ofn = getattr(store, "recent_orders", None) if store is not None else None
        if callable(ofn):
            try:
                orders = list(ofn(limit=100))
                delivered = sum(1 for o in orders if o.success)
                refunded = sum(1 for o in orders if not o.success)
                order_lines = [f"Orders (last 100): {len(orders)} · ✅ {delivered} · ♻️ {refunded}"]
            except Exception:  # noqa: BLE001
                pass
        lines = [
            "📊 TITAN metrics",
            f"\n👥 Users: {len(uids)}",
            f"💵 Customer float: {fs['float'] if fs else '—'}",
            f"\n🏦 Supplier wallet: {status['provider_wallet']}",
            f"📒 Ledger wallet: {status['ledger_wallet']}",
            f"\n💹 Revenue: {pnl['revenue']} · COGS: {pnl['cogs']} · Net: {pnl['net_profit']}",
        ]
        if order_lines:
            lines.append("\n" + order_lines[0])
        pending_refunds = self.refund_pending_count()
        if pending_refunds:
            lines.append(f"↩️ Refunds awaiting credit: {pending_refunds}")
        pinfo = getattr(self.pricer, "target_margin", None)
        if pinfo is not None:
            lines.append(f"🎯 Target margin: {pinfo:.0%}" if hasattr(pinfo, "__truediv__") else f"🎯 Target margin: {pinfo}")
        return Reply("\n".join(lines))

    def cmd_admin(self, user_id: str, args: list[str]) -> Reply:
        """📊 Owner panel. Opens the same panel as the ⚙️ Owner button."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False, buttons=(("🏠 Menu", "m"),))
        from .ui import MenuUI  # local: avoids an import cycle at module level
        return MenuUI(self).admin_panel(user_id)

    def cmd_sunkcost(self, user_id: str, args: list[str]) -> Reply:
        """🕳 Hidden cost of EARLY_CANCEL_DENIED refusals (Phase-1 P4)."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        return Reply(self._cancel_tracker.report())

    def cmd_provider(self, user_id: str, args: list[str]) -> Reply:
        """🏦 Supplier wallet + which operator/pool the rail is using."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        status = self.engine.status()
        return Reply(
            f"🏦 Supplier: {status['provider']}\n"
            f"Supplier wallet: {status['provider_wallet']}\n"
            f"Ledger wallet: {status['ledger_wallet']}\n"
            f"Net profit: {status['pnl']['net_profit']}\n"
            f"Revenue: {status['pnl']['revenue']}"
        )

    def cmd_credit(self, user_id: str, args: list[str]) -> Reply:
        """💳 /credit <user_id> <amount> — add balance to a customer."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        if getattr(self, "is_clone", False):
            return Reply(
                "Clone bots can't add or deduct balances. Customers pay "
                "through our UPI.",
                ok=False,
            )
        if len(args) < 2:
            return Reply("Usage: /credit <user_id> <amount> (e.g. /credit 123456789 250)", ok=False)
        target, amount_s = args[0], args[1]
        try:
            amount = INR(amount_s)
        except Exception:  # noqa: BLE001
            return Reply(f"Bad amount '{amount_s}'. Use rupees, e.g. 250 or 20.50.", ok=False)
        if amount.is_negative or amount.is_zero:
            return Reply("Amount must be positive.", ok=False)
        try:
            new = self.credit(target, amount)
        except Exception as exc:  # noqa: BLE001
            return Reply(f"Could not credit: {exc}", ok=False)
        return Reply(
            f"✅ Credited {amount} to `{target}` (balance now {new}).",
            notify=((target, f"💳 Deposit confirmed! {amount} added to your balance (now {new}).")),
        )

    def cmd_debit(self, user_id: str, args: list[str]) -> Reply:
        """↩️ /debit <user_id> <amount> — take back a balance (admin adjust)."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        if getattr(self, "is_clone", False):
            return Reply(
                "Clone bots can't add or deduct balances. Customers pay "
                "through our UPI.",
                ok=False,
            )
        if len(args) < 2:
            return Reply("Usage: /debit <user_id> <amount>", ok=False)
        target, amount_s = args[0], args[1]
        try:
            amount = INR(amount_s)
        except Exception:  # noqa: BLE001
            return Reply(f"Bad amount '{amount_s}'.", ok=False)
        if amount.is_negative or amount.is_zero:
            return Reply("Amount must be positive.", ok=False)
        try:
            new = self._debit(target, amount)
        except Exception as exc:  # noqa: BLE001
            return Reply(f"Could not debit: {exc}", ok=False)
        return Reply(f"↩️ Debited {amount} from `{target}` (balance now {new}).")

    def cmd_ban(self, user_id: str, args: list[str], *, ban: bool = True) -> Reply:
        """🚫 /ban <user_id> | /unban <user_id> — owner-only access control.

        Maintains the dedicated ban set (not the allowlist): banned users are
        refused in every mode, and banning one user can never change who else
        is allowed. The owner can never be banned.
        """
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        if not args:
            return Reply("Usage: /ban <user_id>  or  /unban <user_id>", ok=False)
        target = args[0]
        if self.owner_id and target == self.owner_id:
            return Reply("The owner cannot be banned.", ok=False)
        if ban:
            if target in self._banned:
                return Reply(f"`{target}` is already banned. Use /unban to restore access.")
            self._banned.add(target)
            return Reply(f"🚫 Banned `{target}` — they can no longer buy until unbanned.")
        if target not in self._banned:
            return Reply(f"`{target}` is not banned.")
        self._banned.discard(target)
        return Reply(f"✅ Unbanned `{target}` — they can buy again.")

    def cmd_maintenance(self, user_id: str, args: list[str]) -> Reply:
        """🛠 /maintenance [on|off] — pivot buying (owner)."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        # Report the real state (the persistent toggle is the Owner panel
        # button in the UI, which writes the KV flag). Never fake an override
        # here that would desync the persisted flag.
        cur = self.maintenance_fn() if self.maintenance_fn else False
        return Reply(
            f"🛠 Maintenance is currently {'ON — buying paused for customers' if cur else 'OFF — buying is live'}.\n"
            "To flip it persistently, use the Owner panel: ⚙️ Owner → 🛠 Toggle maintenance."
        )

    def cmd_orders(self, user_id: str, args: list[str]) -> Reply:
        """📦 /orders [user_id] — recent orders (admin)."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        store = self.wallets
        ofn = getattr(store, "recent_orders", None) if store is not None else None
        if not callable(ofn):
            return Reply("No order store on this deployment (nothing persisted yet).", ok=False)
        uid = args[0] if args else ""
        try:
            orders = list(ofn(user_id=uid, limit=20))
        except Exception as exc:  # noqa: BLE001
            return Reply(f"Could not read orders: {exc}", ok=False)
        if not orders:
            return Reply(f"No orders{' for ' + uid if uid else ''} yet.")
        lines = [f"📦 Orders ({'user ' + uid if uid else 'all'}):"]
        for o in orders:
            when = format_ts(o.ts, sep=" · ", time_fmt="%H:%M")
            name = self.catalog.get(o.slug).name if self.catalog.has(o.slug) else o.slug
            badge = "✅" if o.success else "♻️"
            lines.append(f"\n{badge} #{o.id} · {name} · {o.gross} · {o.status or ('delivered' if o.success else 'refunded')} · {when}")
            lines.append(f"    {o.phone} · user `{o.user_id}`")
        return Reply("\n".join(lines))

    def cmd_users(self, user_id: str, args: list[str]) -> Reply:
        """👥 /users [page] — every customer who used the bot (owner).

        Includes /start-only users (₹0, no wallet row) via the seen-users
        directory, plus anyone with a historical order/top-up/wallet. Pages
        of ``_USERS_PAGE``; never silently truncates.
        """
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        page = 0
        if args:
            try:
                page = max(0, int(str(args[0]).strip()))
            except ValueError:
                page = 0
        store = self.wallets
        lister = getattr(store, "list_users", None) if store is not None else None
        if callable(lister):
            try:
                page_rows, total = lister(
                    limit=_USERS_PAGE, offset=page * _USERS_PAGE)
            except Exception as exc:  # noqa: BLE001
                return Reply(f"Could not read users: {exc}", ok=False)
        else:
            ufn = getattr(store, "user_ids", None) if store is not None else None
            if callable(ufn):
                try:
                    uids = list(ufn())
                except Exception as exc:  # noqa: BLE001
                    return Reply(f"Could not read users: {exc}", ok=False)
            else:
                uids = list(self.balances.keys())
            total = len(uids)
            chunk = uids[page * _USERS_PAGE: (page + 1) * _USERS_PAGE]
            page_rows = [(u, 0.0) for u in chunk]
        if total == 0:
            return Reply("No customers yet.")
        pages = max(1, (total + _USERS_PAGE - 1) // _USERS_PAGE)
        if page >= pages and pages:
            page = pages - 1
            if callable(lister):
                try:
                    page_rows, total = lister(
                        limit=_USERS_PAGE, offset=page * _USERS_PAGE)
                except Exception:  # noqa: BLE001
                    pass
            else:
                page_rows = [(u, 0.0) for u in uids[
                    page * _USERS_PAGE: (page + 1) * _USERS_PAGE]]
        lines = [f"👥 Customers ({total}) · page {page + 1}/{pages}"]
        for u, seen in page_rows:
            bal = self.balance_of(u)
            tag = " 👑" if self._is_owner(u) else ""
            when = ""
            if seen:
                when = f" · {format_ts(seen, sep=' ', time_fmt='%H:%M')}"
            lines.append(f"\n`{u}` · {bal}{when}{tag}")
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("◀️ Prev", f"ax:users:{page - 1}"))
        if (page + 1) * _USERS_PAGE < total:
            nav.append(("Next ▶️", f"ax:users:{page + 1}"))
        rows: list[tuple[tuple[str, str], ...]] = []
        if nav:
            rows.append(tuple(nav))
        rows.append((("◀️ Owner panel", "a"),))
        return Reply("\n".join(lines), rows=tuple(rows))

    def cmd_broadcast(self, user_id: str, args: list[str]) -> Reply:
        """📢 /broadcast <message> — announce to every authorised customer."""
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        msg = " ".join(args).strip()
        if not msg:
            return Reply("Usage: /broadcast <your announcement>", ok=False)
        store = self.wallets
        ufn = getattr(store, "user_ids", None) if store is not None else None
        targets: list[str] = []
        if callable(ufn):
            try:
                targets = [u for u in ufn() if not self._is_owner(u)]
            except Exception:  # noqa: BLE001
                targets = []
        else:
            targets = [u for u in self.balances if not self._is_owner(u)]
        notify = tuple((t, f"📢 Announcement:\n\n{msg}") for t in targets)
        return Reply(
            f"📢 Sent '{msg}' to {len(targets)} customer(s).",
            notify=notify,
        )

    def cmd_setmargin(self, user_id: str, args: list[str]) -> Reply:
        """🎯 /setmargin <fraction> — set the live pricing margin (0.35 = 35%).

        In the deployed ``exact_markup`` strategy the owner's lever is the
        markup, not the margin, so this command sets the markup rate (0.45 =
        45% price = cost x 1.45). In the legacy ``target_margin`` strategy it
        sets the margin as before.
        """
        if not self._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        markup_mode = getattr(self.pricer, "strategy", None) is not None \
            and str(self.pricer.strategy.value) == "exact_markup"
        if not args:
            if markup_mode:
                return Reply(f"Current markup: {getattr(self.pricer, 'markup', '?')}")
            return Reply(f"Current target margin: {getattr(self.pricer, 'target_margin', '?')}")
        try:
            from decimal import Decimal
            new = Decimal(args[0])
        except Exception:  # noqa: BLE001
            return Reply("Bad value. Use a fraction, e.g. 0.45 (45% markup).", ok=False)
        if not (0 < new <= 2):
            return Reply("Value must be between 0 and 2 (e.g. 0.45).", ok=False)
        if markup_mode:
            setattr(self.pricer, "markup", new)
            return Reply(f"🎯 Markup set to {new} — prices are now cost x {1 + new}.")
        setattr(self.pricer, "target_margin", new)
        return Reply(f"🎯 Target margin set to {new} ({new:.0%}).")

