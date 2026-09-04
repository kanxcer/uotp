"""White-label sub-bots: disclosed fees, correct splits, safe lifecycle.

The fee-disclosure tests are the important ones. They pin the guarantee that a
bot cannot be created without its owner having been shown the terms, and that
the rate charged is the rate agreed. Everything else is plumbing.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest

from uotpbot.catalog import Catalog, ServiceCost, WalletPack
from uotpbot.createbot import (
    CB_CONFIRM,
    CB_OWN_API,
    CB_PLATFORM,
    CreateBotFlow,
    Step,
)
from uotpbot.engine import BotEngine, EngineConfig, EngineError
from uotpbot.economics import EconomicsError
from uotpbot.ledger import CASH, PLATFORM_FEE, SALES, Ledger, LedgerError
from uotpbot.money import INR, Money
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockOutcome, MockProvider
from uotpbot.whitelabel import (
    DEFAULT_PLATFORM_FEE,
    API_SIGNUP_URL,
    MultiBotManager,
    PlatformFee,
    SubBot,
    SubBotMode,
    SubBotRegistry,
    WhiteLabelError,
    validate_bot_token,
)

GOOD_TOKEN = "123456789:AAF1234567890abcdefghijklmnopqrstuvw"
GOOD_TOKEN_2 = "987654321:AAG9876543210zyxwvutsrqponmlkjihgfedc"


# ---------------------------------------------------------------- fee policy
def test_platform_api_mode_takes_no_percentage():
    """PLATFORM_API earns through the wholesale spread, not a cut of sales.

    Charging both would be double dipping on the same rupee.
    """
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.PLATFORM_API,
                 fee=DEFAULT_PLATFORM_FEE)
    assert bot.effective_fee_rate == 0
    assert bot.fee_on(INR(1000)).is_zero
    assert bot.owner_keeps(INR(1000)) == INR(1000)


def test_own_api_mode_charges_the_disclosed_rate():
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=DEFAULT_PLATFORM_FEE, provider_key="k" * 16)
    assert bot.effective_fee_rate == Decimal("0.05")
    assert bot.fee_on(INR(1000)) == INR(50)
    assert bot.owner_keeps(INR(1000)) == INR(950)


def test_fee_applies_to_losing_sales_and_that_is_stated():
    """The fee is not profit-conditional, so the disclosure must say so."""
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=DEFAULT_PLATFORM_FEE, provider_key="k" * 16)
    # A sale the owner loses money on still owes the fee.
    assert bot.fee_on(INR(100)) == INR(5)
    assert "whether or not the sale is profitable" in bot.fee_disclosure()


@pytest.mark.parametrize("rate", [Decimal("-0.1"), Decimal("1.5")])
def test_nonsense_fee_rates_are_rejected(rate):
    with pytest.raises(WhiteLabelError):
        PlatformFee(rate=rate)


def test_fixed_fee_cannot_be_negative():
    with pytest.raises(WhiteLabelError):
        PlatformFee(rate=Decimal("0.05"), fixed=INR(-1))


def test_fee_with_fixed_component():
    fee = PlatformFee(rate=Decimal("0.05"), fixed=INR(2))
    assert fee.on(INR(1000)) == INR(52)
    assert "5%" in fee.describe() and "2" in fee.describe()


# --------------------------------------------------------------- disclosure
def test_own_api_disclosure_shows_rate_link_and_per_sale_charging():
    """The three facts an owner needs, before they commit."""
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=DEFAULT_PLATFORM_FEE, provider_key="k" * 16)
    text = bot.fee_disclosure()
    assert "5%" in text
    assert API_SIGNUP_URL in text
    assert "PLATFORM FEE" in text
    assert "cannot change after you create the bot" in text


def test_platform_api_disclosure_states_zero_fee():
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.PLATFORM_API,
                 fee=DEFAULT_PLATFORM_FEE)
    assert "Platform fee: none" in bot.fee_disclosure()


def test_disclosure_is_captured_at_creation_and_immutable():
    """The agreed terms travel with the bot, so they cannot drift later."""
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=PlatformFee(rate=Decimal("0.05")), provider_key="k" * 16)
    agreed = bot.disclosure
    assert "5%" in agreed
    # Changing the live fee object does not rewrite what was agreed.
    bot.fee = PlatformFee(rate=Decimal("0.20"))
    assert "5%" in bot.disclosure
    assert "20%" not in bot.disclosure


def test_own_api_requires_a_provider_key():
    with pytest.raises(WhiteLabelError, match="provider API key"):
        SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
               fee=DEFAULT_PLATFORM_FEE)


def test_platform_api_rejects_a_provider_key():
    with pytest.raises(WhiteLabelError, match="takes no provider key"):
        SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.PLATFORM_API,
               fee=DEFAULT_PLATFORM_FEE, provider_key="k" * 16)


# ---------------------------------------------------------- ledger splitting
def test_sale_split_posts_fee_to_its_own_account():
    ledger = Ledger()
    ledger.record_sale_split(INR(1000), INR(20), Money.zero(), INR(50), ref="w1")
    ledger.verify()
    assert ledger.balance(PLATFORM_FEE) == INR(50)
    assert ledger.balance(SALES) == INR(950)
    assert ledger.balance(CASH) == INR(980)  # 1000 collected, 20 to the PSP


def test_sale_split_with_gst_keeps_liability_separate():
    ledger = Ledger()
    ledger.record_sale_split(INR(1180), INR(20), INR(180), INR(50), ref="w2")
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.revenue == INR(950)
    assert pnl.platform_fee == INR(50)
    assert pnl.gst_collected == INR(180)
    assert ledger.balance(CASH) == INR(1160)


def test_owner_revenue_excludes_the_platform_fee():
    """A sub-bot owner's reported revenue is what they kept, not the gross."""
    ledger = Ledger()
    ledger.record_sale_split(INR(1000), INR(20), Money.zero(), INR(50), ref="w3")
    pnl = ledger.profit_and_loss()
    assert pnl.revenue == INR(950)
    assert pnl.net_profit == INR(950) - INR(20)


