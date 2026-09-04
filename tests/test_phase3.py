"""TITAN v2.0 Phase 3: the six reported bug fixes.

Each test pins one fix so a regression is caught immediately:
  #1 accurate 2-min cancel cooldown (not the OTP window)
  #2 multiple OTPs on one number during its validity window
  #3 quick navigation never queues behind a long OTP wait (two pools)
  #4 guaranteed, durable refund on cancel (outbox + ledger line)
  #5 admin screens always carry a Back button
  #6 the persistent bottom menu routes taps to the right screen
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI, REPLY_MENU_LABELS_LOW, WELCOME
from uotpbot.tz import format_ts
from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer
from uotpbot.provider.base import NumberAllocation, ProviderError
from uotpbot.provider.mock import MockOutcome, MockProvider
from uotpbot.wallets import SqliteWallets

OWNER = "111"
USER = "222"


def _catalog():
    return Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))


class _DenyCancelProvider(MockProvider):
    """A provider whose cancel is REFUSED (EARLY_CANCEL_DENIED phantom)."""

    def cancel_strict(self, order_id: str):
        raise ProviderError("EARLY_CANCEL_DENIED")


def _make_router(catalog, ledger, pricer, provider, wallets):
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=1, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           wallets=wallets)
    # Credit the customer so a buy can proceed (dict mode or a real store).
    if wallets is not None:
        wallets.adjust(USER, INR(500))
    else:
        router.balances[USER] = INR(500)
    return router


def _rig(wallets=None, deny_cancel=False):
    catalog = _catalog()
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = _DenyCancelProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(5000), seed=7) if deny_cancel else MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(5000), seed=7)
    router = _make_router(catalog, ledger, pricer, provider, wallets)
    return router, provider, ledger, catalog


def _wait_token(reply):
    """Pull the co:<token> from a reply's rows."""
    for row in getattr(reply, "rows", ()) or ():
        for _label, data in row:
            if isinstance(data, str) and data.startswith("co:"):
                return data.split(":", 1)[1]
    return None


# ── #1 accurate 2-minute cancel cooldown ──────────────────────────────────
def test_cancel_cooldown_uses_2min_not_otp_window():
    router, provider, _, _ = _rig(deny_cancel=True)
    provider.force_next(MockOutcome("success", otp="555555"))
    r = router.alloc_and_wait(USER, "telegram")
    assert r.ok
    token = _wait_token(r)
    reply = router.cancel_wait(token)
    # Provider refused => we must show the real 2-min cooldown, not the OTP
    # window (~1s here, which the old code rendered as "less than a minute").
    assert "2 min" in reply.text
    assert "Check OTP" in reply.text


def test_cancel_cooldown_left_matches_allocation():
    router, _, _ledger, _catalog = _rig()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alloc = NumberAllocation(order_id="x", phone="+919000000000", service="telegram",
                             country="22", charged=INR(10), allocated_at=now_iso)
    left = router._cancel_cooldown_left(alloc)
    assert left is not None and 115 < left <= 120.5


# ── #2 multiple OTPs on one number ────────────────────────────────────────
def test_new_otp_delivered_on_same_number():
    wallets = SqliteWallets(":memory:")
    router, provider, _ledger, _catalog = _rig(wallets=wallets)
    provider.force_sequence([
        MockOutcome("success", otp="111111"),
        MockOutcome("success", otp="222222"),
    ])
    r = router.alloc_and_wait(USER, "telegram")
    token = _wait_token(r)
    _terminal, first = router.poll_once(token)
    assert first.ok and "111111" in first.text
    assert router.active_valid_remaining(token) > 0  # kept alive for more

    m = router.poll_new_otp(token)
    assert m.ok and "222222" in m.text and "New OTP" in m.text

    # A stale re-check must simply say no new code, not re-show the last one.
    m2 = router.poll_new_otp(token)
    assert m2.ok and "No new code" in m2.text


def test_new_otp_reports_no_code_while_valid():
    wallets = SqliteWallets(":memory:")
    router, provider, _, _ = _rig(wallets=wallets)
    provider.force_sequence([
        MockOutcome("success", otp="333333"),
        MockOutcome("silent"),
    ])
    r = router.alloc_and_wait(USER, "telegram")
    token = _wait_token(r)
    router.poll_once(token)
    m = router.poll_new_otp(token)
    assert m.ok and "No new code" in m.text


# ── #3 quick navigation never queues behind a long wait ──────────────────
def test_separate_fast_and_slow_executors():
    from uotpbot.bot import telegram as tg

    fast = tg._get_executor()
    slow = tg._get_slow_executor()
    assert fast is not slow
    # The slow pool is sized independently, so long waits can't starve fast nav.
    assert slow._max_workers >= 8


# ── #4 guaranteed refund on cancel (outbox + ledger + retry) ─────────────
def test_cancel_refund_is_durable_and_ledgered():
    wallets = SqliteWallets(":memory:")
    router, provider, ledger, _ = _rig(wallets=wallets)
    provider.force_next(MockOutcome("success", otp="444444"))
    initial = wallets.balance(USER)  # ₹500 after the fixture credit
    r = router.alloc_and_wait(USER, "telegram")
    token = _wait_token(r)

    reply = router.cancel_wait(token)
    assert reply.ok and "Cancelled" in reply.text
    # Money really came back to the customer (spent, then refunded -> initial).
    assert wallets.balance(USER) == initial
    # ...the outbox recorded it as done...
    row = wallets.get_refund(token)
    assert row is not None and row.status == "done" and row.amount.paise > 0
    # ...and the ledger got a customer-refund line (P&L stays honest).
    refund_line = [h for h in ledger.history()
                   if h[1] == token or "cancel" in (h[5] or "").lower()]
    assert refund_line


