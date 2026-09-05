"""FamGateway webhook: signature verification + idempotent credit."""
from __future__ import annotations
import hashlib, hmac, json
from decimal import Decimal

import pytest

from uotpbot.wallets import SqliteWallets
from uotpbot.money import Money
from uotpbot.config import Settings, UotpConfig, FeeModel, EngineConfig
from uotpbot.__main__ import _famgateway_webhook, _credit_fg_wallet, _notify_paid
from uotpbot.bot.alerts import PaymentNotifier


def _settings():
    return Settings(
        uotp=UotpConfig(api_key="h1xbnr"), fees=FeeModel(), engine=EngineConfig(),
        famgateway_api_key="test_key_abc", famgateway_base_url="https://famgateway.in",
    )


def _sig(key, body):
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_credits_and_is_idempotent():
    wallets = SqliteWallets(":memory:")
    settings = _settings()
    handler = _famgateway_webhook(settings, wallets)
    assert handler is not None
    # Pre-seed the order mapping the bot writes when it creates the order.
    wallets.kv_set("fg_order:fg_AAA", "222")
    wallets.kv_set("fg_amt:fg_AAA", "100")
    body = json.dumps({"event": "payment.success", "order_id": "fg_AAA",
                       "amount": 100, "utr": "420", "status": "success"}).encode()
    code, payload = handler(body, _sig("test_key_abc", body))
    assert code == 200 and payload["status"] == "credited"
    assert wallets.balance("222") == Money(10000)
    # Duplicate webhook (retry / double callback): credited exactly once.
    code2, payload2 = handler(body, _sig("test_key_abc", body))
    assert code2 == 200 and payload2["status"] == "already_credited"
    assert wallets.balance("222") == Money(10000)


def test_webhook_rejects_bad_signature():
    wallets = SqliteWallets(":memory:")
    handler = _famgateway_webhook(_settings(), wallets)
    body = json.dumps({"event": "payment.success", "order_id": "fg_BBB"}).encode()
    code, payload = handler(body, _sig("WRONG", body))
    assert code == 401
    assert wallets.balance("222") == Money(0)


def test_webhook_ignores_non_success_and_unmapped():
    wallets = SqliteWallets(":memory:")
    handler = _famgateway_webhook(_settings(), wallets)
    # A refund event must never credit.
    body = json.dumps({"event": "payment.failed", "order_id": "fg_CCC"}).encode()
    code, payload = handler(body, _sig("test_key_abc", body))
    assert code == 200 and payload["status"] == "acknowledged"
    assert wallets.balance("222") == Money(0)
    # A success webhook with no order mapping (no user) must NOT credit blindly.
    body2 = json.dumps({"event": "payment.success", "order_id": "fg_DDD"}).encode()
    code2, payload2 = handler(body2, _sig("test_key_abc", body2))
    assert code2 == 200 and payload2["status"] == "no_mapping"
    assert wallets.balance("222") == Money(0)


def test_webhook_reads_live_key_from_kv():
    """The webhook must honour a key set live via the admin panel (kv), not
    just one present in the env at boot. With no env key but a kv key, the
    handler is still registered and credits using the kv key."""
    settings = _settings()
    settings.famgateway_api_key = ""           # no env key
    wallets = SqliteWallets(":memory:")
    wallets.kv_set("famgateway_api_key", "kv_key")   # owner set it live
    wallets.kv_set("fg_order:fg_KV", "222")
    wallets.kv_set("fg_amt:fg_KV", "75")
    handler = _famgateway_webhook(settings, wallets)
    assert handler is not None, "handler must exist even with no env key"
    body = json.dumps({"event": "payment.success", "order_id": "fg_KV",
                       "amount": 75}).encode()
    code, payload = handler(body, _sig("kv_key", body))
    assert code == 200 and payload["status"] == "credited"
    assert wallets.balance("222") == Money(7500)
    # And it rejects a signature for a DIFFERENT key than the live one.
    body2 = json.dumps({"event": "payment.success", "order_id": "fg_KV",
                        "amount": 75}).encode()
    code2, _ = handler(body2, _sig("test_key_abc", body2))  # old stale key
    assert code2 == 401


def test_webhook_404_when_no_key_at_all():
    settings = _settings()
    settings.famgateway_api_key = ""
    wallets = SqliteWallets(":memory:")
    handler = _famgateway_webhook(settings, wallets)
    body = json.dumps({"event": "payment.success", "order_id": "fg_X"}).encode()
    code, payload = handler(body, _sig("whatever", body))
    assert code == 400 and payload["status"] == "not_configured"
    assert wallets.balance("222") == Money(0)


def test_kv_scan_returns_prefixed_orders():
    """kv_scan must list open orders so the background sweep can find them."""
    wallets = SqliteWallets(":memory:")
    wallets.kv_set("fg_order:fg_1", "222")
    wallets.kv_set("fg_order:fg_2", "333")
    wallets.kv_set("fg_amt:fg_1", "50")
    wallets.kv_set("other", "x")          # not an order
    orders = wallets.kv_scan("fg_order:")
    assert orders == {"fg_order:fg_1": "222", "fg_order:fg_2": "333"}
    assert "other" not in orders


