"""Button-first guided interface.

Commands are for developers; customers get buttons. A purchase is three
taps -- [🛒 Buy] → [the service, price printed on it] → [✅ Buy now] -- and
the OTP is edited into the same message when it lands.

Everything here is transport-free: the module speaks in :class:`Reply`
objects (same contract as the /createbot flow), so the whole guided
experience is unit-testable without Telegram and ``telegram.py`` stays a
thin shell.

Money never moves here: the final ["✅ Buy now"] tap calls
``router.handle(user, "/buy <slug>")`` -- one line through the exact path
/commands already use, so wallet debit, fulfilment, refund and ledger
postings exist in precisely one place.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional

from ..catalog import Catalog
from ..pricing import Pricer
from .commands import CommandRouter, Reply

__all__ = ["MenuUI", "WELCOME"]

WELCOME = """👋 Welcome to UOTP numbers.

Get a real phone number, receive your OTP in Telegram, pay only on success.
Prices include everything — no hidden fees, and a failed order refunds itself
automatically within seconds.

Tap 🛒 Buy a number to start."""

_HELP = """ℹ️ How it works

1. Top up: message the bot owner.
2. 🛒 Buy a number → pick a service → ✅ Buy now.
3. The number + OTP appear right here. Average wait ~1 minute.
4. No OTP in ~5 minutes? Automatic full refund, no questions.

Tips
• A number is yours for 20 minutes and can receive the service's OTPs
  more than once inside that window.
• /wallet, /price <service> and /buy <service> still work if you prefer
  typing — commands remain fully supported.
