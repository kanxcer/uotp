"""FamGateway — automated UPI payment verification.

FamGateway turns a FamPay inbox into a payment API: create an order, show
the customer its UPI QR / deep-link, and get confirmed the moment the money
lands (FamGateway reads the FamPay email and verifies the UTR). This module
wraps the three calls the bot needs:

``create_order``
    ``GET /api/qr.php?api_key=...&amount=...`` -> an ``order_id``, the exact
    ``payable_amount`` (which carries a per-order fractional offset so two
    customers paying the same round amount can still be told apart), and the
    ``qr_url`` / ``checkout_url`` / ``upi_intent`` to show the customer.

``verify``
    ``GET /api/verify-order.php?api_key=...&order_id=...`` -> paid "success"
    with the bank ``utr`` and ``sender_name`` once FamGateway sees the money,
    else ``pending``/``expired``. Callers poll this every few seconds (the
    gateway locks an IP that spams faster than ~5 req/s).

``verify_webhook``
    FamGateway also POSTs a signed JSON to a webhook URL. ``X-FamGateway-
    Signature`` is HMAC-SHA256 of the raw body using the api_key. We verify it
    with a constant-time compare so a forged callback (or a non-FamGateway
    caller) can never credit a balance.

Deliberately transport-agnostic and dependency-free: it uses ``urllib`` like
the rest of the codebase, and takes an injectable ``opener`` so the whole set
is unit-testable offline with a stub.

Credentials are never logged or echoed. Every call that touches the network
takes a timeout so a flaky gateway can never hang the bot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union

log = logging.getLogger("uotpbot.gateway")

__all__ = [
    "FamGatewayError",
    "FamGatewayOrder",
    "FamGatewayStatus",
    "FamGateway",
    "verify_webhook_signature",
]

DEFAULT_BASE_URL = "https://famgateway.in"
#: How long a single gateway request may take before we give up.
DEFAULT_TIMEOUT = 12.0


class FamGatewayError(Exception):
    """A gateway call failed (network, auth, rate limit, or an order error)."""

    def __init__(self, message: str, *, code: Optional[int] = None,
                 raw: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


@dataclass(slots=True)
class FamGatewayOrder:
    """A created payment order, as the customer should see it."""

    order_id: str
    amount: Decimal          #: the round amount we asked for
    payable_amount: Decimal  #: the EXACT amount the customer must pay
    upi_id: str = ""
    qr_url: str = ""         #: a QR image the customer can scan (send as photo)
    checkout_url: str = ""   #: hosted mobile-optimised checkout page
    upi_intent: str = ""     #: upi:// deep link
    expires_at_ist: str = ""


@dataclass(slots=True)
class FamGatewayStatus:
    """Result of verifying an order."""

    state: str               #: "success" | "pending" | "expired"
    order_id: str = ""
    utr: str = ""            #: the bank UTR (present only when paid)
    sender_name: str = ""    #: verified payer name (only when paid)
    amount: Optional[Decimal] = None

    @property
    def is_paid(self) -> bool:
        return self.state == "success"


def verify_webhook_signature(raw_body: bytes, signature: Optional[str],
                             api_key: str) -> bool:
    """Constant-time check of a webhook ``X-FamGateway-Signature``.

    The signature is HMAC-SHA256 of the raw request body keyed with the API
    key. ``hmac.compare_digest`` is deliberately used so a timing attack on
    the comparison is not feasible.
    """
    if not signature:
        return False
    expected = hmac.new(
        api_key.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    try:
        return hmac.compare_digest(signature.encode("utf-8"),
                                   expected.encode("utf-8"))
    except TypeError:  # non-ASCII signature
        return False


class FamGateway:
    """A small client for the FamGateway REST API.

    ``opener`` may be injected (tests pass a stub) so nothing here touches
    the network unless a real call is made.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Optional[Union[object, urllib.request]] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request

    # -- plumbing --------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        """Raw GET, returning the parsed body (status field left as-is).

        Some endpoints (``verify-order``) legitimately answer ``status`` of
        ``pending``/``expired``; only callers that require a *successful*
        creation must enforce it, so the success check lives there.
        """
        query = {k: v for k, v in params.items()
                 if v is not None and v != ""}
        query["api_key"] = self.api_key
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, method="GET")
        try:
            with self._opener.urlopen(req, timeout=self.timeout) as resp:  # type: ignore[attr-defined]
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            # famgateway signals domain errors via HTTP codes (401 auth, 403
            # suspended, 404 unknown order, 408 expired, 429 rate limit, 500).
            hint = {
                401: "invalid api_key",
                403: "account suspended",
                404: "order not found",
                408: "order expired",
                429: "rate limited",
            }.get(exc.code, "gateway error")
            raise FamGatewayError(f"{hint} (HTTP {exc.code})",
                                  code=exc.code, raw=body[:200])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FamGatewayError(f"could not reach FamGateway: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FamGatewayError(f"bad JSON from gateway: {exc}",
                                  raw=raw[:200]) from exc
        if not isinstance(data, dict):
            raise FamGatewayError("unexpected gateway response",
                                  raw=str(data)[:200])
        return data

    # -- public API ------------------------------------------------------
    def create_order(
        self,
        amount: Union[int, float, Decimal],
        *,
        customer_name: str = "",
        customer_phone: str = "",
        redirect_url: str = "",
        webhook_url: str = "",
    ) -> FamGatewayOrder:
        """Create a UPI payment order for ``amount`` rupees.

        Returns the order with the exact ``payable_amount`` the customer must
        be shown (never the round ``amount``: the gateway adds a fractional
        offset to make each order uniquely identifiable).
        """
        data = self._get("/api/qr.php", {
            "amount": str(amount),
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "redirect_url": redirect_url,
            "webhook_url": webhook_url,
        })
        if data.get("status") != "success":
            raise FamGatewayError(f"could not create order: {data.get('status')}",
                                  raw=json.dumps(data)[:200])
        d = data.get("data", {})
        try:
            payable = Decimal(str(d.get("payable_amount", d.get("amount", amount))))
            amount_dec = Decimal(str(d.get("amount", amount)))
        except Exception:  # noqa: BLE001 - a bad field must not crash the order
            payable = Decimal(str(amount))
            amount_dec = Decimal(str(amount))
        return FamGatewayOrder(
            order_id=d.get("order_id", ""),
            amount=amount_dec,
            payable_amount=payable,
            upi_id=d.get("upi_id", ""),
            qr_url=d.get("qr_url", ""),
            checkout_url=d.get("checkout_url", ""),
            upi_intent=d.get("upi_intent", ""),
            expires_at_ist=d.get("expires_at_ist", ""),
        )

    def verify(self, order_id: str) -> FamGatewayStatus:
        """Check whether ``order_id`` has been paid.

        Polling guidance: 3-5 seconds between calls; the gateway rate-limits
        anything faster. Returns a ``FamGatewayStatus``; check ``is_paid``.
        """
        data = self._get("/api/verify-order.php", {"order_id": order_id})
        d = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        state = str(data.get("status", "")).lower()
        # "success" from verify carries the txn; pending/expired have no data.
        amount = None
        if state == "success":
            try:
                amount = Decimal(str(d.get("amount")))
            except Exception:  # noqa: BLE001
                amount = None
        return FamGatewayStatus(
            state=state,
            order_id=d.get("order_id", order_id),
            utr=d.get("utr", ""),
            sender_name=d.get("sender_name", ""),
            amount=amount,
        )
