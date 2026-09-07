"""📣 Social boost screens. Callbacks are ``sm:*`` (never OTP prefixes).

The persistent keyboard is unchanged; this shop is inline-only and hidden
unless ``router.smm_shop`` is set (i.e. ``SMM_API_KEY`` is present).
"""

from __future__ import annotations

from decimal import Decimal

from ..money import Money
from ..smm.catalog import platform_label
from ..smm.provider import OPEN_STATUSES, SmmError, SmmUserError
from .commands import Reply

__all__ = ["handle", "handle_text"]

_PAGE = 8  # 4 rows of 2
_STATUS = {
    "pending": "⏳ Pending",
    "in_progress": "🔄 In progress",
    "completed": "✅ Delivered",
    "partial": "📦 Partial",
    "canceled": "♻️ Cancelled",
    "refunded": "↩️ Refunded",
    "failed": "⚠️ Failed",
}


def _shop(ui):
    return getattr(ui.router, "smm_shop", None)


def _extra(ui) -> Decimal:
    return Decimal(getattr(ui.router, "reseller_rate", 0) or 0)


def _clone_owner(ui) -> str:
    if getattr(ui.router, "is_clone", False):
        return str(getattr(ui.router, "owner_id", "") or "")
    return ""


def _trunc(text: str, n: int = 28) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _int(text: str, default: int = 0) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def handle(ui, user_id: str, parts: list[str]) -> Reply:
    """Dispatch ``sm`` / ``sm:…`` taps. ``parts[0]`` is always ``sm``."""
    shop = _shop(ui)
    if shop is None:
        return Reply(
            "Social boost isn't on this bot.",
            ok=False, rows=((("🏠 Menu", "m"),),),
        )
    kind = parts[1] if len(parts) > 1 else "home"
    enabled = True
    fn = getattr(ui, "smm_enabled", None)
    if callable(fn):
        enabled = bool(fn())
    # Existing receipts still open when the shop is off; new buys do not.
    if not enabled and kind not in {"o", "od", "rf", "cx"}:
        return Reply(
            "📣 Social boost is switched off by the owner.",
            ok=False, rows=((("🏠 Menu", "m"),),),
        )
    try:
        shop.catalog.ensure()
    except SmmError:
        return Reply(
            "📣 Social boost is warming up. Try again in a moment.",
            ok=False, rows=((("🏠 Menu", "m"),),),
        )
    if kind in {"home", ""}:
        return home(ui, user_id)
    if kind == "p":
        return home(ui, user_id, page=_int(parts[2] if len(parts) > 2 else "0"))
    if kind == "pl" and len(parts) >= 3:
        return categories(ui, parts[2], page=_int(parts[3] if len(parts) > 3 else "0"))
    if kind == "c" and len(parts) >= 3:
        return services(ui, parts[2], page=_int(parts[3] if len(parts) > 3 else "0"))
    if kind == "s" and len(parts) >= 3:
        return service_card(ui, user_id, parts[2])
    if kind == "cq" and len(parts) >= 3:
        return ask_qty(ui, user_id, parts[2])
    if kind == "q" and len(parts) >= 4:
        return ask_link(ui, user_id, parts[2], _int(parts[3]))
    if kind == "go" and len(parts) >= 4:
        return confirm(ui, user_id, parts[2], _int(parts[3]))
    if kind == "buy" and len(parts) >= 4:
        return begin_buy(ui, user_id, parts[2], _int(parts[3]))
    if kind == "o":
        return my_boosts(ui, user_id)
    if kind == "od" and len(parts) >= 3:
        return order_detail(ui, user_id, parts[2])
    if kind == "rf" and len(parts) >= 3:
        return do_refill(ui, user_id, parts[2])
    if kind == "cx" and len(parts) >= 3:
        return do_cancel(ui, user_id, parts[2])
    return home(ui, user_id)


