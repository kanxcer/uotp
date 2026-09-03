"""Provider layer: mock behaviour, idempotency, OTP parsing, error mapping."""

import io
import os
import urllib.error
from decimal import Decimal

import pytest

from uotpbot.money import INR, Money
from uotpbot.provider.base import (
    AuthError, InsufficientBalance, NumberAllocation, NumberUnavailable,
    ProviderError, PurchaseTimedOut, ServiceUnavailable, SmsMessage,
)
from uotpbot.provider.mock import MockProvider, MockOutcome


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


# ------------------------------------------- UOTP handler_api.php protocol
from uotpbot.provider.uotp import (  # noqa: E402
    ERROR_TOKENS, UotpConfig, UotpProvider, parse_response,
)


# ------------------------------------------------------------- parsing
def test_parse_simple():
    r = parse_response("ACCESS_BALANCE:0")
    assert r.status == "ACCESS_BALANCE"
    assert r.fields == ("0",)
    assert not r.is_error


def test_parse_bare_token_has_no_fields():
    r = parse_response("STATUS_WAIT_CODE")
    assert r.status == "STATUS_WAIT_CODE"
    assert r.fields == ()


def test_parse_preserves_colons_inside_the_payload():
    """ACCESS_NUMBER:id:phone -- a naive 3-way split would mangle a VPA."""
    r = parse_response("ACCESS_NUMBER:12345:+919876543210")
    assert r.status == "ACCESS_NUMBER"
    assert r.fields == ("12345", "+919876543210")
    assert r.field(0) == "12345"
    assert r.field(1) == "+919876543210"


def test_parse_survives_extra_colons():
    r = parse_response("STATUS_OK:1234:extra:bits")
    assert r.status == "STATUS_OK"
    assert r.field(0) == "1234"
    assert r.fields == ("1234", "extra", "bits")


def test_parse_strips_html_and_whitespace():
    assert parse_response("  \n ACCESS_BALANCE:12.50 \n").fields == ("12.50",)


def test_parse_empty_raises():
    with pytest.raises(Exception, match="empty response"):
        parse_response("   ")


def test_field_default():
    r = parse_response("ACCESS_BALANCE:5")
    assert r.field(3, "fallback") == "fallback"


def test_error_tokens_are_flagged():
    for token in ("ERROR_KEY", "ERROR_NO_BALANCE", "ERROR_NO_NUMBERS", "BAD_KEY"):
        assert parse_response(token).is_error
    assert not parse_response("ACCESS_BALANCE:0").is_error


def test_error_token_catalog_is_a_frozenset():
    assert isinstance(ERROR_TOKENS, frozenset)


# --------------------------------------------------------- transport shape
class FakeHTTPResponse:
    def __init__(self, body):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls: list[dict] = []

    def urlopen(self, req, timeout=None):
        self.calls.append({
            "url": req.full_url, "method": req.method,
            "headers": dict(req.headers),
        })
        item = self.bodies.pop(0) if self.bodies else ""
        if isinstance(item, Exception):
            raise item
        return FakeHTTPResponse(item)


def make(*bodies, **cfg):
    opener = FakeOpener(*bodies)
    return UotpProvider(UotpConfig(api_key="TESTKEY", **cfg), opener=opener), opener


def test_request_is_a_get_with_query_params():
    """Verified against the live endpoint: GET, action + api_key in the query."""
    p, opener = make("ACCESS_BALANCE:0")
    p.get_balance()
    url = opener.calls[0]["url"]
    assert url.startswith("https://uotp.store/api/stubs/handler_api.php?")
    assert "action=getBalance" in url
    assert "api_key=TESTKEY" in url
    assert opener.calls[0]["method"] == "GET"


def test_no_authorization_header_is_sent():
    """The key travels in the query string, not a header."""
    p, opener = make("ACCESS_BALANCE:0")
    p.get_balance()
    assert "Authorization" not in opener.calls[0]["headers"]