def test_split_rejects_a_fee_larger_than_the_sale():
    ledger = Ledger()
    with pytest.raises(LedgerError, match="cannot absorb"):
        ledger.record_sale_split(INR(100), INR(0), INR(90), INR(20), ref="w4")


def test_split_rejects_negative_fee():
    ledger = Ledger()
    with pytest.raises(LedgerError, match="cannot be negative"):
        ledger.record_sale_split(INR(100), INR(0), Money.zero(), INR(-5), ref="w5")


def test_zero_fee_split_matches_a_plain_sale():
    """The white-label path must not change the books when the fee is zero."""
    a, b = Ledger(), Ledger()
    a.record_sale(INR(1000), INR(20), INR(180), ref="x")
    b.record_sale_split(INR(1000), INR(20), INR(180), Money.zero(), ref="x")
    a.verify()
    b.verify()
    assert a.balance(SALES) == b.balance(SALES)
    assert a.balance(CASH) == b.balance(CASH)
    assert b.balance(PLATFORM_FEE).is_zero


# -------------------------------------------------------------- engine hooks
def _engine(fee_fn=None):
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"telegram": INR(10)}, balance=INR(5000), seed=7)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=1, otp_timeout_seconds=0.5,
                                           poll_interval=0.01),
                       platform_fee_fn=fee_fn)
    return engine, ledger, provider


def test_engine_platform_fee_is_zero_without_a_hook():
    engine, _, _ = _engine()
    assert engine.platform_fee(INR(1000)).is_zero


def test_engine_rejects_a_fee_exceeding_the_sale():
    engine, _, _ = _engine(fee_fn=lambda g: INR(2000))
    with pytest.raises(EngineError):
        engine.platform_fee(INR(1000))
    # It must be catchable as an EconomicsError so the chat layer handles it.
    assert issubclass(EngineError, EconomicsError)


def test_white_label_order_splits_revenue_and_posts_the_fee():
    """End to end: a real fulfilled order books the platform's cut."""
    engine, ledger, provider = _engine(fee_fn=DEFAULT_PLATFORM_FEE.on)
    provider.force_next(MockOutcome("success", otp="482193"))
    result = engine.fulfil("cust1", "telegram", gross_price=INR(1000))
    assert result.success
    ledger.verify()
    pnl = ledger.profit_and_loss()
    assert pnl.platform_fee == INR(50)
    assert pnl.revenue == INR(950)
    assert ledger.balance(PLATFORM_FEE) == INR(50)
    ledger.close()


def test_white_label_proceeds_are_reduced_by_exactly_the_fee():
    """The figure posted and the figure netted must come from one call."""
    plain, _, p1 = _engine()
    labelled, _, p2 = _engine(fee_fn=DEFAULT_PLATFORM_FEE.on)
    p1.force_next(MockOutcome("success", otp="111111"))
    p2.force_next(MockOutcome("success", otp="222222"))
    a = plain.fulfil("c", "telegram", gross_price=INR(1000))
    b = labelled.fulfil("c", "telegram", gross_price=INR(1000))
    assert a._proceeds - b._proceeds == INR(50)
    plain.ledger.close()
    labelled.ledger.close()


def test_non_white_label_order_is_unchanged():
    """Wiring the hook must not perturb the main bot's books."""
    engine, ledger, provider = _engine()
    provider.force_next(MockOutcome("success", otp="482193"))
    engine.fulfil("cust1", "telegram", gross_price=INR(1000))
    ledger.verify()
    assert ledger.profit_and_loss().revenue == INR(1000)
    assert ledger.balance(PLATFORM_FEE).is_zero
    ledger.close()


