"""FamGateway webhook: signature verification + idempotent credit."""
from __future__ import annotations
import hashlib, hmac, json
from decimal import Decimal

import pytest

from uotpbot.wallets import SqliteWallets
from uotpbot.money import Money
from uotpbot.config import Settings, UotpConfig, FeeModel, EngineConfig
from uotpbot.__main__ import _famgateway_webhook


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