def test_live_balance_response_is_parsed():
    """The exact body returned by the real endpoint on 2026-09-02."""
    p, _ = make("ACCESS_BALANCE:0")
    assert p.get_balance().credit == INR(0)


def test_balance_with_paise():
    p, _ = make("ACCESS_BALANCE:1234.50")
    assert p.get_balance().credit == INR("1234.50")


def test_balance_divisor_handles_a_paise_denominated_account():
    p, _ = make("ACCESS_BALANCE:123450", balance_divisor=Decimal(100))
    assert p.get_balance().credit == INR("1234.50")


def test_wrong_prefix_gives_an_actionable_error():
    p, _ = make("WALLET_BALANCE:10")
    with pytest.raises(Exception, match="Set balance_prefix"):
        p.get_balance()


def test_unparseable_balance_raises():
    p, _ = make("ACCESS_BALANCE:not-a-number")
    with pytest.raises(Exception, match="cannot parse"):
        p.get_balance()


def test_key_param_and_action_param_are_configurable():
    p, opener = make("ACCESS_BALANCE:1", key_param="key", action_param="act",
                     action_balance="balance")
    p.get_balance()
    assert "act=balance" in opener.calls[0]["url"]
    assert "key=TESTKEY" in opener.calls[0]["url"]


def test_extra_params_are_appended():
    p, opener = make("ACCESS_BALANCE:1", extra_params={"version": "2"})
    p.get_balance()
    assert "version=2" in opener.calls[0]["url"]


# ------------------------------------------------------------- error mapping
@pytest.mark.parametrize("token,exc", [
    ("ERROR_KEY", AuthError),
    ("BAD_KEY", AuthError),
    ("ERROR_IP", AuthError),
    ("ERROR_NO_BALANCE", InsufficientBalance),
    ("ERROR_EMPTY_ACCOUNT", InsufficientBalance),
    ("ERROR_NO_NUMBERS", NumberUnavailable),
    ("ERROR_NO_FREE", NumberUnavailable),
    ("BAD_ACTION", ProviderError),
    ("NO_ACTIVATION", ProviderError),
    ("ERROR_SQL", ProviderError),
])
def test_error_tokens_map_to_exceptions(token, exc):
    p, _ = make(token)
    with pytest.raises(exc):
        p.get_balance()


def test_http_error_mapping():
    cases = [(401, AuthError), (402, InsufficientBalance), (429, ServiceUnavailable),
             (500, ServiceUnavailable), (504, PurchaseTimedOut)]
    for code, exc in cases:
        err = urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"boom"))
        p, _ = make(err)
        with pytest.raises(exc):
            p.get_balance()


def test_network_failure_retries_then_becomes_ambiguous():
    err = urllib.error.URLError("connection reset")
    p, opener = make(err, err, err)
    with pytest.raises(PurchaseTimedOut, match="reconcile"):
        p.get_balance()
    assert len(opener.calls) == 3  # initial + 2 retries


def test_network_failure_recovers_on_retry():
    err = urllib.error.URLError("blip")
    p, _ = make(err, "ACCESS_BALANCE:7")
    assert p.get_balance().credit == INR(7)


# ----------------------------------------------------------------- getNumber
def test_buy_number_parses_id_and_phone():
    p, opener = make("ACCESS_NUMBER:98765:+919876543210")
    alloc = p.buy_number("telegram", "in")
    assert alloc.order_id == "98765"
    assert alloc.phone == "+919876543210"
    assert "action=getNumber" in opener.calls[0]["url"]
    assert "service=telegram" in opener.calls[0]["url"]


def test_buy_number_burned_number_is_reported_not_waited_on():
    """ACCESS_CANCEL means charged-but-unusable: waiting 20 min is pure waste."""
    p, _ = make("ACCESS_CANCEL:98765:+919876543210")
    with pytest.raises(NumberUnavailable, match="already registered"):
        p.buy_number("whatsapp", "in")


def test_buy_number_missing_phone_raises():
    p, _ = make("ACCESS_NUMBER:98765")
    with pytest.raises(Exception, match="no phone number"):
        p.buy_number("telegram")


