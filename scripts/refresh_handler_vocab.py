#!/usr/bin/env python3
"""Regenerate src/uotpbot/data/handler_vocab.json from the live provider.

The handler_api vocabulary (which service codes exist, which numeric
operator ids carry stock and at what price) changes upstream; this refreshes
our shipped table. Needs UOTP_API_KEY for the real account. Read-only: it
only ever calls action=getPrices, never getNumber.

Usage:
    UOTP_API_KEY=... python scripts/refresh_handler_vocab.py
    (optionally with UOTP_BASE_URL=... to point elsewhere)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get(
    "UOTP_BASE_URL", "https://uotp.store/api/stubs/handler_api.php"
)
KEY = os.environ["UOTP_API_KEY"]
OUT = Path(__file__).resolve().parent.parent / "src/uotpbot/data/handler_vocab.json"
PRODUCT_SERVICES = os.environ.get(
    "PRODUCT_SERVICES_JSON",
    "/home/user/integrations/harvest/services.json",
)
OPERATORS = ["3", "4", "5", "7", "2", "8", "9", "10", "6"]  # most-stock first


def get(text_qs: str) -> str:
    url = f"{BASE}?{text_qs}&api_key={urllib.parse.quote(KEY)}"
    req = urllib.request.Request(url, headers={"User-Agent": "uotpbot/vocab-refresh"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def main() -> int:
    op_prices: dict[str, dict[str, float]] = {}
    for op in OPERATORS:
        body = get(f"action=getPrices&country=22&operator={op}")
        if not body.lstrip().startswith("["):
            print(f"operator={op}: {body.strip()[:80]} (skipped)")
            continue
        for row in json.loads(body):
            code = str(row["servicecode"])
            try:
                price = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            op_prices.setdefault(code, {})[op] = price
    union = sorted(op_prices)
    if not union:
        print("no services returned -- refusing to overwrite vocab", file=sys.stderr)
        return 1

    smap: dict[str, str] = {}
    unmapped: list[str] = []
    lower = {c.lower(): c for c in union}
    try:
        products = json.load(open(PRODUCT_SERVICES))["data"]
    except FileNotFoundError:
        products = []
        print(f"warning: {PRODUCT_SERVICES_JSON} not found; map limited to exact codes")
    for c in products:
        code = c["code"]
        if code in lower:
            smap[code] = lower[code]
        elif code.lower() in lower:
            smap[code] = lower[code.lower()]
        else:
            unmapped.append(code)

    pref = OPERATORS

    def ops_for(code: str) -> list[str]:
        d = op_prices.get(code, {})
        return sorted(d, key=lambda o: (d[o], pref.index(o) if o in pref else 99))

    out = {
        "operators": [o for o in pref if any(o in op_prices[c] for c in op_prices)],
        "map": smap,
        "unmapped": sorted(unmapped),
        "op_order": {code: ops_for(code) for code in union},
        "op_prices": op_prices,
    }
    OUT.write_text(json.dumps(out, indent=0, sort_keys=True))
    print(f"{len(smap)} mapped, {len(unmapped)} unmapped, {len(union)} handler codes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
