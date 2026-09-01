"""Provider layer: mock behaviour, idempotency, OTP parsing, error mapping."""

import io
import json
import urllib.error

import pytest

from uotpbot.money import INR, Money
from uotpbot.provider.base import (
    AuthError, InsufficientBalance, NumberAllocation, NumberUnavailable,
    PurchaseTimedOut, ServiceUnavailable, SmsMessage,
)
from uotpbot.provider.mock import MockProvider, MockOutcome
from uotpbot.provider.uotp import ResponseShape, UotpConfig, UotpProvider, _as_money


# --------------------------------------------------------------- OTP parsing
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your code is 847293", "847293"),
        ("847293 is your verification code", "847293"),
        ("Code: 4821. Valid for 5 minutes.", "4821"),
        ("Your OTP is 12345678", "12345678"),
        # Must not grab a year or the tail of a phone number.
        ("In 2024 we sent 847293 to +919876543210", "847293"),
        ("Ref 99 and code 524813", "524813"),
        ("No digits here at all", None),
        ("Only 12", None),           # too short to be an OTP
        ("Call 18001234567890", None),  # too long, not word-bounded as OTP
    ],
)
def test_otp_extraction(text, expected):
    assert SmsMessage("X", text, "now").extract_otp() == expected


def test_otp_prefers_the_longest_candidate():
    msg = SmsMessage("X", "PIN 4821 or full code 482193", "now")
    assert msg.extract_otp() == "482193"


# -------------------------------------------------------------- mock provider
def prices():
    return {"telegram": INR(10), "whatsapp": INR(12), "binance": INR(22)}


def test_mock_charges_the_wallet():
    p = MockProvider(prices(), balance=INR(100))
    alloc = p.buy_number("telegram")
    assert alloc.charged == INR(10)
    assert p.get_balance().credit == INR(90)


def test_mock_rejects_overspend():
    p = MockProvider(prices(), balance=INR(15))
    with pytest.raises(InsufficientBalance) as exc:
        p.buy_number("binance")
    assert exc.value.shortfall == INR(7)


def test_mock_rejects_unknown_service():
    p = MockProvider(prices())
    with pytest.raises(NumberUnavailable):
        p.buy_number("myspace")


def test_idempotency_key_prevents_a_double_charge():
    """A retry after an ambiguous timeout must not buy a second number."""
    p = MockProvider(prices(), balance=INR(100))
    first = p.buy_number("telegram", idempotency_key="abc")
    again = p.buy_number("telegram", idempotency_key="abc")
    assert first.order_id == again.order_id
    assert p.get_balance().credit == INR(90)  # charged once, not twice


def test_forced_outcomes_drive_the_scenario():
    p = MockProvider(prices(), balance=INR(100), seed=1)
    p.force_next(MockOutcome("silent"))
    alloc = p.buy_number("telegram")
    result = p.wait_for_otp(alloc, timeout_seconds=1, poll_interval=0.01)
    assert not result.success and result.timed_out

    p.force_next(MockOutcome("success", otp="123456"))
    alloc2 = p.buy_number("telegram")
    result2 = p.wait_for_otp(alloc2, timeout_seconds=1, poll_interval=0.01)
    assert result2.success and result2.code == "123456"


def test_cancel_refunds():
    p = MockProvider(prices(), balance=INR(100))
    alloc = p.buy_number("whatsapp")
    assert p.cancel(alloc.order_id) == INR(12)
    assert p.get_balance().credit == INR(100)


def test_cancel_unknown_order_refunds_nothing():
    p = MockProvider(prices())
    assert p.cancel("nope") == Money.zero()


def test_allocation_expiry():
    a = NumberAllocation("1", "+919", "telegram", "in", INR(10), validity_minutes=0)
    assert a.is_expired()
    b = NumberAllocation("2", "+919", "telegram", "in", INR(10), validity_minutes=20)
    assert not b.is_expired()
    assert 0 < b.seconds_left() <= 1200


# ------------------------------------------------------- UOTP HTTP adapter
class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests, returns canned responses or raises canned errors."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def urlopen(self, req, timeout=None):
        self.calls.append({
            "url": req.full_url, "method": req.method,
            "headers": dict(req.headers),
            "body": req.data.decode() if req.data else None,
        })
        item = self.responses.pop(0) if self.responses else {}
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def provider_with(*responses, **cfg):
    opener = FakeOpener(list(responses))
    return UotpProvider(UotpConfig(api_key="k", **cfg), opener=opener), opener


def test_auth_header_format():
    p, opener = provider_with({"balance": 500})
    p.get_balance()
    assert opener.calls[0]["headers"]["Authorization"] == "Bearer k"