def test_buy_number_out_of_stock():
    # Every operator pool is walked before concluding "no stock".
    p, _ = make(*(["ERROR_NO_NUMBERS"] * 6))
    with pytest.raises(NumberUnavailable):
        p.buy_number("telegram")


def test_buy_number_insufficient_funds():
    p, _ = make(*(["ERROR_NO_BALANCE"] * 6))
    with pytest.raises(InsufficientBalance):
        p.buy_number("telegram")


def test_idempotency_key_is_sent_as_order_param():
    p, opener = make("ACCESS_NUMBER:1:+919")
    p.buy_number("telegram", "in", idempotency_key="ord-42")
    assert "order=ord-42" in opener.calls[0]["url"]


# --------------------------------------------------------------- getStatus
def test_wait_for_otp_returns_the_code():
    p, _ = make("STATUS_WAIT_CODE", "STATUS_OK:482193")
    alloc = NumberAllocation("555", "+919", "telegram", "in", INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=5, poll_interval=0.01)
    assert result.success and result.code == "482193"
    assert result.attempts == 2


def test_wait_for_otp_stops_on_cancel():
    """A canceled number is dead; polling on would forfeit the refund."""
    p, _ = make("STATUS_WAIT_CODE", "STATUS_CANCEL")
    alloc = NumberAllocation("555", "+919", "telegram", "in", INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=5, poll_interval=0.01)
    assert not result.success and result.timed_out
    assert result.attempts == 2


def test_wait_for_otp_times_out():
    p, _ = make("STATUS_WAIT_CODE", "STATUS_WAIT_CODE", "STATUS_WAIT_CODE")
    alloc = NumberAllocation("555", "+919", "telegram", "in", INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=0.05, poll_interval=0.01)
    assert not result.success and result.timed_out


def test_get_sms_wraps_the_code_as_a_message():
    p, _ = make("STATUS_OK:123456")
    msgs = p.get_sms("555")
    assert len(msgs) == 1 and msgs[0].text == "123456"


def test_get_sms_is_empty_while_waiting():
    p, _ = make("STATUS_WAIT_CODE")
    assert p.get_sms("555") == []


def test_get_sms_swallows_provider_errors():
    p, _ = make("NO_ACTIVATION")
    assert p.get_sms("555") == []


def test_poll_failure_does_not_abandon_a_paid_number():
    err = urllib.error.URLError("blip")
    p, _ = make(err, "STATUS_OK:999888")
    alloc = NumberAllocation("555", "+919", "telegram", "in", INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=5, poll_interval=0.01)
    assert result.success and result.code == "999888"


def test_wait_for_otp_rejects_nonpositive_poll():
    p, _ = make("STATUS_WAIT_CODE")
    alloc = NumberAllocation("555", "+919", "telegram", "in", INR(10))
    with pytest.raises(ValueError):
        p.wait_for_otp(alloc, poll_interval=0)


def test_wait_for_otp_adaptive_still_delivers():
    """Adaptive mode must not delay delivery of an OTP that arrives."""
    p, _ = make("STATUS_WAIT_CODE", "STATUS_OK:555000")
    alloc = NumberAllocation("555", "+919", "telegram", "in", INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=5, poll_interval=0.01,
                            adaptive=True)
    assert result.success and result.code == "555000"


# ---------------------------------------------------------------- lifecycle
def test_cancel_sends_the_cancel_status():
    p, opener = make("STATUS_CANCEL")
    p.cancel("555")
    url = opener.calls[0]["url"]
    assert "action=setStatus" in url and "id=555" in url and "status=8" in url


def test_cancel_does_not_invent_a_refund():
    """The protocol reports no amount, so returning one would corrupt the books."""
    p, _ = make("STATUS_CANCEL")
    assert p.cancel("555") == Money.zero()


def test_complete_sends_the_complete_status():
    p, opener = make("STATUS_OK:123")
    p.complete("555")
    assert "status=6" in opener.calls[0]["url"]