# ---------------------------------------------------------------- registry
def test_registry_round_trips_both_modes(tmp_path):
    reg = SubBotRegistry(str(tmp_path / "sub.db"))
    own = SubBot(owner_id="u1", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=PlatformFee(rate=Decimal("0.07")), provider_key="k" * 16)
    reg.add(own)
    reg.close()

    reopened = SubBotRegistry(str(tmp_path / "sub.db"))
    found = reopened.find(own.id)
    assert found is not None
    assert found.mode is SubBotMode.OWN_API
    assert found.fee.rate == Decimal("0.07")
    assert found.provider_key == "k" * 16
    assert found.disclosure == own.disclosure  # terms survive a restart
    reopened.close()


def test_registry_rejects_a_duplicate_token():
    reg = SubBotRegistry()
    reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN, mode=SubBotMode.PLATFORM_API,
                   fee=DEFAULT_PLATFORM_FEE))
    with pytest.raises(WhiteLabelError, match="already registered"):
        reg.add(SubBot(owner_id="u2", bot_token=GOOD_TOKEN,
                       mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))


def test_registry_filters_by_owner_and_active():
    reg = SubBotRegistry()
    a = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                       mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    reg.add(SubBot(owner_id="u2", bot_token=GOOD_TOKEN_2,
                   mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    assert [b.id for b in reg.for_owner("u1")] == [a.id]
    reg.set_active(a.id, False)
    assert reg.all_active() and all(b.id != a.id for b in reg.all_active())


def test_registry_is_thread_safe():
    """Sub-bot pollers all write from their own threads."""
    reg = SubBotRegistry()
    errors: list[Exception] = []

    def writer(n):
        try:
            for i in range(20):
                reg.add(SubBot(owner_id=f"u{n}", bot_token=f"{n}{i:08d}:AAF{'x' * 32}",
                               mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert reg.count() == 160


# --------------------------------------------------------- token validation
@pytest.mark.parametrize("token", [
    GOOD_TOKEN, "1:AAF" + "x" * 32, "123456789:AA-_" + "a" * 31,
])
def test_valid_tokens_accepted(token):
    assert validate_bot_token(token)


@pytest.mark.parametrize("token", [
    "", "notatoken", ":AAF" + "x" * 32, "abcdefghi:AAF" + "x" * 32,
    "123456789:short", "123456789:" + "x" * 100, "t.me/mybot",
])
def test_invalid_tokens_rejected(token):
    assert not validate_bot_token(token)


# ------------------------------------------------------------ createbot flow
def make_flow(fee=None) -> tuple[CreateBotFlow, SubBotRegistry]:
    reg = SubBotRegistry()
    return CreateBotFlow(reg, fee=fee), reg


def test_flow_starts_by_asking_for_a_token():
    flow, _ = make_flow()
    result = flow.start("u1")
    assert "bot token" in result.reply.lower()
    assert "@BotFather" in result.reply
    assert flow.pending("u1").step == Step.AWAIT_TOKEN


def test_flow_rejects_a_malformed_token():
    flow, reg = make_flow()
    flow.start("u1")
    result = flow.on_text("u1", "hello there")
    assert "does not look like a Telegram bot token" in result.reply
    assert reg.count() == 0
    assert flow.pending("u1").step == Step.AWAIT_TOKEN  # still recoverable


def test_flow_offers_both_modes_with_the_fee_named_up_front():
    flow, _ = make_flow()
    flow.start("u1")
    result = flow.on_text("u1", GOOD_TOKEN)
    assert result.buttons
    labels = " ".join(label for label, _ in result.buttons)
    assert "no % fee" in labels
    assert "5%" in labels
    assert flow.pending("u1").step == Step.AWAIT_MODE


def test_mode_button_label_follows_the_configured_fee():
    """The label must not hardcode 5%: a configured 7% shown as 5% is a
    misrepresentation that gets quoted back at you on the first dispute."""
    fee = PlatformFee(rate=Decimal("0.07"))
    flow, _ = make_flow(fee=fee)
    flow.start("u1")
    result = flow.on_text("u1", GOOD_TOKEN)
    labels = " ".join(label for label, _ in result.buttons)
    assert "7%" in labels and "5%" not in labels


def test_platform_path_discloses_then_creates():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    shown = flow.on_button("u1", CB_PLATFORM)
    assert "Platform fee: none" in shown.reply
    assert reg.count() == 0  # not created until confirmed

    done = flow.on_button("u1", CB_CONFIRM)
    assert done.created is not None
    assert done.created.mode is SubBotMode.PLATFORM_API
    assert reg.count() == 1
    assert flow.pending("u1") is None


def test_own_api_path_shows_the_signup_link_and_the_fee_before_creation():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    asked = flow.on_button("u1", CB_OWN_API)
    assert API_SIGNUP_URL in asked.reply
    assert "5%" in asked.reply
    assert reg.count() == 0

    flow.on_text("u1", "k" * 20)
    assert reg.count() == 0  # still awaiting confirmation
    done = flow.on_button("u1", CB_CONFIRM)
    assert done.created is not None
    assert done.created.mode is SubBotMode.OWN_API
    assert done.created.fee_on(INR(1000)) == INR(50)


def test_declining_at_confirmation_creates_nothing():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    flow.on_button("u1", CB_PLATFORM)
    result = flow.on_button("u1", "cb:createbot:abort")
    assert "Cancelled" in result.reply
    assert reg.count() == 0


def test_confirm_without_a_mode_creates_nothing():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    result = flow.on_button("u1", CB_CONFIRM)
    assert "Nothing to confirm" in result.reply
    assert reg.count() == 0


def test_duplicate_token_is_refused():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    flow.on_button("u1", CB_PLATFORM)
    flow.on_button("u1", CB_CONFIRM)

    flow.start("u2")
    result = flow.on_text("u2", GOOD_TOKEN)
    assert "already registered" in result.reply
    assert reg.count() == 1


def test_api_key_is_validated_before_use():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    flow.on_button("u1", CB_OWN_API)
    result = flow.on_text("u1", "ab")
    assert "does not look like an API key" in result.reply
    assert flow.pending("u1").step == Step.AWAIT_KEY


def test_cancel_mid_flow_leaves_no_state():
    flow, reg = make_flow()
    flow.start("u1")
    flow.on_text("u1", GOOD_TOKEN)
    flow.cancel("u1")
    assert flow.pending("u1") is None
    assert reg.count() == 0


def test_text_without_a_session_is_ignored_safely():
    flow, _ = make_flow()
    result = flow.on_text("nobody", "hello")
    assert "/createbot" in result.reply


# -------------------------------------------------------- multi-bot manager
class _Poller:
    def __init__(self, block: bool = True, fail: bool = False):
        self.block = block
        self.fail = fail
        self.started = threading.Event()

    def run(self):
        self.started.set()
        if self.fail:
            raise RuntimeError("token revoked")
        if self.block:
            time.sleep(0.5)


def test_manager_starts_and_stops_a_subbot():
    reg = SubBotRegistry()
    bot = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                         mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    # Block, otherwise the thread finishes before `running()` can observe it.
    mgr = MultiBotManager(reg, lambda sb: None, lambda sb, r: (lambda: time.sleep(1)))
    assert mgr.start(bot.id) is True
    assert mgr.running() == [bot.id]
    assert mgr.stop(bot.id) is True
    assert mgr.running() == []


def test_manager_does_not_double_start():
    reg = SubBotRegistry()
    bot = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                         mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    mgr = MultiBotManager(reg, lambda sb: None, lambda sb, r: (lambda: time.sleep(1)))
    assert mgr.start(bot.id) is True
    assert mgr.start(bot.id) is False
    mgr.stop_all()


def test_a_crashing_poller_is_recorded_and_does_not_take_the_process_down():
    reg = SubBotRegistry()
    bot = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                         mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    mgr = MultiBotManager(reg, lambda sb: _Poller(fail=True),
                          lambda sb, r: r.run)
    mgr.start(bot.id)
    deadline = time.time() + 2
    while not mgr.errors() and time.time() < deadline:
        time.sleep(0.01)
    assert "token revoked" in mgr.errors()[bot.id]
    assert mgr.running() == []


def test_a_bad_router_factory_does_not_stop_the_other_bots():
    reg = SubBotRegistry()
    good = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                          mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    bad = reg.add(SubBot(owner_id="u2", bot_token=GOOD_TOKEN_2,
                        mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))

    def factory(sb):
        if sb.id == bad.id:
            raise ValueError("cannot build provider")
        return object()

    mgr = MultiBotManager(reg, factory, lambda sb, r: (lambda: time.sleep(0.5)))
    started = mgr.start_all()
    assert good.id in started and bad.id not in started
    assert "cannot build provider" in mgr.errors()[bad.id]
    mgr.stop_all()


def test_inactive_bots_are_not_started():
    reg = SubBotRegistry()
    bot = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                         mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    reg.set_active(bot.id, False)
    mgr = MultiBotManager(reg, lambda sb: None, lambda sb, r: (lambda: None))
    assert mgr.start(bot.id) is False
    assert mgr.start_all() == []


def test_restart_replaces_a_dead_poller():
    reg = SubBotRegistry()
    bot = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                         mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    state = {"fail": True}

    def target(sb, r):
        def run():
            if state["fail"]:
                raise RuntimeError("first attempt died")
            time.sleep(0.3)
        return run

    mgr = MultiBotManager(reg, lambda sb: None, target)
    mgr.start(bot.id)
    deadline = time.time() + 2
    while not mgr.errors() and time.time() < deadline:
        time.sleep(0.01)
    assert mgr.errors()
    state["fail"] = False
    assert mgr.restart(bot.id) is True
    assert bot.id in mgr.running()
    mgr.stop_all()


# ----------------------------------------------------------- serve wiring
def test_whitelabel_is_disabled_by_default():
    """An existing deployment must not start spawning pollers unasked."""
    from uotpbot.config import from_environment
    import os

    os.environ.setdefault("UOTP_API_KEY", "dummy")
    os.environ.pop("WHITELABEL_ENABLED", None)
    assert from_environment().whitelabel_enabled is False


def test_whitelabel_without_a_secret_key_refuses_to_boot(monkeypatch):
    """A registry stores live credentials; enabling white-label without the
    key that encrypts them must fail startup, not silently go plaintext."""
    from uotpbot.config import ConfigError, from_environment

    monkeypatch.setenv("UOTP_API_KEY", "dummy")
    monkeypatch.setenv("WHITELABEL_ENABLED", "true")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        from_environment()


def test_make_whitelabel_returns_none_when_disabled():
    from uotpbot.__main__ import _make_whitelabel
    from uotpbot.catalog import load_catalog
    from uotpbot.config import from_environment
    import os

    os.environ.setdefault("UOTP_API_KEY", "dummy")
    os.environ.pop("WHITELABEL_ENABLED", None)
    settings = from_environment()
    ledger = Ledger()
    catalog = load_catalog()
    from uotpbot.wallets import SqliteWallets
    assert _make_whitelabel(settings, catalog, ledger, Pricer(catalog),
                            SqliteWallets(":memory:")) is None
    ledger.close()


def test_make_whitelabel_builds_a_manager_when_enabled(monkeypatch, tmp_path):
    from uotpbot.__main__ import _make_whitelabel
    from uotpbot.catalog import load_catalog
    from uotpbot.config import from_environment

    monkeypatch.setenv("UOTP_API_KEY", "dummy")
    monkeypatch.setenv("WHITELABEL_ENABLED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SUBBOTS_PATH", str(tmp_path / "sub.db"))
    settings = from_environment()
    ledger = Ledger()
    catalog = load_catalog()
    from uotpbot.wallets import SqliteWallets
    wl = _make_whitelabel(settings, catalog, ledger, Pricer(catalog),
                          SqliteWallets(":memory:"))
    try:
        assert wl is not None
        assert wl.registry.count() == 0
        assert wl.manager.running() == []
    finally:
        wl.registry.close()
        ledger.close()


def test_own_api_subbot_gets_its_own_provider_key(monkeypatch):
    """An OWN_API bot must bill its owner's key, not the platform's."""
    from uotpbot.__main__ import _provider_for
    from uotpbot.config import from_environment

    monkeypatch.setenv("UOTP_API_KEY", "the-platform-key")
    settings = from_environment()
    own = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=DEFAULT_PLATFORM_FEE, provider_key="the-owner-key")
    plat = SubBot(owner_id="u", bot_token=GOOD_TOKEN_2,
                  mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE)
    assert _provider_for(own, settings).config.api_key == "the-owner-key"
    assert _provider_for(plat, settings).config.api_key == "the-platform-key"


def test_signup_url_is_never_used_as_a_provider_endpoint(monkeypatch):
    """`provider_url` holds the signup page; pointing a purchase at it would
    send orders to a web form and silently fail."""
    from uotpbot.__main__ import _provider_for
    from uotpbot.config import from_environment

    monkeypatch.setenv("UOTP_API_KEY", "the-platform-key")
    settings = from_environment()
    bot = SubBot(owner_id="u", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=DEFAULT_PLATFORM_FEE, provider_key="k" * 16,
                 provider_url=API_SIGNUP_URL)
    provider = _provider_for(bot, settings)
    assert "register" not in provider.config.base_url
    assert provider.config.base_url == settings.uotp.base_url


# ------------------------------------------------------- health server hooks
def test_server_reports_no_subbots_when_disabled():
    from uotpbot.catalog import load_catalog
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider
    from uotpbot.web import HealthServer

    catalog = load_catalog()
    ledger = Ledger()
    server = HealthServer(
        BotEngine(catalog, MockProvider({}, balance=INR(0)), ledger, Pricer(catalog)),
        ledger, port=0,
    )
    assert server.subbot_health() is None
    assert server.liveness()[1]["subbots_running"] is None
    ledger.close()


def test_dead_subbot_does_not_flip_readiness():
    """One sub-bot with a revoked token must not restart the platform service."""
    from uotpbot.catalog import load_catalog
    from uotpbot.engine import BotEngine as BE
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider
    from uotpbot.web import HealthServer

    reg = SubBotRegistry()
    bot = reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                         mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    mgr = MultiBotManager(reg, lambda sb: None,
                          lambda sb, r: (lambda: (_ for _ in ()).throw(
                              RuntimeError("token revoked"))))
    mgr.start(bot.id)
    deadline = time.time() + 2
    while not mgr.errors() and time.time() < deadline:
        time.sleep(0.01)

    catalog = load_catalog()
    ledger = Ledger()
    server = HealthServer(
        BE(catalog, MockProvider({}, balance=INR(100)), ledger, Pricer(catalog)),
        ledger, port=0, subbots=mgr,
    )
    code, detail = server.readiness()
    assert code == 200, detail  # provider and ledger are fine
    assert detail["subbots"]["errors"][bot.id] == "RuntimeError: token revoked"
    assert detail["status"] == "ready"
    ledger.close()
    mgr.stop_all()


def test_metrics_exposes_the_platform_fee_line():
    from uotpbot.catalog import load_catalog
    from uotpbot.engine import BotEngine as BE
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider
    from uotpbot.web import HealthServer

    catalog = load_catalog()
    ledger = Ledger()
    ledger.record_sale_split(INR(1000), INR(20), Money.zero(), INR(50), ref="m1")
    server = HealthServer(
        BE(catalog, MockProvider({}, balance=INR(0)), ledger, Pricer(catalog)),
        ledger, port=0,
    )
    body = server.metrics()[1]
    assert body["platform_fee"] == "50.00"
    assert body["revenue"] == "950.00"
    ledger.close()


# --------------------------------------------------- configured fee rate
def test_platform_fee_rate_is_read_from_the_environment(monkeypatch):
    """The documented PLATFORM_FEE_RATE must actually govern the fee."""
    from uotpbot.__main__ import _platform_fee
    from uotpbot.config import from_environment

    monkeypatch.setenv("UOTP_API_KEY", "dummy")
    monkeypatch.setenv("PLATFORM_FEE_RATE", "0.12")
    assert from_environment().platform_fee_rate == Decimal("0.12")
    assert _platform_fee(from_environment()).describe() == "12% of each sale"


def test_configured_rate_is_what_the_owner_is_shown(monkeypatch):
    """What is configured, what is disclosed and what is charged must agree."""
    from uotpbot.__main__ import _platform_fee
    from uotpbot.config import from_environment

    monkeypatch.setenv("UOTP_API_KEY", "dummy")
    monkeypatch.setenv("PLATFORM_FEE_RATE", "0.12")
    fee = _platform_fee(from_environment())
    reg = SubBotRegistry()
    flow = CreateBotFlow(reg, fee)
    flow.start("u1")
    shown = flow.on_text("u1", GOOD_TOKEN)
    assert "12% of each sale" in shown.reply
    flow.on_button("u1", CB_OWN_API)
    flow.on_text("u1", "k" * 20)
    created = flow.on_button("u1", CB_CONFIRM).created
    assert created.fee_on(INR(1000)) == INR(120)


@pytest.mark.parametrize("bad", ["1.5", "-0.2", "100"])
def test_out_of_range_fee_rate_fails_at_startup(monkeypatch, bad):
    """Rejected as a ConfigError, before any owner sees terms."""
    from uotpbot.config import ConfigError, from_environment

    monkeypatch.setenv("UOTP_API_KEY", "dummy")
    monkeypatch.setenv("PLATFORM_FEE_RATE", bad)
    with pytest.raises(ConfigError, match="PLATFORM_FEE_RATE"):
        from_environment()


def test_non_numeric_fee_rate_fails_at_startup(monkeypatch):
    from uotpbot.config import ConfigError, from_environment

    monkeypatch.setenv("UOTP_API_KEY", "dummy")
    monkeypatch.setenv("PLATFORM_FEE_RATE", "five percent")
    with pytest.raises(ConfigError, match="PLATFORM_FEE_RATE"):
        from_environment()


# ------------------------------------ regression: the ROUTER path, not the flow
# The 2026-09-02 incident: handle() returned HELP_TEXT for any message that
# did not start with "/", so pasted tokens and keys never reached the flow.
# Every flow-level test stayed green while /createbot was unreachable through
# the real bot. These tests drive router.handle() -- the only path a real
# Telegram message takes.
def _wl_router():
    from uotpbot.bot.commands import CommandRouter
    from uotpbot.catalog import Catalog, ServiceCost, WalletPack
    from uotpbot.engine import BotEngine, EngineConfig
    from uotpbot.ledger import Ledger
    from uotpbot.money import INR
    from uotpbot.pricing import Pricer
    from uotpbot.provider.mock import MockProvider

    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94"), Decimal("0.04"), Decimal("0.95")),
    }, (WalletPack("Pro", INR(1000), INR(1150)),))
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider(
        {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()},
        balance=INR(5000), seed=5,
    )
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(retry_cap=3, otp_timeout_seconds=1.0,
                                           poll_interval=0.01))
    registry = SubBotRegistry()
    router = CommandRouter(engine, catalog, pricer, ledger, owner_id="111",
                           allowed_users=("111",), subbots=registry)
    return router, registry, ledger


