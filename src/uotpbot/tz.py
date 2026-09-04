"""Bot time zone: Asia/Kolkata (India).

Every human-facing timestamp in the bot renders in IST, regardless of the
host server's clock. All timestamps are still stored as absolute epoch seconds
(``time.time()``) so nothing currency- or order-timing-related depends on the
server's timezone; only the *display* is localised here.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

__all__ = ["TZ", "india_now", "format_ts"]

#: The bot's one display timezone (configurable constant; kept module-level so
#: every caller renders the same wall clock).
TZ = ZoneInfo("Asia/Kolkata")


def india_now() -> datetime:
    """Current time, wall-clocked to India."""
    return datetime.now(TZ)


def format_ts(ts: float, *, sep: str = " · ", date_fmt: str = "%d %b %Y",
              time_fmt: str = "%H:%M:%S") -> str:
    """Render an epoch timestamp as an IST wall-clock string, e.g.
    ``04 Sep 2026 · 21:34:05 IST``. Falls back to ``—`` on any bad input.
    """
    try:
        dt = datetime.fromtimestamp(float(ts), TZ)
    except Exception:  # noqa: BLE001 - bad/NaN timestamp
        return "—"
    return f"{dt.strftime(date_fmt)}{sep}{dt.strftime(time_fmt)} IST"
