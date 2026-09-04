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
from ..money import Money
from ..pricing import Pricer
from ..tz import format_ts
from .commands import CommandRouter, Reply

__all__ = ["MenuUI", "WELCOME"]

WELCOME = """👋 Welcome to YCOTP numbers.

Get a real phone number, receive your OTP in Telegram, pay only on success.
Prices include everything — no hidden fees, and a failed order refunds itself
automatically within seconds.

Tap 🛒 Buy a number below, or just type the service name (e.g. `zomato`)."""

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
    "professional": "💼", "other": "📦", "betting": "🎰", "payments": "💸",
    "trading": "📈", "edtech": "📚", "govt": "🏛️", "jobs": "💼",
    "health": "💊", "streaming": "📺",
}

# A number is live this long; mirrored from catalog constants so the UI
# copy cannot silently drift from engine reality.
_VALIDITY = "20 minutes"

#: The label -> callback pair(s) for the persistent bottom menu
#: (ReplyKeyboardMarkup). Customers tap these without typing; Telegram delivers
#: the button text as a normal message, so ``text()`` routes it here.
_REPLY_MENU: tuple[tuple[tuple[str, str], ...], ...] = (
    (("🛒 Buy Number", "l"), ("🧾 My Numbers", "o")),
    (("💰 Wallet", "w"), ("❓ Help", "h")),
    (("⭐ Favourites", "fav"), ("🆘 Support", "support")),
    (("⚙️ Admin Panel", "a"),),
)
REPLY_MENU_LABELS: dict[str, str] = {
    label: cb for row in _REPLY_MENU for label, cb in row
}
#: Case-insensitive lookup (a Telegram reply-keyboard press arrives lowercased).
REPLY_MENU_LABELS_LOW: dict[str, str] = {
    label.lower(): cb for label, cb in REPLY_MENU_LABELS.items()
}


def _emoji(slug: str) -> str:
    return _EMOJI.get(slug, "🔹")


