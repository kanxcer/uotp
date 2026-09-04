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


def test_webhook_none_when_key_unset():
    settings = _settings()
    settings.famgateway_api_key = ""
    assert _famgateway_webhook(settings, SqliteWallets(":memory:")) is None