def handle_text(ui, user_id: str, body: str) -> Reply:
    fn = getattr(ui, "smm_enabled", None)
    if callable(fn) and not fn():
        ui._wizard.pop(user_id, None)
        return Reply(
            "📣 Social boost is switched off by the owner.",
            ok=False, rows=((("🏠 Menu", "m"),),),
        )
    wizard = (ui._wizard or {}).get(user_id) or {}
    if wizard.get("flow") != "smm":
        return home(ui, user_id)
    sid = str(wizard.get("sid") or "")
    if wizard.get("step") == "qty":
        raw = body.replace(",", "").replace(" ", "").strip()
        qty = _int(raw, 0)
        svc = _shop(ui).catalog.get(sid) if sid else None
        if svc is None or qty < svc.min_qty or qty > svc.max_qty:
            return Reply(
                f"Quantity must be between {getattr(svc, 'min_qty', 1):,} and "
                f"{getattr(svc, 'max_qty', 1):,}. Try again or tap ✖️.",
                ok=False,
                rows=((("✖️ Cancel", "sm"),),),
            )
        return ask_link(ui, user_id, sid, qty)
    if wizard.get("step") == "link":
        link = (body or "").strip()
        if len(link) < 3:
            return Reply(
                "That doesn't look like a public link. Paste the profile or post URL.",
                ok=False,
                rows=((("✖️ Cancel", "sm"),),),
            )
        qty = _int(str(wizard.get("qty") or "0"))
        wizard["link"] = link
        ui._wizard[user_id] = wizard
        return confirm(ui, user_id, sid, qty)
    ui._wizard.pop(user_id, None)
    return home(ui, user_id)


