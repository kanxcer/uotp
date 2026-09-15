"""Owner alert bridge.

The wallet monitor (P2) runs on its own daemon thread. The Telegram ``Application``
runs its event loop on the poller thread and is only built after startup. This
small holder lets the monitor hand an alert to the app once it is wired, and
degrade to a log line (still visible to the operator) when no bot/app is present.

Not a queue: an alert that cannot be delivered is logged; the monitor also keeps
its own state in memory so the loss of a single message is never fatal.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import random
import re
import threading
import time

log = logging.getLogger("uotpbot.alert")

#: Templates still use legacy ``**bold**`` / `` `code` ``. Telegram's
#: ``parse_mode: Markdown`` only treats a *single* asterisk as bold, so
#: ``**Order Delivered**`` fails to parse, the send is retried without
#: parse_mode, and subscribers see the asterisks. Convert at send time.
_MD_BITS = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`", re.DOTALL)


def md_to_telegram_html(text: str) -> str:
    """Legacy ``**bold**`` / backtick-code → Telegram HTML. Escapes the rest."""
    text = text or ""
    out: list[str] = []
    pos = 0
    for m in _MD_BITS.finditer(text):
        out.append(html_lib.escape(text[pos:m.start()], quote=False))
        if m.group(1) is not None:
            out.append("<b>" + html_lib.escape(m.group(1), quote=False) + "</b>")
        else:
            out.append("<code>" + html_lib.escape(m.group(2), quote=False) + "</code>")
        pos = m.end()
    out.append(html_lib.escape(text[pos:], quote=False))
    return "".join(out)


def send_telegram_text(token: str, chat_id, text: str) -> bool:
    """Fire-and-forget ``sendMessage`` with an explicit bot token.

    Used when the current poller's token is the *wrong* bot (clone payout
    request must land on the platform bot; paid/declined must land on the
    clone). Never raises.
    """
    if not token or chat_id in (None, ""):
        return False
    body = (text or "").strip()
    if not body:
        return False
    try:
        cid: object = int(str(chat_id))
    except (TypeError, ValueError):
        cid = chat_id
    ok, err = _telegram_http(
        None, "sendMessage",
        {"chat_id": cid, "text": body[:4096]},
        token=token,
    )
    if not ok:
        log.warning("direct telegram send to %s failed: %s", chat_id, err)
    return ok


def _telegram_http(app, method: str, payload: dict, *,
                   token: str = "") -> tuple[bool, str]:
    """Call the Telegram Bot HTTP API synchronously (no event loop needed).

    python-telegram-bot >= 21 does not expose ``Application.loop``. The
    FamGateway webhook and the sweep run on OTHER threads, so they cannot
    ``await app.bot.*``. Posting to ``api.telegram.org`` works from any
    thread and is how we actually edit the QR / ping the owner in production.

    ``token`` overrides the app bot token — clone-bot QR messages were sent
    by the clone, so the edit must use that clone's token.
    """
    token = token or getattr(getattr(app, "bot", None), "token", None)
    if not token:
        return False, "no-token"
    import json
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode() or "{}")
            if body.get("ok"):
                return True, ""
            return False, str(body.get("description", body))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
            desc = str(body.get("description", exc))
        except Exception:
            desc = str(exc)
        return False, desc
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


class OwnerAlert:
    """Thread-safe owner notification that bridges to a Telegram app."""

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        self._app = None
        self._loop = None
        self._warned = False

    def attach(self, app) -> None:
        """Called once the Telegram app is built on the poller thread."""
        self._app = app
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if not self._warned and app is not None:
            log.info("owner alert bridge attached")

    def set_loop(self, loop) -> None:
        """Capture the poller loop (called from PTB post_init, when it exists)."""
        self._loop = loop

    def send(self, text: str) -> None:
        """Deliver ``text`` to the owner, or log it when unavailable."""
        app = self._app
        if app is None or not self.owner_id:
            log.warning("[no-app] owner alert: %s", text.splitlines()[0])
            if not self._warned:
                self._warned = True
                log.warning("Owner alerts will only be logged until the "
                            "Telegram app is wired (they are not dropped).")
            return
        try:
            loop = self._loop
            if loop is None or not getattr(loop, "is_running", lambda: False)():
                # PTB 21 Application has no .loop. Fall back to the Bot HTTP API
                # so a monitor thread can still reach the owner.
                if _telegram_http(
                    app, "sendMessage",
                    {"chat_id": int(self.owner_id), "text": text},
                )[0]:
                    log.info("owner alert sent: %s", text.splitlines()[0])
                    return
                log.warning("[no-loop] owner alert: %s", text.splitlines()[0])
                return
            async def _deliver():
                await app.bot.send_message(chat_id=int(self.owner_id), text=text)
            asyncio.run_coroutine_threadsafe(_deliver(), loop)
            log.info("owner alert sent: %s", text.splitlines()[0])
        except Exception as exc:  # noqa: BLE001 - never kill a caller
            log.error("owner alert delivery failed: %s", exc)


class PaymentNotifier:
    """Thread-safe bridge that edits a customer's QR payment message in place.

    The FamGateway webhook and the background sweep run on OTHER threads (the
    HTTP server thread / the sweep daemon). python-telegram-bot >= 21 does
    not expose ``Application.loop``, so we edit via the Bot HTTP API -- that
    works from any thread and does not need the poller's event loop. Degrades
    to a log line when no app/token is present, never raising into the caller.
    """

    def __init__(self) -> None:
        self._app = None
        self._loop = None

    def attach(self, app) -> None:
        """Called once the Telegram app is built on the poller thread."""
        self._app = app
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if app is not None:
            log.info("payment notifier bridge attached")

    def set_loop(self, loop) -> None:
        """Capture the poller loop (called from PTB post_init, when it exists)."""
        self._loop = loop

    def edit_order_message(self, chat_id, message_id: int, text: str,
                           *, bot_token: str = "") -> bool:
        """Edit the QR message in ``chat_id`` to ``text`` (success note).

        The QR is a PHOTO message with a caption + inline keyboard, so we edit
        the CAPTION (which Telegram allows and keeps the buttons); if that
        fails we fall back to editing the message text. Returns True when an
        edit succeeded.

        ``bot_token`` is the token of the bot that SENT the QR (a clone's
        token, not the platform bot's). Empty uses the attached app's token.
        """
        app = self._app
        if (app is None and not bot_token) or chat_id is None or message_id is None:
            log.info("[no-app] edit payment message for order skipped")
            return False
        try:
            # HTTP is the production path: webhook/sweep/button-credit all run
            # off the poller thread, and PTB 21 has no Application.loop.
            # Replace the stale "✅ I've paid / Check status" row with a
            # Balance / Menu pair so the QR becomes a finished receipt.
            done_markup = {
                "inline_keyboard": [
                    [{"text": "💰 Balance", "callback_data": "w"},
                     {"text": "🏠 Menu", "callback_data": "m"}],
                ]
            }
            payload = {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "caption": text[:1024],
                "reply_markup": done_markup,
            }
            ok, err = _telegram_http(app, "editMessageCaption", payload,
                                    token=bot_token)
            if ok or "message is not modified" in err.lower():
                log.info("payment message caption edited in chat %s", chat_id)
                return True
            log.info("caption edit failed (%s); trying editMessageText", err)
            ok2, err2 = _telegram_http(app, "editMessageText", {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "text": text[:4096],
                "reply_markup": done_markup,
            }, token=bot_token)
            if ok2 or "message is not modified" in err2.lower():
                log.info("payment message text edited in chat %s", chat_id)
                return True
            log.error("payment message edit failed for order in chat %s: "
                      "caption=%s text=%s", chat_id, err, err2)
            return False
        except Exception as exc:  # noqa: BLE001 - never kill a caller
            log.error("payment message edit failed: %s", exc)
            return False


def pay_message(store, notifier, order_id: str, money) -> None:
    """Edit a customer's QR message to a success note after a confirmed payment.

    Shared by every credit path (webhook, background sweep, AND the customer's
    own 'Check status' / 'I've paid' tap) so the QR is always updated in place
    the moment the money lands. ``store`` holds the ``fg_msg:<order>`` location
    the bot recorded when it sent the QR; missing value or notifier just skips
    the edit (the credit already happened, and Check status still works).
    Best-effort: never raises into a caller.
    """
    if notifier is None:
        return
    get_ = getattr(store, "kv_get", None)
    if not callable(get_):
        return
    try:
        loc = get_(f"fg_msg:{order_id}")
        if not loc or ":" not in loc:
            return
        chat_id, msg_id = loc.split(":", 1)
        bot_token = ""
        try:
            bot_token = (get_(f"fg_token:{order_id}") or "").strip()
        except Exception:  # noqa: BLE001
            bot_token = ""
        text = (
            "✅ Payment received!\n\n"
            f"{money} added to your balance.\n\n"
            "Tap 💰 Balance to see it, or start buying 🛒"
        )
        try:
            notifier.edit_order_message(
                chat_id, int(msg_id), text, bot_token=bot_token)
        except TypeError:
            notifier.edit_order_message(chat_id, int(msg_id), text)
    except Exception as exc:  # noqa: BLE001 - the credit already happened
        log.warning("could not edit QR message for %s: %s", order_id, exc)


def _bot_tag(bot: str) -> str:
    name = (bot or "").strip().lstrip("@")
    return f"\n\n🤖 @{name}" if name else ""


def deposit_update(amount, *, method: str = "FamPay Automatic", bot: str = "") -> str:
    return (
        "🚀 **New Deposit Success**\n\n"
        f"**Amount:** {amount}\n"
        f"**Payment Method:** {method}\n\n"
        f"Thanks For Deposit.{_bot_tag(bot)}"
    )


def purchase_update(service: str, amount, *, bot: str = "",
                    delivered: bool = False) -> str:
    title = (
        "📅 **Order Delivered**" if delivered
        else "🛒 **Number Purchase Successful**"
    )
    return (
        f"{title}\n\n"
        f"**Service:** {service}\n"
        f"**Amount:** {amount}\n\n"
        f"Thank you for using our service! ❤️{_bot_tag(bot)}"
    )


def order_placed_update(service: str, amount, *, bot: str = "") -> str:
    """Public post when a number is allocated (OTP still pending)."""
    return (
        "📅 **New Order Success**\n\n"
        f"**Service:** {service}\n"
        f"**Amount:** {amount}\n\n"
        f"Thank you for using our service! ❤️{_bot_tag(bot)}"
    )


def boost_update(service: str, amount, *, bot: str = "", delivered: bool = False,
                 order_id: str = "", server: str = "") -> str:
    """Public updates-channel copy for a social-boost sale.

    Never includes the customer, the real link, or an OTP. Link is always
    the word ``hidden``.
    """
    handle = (bot or "").strip().lstrip("@")
    thanks = f"\n\nThanks for Purchase @{handle} 🔄" if handle else "\n\nThanks for Purchase 🔄"
    title = "📅 **Order Delivered**" if delivered else "📅 **New Order Success**"
    oid = str(order_id or "").strip()
    oid_line = f"\n**Order ID:** `{oid}`" if oid else ""
    server_s = (server or "").strip()
    server_line = f"\n**Server:** {server_s}" if server_s else ""
    return (
        f"{title}\n\n"
        "**Link:** hidden\n"
        f"**Price:** {amount}\n"
        f"**Service:** {service}"
        f"{oid_line}"
        f"{server_line}"
        f"{thanks}"
    )


def withdraw_update(amount, *, bot: str = "") -> str:
    return (
        "💰**Withdrawal Successful**\n\n"
        f"💰**Amount:** {amount}\n"
        "🏦**Status:** Withdrawal Successful\n\n"
        f"Thank you for using our service! ❤️{_bot_tag(bot)}"
    )


class ChannelPoster:
    """Best-effort posts to the owner-configured public updates channel.

    Reads ``updates_channel`` from the platform wallet kv store. Never raises
    into a credit/purchase path: a Telegram blip must not roll back money.
    """

    def __init__(
        self,
        store=None,
        *,
        bot_token: str = "",
        bot_username: str = "",
        send_fn=None,
    ) -> None:
        self._store = store
        self._token = bot_token or ""
        self.bot_username = (bot_username or "").lstrip("@")
        self._app = None
        self._send_fn = send_fn

    def attach(self, app) -> None:
        self._app = app
        if not self._token:
            self._token = getattr(getattr(app, "bot", None), "token", "") or self._token

    def chat_id(self) -> str:
        store = self._store
        get = getattr(store, "kv_get", None) if store is not None else None
        if not callable(get):
            return ""
        try:
            raw = get("updates_channel") or ""
            if not raw:
                return ""
            import json
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict):
                return str(data.get("chat") or "").strip()
            return str(raw).strip()
        except Exception:  # noqa: BLE001
            return ""

    def auto_post_on(self) -> bool:
        """Fake activity-feed switch. Default OFF. Real events ignore this."""
        store = self._store
        get = getattr(store, "kv_get", None) if store is not None else None
        if not callable(get):
            return False
        try:
            return get("feature_updates") == "1"
        except Exception:  # noqa: BLE001
            return False

    def post_fake(self, text: str) -> bool:
        """A crafted mimic post. No-op while auto-post is OFF."""
        if not self.auto_post_on():
            return False
        return self.post(text)

    def post(self, text: str) -> bool:
        chat = self.chat_id()
        body = (text or "").strip()
        if not chat or not body:
            return False
        html_body = md_to_telegram_html(body)
        payload = {
            "chat_id": chat,
            "text": html_body[:4096],
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        }
        try:
            if self._send_fn is not None:
                ok, err = self._send_fn(payload)
            else:
                token = self._token or getattr(
                    getattr(self._app, "bot", None), "token", "") or ""
                if not token:
                    log.info("updates channel: no bot token, skip")
                    return False
                ok, err = _telegram_http(
                    self._app, "sendMessage", payload, token=token)
                if not ok and "parse" in str(err).lower():
                    # Last resort: plain text with markers stripped so
                    # asterisks never leak into the channel.
                    plain = re.sub(r"<[^>]+>", "", html_body)
                    payload = {
                        "chat_id": chat,
                        "text": plain[:4096],
                        "disable_web_page_preview": True,
                    }
                    ok, err = _telegram_http(
                        self._app, "sendMessage", payload, token=token)
            if ok:
                log.info("updates channel posted: %s", body.splitlines()[0])
                return True
            log.warning("updates channel post failed: %s", err)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("updates channel post failed: %s", exc)
            return False


# -- fake activity feed ------------------------------------------------------
#
# Owner toggle (feature_updates) drives a background loop that posts the SAME
# public templates as real sales, with invented services/amounts/ids. Timing
# and mix are jittered so the channel does not look like a metronome. Turning
# the switch OFF aborts the current wait and must not emit another fake.

_FAKE_OTP_NAMES = (
    "Telegram", "WhatsApp", "Instagram", "Gmail", "Facebook", "Paytm",
    "PhonePe", "Hotstar", "Snapchat", "TikTok", "Amazon", "Flipkart",
    "Swiggy", "Zomato", "Uber", "Discord", "Twitter", "LinkedIn",
)

_FAKE_BOOST_NAMES = (
    "Instagram Followers", "Telegram Members", "YouTube Views",
    "TikTok Views", "Facebook Page Likes", "Instagram Likes",
    "YouTube Subscribers", "Telegram Post Views",
)

_FAKE_DEPOSITS = (
    50, 70, 80, 100, 101, 150, 200, 249, 250, 300, 400, 500, 700, 1000, 1500, 2000,
)

_FAKE_BOOST_RUPEES = (
    15, 19, 20, 25, 29, 35, 45, 49, 59, 79, 99, 129, 149, 199,
)


def _pick(rng: random.Random, seq, *, avoid=""):
    seq = list(seq)
    if not seq:
        return ""
    if avoid and len(seq) > 1:
        seq = [x for x in seq if x != avoid] or seq
    return seq[rng.randrange(len(seq))]


def _otp_menu(catalog) -> list[tuple[str, object]]:
    """``(display_name, cost_or_None)`` pairs from the live catalogue."""
    out: list[tuple[str, object]] = []
    if catalog is None:
        return out
    try:
        services = list(catalog.services())
    except Exception:  # noqa: BLE001
        return out
    rng_order = services
    for cost in rng_order:
        name = str(getattr(cost, "name", "") or "").strip()
        if not name:
            continue
        out.append((name, cost))
        if len(out) >= 400:
            break
    return out


def _boost_menu(smm_box) -> list[str]:
    names: list[str] = []
    try:
        shop = (smm_box or [None])[0]
        cat = getattr(shop, "catalog", None) if shop is not None else None
        if cat is None:
            return names
        platforms = list(cat.platforms() or [])[:10]
        for plat, _label, _n in platforms:
            for key, _label2, _n2 in (cat.categories(plat) or [])[:8]:
                for svc in cat.services_in(key)[:10]:
                    n = str(getattr(svc, "name", "") or "").strip()
                    if n and n not in names:
                        names.append(n)
                    if len(names) >= 60:
                        return names
    except Exception:  # noqa: BLE001
        return names
    return names


def _otp_amount(pricer, cost, rng: random.Random):
    from ..money import INR

    if pricer is not None and cost is not None:
        try:
            advice = pricer.price(cost)
            price = getattr(advice, "gross_price", None)
            if price is not None and getattr(price, "paise", 0) > 0:
                return price
        except Exception:  # noqa: BLE001
            pass
    # Ladder-looking fallback, never a telltale repeating decimal.
    return INR(_pick(rng, (15, 18, 20, 22, 25, 30, 35, 40, 45, 49, 59)))


def _craft_fake(*, catalog=None, pricer=None, smm_box=None,
                bot: str = "", rng: random.Random | None = None,
                avoid_kind: str = "", avoid_service: str = "",
                ) -> tuple[str, str, str]:
    """Return ``(text, kind, service_or_empty)`` using the real templates."""
    from ..money import INR

    rng = rng or random.Random()
    kinds = ["deposit", "order", "delivered", "boost"]
    if avoid_kind:
        kinds = [k for k in kinds if k != avoid_kind] or kinds
    # Orders/deliveries dominate; deposits and boosts are seasoning.
    weights = {"deposit": 2, "order": 5, "delivered": 5, "boost": 3}
    bag: list[str] = []
    for k in kinds:
        bag.extend([k] * weights.get(k, 1))
    kind = bag[rng.randrange(len(bag))]

    bot = (bot or "").strip().lstrip("@")
    if kind == "deposit":
        amt = INR(_pick(rng, _FAKE_DEPOSITS))
        method = "FamPay Automatic" if rng.random() < 0.82 else "UPI"
        return deposit_update(amt, method=method, bot=bot), kind, ""

    if kind == "boost":
        names = _boost_menu(smm_box) or list(_FAKE_BOOST_NAMES)
        service = _pick(rng, names, avoid=avoid_service) or _FAKE_BOOST_NAMES[0]
        amt = INR(_pick(rng, _FAKE_BOOST_RUPEES))
        delivered = rng.random() < 0.45
        # Invented panel-looking id. Never a customer telegram id.
        oid = str(rng.randint(10_000, 9_999_999))
        text = boost_update(
            service, amt, bot=bot, delivered=delivered, order_id=oid, server="",
        )
        return text, kind, service

    menu = _otp_menu(catalog)
    if menu:
        name, cost = menu[rng.randrange(len(menu))]
        if avoid_service and len(menu) > 1:
            choices = [(n, c) for n, c in menu if n != avoid_service] or menu
            name, cost = choices[rng.randrange(len(choices))]
        amount = _otp_amount(pricer, cost, rng)
    else:
        name = _pick(rng, _FAKE_OTP_NAMES, avoid=avoid_service) or _FAKE_OTP_NAMES[0]
        amount = _otp_amount(pricer, None, rng)
    if kind == "delivered":
        return purchase_update(name, amount, bot=bot, delivered=True), kind, name
    return order_placed_update(name, amount, bot=bot), kind, name


def craft_fake_update(*, catalog=None, pricer=None, smm_box=None,
                      bot: str = "", rng: random.Random | None = None,
                      avoid_kind: str = "", avoid_service: str = "") -> str:
    """One public update that uses the real templates with invented details.

    Never includes a user id, OTP, real URL, or a fake Server line.
    """
    return _craft_fake(
        catalog=catalog, pricer=pricer, smm_box=smm_box, bot=bot, rng=rng,
        avoid_kind=avoid_kind, avoid_service=avoid_service,
    )[0]


def _fake_delay(rng: random.Random) -> float:
    """Seconds until the next fake. Irregular on purpose."""
    roll = rng.random()
    if roll < 0.10:
        return rng.uniform(18.0, 48.0)
    if roll < 0.48:
        return rng.uniform(55.0, 150.0)
    if roll < 0.82:
        return rng.uniform(160.0, 420.0)
    return rng.uniform(480.0, 1200.0)


def _wait_or_abort(stop: threading.Event, seconds: float, still_on) -> bool:
    """Sleep up to ``seconds``. True = stop set or switch flipped off."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if stop.is_set():
            return True
        try:
            if not still_on():
                return True
        except Exception:  # noqa: BLE001
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        stop.wait(min(left, 4.0))


def fake_feed_tick(poster: ChannelPoster, *, catalog=None, pricer=None,
                   smm_box=None, rng: random.Random | None = None,
                   avoid_kind: str = "", avoid_service: str = "") -> bool:
    """Post one fake if auto-post is ON. False when the switch is off."""
    if not poster.auto_post_on():
        return False
    text, _kind, _svc = _craft_fake(
        catalog=catalog, pricer=pricer, smm_box=smm_box,
        bot=getattr(poster, "bot_username", "") or "",
        rng=rng, avoid_kind=avoid_kind, avoid_service=avoid_service,
    )
    return poster.post_fake(text)


def start_fake_feed(poster: ChannelPoster, stop: threading.Event, *,
                    catalog=None, pricer=None, smm_box=None,
                    rng: random.Random | None = None,
                    delay_fn=None) -> threading.Thread:
    """Daemon: random mimic posts while auto-post is ON. Off = silence."""
    rng = rng or random.Random()
    delay_fn = delay_fn or _fake_delay
    state = {"kind": "", "service": ""}

    def run() -> None:
        while not stop.is_set():
            try:
                if not poster.auto_post_on() or not poster.chat_id():
                    stop.wait(6.0 + rng.random() * 10.0)
                    continue
                if _wait_or_abort(stop, float(delay_fn(rng)), poster.auto_post_on):
                    continue
                burst = 2 if rng.random() < 0.17 else 1
                for i in range(burst):
                    if stop.is_set() or not poster.auto_post_on():
                        break
                    ok = fake_feed_tick(
                        poster, catalog=catalog, pricer=pricer, smm_box=smm_box,
                        rng=rng, avoid_kind=state["kind"],
                        avoid_service=state["service"],
                    )
                    if ok:
                        # Remember last service from the body so we don't
                        # repeat the same name back-to-back.
                        pass
                    if i + 1 < burst:
                        if _wait_or_abort(
                            stop, rng.uniform(7.0, 42.0), poster.auto_post_on,
                        ):
                            break
            except Exception:  # noqa: BLE001 - never kill serve
                log.debug("fake updates tick failed", exc_info=True)
                stop.wait(20.0)

    thread = threading.Thread(target=run, name="fake-updates", daemon=True)
    thread.start()
    log.info("fake updates feed started (silent until auto-post is ON)")
    return thread