def test_refund_retry_credits_a_pending_refund():
    wallets = SqliteWallets(":memory:")
    router, provider, ledger, _ = _rig(wallets=wallets)
    # Simulate a refund whose first credit failed: a pending outbox row that the
    # retry worker's sweep must pick up and credit.
    wallets.write_refund(user_id=USER, amount=INR(10), order_token="ord-123",
                         reason="user_cancelled")
    n = router.refunds.retry_all()
    assert n == 1
    assert wallets.balance(USER) == INR(510)  # 500 seed + 10 refund
    assert wallets.get_refund("ord-123").status == "done"


# ── #5 admin screens always have a Back button ───────────────────────────
def test_admin_action_screen_carries_back():
    router, _, _, _ = _rig(wallets=SqliteWallets(":memory:"))
    ui = MenuUI(router, pay_upi_id="")
    reply = ui.button(OWNER, "ax:metrics")
    data = [d for row in reply.rows for _l, d in row] + \
           [d for _l, d in getattr(reply, "buttons", ())]
    assert "a" in data  # ◀️ Owner panel back navigation is always present


def test_owner_panel_reaches_home():
    router, _, _, _ = _rig(wallets=SqliteWallets(":memory:"))
    ui = MenuUI(router, pay_upi_id="")
    panel = ui.button(OWNER, "a")
    assert any(d == "m" for row in panel.rows for _l, d in row)


# ── #6 persistent bottom menu routes taps ────────────────────────────────
def test_persistent_menu_labels_route():
    router, _, _, _ = _rig()
    ui = MenuUI(router, pay_upi_id="")
    assert "🛒 Buy number".lower() in REPLY_MENU_LABELS_LOW
    assert "🧾 My numbers".lower() in REPLY_MENU_LABELS_LOW
    # Tapping "💰 Wallet" from the persistent bar opens the wallet screen.
    reply = ui.text(USER, "💰 Wallet")
    assert "Your balance" in reply.text
    # Tapping "🧾 My Numbers" opens the history screen (case-insensitive).
    reply = ui.text(USER, "🧾 My Numbers")
    assert "No live numbers" in reply.text or "No orders yet" in reply.text


# ── Welcome copy replaced (no "buttons at the bottom") ────────────────────
def test_welcome_mentions_buy_or_type():
    assert "buttons at the bottom" not in WELCOME
    assert "type the service name" in WELCOME


# ── Wallet transaction history shows active number + top-ups ─────────────
def test_wallet_history_has_transaction_button():
    wallets = SqliteWallets(":memory:")
    router, provider, _, _ = _rig(wallets=wallets)
    ui = MenuUI(router, pay_upi_id="")
    provider.force_next(MockOutcome("success", otp="987654"))
    r = ui.button(USER, "y:telegram")
    r.deferred(USER)  # buy -> allocates the number
    wh = ui.button(USER, "tx")
    assert "Transaction history" in wh.text
    assert "Active purchase" in wh.text
    assert "Telegram" in wh.text
    # A top-up appears as a credit.
    tid = wallets.create_topup(USER, INR(100))
    wallets.decide_topup(tid, "approved", decided_by=OWNER)
    wh2 = ui.button(USER, "tx")
    assert "Credits" in wh2.text and "₹100.00" in wh2.text


# ── Timezone is Asia/Kolkata ─────────────────────────────────────────────
def test_timestamps_render_in_ist():
    import time
    s = format_ts(time.time())  # sample real epoch
    assert "IST" in s
    assert "+05:30" not in s  # human label, not offset
    # A hand-crafted epoch should show IST wall clock (UTC+5:30). 2026-01-01
    # 00:00:00 UTC = 05:30:00 IST.
    import calendar
    epoch = calendar.timegm((2026, 1, 1, 0, 0, 0))
    assert "01 Jan 2026 · 05:30:00 IST" == format_ts(epoch)


# ── Exact cancel cooldown wording ────────────────────────────────────────
def test_exact_duration_wording():
    assert CommandRouter._exact_duration(70) == "1 minute 10 seconds"
    assert CommandRouter._exact_duration(10) == "10 seconds"
    assert CommandRouter._exact_duration(120) == "2 minutes"
    assert CommandRouter._exact_duration(61) == "1 minute 1 second"


def test_cancel_message_shows_exact_time_left():
    wallets = SqliteWallets(":memory:")
    catalog = _catalog()
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(5000), seed=7)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=1, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           wallets=wallets)
    wallets.adjust(USER, INR(500))
    provider.force_next(MockOutcome("success", otp="111111"))
    r = router.alloc_and_wait(USER, "telegram")
    token = _wait_token(r)
    # For a pretend provider that refuses early cancels, the message must give
    # an EXACT cooldown, not a vague "~4 min".
    from uotpbot.provider.base import ProviderError
    provider.cancel_strict = lambda oid: (_ for _ in ()).throw(ProviderError("EARLY_CANCEL_DENIED"))
    rep = router.cancel_wait(token)
    assert "You can cancel in" in rep.text
    assert "minute" in rep.text or "seconds" in rep.text