def test_pasting_a_token_through_the_router_advances_the_flow():
    router, _, ledger = _wl_router()
    try:
        start = router.handle("111", "/createbot")
        assert "token" in start.text.lower()
        reply = router.handle("111", GOOD_TOKEN)
        assert "UOTP bot" not in reply.text, "help text swallowed the token paste"
        assert "Token received" in reply.text
        assert len(reply.buttons) == 2  # Reply must CARRY the buttons
        assert router.handle("111", "hello").buttons or True  # no crash on junk
    finally:
        ledger.close()


def test_full_own_api_flow_through_the_router_with_typed_choices():
    """End to end over handle() with text choices only -- proves the flow is
    completable even on a transport that cannot render buttons."""
    router, registry, ledger = _wl_router()
    try:
        router.handle("111", "/createbot")
        router.handle("111", GOOD_TOKEN)
        ask_key = router.handle("111", "2")
        assert "API key" in ask_key.text and "uotp" in ask_key.text.lower()
        disclosure = router.handle("111", "my-real-provider-key-9999")
        assert "Read this before you confirm" in disclosure.text
        assert "5%" in disclosure.text  # the fee is named in what they accept
        created = router.handle("111", "yes")
        assert "created" in created.text.lower()
        assert registry.count() == 1
        bot = registry.all_active()[0]
        assert bot.mode is SubBotMode.OWN_API
        assert bot.fee.rate == Decimal("0.05")
        # /mybots must never echo the owner's provider key back into chat.
        listing = router.handle("111", "/mybots")
        assert "my-real-provider-key-9999" not in listing.text
    finally:
        ledger.close()


