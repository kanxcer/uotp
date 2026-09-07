"""In-memory SMM provider for tests. Never talks to a network."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Optional, Sequence

from .provider import SmmProviderError, SmmStatus, normalize_status

__all__ = ["MockSmmProvider"]


class MockSmmProvider:
    """Scriptable panel: tests push services and decide add/status outcomes."""

    name = "mock-smm"

    def __init__(
        self,
        services: Optional[list[dict]] = None,
        *,
        balance: Decimal = Decimal("10"),
        currency: str = "USD",
    ) -> None:
        self.services = list(services or [])
        self.balance = Decimal(balance)
        self.currency = currency
        self.orders: dict[str, dict] = {}
        self.next_id = 1
        self.fail_add: Optional[str] = None
        self.ambiguous_add = False
        self.add_calls: list[tuple[str, str, int]] = []
        self.status_calls: list[str] = []
        self.refill_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.balance_calls = 0

    def get_balance(self) -> tuple[Decimal, str]:
        self.balance_calls += 1
        return self.balance, self.currency

    def list_services(self) -> Sequence[Mapping[str, object]]:
        return list(self.services)

    def add_order(self, service_id: str, link: str, quantity: int) -> str:
        self.add_calls.append((str(service_id), str(link), int(quantity)))
        if self.ambiguous_add:
            from .provider import SmmAmbiguous
            raise SmmAmbiguous("simulated timeout on add")
        if self.fail_add:
            raise SmmProviderError(self.fail_add)
        oid = str(self.next_id)
        self.next_id += 1
        self.orders[oid] = {
            "service": str(service_id),
            "link": str(link),
            "quantity": int(quantity),
            "status": "Pending",
            "charge": "0.01",
            "remains": int(quantity),
            "start_count": 0,
            "currency": self.currency,
        }
        return oid

    def get_status(self, order_id: str) -> SmmStatus:
        self.status_calls.append(str(order_id))
        row = self.orders.get(str(order_id))
        if row is None:
            return SmmStatus(order_id=str(order_id), status="failed",
                             error="Incorrect order ID")
        return SmmStatus(
            order_id=str(order_id),
            status=normalize_status(str(row.get("status") or "pending")),
            charge=Decimal(str(row.get("charge") or "0")),
            remains=int(row.get("remains") or 0),
            start_count=int(row.get("start_count") or 0),
            currency=str(row.get("currency") or self.currency),
        )

    def get_statuses(self, order_ids: Sequence[str]) -> dict[str, SmmStatus]:
        return {str(i): self.get_status(i) for i in order_ids}

    def refill(self, order_id: str) -> str:
        self.refill_calls.append(str(order_id))
        if str(order_id) not in self.orders:
            raise SmmProviderError("Order not eligible for refill.")
        return f"r{order_id}"

    def cancel(self, order_ids: Sequence[str]) -> Mapping[str, object]:
        out = []
        for oid in order_ids:
            self.cancel_calls.append(str(oid))
            row = self.orders.get(str(oid))
            if row is None:
                out.append({"order": oid, "cancel": {"error": "Incorrect order ID"}})
            else:
                row["status"] = "Canceled"
                row["remains"] = int(row.get("quantity") or 0)
                out.append({"order": oid, "cancel": 1})
        return out

    def complete(self, order_id: str, *, remains: int = 0, partial: bool = False) -> None:
        row = self.orders[str(order_id)]
        row["status"] = "Partial" if partial else "Completed"
        row["remains"] = remains

    def push_service(self, **row: object) -> None:
        self.services.append(row)