def test_probe_refuses_without_a_key():
    p = UotpProvider(UotpConfig(api_key=""), opener=FakeOpener())
    with pytest.raises(AuthError, match="no API key"):
        p.probe()


def test_probe_returns_the_balance():
    p, _ = make("ACCESS_BALANCE:250.00")
    assert p.probe().credit == INR(250)


def test_config_validation():
    with pytest.raises(ValueError):
        UotpConfig(base_url="")
    with pytest.raises(ValueError):
        UotpConfig(max_retries=-1)
    with pytest.raises(ValueError):
        UotpConfig(balance_divisor=Decimal(0))


def test_get_prices_parses_json():
    p, _ = make('{"telegram": 10, "whatsapp": "12.00"}')
    got = p.get_prices()
    assert got["telegram"] == INR(10)
    assert got["whatsapp"] == INR(12)


def test_get_prices_handles_nested_country_prices():
    p, _ = make('{"telegram": {"price": {"0": 12, "1": 15}}}')
    assert p.get_prices()["telegram"] == INR(12)  # cheapest country


def test_get_prices_reports_a_protocol_error():
    p, _ = make("ERROR_KEY")
    with pytest.raises(AuthError):
        p.get_prices()




# ------------------------------------- responses observed on the live endpoint
# Each of these bodies was returned by the real API on 2026-09-02. They are
# pinned verbatim so a regression in the parser shows up against reality rather
# than against another guess.
def test_live_getbalance():
    p, _ = make("ACCESS_BALANCE:0")
    assert p.get_balance().credit == INR(0)


def test_live_getprices_without_country_is_an_error_not_a_success():
    """BAD_COUNTRY has no colon; unlisted it would parse as a success status."""
    p, _ = make("BAD_COUNTRY")
    with pytest.raises(ProviderError, match="BAD_COUNTRY"):
        p.get_prices()


def test_live_getprices_without_operator_is_an_error_not_a_success():
    p, _ = make("BAD_OPERATOR")
    with pytest.raises(ProviderError, match="BAD_OPERATOR"):
        p.get_prices()


def test_live_bad_action_for_an_unknown_action():
    p, _ = make("BAD_ACTION")
    with pytest.raises(ProviderError, match="BAD_ACTION"):
        p.get_balance()


def test_live_bad_service_on_getnumber():
    """A lone BAD_SERVICE (the service the handler rejects) must now surface as a
    clean NumberUnavailable after the walk tries every operator (verified live:
    Instamart is BAD_SERVICE on operator 3 but a number on operator 4)."""
    p, _ = make("BAD_SERVICE", "BAD_SERVICE", "BAD_SERVICE", "BAD_SERVICE",
                operator_order=("1", "2"))
    with pytest.raises(NumberUnavailable):
        p.buy_number("whatsapp")


def test_bad_tokens_are_all_classified_as_errors():
    for token in ("BAD_COUNTRY", "BAD_OPERATOR", "BAD_SERVICE", "BAD_ACTION",
                  "BAD_STATUS", "BAD_KEY"):
        assert parse_response(token).is_error, token


def test_getprices_sends_country_and_operator():
    """The endpoint demands both; omitting either yields BAD_COUNTRY/BAD_OPERATOR."""
    p, opener = make('{"telegram": 10}')
    p.get_prices()
    url = opener.calls[0]["url"]
    assert "action=getPrices" in url
    assert "country=" in url
    assert "operator=" in url


def test_getprices_params_are_configurable():
    p, opener = make('{"telegram": 10}', prices_country="182", prices_operator="jiotel")
    p.get_prices()
    url = opener.calls[0]["url"]
    assert "country=182" in url and "operator=jiotel" in url


def test_service_map_translates_the_slug_for_getnumber():
    """The provider rejected 'whatsapp'; it uses its own vocabulary."""
    p, opener = make("ACCESS_NUMBER:1:+919")
    p.config = p.config.with_key("TESTKEY")
    p2 = UotpProvider(
        UotpConfig(api_key="TESTKEY", service_map={"whatsapp": "wa"}),
        opener=opener,
    )
    p2.buy_number("whatsapp")
    assert "service=wa" in opener.calls[0]["url"]