def test_unknown_slash_command_is_not_swallowed_by_a_pending_flow():
    router, _, ledger = _wl_router()
    try:
        router.handle("111", "/createbot")
        reply = router.handle("111", "/frobnicate")
        assert not reply.ok and "Unknown command" in reply.text
    finally:
        ledger.close()


def test_cancel_from_midflow_works_through_the_router():
    router, registry, ledger = _wl_router()
    try:
        router.handle("111", "/createbot")
        router.handle("111", GOOD_TOKEN)
        reply = router.handle("111", "/cancel")
        assert "Cancelled" in reply.text
        assert registry.count() == 0
        # and plain text after cancelling goes back to help, not into the flow
        assert "YCOTP bot" in router.handle("111", "random text").text
    finally:
        ledger.close()


def test_handle_callback_advances_the_flow_and_forced_presses_are_inert():
    router, registry, ledger = _wl_router()
    try:
        router.handle("111", "/createbot")
        router.handle("111", GOOD_TOKEN)
        shown = router.handle_callback("111", CB_PLATFORM)
        assert "Platform fee: none" in shown.text
        done = router.handle_callback("111", CB_CONFIRM)
        assert "created" in done.text.lower()
        assert registry.count() == 1
        # after the flow finishes, stale presses cannot do anything
        stale = router.handle_callback("111", CB_CONFIRM)
        assert not stale.ok and "expired" in stale.text.lower()
        outsider = router.handle_callback("999", CB_PLATFORM)
        assert not outsider.ok
    finally:
        ledger.close()


