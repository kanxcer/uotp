"""PerfectPanel v2 HTTP client (Casper SMM and any compatible panel).

POST ``application/x-www-form-urlencoded`` to ``{base}/`` with ``key`` +
``action``. Documented at the panel's ``/api`` page; the vocabulary is the
industry-standard v2 set: ``services``, ``add``, ``status``, ``balance``,
``refill``, ``cancel``.

Customer-facing copy must never name the panel. This module is the only
place the default Casper URL appears.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from .provider import (
    SmmAmbiguous,
    SmmAuthError,
    SmmError,
    SmmProviderError,
    SmmStatus,
    normalize_status,
)

__all__ = ["PanelV2Client", "DEFAULT_API_URL"]

DEFAULT_API_URL = "https://caspersmm.com/api/v2"

_AUTH_HINTS = ("incorrect api key", "invalid api key", "api key", "not authenticated")
_FUNDS_HINTS = ("not enough funds", "no enough", "insufficient", "not enough balance")


class PanelV2Client:
    """Talks to a PerfectPanel-family ``/api/v2`` endpoint."""

    name = "smm-panel"

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        opener: Any = None,
        user_agent: str = "uotpbot-smm/1.0",
    ) -> None:
        if not api_key:
            raise SmmAuthError("SMM API key is empty")
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._opener = opener or urllib.request
        self.user_agent = user_agent

    # -- transport -------------------------------------------------------
    def _call(self, action: str, *, retry: bool = True, **params: Any) -> Any:
        payload = {"key": self.api_key, "action": action}
        for key, value in params.items():
            if value is None:
                continue
            payload[key] = str(value)
        body = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            self.api_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )
        attempts = 0
        while True:
            try:
                with self._opener.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                raw = exc.read() if exc.fp else b""
                if exc.code in (401, 403):
                    raise SmmAuthError(f"HTTP {exc.code}") from None
                if exc.code in (408, 504) or (500 <= exc.code < 600):
                    attempts += 1
                    if (not retry) or attempts > self.max_retries:
                        if action == "add":
                            raise SmmAmbiguous(
                                f"HTTP {exc.code} on add; do not retry"
                            ) from None
                        raise SmmProviderError(f"HTTP {exc.code}") from None
                    time.sleep(min(1.5 ** attempts, 4.0))
                    continue
                raise SmmProviderError(f"HTTP {exc.code}: {raw[:200]!r}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                attempts += 1
                if (not retry) or attempts > self.max_retries:
                    if action == "add":
                        raise SmmAmbiguous(
                            f"add failed after {attempts} attempt(s): {exc}. "
                            "The order may already exist upstream; do not retry."
                        ) from None
                    raise SmmProviderError(f"network error: {exc}") from None
                time.sleep(min(1.5 ** attempts, 4.0))
                continue
            break
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        try:
            parsed = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise SmmProviderError(f"non-JSON response: {text[:200]!r}") from exc
        if isinstance(parsed, dict) and parsed.get("error"):
            raise self._error_for(str(parsed.get("error")), action)
        return parsed

    def _error_for(self, message: str, action: str) -> SmmError:
        low = (message or "").lower()
        if any(h in low for h in _AUTH_HINTS):
            return SmmAuthError(message)
        if action == "add":
            return SmmProviderError(message)
        return SmmProviderError(message)

    # -- Provider --------------------------------------------------------
    def get_balance(self) -> tuple[Decimal, str]:
        payload = self._call("balance")
        if not isinstance(payload, dict):
            raise SmmProviderError(f"unexpected balance payload: {payload!r}")
        raw = payload.get("balance", "0")
        try:
            amount = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise SmmProviderError(f"bad balance {raw!r}") from exc
        currency = str(payload.get("currency") or "USD")
        return amount, currency

    def list_services(self) -> Sequence[Mapping[str, object]]:
        payload = self._call("services")
        if isinstance(payload, list):
            return payload
        raise SmmProviderError(f"unexpected services payload: {type(payload).__name__}")

    def add_order(self, service_id: str, link: str, quantity: int) -> str:
        payload = self._call(
            "add", retry=False,
            service=service_id, link=link, quantity=int(quantity),
        )
        if not isinstance(payload, dict):
            raise SmmProviderError(f"unexpected add payload: {payload!r}")
        order = payload.get("order")
        if order is None or str(order).strip() == "":
            raise SmmProviderError("supplier returned no order id")
        return str(order)

    def get_status(self, order_id: str) -> SmmStatus:
        payload = self._call("status", order=order_id)
        if not isinstance(payload, dict):
            raise SmmProviderError(f"unexpected status payload: {payload!r}")
        if payload.get("error"):
            return SmmStatus(order_id=str(order_id), status="failed",
                             error=str(payload.get("error")))
        return self._status_from(str(order_id), payload)

    def get_statuses(self, order_ids: Sequence[str]) -> dict[str, SmmStatus]:
        ids = [str(i) for i in order_ids if str(i).strip()]
        if not ids:
            return {}
        out: dict[str, SmmStatus] = {}
        # PerfectPanel caps batch at 100.
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            payload = self._call("status", orders=",".join(chunk))
            if isinstance(payload, dict) and any(
                k in payload for k in ("charge", "status", "remains")
            ) and len(chunk) == 1:
                out[chunk[0]] = self._status_from(chunk[0], payload)
                continue
            if not isinstance(payload, dict):
                continue
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                if value.get("error"):
                    out[str(key)] = SmmStatus(
                        order_id=str(key), status="failed",
                        error=str(value.get("error")),
                    )
                else:
                    out[str(key)] = self._status_from(str(key), value)
        return out

    def refill(self, order_id: str) -> str:
        payload = self._call("refill", retry=False, order=order_id)
        if isinstance(payload, dict):
            rid = payload.get("refill")
            if rid is not None:
                return str(rid)
            if payload.get("error"):
                raise SmmProviderError(str(payload.get("error")))
        raise SmmProviderError("supplier did not accept the refill")

    def cancel(self, order_ids: Sequence[str]) -> Mapping[str, object]:
        ids = [str(i) for i in order_ids if str(i).strip()]
        if not ids:
            return {}
        payload = self._call("cancel", retry=False, orders=",".join(ids))
        return payload if isinstance(payload, (dict, list)) else {}

    @staticmethod
    def _status_from(order_id: str, payload: Mapping[str, Any]) -> SmmStatus:
        charge = Decimal("0")
        try:
            charge = Decimal(str(payload.get("charge") or "0"))
        except (InvalidOperation, ValueError):
            charge = Decimal("0")
        remains = _as_int(payload.get("remains"), 0)
        start = _as_int(payload.get("start_count"), 0)
        return SmmStatus(
            order_id=order_id,
            status=normalize_status(str(payload.get("status") or "pending")),
            charge=charge,
            remains=remains,
            start_count=start,
            currency=str(payload.get("currency") or "USD"),
        )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
