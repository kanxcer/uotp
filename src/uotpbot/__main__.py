"""``python -m uotpbot`` -- start the bot, or fall back to the CLI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .catalog import load_catalog
from .cli import main as cli_main
from .config import ConfigError, from_environment
from .engine import BotEngine
from .ledger import Ledger
from .pricing import Pricer
from .provider.uotp import UotpProvider


def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        settings = from_environment()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    catalog = load_catalog(Path(settings.prices_path) if settings.prices_path else None)
    provider = UotpProvider(settings.uotp)
    try:
        balance = provider.probe()
    except Exception as exc:
        print(f"provider check failed: {exc}", file=sys.stderr)
        return 2
    print(f"connected to {provider.name}; wallet {balance.credit}")

    ledger = Ledger(settings.ledger_path)
    pricer = Pricer(catalog, fees=settings.fees)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       fees=settings.fees, config=settings.engine)

    from .bot.commands import CommandRouter

    def factory() -> CommandRouter:
        return CommandRouter(
            engine, catalog, pricer, ledger,
            owner_id=settings.owner_id, allowed_users=settings.allowed_users,
        )

    if not settings.has_telegram:
        print("TELEGRAM_BOT_TOKEN not set; running the CLI instead.", file=sys.stderr)
        return cli_main(sys.argv[1:] or ["prices"])

    from .bot.telegram import run_bot

    run_bot(settings, factory)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