def test_unauthorised_plain_text_cannot_advance_someone_elses_flow():
    """The flow is keyed per user: an outsider's text must not touch the
    owner's pending creation, no matter how valid it looks."""
    router, registry, ledger = _wl_router()
    try:
        router.handle("111", "/createbot")
        router.handle("111", GOOD_TOKEN)
        router.handle("999", "888888888:AAFdeadbeefdeadbeefdeadbeefdead")
        router.handle_callback("999", CB_CONFIRM)
        assert registry.count() == 0
        # owner's flow is untouched and still completes
        assert "API key" in router.handle("111", "2").text
    finally:
        ledger.close()


def test_unauthorised_user_cannot_start_a_flow_by_talking_to_another_one():
    router, _, ledger = _wl_router()
    try:
        out = router.handle("999", "888888888:AAFdeadbeefdeadbeefdeadbeefdead")
        assert not out.ok  # no pending flow -> help, and never a creation
    finally:
        ledger.close()


# ------------------------------------------- transport wiring (needs PTB)
def test_build_from_settings_registers_callback_handler(monkeypatch):
    """The createbot menu is unreachable if the callback handler is not wired;
    this pins the transport registration itself, not just the router method."""
    pytest.importorskip("telegram")
    from unittest.mock import MagicMock

    from uotpbot.bot import telegram as tg
    from uotpbot.config import from_environment

    monkeypatch.setenv("UOTP_API_KEY", "k")
    monkeypatch.setenv(
        "TELEGRAM_BOT_TOKEN", "123456:AAtesttokenforwiringcheckxxxxxxxxxx"
    )
    settings = from_environment(env_file="/nonexistent/.env")

    captured: list[str] = []

    class FakeApp:
        def add_handler(self, handler):
            captured.append(type(handler).__name__)

    class FakeBuilder:
        def token(self, tok):
            return self

        def post_init(self, fn):  # the command menu is registered here
            self._post_init = fn
            return self

        def build(self):
            return FakeApp()

    real_application = tg.Application
    tg.Application = MagicMock(builder=staticmethod(lambda: FakeBuilder()))
    try:
        tg.build_from_settings(settings, lambda: MagicMock())
    finally:
        tg.Application = real_application
    assert "CallbackQueryHandler" in captured, (
        "buttons would render but nothing answers the press"
    )