def test_unmapped_service_passes_through_unchanged():
    p, opener = make("ACCESS_NUMBER:1:+919", service_map={"whatsapp": "wa"})
    p.buy_number("telegram")
    assert "service=telegram" in opener.calls[0]["url"]


# --------------------------------------------------- live integration (opt-in)
@pytest.mark.skipif(
    os.environ.get("UOTP_LIVE_KEY") is None,
    reason="set UOTP_LIVE_KEY to run against the real endpoint",
)
def test_live_get_balance():  # pragma: no cover - requires network + key
    provider = UotpProvider(UotpConfig(api_key=os.environ["UOTP_LIVE_KEY"]))
    balance = provider.probe()
    assert balance.credit.paise >= 0


# --------------------------------------------------- live pins, 2026-09-02
# Bodies below are VERBATIM responses observed from the real endpoint with
# the funded account. They pin the vocabulary the bot runs against and catch
# a provider-side change the moment the live test runs.
def test_funded_wallet_body_is_rupees_not_paise():
    p, _ = make("ACCESS_BALANCE:10")
    assert p.get_balance().credit == INR(10)


def test_rotated_key_raises_auth_error():
    p, _ = make("BAD_KEY")
    with pytest.raises(AuthError):
        p.get_balance()


def test_country_and_operator_rejections_are_config_errors():
    for body in ("BAD_COUNTRY", "BAD_OPERATOR"):
        p, _ = make(body)
        with pytest.raises(ProviderError) as exc:
            p.get_prices()
        assert body in str(exc.value)


def test_prices_country_and_operator_are_actually_sent():
    p, opener = make("NO_CONNECTION")
    with pytest.raises(ServiceUnavailable):
        p.get_prices()
    assert "country=0" in opener.calls[0]["url"]
    assert "operator=2" in opener.calls[0]["url"]


def test_backend_error_on_price_fetch_is_service_unavailable():
    """NO_CONNECTION must not parse as a status -- observed live: the stub
    passes our params through to a dead database layer."""
    p, _ = make("NO_CONNECTION")
    with pytest.raises(ServiceUnavailable):
        p.get_prices()


def test_backend_error_during_sms_polling_fakes_no_code_and_kills_no_order():
    """ERROR_DATABASE from getStatus must not produce a fabricated code, and
    must not abandon a paid-for number: get_sms returns [] (poll again), and
    an outage that lasts the whole lease ends as timeout -> refund path."""
    p, _ = make("ERROR_DATABASE")
    assert p.get_sms("12345") == []


def test_total_backend_outage_times_out_cleanly():
    p, _ = make(*(["ERROR_DATABASE"] * 40))
    alloc = NumberAllocation(order_id="999", phone="+919876543210",
                             service="whatsapp", country="in", charged=INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=0.3, poll_interval=0.01)
    assert not result.success and result.code is None


def test_backend_error_on_buy_is_outcome_unknown_not_a_config_error():
    """The money-critical classification. NO_CONNECTION from getNumber means
    the charge may already have applied before the backend died. Surfacing it
    as PurchaseTimedOut makes the engine hold and reconcile; the engine must
    NOT buy a second number on this response."""
    p, _ = make("NO_CONNECTION")
    with pytest.raises(PurchaseTimedOut) as exc:
        p.buy_number("whatsapp")
    assert "reconcile" in str(exc.value).lower()


def test_error_sql_on_buy_also_holds_the_order():
    p, _ = make("ERROR_SQL")
    with pytest.raises(PurchaseTimedOut):
        p.buy_number("telegram")


def test_wait_for_otp_survives_a_backend_outage_mid_lease():
    """A paid-for number must never be abandoned because the provider's DB
    blipped: transient errors poll until the deadline, then cancel-shape."""
    p, _ = make(
        "ERROR_DATABASE",          # poll 1: backend down -> tolerated
        "STATUS_OK:482193",        # poll 2: code arrived
    )
    alloc = NumberAllocation(order_id="999", phone="+919876543210",
                             service="whatsapp", country="in", charged=INR(10))
    result = p.wait_for_otp(alloc, timeout_seconds=2.0, poll_interval=0.01)
    assert result.code == "482193"
    assert result.success


