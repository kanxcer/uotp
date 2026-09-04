"""Telegram transport regression tests.

The poller runs in a background thread (the HTTP server owns the main thread).
python-telegram-bot registers UNIX signal handlers by default, which only work
in the main thread -- the exact failure seen live on Render:

    RuntimeError: set_wakeup_fd only works in main thread of the main interpreter

The poller died at startup while the health-checked HTTP server kept serving
200s. This pins the fix: run_polling must be called with stop_signals=().
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from uotpbot.bot.telegram import _start_polling  # noqa: E402


class FakeApp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_polling(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_polling_registers_no_signal_handlers():
    app = FakeApp()
    _start_polling(app)
    assert app.calls, "run_polling was never called"
    assert app.calls[0].get("stop_signals") == (), (
        "run_polling must disable PTB's default signal handlers -- they only "
        "work in the main thread and kill a threaded poller at startup"
    )


def test_polling_accepts_all_update_types():
    app = FakeApp()
    _start_polling(app)
    from telegram import Update

    assert app.calls[0].get("allowed_updates") is Update.ALL_TYPES


def test_reply_markup_normalizes_flat_rows():
    """A Reply whose ``rows`` is a FLAT tuple of (label, data) pairs must render
    instead of raising. This is exactly the bug that made the 🆘 Support button
    show only the "OK" popup: _reply_markup used to do
    ``for label, data in row`` over a bare pair, which unpacked the pair's
    elements and the transport's swallow hid the failure."""
    from uotpbot.bot.telegram import _reply_markup

    class R:
        rows = (("🛒 Buy a number", "l"), ("🧾 My numbers", "o"),
                ("🏠 Menu", "m"))

    mark = _reply_markup(R())
    labels = [(b.text, b.callback_data) for row_ in mark.inline_keyboard
              for b in row_]
    assert labels == [("🛒 Buy a number", "l"), ("🧾 My numbers", "o"),
                      ("🏠 Menu", "m")], labels


def test_reply_markup_normalizes_over_nested_row():
    """A single button that was accidentally double-nested must render as a
    one-button row. This is the bug that made 🧾 My numbers show only the "OK"
    popup after a purchase."""
    from uotpbot.bot.telegram import _reply_markup

    class R:
        rows = (("📜 Completed history", "h:all"),)

    mark = _reply_markup(R())
    labels = [(b.text, b.callback_data) for row_ in mark.inline_keyboard
              for b in row_]
    assert labels == [("📜 Completed history", "h:all")], labels