def test_on_callback_edits_message_and_answers_query():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from uotpbot.bot.telegram import TelegramFrontend

    router = MagicMock()
    router.handle_callback.return_value = type("R", (), {
        "text": "done", "ok": True, "buttons": (),
    })()
    frontend = TelegramFrontend(router)
    query = MagicMock()
    query.from_user.id = 42
    query.data = "cb:createbot:confirm"
    query.message = MagicMock()
    query.message.edit_text = AsyncMock()
    query.answer = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    asyncio.run(frontend.on_callback(update, None))
    router.handle_callback.assert_called_once_with("42", "cb:createbot:confirm")
    query.message.edit_text.assert_awaited_once_with("done", reply_markup=None)
    query.answer.assert_awaited_once_with("OK")


# ---------------------------------------------------------------- encryption
def test_registry_stores_credentials_as_ciphertext(tmp_path):
    """With a SECRET_KEY, raw rows must not contain the live credentials."""
    import sqlite3

    path = str(tmp_path / "enc.db")
    reg = SubBotRegistry(path, secret_key="test-secret-key-0123456789")
    bot = SubBot(owner_id="u1", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=PlatformFee(rate=Decimal("0.05")), provider_key="secret-provider-key")
    reg.add(bot)
    reg.close()

    conn = sqlite3.connect(path)
    raw_token, raw_key = conn.execute(
        "SELECT bot_token, provider_key FROM subbots").fetchone()
    conn.close()
    assert GOOD_TOKEN not in raw_token
    assert "secret-provider-key" not in raw_key
    # Fernet tokens are base64 starting with gAAAAA -- ciphertext, not plaintext.
    assert raw_token.startswith("gAAAAA")
    assert raw_key.startswith("gAAAAA")