def test_sweep_credits_paid_orders_and_skips_unpaid():
    """The sweep credits a paid-but-no-webhook order and leaves unpaid ones.

    Stubs the gateway so the test never touches the network."""
    from uotpbot import __main__ as mm
    import uotpbot.gateway as gw
    from uotpbot.wallets import SqliteWallets

    wallets = SqliteWallets(":memory:")
    wallets.kv_set("fg_order:fg_paid", "222")
    wallets.kv_set("fg_amt:fg_paid", "100")
    wallets.kv_set("fg_order:fg_wait", "333")
    wallets.kv_set("fg_amt:fg_wait", "50")

    calls = []
    class _FakeG:
        def __init__(self, key, base_url):
            calls.append((key, base_url))
        def verify(self, order_id):
            class S: pass
            s = S()
            s.is_paid = order_id == "fg_paid"
            s.state = "success" if s.is_paid else "pending"
            s.utr = "4" if s.is_paid else ""
            s.sender_name = "P" if s.is_paid else ""
            s.amount = None
            return s

    orig = gw.FamGateway
    gw.FamGateway = _FakeG
    try:
        mm._famgateway_sweep(wallets, "k", "https://famgateway.in")
    finally:
        gw.FamGateway = orig

    assert wallets.balance("222") == Money(10000), "paid order credited"
    assert wallets.balance("333") == Money(0), "unpaid order not credited"
    assert wallets.kv_get("fg_credited:fg_paid") == "1"
    assert ("k", "https://famgateway.in") in calls


# -- QR message auto-edit on payment completion ---------------------------
def test_paid_order_edits_the_qr_message_via_notifier():
    """Once a payment is credited, the QR message the customer was looking at
    must be edited to a success note (via the PaymentNotifier bridge) -- no tap
    needed. Uses a materialised chat:message carried in fg_msg:<order>."""
    wallets = SqliteWallets(":memory:")
    # The bot remembered where it sent the QR for this order.
    wallets.kv_set("fg_order:fg_EDIT", "222")
    wallets.kv_set("fg_amt:fg_EDIT", "100")
    wallets.kv_set("fg_msg:fg_EDIT", "222:77")

    edited = {}
    class _FakeNotifier:
        def edit_order_message(self, chat_id, message_id, text):
            edited["call"] = (chat_id, message_id, text)
            return True
    notifier = _FakeNotifier()

    ok = _credit_fg_wallet(wallets, "222", "fg_EDIT", Decimal("100"),
                           notifier=notifier)
    assert ok is True
    assert wallets.balance("222") == Money(10000)
    # The QR message was edited to a success note, not left as a plain QR.
    assert edited["call"] is not None
    chat_id, message_id, text = edited["call"]
    assert (chat_id, message_id) == ("222", 77)
    assert "Payment received" in text
    assert "100" in text


def test_happy_path_message_not_found_skips_edit():
    """Without a stored message (e.g. redeploy before the QR was sent, or a
    sweep-only run) the credit still happens and edit is simply skipped."""
    wallets = SqliteWallets(":memory:")
    wallets.kv_set("fg_order:fg_NOEDIT", "222")
    wallets.kv_set("fg_amt:fg_NOEDIT", "50")
    edited = []
    class _FakeNotifier:
        def edit_order_message(self, chat_id, message_id, text):
            edited.append((chat_id, message_id, text))
            return True
    ok = _credit_fg_wallet(wallets, "222", "fg_NOEDIT", Decimal("50"),
                           notifier=_FakeNotifier())
    assert ok is True
    assert wallets.balance("222") == Money(5000)
    assert edited == [], "no fg_msg stored -> no edit attempted"


def test_payment_notifier_falls_back_to_text_when_caption_edit_fails():
    """The notifier edits via the Bot HTTP API (PTB 21 has no Application.loop,
    so webhook/sweep threads cannot await app.bot.*). Caption edit first; if
    that is rejected, fall back to editMessageText."""
    import json
    from io import BytesIO
    from unittest.mock import MagicMock, patch
    import urllib.error

    app = MagicMock()
    app.bot.token = "123:ABC"
    notifier = PaymentNotifier()
    notifier.attach(app)

    calls = []

    def fake_urlopen(req, timeout=15):
        url = getattr(req, "full_url", str(req))
        calls.append(url)
        if "editMessageCaption" in url:
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", hdrs=None,
                fp=BytesIO(json.dumps({
                    "ok": False, "description": "there is no caption",
                }).encode()),
            )

        class _Resp:
            def read(self):
                return json.dumps({"ok": True, "result": True}).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = notifier.edit_order_message(222, 77, "✅ done")
    assert ok is True
    assert any("editMessageCaption" in u for u in calls)
    assert any("editMessageText" in u for u in calls)


def test_payment_notifier_edits_without_app_loop():
    """The production bug: PTB 21 Application has no .loop, so the old
    run_coroutine_threadsafe path skipped the edit. HTTP must still work."""
    import json
    from unittest.mock import MagicMock, patch

    app = MagicMock()
    app.bot.token = "123:ABC"
    # Deliberately NO app.loop -- this is the PTB 21 production shape.
    if hasattr(app, "loop"):
        del app.loop
    notifier = PaymentNotifier()
    notifier.attach(app)

    class _Resp:
        def read(self):
            return json.dumps({"ok": True, "result": True}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
        ok = notifier.edit_order_message(222, 77, "✅ Payment received!")
    assert ok is True