• Prices refresh with the provider's catalogue; the price printed on the
  Buy button is always the one you are charged."""


#: Slug → emoji. Cosmetics only; unknown slugs get the default dot.
_EMOJI: dict[str, str] = {
    "telegram": "✈️", "whatsapp": "🟢", "discord": "🎮", "line": "🟩",
    "wechat": "🐉", "snapchat": "👻", "viber": "☎️", "signal": "📶",
    "instagram": "📸", "facebook": "👥", "twitter": "🐦", "tiktok": "🎵",
    "youtube": "▶️", "vk": "🌊", "reddit": "🤖",
    "google": "🔍", "microsoft": "🪟", "apple": "🍎", "yahoo": "🟣",
    "steam": "🎯", "epicgames": "🕹️",
    "uber": "🚗", "ola": "🚕", "bolt": "⚡", "grab": "🛺", "lyft": "🚙",
    "deliveroo": "🍟", "doordash": "🍔", "swiggy": "🍜", "zomato": "🍅",
    "booking": "🛎️", "airbnb": "🏠", "expedia": "🧳", "agoda": "🌏",
    "netflix": "🎬", "spotify": "🎧", "hotstar": "📺", "prime": "📦",
    "amazon": "🛒", "flipkart": "🛍️", "ebay": "🔨", "aliexpress": "🧧",
    "binance": "🪙", "coinbase": "🔵", "kraken": "🐙", "wazirx": "📈",
    "paypal": "💳", "paytm": "💸", "phonepe": "💰", "gpay": "💵",
    "tinder": "❤️", "bumble": "💛", "hinge": "💜", "okcupid": "💘",
    "linkedin": "💼",
}
_CATEGORY_EMOJI: dict[str, str] = {
    "messaging": "💬", "social": "🌐", "dating": "❤️", "transport": "🚗",
    "gaming": "🎮", "food": "🍔", "travel": "✈️", "entertainment": "🎬",
    "tech": "💻", "shopping": "🛍️", "crypto": "🪙", "finance": "🏦",
    "professional": "💼", "other": "📦",
}

# A number is live this long; mirrored from catalog constants so the UI
# copy cannot silently drift from engine reality.
_VALIDITY = "20 minutes"


def _emoji(slug: str) -> str:
    return _EMOJI.get(slug, "🔹")


class MenuUI:
    """Guided interface over a :class:`CommandRouter`.

    Keeps zero money state itself; the only per-process state is the
    order-history cache, which is openly labelled "this session" because a
    redeploy legitimately loses it.
    """

    def __init__(
        self,
        router: CommandRouter,
        *,
        support_contact: str = "",
        history_size: int = 20,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.router = router
        self.catalog: Catalog = router.catalog
        self.pricer: Pricer = router.pricer
        self.support_contact = support_contact.strip()
        #: user_id -> [(timestamp, slug, ok, one-line summary)].
        #: Session-scoped by design; say that on the screen.
        self._history: dict[str, list[tuple[float, str, bool, str]]] = {}
        self._history_size = history_size
        self._now = now or time.time

    # -- text entry ------------------------------------------------------
    def text(self, user_id: str, body: str) -> Reply:
        """One funnel for typed messages.

        /start and /help open the menu; /createbot and the rest of the
        command surface keep working verbatim for power users; anything
        else is either an in-progress /createbot answer (handled by the
        router) or gets the menu. A customer should never parse prose to
        guess what to type next.
        """
        body = (body or "").strip()
        low = body.lower()
        if low in ("/start", "/help"):
            return self.main_menu(user_id)
        flow = self.router._createbot_flow  # pending-token funnel lives there
        if flow is not None and flow.pending(user_id) and not body.startswith("/"):
            return self.router.handle(user_id, body)
        if body.startswith("/"):
            return self.router.handle(user_id, body)
        return self.main_menu(user_id)

    # -- the menu tree ---------------------------------------------------
    def main_menu(self, user_id: str) -> Reply:
        balance = self.router.balance_of(user_id)
        rows = [
            (("🛒 Buy a number", "l"),),
            ((f"💰 Balance: {balance}", "w"), ("🧾 My numbers", "o")),
            (("❓ How it works", "h"),),
        ]
        if self._can_createbot():
            rows.append((("🤖 Run your own bot", "cb"),))
        if self.router._is_owner(user_id):
            rows.append((("📊 Owner: profit & health", "a"),))
        return Reply(
            f"✳️ UOTP Numbers\n\n💰 Your balance: {balance}\n\n"
            "What would you like to do?",
            rows=tuple(rows),
        )

    def services_grid(self, user_id: str, category: Optional[str] = None) -> Reply:
        """All services as tap-targets, price printed on each button.

        32 buttons sit fine on one screen as 2-column rows, so there is no
        pagination to lose state in -- every callback carries its full
        context, which also means a stale button can never double-charge:
        the price is recomputed at confirm time, not trusted from an old
        screen.
        """
        services = [
            s for s in self.catalog.services()
            if category is None or s.category == category
        ]
        if not services:
            return Reply(
                f"No services in {category or 'the catalogue'} right now.",
                ok=False,
                buttons=(("🏠 Menu", "m"),),
            )
        rows = []
        for i in range(0, len(services), 2):
            row = tuple(
                (f"{_emoji(s.slug)} {s.name} · {self.pricer.price(s).gross_price}",
                 f"s:{s.slug}")
                for s in services[i:i + 2]
            )
            rows.append(row)
        cats = sorted({s.category for s in self.catalog.services()})
        if category is None and len(cats) > 1:
            chips = []
            for i in range(0, len(cats), 4):
                chips.append(tuple(
                    (f"{_CATEGORY_EMOJI.get(c, '📦')} {c}", f"c:{c}")
                    for c in cats[i:i + 4]
                ))
            rows.extend(chips)
        rows.append((("🏠 Menu", "m"),))
        title = (
            "🛒 Pick a service"
            if category is None
            else f"{_CATEGORY_EMOJI.get(category, '📦')} {category.title()}"
        )
        return Reply(f"{title}\n\nPrice on each button is final — tap one:", rows=tuple(rows))

    def service_card(self, user_id: str, slug: str) -> Reply:
        if not self.catalog.has(slug):
            return Reply(
                "That service just left the catalogue. Here is the current list:",
                ok=False,
                rows=((("🛒 Browse", "l"),),),
            )
        cost = self.catalog.get(slug)
        advice = self.pricer.price(cost)
        balance = self.router.balance_of(user_id)
        short = advice.gross_price - balance
        warn = (
            f"\n\n⚠️ Your balance is {balance} — you'll need {short} more."
            if balance.paise < advice.gross_price.paise
            else f"\n\n💰 Your balance covers it ({balance})."
        )
        rows = (
            ((f"✅ Buy now · {advice.gross_price}", f"y:{slug}"),),
            (("◀️ Back to services", "l"), ("🏠 Menu", "m")),
        )
        return Reply(
            f"{_emoji(slug)} {cost.name}\n\n"
            f"Price: {advice.gross_price} (all-in, no hidden fees)\n"
            f"Country: 🇮🇳 India · Operator: any\n"
            f"Number yours for {_VALIDITY}; OTP delivered here.\n"
            f"🛟 No OTP in ~5 min = automatic full refund."
            f"{warn}",
            rows=rows,
        )

    def wallet_card(self, user_id: str) -> Reply:
        contact = self.support_contact or "the bot owner"
        return Reply(
            f"💰 Balance: {self.router.balance_of(user_id)}\n\n"
            f"To add money, message {contact} with your Telegram ID "
            f"(yours: {user_id}) and the amount. Payments by UPI; your wallet "
            f"here is credited as soon as the owner confirms.",
            rows=((("🛒 Buy a number", "l"), ("🏠 Menu", "m")),),
        )

    def history_card(self, user_id: str) -> Reply:
        entries = self._history.get(user_id, [])
        if not entries:
            return Reply(
                "🧾 No numbers this session.\n\n"
                "Bought ones are listed here with the OTP that arrived.",
                rows=((("🛒 Buy a number", "l"), ("🏠 Menu", "m")),),
            )
        lines = ["🧾 This session's numbers (newest first):"]
        for ts, slug, ok, summary in entries:
            when = time.strftime("%H:%M", time.localtime(ts))
            lines.append(f"\n{'✅' if ok else '♻️ refunded'} {when} — {summary}")
        return Reply(
            "\n".join(lines),
            rows=((("🛒 Buy again", "l"), ("🏠 Menu", "m")),),
        )

    def admin_panel(self, user_id: str) -> Reply:
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False, buttons=(("🏠 Menu", "m"),))
        status = self.router.engine.status()
        pnl = status["pnl"]
        return Reply(
            "📊 Owner panel\n\n"
            f"Provider wallet: {status['provider_wallet']}\n"
            f"Ledger wallet:   {status['ledger_wallet']}\n"
            f"Revenue: {pnl['revenue']} · Costs: {pnl['cogs']}\n"
            f"Net profit: {pnl['net_profit']}\n\n"
            "Full P&L: /report · Health: /status",
            rows=((("🏠 Menu", "m"),),),
        )

    # -- buttons ---------------------------------------------------------
    def button(self, user_id: str, data: str) -> Reply:
        """Dispatch one tap. Unknown/stale presses fall back to the menu:
        a wrong guess here can never spend money, because spending requires
        the confirm step, which re-quotes the price live.
        """
        if not self.router._authorised(user_id):
            return Reply("You are not authorised to use this bot.", ok=False)
        if not data:
            return Reply("Hmm, that button said nothing. Fresh menu:", ok=False,
                         rows=((("🏠 Menu", "m"),),))
        parts = data.split(":")
        kind = parts[0]
        if kind == "m":
            return self.main_menu(user_id)
        if kind == "l":
            return self.services_grid(user_id)
        if kind == "c" and len(parts) == 2:
            return self.services_grid(user_id, parts[1])
        if kind == "s" and len(parts) == 2:
            return self.service_card(user_id, parts[1])
        if kind == "w":
            return self.wallet_card(user_id)
        if kind == "o":
            return self.history_card(user_id)
        if kind == "h":
            return Reply(_HELP, rows=((("🛒 Buy a number", "l"), ("🏠 Menu", "m")),))
        if kind == "a":
            return self.admin_panel(user_id)
        if kind == "cb":
            if not self._can_createbot():
                return Reply("White-label bots are not enabled on this bot.",
                             ok=False, rows=((("🏠 Menu", "m"),),))
            return self.router.handle(user_id, "/createbot")
        if kind == "nop":
            return Reply("Already handled.", rows=((("🏠 Menu", "m"),),))
        if kind == "y" and len(parts) == 2:
            return self._begin_purchase(user_id, parts[1])
        # Unknown: menus advance; they never rotate an old menu back.
        return Reply("That menu moved on. Fresh one:", ok=False,
                     rows=((("🏠 Menu", "m"),),))

    # -- purchase --------------------------------------------------------
    def _begin_purchase(self, user_id: str, slug: str) -> Reply:
        """Answer the ✅ Buy now tap instantly; the real work runs deferred.

        ``deferred`` is the transport's contract: edit the placeholder,
        run this callable off the event loop, edit the message again with
        its Reply. Spending happens inside the command path, nowhere else.
        """
        if not self.catalog.has(slug):
            return Reply("That service just left the catalogue. Refreshed list:",
                         ok=False, rows=((("🛒 Browse", "l"),),))
        cost = self.catalog.get(slug)
        price = self.pricer.price(cost).gross_price

        def job(uid: str) -> Reply:
            reply = self.router.handle(uid, f"/buy {slug}")
            self._record(uid, slug, reply)
            return self._outcome(slug, reply)

        return Reply(
            f"⏳ Buying {cost.name} for {price}…\n\n"
            "Reserving a number now; the OTP will be edited into THIS message.\n"
            "Usually under a minute; automatic refund if nothing arrives.",
            deferred=job,
        )

    def _record(self, user_id: str, slug: str, reply: Reply) -> None:
        first = reply.text.splitlines()[0] if reply.text else "(no detail)"
        entries = self._history.setdefault(user_id, [])
        entries.insert(0, (self._now(), slug, reply.ok, first[:120]))
        del entries[self._history_size:]

    def _outcome(self, slug: str, reply: Reply) -> Reply:
        rows = (
            ((f"🔁 Another {_emoji(slug)}", f"s:{slug}"), ("🧾 My numbers", "o")),
            (("🏠 Menu", "m"),),
        )
        return replace(reply, rows=rows)

    def _can_createbot(self) -> bool:
        return self.router.subbots is not None
