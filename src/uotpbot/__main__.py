"""``python -m uotpbot`` entry point.

Modes::

    python -m uotpbot serve     HTTP server on $PORT + Telegram poller (deploy)
    python -m uotpbot check     verify config and provider, then exit (CI/smoke)
    python -m uotpbot <cmd>     anything else is forwarded to the CLI

``serve`` is what a platform Web Service should run: it binds ``$PORT``,
answers ``/healthz`` and ``/readyz``, and runs the Telegram poller in a
background thread.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .whitelabel import MultiBotManager, SubBotRegistry

from .cli import main as cli_main
from .config import ConfigError, Settings, from_environment
from .engine import BotEngine
from .money import Rate
from .provider.base import ProviderError, ServiceUnavailable
from .pricing import Pricer
from .store import backend_name, make_ledger, make_registry

log = logging.getLogger("uotpbot")

#: Paths that are wiped on every deploy on platforms like Render. A sqlite
#: ledger stored here is silently destroyed by the next release, which means
#: the audit trail -- and any unreconciled order -- disappears.
EPHEMERAL_PREFIXES = ("/opt/render", "/var/task", "/tmp", "/app")


def _warn_if_ephemeral(path: str, *, var: str = "LEDGER_PATH") -> None:
    """Shout when a database sits on storage that a redeploy will wipe."""
    resolved = str(Path(path).resolve())
    if any(resolved.startswith(p) for p in EPHEMERAL_PREFIXES):
        log.warning(
            "%s=%s is on ephemeral storage and WILL BE WIPED on the next "
            "deploy. For the ledger that means the whole audit trail; for the "
            "sub-bot registry it means every white-label bot and the fee terms "
            "its owner agreed to. Attach a persistent disk and point %s at it "
            "(e.g. /var/data/), or move to a managed database.",
            var, resolved, var,
        )


def _build(settings: Settings):
    """Wire up catalogue, provider, ledger, pricer and engine."""
    from .catalog import load_catalog
    from .provider.uotp import UotpProvider

    catalog = load_catalog(Path(settings.prices_path) if settings.prices_path else None)
    provider = UotpProvider(settings.uotp)
    ledger = make_ledger(settings)
    if not settings.database_url:
        # With Postgres the durability story is the database's, not the disk's.
        _warn_if_ephemeral(settings.ledger_path)
    from .pricing import Strategy
    pricer = Pricer(
        catalog, fees=settings.fees,
        target_margin=Rate(str(settings.pricing_target_margin)),
        strategy=Strategy(settings.pricing_strategy),
        markup=Rate(str(settings.pricing_markup_rate)),
    )
    engine = BotEngine(
        catalog, provider, ledger, pricer, fees=settings.fees, config=settings.engine
    )
    return catalog, provider, ledger, pricer, engine


def _make_poller(settings: Settings, router_factory, *, owner_alert=None):
    """Return a callable that runs the Telegram poller, or None."""
    if not settings.has_telegram:
        log.warning(
            "TELEGRAM_BOT_TOKEN is not set: serving HTTP only, no bot. "
            "Set it to accept orders."
        )
        return None
    from .bot.telegram import HAS_TELEGRAM, run_bot

    if not HAS_TELEGRAM:
        log.error(
            "python-telegram-bot is not installed, so no bot will run. "
            "Install with: pip install '.[telegram]'"
        )
        return None
    return lambda: run_bot(settings, router_factory, owner_alert=owner_alert)


def _credit_fg_wallet(store, uid: str, order_id: str, amount_dec) -> bool:
    """Atomically credit ``amount_dec`` rupees to ``uid`` and mark the order
    credited. Returns True on success. Idempotent: callers check the
    ``fg_credited:<order>`` marker first; the marker is (re)set here so two
    paths (webhook + sweep) can never double-credit."""
    from decimal import Decimal
    from .money import Money

    try:
        money = Money(int(amount_dec * Decimal(100)))
        store.adjust(f"{uid}", money)
        set_ = getattr(store, "kv_set", None)
        if callable(set_):
            set_(f"fg_credited:{order_id}", "1")
    except Exception as exc:  # noqa: BLE001
        log.error("FamGateway credit failed for %s: %s", order_id, exc)
        return False
    log.info("FamGateway credited %s to user %s (order %s)", money, uid, order_id)
    return True


def _live_fg_key(store, env_key: str) -> str:
    """The live FamGateway API key: kv override wins over the env default."""
    get_ = getattr(store, "kv_get", None)
    if callable(get_):
        try:
            return (get_("famgateway_api_key") or "").strip() or env_key
        except Exception:  # noqa: BLE001
            return env_key
    return env_key


def _famgateway_sweep(store, key: str, base_url: str) -> None:
    """Verify every open (uncredited) order once and credit any that is paid.

    Safety net for the case where a payment webhook is dropped, missed, or the
    customer pays without the push ever arriving. Each pass scans the
    ``fg_order:<id>`` mappings and checks the gateway; paid orders are credited
    through ``_credit_fg_wallet`` (idempotent, same marker the webhook uses).
    Orders that fail to verify are left for the next pass, so a transient
    gateway hiccup never loses a payment.
    """
    from .gateway import FamGateway

    scan = getattr(store, "kv_scan", None)
    if not callable(scan):
        return
    get_ = getattr(store, "kv_get", None)
    if not callable(get_):
        return
    try:
        gateway = FamGateway(key, base_url=base_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not build FamGateway client for sweep: %s", exc)
        return
    for order_key, uid in scan("fg_order:").items():
        if not uid:
            continue
        order_id = order_key.split("fg_order:", 1)[-1]
        if get_(f"fg_credited:{order_id}"):
            continue
        amt_s = get_(f"fg_amt:{order_id}")
        if not amt_s:
            continue
        try:
            status = gateway.verify(order_id)
        except Exception as exc:  # noqa: BLE001 - leave for next pass
            log.warning("sweep verify failed for %s: %s", order_id, exc)
            continue
        if status.is_paid:
            try:
                from decimal import Decimal
                amt = Decimal(amt_s)
            except Exception:  # noqa: BLE001
                continue
            _credit_fg_wallet(store, uid, order_id, amt)


def _start_fg_sweeper(settings: Settings, wallets, stop: threading.Event,
                      *, interval: float = 60.0) -> Optional[threading.Thread]:
    """Start the background FamGateway sweep thread, or return None.

    Returns None when there is no live API key (nothing to poll) or the
    interval is <= 0. The thread loops: read the live key each pass (so a key
    set live via the admin panel is honoured), sweep open orders, sleep.
    """
    base_url = getattr(settings, "famgateway_base_url",
                       "https://famgateway.in")

    def run() -> None:
        while not stop.is_set():
            key = _live_fg_key(wallets, getattr(settings, "famgateway_api_key", ""))
            if key:
                try:
                    _famgateway_sweep(wallets, key, base_url)
                except Exception as exc:  # noqa: BLE001 - never kill the thread
                    log.warning("FamGateway sweep failed: %s", exc)
            if interval <= 0:
                return
            stop.wait(interval)

    thread = threading.Thread(target=run, name="fg-sweep", daemon=True)
    thread.start()
    log.info("FamGateway sweep started (every %ss)", interval)
    return thread


def _famgateway_webhook(settings: Settings, wallets):
    """Build the FamGateway webhook handler.

    Always registered, so ``POST /webhooks/famgateway`` exists even when the
    owner configures the key live (admin panel -> 💳 FamGateway) rather than
    in the env. The live key is read from the wallet kv table on every request
    (falling back to the env default), so a runtime-configured key is honoured
    without a redeploy.

    Credits a paid order idempotently, keyed on the order id. Uses the same
    kv table the bot writes order->user+amount mappings into, so a webhook
    that arrives after a redeploy still credits the right wallet. ``adjust``
    is atomic; the ``fg_credited:<order>`` marker is the single-flight lock
    against duplicate webhooks / retries.
    """
    env_key = getattr(settings, "famgateway_api_key", "")
    base_url = getattr(settings, "famgateway_base_url",
                       "https://famgateway.in")
    store = wallets  # the wallet store has kv_get/kv_set + adjust

    get_ = getattr(store, "kv_get", None)
    kv_ok = callable(get_)

    def handle(raw_body: bytes, signature: str, _content_type: str = ""):
        # Resolve the live API key: kv override wins over the env default,
        # matching exactly what the Add Money flow screens show the customer.
        key = ""
        if kv_ok:
            try:
                key = (get_("famgateway_api_key") or "").strip()
            except Exception:  # noqa: BLE001 - fall back to env
                key = ""
        if not key:
            key = env_key
        if not key:
            log.warning("FamGateway webhook but no API key configured")
            return 400, {"status": "not_configured"}

        from .gateway import verify_webhook_signature
        if not verify_webhook_signature(raw_body, signature, key):
            log.warning("FamGateway webhook rejected: bad signature")
            return 401, {"status": "invalid_signature"}
        try:
            import json as _json
            payload = _json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return 400, {"status": "bad_json"}
        order_id = str(payload.get("order_id", "") or "")
        event = payload.get("event", "")
        if event != "payment.success" or not order_id:
            # Acknowledge-but-ignore (refund/expire etc.) so the gateway stops
            # retrying; never credit on a non-success event.
            return 200, {"status": "acknowledged"}
        set_ = getattr(store, "kv_set", None)
        if not kv_ok or not callable(set_):
            log.error("wallet store has no kv interface; cannot credit webhook")
            return 200, {"status": "no_store"}
        if get_(f"fg_credited:{order_id}"):
            return 200, {"status": "already_credited"}
        uid = get_(f"fg_order:{order_id}")
        amt_s = get_(f"fg_amt:{order_id}")
        if not uid or not amt_s:
            log.error(
                "FamGateway webhook for %s but no order mapping; amount %s "
                "needs manual credit", order_id, payload.get("amount"))
            return 200, {"status": "no_mapping"}
        try:
            from decimal import Decimal
            amt_dec = Decimal(amt_s)
        except Exception:  # noqa: BLE001
            return 200, {"status": "bad_amount"}
        if not _credit_fg_wallet(store, uid, order_id, amt_dec):
            return 200, {"status": "credit_error"}
        return 200, {"status": "credited"}

    return handle


def _serve(settings: Settings) -> int:
    from .web import HealthServer

    catalog, provider, ledger, pricer, engine = _build(settings)
    try:
        balance = provider.probe()
    except Exception as exc:  # noqa: BLE001 - report and continue serving
        log.error("provider probe failed: %s", exc)
        log.error(
            "Continuing to serve so /readyz can report the failure. Fix the "
            "key or endpoint, then redeploy."
        )
        balance = None
    else:
        log.info("connected to %s; wallet %s", provider.name, balance.credit)

    from .bot.commands import CommandRouter
    from .store import make_wallets

    wallets = make_wallets(settings)
    subbots = _make_whitelabel(settings, catalog, ledger, pricer, wallets)

    # One persistent router for the platform bot (sub-bots each get their own
    # inside _make_whitelabel). Holding it lets the health server read Phase-1
    # subsystem stats (rate limiter + cancel tracker) on /metrics.
    from .ratelimit import RateLimitConfig

    main_router = CommandRouter(
        engine, catalog, pricer, ledger,
        owner_id=settings.owner_id, allowed_users=settings.allowed_users,
        wallets=wallets,
        subbots=subbots.registry if subbots else None,
        platform_fee=_platform_fee(settings),
        rate_limit=RateLimitConfig(
            max_buys=settings.rate_limit_max_buys,
            window_seconds=settings.rate_limit_window,
        ),
    )

    def router_factory() -> CommandRouter:
        return main_router

    # Phase-1 provider wallet monitor (P2): alert the owner BEFORE the wallet
    # runs dry, so an order never dies to NO_BALANCE mid-bulk.
    from .bot.alerts import OwnerAlert
    from .wallet_monitor import WalletMonitor

    owner_alert = OwnerAlert(settings.owner_id)
    wallet_monitor = WalletMonitor(
        provider, notify_owner=owner_alert.send,
        check_interval=float(settings.wallet_monitor_seconds),
    )
    wallet_monitor.start()

    poller = _make_poller(settings, router_factory, owner_alert=owner_alert)
    if subbots is not None:
        started = subbots.manager.start_all()
        log.info("white-label: %d sub-bot(s) registered, started %d",
                 subbots.registry.count(), len(started))
        # `started` means "a thread was launched", not "it is working". Pollers
        # fail asynchronously, so give them a moment before reporting.
        time.sleep(1.0)
        live = subbots.manager.running()
        if subbots.manager.errors():
            log.error(
                "sub-bot pollers are not running: %s", subbots.manager.errors()
            )
        log.info("white-label: %d of %d sub-bot poller(s) alive", len(live), len(started))
        # Wire /createbot to the manager so a newly created bot goes live
        # without a redeploy.
        def _on_created(bot) -> None:
            if not subbots.manager.start(bot.id):
                log.error("could not start sub-bot %s: %s", bot.id,
                          subbots.manager.errors().get(bot.id, "unknown"))
        subbots.on_created = _on_created

    server = HealthServer(
        engine, ledger,
        poller=poller,
        subbots=subbots.manager if subbots else None,
        wallet_monitor=wallet_monitor,
        subsystem_stats=main_router.phase1_snapshot,
        metrics_token=settings.metrics_token,
        famgateway_webhook=_famgateway_webhook(settings, wallets),
    )
    # Background FamGateway sweeper: verify open orders and credit any that are
    # paid, so a dropped/missed webhook never loses a payment. Daemon thread;
    # dies with the process. Skipped entirely when no API key is available.
    sweep_stop = threading.Event()
    sweep_thread = _start_fg_sweeper(
        settings, wallets, sweep_stop,
        interval=max(0.0, settings.fg_sweep_seconds),
    )
    try:
        server.serve_forever()
    finally:
        sweep_stop.set()
        wallet_monitor.stop()
    if subbots is not None:
        subbots.manager.stop_all()
        subbots.registry.close()
    return 0


def _platform_fee(settings: Settings):
    """The disclosed fee owners will be shown, from PLATFORM_FEE_RATE."""
    from .whitelabel import PlatformFee

    return PlatformFee(rate=settings.platform_fee_rate)


@dataclass
class WhiteLabel:
    """The white-label pieces ``serve`` needs, grouped for teardown."""

    registry: "SubBotRegistry"
    manager: "MultiBotManager"
    on_created: Optional[Callable[[object], None]] = None


def _make_whitelabel(settings: Settings, catalog, ledger, pricer, wallets) -> Optional[WhiteLabel]:
    """Build the sub-bot registry and manager, or None when disabled."""
    if not settings.whitelabel_enabled:
        return None
    from .bot.commands import CommandRouter
    from .wallets import ScopedWallets
    from .whitelabel import MultiBotManager

    registry = make_registry(settings)
    if not settings.database_url:
        _warn_if_ephemeral(settings.subbots_path, var="SUBBOTS_PATH")

    def router_factory(bot):
        """One router per sub-bot.

        OWN_API bots charge the platform fee, routed through the engine so the
        split happens in the same place the sale is posted. PLATFORM_API bots
        get no fee hook at all: their platform revenue is the wholesale spread.
        """
        fee_fn = bot.fee.on if bot.mode.value == "own_api" else None
        sub_engine = BotEngine(
            catalog, _provider_for(bot, settings), ledger, pricer,
            fees=settings.fees, config=settings.engine,
            platform_fee_fn=fee_fn, fee_disclosure=bot.disclosure,
        )
        return CommandRouter(
            sub_engine, catalog, pricer, ledger,
            owner_id=bot.owner_id, allowed_users=(),
            wallets=ScopedWallets(wallets, bot.id),
        )

    manager = MultiBotManager(registry, router_factory,
                              poller_factory=lambda b, r: (lambda: _run_subbot(b, r, settings)))
    wl = WhiteLabel(registry=registry, manager=manager)
    return wl


def _provider_for(bot, settings: Settings):
    """Provider for a sub-bot: its own key in OWN_API mode, the platform's otherwise.

    Only the credential changes. The endpoint, vocabulary and unit divisor are
    inherited, because an OWN_API owner registers on the same provider -- they
    supply a key, not a different API. ``bot.provider_url`` deliberately holds
    the *signup page* shown to the owner, so it is never used as a base URL;
    doing so would point every purchase at a web form.
    """
    import dataclasses

    from .provider.uotp import UotpProvider

    if bot.mode.value == "own_api" and bot.provider_key:
        return UotpProvider(dataclasses.replace(settings.uotp, api_key=bot.provider_key))
    return UotpProvider(settings.uotp)


def _run_subbot(bot, router, settings: Settings) -> None:
    """Long-poll one sub-bot. Raises if the transport is unavailable."""
    from .bot.telegram import HAS_TELEGRAM, TelegramFrontend

    if not HAS_TELEGRAM:
        raise RuntimeError("python-telegram-bot is not installed")
    from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

    # Sub-bots get the same button UI; their router has subbots=None, so the
    # nested "Run your own bot" entry hides itself automatically.
    frontend = TelegramFrontend(router)
    app = Application.builder().token(bot.bot_token).build()
    app.add_handler(CallbackQueryHandler(frontend.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, frontend.on_message))
    app.add_handler(MessageHandler(filters.COMMAND, frontend.on_message))
    app.add_handler(MessageHandler(filters.PHOTO, frontend.on_photo))  # payments/QR
    # stop_signals=(): same reason as bot.telegram -- we run in a background
    # thread, and PTB's default signal handlers only work in the main thread.
    app.run_polling(stop_signals=())


def _check(settings: Settings) -> int:
    """Verify everything the bot needs, then exit. Good for CI and smoke tests."""
    catalog, provider, ledger, pricer, engine = _build(settings)
    problems: list[str] = []
    try:
        balance = provider.probe()
        print(f"provider   ok   wallet {balance.credit}")
        # Layer the diagnosis: auth passing does not mean orders can run.
        # getPrices is read-only and exercises the provider's database, so
        # it separates "your parameters are wrong" from "their backend is
        # down" -- exactly the distinction that decides whether to fund the
        # wallet or wait for the provider.
        get_prices = getattr(provider, "get_prices", None)
        if callable(get_prices):
            try:
                prices = get_prices()
                if prices:
                    print(f"prices     ok   {len(prices)} live entries")
                else:
                    print("prices     warn provider accepted the query but "
                          "returned no price book; catalog CSV stays in force")
            except ServiceUnavailable as exc:
                problems.append(f"provider backend down: {exc}")
                print(f"prices     DOWN auth+params passed, provider's own "
                      f"backend refused: {exc}")
            except ProviderError as exc:
                problems.append(f"prices: {exc}")
                print(f"prices     FAIL {exc}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"provider: {exc}")
        print(f"provider   FAIL {exc}")
    try:
        ledger.verify()
        print(f"ledger     ok   {backend_name(settings)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"ledger: {exc}")
        print(f"ledger     FAIL {exc}")
    print(f"catalog    ok   {len(catalog)} services, floor "
          f"{catalog.cheapest_price()}, best pack "
          f"{catalog.best_pack().label if catalog.best_pack() else 'none'}")
    book = pricer.price_book()
    unviable = pricer.loss_makers(book)
    print(f"pricing    ok   {len(book) - len(unviable)}/{len(book)} viable")
    if unviable:
        print(f"           warn unviable: {', '.join(a.service.slug for a in unviable)}")
    print(f"telegram   {'ok' if settings.has_telegram else 'not configured'}")
    if problems:
        print("\nNOT READY")
        return 1
    print("\nREADY")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "serve"

    try:
        settings = from_environment()
    except ConfigError as exc:
        # Fail fast and loudly: a silent half-started bot loses money.
        print(f"configuration error: {exc}", file=sys.stderr)
        print("Set the required variables -- see .env.example.", file=sys.stderr)
        return 2

    if mode == "serve":
        return _serve(settings)
    if mode == "check":
        return _check(settings)
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
