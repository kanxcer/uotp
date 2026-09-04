"""HTTP layer and thread safety.

The regression that matters here: a sqlite connection opened in one thread
cannot be used from another unless it is created with check_same_thread=False.
An HTTP server handles every request on a fresh thread, so without that flag
/readyz and /metrics return 500 forever and the platform never marks the
service healthy. test_ledger_is_usable_from_another_thread pins it.
"""

import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from uotpbot.catalog import Catalog, ServiceCost
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import COGS, OWNER, WALLET, Ledger
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider
from uotpbot.web import HealthServer


# ------------------------------------------------------- thread safety
def test_ledger_is_usable_from_another_thread():
    """The exact failure that broke /readyz and /metrics on first deploy."""
    ledger = Ledger()  # opened on the main thread
    ledger.post(WALLET, OWNER, INR(100), ref="seed")
    errors: list[BaseException] = []
    result: dict[str, str] = {}

    def read_from_worker() -> None:
        try:
            ledger.verify()
            result["balance"] = ledger.balance(WALLET).to_plain()
            result["profit"] = ledger.profit_and_loss().net_profit.to_plain()
        except BaseException as exc:  # noqa: BLE001 - the point is to catch it
            errors.append(exc)

    t = threading.Thread(target=read_from_worker)
    t.start()
    t.join()

    assert not errors, f"cross-thread ledger access failed: {errors[0]!r}"
    assert result["balance"] == "100.00"


def test_ledger_survives_concurrent_writes():
    """Many threads posting at once must not lose a posting or unbalance."""
    ledger = Ledger()
    ledger.post(WALLET, OWNER, INR(10_000), ref="seed")
    threads = 16
    per_thread = 25

    def worker(n: int) -> None:
        for i in range(per_thread):
            ledger.post(COGS, WALLET, INR(1), ref=f"order-{n}-{i}")

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    ledger.verify()
    expected = INR(threads * per_thread)
    assert ledger.balance(COGS) == expected
    assert ledger.balance(WALLET) == INR(10_000) - expected


def test_ledger_survives_concurrent_reads_and_writes():
    ledger = Ledger()
    ledger.post(WALLET, OWNER, INR(1000), ref="seed")
    failures: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(20):
                ledger.post(COGS, WALLET, INR(1), ref=f"w{n}-{i}")
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def reader() -> None:
        try:
            for _ in range(40):
                ledger.profit_and_loss()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, failures[0]
    ledger.verify()


#: Token the rig's metrics endpoint requires (see the auth tests below).
METRICS_TOKEN = "test-metrics-token"


# ----------------------------------------------------------- fixtures
@pytest.fixture()
def rig():
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    })
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(500), seed=3,
    )
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    yield (HealthServer(engine, ledger, cache_seconds=0.0,
                        metrics_token=METRICS_TOKEN), ledger, provider)
    ledger.close()


class BrokenProvider(MockProvider):
    def get_balance(self):
        raise RuntimeError("provider down")


# -------------------------------------------------------------- checks
def test_liveness_never_touches_the_network(rig):
    server, _, _ = rig
    code, body = server.liveness()
    assert code == 200
    assert body["status"] == "ok"
    assert body["uptime_seconds"] >= 0


def test_liveness_survives_a_dead_provider(rig):
    """Liveness must not depend on the provider, or an outage triggers restarts."""
    server, _, provider = rig
    server.engine.provider = BrokenProvider({})
    code, body = server.liveness()
    assert code == 200 and body["status"] == "ok"


def test_readiness_is_200_when_everything_works(rig):
    server, _, _ = rig
    code, body = server.readiness()
    assert code == 200, body
    assert body["status"] == "ready"
    assert body["ledger"] == "balanced"
    assert body["provider"] == "reachable"
    # An unauthenticated endpoint must not publish business figures.
    assert "provider_wallet" not in body
    assert "net_profit" not in body
    assert "revenue" not in body


def test_readiness_is_503_when_the_provider_is_down(rig):
    server, _, _ = rig
    server.engine.provider = BrokenProvider({})
    code, body = server.readiness()
    assert code == 503
    assert body["status"] == "not_ready"
    assert "provider_error" in body
    # Only the exception CLASS leaks -- the message can carry URLs/credentials.
    assert body["provider_error"] == "RuntimeError"
    assert "provider down" not in body["provider_error"]
    # The ledger is still fine, and that must be reported separately.
    assert body["ledger"] == "balanced"


def test_readiness_is_503_when_the_ledger_is_corrupt(rig):
    server, ledger, _ = rig
    ledger._conn.execute(
        "INSERT INTO postings (ts, ref, account, debit_p, credit_p, memo) "
        "VALUES ('x','x','asset:wallet',500,0,'forged')"
    )
    ledger._conn.commit()
    code, body = server.readiness()
    assert code == 503
    assert body["ledger"] == "unbalanced"


def test_readiness_caches(rig):
    server, _, _ = rig
    server._cache_seconds = 3600.0
    first, _ = server.readiness()
    calls = []
    server.engine.provider.get_balance = lambda: calls.append(1) or MockProvider({}).get_balance()
    second, body = server.readiness()
    assert first == second == 200
    assert body.get("cached") is True
    assert calls == []  # served from cache; no API request


def test_metrics_reports_the_pnl(rig):
    server, ledger, _ = rig
    ledger.post(WALLET, OWNER, INR(100), ref="seed")
    ledger.post(COGS, WALLET, INR(10), ref="o1")
    ledger.record_sale(INR(20), Money.zero(), Money.zero(), ref="o1")
    code, body = server.metrics()
    assert code == 200
    assert body["revenue"] == "20.00"
    assert body["cogs"] == "10.00"
    assert body["net_profit"] == "10.00"


