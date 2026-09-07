"""Durable social-boost orders. Same sqlite/postgres connection as wallets."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..money import Money

__all__ = ["SmmOrderRow", "SmmStore", "SMM_SCHEMA", "OPEN_SQL_STATUSES"]

OPEN_SQL_STATUSES = ("pending", "in_progress")

SMM_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    {pk}
    scope TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    provider_order_id TEXT NOT NULL DEFAULT '',
    service_id TEXT NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL,
    charge_paise INTEGER NOT NULL,
    cost_paise INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    remains INTEGER NOT NULL DEFAULT 0,
    start_count INTEGER NOT NULL DEFAULT 0,
    refunded_paise INTEGER NOT NULL DEFAULT 0,
    refillable INTEGER NOT NULL DEFAULT 0,
    cancelable INTEGER NOT NULL DEFAULT 0,
    earnings_paid INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT ''
)
"""

_SELECT = (
    "id, scope, user_id, provider_order_id, service_id, service_name, platform, "
    "category, link, quantity, charge_paise, cost_paise, status, remains, "
    "start_count, refunded_paise, refillable, cancelable, earnings_paid, "
    "ts, updated_ts, last_error, extra"
)


@dataclass(frozen=True, slots=True)
class SmmOrderRow:
    id: int
    scope: str
    user_id: str
    provider_order_id: str
    service_id: str
    service_name: str
    platform: str
    category: str
    link: str
    quantity: int
    charge: Money
    cost: Money
    status: str
    remains: int
    start_count: int
    refunded: Money
    refillable: bool
    cancelable: bool
    earnings_paid: bool
    ts: float
    updated_ts: float
    last_error: str = ""
    extra: str = ""

    @property
    def net(self) -> Money:
        left = self.charge.paise - self.refunded.paise
        return Money(left if left > 0 else 0)

    def extra_map(self) -> dict[str, Any]:
        if not self.extra:
            return {}
        try:
            data = json.loads(self.extra)
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def refill_days(self) -> int:
        try:
            return max(0, int(self.extra_map().get("refill_days") or 0))
        except (TypeError, ValueError):
            return 0

    def refill_open(self, now: Optional[float] = None) -> bool:
        """True while the supplier refill window is still open."""
        if not self.refillable:
            return False
        if self.status not in {"completed", "partial"}:
            return False
        days = self.refill_days
        if days <= 0:
            return True
        data = self.extra_map()
        try:
            start = float(data.get("completed_ts") or 0)
        except (TypeError, ValueError):
            start = 0.0
        if start <= 0:
            start = float(self.updated_ts or self.ts or 0)
        if start <= 0:
            return True
        now_ts = time.time() if now is None else now
        return now_ts <= start + days * 86400.0


def _row(r) -> SmmOrderRow:
    return SmmOrderRow(
        id=int(r[0]),
        scope=r[1] or "",
        user_id=r[2] or "",
        provider_order_id=r[3] or "",
        service_id=r[4] or "",
        service_name=r[5] or "",
        platform=r[6] or "",
        category=r[7] or "",
        link=r[8] or "",
        quantity=int(r[9] or 0),
        charge=Money(int(r[10] or 0)),
        cost=Money(int(r[11] or 0)),
        status=r[12] or "pending",
        remains=int(r[13] or 0),
        start_count=int(r[14] or 0),
        refunded=Money(int(r[15] or 0)),
        refillable=bool(r[16]),
        cancelable=bool(r[17]),
        earnings_paid=bool(r[18]),
        ts=float(r[19] or 0),
        updated_ts=float(r[20] or 0),
        last_error=r[21] or "",
        extra=r[22] or "",
    )


