"""Phase-1 (TITAN v2.0) enhancements: adaptive polling, wallet monitor,
rate limiter, cancel tracker. All pure / in-process, no network."""

from __future__ import annotations

import time

import pytest

from uotpbot.adaptive import AdaptivePollConfig, adaptive_poll_interval
from uotpbot.cancel_tracker import CancelTracker
from uotpbot.ratelimit import RateLimitConfig, RateLimiter
from uotpbot.wallet_monitor import WalletMonitor


# -- [P1] adaptive poll interval ----------------------------------------
class TestAdaptivePoll:
    def _cfg(self, **kw) -> AdaptivePollConfig:
        base = dict(
            fresh_interval=3.0, fresh_until_seconds=60.0,
            aging_interval=8.0, aging_until_seconds=180.0,
            stale_interval=20.0, max_interval=30.0, backoff_factor=1.2,
        )
        base.update(kw)
        return AdaptivePollConfig(**base)

    def test_fresh_age_uses_fast_interval(self):
        cfg = self._cfg()
        assert adaptive_poll_interval(age_seconds=5, consecutive_waits=0, config=cfg) == 3.0

    def test_aging_age_uses_slower_interval(self):
        cfg = self._cfg()
        assert adaptive_poll_interval(age_seconds=120, consecutive_waits=0, config=cfg) == 8.0

    def test_stale_age_backs_off_with_waits(self):
        cfg = self._cfg()
        # At 300s, one wait is 20*1.2 = 24s.
        assert adaptive_poll_interval(age_seconds=300, consecutive_waits=1, config=cfg) == pytest.approx(24.0)
        # Many waits hit the max cap.
        assert adaptive_poll_interval(age_seconds=300, consecutive_waits=20, config=cfg) == 30.0

    def test_never_below_minimum(self):
        cfg = self._cfg()
        assert adaptive_poll_interval(age_seconds=0, consecutive_waits=0, config=cfg) >= 0.1

    def test_monotonic_growth(self):
        cfg = self._cfg()
        vals = [
            adaptive_poll_interval(age_seconds=300, consecutive_waits=n, config=cfg)
            for n in range(0, 6)
        ]
        assert vals == sorted(vals)
        assert vals[-1] <= cfg.max_interval


# -- [P3] per-user buy rate limiter -------------------------------------
class TestRateLimiter:
    def test_allows_up_to_max_then_blocks(self):
        rl = RateLimiter(RateLimitConfig(max_buys=3, window_seconds=60, cooldown_seconds=0))
        for _ in range(3):
            allowed, _ = rl.check("u1")
            assert allowed
            rl.record("u1")
        allowed, why = rl.check("u1")
        assert not allowed and "Wait" in why

    def test_window_evicts_old_buys(self):
        rl = RateLimiter(RateLimitConfig(max_buys=1, window_seconds=60, cooldown_seconds=0))
        assert rl.check("u1")[0]
        rl.record("u1")
        assert not rl.check("u1")[0]
        # Pretend the window has passed.
        with rl._lock:
            rl._history["u1"] = [time.time() - 100]
        assert rl.check("u1")[0]

    def test_cooldown_gap(self):
        rl = RateLimiter(RateLimitConfig(max_buys=5, window_seconds=60, cooldown_seconds=5))
        assert rl.check("u1")[0]
        rl.record("u1")
        assert not rl.check("u1")[0]  # within cooldown

    def test_independent_users(self):
        rl = RateLimiter(RateLimitConfig(max_buys=1, window_seconds=60, cooldown_seconds=0))
        rl.record("a")
        assert rl.check("a")[0] is False
        assert rl.check("b")[0] is True

    def test_override_bypasses(self):
        rl = RateLimiter(RateLimitConfig(max_buys=0, window_seconds=60, cooldown_seconds=0))
        rl.set_user_override("owner")
        allowed, _ = rl.check("owner")
        assert allowed


# -- [P4] cancel tracker -------------------------------------------------
class TestCancelTracker:
    def test_records_and_summarises(self):
        tr = CancelTracker()
        tr.record_denied(service="telegram", server="1", order_id="x|1", cost_paise=300)
        tr.record_denied(service="telegram", server="1", order_id="y|1", cost_paise=200)
        s = tr.summary()
        assert s["cancel_denied_total"] == 2
        assert s["cancel_denied_sunk_paise"] == 500
        assert s["cancel_denied_sunk_inr"] == 5.0
        assert len(s["by_server"]) == 1

    def test_empty_report(self):
        assert "No EARLY_CANCEL_DENIED events" in CancelTracker().report()


# -- [P2] wallet monitor (threshold logic, no network) -------------------
class _Money:
    def __init__(self, paise):
        self.paise = paise


class _Credit:
    def __init__(self, paise):
        self.credit = _Money(paise)


class _FakeProvider:
    def __init__(self, paise):
        self._paise = paise

    def get_balance(self):
        return _Credit(self._paise)


def test_wallet_monitor_flags_low():
    mon = WalletMonitor(_FakeProvider(1000), check_interval=10.0,
                        critical_paise=500, low_paise=2000)
    # No notify_owner -> only logs; assert state flags.
    mon._check_once()
    st = mon.state()
    assert st["low"] is True and st["critical"] is False


class _FakeProviderSeq:
    """Provider that returns a fixed sequence of balances."""
    def __init__(self, values):
        self._values = list(values)

    def get_balance(self):
        paise = self._values.pop(0)
        return _Credit(paise)


def test_wallet_monitor_alerts_on_sudden_drop():
    """A >50% drop between checks must trigger the drop alert."""
    sent = []
    # Rs.1000 -> Rs.400 (60% drop), well above both thresholds so only the
    # drop branch fires.
    mon = WalletMonitor(_FakeProviderSeq([100000, 40000]), notify_owner=sent.append,
                        check_interval=10.0, critical_paise=5000, low_paise=20000)
    mon._check_once()
    assert len(sent) == 0  # first check, no previous to compare
    sent.clear()
    mon._check_once()
    assert any("DROP" in s for s in sent)
