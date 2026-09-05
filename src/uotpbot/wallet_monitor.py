"""Provider wallet monitor (P2).

The bot can only top up the provider wallet if it has a *funding source*; when
the uotp.store dashboard wallet is empty, every order fails with NO_BALANCE.
The right fix is to alert the owner BEFORE depletion, not after.

A daemon thread periodically calls ``provider.get_balance()`` and:
  - threshold alerts (critical / low) with a min gap so it does not spam,
  - a sudden-drop alert (>50% between checks),
  - logs every check and records state for ``/readyz`` and ``/metrics``.

``notify_owner`` is an optional callable ``(text) -> None``. In the bot the
Telegram app registers a sender; in headless/test runs it is a no-op logger.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("uotpbot.walletmonitor")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class WalletMonitor:
    CRITICAL_THRESHOLD_PAISE = 50_00  # Rs.50
    LOW_THRESHOLD_PAISE = 200_00       # Rs.200
    CRITICAL_ALERT_INTERVAL = 300.0    # 5 min
    LOW_ALERT_INTERVAL = 1800.0        # 30 min
    DROP_RATIO = 0.5

    def __init__(
        self,
        provider,
        *,
        notify_owner: Optional[Callable[[str], None]] = None,
        check_interval: float = 120.0,
        critical_paise: Optional[int] = None,
        low_paise: Optional[int] = None,
    ) -> None:
        self.provider = provider
        self.notify_owner = notify_owner
        self.check_interval = check_interval
        self.critical_paise = critical_paise or _int_env(
            "WALLET_MONITOR_CRITICAL_PAISE", self.CRITICAL_THRESHOLD_PAISE)
        self.low_paise = low_paise or _int_env(
            "WALLET_MONITOR_LOW_PAISE", self.LOW_THRESHOLD_PAISE)
        self.critical_alert_interval = float(self._env("WALLET_MONITOR_CRITICAL_ALERT", 300.0))
        self.low_alert_interval = float(self._env("WALLET_MONITOR_LOW_ALERT", 1800.0))
        self._stop = threading.Event()
        self._last_alert = 0.0
        self._last_balance_paise: Optional[int] = None
        self._last_check: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wallet-monitor",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- state -----------------------------------------------------------
    @staticmethod
    def _env(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def state(self) -> dict:
        """Snapshot for /readyz and /metrics."""
        return {
            "provider_wallet_paise": self._last_balance_paise,
            "last_check": self._last_check,
            "critical": bool(self._last_balance_paise is not None
                             and self._last_balance_paise < self.critical_paise),
            "low": bool(self._last_balance_paise is not None
                        and self._last_balance_paise < self.low_paise),
        }

    # -- loop ------------------------------------------------------------
    def _run(self) -> None:
        log.info("wallet monitor started (every %ss)", self.check_interval)
        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception as exc:  # noqa: BLE001 - never kill the monitor
                log.error("wallet monitor check failed: %s", exc, exc_info=True)
            self._stop.wait(self.check_interval)

    def _check_once(self) -> None:
        bal = self.provider.get_balance()
        paise = bal.credit.paise if hasattr(bal.credit, "paise") else int(bal.credit)
        now = time.time()
        previous = self._last_balance_paise
        self._last_balance_paise = paise
        self._last_check = now
        log.info("provider wallet: ₹%.2f", paise / 100)

        # Sudden-drop check uses the value from the PREVIOUS check.
        if previous is not None and previous > 0:
            drop = (previous - paise) / previous
            if drop > self.DROP_RATIO:
                self._alert(
                    f"🚨 PROVIDER WALLET DROP\n\n"
                    f"Balance fell {drop:.0%}:\n"
                    f"₹{previous / 100:.2f} → ₹{paise / 100:.2f}\n\n"
                    f"Possible bulk order or leak. Check immediately.",
                    force=True,
                )

        if paise < self.critical_paise:
            self._alert(
                f"🔴 CRITICAL: provider wallet almost empty\n\n"
                f"Balance: ₹{paise / 100:.2f}\n"
                f"Orders will FAIL with NO_BALANCE soon.\n\n"
                f"Top up the YC OTP supplier wallet NOW.",
                interval=self.critical_alert_interval,
            )
        elif paise < self.low_paise:
            self._alert(
                f"🟡 LOW: provider wallet at ₹{paise / 100:.2f}\n"
                f"Top up before the next bulk of orders.",
                interval=self.low_alert_interval,
            )

    def _alert(self, text: str, *, interval: float = 0.0, force: bool = False) -> None:
        now = time.time()
        if not force and interval > 0 and now - self._last_alert < interval:
            return
        self._last_alert = now
        if self.notify_owner is not None:
            try:
                self.notify_owner(text)
            except Exception as exc:  # noqa: BLE001
                log.error("owner alert send failed: %s", exc)
        log.warning("wallet alert: %s", text.splitlines()[0])
