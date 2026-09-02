"""Payment top-ups: the UPI + screenshot + approve/decline flow.

Money rules under test: nothing credits before the owner approves; an owner
cannot approve twice; a stranger cannot approve at all; the customer is
notified; everything survives a store reopen; sub-bot owners see only their
own bot's payments.
"""

from __future__ import annotations

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider
from uotpbot.wallets import ScopedWallets, SqliteWallets

OWNER = "111"
USER = "222"
OTHER = "333"


@pytest.fixture()
def rig(tmp_path):
    from decimal import Decimal

    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94")),
    })
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"telegram": INR(10)}, balance=INR(5000), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(otp_timeout_seconds=0.5, poll_interval=0.01))
    store = SqliteWallets(str(tmp_path / "w.db"))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER, OTHER),
                           wallets=store)
    ui = MenuUI(router, pay_upi_id="shop@okaxis")
    yield ui, router, store
    store.close()


def complete_topup(ui, user=USER, amount_text="200", fid="FILE-ID-ABC"):
    ui.button(user, "w")
    ui.button(user, "t")
    ui.button(user, "t:paid")
    ui.text(user, amount_text)
    r = ui.photo(user, fid)
    return r


# -- happy path --------------------------------------------------------------

def test_full_flow_credits_only_after_approval(rig):
    ui, router, store = rig
    r = complete_topup(ui)
    assert r.ok and "submitted" in r.text
    assert router.balance_of(USER).paise == 0  # nothing before approval
    # Owner got pinged with id and amount; screenshot flagged for forwarding.
    assert r.notify and r.notify[0][0] == OWNER and "200" in r.notify[0][1]
    assert r.forward_photo
    pending = store.pending_topups()
    assert len(pending) == 1 and pending[0].amount.paise == INR(200).paise
    # Approve via the admin screen.
    dec = ui.button(OWNER, f"ap:{pending[0].id}")
    assert dec.ok and "credited" in dec.text
    assert router.balance_of(USER).paise == INR(200).paise
    # Customer was told, with their fresh balance.
    assert dec.notify[0][0] == USER and "approved" in dec.notify[0][1].lower()
    assert store.pending_topups() == []


def test_decline_moves_no_money_and_tells_the_customer(rig):
    ui, router, store = rig
    complete_topup(ui)
    tid = store.pending_topups()[0].id
    dec = ui.button(OWNER, f"ad:{tid}")
    assert dec.ok
    assert router.balance_of(USER).paise == 0
    assert "could not" in dec.notify[0][1]
    assert store.get_topup(tid).status == "declined"


# -- money-safety rails -------------------------------------------------------

def test_double_approve_credits_once(rig):
    ui, router, store = rig
    complete_topup(ui)
    tid = store.pending_topups()[0].id
    ui.button(OWNER, f"ap:{tid}")
    again = ui.button(OWNER, f"ap:{tid}")
    assert not again.ok
    assert router.balance_of(USER).paise == INR(200).paise


def test_strangers_cannot_approve(rig):
    ui, router, store = rig
    complete_topup(ui)
    tid = store.pending_topups()[0].id
    for stranger in (USER, OTHER):
        assert not ui.button(stranger, f"ap:{tid}").ok
        assert not ui.button(stranger, f"ad:{tid}").ok
    assert router.balance_of(USER).paise == 0
    assert store.pending_topups()  # still pending


def test_non_owner_cannot_open_admin_screens(rig):
    ui, *_ = rig
    for tap in ("a", "a:t", "a:qr"):
        assert not ui.button(USER, tap).ok, tap


@pytest.mark.parametrize("bad", ["abc", "", "0", "-20", "9", "100001", "12.345", "₹₹"])
def test_amount_validation_rejects_garbage(rig, bad):
    ui, router, store = rig
    ui.button(USER, "t"); ui.button(USER, "t:paid")
    assert not ui.text(USER, bad).ok
    ui.photo(USER, "NOPE")  # photo anyway -> must not create a topup
    assert store.pending_topups() == []


@pytest.mark.parametrize("text,rupees", [("200", 200), ("₹500", 500), ("1,000", 1000),
                                          ("Rs 750", 750), ("10", 10), ("INR 99.50", 99)])
def test_amount_parsing_accepts_human_input(rig, text, rupees):
    ui, router, store = rig
    ui.button(USER, "t"); ui.button(USER, "t:paid")
    r = ui.text(USER, text)
    assert r.ok
    r2 = ui.photo(USER, "FID")
    assert store.pending_topups()[0].amount.paise == rupees * 100 + (50 if "." in text else 0)
    _ = r2


def test_opening_another_menu_cancels_the_wizard(rig):
    ui, router, store = rig
    ui.button(USER, "t"); ui.button(USER, "t:paid"); ui.text(USER, "200")
    ui.button(USER, "m")  # wander off
    uninvited = ui.photo(USER, "FID")
    assert not uninvited.ok
    assert store.pending_topups() == []


# -- QR -----------------------------------------------------------------------

def test_owner_sets_qr_and_customers_see_it(rig):
    ui, router, store = rig
    ui.button(OWNER, "a:qr")
    saved = ui.photo(OWNER, "QR-FILE-ID")
    assert saved.ok and "saved" in saved.text
    assert store.kv_get("pay_qr_file_id") == "QR-FILE-ID"
    card = ui.button(USER, "t")
    assert card.photo == "QR-FILE-ID"
    assert "shop@okaxis" in card.text


# -- persistence & isolation ----------------------------------------------------

def test_topups_and_qr_survive_reopen(tmp_path):
    db = str(tmp_path / "w.db")
    s1 = SqliteWallets(db); s1.kv_set("pay_qr_file_id", "QR1")
    tid = s1.create_topup(USER, INR(150), photo_file_id="F1"); s1.close()
    s2 = SqliteWallets(db)
    t = s2.get_topup(tid)
    assert t and t.amount.paise == INR(150).paise and t.status == "pending"
    assert s2.kv_get("pay_qr_file_id") == "QR1"
    s2.decide_topup(tid, "approved", decided_by=OWNER)
    s3 = SqliteWallets(db)
    assert s3.get_topup(tid).status == "approved"
    assert s3.decide_topup(tid, "declined", decided_by=OWNER) is False
    s2.close(); s3.close()


def test_subbot_owner_sees_only_own_scope(tmp_path):
    inner = SqliteWallets(str(tmp_path / "w.db"))
    a = ScopedWallets(inner, "bta1")
    b = ScopedWallets(inner, "btb2")
    tid_a = a.create_topup(USER, INR(50))
    tid_b = b.create_topup(USER, INR(70))
    assert [t.id for t in a.pending_topups()] == [tid_a]
    assert a.get_topup(tid_b) is None            # cross-bot read blocked
    assert a.decide_topup(tid_b, "approved", decided_by=OWNER) is False
    assert b.decide_topup(tid_b, "approved", decided_by=OWNER) is True
    a.kv_set("pay_qr_file_id", "QRA")
    assert a.kv_get("pay_qr_file_id") == "QRA" and b.kv_get("pay_qr_file_id") is None
    inner.close()