@pytest.mark.parametrize("token, exc", [
    # Bare (un-prefixed) forms the LIVE uotp.store handler uses on getNumber.
    ("NO_NUMBERS", NumberUnavailable),
    ("NO_BALANCE", InsufficientBalance),
    ("NO_FREE", NumberUnavailable),
    ("NO_MONEY", InsufficientBalance),
    # Provider-backend-infrastructure tokens must NOT be treated as success.
    ("NO_CONNECTION", ServiceUnavailable),
    ("ERROR_DATABASE", ServiceUnavailable),
    # Cancelling a fresh activation; must be ProviderError, not mis-parsed.
    ("EARLY_CANCEL_DENIED", ProviderError),
])
def test_live_tokens_map_to_exceptions(token, exc):
    """Tokens the real uotp.store handler returns (observed live 2026-09-03)
    must map to the right exception so the bot never parses a failure as a
    success status."""
    from uotpbot.provider.base import ServiceUnavailable
    p, _ = make(token)
    with pytest.raises(exc):
        p.get_balance()


def test_buy_number_walks_past_a_bad_service_operator():
    """A service that one operator answers BAD_SERVICE on must still be tried on
    the next operator (verified live: Instamart is BAD_SERVICE on op 3 but
    returns a number on op 4). The walk must not abort on a single operator's
    BAD_SERVICE."""
    bodies = ["BAD_SERVICE", "ACCESS_NUMBER:a1b2c3:917000000001:10"]
    p, opener = make(*bodies, operator_order=("1", "2"))
    alloc = p.buy_number("zznotavocab", "22")
    assert alloc.phone == "917000000001"
    assert alloc.order_id == "a1b2c3"
    # Two getNumber calls were made: op=1 (BAD_SERVICE) then op=2 (success).
    number_calls = [c["url"] for c in opener.calls if "getNumber" in c["url"]]
    assert len(number_calls) == 2
    assert "operator=1" in number_calls[0]
    assert "operator=2" in number_calls[1]


def test_buy_number_aborts_if_all_operators_bad_service():
    """If every operator answers BAD_SERVICE, the buy fails with a clean
    NumberUnavailable instead of an opaque BAD_SERVICE leak."""
    p, opener = make("BAD_SERVICE", "BAD_SERVICE", operator_order=("1", "2"))
    from uotpbot.provider.base import NumberUnavailable, ProviderError
    try:
        p.buy_number("zznotavocab", "22")
        raise AssertionError("should have raised")
    except NumberUnavailable:
        pass  # expected: all operators rejected the service
    except ProviderError as exc:
        raise AssertionError(f"unexpected ProviderError: {exc}")


def test_buy_number_walks_past_a_partial_harvest():
    """A partial/stale harvest must not hide a pool that actually carries stock.

    The walk unions the per-service harvested operator list with
    ``operator_order``; without that, a service whose harvested pools all
    answer BAD_SERVICE is declared out of stock even when a fallback pool
    (omitted from the harvest) has the number.
    """
    bodies = ["BAD_SERVICE", "ACCESS_NUMBER:aaa:917000000002:10"]
    p, opener = make(*bodies, operator_order=("2", "3"))
    # Simulate a service whose harvest lists only operator 1 (stale/partial),
    # while the real stock sits on operator 2.
    p._handler_map["bestsms"] = "bestSMS"
    p._handler_ops["bestsms"] = ["1"]
    alloc = p.buy_number("bestsms", "22")
    assert alloc.phone == "917000000002"
    number_calls = [c["url"] for c in opener.calls if "getNumber" in c["url"]]
    # Walk was op=1 (BAD_SERVICE) then op=2 (success) -- reached the fallback.
    assert len(number_calls) == 2
    assert "operator=1" in number_calls[0]
    assert "operator=2" in number_calls[1]
