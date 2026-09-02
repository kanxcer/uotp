# uotpbot

A virtual-number OTP reseller bot whose unit economics are derived, verified
and auditable — not guessed.

Built against [uotp.in](https://uotp.in) pricing, but provider-agnostic: the
supplier sits behind a small protocol, so any virtual-number API can be
plugged in.

> **Read [`docs/RESEARCH.md`](docs/RESEARCH.md) first.** It records what
> uotp.in actually charges, including the fact that the advertised "from ₹2"
> is marketing and the real floor is **₹10** — and why building on the ₹2
> figure loses money on every order. It also notes that uotp.in's terms
> prohibit reselling, which matters before you automate against an account.

## Why this exists

The obvious way to price an OTP resale is `cost × 1.5`. That is wrong, because
the sticker price of one number is not the cost of one delivered OTP:

1. A number can arrive **burned** — already registered on the target service.
   You paid; you get nothing.
2. A number can be live but **never receive the OTP**. You paid; at best a
   partial wallet refund comes back.
3. Each retry is a fresh charge, so the cost of one *successful* delivery is
   the sticker price inflated by `1/p(success)`.
4. Wallet top-up bonuses and payment-rail fees scale the whole thing.
5. On the way in, the gateway fee, its GST, output GST and disputes take a cut
   of revenue.

Get any one of those wrong and a service showing 50% margin at the sticker
price is actually loss-making.

## The five numbers that matter

```
$ python -m uotpbot.cli price telegram
```
```
  provider sticker price         ₹10.00
  wallet multiplier (best pack)  0.869565      <- Pro pack: 1000 paid / 1150 credit
  P(one number works)            94.0%
  expected numbers per order     1.064
  COGS per initiated order       ₹9.08         <- real money, retries included
  cost per successful delivery   ₹9.09         <- what you actually spend per sale
  shelf price                    ₹15.00
  net proceeds                   ₹14.64        <- after gateway fee and GST
  expected profit per order      ₹5.56
  break-even price               ₹9.31
  break-even success rate        12.01%        <- how bad delivery can get first
```

Two non-obvious results the test-suite pins:

- **Cost per delivered OTP is invariant to the retry cap.** Conditional on
  eventual success the attempt count is a plain geometric, so
  `E[attempts]/P(success) = 1/p` at every cap. Retrying more raises your
  *success rate*, never lowers your *unit cost* — it is a conversion lever,
  not a cost lever.
- **The myopic retry rule is provably optimal.** "Buy another number iff
  `p × net proceeds > marginal cost`" equals the exact optimal-stopping value
  from backward induction. `test_orders.py::test_policy_is_optimal_against_brute_force_dp`
  checks the two agree at every state.

## Money handling

Every rupee is stored as an **integer count of paise**. No float ever holds
currency — `0.1 + 0.2 != 0.3` in IEEE-754, and rounding at the wrong moment
silently invents or loses money.

`Money` combines only with `Money`. Multiplying by a *ratio* is deliberately
rejected; you must call `.scale(rate, ROUND_CEILING)` and name the rounding
direction, because that direction is a business decision:

```python
cost  = amount.scale(rate, ROUND_CEILING)   # never under-provision a cost
refund = amount.scale(rate, ROUND_DOWN)     # never over-refund a customer
```

Profit is never stored. The ledger records double-entry postings and profit is
always *derived*, so it cannot drift away from reality. `Ledger.verify()`
asserts debits == credits and runs on every read.

## Install

```bash
pip install -e ".[dev]"          # core + tests
pip install -e ".[telegram]"     # add the Telegram transport
pip install -e ".[postgres]"     # add Postgres storage (Supabase)
```

Requires Python 3.10+. The core has **no runtime dependencies** — the HTTP
client is `urllib`, storage defaults to `sqlite3`.

## Configure

```bash
cp .env.example .env             # then fill in UOTP_API_KEY
```

UOTP's endpoint uses the **`handler_api.php` protocol**, verified live:

```
GET .../handler_api.php?action=getBalance&api_key=<KEY>
-> ACCESS_BALANCE:0
```

GET requests, key in the query string, plain-text `PREFIX:field:field`
responses — not JSON. The adapter parses accordingly, splitting on the first
colon only so payloads containing colons survive, and detecting bare error
tokens (`ERROR_KEY`, `ERROR_NO_BALANCE`) before any field extraction.

Only `getBalance` is provider-documented; the other actions follow this
protocol family's conventions, so every action name and response prefix is
configurable in `.env`. `UotpProvider.probe()` verifies the key and the
balance prefix at startup, so a wrong mapping fails at boot rather than
mid-order.

**Keep the key in `.env`, never in source.** It is gitignored; a key that
reaches a public repo should be rotated.

## Commands

| Command | What it does |
|---|---|
| `uotpbot prices [services...]` | Cost table with break-even, recommended price and margin per service |
| `uotpbot price <service>` | Full economics breakdown for one service |
| `uotpbot simulate <service> --orders 50000` | Monte Carlo check of the cost model |
| `uotpbot calibrate obs.csv [--write]` | Replace prior rates with measured ones |
| `uotpbot run <service>` | Fulfil one order against the mock provider |
| `uotpbot report --db ledger.db` | Ledger profit and loss |

Common flags: `--gateway-rate`, `--gateway-fixed`, `--gst-rate`,
`--gst-exclusive`, `--chargeback-rate`, `--retry-cap`, `--strategy`,
`--target-margin`, `--safety-buffer`.

## White-label sub-bots

Owners can launch their own clone of the bot. Off by default:
`WHITELABEL_ENABLED=true`.

`/createbot` walks them through pasting a bot token and choosing how the bot
sources numbers:

| Mode | Who pays the provider | Platform fee |
|---|---|---|
| **Platform numbers** | The platform's wallet, at the wholesale price | None |
| **Your own API** | The owner, on their own provider key | `PLATFORM_FEE_RATE` of each sale |

Platform-number bots take no percentage because the platform already earns the
spread built into the wholesale price; charging both would be double dipping on
the same rupee.

### The fee is disclosed, not hidden

This is a deliberate design decision, and `tests/test_whitelabel.py` pins it:

- The rate is shown **before** the bot is created, with the signup link.
- The agreed terms are stored on the sub-bot record, so the rate charged
  cannot drift from the rate agreed (`test_disclosure_is_captured_at_creation_and_immutable`).
- The cut is posted to its own ledger account, `revenue:platform_fee`, so an
  owner can reconcile it line by line against their own provider invoice.

A percentage taken quietly from someone else's sale is an unfair trade practice
under the Consumer Protection Act 2019. It is also technically pointless here:
in own-API mode the owner holds the API key, so their provider dashboard shows
exactly what they were charged. A cut that does not reconcile with their own
spend gets found, and then it becomes a chargeback rather than revenue.
Disclosed, it is just a price.

Set `PLATFORM_FEE_RATE=0.05` for 5%. Out-of-range values fail at startup, not
mid-sale.

## Verifying the maths

`simulate` runs a Monte Carlo written independently of the analytics, then
compares the two:

```
$ python -m uotpbot.cli simulate google --orders 30000
  observed_success_rate         0.9895
  modelled_success_rate         0.9894
  observed_cost_per_delivery    15.36
  modelled_cost_per_delivery    15.40
  cost_delta                    -0.04
```

If the closed-form expectation were wrong, those would diverge. The suite runs
this check across the whole catalogue.

```bash
pytest            # 370 tests, lint clean (ruff)
```

## Storage: sqlite or Postgres (Supabase)

Two backends, picked by one setting:

| | sqlite (default) | Postgres (`DATABASE_URL` set) |
|---|---|---|
| Tables | `ledger.db`, `subbots.db` files | `uotp.postings`, `uotp.subbots` |
| Durability | needs a persistent disk | the database's problem |
| Fits | local dev, paid Render + disk | Render free tier, containers |

```bash
DATABASE_URL=postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

Use the **session pooler** (port 5432): the direct `db.<ref>.supabase.co` host
is IPv6-only and unreachable from Render, and the transaction pooler (6543)
adds pgBouncer caveats for no benefit here (prepared statements are already
switched off in `pgstore`). The Postgres backends are covered by the same
conformance battery as sqlite — `tests/test_pgstore.py` runs the identical
money flows against both (`UOTP_TEST_PG_DSN=... pytest tests/test_pgstore.py`).

With `DATABASE_URL` set the ledger **and** the sub-bot registry move together
— they must share one durability story, because a registry that outlives the
ledger keeps charging owners fees nobody can account for.

## Deploying to Render

The bot is a long-polling Telegram client — it opens an outbound connection and
never needs inbound traffic. But `serve` also binds `$PORT` and answers
`/healthz` and `/readyz`, so it works as either a Web Service or a Background
Worker.

```
Build:  pip install --upgrade pip && pip install ".[telegram,postgres]"
Start:  python -m uotpbot serve
Health: /healthz
```

`render.yaml` in this repo wires all of that up — free plan, no disk —
`render up` and you're done.

Required env vars: `DATABASE_URL`, `UOTP_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_OWNER_ID`, `TELEGRAM_ALLOWED_USERS`. See `.env.example` for the rest.

**The extras matter.** A bare `pip install .` starts an HTTP server that
accepts no orders and cannot reach Postgres.

**Do not use sqlite on the free plan.** Render wipes the container filesystem
on every deploy, so `ledger.db` — the entire audit trail, and any unreconciled
order — disappears on the next release. That is exactly why the Postgres
backend exists. With sqlite, pay for a disk; with Postgres, the free plan is
fine. The server logs a loud warning at startup if a sqlite path sits on
ephemeral storage.

`/healthz` is deliberately liveness-only and never calls the network. Using
`/readyz` as the health check would mean a provider outage gets the process
killed and restarted — and a restart mid-order is how you charge a customer and
deliver nothing. `/readyz` reports the outage instead.

Smoke-test a deploy with:

```
python -m uotpbot check     # verifies key, ledger, catalogue, pricing; exits 0/1
```

## Layout

```
src/uotpbot/
  money.py       exact integer-paise arithmetic; the only place money is made
  catalog.py     provider cost table, wallet packs, the Rs.10 floor
  economics.py   delivery model, break-even, margin, expected contribution
  pricing.py     price ladder and markup strategies
  ledger.py      double-entry ledger; profit is derived, never stored
  pgstore.py     Postgres (Supabase) backends for the ledger and registry
  store.py       backend selection: sqlite by default, Postgres on DATABASE_URL
  orders.py      order lifecycle and the optimal retry policy
  engine.py      fulfilment; the only place money actually moves
  cli.py         commands, including the Monte Carlo validator
  config.py      environment-driven settings
  web.py         HTTP liveness/readiness/metrics for a Web Service
  whitelabel.py  sub-bot records, the disclosed fee policy, poller manager
  createbot.py   the /createbot conversation, terms shown before creation
  provider/      base protocol, UOTP HTTP adapter, deterministic mock
  bot/           transport-free command router + thin Telegram shell
src/uotpbot/data/uotp_prices.csv   real uotp.in prices (transcribed 2026-09-01)
docs/RESEARCH.md       what the provider actually charges, with evidence
```

## Cost calibration

The success/burn/refund rates in `src/uotpbot/data/uotp_prices.csv` are **engineering
priors**, set by how hard each platform blocks VoIP ranges. They are not
measured truth. Once you have order history:

```bash
python -m uotpbot.cli calibrate observations.csv --write
```

with `slug,orders,attempts,successes,refunded_attempts[,refunded_amount,silent_amount]`.
`refund_share` is only updated when rupee amounts are supplied — it is not
estimable from counts, and inventing a value would silently bias every price.

## Responsible use

Virtual numbers are dual-use: legitimate for privacy, QA and testing; abused
for mass fake-account creation and fraud. `uotp.in/terms` §3 explicitly
prohibits *"bulk automated abuse or reselling"* and §4 prohibits fake accounts
for malicious purposes, spam, phishing and financial fraud.

Confirm your account's terms permit automated resale before pointing this at
UOTP in production. The `Provider` protocol exists precisely so it can point
somewhere else.