def _int(text: str, default: int) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


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
        pay_upi_id: str = "",
        history_size: int = 20,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.router = router
        self.catalog: Catalog = router.catalog
        self.pricer: Pricer = router.pricer
        #: Default support contact (from SUPPORT_CONTACT env). The owner can
        #: change it live from the admin panel, which persists an override in
        #: the wallet store's kv table; ``support_contact`` reads that first.
        self._support_default = support_contact.strip()
        #: UPI VPA customers pay to (owner sets PAY_UPI_ID). The QR image is
        #: set by the owner from inside the bot and persisted in the wallet
        #: store's kv table -- it survives redeploys, unlike disk files.
        self.pay_upi_id = pay_upi_id.strip()
        self._maintenance_memory = False  # in-memory fallback if no kv store
        #: user_id -> [(timestamp, slug, ok, one-line summary)].
        #: Session-scoped by design; say that on the screen.
        self._history: dict[str, list[tuple[float, str, bool, str]]] = {}
        self._history_size = history_size
        #: In-memory favourites fallback when there is no kv store (tests/dev).
        self._fav_fallback: dict[str, list[str]] = {}
        self._now = now or time.time
        #: Active top-up / QR wizards. Session-scoped is fine: a restart
        #: just makes the customer re-tap Add Money, it never loses money
        #: (rows are written only after the screenshot lands).
        self._wizard: dict[str, dict] = {}

    def favourites_card(self, user_id: str) -> Reply:
        """⭐ Favourites: the services this customer starred, each buyable."""
        favs = self.favourites(user_id)
        if not favs:
            return Reply(
                "⭐ Favourites\n\nYou haven't starred anything yet. Open a service "
                "and tap ★ to save it here for one-tap access.",
                rows=((("🛒 Browse services", "l"), ("🏠 Menu", "m")),),
            )
        rows: list[tuple[tuple[str, str], ...]] = []
        lines = ["⭐ Your favourites:\n"]
        for slug in favs:
            try:
                cost = self.catalog.get(slug)
                name = cost.name
                price = self.pricer.price(cost).gross_price
            except Exception:  # noqa: BLE001 - a removed service shouldn't blank it
                continue
            lines.append(f"{_emoji(slug)} {name} · {price}")
            rows.append(((f"✅ Buy · {price}", f"y:{slug}"),
                         (f"★ Remove", f"fvt:{slug}")))
        rows.append((("🛒 Browse services", "l"), ("🏠 Menu", "m")))
        return Reply("\n".join(lines), rows=tuple(rows))

    def support_card(self, user_id: str) -> Reply:
        """🆘 Support screen: how to reach the owner."""
        contact = self.support_contact
        if contact:
            # If it's a bare @username, keep the @; the helper formats it.
            handle = contact if contact.startswith("@") else f"@{contact}"
            lines = (
                f"🆘 Need help?\n\n"
                f"Message the owner on Telegram: {handle}\n\n"
                "For issues, include the order/number and what happened. "
                "Refunds happen automatically if no OTP arrives."
            )
            rows = (("🛒 Buy a number", "l"), ("🧾 My numbers", "o"),
                    ("🏠 Menu", "m"))
            return Reply(lines, rows=rows)
        return Reply(
            "🆘 Need help?\n\n"
            "Support is being set up — message the bot owner directly for now.",
            rows=((("🛒 Buy a number", "l"), ("🏠 Menu", "m")),),
        )

    # -- stores / small helpers -------------------------------------------
    @property
    def _store(self):
        """The wallet store backing top-ups, or None in dict-mode (tests/dev)."""
        return getattr(self.router, "wallets", None)

    @property
    def support_contact(self) -> str:
        """The support username customers see, live-editable by the owner.

        An owner-set override wins (persisted in the wallet kv table, so it
        survives redeploys); otherwise the SUPPORT_CONTACT env default is used.
        """
        store = self._store_for_kv()
        if store is not None:
            try:
                v = store.kv_get("support_contact")
                if v:
                    return v
            except Exception:  # noqa: BLE001 - fall back to default
                pass
        return self._support_default

    def _set_support_contact(self, value: str) -> None:
        """Persist the owner-edited support username."""
        store = self._store_for_kv()
        if store is not None:
            try:
                store.kv_set("support_contact", value.strip())
                return
            except Exception:  # noqa: BLE001
                pass
        # In-memory fallback (tests / no kv store).
        self._support_default = value.strip()

    # -- favourites ------------------------------------------------------
    def _fav_key(self, user_id: str) -> str:
        return f"fav:{user_id}"

    def favourites(self, user_id: str) -> list[str]:
        """The slugs this user has starred, in the order added."""
        store = self._store_for_kv()
        raw = ""
        if store is not None:
            try:
                raw = store.kv_get(self._fav_key(user_id)) or ""
            except Exception:  # noqa: BLE001
                raw = ""
        else:
            raw = ",".join(self._fav_fallback.get(user_id, []))
        return [s for s in raw.split(",") if s and self.catalog.has(s)]

    def is_favourite(self, user_id: str, slug: str) -> bool:
        return slug in self.favourites(user_id)

    def toggle_favourite(self, user_id: str, slug: str) -> bool:
        """Flip a service's starred state; returns True if now a favourite."""
        cur = self.favourites(user_id)
        if slug in cur:
            cur.remove(slug)
            fav = False
        else:
            cur.append(slug)
            fav = True
        store = self._store_for_kv()
        if store is not None:
            try:
                store.kv_set(self._fav_key(user_id), ",".join(cur))
            except Exception:  # noqa: BLE001 - non-fatal
                pass
        else:
            self._fav_fallback[user_id] = cur
        return fav

    def maintenance_on(self) -> bool:
        """True while the owner has paused buying (admin panel toggle)."""
        store = self._store_for_kv()
        if store is not None:
            return (store.kv_get("maintenance") or "0") == "1"
        return self._maintenance_memory

    def _toggle_maintenance(self) -> bool:
        """Flip the flag; returns the new state. Persisted across redeploys."""
        new = not self.maintenance_on()
        store = self._store_for_kv()
        if store is not None:
            store.kv_set("maintenance", "1" if new else "0")
        self._maintenance_memory = new
        return new

    def _store_for_kv(self):
        store = self._store
        if store is not None and callable(getattr(store, "kv_get", None)) \
                and callable(getattr(store, "kv_set", None)):
            return store
        return None

    def _qr_file_id(self) -> Optional[str]:
        store = self._store
        if store is None:
            return None
        get = getattr(store, "kv_get", None)
        return get("pay_qr_file_id") if callable(get) else None

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
        # Persistent bottom-menu taps arrive as plain text ("🛒 Buy Number").
        # Route them straight to the same screen their inline-tap twin opens.
        # The reply keyboard reaches us lowercased, so match case-insensitively.
        if low in REPLY_MENU_LABELS_LOW:
            # Reuse the inline-tap dispatcher so a menu tap and an inline tap
            # can never diverge (same authorisation, same screen, same layout).
            self._wizard.pop(user_id, None)
            return self.button(user_id, REPLY_MENU_LABELS_LOW[low])
        if low in ("/admin", "/owner", "/panel", "⚙️ admin panel", "admin panel",
                   "⚙ owner panel", "owner panel"):
            self._wizard.pop(user_id, None)
            return self.admin_panel(user_id)
        if low in ("/start", "/help"):
            self._wizard.pop(user_id, None)
            return self.main_menu(user_id)
        # "My numbers" as a typed command or phrase must go to the SAME screen
        # as the 🧾 My numbers button, not fall through to search (which returns
        # the menu and looks like "not working").
        if low in ("/numbers", "/my", "/my numbers", "/mynumbers", "my numbers",
                   "mynumbers", "numbers", "/history", "/my-numbers"):
            self._wizard.pop(user_id, None)
            return self.history_card(user_id)
        flow = self.router._createbot_flow  # pending-token funnel lives there
        if flow is not None and flow.pending(user_id) and not body.startswith("/"):
            return self.router.handle(user_id, body)
        wizard = self._wizard.get(user_id)
        if wizard and not body.startswith("/"):
            return self._wizard_text(user_id, body)
        if body.startswith("/"):
            self._wizard.pop(user_id, None)
            return self.router.handle(user_id, body)
        if len(body) >= 2:
            # Plain text outside any wizard is a search: "zomato" should find
            # Zomato without digging through fifty pages.
            found = self.search(user_id, body)
            if found is not None:
                return found
        return self.main_menu(user_id)

    # -- browsing ---------------------------------------------------------
    _PAGE = 16  # services per shop page (8 rows of 2)

    def search(self, user_id: str, query: str) -> Optional[Reply]:
        """Substring match over name+slug; None when nothing matched."""
        q = query.strip().lower()
        matches = [
            s for s in self.catalog.services()
            if q in s.slug.lower() or q in s.name.lower()
        ]
        if not matches:
            return None
        matches = matches[:12]
        rows = [
            tuple(
                (f"{_emoji(s.slug)} {s.name} · {self.pricer.price(s).gross_price}",
                 f"s:{s.slug}")
                for s in matches[i:i + 2]
            )
            for i in range(0, len(matches), 2)
        ]
        rows.append((("🏠 Menu", "m"),))
        near = f" (showing first {len(matches)})" if len(matches) == 12 else ""
        return Reply(f"🔍 “{query}”{near}:", rows=tuple(rows))

    # -- the menu tree ---------------------------------------------------
    def main_menu(self, user_id: str) -> Reply:
        balance = self.router.balance_of(user_id)
        fav_slug = self.favourites(user_id)
        rows = [
            (("🛒 Buy a number", "l"),),
            ((f"💰 Balance: {balance}", "w"), ("🧾 My numbers", "o")),
            ((f"⭐ Favourites ({len(fav_slug)})", "fav"), ("❓ How it works", "h")),
            (("🆘 Support", "support"),),
        ]
        if self._can_createbot():
            rows.append((("🤖 Run your own bot", "cb"),))
        if self.router._is_owner(user_id):
            rows.append((("📊 Owner: profit & health", "a"),))
        count = len(self.catalog)
        banner = (
            "🛠 Maintenance in progress — buying is paused, everything else is open.\n\n"
            if self.maintenance_on() and not self.router._is_owner(user_id)
            else ""
        )
        return Reply(
            f"{banner}"
            "✳️ YCOTP Numbers\n\n"
            f"⚡ {count:,} services · 🇮🇳 real SIMs · 🛟 auto-refund if no OTP\n"
            f"💰 Your balance: {balance}\n\n"
            "Tap 🛒 Buy a number below, or just type the service name "
            "(e.g. `zomato`) to search.",
            rows=tuple(rows),
            persistent_menu=True,
        )

    def services_grid(
        self, user_id: str, category: Optional[str] = None, page: int = 0
    ) -> Reply:
        """Paginated tap-grid, price printed on each button.

        Every callback carries its full context (category + page), so a
        stale button can never double-charge: the price is re-quoted at
        confirm time, not trusted from an old screen. With a 1000+ service
        catalogue, pagination and search are the only honest navigation.
        """
        all_services = [
            s for s in self.catalog.services()
            if category is None or s.category == category
        ]
        if not all_services:
            return Reply(
                f"No services in {category or 'the catalogue'} right now.",
                ok=False,
                rows=((("🛒 Browse", "l"),),),
            )
        pages = max(1, (len(all_services) + self._PAGE - 1) // self._PAGE)
        page = max(0, min(page, pages - 1))
        window = all_services[page * self._PAGE: (page + 1) * self._PAGE]
        rows: list[tuple[tuple[str, str], ...]] = []
        for i in range(0, len(window), 2):
            rows.append(tuple(
                (f"{_emoji(s.slug)} {s.name} · {self.pricer.price(s).gross_price}",
                 f"s:{s.slug}")
                for s in window[i:i + 2]
            ))
        base = f"c:{category}" if category else "l"
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("◀️ Prev", f"{base}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "nop"))
        if page < pages - 1:
            nav.append(("Next ▶️", f"{base}:{page + 1}"))
        if pages > 1:
            rows.append(tuple(nav))
        if category is None and page == 0:
            cats = sorted({s.category for s in self.catalog.services()})
            for i in range(0, len(cats), 4):
                rows.append(tuple(
                    (f"{_CATEGORY_EMOJI.get(c, '📦')} {c}", f"c:{c}")
                    for c in cats[i:i + 4]
                ))
        rows.append((("🏠 Menu", "m"),))
        title = (
            f"🛒 All services ({len(all_services)})"
            if category is None
            else f"{_CATEGORY_EMOJI.get(category, '📦')} {category.title()} ({len(all_services)})"
        )
        return Reply(
            f"{title}\n\nPrice on each button is final "
            "— or just type a name (e.g. zomato) to search:",
            rows=tuple(rows),
        )

    def service_card(self, user_id: str, slug: str, page: int = 0) -> Reply:
        """Service screen = the server picker.

        The provider stocks every service from several SIM-bank servers --
        same app, different price/stock/success-quality. Customers see all
        of them (cheapest first, provider-recommended starred) and buy from
        the one they tapped; the charge is computed on THAT server's price.
        """
        if not self.catalog.has(slug):
            return Reply(
                "That service just left the catalogue. Here is the current list:",
                ok=False,
                rows=((("🛒 Browse", "l"),),),
            )
        cost = self.catalog.get(slug)
        balance = self.router.balance_of(user_id)
        options = self.catalog.servers_for(slug)

        serve = getattr(self.router.engine.provider, "can_serve", None)
        if callable(serve) and not serve(slug) and not self.router._is_owner(user_id):
            return Reply(
                f"😔 {cost.name} numbers are temporarily unavailable from our "
                "supplier — this one isn't listed as buyable, so you never pay "
                "for a number that can't arrive.",
                ok=False,
                rows=((("🛒 Browse services", "l"), ("🏠 Menu", "m")),),
            )

        advice = self.pricer.price(cost)

        if not options:
            # Flat catalogue (tests/dev): single buy button as before.
            short = advice.gross_price - balance
            warn = (
                f"\n\n⚠️ Your balance is {balance} — you'll need {short} more."
                if balance.paise < advice.gross_price.paise
                else f"\n\n💰 Your balance covers it ({balance})."
            )
            fav_label = "★ Remove from favourites" if self.is_favourite(user_id, slug) \
                else "⭐ Save to favourites"
            rows = (
                ((f"✅ Buy now · {advice.gross_price}", f"y:{slug}"),),
                ((fav_label, f"fvt:{slug}"),),
                (("◀️ Back to services", "l"), ("🏠 Menu", "m")),
            )
            return Reply(
                f"{_emoji(slug)} {cost.name}\n\n"
                f"Price: {advice.gross_price} (all-in, no hidden fees)\n"
                f"Country: 🇮🇳 India · Number yours for {_VALIDITY}\n"
                f"🛟 No OTP in ~5 min = automatic full refund.{warn}",
                rows=rows,
            )

        live = [o for o in options if o.stock > 0] or list(options)
        per = 8
        pages = max(1, (len(live) + per - 1) // per)
        page = max(0, min(page, pages - 1))
        window = live[page * per: (page + 1) * per]
        rows: list[tuple[tuple[str, str], ...]] = []
        for opt in window:
            opt_price = self.pricer.price(
                cost.with_overrides(list_price=opt.price)
            ).gross_price
            star = "⭐ " if opt.is_best else ""
            stock_txt = f"{opt.stock:,} left" if opt.stock else "restocking"
            rows.append((
                (f"{star}Server {opt.server_id} · {opt_price}", f"y:{slug}:{opt.server_id}"),
                ("📦 " + stock_txt, "nop"),
            ))
        if pages > 1:
            nav: list[tuple[str, str]] = []
            if page > 0:
                nav.append(("◀️ Prev", f"s:{slug}:{page - 1}"))
            nav.append((f"{page + 1}/{pages}", "nop"))
            if page < pages - 1:
                nav.append(("Next ▶️", f"s:{slug}:{page + 1}"))
            rows.append(tuple(nav))
        fav_label = "★ Remove from favourites" if self.is_favourite(user_id, slug) \
            else "⭐ Save to favourites"
        rows.append(((fav_label, f"fvt:{slug}"),))
        rows.append((("◀️ Back to services", "l"), ("🏠 Menu", "m")))

        cheapest = live[0]
        cheapest_price = self.pricer.price(
            cost.with_overrides(list_price=cheapest.price)
        ).gross_price
        short = cheapest_price - balance
        warn = (
            f"⚠️ Balance {balance} — top up {short} to buy."
            if balance.paise < cheapest_price.paise
            else f"💰 Your balance: {balance} ✅"
        )
        return Reply(
            f"{_emoji(slug)} {cost.name}\n"
            f"🇮🇳 India · {len(live)} server(s) live · from {cheapest_price}\n\n"
            "Pick a server — price shown is your final price:\n"
            "⭐ = provider's best-rated · 📦 = numbers in stock\n\n"
            f"🛟 No OTP in ~5 min = automatic full refund.\n{warn}",
            rows=tuple(rows),
        )

    def wallet_card(self, user_id: str) -> Reply:
        balance = self.router.balance_of(user_id)
        floor = self.catalog.cheapest_sticker()
        nudge = ""
        if floor is not None and balance.paise < floor.paise:
            short = Money(floor.paise - balance.paise)
            nudge = (
                f"\n\n⚠️ Your balance is below the cheapest service (from {floor}). "
                f"Add at least {short} to start buying.\n"
                "Top up via UPI — it is credited after a quick check."
            )
        return Reply(
            f"💰 Your balance: {balance}\n\n"
            "Every successful top-up is credited here and never expires.\n"
            "Refunds land here automatically." + nudge,
            rows=(
                (("➕ Add money", "t"), ("🧾 Transaction history", "tx")),
                (("🛒 Buy a number", "l"),),
                (("🏠 Menu", "m"),),
            ),
        )

    def wallet_history(self, user_id: str) -> Reply:
        """🧾 Transaction history: every top-up, purchase and refund for this
        customer, plus any LIVE purchased number, newest first. IST timestamps.
        """
        balance = self.router.balance_of(user_id)
        store = self._store
        lines = [f"🧾 *Transaction history*\n\n💰 Balance: {balance}\n"]
        rows: list[tuple[tuple[str, str], ...]] = []

        # LIVE purchased numbers are the most important "transaction" while they
        # are sitting in the user's pocket -- surface them at the top.
        live = self._active_numbers(user_id)
        if live:
            lines.append("📱 Active purchase:")
            for a in live:
                mins = max(1, int(round(a.seconds_left / 60)))
                try:
                    name = self.catalog.get(a.slug).name if self.catalog.has(a.slug) else a.slug
                except Exception:  # noqa: BLE001
                    name = a.slug
                lines.append(f"  {name} · {a.gross} · ~{mins} min left")
                lines.append(f"  📱 {a.phone}" + (f" · OTP {a.otp}" if a.has_otp else ""))
                mode = "💰 New OTP" if a.has_otp else "💰 Check OTP"
                rows.append(((f"{mode}", f"{'nx:' if a.has_otp else 'co:'}{a.token}"),
                             ("♻️ Cancel", f"cx:{a.token}")))
            lines.append("")

        # Top-ups (approved / pending / declined), newest first.
        entries: list[tuple[float, str, str]] = []
        get_tups = getattr(store, "user_topups", None)
        if callable(get_tups):
            try:
                for t in list(get_tups(user_id)):
                    badge = {"approved": "🟢 Credits", "pending": "⏳ Pending",
                             "declined": "🔴 Declined"}.get(t.status, "•")
                    entries.append((t.created_ts, f"{badge} {t.amount}",
                                    f"top-up #{t.id}"))
            except Exception:  # noqa: BLE001 - display only
                pass

        # Orders (purchases / refunds / cancels), newest first.
        recent = self._recent_orders(user_id)
        for o in recent:
            if hasattr(o, "success"):
                try:
                    name = self.catalog.get(o.slug).name if self.catalog.has(o.slug) else o.slug
                except Exception:  # noqa: BLE001
                    name = o.slug
                status = o.status or ("delivered" if o.success else "refunded")
                label = {"delivered": "🟢 Bought & delivered",
                         "refunded": "🔵 Refunded",
                         "cancelled": "🔵 Cancelled — refunded",
                         "failed": "⚪ Failed — refunded"}.get(status, "•")
                net = Money(o.gross.paise - o.refunded.paise)
                entries.append((o.ts, f"{label} {o.gross}", f"{name} · net {net}"))

        entries.sort(key=lambda e: -e[0])
        if entries:
            lines.append("📜 Everything:")
            for ts, amt, note in entries:
                lines.append(f"  {amt} · {note}")
                lines.append(f"      {format_ts(ts)}")
        else:
            lines.append("No transactions yet. Add money or buy a number to "
                         "see them here.")

        if live:
            rows.append((("🧾 My numbers", "o"),))
        rows.append((("➕ Add money", "t"), ("🛒 Buy a number", "l"), ("🏠 Menu", "m")))
        return Reply("\n".join(lines), rows=tuple(rows))

    # -- top-ups ----------------------------------------------------------
    # Flow: 💰 → ➕ Add money → pay by UPI → ✅ I've paid → amount →
    # screenshot → owner gets a ping + the screenshot with approve/decline.
    def topup_card(self, user_id: str) -> Reply:
        if self._store is None:
            return Reply(
                "Top-ups are not enabled on this deployment yet.\n"
                f"Message {self.support_contact or 'the bot owner'} to add money.",
                ok=False, rows=((("🏠 Menu", "m"),),),
            )
        payee = (
            f"UPI: `{self.pay_upi_id}`" if self.pay_upi_id
            else "UPI ID: not configured yet — ask the owner"
        )
        qr = self._qr_file_id()
        note = "" if qr else "\n\n(Owner hasn't uploaded a QR yet — pay to the UPI ID.)"
        return Reply(
            "➕ Add money\n\n"
            f"1️⃣ Pay any amount (min ₹10) to:\n   {payee}{note}\n\n"
            "2️⃣ Tap ✅ I've paid below and follow the steps.\n\n"
            "⚡ Credited after a quick owner verification (usually minutes).\n"
            "❌ Sending fake screenshots gets your account blocked.",
            rows=(
                (("✅ I've paid", "t:paid"),),
                (("◀️ Back", "w"), ("🏠 Menu", "m")),
            ),
            photo=qr,
        )

    def topup_amount_prompt(self, user_id: str) -> Reply:
        self._wizard[user_id] = {"flow": "topup", "step": "amount"}
        return Reply(
            "💳 How much did you pay?\n\n"
            "Reply with just the amount in ₹ — e.g. 200\n"
            "(minimum ₹10)",
            rows=((("✖️ Cancel", "w"),),),
        )

    def _admin_input_prompt(self, user_id: str, action: str) -> Reply:
        """Start the collect-input wizard for an owner action."""
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False, rows=((("🏠 Menu", "m"),),))
        prompt = {
            "credit": ("➕ ADD BALANCE TO A CUSTOMER",
                       "Send the user's Telegram id and the amount, e.g.\n"
                       "`123456789 250`"),
            "debit": ("↩️ DEDUCT FROM A CUSTOMER",
                      "Send the user's Telegram id and the amount, e.g.\n"
                      "`123456789 50`"),
            "ban": ("🚫 BAN / UNBAN A CUSTOMER",
                    "Send the user's Telegram id to toggle their access, e.g.\n"
                    "`123456789`"),
            "broadcast": ("📢 BROADCAST TO ALL CUSTOMERS",
                          "Send your announcement text, e.g.\n"
                          "`Big sale today on all numbers!`"),
            "support": ("🆘 EDIT SUPPORT USERNAME",
                        "Send the support username customers see on the 🆘 "
                        "Support screen, e.g.\n`@your_support`\n"
                        f"(current: `{self.support_contact or 'not set'}`)"),
        }[action]
        self._wizard[user_id] = {"flow": "admin", "action": action, "step": "input"}
        return Reply(f"{prompt[0]}\n\n{prompt[1]}\n\nTap ✖️ Cancel to abort.",
                     rows=((("✖️ Cancel", "a"),),))

    def _wizard_text(self, user_id: str, body: str) -> Reply:
        wizard = self._wizard.get(user_id) or {}
        flow = wizard.get("flow")
        if flow == "admin" and wizard.get("step") == "input":
            action = wizard.get("action", "")
            if action == "credit":
                args = self._split_uid_amount(body)
                if args is None:
                    return Reply("Format: <user id> <amount> — e.g. `123456789 250`.",
                                 ok=False, rows=((("✖️ Cancel", "a"),),))
                return self.router.handle(user_id, f"/credit {args}")
            if action == "debit":
                args = self._split_uid_amount(body)
                if args is None:
                    return Reply("Format: <user id> <amount> — e.g. `123456789 50`.",
                                 ok=False, rows=((("✖️ Cancel", "a"),),))
                return self.router.handle(user_id, f"/debit {args}")
            if action == "ban":
                uid = body.strip().split()[0] if body.strip() else ""
                if not uid:
                    return Reply("Please send just the user's Telegram id.",
                                 ok=False, rows=((("✖️ Cancel", "a"),),))
                return self.router.handle(user_id, f"/ban {uid}")
            if action == "broadcast":
                text = body.strip()
                if not text:
                    return Reply("Please send the announcement text.",
                                 ok=False, rows=((("✖️ Cancel", "a"),),))
                return self.router.handle(user_id, f"/broadcast {text}")
            if action == "support":
                val = (body.strip() or "").lstrip("@")
                if not val:
                    return Reply("Please send the support username, e.g. `@your_support`.",
                                 ok=False, rows=((("✖️ Cancel", "a"),),))
                self._set_support_contact(val)
                return Reply(
                    f"✅ Support username set to @{val}.\n\n"
                    f"It's now shown on the 🆘 Support screen.",
                    ok=True, rows=((("🆘 Preview Support", "support"),),
                                   (("📊 Admin Panel", "a"),)),
                )
            self._wizard.pop(user_id, None)
            return self.main_menu(user_id)

        if wizard.get("flow") != "topup" or wizard.get("step") != "amount":
            self._wizard.pop(user_id, None)
            return self.main_menu(user_id)
        amount = self._parse_amount(body)
        if amount is None:
            return Reply(
                "That doesn't look like an amount. Numbers only — e.g. 200 "
                "(₹10 minimum, ₹1,00,000 maximum).\n\nTry again or tap ✖️ Cancel.",
                ok=False, rows=((("✖️ Cancel", "w"),),),
            )
        self._wizard[user_id] = {"flow": "topup", "step": "screenshot", "amount": amount.paise}
        return Reply(
            f"Amount noted: {amount}\n\n"
            "📸 Now send the payment screenshot as a photo — "
            "it goes straight to the owner with approve/decline buttons.",
            rows=((("✖️ Cancel", "w"),),),
        )

    @staticmethod
    def _split_uid_amount(text: str) -> Optional[str]:
        """\"123456789 250\" -> the two args for /credit|/debit, else None."""
        tokens = (text or "").strip().split()
        if len(tokens) == 2 and tokens[0].strip().lstrip("-").isdigit():
            return f"{tokens[0].strip()} {tokens[1].strip()}"
        return None

    @staticmethod
    def _parse_amount(text: str) -> Optional[Money]:
        raw = text.lower().replace("₹", "").replace("rs.", "").replace("rs", "")
        raw = raw.replace("inr", "").replace(",", "").strip()
        raw = raw.rstrip(".").strip()
        digits = raw.split(".", 1)[0]
        paise = ""
        if not digits.isdigit():
            return None
        rupees = int(digits)
        if "." in raw:
            frac = raw.split(".", 1)[1]
            if not (frac.isdigit() and len(frac) <= 2):
                return None
            paise = frac.ljust(2, "0")
        if not (10 <= rupees <= 100_000):
            return None
        return Money(rupees * 100 + (int(paise) if paise else 0))

    def photo(self, user_id: str, file_id: str) -> Reply:
        """A photo message arrives: payment screenshot, QR, or confusion."""
        wizard = self._wizard.get(user_id) or {}
        flow, step = wizard.get("flow"), wizard.get("step")
        if flow == "topup" and step == "screenshot":
            store = self._store
            if store is None:
                self._wizard.pop(user_id, None)
                return Reply("Top-ups are offline right now. Try later.", ok=False)
            amount = Money(int(wizard["amount"]))
            topup_id = store.create_topup(user_id, amount, photo_file_id=file_id)
            self._wizard.pop(user_id, None)
            owner = self.router.owner_id
            return Reply(
                f"✅ Payment of {amount} submitted (no. {topup_id}).\n\n"
                "The owner is verifying it — your balance updates automatically "
                "when approved. You'll get a message here either way.",
                rows=((("💰 My balance", "w"), ("🏠 Menu", "m")),),
                notify=((owner,
                         f"💳 New top-up #{topup_id}: {amount} from user {user_id}.\n"
                         "Screenshot follows. Review: 📊 panel → 🧾 Top-ups."),)
                if owner else (),
                forward_photo=bool(owner),
            )
        if flow == "qr" and self.router._is_owner(user_id):
            if self._store is not None:
                self._store.kv_set("pay_qr_file_id", file_id)
            self._wizard.pop(user_id, None)
            return Reply(
                "🖼 Payment QR saved — customers now see it on the Add Money screen.",
                rows=((("📊 Owner panel", "a"),),),
            )
        return Reply(
            "I got a photo, but no payment is in progress.\n"
            "If that was a payment screenshot: 💰 Balance → ➕ Add money first, "
            "then send it when asked.",
            ok=False, rows=((("💰 Balance", "w"), ("🏠 Menu", "m")),),
        )

    def history_card(self, user_id: str) -> Reply:
        """🧾 My numbers.

        Inline-button driven. Every LIVE (not-yet-expired) number gets its own
        row of buttons: 💰 Check OTP, 🔁 Resend, ♻️ Cancel. Every completed order
        gets a 👁 Details button that opens a full receipt (date/time, status,
        reason, OTP, charged/refunded, balance after). No typing, no commands.
        """
        lines: list[str] = []
        rows: list[tuple[tuple[str, str], ...]] = []
        live = self._active_numbers(user_id)
        if live:
            lines.append("🔴 LIVE numbers (tap a number for its buttons):")
            for a in live:
                mins = max(1, int(round(a.seconds_left / 60)))
                try:
                    name = self.catalog.get(a.slug).name if self.catalog.has(a.slug) else a.slug
                except Exception:  # noqa: BLE001 - never blank the whole screen
                    name = a.slug
                if a.has_otp:
                    lines.append(f"\n✅ {name} · {a.gross} · {mins} min left")
                    lines.append(f"    📱 {a.phone} · OTP `{a.otp}`")
                else:
                    lines.append(f"\n⏳ {name} · {a.gross} · {mins} min left")
                    lines.append(f"    📱 {a.phone}")
                btns = [[("💰 Check OTP", f"co:{a.token}")]
                        if not a.has_otp else [("💰 New OTP", f"nx:{a.token}")]
                        ]
                btns[0].append(("🔁 Resend", f"rs:{a.token}"))
                btns[0].append(("♻️ Cancel", f"cx:{a.token}"))
                rows.append(tuple(btns[0]))
            rows.append(((("📜 Completed history", "h:all"),),))
        else:
            lines.append("No live numbers right now.")

        entries = self._recent_orders(user_id)
        if entries:
            lines.append("\n📜 Completed:")
            for i, o in enumerate(entries[:10]):
                if hasattr(o, "success"):  # persisted OrderRow
                    when = self._iso(o.ts)
                    try:
                        name = self.catalog.get(o.slug).name if self.catalog.has(o.slug) else o.slug
                    except Exception:  # noqa: BLE001
                        name = o.slug
                    badge = "✅ delivered" if o.success else "♻️ refunded"
                    lines.append(f"\n{badge} · {name} · {o.gross} · {when}")
                    rows.append(((f"👁 {name} details", f"h:{o.id}"),))
                else:  # session-dict fallback (no wallet store)
                    ts, slug, ok, summary = o
                    when = self._iso(ts)
                    name = self.catalog.get(slug).name if self.catalog.has(slug) else slug
                    badge = "✅ delivered" if ok else "♻️ refunded"
                    lines.append(f"\n{badge} · {name} · {when}")
                    lines.append(f"    {summary}")
        elif not live:
            lines.append("\nNo orders yet. Buy a number and it shows up here.")

        rows.append((("🛒 Buy again", "l"), ("🏠 Menu", "m")))
        return Reply("\n".join(lines), rows=tuple(rows))

    def history_detail(self, user_id: str, order_id_s: str) -> Reply:
        """Full receipt for one past order (👁 Details)."""
        store = self._store
        get_one = getattr(store, "get_order", None)
        if not callable(get_one):
            return Reply("History detail is offline on this deployment.", ok=False,
                         rows=((("🧾 My numbers", "o"),),))
        try:
            oid = int(order_id_s)
        except ValueError:
            return self._badtap()
        o = get_one(oid, user_id=user_id)
        if o is None:
            return Reply("That order isn't visible to this account.", ok=False,
                         rows=((("🧾 My numbers", "o"),),))
        name = self.catalog.get(o.slug).name if self.catalog.has(o.slug) else o.slug
        status = o.status or ("delivered" if o.success else "refunded")
        when = self._iso(o.ts)
        lines = [
            f"🧾 {name} — receipt #{o.id}",
            f"\n🕒 {when}",
            f"\n📊 Status: {status}",
            f"\n📱 Number: {o.phone or '—'}",
        ]
        if o.otp:
            lines.append(f"🔑 OTP: `{o.otp}`")
        lines.append(f"\n💵 Charged: {o.gross}")
        if o.refunded.paise > 0:
            lines.append(f"↩️ Refunded: {o.refunded}")
            lines.append(f"     net paid: {o.gross - o.refunded}")
        if o.spent.paise > 0:
            lines.append(f"🧾 Provider cost: {o.spent}")
        lines.append(f"💰 Balance after: {o.balance_after}")
        if o.reason:
            lines.append(f"\nℹ️ {o.reason}")
        return Reply("\n".join(lines),
                     rows=((("🛒 Buy again", "l"), ("🧾 My numbers", "o")),
                           (("🏠 Menu", "m"),)))

    def _recent_orders(self, user_id: str):
        store = self._store
        get_orders = getattr(store, "recent_orders", None)
        if callable(get_orders):
            try:
                return list(get_orders(user_id=user_id, limit=12))
            except Exception:  # noqa: BLE001
                return []
        return self._history.get(user_id, [])

    @staticmethod
    def _iso(ts: float) -> str:
        """Render a timestamp as IST (Asia/Kolkata), the bot's timezone."""
        return format_ts(ts)

    def _active_numbers(self, user_id: str) -> list:
        store = self._store
        fn = getattr(store, "active_numbers", None)
        if not callable(fn):
            return []
        try:
            return list(fn(user_id=user_id))
        except Exception:  # noqa: BLE001 - display only
            return []

    def admin_panel(self, user_id: str) -> Reply:
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False, buttons=(("🏠 Menu", "m"),))
        status = self.router.engine.status()
        pnl = status["pnl"]
        pending = self._pending_topups()
        qr_state = "set ✅" if self._qr_file_id() else "not set ❌"
        fs = self._float_stats()
        float_line = ""
        if fs:
            float_line = f"\n👥 Customers: {fs['users']} · float held: {fs['float']}"
        return Reply(
            "📊 Owner panel\n\n"
            f"🏦 Provider wallet: {status['provider_wallet']}\n"
            f"📒 Ledger wallet:    {status['ledger_wallet']}\n"
            f"💹 Revenue: {pnl['revenue']} · Costs: {pnl['cogs']}\n"
            f"📈 Net profit: {pnl['net_profit']}"
            f"{float_line}\n"
            f"💳 Payments waiting: {len(pending)}\n"
            f"🖼 Payment QR: {qr_state}\n"
            f"🛠 Maintenance: {'🟢 ON — buying paused for customers' if self.maintenance_on() else '⚪ off'}\n\n"
            "Full P&L: /report · Health: /status",
            rows=(
                ((f"🧾 Top-ups ({len(pending)} pending)", "a:t"),),
                (("📦 Orders & per-order profit", "a:o"),),
                (("💳 Add balance", "ax:credit"), ("↩️ Deduct", "ax:debit")),
                (("👥 Customers", "ax:users"), ("🚫 Ban/Unban", "ax:ban")),
                (("📢 Broadcast", "ax:broadcast"), ("📊 Metrics", "ax:metrics")),
                (("🛠 Toggle maintenance", "a:mm"), ("🖼 Payment QR", "a:qr")),
                (("🕳 Sunk cost", "ax:sunkcost"), ("🏦 Provider", "ax:provider")),
                (("🆘 Support username", "ax:support"),),
                (("🏠 Menu", "m"),),
            ),
        )

    def _float_stats(self):
        store = self._store
        fn = getattr(store, "float_stats", None)
        return fn() if callable(fn) else None

    def orders_screen(self, user_id: str) -> Reply:
        """Owner's per-order P&L: every sale with the profit it made."""
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False, rows=((("🏠 Menu", "m"),),))
        store = self._store
        fn = getattr(store, "recent_orders", None)
        orders = fn(limit=10) if callable(fn) else []
        if not orders:
            return Reply("📦 No orders yet.", rows=((("◀️ Owner panel", "a"),),))
        lines = ["📦 Recent orders (newest first):"]
        tot_g = tot_p = 0
        n = 0
        for o in orders:
            when = format_ts(o.ts, sep=" · ", time_fmt="%H:%M")
            if o.success:
                ratio = o.profit_ratio
                pct = f" ({ratio:.0%})" if ratio is not None else ""
                lines.append(f"\n✅ {when} · {o.slug} · {o.gross} → profit {o.profit}{pct}")
                tot_g += o.gross.paise; tot_p += o.profit.paise; n += 1
            else:
                lines.append(f"\n♻️ {when} · {o.slug} · refunded")
        if n:
            lines.append(f"\n—— shown sales margin: {tot_p / tot_g:.0%}")
        return Reply("\n".join(lines), rows=((("◀️ Owner panel", "a"),),))

    def _pending_topups(self) -> list:
        store = self._store
        if store is None or not callable(getattr(store, "pending_topups", None)):
            return []
        return list(store.pending_topups())

    def topups_screen(self, user_id: str) -> Reply:
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False, rows=((("🏠 Menu", "m"),),))
        pending = self._pending_topups()[:10]
        if not pending:
            return Reply(
                "💳 Payments\n\n✨ Nothing waiting. New top-ups ping you here "
                "automatically (with the customer's screenshot).",
                rows=((("◀️ Owner panel", "a"),),),
            )
        lines = ["💳 Waiting for your review (newest first):"]
        rows: list[tuple[tuple[str, str], ...]] = []
        for t in pending:
            when = format_ts(t.created_ts, sep=" · ", time_fmt="%H:%M")
            lines.append(f"\n#{t.id} · {t.amount} · user `{t.user_id}` · {when}")
            rows.append((
                (f"✅ Credit {t.amount} (#{t.id})", f"ap:{t.id}"),
                (f"❌ Decline (#{t.id})", f"ad:{t.id}"),
            ))
        rows.append((("🔄 Refresh", "a:t"), ("◀️ Owner panel", "a")))
        return Reply("\n".join(lines), rows=tuple(rows))

    def decide_topup(self, user_id: str, topup_id_s: str, approve: bool) -> Reply:
        """Owner tapped approve/decline on a payment."""
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False)
        store = self._store
        if store is None:
            return Reply("Payments are offline on this deployment.", ok=False)
        try:
            topup_id = int(topup_id_s)
        except ValueError:
            return Reply("Bad payment id.", ok=False)
        topup = store.get_topup(topup_id)
        if topup is None:
            return Reply(f"No payment #{topup_id} here.", ok=False)
        if topup.status != "pending":
            return Reply(f"#{topup_id} was already {topup.status}.", ok=False,
                         rows=((("🧾 Top-ups", "a:t"),),))
        if not store.decide_topup(topup_id, "approved" if approve else "declined",
                                  decided_by=user_id):
            return Reply(f"#{topup_id} was already processed.", ok=False)
        if approve:
            self.router.credit(topup.user_id, topup.amount)
            fresh = self.topups_screen(user_id)
            return Reply(
                f"✅ #{topup_id} approved — {topup.amount} credited to "
                f"user `{topup.user_id}`.\n\n{fresh.text}",
                rows=fresh.rows,
                notify=((topup.user_id,
                         f"🎉 Payment approved! {topup.amount} added to your "
                         f"balance ({self.router.balance_of(topup.user_id)} now). "
                         "Tap /start and buy away 🛒"),),
            )
        fresh = self.topups_screen(user_id)
        return Reply(
            f"❌ #{topup_id} declined — no money moved.\n\n{fresh.text}",
            rows=fresh.rows,
            notify=((topup.user_id,
                     f"⚠️ Your payment of {topup.amount} (no. {topup_id}) could not "
                     "be verified, so no balance was added. Paid by mistake? "
                     "Contact the owner with the correct proof."),),
        )

    def qr_screen(self, user_id: str) -> Reply:
        if not self.router._is_owner(user_id):
            return Reply("Owner only.", ok=False, rows=((("🏠 Menu", "m"),),))
        state = "a QR is live ✅" if self._qr_file_id() else "no QR uploaded yet ❌"
        self._wizard[user_id] = {"flow": "qr", "step": "photo"}
        return Reply(
            f"🖼 Payment QR — {state}\n\n"
            "Send the QR image to THIS chat as a photo and it becomes what "
            "customers see on ➕ Add money. (Your UPI ID text comes from the "
            "PAY_UPI_ID setting.)",
            rows=((("✖️ Cancel", "a"),),),
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
        # Navigating away silently cancels any half-done top-up wizard; the
        # payment itself only exists once the screenshot lands, so nothing is lost.
        if kind not in {"t", "ap", "ad"} and user_id in self._wizard:
            self._wizard.pop(user_id, None)
        if kind == "m":
            return self.main_menu(user_id)
        if kind == "t":
            if len(parts) == 2 and parts[1] == "paid":
                return self.topup_amount_prompt(user_id)
            return self.topup_card(user_id) if len(parts) == 1 else self._badtap()
        if kind == "ap" and len(parts) == 2:
            return self.decide_topup(user_id, parts[1], approve=True)
        if kind == "ad" and len(parts) == 2:
            return self.decide_topup(user_id, parts[1], approve=False)
        if kind == "a" and len(parts) == 2:
            if parts[1] == "t":
                return self.topups_screen(user_id)
            if parts[1] == "qr":
                return self.qr_screen(user_id)
            if parts[1] == "o":
                return self.orders_screen(user_id)
            if parts[1] == "mm":
                if not self.router._is_owner(user_id):
                    return Reply("Owner only.", ok=False, rows=((("🏠 Menu", "m"),),))
                on = self._toggle_maintenance()
                text = (
                    "🛠 Maintenance ON — customers see the paused banner and "
                    "can't buy. You (owner) still can, to test."
                    if on else
                    "✅ Maintenance OFF — buying is live again."
                )
                return Reply(text, rows=((("◀️ Owner panel", "a"),),))
            return self._badtap()
        if kind == "ax":
            # Admin-panel action button. Display-only actions run the owner
            # command and render its reply; input actions (credit/debit/ban/
            # broadcast) start a short prompt that collects the details on the
            # next message. Owner-gated; blocked for anyone else.
            if not self.router._is_owner(user_id) or len(parts) != 2:
                return Reply("Owner only.", ok=False, rows=((("🏠 Menu", "m"),),))
            action = parts[1]
            if action in {"users", "orders", "metrics", "provider", "sunkcost"}:
                # Command replies render fine but carry no navigation; wrap
                # them so the owner never gets stuck (Fix: back button everywhere).
                return self._with_back(self.router.handle(user_id, f"/{action}"))
            if action in {"credit", "debit", "ban", "broadcast"}:
                return self._admin_input_prompt(user_id, action)
            if action == "support":
                # ✏️ Edit the support username customers see on 🆘 Support.
                return self._admin_input_prompt(user_id, "support")
            return self._badtap()
        if kind == "fav":
            return self.favourites_card(user_id)
        if kind == "support":
            return self.support_card(user_id)
        if kind == "fvt" and len(parts) == 2:
            # Star/unstar a service; return to its screen.
            fav = self.toggle_favourite(user_id, parts[1])
            name = self.catalog.get(parts[1]).name if self.catalog.has(parts[1]) else parts[1]
            tiny = "added to" if fav else "removed from"
            return self._with_back(
                Reply(f"⭐ {name} {tiny} your favourites.", rows=((("⭐ Favourites", "fav"),),)),
                back_data="fav", back_label="⭐ Favourites",
            )
        if kind == "l":
            return self.services_grid(user_id, page=_int(parts[1], 0) if len(parts) == 2 else 0)
        if kind == "c":
            cat_page = _int(parts[2], 0) if len(parts) == 3 else 0
            return self.services_grid(user_id, parts[1], cat_page) if len(parts) >= 2 else self._badtap()
        if kind == "s" and len(parts) >= 2:
            return self.service_card(user_id, parts[1],
                                     _int(parts[2], 0) if len(parts) == 3 else 0)
        if kind == "w":
            return self.wallet_card(user_id)
        if kind == "tx":
            return self.wallet_history(user_id)
        if kind == "o":
            return self.history_card(user_id)
        if kind == "h" and len(parts) == 2:
            if parts[1] == "all":
                # "📜 Completed history" on the My-numbers screen -> the full
                # transaction history (top-ups + purchases + refunds + active).
                return self.wallet_history(user_id)
            return self.history_detail(user_id, parts[1])
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
        if kind == "y" and len(parts) >= 2:
            server = parts[2] if len(parts) == 3 else ""
            return self._begin_purchase(user_id, parts[1], server)
        if kind == "ry" and len(parts) >= 2:
            # 🔄 Retry on a refunded no-stock / operator failure. Bypasses the
            # per-user buy throttle (the customer's first attempt already spent
            # a slot and was refunded) and re-runs the same money path.
            server = parts[2] if len(parts) == 3 else ""
            return self._begin_purchase(user_id, parts[1], server, retry=True)
        if kind in ("co", "rs", "cx", "nx") and len(parts) == 2:
            token = parts[1]
            owner = self.router.active_owner(token)
            if owner is None or owner != user_id:
                return Reply("That's not your order.", ok=False,
                             rows=((("🏠 Menu", "m"),),))
            if kind == "co":
                return self._check_waiting(user_id, token)
            if kind == "nx":
                # 💰 New OTP on an already-delivered number (multi-OTP window).
                return self.router.poll_new_otp(token)
            if kind == "rs":
                return self.router.resend_sms(token)
            reply = self.router.cancel_wait(token)
            self._record(user_id, self._slug_for_token(token), reply)
            return reply
        # Unknown: menus advance; they never rotate an old menu back.
        return Reply("That menu moved on. Fresh one:", ok=False,
                     rows=((("🏠 Menu", "m"),),))

    # -- purchase --------------------------------------------------------
    def _begin_purchase(self, user_id: str, slug: str, server: str = "",
                        retry: bool = False) -> Reply:
        """Answer the ✅ Buy tap instantly; the real work runs deferred.

        ``deferred`` is the transport's contract: edit the placeholder,
        run this callable off the event loop, edit the message again with
        its Reply. Spending happens inside ``router.purchase`` -- the one
        and only money path -- never re-implemented here.

        ``retry=True`` is the 🔄 Retry press on a refunded no-stock/operator
        failure; it re-runs the same flow without the per-user buy throttle.
        """
        if not self.catalog.has(slug):
            return Reply("That service just left the catalogue. Refreshed list:",
                         ok=False, rows=((("🛒 Browse", "l"),),))
        if self.maintenance_on() and not self.router._is_owner(user_id):
            # Gate the tap itself: no placeholder, no debit, no confusion.
            return Reply(
                "🛠 Maintenance in progress — purchases are paused for a few "
                "minutes. Nothing was charged; please try again shortly.",
                ok=False,
                rows=((("🏠 Menu", "m"),),),
            )
        opt = self.catalog.server_option(slug, server) if server else None
        if server and opt is None:
            return Reply("That server just sold out. Refreshed list:",
                         ok=False, rows=((("◀️ Back", f"s:{slug}"),),))
        cost = self.catalog.get(slug)
        price = self.pricer.price(
            cost.with_overrides(list_price=opt.price) if opt else cost
        ).gross_price
        where = f" · server {server}" if server else ""

        def job(uid: str) -> Reply:
            reply = self.router.alloc_and_wait(uid, slug, server=server, retry=retry)
            self._record(uid, slug, reply)
            # Number-first: alloc_and_wait already returns the number (or a
            # clean refund). For the success case we keep its Check OTP buttons;
            # for a failure we wrap it like the old outcome so tests stay valid.
            if reply.ok:
                return reply
            return self._outcome(slug, reply, server)

        return Reply(
            f"⏳ Reserving a {cost.name} number for {price}…\n\n"
            "You'll get your number in a moment, then tap 💰 Check OTP once "
            "you've requested the code. Auto-refund if nothing arrives.",
            deferred=job,
        )

    def _check_waiting(self, user_id: str, token: str) -> Reply:
        """💰 Check OTP tap: wait (off the event loop) for the code to land.

        The transport runs ``deferred`` in a worker thread, so a bounded wait
        is fine. We wait the remaining OTP window on this tap; if it comes, we
        deliver it; if not, we tell the customer to try again shortly.
        """
        def job(uid: str) -> Reply:
            reply = self.router.check_otp(token)
            self._record(uid, self._slug_for_token(token), reply)
            return reply

        return Reply(
            "⏳ Waiting for the OTP…\n\n"
            "If you haven't already, enter your number on the service to "
            "request the code. This waits a while, so do not tap repeatedly.",
            deferred=job,
        )

    def _slug_for_token(self, token: str) -> str:
        entry = getattr(self.router, "_awaiting", {}).get(token)
        return entry[1] if entry else ""

    def _record(self, user_id: str, slug: str, reply: Reply) -> None:
        first = reply.text.splitlines()[0] if reply.text else "(no detail)"
        entries = self._history.setdefault(user_id, [])
        entries.insert(0, (self._now(), slug, reply.ok, first[:120]))
        del entries[self._history_size:]

    def _outcome(self, slug: str, reply: Reply, server: str = "") -> Reply:
        # A refunded no-stock / operator failure gets a one-tap 🔄 Retry that
        # re-runs the same money path without the per-user buy throttle.
        if CommandRouter._retryable(reply.text):
            rows = (
                (("🔄 Retry", f"ry:{slug}:{server}"), ("🧾 My numbers", "o")),
                (("🏠 Menu", "m"),),
            )
        else:
            rows = (
                ((f"🔁 Another {_emoji(slug)}", f"s:{slug}"), ("🧾 My numbers", "o")),
                (("🏠 Menu", "m"),),
            )
        return replace(reply, rows=rows)

    def _can_createbot(self) -> bool:
        return self.router.subbots is not None

    @staticmethod
    def _with_back(reply: Reply, *, back_data: str = "a",
                   back_label: str = "◀️ Owner panel") -> Reply:
        """Guarantee a Back button on a screen that came from the owner panel.

        Command replies (``/metrics``, ``/orders``, ...) can be long and useful
        but carry no navigation, so an owner tapping one from the panel could
        get stuck without typing. Appends a Back row unless one already exists.
        """
        rows = getattr(reply, "rows", ()) or ()
        if rows:
            for row in rows:
                if any(data == back_data for _label, data in row):
                    return reply  # already navigable
            return replace(reply, rows=rows + ((back_label, back_data),))
        return replace(reply, buttons=((back_label, back_data),))

    @staticmethod
    def _badtap() -> Reply:
        return Reply("That button went stale. Fresh menu:", ok=False,
                     rows=((("🏠 Menu", "m"),),))