def test_support_and_my_numbers_callback_edit_the_message():
    """Regression: tapping 🆘 Support or 🧾 My numbers (after a purchase) must
    actually edit the message, not just fire the 'OK' popup. Previously the
    broken row shape raised inside _reply_markup and _safe_edit swallowed it."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from uotpbot.bot.commands import CommandRouter
    from uotpbot.bot.telegram import TelegramFrontend
    from uotpbot.bot.commands import CommandRouter
    from uotpbot.bot.telegram import TelegramFrontend
    from uotpbot.bot.ui import MenuUI
    from uotpbot.catalog import Catalog, ServiceCost, WalletPack
    from uotpbot.engine import BotEngine, EngineConfig
    from uotpbot.ledger import Ledger
    from uotpbot.money import INR
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider
    from uotpbot.wallets import SqliteWallets

    def build():
        cat = Catalog(
            {"telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                     Decimal("0.94"), Decimal("0.04"), Decimal("0.95"))},
            (WalletPack("Pro", INR(1000), INR(1150)),))
        ledger = Ledger()
        pricer = Pricer(cat)
        prov = MockProvider({s.slug: cat.sticker_price(s.slug)
                             for s in cat.services()}, balance=INR(5000), seed=7)
        eng = BotEngine(cat, prov, ledger, pricer,
                        config=EngineConfig(retry_cap=3, otp_timeout_seconds=300,
                                            poll_interval=0.05))
        w = SqliteWallets(":memory:")
        router = CommandRouter(eng, cat, pricer, ledger,
                               owner_id="111", allowed_users=("111", "222"),
                               wallets=w)
        w.adjust("222", INR(500))
        return router, MenuUI(router, support_contact="TURVEI")

    router, ui = build()
    router.alloc_and_wait("222", "telegram")  # buy a number (live)
    fe = TelegramFrontend(router, ui=ui)

    async def tap(data):
        msg = MagicMock()
        msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock()
        msg.chat_id = "222"
        msg.message_id = 1
        q = MagicMock()
        q.from_user.id = 222
        q.data = data
        q.message = msg
        q.answer = AsyncMock()
        upd = MagicMock()
        upd.callback_query = q
        await fe.on_callback(upd)
        return msg

    for data in ("support", "o"):
        msg = asyncio.run(tap(data))
        assert msg.edit_text.await_count == 1, \
            f"{data!r} must edit the message, not only answer the popup"
        q_args = msg.edit_text.await_args_list[0][0][0]
        assert q_args, f"{data!r} must edit to non-empty text"


def test_famgateway_amount_message_delivers_qr_photo():
    """The Add Money amount text must run the deferred order-creation and then
    deliver the hosted QR as a fresh photo message (photo_url), not drop it."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from decimal import Decimal

    from uotpbot.bot.commands import CommandRouter
    from uotpbot.bot.telegram import TelegramFrontend
    from uotpbot.bot.ui import MenuUI
    from uotpbot.catalog import Catalog, ServiceCost, WalletPack
    from uotpbot.engine import BotEngine, EngineConfig
    from uotpbot.ledger import Ledger
    from uotpbot.money import INR
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider
    from uotpbot.wallets import SqliteWallets

    cat = Catalog(
        {"telegram": ServiceCost("telegram", "Telegram", "messaging",
                                 INR(10), Decimal("0.94"), Decimal("0.04"),
                                 Decimal("0.95"))},
        (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(cat)
    prov = MockProvider({s.slug: cat.sticker_price(s.slug) for s in cat.services()},
                        balance=INR(5000), seed=7)
    eng = BotEngine(cat, prov, ledger, pricer,
                    config=EngineConfig(retry_cap=3, otp_timeout_seconds=300,
                                        poll_interval=0.05))
    w = SqliteWallets(":memory:")
    router = CommandRouter(eng, cat, pricer, ledger, owner_id="111",
                           allowed_users=("111", "222"), wallets=w)
    ui = MenuUI(router, famgateway_api_key="k", famgateway_base_url="https://famgateway.in")

    class _Client:
        api_key = "k"
        def create_order(self, amount, webhook_url=""):
            class O: pass
            o = O()
            o.order_id = "fg_T"
            o.amount = amount
            o.payable_amount = Decimal("100.05")
            o.qr_url = "https://famgateway.in/s.png"
            return o

    ui._fg_client = _Client()
    fe = TelegramFrontend(router, ui=ui)

    async def run():
        ui.button("222", "t:amount")            # open the amount wizard
        msg = MagicMock()
        msg.text = "100"
        msg.from_user.id = 222
        msg.chat_id = "222"
        msg.message_id = 5
        msg.reply_text = AsyncMock()
        msg.reply_photo = AsyncMock()
        upd = MagicMock()
        upd.message = msg
        await fe.on_message(upd)
        assert msg.reply_photo.await_count == 1, "must deliver the QR as a photo"
        a = msg.reply_photo.await_args_list[-1]
        assert a[1].get("photo") == "https://famgateway.in/s.png"
        assert "fg_T" in a[1].get("caption", "")
        assert msg.reply_text.await_count == 1, "placeholder sent once"

    asyncio.run(run())


def test_callback_answer_has_no_popup_toast():
    """Navigation taps (Support, My numbers, Buy, ...) must answer the
    callback with EMPTY text so no 'OK' toast popup shows -- the edited screen
    is the feedback. Telegram still requires answering (else the button
    spinner hangs)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from uotpbot.bot.commands import CommandRouter
    from uotpbot.bot.telegram import TelegramFrontend
    from uotpbot.bot.ui import MenuUI
    from uotpbot.catalog import Catalog, ServiceCost, WalletPack
    from uotpbot.engine import BotEngine, EngineConfig
    from uotpbot.ledger import Ledger
    from uotpbot.money import INR
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider
    from uotpbot.wallets import SqliteWallets

    cat = Catalog(
        {"telegram": ServiceCost("telegram", "Telegram", "messaging",
                                 INR(10), Decimal("0.94"), Decimal("0.04"),
                                 Decimal("0.95"))},
        (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(cat)
    prov = MockProvider({s.slug: cat.sticker_price(s.slug) for s in cat.services()},
                        balance=INR(5000), seed=7)
    eng = BotEngine(cat, prov, ledger, pricer,
                    config=EngineConfig(retry_cap=3, otp_timeout_seconds=300,
                                        poll_interval=0.05))
    w = SqliteWallets(":memory:")
    router = CommandRouter(eng, cat, pricer, ledger, owner_id="111",
                           allowed_users=("111", "222"), wallets=w)
    ui = MenuUI(router, support_contact="@support")
    fe = TelegramFrontend(router, ui=ui)

    async def tap(data):
        q = MagicMock()
        q.from_user.id = 222
        q.data = data
        q.message = MagicMock()
        q.message.edit_text = AsyncMock()
        q.message.reply_text = AsyncMock()
        q.answer = AsyncMock()
        upd = MagicMock()
        upd.callback_query = q
        await fe.on_callback(upd)
        return q

    q1 = asyncio.run(tap("support"))
    q1.answer.assert_awaited_once_with(), "Support must answer with empty text"
    q1.message.edit_text.assert_awaited(), "Support must edit the message"

    q2 = asyncio.run(tap("o"))
    q2.answer.assert_awaited_once_with(), "My numbers must answer with empty text"
    q2.message.edit_text.assert_awaited(), "My numbers must edit the message"