class SmmStore:
    """CRUD over ``smm_orders``. ``pg=True`` uses ``%s`` placeholders."""

    def __init__(self, conn, lock, table: str, *, pg: bool = False) -> None:
        self._conn = conn
        self._lock = lock
        self._t = table
        self._pg = pg

    def _sql(self, sql: str) -> str:
        formatted = sql.replace("{t}", self._t)
        if self._pg:
            return formatted.replace("?", "%s")
        return formatted

    def ensure(self) -> None:
        pk = (
            "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            if self._pg else
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        )
        ddl = SMM_SCHEMA.format(t=self._t, pk=pk)
        with self._lock:
            self._conn.execute(ddl)
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_smm_user ON {self._t}(scope, user_id)"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_smm_open ON {self._t}(status, provider_order_id)"
            )
            if not self._pg:
                self._conn.commit()

    def create(
        self,
        *,
        user_id: str,
        service_id: str,
        service_name: str = "",
        platform: str = "",
        category: str = "",
        link: str = "",
        quantity: int,
        charge: Money,
        cost: Money = Money(0),
        provider_order_id: str = "",
        status: str = "pending",
        remains: int = 0,
        refillable: bool = False,
        cancelable: bool = False,
        scope: str = "",
        extra: str = "",
    ) -> int:
        now = time.time()
        sql = self._sql(
            "INSERT INTO {t}(scope, user_id, provider_order_id, service_id, "
            "service_name, platform, category, link, quantity, charge_paise, "
            "cost_paise, status, remains, refillable, cancelable, ts, updated_ts, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            scope, user_id, provider_order_id, service_id, service_name,
            platform, category, link, int(quantity), charge.paise, cost.paise,
            status, int(remains), 1 if refillable else 0, 1 if cancelable else 0,
            now, now, extra or "",
        )
        with self._lock:
            if self._pg:
                row = self._conn.execute(sql + " RETURNING id", params).fetchone()
                return int(row[0])
            with self._conn:
                cur = self._conn.execute(sql, params)
                return int(cur.lastrowid or 0)

    def get(self, oid: int, *, scope: Optional[str] = None,
            user_id: str = "") -> Optional[SmmOrderRow]:
        sql = f"SELECT {_SELECT} FROM {self._t} WHERE id = ?"
        params: list[Any] = [int(oid)]
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._lock:
            row = self._conn.execute(self._sql(sql), params).fetchone()
        return _row(row) if row else None

    def list_user(self, user_id: str, *, scope: str = "",
                  limit: int = 20) -> list[SmmOrderRow]:
        sql = (
            f"SELECT {_SELECT} FROM {self._t} WHERE scope = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(
                self._sql(sql), (scope, user_id, int(limit)),
            ).fetchall()
        return [_row(r) for r in rows]

    def list_open(self, *, limit: int = 200) -> list[SmmOrderRow]:
        """Every still-open order that has an upstream id (poller)."""
        placeholders = ",".join("?" * len(OPEN_SQL_STATUSES))
        sql = (
            f"SELECT {_SELECT} FROM {self._t} WHERE status IN ({placeholders}) "
            "AND provider_order_id <> '' ORDER BY id ASC LIMIT ?"
        )
        params = (*OPEN_SQL_STATUSES, int(limit))
        with self._lock:
            rows = self._conn.execute(self._sql(sql), params).fetchall()
        return [_row(r) for r in rows]

    def update(self, oid: int, **fields: Any) -> bool:
        """Patch columns by name. Unknown keys are ignored."""
        allowed = {
            "provider_order_id": "provider_order_id",
            "status": "status",
            "remains": "remains",
            "start_count": "start_count",
            "refunded": "refunded_paise",
            "refillable": "refillable",
            "cancelable": "cancelable",
            "earnings_paid": "earnings_paid",
            "last_error": "last_error",
            "extra": "extra",
        }
        sets = ["updated_ts = ?"]
        params: list[Any] = [time.time()]
        for key, col in allowed.items():
            if key not in fields:
                continue
            value = fields[key]
            if key in {"refillable", "cancelable", "earnings_paid"}:
                value = 1 if value else 0
            elif key == "refunded" and hasattr(value, "paise"):
                value = value.paise
            sets.append(f"{col} = ?")
            params.append(value)
        if len(sets) == 1:
            return False
        params.append(int(oid))
        sql = f"UPDATE {self._t} SET {', '.join(sets)} WHERE id = ?"
        with self._lock:
            if self._pg:
                cur = self._conn.execute(self._sql(sql), params)
                return (cur.rowcount or 0) == 1
            with self._conn:
                cur = self._conn.execute(self._sql(sql), params)
                return (cur.rowcount or 0) == 1
