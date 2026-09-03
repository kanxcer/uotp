"""Per-user buy rate limiter (P3).

Each buy triggers a provider ``getNumber`` call, and a spammy user (or a bot)
can burn provider credit and API rate budget for nothing. This gates the buy
entry points with a sliding-window limit plus a minimum gap between individual
buys.

In-memory by default (thread-safe, fast path). An optional backing store -- any
object with ``kv_get``/``kv_set``, such as the wallet store -- lets the window
survive a redeploy, so a restart cannot be used to bypass the limit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    #: Maximum buys a user may start within ``window_seconds``.
    max_buys: int = 3
    window_seconds: int = 30
    #: Minimum gap between two buys from the same user.
    cooldown_seconds: float = 8.0


class RateLimiter:
    """Sliding-window buy limiter keyed by user_id.

    ``check`` returns ``(allowed, reason)``; on an allowed call you should then
    call :meth:`record`. ``set_user_override`` exempts an id (e.g. the owner or
    a white-label operator) from the limit.
    """

    def __init__(self, config: Optional[RateLimitConfig] = None, *, store=None) -> None:
        self.config = config or RateLimitConfig()
        self._store = store
        self._lock = threading.Lock()
        #: user_id -> list of buy timestamps (the window), held in memory.
        self._history: dict[str, list[float]] = {}
        self._override: set[str] = set()

    # -- persistence (optional) ------------------------------------------
    def _read(self, user_id: str) -> list[float]:
        with self._lock:
            hit = self._history.get(user_id)
        if hit is not None:
            return hit
        if self._store is not None and callable(getattr(self._store, "kv_get", None)):
            raw = self._store.kv_get(f"rl:{user_id}")
            if raw:
                try:
                    ts = [float(x) for x in raw.split(",") if x]
                    with self._lock:
                        self._history[user_id] = ts
                    return ts
                except ValueError:
                    pass
        return []

    def _write(self, user_id: str, ts: list[float]) -> None:
        with self._lock:
            self._history[user_id] = ts
        if self._store is not None and callable(getattr(self._store, "kv_set", None)):
            try:
                self._store.kv_set(f"rl:{user_id}", ",".join(f"{t:.1f}" for t in ts))
            except Exception:  # noqa: BLE001 - persistence must never break a buy
                pass

    def _cleanup(self, ts: list[float], now: float) -> list[float]:
        cutoff = now - self.config.window_seconds
        return [t for t in ts if t > cutoff]

    def set_user_override(self, user_id: str, on: bool = True) -> None:
        with self._lock:
            if on:
                self._override.add(user_id)
            else:
                self._override.discard(user_id)

    def check(self, user_id: str) -> tuple[bool, str]:
        """Whether this user may start a buy now. Does not record anything."""
        with self._lock:
            if user_id in self._override:
                return True, ""
        now = time.time()
        ts = self._cleanup(self._read(user_id), now)
        if len(ts) >= self.config.max_buys:
            wait = int(self.config.window_seconds - (now - ts[0])) + 1
            return False, (f"⏳ Too many purchases in a short window. "
                           f"Wait {max(wait, 1)}s before buying again.")
        if ts:
            gap = now - ts[-1]
            if gap < self.config.cooldown_seconds:
                wait = int(self.config.cooldown_seconds - gap) + 1
                return False, f"⏳ Please wait {max(wait, 1)}s between purchases."
        return True, ""

    def record(self, user_id: str) -> None:
        now = time.time()
        ts = self._cleanup(self._read(user_id), now)
        ts.append(now)
        self._write(user_id, ts)

    def stats(self) -> dict:
        now = time.time()
        active = 0
        with self._lock:
            for uid in list(self._history.keys()):
                clean = self._cleanup(self._history.get(uid, []), now)
                if clean:
                    self._history[uid] = clean
                    active += 1
                else:
                    self._history.pop(uid, None)
            return {
                "tracked_users": active,
                "buys_last_window": sum(len(v) for v in self._history.values()),
            }