def test_registry_decrypts_credentials_on_read(tmp_path):
    path = str(tmp_path / "enc2.db")
    key = "another-secret-key"
    reg = SubBotRegistry(path, secret_key=key)
    bot = SubBot(owner_id="u1", bot_token=GOOD_TOKEN, mode=SubBotMode.OWN_API,
                 fee=DEFAULT_PLATFORM_FEE, provider_key="pkey" * 4)
    reg.add(bot)
    reg.close()

    reopened = SubBotRegistry(path, secret_key=key)
    found = reopened.find(bot.id)
    assert found is not None
    assert found.bot_token == GOOD_TOKEN
    assert found.provider_key == "pkey" * 4
    by_token = reopened.find_by_token(GOOD_TOKEN)
    assert by_token is not None and by_token.id == bot.id
    reopened.close()


def test_registry_duplicate_detection_works_with_encryption(tmp_path):
    """find_by_token must match through ciphertext, or a stolen token could be
    registered twice (two pollers, one Telegram account)."""
    reg = SubBotRegistry(str(tmp_path / "enc3.db"), secret_key="k" * 32)
    reg.add(SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                   mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))
    with pytest.raises(WhiteLabelError, match="already registered"):
        reg.add(SubBot(owner_id="u2", bot_token=GOOD_TOKEN,
                       mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE))


def test_registry_legacy_plaintext_rows_survive_enabling_encryption(tmp_path):
    """A registry written before SECRET_KEY existed must still be readable
    once encryption is switched on (no forced re-creation of every bot)."""
    path = str(tmp_path / "legacy.db")
    old = SubBotRegistry(path)
    bot = SubBot(owner_id="u1", bot_token=GOOD_TOKEN, mode=SubBotMode.PLATFORM_API,
                 fee=DEFAULT_PLATFORM_FEE)
    old.add(bot)
    old.close()

    new = SubBotRegistry(path, secret_key="key-after-migration")
    found = new.find_by_token(GOOD_TOKEN)
    assert found is not None and found.id == bot.id
    assert new.find(bot.id).bot_token == GOOD_TOKEN
    new.close()


def test_registry_wrong_key_never_crashes_reads(tmp_path):
    """A changed SECRET_KEY must not take the whole registry down: reads fall
    back to the stored bytes, the bot just stops authenticating (visible via
    /mybots), instead of every read raising."""
    path = str(tmp_path / "wrongkey.db")
    reg = SubBotRegistry(path, secret_key="original-key")
    bot = SubBot(owner_id="u1", bot_token=GOOD_TOKEN,
                 mode=SubBotMode.PLATFORM_API, fee=DEFAULT_PLATFORM_FEE)
    reg.add(bot)
    reg.close()

    other = SubBotRegistry(path, secret_key="rotated-key")
    assert other.find(bot.id) is not None  # does not raise
    assert other.find_by_token(GOOD_TOKEN) is None  # no false match
    other.close()
