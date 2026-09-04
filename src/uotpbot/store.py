"""Storage selection: sqlite by default, Postgres when DATABASE_URL is set.

This is the single decision point, so the rule lives in one place:

* ``DATABASE_URL`` unset -> file-backed sqlite (Ledger / SubBotRegistry),
  with a loud warning when the path sits on storage a redeploy will wipe.
* ``DATABASE_URL`` set   -> PostgresLedger / PostgresRegistry. On a host like
  Render's free tier -- where no persistent disk exists -- this is the only
  configuration in which the audit trail survives a redeploy.
"""

from __future__ import annotations

import logging

from .config import Settings
from .ledger import Ledger
from .whitelabel import SubBotRegistry

log = logging.getLogger("uotpbot.store")

__all__ = ["make_ledger", "make_registry", "make_wallets", "backend_name"]


def make_wallets(settings: Settings):
    """Customer wallet store matching the ledger's durability story.

    A paying customer's balance must survive the same redeploys the audit
    trail does, so the wallets follow DATABASE_URL exactly like the ledger.
    """
    if settings.database_url:
        from .wallets import PostgresWallets

        return PostgresWallets(settings.database_url, schema=settings.database_schema)
    from .wallets import SqliteWallets

    return SqliteWallets(settings.wallets_path)


def backend_name(settings: Settings) -> str:
    if settings.database_url:
        return f"postgres (schema {settings.database_schema})"
    return f"sqlite ({settings.ledger_path})"


def make_ledger(settings: Settings) -> Ledger:
    """The ledger backend implied by the configuration."""
    if settings.database_url:
        from .pgstore import PostgresLedger

        log.info("ledger: postgres schema %s", settings.database_schema)
        return PostgresLedger(settings.database_url, schema=settings.database_schema)
    log.info("ledger: sqlite %s", settings.ledger_path)
    return Ledger(settings.ledger_path)


def make_registry(settings: Settings) -> SubBotRegistry:
    """The sub-bot registry backend implied by the configuration.

    Deliberately follows the ledger: registry and ledger must share one
    durability story, because they are two halves of the same promise.
    """
    if settings.database_url:
        from .pgstore import PostgresRegistry

        return PostgresRegistry(settings.database_url, schema=settings.database_schema,
                                secret_key=settings.secret_key)
    return SubBotRegistry(settings.subbots_path, secret_key=settings.secret_key)