def home(ui, user_id: str, page: int = 0) -> Reply:
    shop = _shop(ui)
    plats = shop.catalog.platforms()
    if not plats:
        return Reply(
            "📣 Social boost\n\nCatalogue is empty right now. Try again shortly.",
            rows=((("🏠 Menu", "m"),),),
        )
    pages = max(1, (len(plats) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    window = plats[page * _PAGE: (page + 1) * _PAGE]
    rows: list[tuple[tuple[str, str], ...]] = []
    for i in range(0, len(window), 2):
        chunk = window[i:i + 2]
        rows.append(tuple(
            (f"{label} ({n})", f"sm:pl:{slug}:0") for slug, label, n in chunk
        ))
    if pages > 1:
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("◀️ Prev", f"sm:p:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "nop"))
        if page < pages - 1:
            nav.append(("Next ▶️", f"sm:p:{page + 1}"))
        rows.append(tuple(nav))
    rows.append((("🧾 My boosts", "sm:o"), ("🏠 Menu", "m")))
    return Reply(
        "📣 Social boost\n\n"
        "Same wallet as numbers. Pick a platform — we'll ask for a public "
        "link and a quantity. Delivery is not instant (minutes to a day). "
        "Partial or cancelled orders refund the undelivered share.",
        rows=tuple(rows),
    )


def categories(ui, platform: str, page: int = 0) -> Reply:
    shop = _shop(ui)
    cats = shop.catalog.categories(platform)
    if not cats:
        return Reply(
            "Nothing in that platform right now.",
            ok=False, rows=((("◀️ Platforms", "sm"),),),
        )
    pages = max(1, (len(cats) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    window = cats[page * _PAGE: (page + 1) * _PAGE]
    rows: list[tuple[tuple[str, str], ...]] = []
    for i in range(0, len(window), 2):
        chunk = window[i:i + 2]
        rows.append(tuple(
            (_trunc(name, 22) + f" ({n})", f"sm:c:{cid}:0")
            for cid, name, n in chunk
        ))
    if pages > 1:
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("◀️ Prev", f"sm:pl:{platform}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "nop"))
        if page < pages - 1:
            nav.append(("Next ▶️", f"sm:pl:{platform}:{page + 1}"))
        rows.append(tuple(nav))
    rows.append((("◀️ Platforms", "sm"), ("🏠 Menu", "m")))
    return Reply(
        f"{platform_label(platform)}\n\nPick a category:",
        rows=tuple(rows),
    )


def services(ui, cat_key: str, page: int = 0) -> Reply:
    shop = _shop(ui)
    svcs = shop.catalog.services_in(cat_key)
    if not svcs:
        return Reply(
            "That category just emptied.",
            ok=False, rows=((("◀️ Platforms", "sm"),),),
        )
    pages = max(1, (len(svcs) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    window = svcs[page * _PAGE: (page + 1) * _PAGE]
    extra = _extra(ui)
    rows: list[tuple[tuple[str, str], ...]] = []
    for i in range(0, len(window), 2):
        pair = []
        for svc in window[i:i + 2]:
            try:
                price = shop.catalog.quote(svc, svc.min_qty, extra_rate=extra)
            except SmmError:
                price = Money(0)
            pair.append((
                f"{_trunc(svc.name, 18)} · from {price}",
                f"sm:s:{svc.service_id}",
            ))
        rows.append(tuple(pair))
    plat = shop.catalog.cat_platform(cat_key)
    if pages > 1:
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("◀️ Prev", f"sm:c:{cat_key}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "nop"))
        if page < pages - 1:
            nav.append(("Next ▶️", f"sm:c:{cat_key}:{page + 1}"))
        rows.append(tuple(nav))
    rows.append(((f"◀️ {platform_label(plat)}", f"sm:pl:{plat}:0"), ("🏠 Menu", "m")))
    title = shop.catalog.cat_name(cat_key) or "Services"
    return Reply(
        f"{_trunc(title, 40)}\n\nPrice is for the minimum quantity. "
        "Re-quoted when you confirm.",
        rows=tuple(rows),
    )


def _qty_choices(svc) -> list[int]:
    out = [svc.min_qty]
    for q in (100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000):
        if svc.min_qty < q < svc.max_qty:
            out.append(q)
    if svc.max_qty not in out:
        out.append(svc.max_qty)
    # Keep callbacks short; 8 presets + custom on the card.
    return out[:8]


def service_card(ui, user_id: str, sid: str) -> Reply:
    shop = _shop(ui)
    svc = shop.catalog.get(sid)
    if svc is None:
        return Reply(
            "That service just left the catalogue.",
            ok=False, rows=((("◀️ Platforms", "sm"),),),
        )
    extra = _extra(ui)
    try:
        min_price = shop.catalog.quote(svc, svc.min_qty, extra_rate=extra)
    except SmmError:
        min_price = Money(0)
    flags = []
    if svc.refill:
        flags.append("refill")
    if svc.cancel:
        flags.append("cancel")
    flag_s = f" · {' / '.join(flags)}" if flags else ""
    rows: list[tuple[tuple[str, str], ...]] = []
    choices = _qty_choices(svc)
    pair: list[tuple[str, str]] = []
    for q in choices:
        try:
            price = shop.catalog.quote(svc, q, extra_rate=extra)
            label = f"{q:,} · {price}"
        except SmmError:
            label = f"{q:,}"
        pair.append((label, f"sm:q:{sid}:{q}"))
        if len(pair) == 2:
            rows.append(tuple(pair))
            pair = []
    if pair:
        rows.append(tuple(pair))
    rows.append((("✏️ Custom qty", f"sm:cq:{sid}"),))
    rows.append((
        (f"◀️ { _trunc(svc.category, 18) }", f"sm:c:{svc.cat_key}:0"),
        ("🏠 Menu", "m"),
    ))
    bal = ui.router.balance_of(user_id)
    return Reply(
        f"📣 {svc.name}\n"
        f"{svc.category}{flag_s}\n\n"
        f"Qty {svc.min_qty:,}–{svc.max_qty:,} · from {min_price}\n"
        f"💰 Your balance: {bal}\n\n"
        "Pick a quantity. Next we'll ask for the public link.",
        rows=tuple(rows),
    )


def ask_qty(ui, user_id: str, sid: str) -> Reply:
    shop = _shop(ui)
    svc = shop.catalog.get(sid)
    if svc is None:
        return home(ui, user_id)
    ui._wizard[user_id] = {"flow": "smm", "step": "qty", "sid": sid}
    return Reply(
        f"✏️ Quantity for {svc.name}\n\n"
        f"Send a number between {svc.min_qty:,} and {svc.max_qty:,}.",
        rows=((("✖️ Cancel", f"sm:s:{sid}"),),),
    )


def ask_link(ui, user_id: str, sid: str, qty: int) -> Reply:
    shop = _shop(ui)
    svc = shop.catalog.get(sid)
    if svc is None:
        return home(ui, user_id)
    if qty < svc.min_qty or qty > svc.max_qty:
        return service_card(ui, user_id, sid)
    ui._wizard[user_id] = {"flow": "smm", "step": "link", "sid": sid, "qty": qty}
    try:
        price = shop.catalog.quote(svc, qty, extra_rate=_extra(ui))
        priced = f"This qty costs {price}.\n\n"
    except SmmError as exc:
        return Reply(str(exc), ok=False, rows=((("◀️ Back", f"sm:s:{sid}"),),))
    return Reply(
        f"🔗 Link for {svc.name} × {qty:,}\n\n"
        f"{priced}"
        "Paste the public URL of the profile or post (https://…). "
        "Private / login-walled links will fail.",
        rows=((("✖️ Cancel", f"sm:s:{sid}"),),),
    )


def confirm(ui, user_id: str, sid: str, qty: int) -> Reply:
    shop = _shop(ui)
    svc = shop.catalog.get(sid)
    wizard = (ui._wizard or {}).get(user_id) or {}
    link = str(wizard.get("link") or "")
    if svc is None or not link or qty <= 0:
        return ask_link(ui, user_id, sid, qty)
    extra = _extra(ui)
    try:
        price = shop.catalog.quote(svc, qty, extra_rate=extra)
    except SmmError as exc:
        return Reply(str(exc), ok=False, rows=((("◀️ Back", f"sm:s:{sid}"),),))
    bal = ui.router.balance_of(user_id)
    warn = ""
    if bal.paise < price.paise:
        warn = f"\n\n⚠️ Balance {bal} — top up {price - bal} first."
    ui._wizard[user_id] = {
        "flow": "smm", "step": "confirm", "sid": sid, "qty": qty, "link": link,
    }
    return Reply(
        f"✅ Confirm social boost\n\n"
        f"{svc.name}\n"
        f"Qty {qty:,} · {price}\n"
        f"Link: {link}\n"
        f"💰 Balance {bal}{warn}\n\n"
        "Price is re-quoted at this tap. Delivery is not instant and there "
        "is no 5-minute auto-refund.",
        rows=(
            ((f"✅ Pay {price}", f"sm:buy:{sid}:{qty}"),),
            (("✖️ Cancel", "sm"),),
        ),
    )


def begin_buy(ui, user_id: str, sid: str, qty: int) -> Reply:
    shop = _shop(ui)
    svc = shop.catalog.get(sid)
    wizard = (ui._wizard or {}).get(user_id) or {}
    link = str(wizard.get("link") or "")
    if svc is None or not link:
        return ask_link(ui, user_id, sid, qty)
    extra = _extra(ui)
    owner = _clone_owner(ui)
    try:
        price = shop.catalog.quote(svc, qty, extra_rate=extra)
    except SmmError as exc:
        return Reply(str(exc), ok=False, rows=((("◀️ Back", f"sm:s:{sid}"),),))

    def job(uid: str) -> Reply:
        try:
            row = shop.place(
                uid, svc, qty, link,
                extra_rate=extra,
                clone_owner=owner,
                wallets=ui.router.wallets,
            )
        except SmmUserError as exc:
            return Reply(
                str(exc),
                ok=False,
                rows=((("💰 Wallet", "w"), ("📣 Social boost", "sm")),
                      (("🏠 Menu", "m"),)),
            )
        ui._wizard.pop(uid, None)
        if row is None:
            return Reply(
                "Order was sent. Check 🧾 My boosts.",
                rows=((("🧾 My boosts", "sm:o"), ("🏠 Menu", "m"))),
            )
        return Reply(
            f"📣 Boost placed #{row.id}\n\n"
            f"{row.service_name}\n"
            f"Qty {row.quantity:,} · charged {row.charge}\n"
            f"Status: {_STATUS.get(row.status, row.status)}\n\n"
            "We'll message you when it finishes. No 5-minute auto-refund — "
            "partial/cancel credits the undelivered share.",
            rows=((("🧾 My boosts", "sm:o"), ("🏠 Menu", "m"))),
        )

    ui._wizard[user_id] = wizard  # keep link through the deferred job
    return Reply(
        f"⏳ Placing {svc.name} × {qty:,} for {price}…\n\n"
        "One moment — we won't retry if the supplier is slow.",
        deferred=job,
    )


def my_boosts(ui, user_id: str) -> Reply:
    store = getattr(ui.router, "wallets", None)
    fn = getattr(store, "user_smm_orders", None) if store is not None else None
    rows_data = []
    if callable(fn):
        try:
            rows_data = list(fn(user_id, limit=12))
        except Exception:  # noqa: BLE001
            rows_data = []
    if not rows_data:
        return Reply(
            "🧾 No social boosts yet.\n\nTap 📣 Social boost to place one.",
            rows=((("📣 Social boost", "sm"), ("🏠 Menu", "m"))),
        )
    rows: list[tuple[tuple[str, str], ...]] = []
    for o in rows_data:
        tag = _STATUS.get(o.status, o.status)
        rows.append(((
            f"{tag} · {_trunc(o.service_name, 16)} · {o.charge}",
            f"sm:od:{o.id}",
        ),))
    rows.append((("📣 New boost", "sm"), ("🏠 Menu", "m")))
    return Reply("🧾 Your social boosts:", rows=tuple(rows))


def _load_order(ui, user_id: str, oid_s: str):
    store = getattr(ui.router, "wallets", None)
    get = getattr(store, "get_smm_order", None) if store is not None else None
    if not callable(get):
        return None
    try:
        oid = int(oid_s)
    except ValueError:
        return None
    try:
        return get(oid, user_id=user_id)
    except TypeError:
        return get(oid)


def order_detail(ui, user_id: str, oid_s: str) -> Reply:
    shop = _shop(ui)
    row = _load_order(ui, user_id, oid_s)
    if row is None:
        return Reply(
            "That boost isn't visible on this account.",
            ok=False, rows=((("🧾 My boosts", "sm:o"),),),
        )
    if row.provider_order_id and row.status in OPEN_STATUSES:
        try:
            row = shop.refresh(row, wallets=ui.router.wallets)
        except Exception:  # noqa: BLE001
            pass
    tag = _STATUS.get(row.status, row.status)
    lines = [
        f"📣 {row.service_name} — #{row.id}",
        f"\n📊 {tag}",
        f"Qty {row.quantity:,}"
        + (f" · remains {row.remains:,}" if row.remains else ""),
        f"💵 Charged {row.charge}",
    ]
    if row.refunded.paise > 0:
        lines.append(f"↩️ Refunded {row.refunded}")
    if row.link:
        lines.append(f"\n🔗 {row.link}")
    actions: list[tuple[str, str]] = []
    if row.refillable and row.status in {"completed", "partial"}:
        actions.append(("🔁 Refill", f"sm:rf:{row.id}"))
    if row.cancelable and row.status in OPEN_STATUSES:
        actions.append(("♻️ Cancel", f"sm:cx:{row.id}"))
    rows: list[tuple[tuple[str, str], ...]] = []
    if actions:
        rows.append(tuple(actions))
    rows.append((("🧾 My boosts", "sm:o"), ("🏠 Menu", "m")))
    return Reply("\n".join(lines), rows=tuple(rows))


def do_refill(ui, user_id: str, oid_s: str) -> Reply:
    shop = _shop(ui)
    row = _load_order(ui, user_id, oid_s)
    if row is None:
        return Reply("That boost isn't visible here.", ok=False,
                     rows=((("🧾 My boosts", "sm:o"),),))
    try:
        shop.request_refill(row)
    except SmmUserError as exc:
        return Reply(str(exc), ok=False, rows=((("◀️ Back", f"sm:od:{row.id}"),),))
    return Reply(
        "🔁 Refill requested. We'll keep the original receipt — check status "
        "from My boosts in a bit.",
        rows=((("◀️ Receipt", f"sm:od:{row.id}"), ("🧾 My boosts", "sm:o"))),
    )


def do_cancel(ui, user_id: str, oid_s: str) -> Reply:
    shop = _shop(ui)
    row = _load_order(ui, user_id, oid_s)
    if row is None:
        return Reply("That boost isn't visible here.", ok=False,
                     rows=((("🧾 My boosts", "sm:o"),),))
    try:
        shop.request_cancel(row)
    except SmmUserError as exc:
        return Reply(str(exc), ok=False, rows=((("◀️ Back", f"sm:od:{row.id}"),),))
    return Reply(
        "♻️ Cancel requested. If the supplier accepts, the undelivered share "
        "returns to your wallet (not instant).",
        rows=((("◀️ Receipt", f"sm:od:{row.id}"), ("🧾 My boosts", "sm:o"))),
    )
