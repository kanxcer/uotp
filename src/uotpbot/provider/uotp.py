"""UOTP HTTP adapter.

UOTP does not publish API documentation, so rather than guess an endpoint
shape and ship something that quietly breaks, this adapter takes three things
from configuration:

* **paths** -- where each operation lives,
* **auth**    -- header name and whether the key needs a ``Bearer`` prefix,
* **field map** -- which JSON keys hold balance, order id, phone, SMS body.

Set them in ``.env`` / ``config.yaml`` against your real account and the
adapter works without code edits. :meth:`UotpProvider.probe` hits the balance
endpoint once at startup and raises a clear error if the shape does not match,
so a wrong mapping fails loudly at boot instead of mid-order.

Only the standard library is used, so there is no dependency to drift.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from ..catalog import PROVIDER_VALIDITY_MINUTES
from ..money import INR, Money, quantize_money
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

__all__ = ["UotpConfig", "UotpProvider", "ResponseShape", "DEFAULT_SHAPE"]

JSON = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResponseShape:
    """Which JSON keys to read. Dotted paths supported (``data.balance``)."""

    balance: str = "balance"
    balance_currency: str = "currency"
    order_id: str = "id"
    phone: str = "number"
    charged: str = "price"
    sms_list: str = "messages"
    sms_text: str = "text"
    sms_sender: str = "sender"
    sms_time: str = "created_at"
    prices: str = "prices"
    #: Key the provider uses to signal failure, and the message beside it.
    error: str = "error"
    error_message: str = "message"
    status: str = "status"
    ok_value: str = "success"


DEFAULT_SHAPE = ResponseShape()


def _dig(payload: Any, dotted: str) -> Any:
    """Fetch ``a.b.c`` out of nested dicts, returning None if absent."""
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


@dataclass(frozen=True, slots=True)
class UotpConfig:
    """Connection settings. Nothing here is secret except ``api_key``."""

    base_url: str = "https://uotp.store"
    api_key: str = ""
    #: Header the key goes in, and whether it needs a scheme prefix.
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    balance_path: str = "/api/v1/balance"
    prices_path: str = "/api/v1/prices"
    buy_path: str = "/api/v1/number"
    sms_path: str = "/api/v1/sms"
    cancel_path: str = "/api/v1/cancel"
    timeout: float = 20.0
    #: Never retry a purchase more than this. Each retry can cost real money.
    max_retries: int = 2
    backoff: float = 1.5
    shape: ResponseShape = DEFAULT_SHAPE
    user_agent: str = "uotpbot/1.0"
    validity_minutes: int = PROVIDER_VALIDITY_MINUTES

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url is required")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")

    def with_key(self, api_key: str) -> "UotpConfig":
        return replace(self, api_key=api_key)


class UotpProvider:
    """Talks to UOTP over HTTP/JSON."""

    name = "uotp"

    def __init__(self, config: Optional[UotpConfig] = None, *, opener: Any = None) -> None:
        self.config = config or UotpConfig()
        # ``opener`` is injectable purely so tests can supply a fake transport
        # without monkeypatching urllib.
        self._opener = opener or urllib.request

    # -- transport -------------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        c = self.config
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": c.user_agent,
        }
        if c.api_key:
            value = f"{c.auth_scheme} {c.api_key}".strip() if c.auth_scheme else c.api_key
            headers[c.auth_header] = value
        return headers

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> JSON:
        url = self._url(path)
        if params:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {**self._headers(), **(extra_headers or {})}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        attempt = 0
        while True:
            try:
                with self._opener.urlopen(req, timeout=self.config.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return self._parse(raw, resp.status)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                raise self._map_http_error(exc.code, raw) from None
            except urllib.error.URLError as exc:
                # Network-level failure. Safe to retry for reads; for a
                # purchase the caller must reconcile via idempotency key.
                attempt += 1
                if attempt > self.config.max_retries or method == "POST":
                    raise PurchaseTimedOut(
                        f"{method} {path} failed after {attempt} attempt(s): {exc.reason}. "
                        "The request may have been processed -- reconcile before retrying."
                    ) from None
                time.sleep(self.config.backoff**attempt)
            except TimeoutError as exc:
                raise PurchaseTimedOut(f"{method} {path} timed out: {exc}") from None

    def _parse(self, raw: str, status: int) -> JSON:
        if not raw.strip():
            if 200 <= status < 300:
                return {}
            raise ProviderError(f"empty response body with HTTP {status}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"provider returned non-JSON (HTTP {status}): {raw[:200]!r}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderError(f"expected a JSON object, got {type(payload).__name__}")
        shape = self.config.shape
        err = _dig(payload, shape.error)
        if err not in (None, False, "", 0):
            message = str(_dig(payload, shape.error_message) or err)
            low = message.lower()
            if "balance" in low or "insufficient" in low or "funds" in low:
                raise InsufficientBalance(message)
            if "stock" in low or "no number" in low or "unavailable" in low:
                raise NumberUnavailable(message)
            if "auth" in low or "key" in low or "token" in low:
                raise AuthError(message)
            raise ProviderError(message)
        return dict(payload)

    def _map_http_error(self, code: int, raw: str) -> ProviderError:
        detail = ""
        try:
            payload = json.loads(raw)
            if isinstance(payload, Mapping):
                detail = str(
                    _dig(payload, self.config.shape.error_message)
                    or _dig(payload, self.config.shape.error)
                    or raw[:200]
                )
        except json.JSONDecodeError:
            detail = raw[:200]
        if code in (401, 403):
            return AuthError(f"HTTP {code}: {detail or 'credentials rejected'}")
        if code == 402:
            return InsufficientBalance(f"HTTP 402: {detail or 'insufficient balance'}")
        if code == 404:
            return ServiceUnavailable(f"HTTP 404: {detail or 'not found'}")
        if code in (408, 425, 504):
            return PurchaseTimedOut(f"HTTP {code}: {detail or 'gateway timeout'}")
        if code == 429:
            return ServiceUnavailable(f"HTTP 429: {detail or 'rate limited'}")
        if 500 <= code < 600:
            return ServiceUnavailable(f"HTTP {code}: {detail or 'server error'}")
        return ProviderError(f"HTTP {code}: {detail or 'request failed'}")

    # -- Provider implementation ----------------------------------------
    def get_balance(self) -> Balance:
        shape = self.config.shape
        payload = self._request(self.config.balance_path)
        raw = _dig(payload, shape.balance)
        if raw is None:
            raise ProviderError(
                f"balance response has no {shape.balance!r} key; keys were "
                f"{sorted(payload)}. Set shape.balance to the right path."
            )
        return Balance(
            credit=_as_money(raw),
            currency=str(_dig(payload, shape.balance_currency) or "INR"),
        )

    def get_prices(self) -> Mapping[str, Money]:
        shape = self.config.shape
        payload = self._request(self.config.prices_path)
        raw = _dig(payload, shape.prices)
        if raw is None:
            raise ProviderError(
                f"price response has no {shape.prices!r} key; keys were {sorted(payload)}"
            )
        out: dict[str, Money] = {}
        if isinstance(raw, Mapping):
            for slug, value in raw.items():
                price = value.get("price") if isinstance(value, Mapping) else value
                if price is None:
                    continue
                out[str(slug)] = _as_money(price)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                slug = item.get("service") or item.get("slug") or item.get("name")
                price = item.get("price")
                if slug is not None and price is not None:
                    out[str(slug)] = _as_money(price)
        return out

    def buy_number(
        self, service: str, country: str = "in", *, idempotency_key: Optional[str] = None
    ) -> NumberAllocation:
        """Allocate a number.

        ``idempotency_key`` is sent as ``Idempotency-Key``. If the provider
        honours it, a network retry after a timeout returns the *same* number
        instead of charging for a second one -- which is the difference between
        a retried order costing 1x and 2x.
        """
        shape = self.config.shape
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        payload = self._request(
            self.config.buy_path,
            method="POST",
            body={"service": service, "country": country},
            extra_headers=headers,
        )
        order_id = _dig(payload, shape.order_id)
        phone = _dig(payload, shape.phone)
        if order_id is None or phone is None:
            raise ProviderError(
                f"purchase response missing {shape.order_id!r}/{shape.phone!r}; "
                f"got {payload}. Adjust ResponseShape to match your API."
            )
        charged_raw = _dig(payload, shape.charged)
        return NumberAllocation(
            order_id=str(order_id),
            phone=str(phone),
            service=service,
            country=country,
            charged=_as_money(charged_raw) if charged_raw is not None else Money(0),
            validity_minutes=self.config.validity_minutes,
        )

    def get_sms(self, order_id: str) -> Sequence[SmsMessage]:
        shape = self.config.shape
        payload = self._request(self.config.sms_path, params={"id": order_id})
        raw = _dig(payload, shape.sms_list)
        if raw is None:
            # Some APIs return a bare list under a different key, or a single
            # message object. Handle both rather than failing a live order.
            if isinstance(payload.get("message"), Mapping):
                raw = [payload["message"]]
            else:
                return []
        if isinstance(raw, Mapping):
            raw = [raw]
        out: list[SmsMessage] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            text = _dig(item, shape.sms_text)
            if text is None:
                continue
            out.append(
                SmsMessage(
                    sender=str(_dig(item, shape.sms_sender) or ""),
                    text=str(text),
                    received_at=str(_dig(item, shape.sms_time) or _now()),
                )
            )
        return out

    def wait_for_otp(
        self,
        allocation: NumberAllocation,
        *,
        timeout_seconds: float = 300.0,
        poll_interval: float = 3.0,
        expect: Optional[str] = None,
    ) -> OtpResult:
        """Poll until an OTP appears or the number/timeout expires.

        ``expect`` optionally filters by sender, which matters when a number is
        reused and carries messages from a previous tenant.
        """
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = min(
            time.monotonic() + timeout_seconds,
            time.monotonic() + max(allocation.seconds_left(), 0.0),
        )
        attempts = 0
        started = time.monotonic()
        seen: set[str] = set()
        while True:
            attempts += 1
            try:
                messages = self.get_sms(allocation.order_id)
            except (ServiceUnavailable, ProviderError):
                # A transient poll failure must not abort a paid-for number.
                messages = []
            for msg in messages:
                key = f"{msg.sender}|{msg.text}"
                if key in seen:
                    continue
                seen.add(key)
                if expect and expect.lower() not in msg.sender.lower():
                    continue
                code = msg.extract_otp()
                if code:
                    return OtpResult(code, msg, attempts, time.monotonic() - started, False)
            if time.monotonic() >= deadline:
                return OtpResult(None, None, attempts, time.monotonic() - started, True)
            time.sleep(min(poll_interval, max(deadline - time.monotonic(), 0.1)))

    def cancel(self, order_id: str) -> Money:
        payload = self._request(
            self.config.cancel_path, method="POST", body={"id": order_id}
        )
        for key in ("refund", "refunded", "amount", "credit"):
            value = _dig(payload, key)
            if value is not None:
                return _as_money(value)
        return Money(0)

    # -- diagnostics -----------------------------------------------------
    def probe(self) -> Balance:
        """Verify credentials and response shape at startup. Fails loudly."""
        if not self.config.api_key:
            raise AuthError(
                "no API key configured; set UOTP_API_KEY. Refusing to run an "
                "unauthenticated bot that would silently fail on first order."
            )
        balance = self.get_balance()
        if balance.credit.is_negative:
            raise ProviderError(f"provider reported a negative balance: {balance.credit}")
        return balance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_money(value: Any) -> Money:
    """Coerce a provider's price/balance field into exact paise.

    Providers variously send rupees as a float, a string, or paise as an int.
    Guessing wrong here is a 100x error, so anything ambiguous raises rather
    than silently scaling.
    """
    if isinstance(value, Money):
        return value
    if isinstance(value, bool):
        raise ProviderError(f"cannot interpret boolean {value!r} as money")
    if isinstance(value, int):
        # A bare int is rupees (₹12), not paise -- but ₹1200 as paise is a
        # plausible-looking number too. Rupees is the overwhelmingly common
        # convention; callers whose API sends paise should send a string.
        return INR(value)
    if isinstance(value, float):
        return quantize_money(Decimal(repr(value)))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        # Indian providers variously prefix with Rs., Rs, INR or the rupee sign.
        for token in ("Rs.", "Rs", "INR", "\u20b9"):
            if text.startswith(token):
                text = text[len(token):]
                break
        text = text.strip()
        try:
            return quantize_money(Decimal(text))
        except (InvalidOperation, ValueError) as exc:
            raise ProviderError(f"cannot parse {value!r} as a money amount") from exc
    raise ProviderError(f"cannot interpret {type(value).__name__} as money")
