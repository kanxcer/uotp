"""UOTP adapter for the ``handler_api.php`` protocol.

Verified against the live endpoint on 2026-09-02. This is **not** a JSON API.
It is the SMS-Activate-family protocol:

* every call is a **GET** with the action and key in the query string;
* responses are **plain text**, ``PREFIX:field:field``, one line, no JSON.

Confirmed by a real request::

    GET .../handler_api.php?action=getBalance&api_key=...
    -> ACCESS_BALANCE:0

Because a response body like ``ACCESS_NUMBER:12345:+919876543210`` carries
colons *inside* the payload, parsing splits on the first colon only and then
tokenises the remainder -- a naive ``split(":")`` into three fields would
mangle any value that itself contains a colon (a UPI VPA, a URL, a code with
a separator).

Only ``getBalance`` is documented by the provider. The remaining actions
follow the conventions of this protocol family and are therefore **inferred**;
every action name and response prefix is configurable so a divergence is a
config change, not a code change. :meth:`UotpProvider.probe` verifies the key
and the balance prefix at startup.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from ..catalog import PROVIDER_VALIDITY_MINUTES
from ..money import Money, quantize_money
from .base import (
    AuthError,
    Balance,
    InsufficientBalance,
    NumberAllocation,
    NumberUnavailable,
    OtpResult,
    ProviderError,
    PurchaseTimedOut,
    ServiceUnavailable,
    SmsMessage,
)

__all__ = [
    "UotpConfig",
    "UotpProvider",
    "ApiResponse",
    "parse_response",
    "ERROR_TOKENS",
]

JSON = dict[str, Any]


# --------------------------------------------------------------- responses
@dataclass(frozen=True, slots=True)
class ApiResponse:
    """A parsed ``PREFIX:a:b:c`` line.

    ``status`` is the token before the first colon. ``fields`` are the
    colon-separated tokens after it, in order. ``raw`` is kept for error
    messages, because this protocol signals failure in the same shape as
    success and the original text is often the only useful diagnostic.
    """

    status: str
    fields: tuple[str, ...]
    raw: str

    @property
    def is_error(self) -> bool:
        return self.status in ERROR_TOKENS

    def field(self, index: int, default: str = "") -> str:
        """The ``index``-th payload field, or ``default`` if absent."""
        return self.fields[index] if 0 <= index < len(self.fields) else default


def parse_response(text: str) -> ApiResponse:
    """Parse one protocol line.

    Splits on the *first* colon only, so payloads containing colons survive.
    A body with no colon is a bare status token (``STATUS_WAIT_CODE``), which
    is the common case for polling.
    """
    body = (text or "").strip()
    # Some deployments wrap the line in HTML or trailing whitespace; take the
    # first non-empty line and strip stray markup.
    for line in body.splitlines():
        candidate = line.strip().strip("<>").strip()
        if candidate:
            body = candidate
            break
    if not body:
        raise ProviderError("provider returned an empty response")
    if ":" in body:
        status, _, rest = body.partition(":")
        return ApiResponse(status.strip(), tuple(rest.split(":")), body)
    return ApiResponse(body, (), body)


#: Failure tokens in this protocol family, mapped to what they mean here.
ERROR_TOKENS: frozenset[str] = frozenset({
    "ERROR_SQL", "ERROR_KEY", "BAD_KEY", "BAD_ACTION", "BAD_STATUS",
    "BAD_SERVICE", "ERROR_NO_NUMBERS", "ERROR_NO_BALANCE", "NO_ACTIVATION",
    "ERROR_EMPTY_ACCOUNT", "ERROR_WRONG_ACTION", "ERROR_NO_FREE",
    "ACCOUNT_INACTIVE", "ERROR_IP", "ERROR_NO_SERVICE",
    # Bare (un-prefixed) variants the live uotp.store handler actually returns,
    # verified 2026-09-03: getNumber answers NO_NUMBERS (that operator pool is
    # empty) and NO_BALANCE (wallet can't cover that pool), with NO "ERROR_"
    # prefix. Without these listed they parse as a *success* status, the
    # operator walk stops at the first empty pool, and the purchase explodes
    # with "expected ACCESS_NUMBER but got NO_NUMBERS" instead of moving to the
    # next operator or reporting a clean no-stock/no-funds. A one-tap buy was
    # failing on bigbasket exactly because of this.
    "NO_NUMBERS", "NO_NUMBER", "NO_FREE", "NO_BALANCE", "NO_MONEY",
    "ERROR_SERVICE", "WRONG_SERVICE", "ERROR_NO_ACTIVATION",
    # Observed live on 2026-09-02: getPrices without a country returns
    # BAD_COUNTRY, and with one but no operator returns BAD_OPERATOR. Both
    # have no colon, so an unlisted token would parse as a *success* status
    # and be mis-handled rather than raising.
    "BAD_COUNTRY", "BAD_OPERATOR",
    # Observed live 2026-09-02: once parameters validate, the provider's own
    # backend answers NO_CONNECTION (getPrices) and ERROR_DATABASE
    # (getStatus). Bare tokens again -- listed or they would parse as a
    # success status and be polled as "no OTP yet" instead of raising.
    "NO_CONNECTION", "ERROR_DATABASE",
    # Observed live 2026-09-03: cancelling a fresh activation is refused --
    # the number must sit out its OTP window before the provider will release
    # or refund it. Not a stock problem; a customer asking to cancel should see
    # a clear message, not a confusing success-status parse.
    "EARLY_CANCEL_DENIED", "CANCEL_DENIED", "PRICE_CHANGED", "PROVIDER_2FA",
})

#: Which exception each error token becomes.
_FATAL_AUTH = frozenset({"ERROR_KEY", "BAD_KEY", "ERROR_IP", "ACCOUNT_INACTIVE"})
_NO_STOCK = frozenset({"ERROR_NO_NUMBERS", "ERROR_NO_FREE", "ERROR_NO_SERVICE",
                       "NO_NUMBERS", "NO_NUMBER", "NO_FREE", "ERROR_SERVICE",
                       "WRONG_SERVICE"})
_NO_FUNDS = frozenset({"ERROR_NO_BALANCE", "ERROR_EMPTY_ACCOUNT",
                       "NO_BALANCE", "NO_MONEY"})
_BAD_REQUEST = frozenset({"BAD_ACTION", "BAD_STATUS", "BAD_SERVICE",
                          "NO_ACTIVATION", "ERROR_NO_ACTIVATION",
                          "ERROR_WRONG_ACTION", "BAD_COUNTRY", "BAD_OPERATOR"})
#: Provider-side infrastructure failures (observed live on the stub API).
#: On reads these are transient and safe to retry. On a purchase the money
#: may already have moved before the backend died, so the buy path must
#: treat them as outcome-unknown and reconcile -- never auto-retry.
_INFRA = frozenset({"NO_CONNECTION", "ERROR_DATABASE", "ERROR_SQL"})


# ------------------------------------------------------------------ config
@dataclass(frozen=True, slots=True)
class UotpConfig:
    """Endpoint, credentials and the protocol's vocabulary.

    ``balance_divisor`` exists because this family of API is not consistent
    about units: some deployments report rupees, others paise. UOTP's public
    pricing is in rupees, so rupees is the default -- but if ``getBalance``
    returns ``100000`` where the dashboard shows Rs.1000, set it to ``100``
    rather than editing code. Getting this wrong is a 100x error, which is why
    it is a named setting and not a guess buried in a function.
    """

    base_url: str = "https://uotp.store/api/stubs/handler_api.php"
    api_key: str = ""
    #: The key travels as a query parameter in this protocol, not a header.
    key_param: str = "api_key"
    action_param: str = "action"
    action_balance: str = "getBalance"
    action_prices: str = "getPrices"
    action_get_number: str = "getNumber"
    action_get_status: str = "getStatus"
    action_set_status: str = "setStatus"
    action_active: str = "getActiveActivations"
    #: Response prefixes.
    balance_prefix: str = "ACCESS_BALANCE"
    number_prefix: str = "ACCESS_NUMBER"
    #: getNumber uses this prefix when it hands back a number that is already
    #: registered on the target service -- you paid, it will not work, and it
    #: must be treated as a burned attempt rather than a usable number.
    cancel_prefix: str = "ACCESS_CANCEL"
    ok_prefix: str = "STATUS_OK"
    wait_prefix: str = "STATUS_WAIT_CODE"
    resend_prefix: str = "STATUS_WAIT_RESEND"
    canceled_prefix: str = "STATUS_CANCEL"
    #: setStatus codes: 1 ready, 3 request another SMS, 6 complete, 8 cancel.
    status_complete: str = "6"
    status_cancel: str = "8"
    #: setStatus code that asks the provider to resend the SMS. The
    #: SMS-activate-family convention is 3 ("request another"), and the live
    #: handler accepts it (it reached the backend, which was down on our probe);
    #: statuses 6/4 were BAD_STATUS by contrast. Configurable in case the
    #: provider names it differently.
    status_resend: str = "3"
    #: getPrices was observed to require both of these. Their meaning is
    #: provider-specific and undocumented, so both are settings.
    #: Tuned against the live endpoint on 2026-09-02: country 0 and 1 pass
    #: validation (999 returns BAD_COUNTRY); operators "any"/"all"/"0"/"1"
    #: return BAD_OPERATOR while 2 and 3 get through to the backend (which
    #: was down, so operator ids' meaning is still unconfirmed).
    prices_country: str = "0"
    prices_operator: str = "2"
    #: Maps this bot's service slug to the provider's. Union stock table and
    #: operator preferences land in ``handler_vocab.json`` (regenerate with
    #: scripts/refresh_handler_vocab.py); an explicit map overrides it.
    service_map: Mapping[str, str] = field(default_factory=dict)
    #: getNumber requires an ``operator`` (validated live: ids 2..10 pass,
    #: names/any/0/1 are BAD_OPERATOR). ``operator`` forces one id; otherwise
    #: the provider walks ``operator_order``, usually the harvested
    #: cheapest-first list for that service.
    operator: str = ""
    operator_order: Sequence[str] = ("3", "4", "5", "7", "2", "8")
    balance_divisor: Decimal = Decimal(1)
    timeout: float = 30.0
    max_retries: int = 2
    backoff: float = 1.5
    validity_minutes: int = PROVIDER_VALIDITY_MINUTES
    user_agent: str = "uotpbot/1.1"
    #: Query parameter carrying the SIM-bank server id on getNumber.
    server_param: str = "server"
    #: Extra fixed query params, e.g. a version or a partner id.
    extra_params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url is required")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.balance_divisor <= 0:
            raise ValueError(f"balance_divisor must be positive, got {self.balance_divisor}")

    def with_key(self, api_key: str) -> "UotpConfig":
        return replace(self, api_key=api_key)


# ---------------------------------------------------------------- provider
class UotpProvider:
    """Talks to UOTP's ``handler_api.php`` endpoint."""

    name = "uotp"

    def __init__(self, config: Optional[UotpConfig] = None, *, opener: Any = None) -> None:
        self.config = config or UotpConfig()
        # Injectable so tests can supply a fake transport without touching
        # urllib or the network.
        self._opener = opener or urllib.request
        # Vocabulary dump harvested from the live getPrices endpoint: which
        # handler codes exist (cats pre-validate getNumber with BAD_SERVICE)
        # and which operator ids carry stock, cheapest first. Lives in the
        # package so the bot boots with zero extra network calls.
        self._handler_map: dict[str, str] = {}
        self._handler_ops: dict[str, list[str]] = {}
        try:
            import json as _json
            from pathlib import Path as _Path
            vocab = _json.loads((_Path(__file__).resolve().parent.parent
                                 / "data" / "handler_vocab.json").read_text())
            self._handler_map = {k.lower(): v for k, v in vocab.get("map", {}).items()}
            self._handler_ops = {
                k.lower(): [str(o) for o in v] for k, v in vocab.get("op_order", {}).items()
            }
        except Exception:  # pragma: no cover - defensive: map-less = try raw slugs
            pass
        for k, v in self.config.service_map.items():
            self._handler_map[str(k).lower()] = str(v)

    def can_serve(self, service: str) -> bool:
        """Whether the fulfilment rail knows this service at all.

        The site's 1047 product codes include ~250 that handler_api rejects
        with BAD_SERVICE (unavailable upstream). Listing them as buyable
        would sell guaranteed-instant-refund orders, so the picker consults
        this. Map-less (offline/first run) => optimistically True.
        """
        if not self._handler_map:
            return True
        return self._resolve(service) is not None

    def _resolve(self, service: str) -> Optional[str]:
        key = service.lower()
        code = self._handler_map.get(key)
        if code is not None:
            return code
        return service if service in self._handler_ops else None

    # -- transport -------------------------------------------------------
    def _call(self, action: str, **params: Any) -> str:
        """Issue one GET and return the raw response text."""
        c = self.config
        query: dict[str, str] = {c.action_param: action, c.key_param: c.api_key}
        query.update({k: str(v) for k, v in c.extra_params.items()})
        query.update(
            {k: str(v) for k, v in params.items() if v is not None}
        )
        url = f"{c.base_url}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": c.user_agent, "Accept": "text/plain"}, method="GET"
        )
        attempt = 0
        while True:
            try:
                with self._opener.urlopen(req, timeout=c.timeout) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                raise self._map_http_error(exc.code, body) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                attempt += 1
                reason = getattr(exc, "reason", exc)
                if attempt > c.max_retries:
                    # For getNumber this is the dangerous case: the number may
                    # already be allocated and charged. Callers must reconcile
                    # rather than retry blindly.
                    raise PurchaseTimedOut(
                        f"action={action} failed after {attempt} attempt(s): {reason}. "
                        "The request may have been processed -- reconcile before retrying."
                    ) from None
                time.sleep(c.backoff**attempt)

    def _mapped_http_error(self, code: int, body: str) -> ProviderError:
        detail = body.strip()[:200] or "no body"
        if code in (401, 403):
            return AuthError(f"HTTP {code}: {detail}")
        if code == 402:
            return InsufficientBalance(f"HTTP 402: {detail}")
        if code in (408, 425, 504):
            return PurchaseTimedOut(f"HTTP {code}: {detail}")
        if code == 429:
            return ServiceUnavailable(f"HTTP 429: {detail}")
        if 500 <= code < 600:
            return ServiceUnavailable(f"HTTP {code}: {detail}")
        return ProviderError(f"HTTP {code}: {detail}")

    _map_http_error = _mapped_http_error

    def _request(self, action: str, **params: Any) -> ApiResponse:
        """Call an action and translate protocol errors into exceptions."""
        parsed = parse_response(self._call(action, **params))
        if parsed.is_error:
            raise self._error_for(parsed, action)
        return parsed

    def _error_for(self, parsed: ApiResponse, action: str) -> ProviderError:
        token = parsed.status
        detail = ":".join(parsed.fields) or token
        message = f"provider returned {token} for action={action}" + (
            f" ({detail})" if detail != token else ""
        )
        if token in _INFRA:
            if action == self.config.action_get_number:
                # The dangerous shape: the charge may have applied before the
                # backend died. Surface as ambiguous so the engine holds the
                # order for reconciliation instead of buying a second number.
                return PurchaseTimedOut(
                    message
                    + " -- the charge may still have been applied; reconcile "
                      "wallet balance and active orders before any retry"
                )
            return ServiceUnavailable(message + " -- provider backend issue, retry-safe")
        if token in _FATAL_AUTH:
            return AuthError(message)
        if token in _NO_FUNDS:
            return InsufficientBalance(message)
        if token in _NO_STOCK:
            return NumberUnavailable(message)
        if token in _BAD_REQUEST:
            # NO_ACTIVATION and friends are "that order id is gone", which the
            # caller should see as a provider error, not a stock problem.
            return ProviderError(message)
        return ProviderError(message)

    # -- Provider implementation ----------------------------------------
    def get_balance(self) -> Balance:
        """Wallet balance, from ``ACCESS_BALANCE:<amount>``."""
        c = self.config
        parsed = self._request(c.action_balance)
        if parsed.status != c.balance_prefix:
            raise ProviderError(
                f"expected {c.balance_prefix!r} but got {parsed.status!r} "
                f"(raw: {parsed.raw!r}). Set balance_prefix to match."
            )
        return Balance(credit=self._to_money(parsed.field(0), raw=parsed.raw))

    def _to_money(self, text: str, *, raw: str = "") -> Money:
        """Parse a protocol amount, applying ``balance_divisor``."""
        token = text.strip()
        if not token:
            raise ProviderError(f"no amount in response {raw!r}")
        try:
            value = Decimal(token)
        except (InvalidOperation, ValueError) as exc:
            raise ProviderError(f"cannot parse {token!r} as an amount (raw {raw!r})") from exc
        if not value.is_finite():
            raise ProviderError(f"amount {token!r} is not finite (raw {raw!r})")
        return quantize_money(value / self.config.balance_divisor)

    def get_prices(self) -> Mapping[str, Money]:
        """Live prices. ``getPrices`` in this family returns JSON, not colon text.

        The live endpoint rejects a bare call with ``BAD_COUNTRY`` and one with
        only a country with ``BAD_OPERATOR``, so both are sent. Handles both
        response shapes: a JSON object keyed by service, and a
        ``{service: {country: price}}`` nest. Anything unparseable is skipped
        rather than raising, so one odd entry cannot blind the whole book.
        """
        c = self.config
        text = self._call(
            c.action_prices, country=c.prices_country, operator=c.prices_operator
        )
        stripped = text.lstrip()
        # Only attempt JSON when the body actually looks like JSON. Anything
        # else is colon-text -- which includes bare error tokens like
        # ERROR_KEY that contain no colon at all.
        if not stripped.startswith(("{", "[")):
            parsed = parse_response(text)
            if parsed.is_error:
                raise self._error_for(parsed, self.config.action_prices)
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise ProviderError(f"getPrices returned invalid JSON: {text[:200]!r}")
        out: dict[str, Money] = {}
        if isinstance(payload, Mapping):
            for service, value in payload.items():
                price = self._extract_price(value)
                if price is not None:
                    out[str(service)] = price
        return out

    @staticmethod
    def _extract_price(value: Any) -> Optional[Money]:
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                return quantize_money(Decimal(str(value)))
            except (InvalidOperation, ValueError):
                return None
        if isinstance(value, Mapping):
            for key in ("price", "cost", "all"):
                if key in value:
                    nested = value[key]
                    if isinstance(nested, Mapping):
                        costs = [v for v in nested.values() if isinstance(v, (int, float))]
                        return quantize_money(Decimal(str(min(costs)))) if costs else None
                    return UotpProvider._extract_price(nested)
        return None

    # handler_api accepts numeric country ids only ("22" is India); the
    # engine's generic default used to send "in" and eat BAD_COUNTRY.
    _COUNTRY_ALIASES = {"in": "22", "india": "22", "ind": "22"}

    def buy_number(
        self, service: str, country: str = "22", *,
        idempotency_key: Optional[str] = None, server: str = "",
    ) -> NumberAllocation:
        """Allocate a number via ``getNumber``.

        Two distinct outcomes are distinguished because they are economically
        opposite:

        ``ACCESS_NUMBER:<id>:<phone>``  a usable number
        ``ACCESS_CANCEL:<id>:<phone>``  charged, but already registered on the
                                       target service -- burned, no OTP will
                                       ever arrive

        Treating the second as a usable number would burn the full 20-minute
        window waiting for an OTP that cannot come.

        ``idempotency_key`` is not part of this protocol, so it is carried as
        the ``order`` parameter where the provider supports it. Where it does
        not, an ambiguous timeout is surfaced as :class:`PurchaseTimedOut` so
        the caller reconciles instead of buying a second number.
        """
        c = self.config
        # Translate through the harvested handler vocabulary first
        # ("instagram" -> "Instagram"), then the caller's explicit map,
        # then the raw slug -- validation will still BAD_SERVICE unknowns.
        slug = self._resolve(service) or service
        country = self._COUNTRY_ALIASES.get(str(country).lower(), country)
        params: dict[str, Any] = {"service": slug, "country": country}
        if server:
            # Stock and price live per SIM-bank server on uotp.store. The
            # handler currently ignores it (server number comes from the
            # operator's pool) -- harmless to send, cheap to verify later.
            params[c.server_param] = server
        if idempotency_key:
            params["order"] = idempotency_key

        # ``operator`` is REQUIRED (BAD_OPERATOR without it, verified live).
        # Operator ids are stock pools; a "no stock" answer on one says
        # nothing about the others, so one tap may try several. Ordered by
        # the harvested per-service price table, cheapest first; an explicit
        # config override is tried ahead of the list. CRITICAL: the walk
        # stops on PurchaseTimedOut -- a timed-out request may already hold
        # the charge, and trying the next operator could buy two numbers.
        ops = list(self._handler_ops.get(slug.lower(), []))
        if c.operator:
            ops = [c.operator] + [o for o in ops if o != c.operator]
        if not ops:
            ops = list(c.operator_order)
        if c.operator and c.operator not in ops:
            ops.insert(0, c.operator)

        last: Optional[ProviderError] = None
        for op in dict.fromkeys(ops):
            try:
                parsed = self._request(c.action_get_number, operator=op, **params)
            except PurchaseTimedOut:
                raise  # maybe charged -- engine reconciles, never blind-retries
            except (NumberUnavailable, InsufficientBalance) as exc:
                last = exc
                continue
            except ProviderError as exc:
                # BAD_OPERATOR: this id does not exist here; try the next.
                if "BAD_OPERATOR" in str(exc):
                    last = exc
                    continue
                raise
            break
        else:
            raise last or NumberUnavailable(f"no stock for {service}")

        if parsed.status == c.cancel_prefix:
            # Charged and immediately useless. Record it as an allocation so
            # the charge is booked and the refund path can run.
            alloc = self._allocation(parsed, service, country)
            alloc = replace(alloc, order_id=f"{alloc.order_id}|burned")
            raise NumberUnavailable(
                f"number {alloc.phone} was already registered on {service}; "
                f"charged and unusable (id={alloc.order_id})"
            )
        if parsed.status != c.number_prefix:
            raise ProviderError(
                f"expected {c.number_prefix!r} but got {parsed.status!r} "
                f"(raw: {parsed.raw!r}). Set number_prefix to match."
            )
        alloc = self._allocation(parsed, service, country)
        alloc = replace(alloc, charged=alloc.charged, order_id=alloc.order_id)
        return alloc

    def _allocation(
        self, parsed: ApiResponse, service: str, country: str
    ) -> NumberAllocation:
        order_id = parsed.field(0)
        phone = parsed.field(1)
        if not order_id:
            raise ProviderError(f"no activation id in {parsed.raw!r}")
        if not phone:
            raise ProviderError(f"no phone number in {parsed.raw!r}")
        charged = Money(0)
        # Some deployments append the price; if present, use it.
        if parsed.field(2):
            try:
                charged = self._to_money(parsed.field(2), raw=parsed.raw)
            except ProviderError:
                charged = Money(0)
        return NumberAllocation(
            order_id=order_id,
            phone=phone,
            service=service,
            country=country,
            charged=charged,
            validity_minutes=self.config.validity_minutes,
        )

    def get_sms(self, order_id: str) -> Sequence[SmsMessage]:
        """Return the OTP if one has arrived, else nothing.

        This protocol has no SMS inbox: ``getStatus`` returns ``STATUS_OK:<code>``
        once a code has landed. The code is wrapped into a :class:`SmsMessage`
        so the rest of the bot is agnostic.
        """
        c = self.config
        activation = order_id.split("|")[0]  # strip the burned marker
        try:
            parsed = self._request(c.action_get_status, id=activation)
        except ProviderError:
            return []
        if parsed.status == c.ok_prefix:
            code = parsed.field(0)
            if code:
                return [
                    SmsMessage(
                        sender="",
                        text=code,
                        received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                ]
        return []

    def wait_for_otp(
        self,
        allocation: NumberAllocation,
        *,
        timeout_seconds: float = 290.0,
        poll_interval: float = 3.0,
        expect: Optional[str] = None,
    ) -> OtpResult:
        """Poll ``getStatus`` until a code arrives, the number is canceled, or
        the lease expires.

        Returns early on ``STATUS_CANCEL`` -- the number is dead and continuing
        to poll would waste the remaining window while forfeiting the refund.
        """
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        c = self.config
        activation = allocation.order_id.split("|")[0]
        budget = min(timeout_seconds, max(allocation.seconds_left(), 0.0))
        deadline = time.monotonic() + budget
        attempts = 0
        started = time.monotonic()
        while True:
            attempts += 1
            try:
                parsed = self._request(c.action_get_status, id=activation)
            except ProviderError:
                # A transient poll failure must not abandon a paid-for number.
                parsed = None
            if parsed is not None:
                if parsed.status == c.ok_prefix and parsed.field(0):
                    code = parsed.field(0)
                    if not expect or expect.lower() in code.lower():
                        return OtpResult(
                            code,
                            SmsMessage("", code, datetime.now(timezone.utc).isoformat()),
                            attempts,
                            time.monotonic() - started,
                            False,
                        )
                if parsed.status == c.canceled_prefix:
                    return OtpResult(None, None, attempts, time.monotonic() - started, True)
            if time.monotonic() >= deadline:
                return OtpResult(None, None, attempts, time.monotonic() - started, True)
            time.sleep(min(poll_interval, max(deadline - time.monotonic(), 0.1)))

    def cancel_strict(self, order_id: str) -> Money:
        """Release an activation and propagate failure to the caller.

        Normal cleanup is best-effort, but rollback after a database failure
        must know whether the live activation was actually released.
        """
        c = self.config
        activation = order_id.split("|")[0]
        self._request(c.action_set_status, id=activation, status=c.status_cancel)
        # The protocol does not report the refund amount. Reconcile the
        # provider wallet separately instead of inventing a number.
        return Money(0)

    def cancel(self, order_id: str) -> Money:
        """Release a number; best-effort for ordinary timeout cleanup."""
        try:
            return self.cancel_strict(order_id)
        except ProviderError:
            return Money(0)

    def complete(self, order_id: str) -> None:
        """Mark an activation finished so the number is freed."""
        c = self.config
        try:
            self._request(
                c.action_set_status, id=order_id.split("|")[0], status=c.status_complete
            )
        except ProviderError:
            pass

    def resend(self, order_id: str) -> bool:
        """Ask the provider to resend the SMS (setStatus=3, the protocol-family
        'request another/code' code). Best-effort: returns True if the provider
        accepted it (i.e. no auth/parameter error), False on any failure.
        """
        c = self.config
        try:
            self._request(
                c.action_set_status, id=order_id.split("|")[0], status=c.status_resend
            )
            return True
        except ProviderError:
            return False

    def get_active(self) -> Sequence[Mapping[str, str]]:
        """Open activations, for reconciling after a crash."""
        try:
            parsed = self._request(self.config.action_active)
        except ProviderError:
            return []
        return [{"raw": parsed.raw, "status": parsed.status, "fields": list(parsed.fields)}]

    # -- diagnostics -----------------------------------------------------
    def probe(self) -> Balance:
        """Verify the key and response shape at startup. Fails loudly."""
        if not self.config.api_key:
            raise AuthError(
                "no API key configured; set UOTP_API_KEY. Refusing to start a "
                "bot that would fail on its first order."
            )
        balance = self.get_balance()
        if balance.credit.is_negative:
            raise ProviderError(f"provider reported a negative balance: {balance.credit}")
        return balance
