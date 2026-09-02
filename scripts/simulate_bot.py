"""Drive the full /createbot conversation through CommandRouter.handle().

This exists because the 2026-09-02 incident: every flow test called the
CreateBotFlow directly, all stayed green, while handle() swallowed plain-text
messages into HELP_TEXT and the feature was unreachable through the real bot.
This script drives the ONLY path a real Telegram message takes: handle().

Usage (token comes from the environment -- never hardcode a token in the
repo; one was committed once and had to be history-rewritten):

    TELEGRAM_BOT_TOKEN='123:ABC...' python scripts/simulate_bot.py
"""

from __future__ import annotations

import os
import re
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uotpbot.bot.commands import CommandRouter  # noqa: E402
from uotpbot.catalog import Catalog, ServiceCost, WalletPack  # noqa: E402
from uotpbot.engine import BotEngine, EngineConfig  # noqa: E402
from uotpbot.economics import FeeModel  # noqa: E402
from uotpbot.ledger import CASH, COGS, PLATFORM_FEE, SALES, Ledger  # noqa: E402
from uotpbot.money import INR  # noqa: E402
from uotpbot.pricing import Pricer  # noqa: E402
from uotpbot.provider.mock import MockProvider  # noqa: E402
from uotpbot.whitelabel import SubBotRegistry  # noqa: E402

GOOD_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "123456789:AAF1234567890abcdefghijklmnopqrstuvw")
OWNER = "111"


def build_router(registry: SubBotRegistry):
    catalog = Catalog(
        {
            "telegram": ServiceCost(
                "telegram", "Telegram", "messaging", INR(10),
                Decimal("0.94"), Decimal("0.04"), Decimal("0.95"),
            ),
        },
        (WalletPack("Pro", INR(1000), INR(1150)),),
    )
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(5000), seed=5,
    )
    engine = BotEngine(
        catalog, provider, ledger, pricer, fees=FeeModel(Decimal("0.02")),
        config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0, poll_interval=0.01),
    )
    return CommandRouter(
        engine, catalog, pricer, ledger,
        owner_id=OWNER, allowed_users=(OWNER,), subbots=registry,
    ), ledger


def run() -> int:
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,40}", GOOD_TOKEN):
        print("token from TELEGRAM_BOT_TOKEN does not look valid; aborting")
        return 2
    registry = SubBotRegistry()
    router, ledger = build_router(registry)
    steps = [
        ("/createbot", "ask for token"),
        (GOOD_TOKEN, "paste token"),
        ("2", "type own-API choice (no buttons)"),
        ("live-provider-key-abcdef123456", "paste provider key"),
        ("yes", "accept terms"),
    ]
    for text, label in steps:
        reply = router.handle(OWNER, text)
        print(f"\n[{label}]")
        print("\n".join("  " + line for line in reply.text.splitlines()[:4]))
        if reply.buttons:
            print(f"  (buttons: {len(reply.buttons)})")
        if "5%" not in reply.text and label == "paste provider key":
            print("  !! fee not disclosed before confirmation")
            return 1
    bots = registry.for_owner(OWNER)
    if len(bots) != 1 or not bots[0].active:
        print("\nFAIL: sub-bot not registered+active")
        return 1
    bot = bots[0]
    print(f"\ncreated: {bot.id} mode={bot.mode.value} fee={bot.fee.describe()}")
    if "live-provider-key" in router.handle(OWNER, "/mybots").text:
        print("FAIL: provider key leaked into chat")
        return 1

    # and the economics of the sub-bot's own sale, split with the fee
    sub_ledger = Ledger()
    gross, fee, gateway = INR(100), INR(5), INR(2)
    sub_ledger.record_sale_split(gross, gateway, INR(0), fee, ref="wl-1")
    sub_ledger.record_number_purchase(INR(10), ref="wl-1")
    sub_ledger.verify()
    assert sub_ledger.balance(SALES) == gross - fee, "owner revenue = gross - platform fee"
    assert sub_ledger.balance(PLATFORM_FEE) == fee
    assert sub_ledger.balance(CASH) == gross - gateway
    assert sub_ledger.balance(COGS) == INR(10)
    pnl = sub_ledger.profit_and_loss()
    print(f"owner P&L on a Rs.100 sale: revenue {pnl.revenue}, cogs {pnl.cogs}, "
          f"net {pnl.net_profit}; platform {fee}")
    print("\nALL GOOD")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
