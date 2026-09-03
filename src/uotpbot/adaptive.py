"""Adaptive OTP polling (P1).

The provider charges a partial refund if we ask too late and may throttle if we
ask too often. A fixed 3s interval around 20 concurrent orders is ~7 req/s for
the whole window -- rate-limit risk with no benefit, because an OTP barely ever
arrives in the first seconds for a service that will deliver late anyway.

This module resolves the NEXT poll interval per order from how old it is and how
many consecutive empty (STATUS_WAIT_CODE) answers it has had. It is a pure
function, so it is trivially testable and drops into the blocking poll loop in
``Provider.wait_for_otp`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdaptivePollConfig:
    #: Seconds while an order is "fresh" -- OTP is likely imminent.
    fresh_interval: float = 3.0
    fresh_until_seconds: float = 60.0
    #: Seconds once the order has aged into "moderate" territory.
    aging_interval: float = 8.0
    aging_until_seconds: float = 180.0
    #: Base for the exponential backoff after an order is "old".
    stale_interval: float = 20.0
    #: Absolute cap -- never wait longer than this between polls.
    max_interval: float = 30.0
    #: Backoff growth factor per consecutive empty poll (capped).
    backoff_factor: float = 1.2


def adaptive_poll_interval(
    *,
    age_seconds: float,
    consecutive_waits: int,
    config: AdaptivePollConfig | None = None,
) -> float:
    """The interval to wait before the NEXT poll for this order.

    ``age_seconds`` is elapsed since the number was allocated;
    ``consecutive_waits`` is the run of empty (no-code) poll answers so far.
    """
    cfg = config or AdaptivePollConfig()
    if age_seconds < cfg.fresh_until_seconds:
        base = cfg.fresh_interval
    elif age_seconds < cfg.aging_until_seconds:
        base = cfg.aging_interval
    else:
        base = min(
            cfg.stale_interval * (cfg.backoff_factor ** min(consecutive_waits, 5)),
            cfg.max_interval,
        )
    return max(0.1, base)


__all__ = ["AdaptivePollConfig", "adaptive_poll_interval"]
