"""Social-boost shop: catalogue, quoting, money path, UI gating."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
import json

import pytest

from uotpbot.bot.commands import CommandRouter
from uotpbot.bot.ui import MenuUI
from uotpbot.catalog import Catalog, ServiceCost
from uotpbot.engine import BotEngine, EngineConfig
from uotpbot.ledger import Ledger
from uotpbot.money import INR, Money, ROUND_CEILING, quantize_money
from uotpbot.pricing import Pricer
from uotpbot.provider.mock import MockProvider
from uotpbot.reseller import clone_price, earnings_balance
from uotpbot.smm.catalog import SmmCatalog, cat_id, detect_platform, parse_service
from uotpbot.smm.mock import MockSmmProvider
from uotpbot.smm.panel import PanelV2Client
from uotpbot.smm.provider import SmmAmbiguous, SmmProviderError, SmmUserError
from uotpbot.smm.shop import SmmShop
from uotpbot.wallets import ScopedWallets, SqliteWallets

OWNER = "1"
USER = "2"


def _otp_ui(wallets=None, smm_shop=None):
    catalog = Catalog({
        "telegram": ServiceCost("telegram", "Telegram", "messaging", INR(10),
                                Decimal("0.94")),
    })
    ledger = Ledger()
    pricer = Pricer(catalog)
    provider = MockProvider({"telegram": catalog.sticker_price("telegram")},
                            balance=INR(9999), seed=5)
    engine = BotEngine(catalog, provider, ledger, pricer,
                       config=EngineConfig(otp_timeout_seconds=0.3, poll_interval=0.01))
    router = CommandRouter(engine, catalog, pricer, ledger,
                           owner_id=OWNER, allowed_users=(OWNER, USER),
                           wallets=wallets)
    router.smm_shop = smm_shop
    return MenuUI(router), router


def _svc_row(**kw):
    row = {
        "service": "1261",
        "name": "TikTok Views",
        "type": "Default",
        "category": "TikTok | Views",
        "rate": "0.0066",
        "min": "100",
        "max": "100000",
        "refill": False,
        "cancel": True,
    }
    row.update(kw)
    return row


def test_parse_skips_non_default_and_zero_rate():
    assert parse_service(_svc_row(type="Custom Comments")) is None
    assert parse_service(_svc_row(rate="0")) is None
    assert parse_service(_svc_row(rate="-1")) is None
    svc = parse_service(_svc_row())
    assert svc is not None
    assert svc.service_id == "1261"
    assert svc.min_qty == 100


def test_platform_detect_never_uses_bare_x():
    assert detect_platform("Max views pack") == "other"
    assert detect_platform("Next-gen likes") == "other"
    assert detect_platform("Twitter Followers") == "twitter"
    assert detect_platform("X (Twitter) Likes") == "twitter"
    assert detect_platform("TikTok Views") == "tiktok"
    assert detect_platform("Instagram Followers") == "instagram"


def test_cat_id_is_sha1_prefix():
    assert len(cat_id("TikTok | Views")) == 8
    assert cat_id("TikTok | Views") == cat_id("TikTok | Views")
    assert cat_id("A") != cat_id("B")


def test_quote_is_ceiling_on_cost_and_sell():
    mock = MockSmmProvider([_svc_row()])
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"),
                     min_charge=Money(100))
    cat.refresh()
    svc = cat.get("1261")
    # 1000 units * 0.0066 USD/1000 = 0.0066 USD * 95 = 0.627 INR -> 63 paise ceil
    usd = svc.cost_usd(1000)
    assert usd == Decimal("0.0066")
    cost = quantize_money(usd * Decimal("95"), ROUND_CEILING)
    sell = cost.scale(Decimal("1.45"), ROUND_CEILING)
    if sell.paise < 100:
        sell = Money(100)
    assert cat.quote(svc, 1000) == sell
    # min charge floor
    assert cat.quote(svc, 100).paise >= 100


def test_clone_extra_is_on_selling_price():
    mock = MockSmmProvider([_svc_row()])
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"),
                     min_charge=Money(100))
    cat.refresh()
    svc = cat.get("1261")
    base = cat.quote(svc, 1000)
    bumped = cat.quote(svc, 1000, extra_rate=Decimal("0.38"))
    assert bumped == clone_price(base, Decimal("0.38"))
    assert bumped.paise > base.paise


def test_place_debits_then_adds_and_persists(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"),
                     min_charge=Money(100))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    before = store.balance(USER)
    row = shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    assert row.provider_order_id == "1"
    assert row.status == "pending"
    assert store.balance(USER).paise == before.paise - row.charge.paise
    assert mock.add_calls == [("1261", "https://tiktok.com/@x", 1000)]
    loaded = store.get_smm_order(row.id, user_id=USER)
    assert loaded is not None and loaded.quantity == 1000
    store.close()


def test_explicit_add_error_refunds(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    mock.fail_add = "not enough funds"
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    before = store.balance(USER)
    with pytest.raises(SmmUserError):
        shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    assert store.balance(USER).paise == before.paise
    assert store.user_smm_orders(USER) == []
    store.close()


def test_ambiguous_add_keeps_debit_does_not_retry(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    mock.ambiguous_add = True
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    before = store.balance(USER)
    with pytest.raises(SmmUserError, match="pending confirmation"):
        shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    assert store.balance(USER).paise < before.paise  # debit kept
    rows = store.user_smm_orders(USER)
    assert len(rows) == 1
    assert rows[0].provider_order_id == ""
    assert rows[0].status == "pending"
    assert len(mock.add_calls) == 1  # never retried
    store.close()


def test_partial_refund_is_floor_and_idempotent(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    row = shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    charged = row.charge.paise
    after_debit = store.balance(USER).paise
    mock.complete("1", remains=250, partial=True)
    notices = shop.poll_open()
    updated = store.get_smm_order(row.id, user_id=USER)
    due = (charged * 250) // 1000  # floor
    assert updated.status == "partial"
    assert updated.refunded.paise == due
    assert store.balance(USER).paise == after_debit + due
    # second poll must not refund again
    shop.poll_open()
    again = store.get_smm_order(row.id, user_id=USER)
    assert again.refunded.paise == due
    assert store.balance(USER).paise == after_debit + due
    assert notices and "partial" in notices[0].text.lower()
    store.close()


def test_cancel_refunds_full_charge(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    row = shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    before = store.balance(USER)
    mock.orders["1"]["status"] = "Canceled"
    mock.orders["1"]["remains"] = 1000
    shop.poll_open()
    updated = store.get_smm_order(row.id, user_id=USER)
    assert updated.status == "canceled"
    assert updated.refunded.paise == row.charge.paise
    assert store.balance(USER).paise == before.paise + row.charge.paise
    store.close()


def test_clone_earnings_on_terminal_net(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store, margin_fee_rate=Decimal("0.05"))
    svc = cat.get("1261")
    row = shop.place(
        USER, svc, 1000, "https://tiktok.com/@x",
        extra_rate=Decimal("0.38"), clone_owner="clone-owner",
    )
    mock.complete("1", remains=0, partial=False)
    shop.poll_open()
    earn = earnings_balance(store, "clone-owner")
    assert earn.paise > 0
    # canceled would have been zero; completed credits once
    shop.poll_open()
    assert earnings_balance(store, "clone-owner").paise == earn.paise
    store.close()
    assert row.charge.paise > 0


def test_scoped_orders_do_not_leak(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    a = ScopedWallets(store, "bota")
    b = ScopedWallets(store, "botb")
    a.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, a)
    svc = cat.get("1261")
    row = shop.place(USER, svc, 1000, "https://tiktok.com/@x", wallets=a)
    assert a.get_smm_order(row.id, user_id=USER) is not None
    assert b.get_smm_order(row.id, user_id=USER) is None
    store.close()


def test_poller_refunds_clone_scoped_wallet(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    scoped = ScopedWallets(store, "clone1")
    scoped.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, scoped)
    svc = cat.get("1261")
    row = shop.place(USER, svc, 1000, "https://tiktok.com/@x", wallets=scoped)
    after = scoped.balance(USER)
    mock.orders["1"]["status"] = "Canceled"
    mock.orders["1"]["remains"] = 1000
    shop.poll_open()
    updated = scoped.get_smm_order(row.id, user_id=USER)
    assert updated.status == "canceled"
    assert scoped.balance(USER).paise == after.paise + row.charge.paise
    # platform-unscoped key must not have been credited
    assert store.balance(USER).paise == 0
    store.close()


def test_ui_hides_smm_without_shop():
    ui, _ = _otp_ui()
    menu = ui.main_menu(USER)
    labels = [lbl for row in menu.rows for lbl, _ in row]
    assert "📣 Social boost" not in labels
    assert "🛒 Buy a number" in labels
    tap = ui.button(USER, "sm")
    assert "isn't on this bot" in tap.text


def test_ui_hides_smm_when_shop_wired_but_off(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    ui, _ = _otp_ui(wallets=store, smm_shop=shop)
    menu = ui.main_menu(USER)
    labels = [lbl for row in menu.rows for lbl, _ in row]
    assert "📣 Social boost" not in labels
    tap = ui.button(USER, "sm")
    assert "switched off" in tap.text
    store.close()


def test_admin_toggle_smm_shows_and_hides(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    ui, _ = _otp_ui(wallets=store, smm_shop=shop)
    panel = ui.admin_panel(OWNER)
    assert "Social boost: off" in panel.text
    assert any(d == "a:smm" for row in panel.rows for _, d in row)
    r = ui.button(OWNER, "a:smm")
    assert "**ON**" in r.text
    assert ui.smm_enabled() is True
    menu = ui.main_menu(USER)
    labels = [lbl for row in menu.rows for lbl, _ in row]
    assert "📣 Social boost" in labels
    r2 = ui.button(OWNER, "a:smm")
    assert "**OFF**" in r2.text
    labels2 = [lbl for row in ui.main_menu(USER).rows for lbl, _ in row]
    assert "📣 Social boost" not in labels2
    # customer cannot flip it
    assert "Owner only" in ui.button(USER, "a:smm").text
    store.close()


def test_ui_shows_smm_when_shop_wired(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    ui, router = _otp_ui(wallets=store, smm_shop=shop)
    ui.button(OWNER, "a:smm")  # owner must turn it on
    menu = ui.main_menu(USER)
    labels = [lbl for row in menu.rows for lbl, _ in row]
    assert "📣 Social boost" in labels
    hub = ui.button(USER, "sm")
    assert "Social boost" in hub.text
    assert any(cb.startswith("sm:pl:") for row in hub.rows for _, cb in row)
    for row in hub.rows:
        for label, cb in row:
            assert len(cb.encode()) <= 64
    # walk into service
    plat = ui.button(USER, "sm:pl:tiktok:0")
    assert plat.rows
    svc_list = ui.button(USER, f"sm:c:{cat_id('TikTok | Views')}:0")
    assert any("sm:s:1261" in cb for row in svc_list.rows for _, cb in row)
    card = ui.button(USER, "sm:s:1261")
    assert "TikTok Views" in card.text
    store.close()


def test_admin_usd_line_only_when_shop(tmp_path):
    ui, _ = _otp_ui()
    panel = ui.admin_panel(OWNER)
    assert "Social boost USD" not in panel.text
    store = SqliteWallets(str(tmp_path / "w.db"))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("1.23"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    shop.last_usd = Decimal("1.23")
    ui2, _ = _otp_ui(wallets=store, smm_shop=shop)
    panel2 = ui2.admin_panel(OWNER)
    assert "Social boost USD: $1.23" in panel2.text
    store.close()


class _FakeResp:
    def __init__(self, body: bytes, code: int = 200):
        self.body = body
        self.code = code
        self.fp = BytesIO(body)

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def urlopen(self, req, timeout=None):
        self.calls += 1
        item = self.bodies.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


def test_panel_add_timeout_is_ambiguous_and_not_retried():
    opener = _FakeOpener([TimeoutError("slow")])
    client = PanelV2Client("k", opener=opener, timeout=1, max_retries=3)
    with pytest.raises(SmmAmbiguous):
        client.add_order("1261", "https://x", 100)
    assert opener.calls == 1


def test_panel_add_error_is_provider_error():
    opener = _FakeOpener([json.dumps({"error": "Incorrect service ID"}).encode()])
    client = PanelV2Client("k", opener=opener)
    with pytest.raises(SmmProviderError, match="Incorrect service"):
        client.add_order("9", "https://x", 100)


def test_short_balance_refuses_before_add(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, Money(50))  # 50 paise, below the ₹1 floor
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"),
                     min_charge=Money(100))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    with pytest.raises(SmmUserError, match="Top up"):
        shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    assert mock.add_calls == []
    store.close()


def test_place_logs_boost_tx_and_posts_update(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    posted = []

    def announce(service, amount, *, delivered=False):
        posted.append((service, str(amount), delivered))

    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"),
                     min_charge=Money(100))
    cat.refresh()
    shop = SmmShop(mock, cat, store, announce=announce)
    svc = cat.get("1261")
    row = shop.place(USER, svc, 1000, "https://tiktok.com/@secret")
    txs, _n = store.list_wallet_tx(USER)
    kinds = [t.kind for t in txs]
    assert "boost" in kinds
    boost = next(t for t in txs if t.kind == "boost")
    assert boost.delta.paise == -row.charge.paise
    assert "TikTok" in boost.note
    assert posted and posted[0][0] == "TikTok Views" and posted[0][2] is False
    blob = " ".join(p[0] for p in posted)
    assert USER not in blob and "tiktok.com" not in blob
    ui, _ = _otp_ui(wallets=store, smm_shop=shop)
    ui.button(OWNER, "a:smm")
    wh = ui.button(USER, "tx")
    assert "Social boost" in wh.text
    assert "TikTok" in wh.text
    store.close()


def test_completed_boost_posts_delivered(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    posted = []

    def announce(service, amount, *, delivered=False):
        posted.append((service, delivered))

    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store, announce=announce)
    svc = cat.get("1261")
    shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    mock.complete("1", remains=0, partial=False)
    shop.poll_open()
    assert any(delivered for _s, delivered in posted)
    assert posted[0][1] is False  # place
    store.close()


def test_boost_refund_is_on_the_ledger(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("10"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    mock.orders["1"]["status"] = "Canceled"
    mock.orders["1"]["remains"] = 1000
    shop.poll_open()
    txs, _n = store.list_wallet_tx(USER)
    kinds = [t.kind for t in txs]
    assert "boost" in kinds and "boost_refund" in kinds
    ui, _ = _otp_ui(wallets=store, smm_shop=shop)
    wh = ui.button(USER, "tx")
    assert "Boost refund" in wh.text or "Social boost" in wh.text
    store.close()


def test_history_shows_admin_credit_debit_and_paginates(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    ui, router = _otp_ui(wallets=store)
    router.credit(USER, INR(200), kind="admin", note="owner credit")
    router._debit(USER, INR(25), kind="admin", note="owner debit")
    for i in range(10):
        router.credit(USER, INR(10), kind="deposit", note=f"pay {i}")
    wh = ui.button(USER, "tx")
    assert "Credits" in wh.text
    datas = [d for row in wh.rows for _, d in row]
    assert any(d.startswith("tx:") for d in datas)
    page2 = ui.button(USER, "tx:1")
    assert "Transaction history" in page2.text
    assert "Owner adjust" in page2.text
    store.close()


def test_provider_usd_short_refuses_before_debit(tmp_path):
    store = SqliteWallets(str(tmp_path / "w.db"))
    store.adjust(USER, INR(500))
    mock = MockSmmProvider([_svc_row()], balance=Decimal("0"))
    cat = SmmCatalog(mock, usd_inr=Decimal("95"), markup=Decimal("0.45"))
    cat.refresh()
    shop = SmmShop(mock, cat, store)
    svc = cat.get("1261")
    before = store.balance(USER)
    with pytest.raises(SmmUserError, match="unavailable"):
        shop.place(USER, svc, 1000, "https://tiktok.com/@x")
    assert store.balance(USER).paise == before.paise
    assert mock.add_calls == []
    store.close()
