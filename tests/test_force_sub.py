"""Force-subscribe: owner sets a channel; customers must join to use the bot."""

from __future__ import annotations

from decimal import Decimal

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider

OWNER, USER = "1", "2"


@pytest.fixture
def rig():
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {"telegram": catalog.sticker_price("telegram")}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER))
    ui = MenuUI(router)
    yield ui, router
    ledger.close()


def _datas(reply) -> list[str]:
    return [d for row in (reply.rows or ()) for _l, d in row]


def test_parse_channel_shapes():
    p = MenuUI._parse_channel
    assert p("off") == ""
    assert p("@MyChan") == "@MyChan"
    assert p("https://t.me/mychan") == "@mychan"
    assert p("t.me/mychan") == "@mychan"
    assert p("-1001234567890") == "-1001234567890"
    assert p("1001234567890") == "-1001234567890"
    assert p("https://t.me/+AbCdEf") is None
    assert p("not a channel!!") is None
    assert p("mychan") == "@mychan"


def test_admin_panel_has_force_sub_button(rig):
    ui, _ = rig
    panel = ui.admin_panel(OWNER)
    assert "a:fs" in _datas(panel)
    assert "Force sub" in panel.text
    assert ui.force_sub_label() == "off"


def test_owner_sets_and_clears_channel(rig):
    ui, _ = rig
    screen = ui.button(OWNER, "a:fs")
    assert "Force subscribe — OFF" in screen.text
    prompt = ui.button(OWNER, "ax:fs")
    assert "FORCE SUBSCRIBE" in prompt.text
    done = ui.text(OWNER, "@otpclub")
    assert done.ok and "ON" in done.text
    assert ui.force_sub_config()["username"] == "otpclub"
    assert ui.force_sub_label() == "ON · @otpclub"
    panel = ui.admin_panel(OWNER)
    assert "ON · @otpclub" in panel.text
    off = ui.button(OWNER, "a:fsoff")
    assert "OFF" in off.text
    assert ui.force_sub_config() is None


def test_non_owner_cannot_set_channel(rig):
    ui, _ = rig
    r = ui.button(USER, "ax:fs")
    assert "Owner only" in r.text
    r2 = ui.button(USER, "a:fsoff")
    assert "Owner only" in r2.text


def test_unjoined_user_is_walled(rig):
    ui, _ = rig
    ui._set_force_sub({"chat": "@otpclub", "title": "OTP Club",
                       "username": "otpclub", "link": "https://t.me/otpclub"})
    wall = ui.text(USER, "/start")
    assert not wall.ok
    assert "Join our channel" in wall.text
    assert "fs:ok" in _datas(wall)
    assert any(d.startswith("url:https://t.me/otpclub") for d in _datas(wall))
    # Buttons and photos are walled too.
    assert "Join our channel" in ui.button(USER, "l").text
    assert "Join our channel" in ui.photo(USER, "file-1").text
    # Owner is never walled.
    assert "YC OTP" in ui.text(OWNER, "/start").text


def test_ive_joined_rechecks_membership(rig):
    ui, _ = rig
    ui._set_force_sub({"chat": "@otpclub", "title": "OTP Club",
                       "username": "otpclub", "link": "https://t.me/otpclub"})
    ui.chat_member_fn = lambda chat, uid: "left"
    again = ui.button(USER, "fs:ok")
    assert "not in the channel" in again.text
    ui.chat_member_fn = lambda chat, uid: "member"
    welcome = ui.button(USER, "fs:ok")
    assert welcome.ok
    assert "You're in" in welcome.text
    assert "YC OTP" in welcome.text


def test_member_can_use_bot(rig):
    ui, _ = rig
    ui._set_force_sub({"chat": "@otpclub", "username": "otpclub",
                       "title": "OTP", "link": "https://t.me/otpclub"})
    ui.chat_member_fn = lambda chat, uid: "member"
    menu = ui.text(USER, "/start")
    assert menu.ok and "YC OTP" in menu.text


def test_api_error_fails_open(rig):
    """A Telegram blip must not lock paying customers out."""
    ui, _ = rig
    ui._set_force_sub({"chat": "@otpclub", "username": "otpclub",
                       "title": "OTP", "link": ""})
    ui.chat_member_fn = lambda chat, uid: "error"
    assert ui.text(USER, "/start").ok


def test_chat_info_requires_bot_admin(rig):
    ui, _ = rig
    ui.chat_info_fn = lambda chat: {
        "ok": True, "id": "-1001", "title": "X", "username": "x",
        "invite_link": "https://t.me/x", "bot_is_admin": False,
    }
    ui.button(OWNER, "ax:fs")
    r = ui.text(OWNER, "@x")
    assert not r.ok
    assert "not an admin" in r.text
    assert ui.force_sub_config() is None


def test_chat_info_saves_resolved_chat(rig):
    ui, _ = rig
    ui.chat_info_fn = lambda chat: {
        "ok": True, "id": "-100999", "title": "OTP Club", "username": "otpclub",
        "invite_link": "https://t.me/+secret", "bot_is_admin": True,
    }
    ui.button(OWNER, "ax:fs")
    r = ui.text(OWNER, "@otpclub")
    assert r.ok
    cfg = ui.force_sub_config()
    assert cfg["chat"] == "-100999"
    assert cfg["link"] == "https://t.me/+secret"
    wall = ui.text(USER, "/start")
    assert any(d == "url:https://t.me/+secret" for d in _datas(wall))


def test_force_sub_persists_in_kv(rig):
    from uotpbot.wallets import SqliteWallets
    ui, router = rig
    router.wallets = SqliteWallets(":memory:")
    ui.button(OWNER, "ax:fs")
    ui.text(OWNER, "@persistme")
    other = MenuUI(router)
    assert other.force_sub_config()["username"] == "persistme"


def test_url_buttons_render_as_telegram_url():
    from types import SimpleNamespace
    from uotpbot.bot.telegram import HAS_TELEGRAM, _reply_markup
    if not HAS_TELEGRAM:
        pytest.skip("python-telegram-bot not installed")
    reply = SimpleNamespace(
        rows=((("📢 Join", "url:https://t.me/otpclub"), ("✅ I've joined", "fs:ok")),),
        buttons=(),
    )
    mark = _reply_markup(reply)
    join, check = mark.inline_keyboard[0]
    assert join.url == "https://t.me/otpclub"
    assert check.callback_data == "fs:ok"