def test_auth_header_can_be_raw():
    p, opener = provider_with({"balance": 500}, auth_scheme="", auth_header="X-Api-Key")
    p.get_balance()
    assert opener.calls[0]["headers"]["X-api-key"] == "k" or \
        opener.calls[0]["headers"].get("X-Api-Key") == "k"


def test_balance_is_parsed_from_a_nested_path():
    p, _ = provider_with({"data": {"wallet": {"balance": "1234.50"}}})
    cfg = ResponseShape(balance="data.wallet.balance")
    p.config = UotpConfig(api_key="k", shape=cfg)
    assert p.get_balance().credit == INR("1234.50")


def test_missing_balance_key_gives_an_actionable_error():
    p, _ = provider_with({"foo": 1})
    with pytest.raises(Exception, match="Set shape.balance"):
        p.get_balance()


def test_prices_accepts_a_mapping():
    p, _ = provider_with({"prices": {"telegram": 10, "whatsapp": "12.00"}})
    got = p.get_prices()
    assert got["telegram"] == INR(10)
    assert got["whatsapp"] == INR(12)


def test_prices_accepts_a_list():
    p, _ = provider_with(
        {"prices": [{"service": "telegram", "price": 10}, {"slug": "google", "price": 15}]}
    )
    got = p.get_prices()
    assert got == {"telegram": INR(10), "google": INR(15)}


def test_buy_sends_the_idempotency_key():
    p, opener = provider_with({"id": "abc", "number": "+919999", "price": 10})
    alloc = p.buy_number("telegram", "in", idempotency_key="ord-1")
    assert alloc.order_id == "abc"
    assert alloc.phone == "+919999"
    assert alloc.charged == INR(10)
    assert opener.calls[0]["headers"]["Idempotency-key"] == "ord-1"


def test_buy_failure_on_missing_fields():
    p, _ = provider_with({"unexpected": True})
    with pytest.raises(Exception, match="missing"):
        p.buy_number("telegram")


def test_sms_list_is_parsed():
    p, _ = provider_with(
        {"messages": [{"sender": "WA", "text": "code 123456", "created_at": "t"}]}
    )
    msgs = p.get_sms("abc")
    assert len(msgs) == 1
    assert msgs[0].extract_otp() == "123456"


def test_http_error_mapping():
    cases = [
        (401, AuthError), (403, AuthError), (402, InsufficientBalance),
        (404, ServiceUnavailable), (408, PurchaseTimedOut),
        (429, ServiceUnavailable), (500, ServiceUnavailable), (503, ServiceUnavailable),
    ]
    for code, exc in cases:
        err = urllib.error.HTTPError(
            "u", code, "x", {}, io.BytesIO(json.dumps({"message": "m"}).encode())
        )
        p, _ = provider_with(err)
        with pytest.raises(exc):
            p.get_balance()


def test_error_body_signals_insufficient_balance():
    p, _ = provider_with({"error": True, "message": "Insufficient balance"})
    with pytest.raises(InsufficientBalance):
        p.get_balance()


def test_error_body_signals_no_stock():
    p, _ = provider_with({"error": True, "message": "No numbers available in stock"})
    with pytest.raises(NumberUnavailable):
        p.get_balance()


def test_error_body_signals_auth():
    p, _ = provider_with({"error": True, "message": "Invalid API key"})
    with pytest.raises(AuthError):
        p.get_balance()


def test_non_json_response_is_reported():
    class Html(FakeResponse):
        def __init__(self):
            self._body = b"<html>502 bad gateway</html>"
            self.status = 200

    class Opener:
        def urlopen(self, req, timeout=None):
            return Html()

    p = UotpProvider(UotpConfig(api_key="k"), opener=Opener())
    with pytest.raises(Exception, match="non-JSON"):
        p.get_balance()


def test_probe_refuses_to_run_without_a_key():
    p = UotpProvider(UotpConfig(api_key=""), opener=FakeOpener([]))
    with pytest.raises(AuthError, match="no API key"):
        p.probe()


def test_config_strips_trailing_slash():
    assert UotpConfig(base_url="https://x.test/").base_url == "https://x.test"


def test_money_coercion():
    assert _as_money(10) == INR(10)
    assert _as_money("12.50") == INR("12.50")
    assert _as_money(12.5) == INR("12.50")
    assert _as_money("Rs.1,234.00") == INR("1234.00")
    with pytest.raises(Exception):
        _as_money("not a number")
    with pytest.raises(Exception):
        _as_money(True)
