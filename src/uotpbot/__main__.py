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
from pathlib import Path

from .cli import main as cli_main
from .config import ConfigError, Settings, from_environment
from .engine import BotEngine
from .ledger import Ledger
from .pricing import Pricer

log = logging.getLogger("uotpbot")

#: Paths that are wiped on every deploy on platforms like Render. A sqlite
#: ledger stored here is silently destroyed by the next release, which means
#: the audit trail -- and any unreconciled order -- disappears.
EPHEMERAL_PREFIXES = ("/opt/render", "/var/task", "/tmp", "/app")


def _warn_if_ephemeral(path: str) -> None:
    resolved = str(Path(path).resolve())
    if any(resolved.startswith(p) for p in EPHEMERAL_PREFIXES):
        log.warning(
            "LEDGER_PATH=%s is on ephemeral storage and WILL BE WIPED on the "
            "next deploy, taking the entire audit trail with it. Attach a "
            "persistent disk and point LEDGER_PATH at it (e.g. "
            "/var/data/ledger.db), or move to a managed database.",
            resolved,
        )


def _build(settings: Settings):
    """Wire up catalogue, provider, ledger, pricer and engine."""
    from .catalog import load_catalog
    from .provider.uotp import UotpProvider

    catalog = load_catalog(Path(settings.prices_path) if settings.prices_path else None)
    provider = UotpProvider(settings.uotp)
    ledger = Ledger(settings.ledger_path)
    _warn_if_ephemeral(settings.ledger_path)
    pricer = Pricer(catalog, fees=settings.fees)
    engine = BotEngine(
        catalog, provider, ledger, pricer, fees=settings.fees, config=settings.engine
    )
    return catalog, provider, ledger, pricer, engine


def _make_poller(settings: Settings, router_factory):
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
    return lambda: run_bot(settings, router_factory)


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

    def router_factory() -> CommandRouter:
        return CommandRouter(
            engine, catalog, pricer, ledger,
            owner_id=settings.owner_id, allowed_users=settings.allowed_users,
        )

    server = HealthServer(
        engine, ledger,
        poller=_make_poller(settings, router_factory),
    )
    server.serve_forever()
    return 0


def _check(settings: Settings) -> int:
    """Verify everything the bot needs, then exit. Good for CI and smoke tests."""
    catalog, provider, ledger, pricer, engine = _build(settings)
    problems: list[str] = []
    try:
        balance = provider.probe()
        print(f"provider   ok   wallet {balance.credit}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"provider: {exc}")
        print(f"provider   FAIL {exc}")
    try:
        ledger.verify()
        print(f"ledger     ok   {settings.ledger_path}")
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
