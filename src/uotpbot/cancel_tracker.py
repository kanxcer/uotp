"""EARLY_CANCEL_DENIED cost tracker (P4).

When the provider refuses an early cancel (``EARLY_CANCEL_DENIED`` -- a fresh
activation must sit out its OTP window), the number is still live and charged,
but the customer is told it cannot be released. That cost is real and hidden:
it should feed the delivery model's burn rate and be visible to the owner.

Pure in-process accounting (thread-safe). A snapshot is surfaced on ``/metrics``
and an owner report command. Purely advisory -- it never changes the ledger
(which stays the source of truth for money).
"""

from __future__ import annotations

import threading
import time


class CancelTracker:
    """Counts cancellation refusals and the provider cost they soak up."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: (service, server) -> {"denied": int, "cost_paise": int}
        self._by_server: dict[tuple[str, str], dict[str, int]] = {}
        self._events: list[tuple[str, str, str, int, float]] = []  # svc, server, order, cost, ts

    def record_denied(
        self, *, service: str, server: str = "", order_id: str = "",
        cost_paise: int = 0, reason: str = "EARLY_CANCEL_DENIED",
    ) -> None:
        with self._lock:
            key = (service, server)
            slot = self._by_server.setdefault(key, {"denied": 0, "cost_paise": 0})
            slot["denied"] += 1
            slot["cost_paise"] += cost_paise
            self._events.append((service, server, order_id, cost_paise, time.time()))

    def summary(self) -> dict:
        with self._lock:
            total_denied = sum(s["denied"] for s in self._by_server.values())
            total_cost = sum(s["cost_paise"] for s in self._by_server.values())
            by_server = [
                {
                    "service": svc, "server": server, "denied": s["denied"],
                    "sunk_paise": s["cost_paise"],
                    "sunk_inr": round(s["cost_paise"] / 100, 2),
                }
                for (svc, server), s in sorted(
                    self._by_server.items(), key=lambda kv: -kv[1]["cost_paise"]
                )
            ]
            return {
                "cancel_denied_total": total_denied,
                "cancel_denied_sunk_paise": total_cost,
                "cancel_denied_sunk_inr": round(total_cost / 100, 2),
                "events": len(self._events),
                "by_server": by_server,
            }

    def report(self) -> str:
        s = self.summary()
        if not s["cancel_denied_total"]:
            return "✅ No EARLY_CANCEL_DENIED events recorded."
        lines = [
            "📊 Cancel-denied hidden cost",
            f"Events: {s['cancel_denied_total']} · Sunk: ₹{s['cancel_denied_sunk_inr']:.2f}",
        ]
        for row in s["by_server"]:
            lines.append(f"  {row['service']}/sr{row['server']}: "
                         f"{row['denied']} refused · ₹{row['sunk_inr']:.2f}")
        return "\n".join(lines)
