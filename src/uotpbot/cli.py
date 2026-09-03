"""Command line interface.

Run ``python -m uotpbot.cli --help``. Every command is read-only against the
provider except ``run``, which fulfils a real order.

The most useful command for checking the maths is ``simulate``: it runs a
Monte Carlo of the delivery model and compares the observed cost per
successful delivery against the closed-form figure. If the two disagree, the
pricing is wrong -- they should agree to well under a rupee.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from decimal import Decimal
from pathlib import Path
from typing import Optional, Sequence

from .catalog import _DATA_DIR, Catalog, ServiceCost, default_catalog, load_catalog
from .economics import EconomicsError, FeeModel, OrderEconomics
from .engine import BotEngine, EngineConfig
from .ledger import Ledger
from .money import INR, Money, quantize_money
from .pricing import Pricer, Strategy
from .provider.mock import MockProvider

__all__ = ["main", "build_parser", "monte_carlo"]


# --------------------------------------------------------------------- math
def monte_carlo(
    cost: ServiceCost,
    gross: Money,
    *,
    orders: int = 20_000,
    retry_cap: int = 3,
    wallet_multiplier: Decimal = Decimal(1),
    seed: int = 7,
    fees: Optional[FeeModel] = None,
) -> dict[str, object]:
    """Simulate ``orders`` customer orders and report realised economics.

    Deliberately does *not* use :class:`OrderEconomics` for the simulation
    itself -- it rolls the dice independently. Comparing the two is a genuine
    check that the closed-form expectation in ``economics.py`` is right.
    """
    rng = random.Random(seed)
    fees = fees or FeeModel()
    p = cost.otp_success_rate
    b = cost.burn_rate
    sticker = cost.list_price

    successes = 0
    total_spend = Money.zero()
    total_revenue = Money.zero()
    attempts_hist: dict[int, int] = {}

    for _ in range(orders):
        spend = Money.zero()
        delivered = False
        attempts = 0
        for _attempt in range(retry_cap):
            attempts += 1
            roll = Decimal(str(rng.random()))
            spend = spend + sticker
            if roll < p:
                delivered = True
                break
            # silent (refundable) vs burned (not)
            if roll >= p + b:
                refund = sticker.scale(cost.refund_share)
                spend = spend - refund
        attempts_hist[attempts] = attempts_hist.get(attempts, 0) + 1
        total_spend = total_spend + spend.scale(wallet_multiplier)
        if delivered:
            successes += 1
            total_revenue = total_revenue + fees.net_proceeds(gross)

    if successes == 0:
        return {"orders": orders, "successes": 0, "note": "nothing succeeded"}

    observed_cost_per_delivery = quantize_money(total_spend / successes)
    observed_revenue_per_delivery = quantize_money(total_revenue / successes)
    econ = OrderEconomics.for_service(
        cost, gross, fees=fees, wallet_multiplier=wallet_multiplier, retry_cap=retry_cap
    )
    return {
        "orders": orders,
        "successes": successes,
        "observed_success_rate": f"{Decimal(successes) / orders:.4f}",
        "modelled_success_rate": f"{econ.delivery.order_success_rate:.4f}",
        "observed_cost_per_delivery": observed_cost_per_delivery.to_plain(),
        "modelled_cost_per_delivery": econ.cost_per_successful_delivery.to_plain(),
        "cost_delta": (observed_cost_per_delivery - econ.cost_per_successful_delivery).to_plain(),
        "observed_profit_per_delivery": (
            observed_revenue_per_delivery - observed_cost_per_delivery
        ).to_plain(),
        "modelled_profit_per_delivery": econ.contribution_per_delivery.to_plain(),
        "observed_total_profit": (total_revenue - total_spend).to_plain(),
        "modelled_total_profit": (econ.expected_contribution * orders).to_plain(),
        "attempts_histogram": dict(sorted(attempts_hist.items())),
    }


# ------------------------------------------------------------------ output
def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(out)


def _catalog(args: argparse.Namespace) -> Catalog:
    return load_catalog(Path(args.prices)) if getattr(args, "prices", None) else default_catalog()


def _fees(args: argparse.Namespace) -> FeeModel:
    return FeeModel(
        gateway_rate=Decimal(str(args.gateway_rate)),
        gateway_fixed=INR(args.gateway_fixed),
        fee_gst_rate=Decimal(str(args.fee_gst)),
        gst_rate=Decimal(str(args.gst_rate)),
        gst_inclusive=not args.gst_exclusive,
        chargeback_rate=Decimal(str(args.chargeback_rate)),
    )


def _pricer(args: argparse.Namespace) -> Pricer:
    catalog = _catalog(args)
    return Pricer(
        catalog,
        fees=_fees(args),
        strategy=Strategy(args.strategy),
        target_margin=Decimal(str(args.target_margin)),
        retry_cap=args.retry_cap,
        safety_buffer=Decimal(str(args.safety_buffer)),
    )


# ---------------------------------------------------------------- commands
def cmd_prices(args: argparse.Namespace) -> int:
    pricer = _pricer(args)
    book = pricer.price_book(args.service)
    rows = [
        (
            a.service.name,
            a.service.list_price,
            f"{a.econ.delivery.order_success_rate:.1%}",
            f"{a.econ.expected_attempts:.2f}",
            a.econ.cost_per_successful_delivery,
            a.break_even,
            a.gross_price,
            a.econ.expected_contribution,
            f"{a.econ.gross_margin_ratio:.0%}",
            "OK" if a.econ.is_profitable else "LOSS",
        )
        for a in book
    ]
    print(
        _table(
            rows,
            ["service", "cost", "p(deliver)", "attempts", "cost/delivery",
             "break-even", "price", "profit/order", "margin", "status"],
        )
    )
    summary = pricer.portfolio_summary(book)
    print()
    for key, value in summary.items():
        print(f"  {key.replace('_', ' '):<28} {value}")
    losses = pricer.loss_makers(book)
    if losses:
        print(f"\nDo not sell these (cannot clear break-even): "
              f"{', '.join(a.service.name for a in losses)}")
    return 0


def cmd_price(args: argparse.Namespace) -> int:
    catalog = _catalog(args)
    cost = catalog.get(args.service)
    pricer = _pricer(args)
    advice = pricer.price(cost)
    econ = advice.econ
    print(f"{cost.name}  [{cost.slug}]  category={cost.category}")
    print(f"  {advice.reason}\n")
    rows = [
        ("provider sticker price", econ.sticker_price),
        ("wallet multiplier (best pack)", f"{econ.wallet_multiplier:.6f}"),
        ("P(one number works)", f"{econ.delivery.success_rate:.1%}"),
        ("P(burned)", f"{econ.delivery.burn_rate:.1%}"),
        ("P(silent, refundable)", f"{econ.delivery.silent_rate:.1%}"),
        ("expected numbers per order", f"{econ.expected_attempts:.3f}"),
        ("P(order succeeds)", f"{econ.delivery.order_success_rate:.1%}"),
        ("COGS per initiated order", econ.cogs),
        ("cost per successful delivery", econ.cost_per_successful_delivery),
        ("shelf price", econ.gross_price),
        ("gateway fee", econ.gateway_cost),
        ("GST payable", econ.gst_payable),
        ("net proceeds", econ.net_proceeds),
        ("gross margin", econ.gross_margin),
        ("expected profit per order", econ.expected_contribution),
        ("break-even price", advice.break_even),
        ("headroom above break-even", f"{advice.headroom} ({advice.headroom_ratio:.1%})"),
    ]
    try:
        rows.append(("break-even success rate", f"{econ.break_even_success_rate():.2%}"))
    except EconomicsError as exc:
        rows.append(("break-even success rate", f"n/a ({exc})"))
    print(_table([(k, str(v)) for k, v in rows], ["metric", "value"]))
    if not econ.is_profitable:
        print("\nWARNING: this service loses money at the recommended price.")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    catalog = _catalog(args)
    cost = catalog.get(args.service)
    pricer = _pricer(args)
    gross = pricer.price(cost).gross_price
    result = monte_carlo(
        cost, gross,
        orders=args.orders,
        retry_cap=args.retry_cap,
        wallet_multiplier=catalog.effective_multiplier(catalog.best_pack()),
        seed=args.seed,
        fees=_fees(args),
    )
    print(f"Monte Carlo: {cost.name} at {gross}, {args.orders} orders, "
          f"retry_cap={args.retry_cap}\n")
    print(_table([(k, str(v)) for k, v in result.items()], ["metric", "value"]))
    delta = Money.from_paise(
        int(Decimal(str(result.get("cost_delta", "0"))) * 100)
    )
    print()
    if abs(delta.paise) <= max(20, int(Decimal(str(result["modelled_cost_per_delivery"])) * 100 * Decimal("0.02"))):
        print(f"OK: simulated and modelled cost agree within {delta} (<= 2%).")
    else:
        print(f"MISMATCH: simulated vs modelled differ by {delta}. "
              "Check the delivery model before pricing off it.")
        return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.db) if args.db else ":memory:")
    pnl = ledger.profit_and_loss()
    print(_table([(k, v) for k, v in pnl.as_dict().items()], ["account", "amount"]))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Fulfil one order against the mock provider (offline demonstration)."""
    catalog = _catalog(args)
    pricer = _pricer(args)
    prices = {s.slug: catalog.sticker_price(s.slug) for s in catalog.services()}
    rates = {s.slug: float(s.otp_success_rate) for s in catalog.services()}
    provider = MockProvider(prices, balance=INR(args.balance), success_rates=rates, seed=args.seed)
    ledger = Ledger(Path(args.db) if args.db else ":memory:")
    engine = BotEngine(
        catalog, provider, ledger, pricer,
        fees=_fees(args),
        config=EngineConfig(retry_cap=args.retry_cap, otp_timeout_seconds=1.0,
                            poll_interval=0.01),
    )
    result = engine.fulfil(args.customer, args.service, country=args.country)
    print(json.dumps(result.summary(), indent=2))
    print()
    print("Ledger:")
    pnl = ledger.profit_and_loss()
    print(_table([(k, v) for k, v in pnl.as_dict().items()], ["account", "amount"]))
    return 0 if result.success else 2



