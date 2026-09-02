"""HTTP server so the bot can run as a Render Web Service.

The bot itself is a long-polling Telegram client: it opens an outbound
connection and never listens. A platform Web Service is expected to bind
``$PORT`` and answer HTTP, so without this module the process starts, binds
nothing, and the platform restarts it forever.

This runs both: the poller in a daemon thread, and a stdlib HTTP server on the
main thread. No web framework, so no new dependency.

Endpoints:

``/healthz``
    Liveness. Answers 200 whenever the process can serve HTTP. Deliberately
    does **not** touch the network -- a provider outage must not cause the
    platform to kill and restart a process that is otherwise fine, because a
    restart mid-order is how you charge a customer and deliver nothing.

``/readyz``
    Readiness. Verifies the provider is reachable and the ledger balances.
    Results are cached for ``cache_seconds`` so a health-check interval cannot
    turn into an API request storm that gets the key rate-limited.

``/metrics``
    P&L, wallet balances and order counters as JSON.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .engine import BotEngine
from .ledger import Ledger, LedgerError
from .provider.base import ProviderError

__all__ = ["HealthServer", "run_web", "PORT_ENV"]

log = logging.getLogger("uotpbot.web")

PORT_ENV = "PORT"
DEFAULT_PORT = 8080


@dataclass(slots=True)
class _CachedCheck:
    """Result of the last readiness probe, with a TTL.

    ``at`` is ``None`` until the first real probe. Using ``0.0`` as the
    sentinel instead would be read as "recent" on a freshly booted container,
    where ``time.monotonic()`` is still small, and /readyz would serve a stale
    not_ready for a whole TTL.
    """

    ok: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    at: Optional[float] = None

    def fresh(self, ttl: float) -> bool:
        return self.at is not None and (time.monotonic() - self.at) < ttl


class HealthServer:
    """Serves health/readiness/metrics and hosts the bot poller."""

    def __init__(
        self,
        engine: BotEngine,
        ledger: Ledger,
        *,
        port: Optional[int] = None,
        poller: Optional[Callable[[], None]] = None,
        cache_seconds: float = 30.0,
    ) -> None:
        self.engine = engine
        self.ledger = ledger
        self.port = port if port is not None else int(os.environ.get(PORT_ENV, DEFAULT_PORT))
        self._poller = poller
        self._cache_seconds = cache_seconds
        self._cache = _CachedCheck()
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._poller_thread: Optional[threading.Thread] = None
        self._httpd: Optional[ThreadingHTTPServer] = None

    # -- checks ----------------------------------------------------------
    def liveness(self) -> tuple[int, dict[str, Any]]:
        """Always 200 if we can answer. Never calls the network."""
        return 200, {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - self._started, 1),
            "poller_alive": bool(
                self._poller_thread and self._poller_thread.is_alive()
            )
            if self._poller is not None
            else None,
        }

    def readiness(self) -> tuple[int, dict[str, Any]]:
        """200 only when the provider answers and the ledger balances."""
        with self._lock:
            if self._cache.fresh(self._cache_seconds):
                cached = self._cache
                return (200 if cached.ok else 503), dict(cached.detail, cached=True)

        detail: dict[str, Any] = {}
        ok = True
        try:
            balance = self.engine.provider.get_balance()
            detail["provider_wallet"] = balance.credit.to_plain()
        except (ProviderError, Exception) as exc:  # noqa: BLE001 - report, never raise
            ok = False
            detail["provider_error"] = f"{type(exc).__name__}: {exc}"
        try:
            self.ledger.verify()
            detail["ledger"] = "balanced"
        except LedgerError as exc:
            ok = False
            detail["ledger"] = f"UNBALANCED: {exc}"
        try:
            pnl = self.ledger.profit_and_loss()
            detail["net_profit"] = pnl.net_profit.to_plain()
            detail["revenue"] = pnl.revenue.to_plain()
        except LedgerError as exc:
            ok = False
            detail["pnl_error"] = str(exc)

        detail["status"] = "ready" if ok else "not_ready"
        with self._lock:
            self._cache = _CachedCheck(ok=ok, detail=detail, at=time.monotonic())
        return (200 if ok else 503), detail

    def metrics(self) -> tuple[int, dict[str, Any]]:
        try:
            pnl = self.ledger.profit_and_loss()
            body = pnl.as_dict()
        except LedgerError as exc:
            return 503, {"error": str(exc)}
        body["uptime_seconds"] = round(time.monotonic() - self._started, 1)
        return 200, body

    # -- HTTP ------------------------------------------------------------
    def _handler_class(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "uotpbot/1.1"

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, indent=2).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                try:
                    if path == "/healthz":
                        code, payload = server.liveness()
                    elif path == "/readyz":
                        code, payload = server.readiness()
                    elif path == "/metrics":
                        code, payload = server.metrics()
                    elif path == "/":
                        code, payload = 200, {
                            "service": "uotpbot",
                            "endpoints": ["/healthz", "/readyz", "/metrics"],
                        }
                    else:
                        code, payload = 404, {"error": f"no route for {path}"}
                except Exception as exc:  # never let a handler kill the server
                    log.exception("handler failed for %s", path)
                    code, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
                self._send(code, payload)

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("%s - %s", self.address_string(), fmt % args)

        return Handler

    # -- lifecycle -------------------------------------------------------
    def start_poller(self) -> None:
        """Start the Telegram poller in a daemon thread."""
        if self._poller is None:
            return
        if self._poller_thread and self._poller_thread.is_alive():
            return

        def run() -> None:
            while True:
                try:
                    self._poller()  # type: ignore[misc]
                    log.warning("poller returned; restarting in 5s")
                except Exception:  # noqa: BLE001 - a poller crash must not kill HTTP
                    log.exception("poller crashed; restarting in 5s")
                time.sleep(5)

        self._poller_thread = threading.Thread(target=run, name="poller", daemon=True)
        self._poller_thread.start()

    def serve_forever(self) -> None:
        """Bind $PORT and serve until SIGTERM/SIGINT."""
        self.start_poller()
        # 0.0.0.0 is required: platforms route to the container's external
        # interface, and binding 127.0.0.1 makes the service unreachable.
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler_class())
        self._httpd.daemon_threads = True
        log.info("listening on 0.0.0.0:%s", self.port)

        stop = threading.Event()

        def shutdown(signum: int, _frame: Any) -> None:
            log.info("received signal %s, shutting down", signum)
            stop.set()
            if self._httpd:
                threading.Thread(target=self._httpd.shutdown, daemon=True).start()

        # Render sends SIGTERM on deploy; ignoring it would leave orders
        # mid-flight and mark the deploy as failed.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, shutdown)
            except ValueError:
                pass  # not on the main thread; signals are already handled

        try:
            self._httpd.serve_forever(poll_interval=0.5)
        finally:
            self._httpd.server_close()
            log.info("server stopped")


def run_web(engine: BotEngine, ledger: Ledger, *, poller: Optional[Callable[[], None]] = None,
            port: Optional[int] = None) -> None:
    """Convenience entry point."""
    HealthServer(engine, ledger, port=port, poller=poller).serve_forever()
