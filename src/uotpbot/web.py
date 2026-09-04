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
    Deliberately reports NO business figures (revenue, profit, wallet
    balances): an unauthenticated endpoint must not publish P&L. Results are
    cached for ``cache_seconds`` so a health-check interval cannot turn into
    an API request storm that gets the key rate-limited.

``/metrics``
    P&L, wallet balances and order counters as JSON. Business-sensitive, so
    it only EXISTS when ``metrics_token`` is set (404 otherwise), and then
    requires ``Authorization: Bearer <token>`` or ``?token=<token>``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
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
        subbots: Optional[Any] = None,
        wallet_monitor: Optional[Any] = None,
        subsystem_stats: Optional[Callable[[], dict]] = None,
        metrics_token: str = "",
    ) -> None:
        self.engine = engine
        self.ledger = ledger
        self.port = port if port is not None else int(os.environ.get(PORT_ENV, DEFAULT_PORT))
        # Business metrics are only served when a token is configured, and
        # then only to a bearer that matches it. Empty (the default) means
        # /metrics does not exist at all -- an unauthenticated endpoint that
        # publishes revenue and wallet balances is a leak, not a feature.
        self._metrics_token = metrics_token
        self._poller = poller
        #: Phase-1 provider wallet monitor (P2), for /readyz + /metrics.
        self._wallet_monitor = wallet_monitor
        #: Phase-1 subsystem stats (rate limiter, cancel tracker) from the router.
        self._subsystem_stats = subsystem_stats
        #: White-label sub-bot manager. Reported by /readyz and /metrics but
        #: deliberately never allowed to flip readiness: one sub-bot with a
        #: revoked token must not cause the platform to restart this process
        #: and drop the main bot mid-order.
        self._subbots = subbots
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
            "subbots_running": (
                self._subbots.running() if self._subbots is not None else None
            ),
        }

    def subbot_health(self) -> Optional[dict[str, Any]]:
        """Sub-bot poller state, or None when white-label is disabled."""
        if self._subbots is None:
            return None
        return self._subbots.health()

    def readiness(self) -> tuple[int, dict[str, Any]]:
        """200 only when the provider answers and the ledger balances."""
        with self._lock:
            if self._cache.fresh(self._cache_seconds):
                cached = self._cache
                return (200 if cached.ok else 503), dict(cached.detail, cached=True)

        # Readiness = "can we serve orders right now", reported as booleans
        # and error CLASS NAMES only. No money figures, no exception messages
        # (they can echo URLs, tokens or provider payloads): this endpoint is
        # unauthenticated.
        detail: dict[str, Any] = {}
        ok = True
        try:
            self.engine.provider.get_balance()
            detail["provider"] = "reachable"
        except (ProviderError, Exception) as exc:  # noqa: BLE001 - report, never raise
            ok = False
            # Class name only: the message can carry URLs/credentials.
            detail["provider_error"] = type(exc).__name__
        try:
            self.ledger.verify()
            detail["ledger"] = "balanced"
        except LedgerError:
            ok = False
            detail["ledger"] = "unbalanced"

        health = self.subbot_health()
        if health is not None:
            # Informational: a dead sub-bot is reported, not fatal.
            detail["subbots"] = health

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
        health = self.subbot_health()
        if health is not None:
            body["subbots"] = health
        # Phase-1 subsystem stats.
        if self._wallet_monitor is not None:
            body["provider_wallet"] = self._wallet_monitor.state()
        if self._subsystem_stats is not None:
            try:
                body["phase1"] = self._subsystem_stats()
            except Exception as exc:  # noqa: BLE001 - metrics never break
                body["phase1"] = {"error": str(exc)}
        return 200, body

    # -- HTTP ------------------------------------------------------------
    def _metrics_authed(self, token: str) -> bool:
        """Constant-time check of a presented metrics token."""
        if not self._metrics_token:
            return False
        return bool(token) and secrets.compare_digest(
            token.encode("utf-8"), self._metrics_token.encode("utf-8"))

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

            def _metrics_token_presented(self) -> str:
                """Bearer header first, then a ?token= query (for curl)."""
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
                try:
                    from urllib.parse import parse_qs, urlparse
                    query = parse_qs(urlparse(self.path).query)
                    values = query.get("token", [])
                    return values[0] if values else ""
                except Exception:  # noqa: BLE001 - malformed query is a 403, not 500
                    return ""

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                try:
                    if path == "/healthz":
                        code, payload = server.liveness()
                    elif path == "/readyz":
                        code, payload = server.readiness()
                    elif path == "/metrics":
                        # Business figures: no token configured -> the route
                        # does not exist (404, so it can't be probed into);
                        # token configured -> Bearer/query auth, else 403.
                        if not server._metrics_token:
                            code, payload = 404, {"error": f"no route for {path}"}
                        elif not server._metrics_authed(self._metrics_token_presented()):
                            code, payload = 403, {"error": "unauthorised"}
                        else:
                            code, payload = server.metrics()
                    elif path == "/versionz":
                        code, payload = 200, {
                            # Render injects these; unknown in local/dev runs.
                            "git_commit": os.environ.get("RENDER_GIT_COMMIT", "unknown"),
                            "git_branch": os.environ.get("RENDER_GIT_BRANCH", "unknown"),
                            "service": os.environ.get("RENDER_SERVICE_NAME", "uotpbot"),
                        }
                    elif path == "/":
                        endpoints = ["/healthz", "/readyz"]
                        if server._metrics_token:
                            endpoints.append("/metrics (token required)")
                        code, payload = 200, {"service": "uotpbot", "endpoints": endpoints}
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