def cmd_calibrate(args: argparse.Namespace) -> int:
    """Replace the prior success/burn/refund rates with measured ones.

    The bundled rates are engineering guesses. This command turns real order
    history into maximum-likelihood estimates and rewrites the cost CSV, so
    pricing converges on what the provider actually does rather than on what
    we assumed.

    Input is a CSV of per-service aggregates::

        slug,orders,attempts,successes,refunded_attempts[,refunded_amount,silent_amount]

    ``success_rate`` and ``burn_rate`` follow directly from the counts: a
    success is one successful attempt out of ``attempts`` total, a refund
    marks a silent (refundable) failure, and whatever is left over was a
    burned number.

    ``refund_share`` is *not* estimable from counts -- it is the fraction of
    the charge returned, so it needs rupee amounts. It is updated only when
    ``refunded_amount`` and ``silent_amount`` are both supplied, and left at
    its prior value otherwise. Silently inventing a value here would quietly
    bias every price in the book.
    """
    target = Path(args.prices) if args.prices else _DATA_DIR / "uotp_prices.csv"
    obs_path = Path(args.observations)
    if not obs_path.exists():
        print(f"error: observations file not found: {obs_path}", file=sys.stderr)
        return 2

    observed: dict[str, dict[str, int]] = {}
    for row in csv.DictReader(obs_path.read_text(encoding="utf-8").splitlines()):
        slug = (row.get("slug") or "").strip()
        if not slug or slug.startswith("#"):
            continue
        attempts = int(row["attempts"])
        successes = int(row["successes"])
        refunded = int(row.get("refunded_attempts") or 0)
        if attempts <= 0:
            continue
        if successes + refunded > attempts:
            print(f"error: {slug}: successes + refunds ({successes}+{refunded}) "
                  f"exceed attempts ({attempts})", file=sys.stderr)
            return 2
        burned = attempts - successes - refunded
        entry = {
            "attempts": attempts,
            "success_rate": successes,
            "burn": burned,
            "refunded": refunded,
            "refund_share": None,
        }
        refunded_amount = (row.get("refunded_amount") or "").strip()
        silent_amount = (row.get("silent_amount") or "").strip()
        if refunded_amount and silent_amount:
            charged = Decimal(silent_amount)
            if charged > 0:
                entry["refund_share"] = Decimal(refunded_amount) / charged
        observed[slug] = entry

    lines = target.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    updated: list[tuple[str, int, str, str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("slug,"):
            out.append(line)
            continue
        fields = line.split(",")
        slug = fields[0].strip()
        stats = observed.get(slug)
        if stats is None or len(fields) < 7:
            out.append(line)
            continue
        n = stats["attempts"]
        s = Decimal(stats["success_rate"]) / n
        b = Decimal(stats["burn"]) / n
        fields[4] = f"{s:.4f}"
        fields[5] = f"{b:.4f}"
        share = stats["refund_share"]
        share_text = fields[6]
        if share is not None:
            share_text = f"{min(max(share, Decimal(0)), Decimal(1)):.4f}"
            fields[6] = share_text
        out.append(",".join(fields))
        updated.append((slug, n, f"{s:.4f}", f"{b:.4f}",
                        share_text if share is not None else "(kept)"))

    if not updated:
        print("no matching services found; nothing to update.")
        return 1

    print(_table(updated, ["service", "attempts", "success_rate", "burn_rate", "refund_share"]))
    if args.write:
        target.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nwrote {target}")
    else:
        print("\ndry run; pass --write to update the catalogue")
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uotpbot", description="Virtual-number OTP reseller with auditable economics."
    )
    p.add_argument("--prices", help="path to a cost CSV (default: bundled src/uotpbot/data/uotp_prices.csv)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--gateway-rate", type=float, default=0.02,
                        help="PSP fee as a fraction of the sale (default 0.02)")
        sp.add_argument("--gateway-fixed", type=float, default=0.0,
                        help="PSP fixed fee in rupees")
        sp.add_argument("--fee-gst", type=float, default=0.18, help="GST on the PSP fee")
        sp.add_argument("--gst-rate", type=float, default=0.0,
                        help="output GST on sales (0.18 if registered)")
        sp.add_argument("--gst-exclusive", action="store_true",
                        help="add GST on top of the price instead of including it")
        sp.add_argument("--chargeback-rate", type=float, default=0.0,
                        help="fraction of delivered orders disputed")
        sp.add_argument("--retry-cap", type=int, default=3,
                        help="max numbers bought per order")
        sp.add_argument("--strategy", default=Strategy.TARGET_MARGIN.value,
                        choices=[s.value for s in Strategy])
        sp.add_argument("--target-margin", type=float, default=0.35,
                        help="target margin ratio for the target_margin strategy")
        sp.add_argument("--safety-buffer", type=float, default=0.10,
                        help="extra buffer above break-even, as a fraction")

    sp = sub.add_parser("prices", help="cost table with break-even and recommended prices")
    sp.add_argument("service", nargs="*", help="limit to these services")
    add_common(sp)
    sp.set_defaults(func=cmd_prices)

    sp = sub.add_parser("price", help="detailed economics for one service")
    sp.add_argument("service")
    add_common(sp)
    sp.set_defaults(func=cmd_price)

    sp = sub.add_parser("simulate", help="Monte Carlo check of the cost model")
    sp.add_argument("service")
    sp.add_argument("--orders", type=int, default=20_000)
    sp.add_argument("--seed", type=int, default=7)
    add_common(sp)
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("calibrate", help="replace prior rates with measured ones")
    sp.add_argument("observations", help="CSV of slug,orders,attempts,successes,refunded_attempts")
    sp.add_argument("--write", action="store_true", help="rewrite the cost CSV in place")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("report", help="ledger profit and loss")
    sp.add_argument("--db", help="path to the ledger sqlite file")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("run", help="fulfil one order against the mock provider")
    sp.add_argument("service")
    sp.add_argument("--customer", default="demo")
    sp.add_argument("--country", default="in")
    sp.add_argument("--balance", type=float, default=1000.0)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--db", help="path to the ledger sqlite file")
    add_common(sp)
    sp.set_defaults(func=cmd_run)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, EconomicsError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