# ------------------------------------------------------------- real HTTP
@pytest.fixture()
def live_server(rig):
    """Serve on an ephemeral port for a genuine end-to-end HTTP test."""
    server, ledger, provider = rig
    srv = HealthServer(server.engine, ledger, port=0, cache_seconds=0.0,
                       metrics_token=METRICS_TOKEN)
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv._handler_class())
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", srv
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_http_healthz(live_server):
    base, _ = live_server
    code, body = _get(f"{base}/healthz")
    assert code == 200 and body["status"] == "ok"


def test_http_metrics_over_a_real_socket(live_server):
    """This is the request that returned 500 before the thread fix."""
    base, srv = live_server
    code, body = _get(f"{base}/metrics?token={METRICS_TOKEN}")
    assert code == 200, body
    assert "net_profit" in body


def test_http_metrics_accepts_a_bearer_header(live_server):
    base, _ = live_server
    req = urllib.request.Request(
        f"{base}/metrics",
        headers={"Authorization": f"Bearer {METRICS_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        assert "net_profit" in json.loads(resp.read().decode())


def test_http_metrics_is_403_with_the_wrong_token(live_server):
    base, _ = live_server
    code, body = _get(f"{base}/metrics?token=wrong-token")
    assert code == 403
    assert body["error"] == "unauthorised"


def test_http_metrics_is_403_without_a_token(live_server):
    base, _ = live_server
    code, body = _get(f"{base}/metrics")
    assert code == 403
    assert body["error"] == "unauthorised"


def test_http_metrics_is_404_when_no_token_is_configured(rig):
    """No token configured -> the route does not exist at all."""
    server, ledger, provider = rig
    from http.server import ThreadingHTTPServer

    anon = HealthServer(server.engine, ledger, port=0, cache_seconds=0.0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), anon._handler_class())
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        code, body = _get(f"{base}/metrics?token=anything")
        assert code == 404
        assert "no route" in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_readyz_over_a_real_socket(live_server):
    base, _ = live_server
    code, body = _get(f"{base}/readyz")
    assert code == 200, body
    assert body["ledger"] == "balanced"


def test_http_unknown_route_is_404(live_server):
    base, _ = live_server
    code, body = _get(f"{base}/nope")
    assert code == 404 and "no route" in body["error"]


def test_http_root_lists_endpoints(live_server):
    base, _ = live_server
    code, body = _get(base)
    assert code == 200
    assert "/healthz" in body["endpoints"]


def test_http_concurrent_requests_do_not_error(live_server):
    base, _ = live_server
    url = f"{base}/metrics?token={METRICS_TOKEN}"
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: _get(url)[0], range(36)))
    assert results == [200] * 36


# ------------------------------------------------------------- poller
def test_poller_crash_does_not_kill_the_server(rig):
    server, _, _ = rig
    calls = {"n": 0}

    def flaky_poller():
        calls["n"] += 1
        raise RuntimeError("poller exploded")

    server._poller = flaky_poller
    server._cache_seconds = 0.0
    server.start_poller()
    # Give the supervised poller time to crash and be restarted.
    import time
    time.sleep(0.2)
    assert server._poller_thread.is_alive()
    code, _ = server.liveness()
    assert code == 200
    server._poller_thread = None  # stop the restart loop


def test_no_poller_is_allowed(rig):
    """Without a Telegram token the service still serves HTTP."""
    server, _, _ = rig
    server._poller = None
    server.start_poller()
    code, body = server.liveness()
    assert code == 200
    assert body["poller_alive"] is None


# -- FamGateway webhook over a real socket --------------------------------
def _post(url, data, signature=None):
    req = urllib.request.Request(url, data=data, method="POST")
    if signature is not None:
        req.add_header("X-FamGateway-Signature", signature)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_http_famgateway_webhook_credit_and_404(rig):
    """POST /webhooks/famgateway credits once and only with a valid signature.

    No webhook configured -> 404. Configured -> verifies signature, credits,
    and is idempotent across a duplicated body.
    """
    import hashlib, hmac
    import threading
    from http.server import ThreadingHTTPServer

    from uotpbot.wallets import SqliteWallets
    from uotpbot import __main__ as mm

    server, ledger, provider = rig
    wallets = SqliteWallets(":memory:")
    wallets.kv_set("fg_order:fg_SOCK", "222")
    wallets.kv_set("fg_amt:fg_SOCK", "50")

    class _Settings:
        famgateway_api_key = "sock_key"
        famgateway_base_url = "https://famgateway.in"

    handler = mm._famgateway_webhook(_Settings(), wallets)
    srv = HealthServer(server.engine, ledger, port=0, cache_seconds=0.0,
                       famgateway_webhook=handler)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv._handler_class())
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        body = json.dumps({"event": "payment.success",
                           "order_id": "fg_SOCK", "amount": 50}).encode()
        sig = hmac.new(b"sock_key", body, hashlib.sha256).hexdigest()
        # Correct signature credits.
        code, payload = _post(f"{base}/webhooks/famgateway", body, sig)
        assert code == 200 and payload["status"] == "credited"
        assert wallets.balance("222") == INR(50)
        # Duplicate -> idempotent.
        code2, payload2 = _post(f"{base}/webhooks/famgateway", body, sig)
        assert code2 == 200 and payload2["status"] == "already_credited"
        assert wallets.balance("222") == INR(50)
        # Wrong signature rejected.
        code3, payload3 = _post(f"{base}/webhooks/famgateway", body, "00")
        assert code3 == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
