# UOTP.in — research notes

Researched 2026-09-01. Everything below was read directly off the live site.

## What it is

UOTP sells temporary virtual phone numbers that receive SMS OTPs. Two sites:

| Site | Role |
|---|---|
| `uotp.in` | Marketing / SEO front. Public pricing pages. |
| `uotp.store` | The actual application — register, login, dashboard. |

Numbers are valid **20 minutes**, can receive multiple OTPs for the same
service inside that window, and the provider refunds to your wallet if no OTP
arrives within **5 minutes**. Support is via Telegram (`@Uotpsupport`,
`t.me/uotpstore`), email `support@uotp.in`.

## The ₹2 price is marketing. The real floor is ₹10.

This is the single most important finding, and the reason this codebase exists.

The homepage advertises *"Virtual Numbers for WhatsApp OTP — starting Rs.2
only"*, with a price table reading Telegram ₹2, Discord ₹3, Twitter/X ₹4,
WhatsApp ₹5, Instagram ₹6, Microsoft ₹7, Google ₹8, LinkedIn ₹8. Its
structured data repeats it (`"price": "2"`, *"Telegram costs Rs.2, WhatsApp
Rs.5, Google Rs.8"*), and the Terms of Service repeat it too (§2: *"prices …
start from Rs.2 per number"*).

The live service catalogue at `uotp.in/services` says otherwise:

| Service | Advertised | Real | Service | Real |
|---|---|---|---|---|
| Telegram | ₹2 | **₹10** | Instagram | ₹14 |
| Discord | ₹3 | **₹10** | Uber | ₹14 |
| Twitter/X | ₹4 | **₹11** | Google | ₹15 |
| WhatsApp | ₹5 | **₹12** | Microsoft | ₹15 |
| Instagram | ₹6 | **₹14** | LinkedIn | ₹16 |
| Google | ₹8 | **₹15** | Amazon | ₹18 |
| Microsoft | ₹7 | **₹15** | Apple | ₹18 |
| LinkedIn | ₹8 | **₹16** | PayPal | ₹20 |
| Amazon | ₹10 | **₹18** | Coinbase | ₹20 |
| Facebook | ₹5 | **₹13** | Binance | ₹22 |

Nothing on the page is below ₹10. The full transcribed table is in
[`data/uotp_prices.csv`](../data/uotp_prices.csv).

### Why this matters arithmetically

Pricing off ₹2 is not a small error, it is a fatal one. Take Telegram, with
the bundled priors (p(success) = 0.94, so 1.064 numbers per order) and the Pro
pack multiplier:

| | Cost model believes ₹2 | Real cost ₹10 |
|---|---|---|
| Sticker price | ₹2.00 | ₹10.00 |
| COGS per order (×1.064 attempts, −1.9% refund recovery, ×0.8696) | ₹1.82 | ₹9.08 |
| Sell at ₹5, net of the 2% gateway fee and its GST | ₹4.88 | ₹4.88 |
| **Profit per order** | **+₹3.06 (looks great)** | **−₹4.20** |

The two models disagree by **₹7.26 per order**. A bot built on the advertised
price reports healthy margins while losing money on every single order, and
the loss stays invisible until the wallet runs dry.
`tests/test_economics.py::test_pricing_off_the_advertised_two_rupee_price_loses_money`
pins exactly this scenario so it cannot regress.

## Wallet and top-ups

Minimum top-up **₹50**. Packs:

| Pack | Pay | Credit | Bonus | Real cost per ₹1 credit |
|---|---|---|---|---|
| Starter | ₹50 | ₹50 | — | 1.0000 |
| Popular | ₹200 | ₹220 | +10% | 0.9091 |
| Pro | ₹1000 | ₹1150 | +15% | **0.8696** |

The Pro pack is a **13.04% discount on every purchase**, not a rounding
detail. On Telegram it moves cost per delivered OTP from ₹10.45 to ₹9.09 and
break-even from ₹10.71 to ₹9.31 — a ₹1.40 swing per order, which is most of
the margin on a ₹15 sale. `Catalog.best_pack()` selects the cheapest
real-money-per-credit pack automatically and `wallet_multiplier` carries it
through the whole model.

## Refunds and delivery risk

- Homepage: *"Auto-refund if OTP fails to arrive"*, refund requestable if no
  OTP within 5 minutes, *"99.9% success rate"*.
- Terms §5 contradicts the marketing: *"We do not guarantee 100% success rate
  for OTP delivery"*, *"not responsible for third-party service blocks or
  number rejection"*, liability capped at the amount paid.

So delivery failure is a real cost, and the refund is conditional. The model
splits failure into two kinds because they are treated differently:

- **burned** — number already registered / rejected by the target service.
  Charged, essentially never refunded.
- **silent** — number is live but no OTP ever arrives. This is the refundable
  case, and only for `refund_share` of the charge.

Rates per service in `data/uotp_prices.csv` are **engineering priors**, set by
how hard each platform blocks VoIP ranges (Google, Apple, Meta, Binance,
Coinbase, PayPal worst; plain messaging apps best). They are not measured
truth. Run `python -m uotpbot.cli calibrate` with real order history to
replace them.

## API — verified 2026-09-02

There is no public API documentation page (`uotp.in/api`, `/docs`,
`uotp.store/api`, `/swagger`, `/openapi.json` all return nothing), but the
endpoint itself is reachable and its behaviour was confirmed by a live call:

```
GET https://uotp.store/api/stubs/handler_api.php?action=getBalance&api_key=<KEY>
-> ACCESS_BALANCE:0
```

So this is **not** a JSON API. It is the SMS-Activate-family protocol:

| Property | Reality |
|---|---|
| Method | **GET** (no POST, no request body) |
| Auth | `api_key` as a **query parameter**, not an `Authorization` header |
| Response | **plain text**, `PREFIX:field:field`, one line |
| Errors | the same shape: a bare token like `ERROR_KEY`, `ERROR_NO_BALANCE` |

Two consequences the adapter has to get right:

1. **Colons inside the payload.** `ACCESS_NUMBER:12345:+919876543210` must be
   split on the *first* colon only. A naive three-way `split(":")` mangles any
   value that itself contains a colon.
2. **Errors look like successes.** A failure is a bare token with no colon, so
   detection has to happen before any attempt at field extraction.

### Probed further on 2026-09-02

| Request | Response | Meaning |
|---|---|---|
| `action=getBalance` | `ACCESS_BALANCE:0` | works; wallet empty |
| `action=getPrices` | `BAD_COUNTRY` | action exists, needs `country` |
| `action=getPrices&country=0` | `BAD_OPERATOR` | also needs `operator` |
| `action=getTopCountriesByService&service=whatsapp` | `BAD_ACTION` | **action does not exist** |
| `action=getNumber&service=whatsapp&country=0` | `BAD_SERVICE` | action exists, slug not recognised |

Four things this establishes, all now encoded in the code:

1. **`BAD_COUNTRY` and `BAD_OPERATOR` are real error tokens** and have no
   colon. They were *not* in the initial error table, which meant they would
   have parsed as a success status and been silently mis-handled. They are now
   classified and pinned by tests against the verbatim bodies.
2. **`BAD_ACTION` is the unknown-action response**, confirming the error
   vocabulary — and that `getTopCountriesByService` is not available here.
3. **`getPrices` needs both `country` and `operator`**, so both are sent and
   both are configurable (`UOTP_PRICES_COUNTRY`, `UOTP_PRICES_OPERATOR`).
4. **The provider's service slugs differ from ours** — `whatsapp` was rejected.
   `UOTP_SERVICE_MAP` translates (`whatsapp=wa,telegram=tg`) once the real
   vocabulary is known.

### Re-probed on 2026-09-02 with the funded account (₹10)

| Request | Response | Meaning |
|---|---|---|
| `action=getBalance` | `ACCESS_BALANCE:10` | key works; **rupees, not paise** → `UOTP_BALANCE_DIVISOR=1` is correct (a wrong divisor is a 100× pricing error, so this one is load-bearing) |
| old 8-char key | `BAD_KEY` | correctly raises `AuthError`; already in the error table |
| `getPrices&country=1&operator=any` | `BAD_OPERATOR` | country validated, operator not |
| `getPrices&country=999&operator=abc` | `BAD_COUNTRY` | **country IS validated** — 0 and 1 pass, 999 does not |
| `getPrices&country=0&operator=2` | `NO_CONNECTION` | operator validated, then the provider's own backend failed |
| `getPrices&country=0&operator=3` | `NO_CONNECTION` | same — numeric ids ≥ 2 pass validation; 0/1 do not |
| `getPrices&country=0&operator=any` / `all` / `jio` | `BAD_OPERATOR` | no string alias works |
| `getStatus&id=1` | `ERROR_DATABASE` | reached the backend; database down |
| `getActive`, `getHistory`, `getCountries`, `getPriceCountries`, `getPriceOperators` | `BAD_ACTION` | **do not exist on this deployment** |

**The conclusion is a provider-side fault, not a config one.** Parameter
validation runs, then every action that needs the database returns
`NO_CONNECTION` / `ERROR_DATABASE`. Combined with the literal `/api/stubs/`
path, the honest reading: `getBalance` is the only fully functional action
right now. `getPrices` can be made to pass validation (`country=0`,
`operator=2`) but the backend must come up before it returns prices. No
purchase has ever completed against this endpoint, so the catalogue in
`data/uotp_prices.csv` (transcribed from the public services page) remains
the source of truth until `getPrices` answers.

Two safety decisions fall directly out of this and are implemented in
`provider/uotp.py`:

1. `NO_CONNECTION` / `ERROR_DATABASE` / `ERROR_SQL` are classified as provider
   **infrastructure** errors: reads surface them as retry-safe
   `ServiceUnavailable`, and `wait_for_otp` deliberately tolerates them so a
   DB blip never abandons a paid-for number mid-lease.
2. On the **buy** action the same tokens mean the charge may have applied
   *before* the backend died. They surface as `PurchaseTimedOut`
   (outcome-unknown), and the engine's contract is: hold, reconcile
   `getBalance`, never auto-retry a purchase into that response.
   `uotpbot check` now prints the layered diagnosis (auth ok → params ok →
   backend down) so this is visible before any wallet is funded.

Only `getBalance` is documented by the provider. The remaining actions
(`getNumber`, `getStatus`, `setStatus`, `getPrices`) follow this protocol
family's conventions and are **inferred** — every action name and response
prefix is configurable in `.env`, so a divergence is a config change rather
than a code change. `UotpProvider.probe()` verifies the key and the balance
prefix at startup; `uotpbot check` additionally exercises `getPrices` to
separate "bad config" from "provider outage".

Purchases carry an `order` parameter where the provider supports it. Where it
does not, an ambiguous timeout surfaces as `PurchaseTimedOut` so the caller
reconciles against `getBalance` instead of buying a second number.

`setStatus` with code `8` cancels an activation, but the protocol reports no
refund amount — so `cancel()` returns zero and the caller reconciles from the
next balance read. Inventing a figure would corrupt the ledger.

### Keep the key out of the repository

The API key belongs in `.env` (gitignored), never in source or in a commit. A
key that reaches a public repository should be treated as compromised and
rotated.

## Terms — read before automating

`uotp.in/terms` §3 (Acceptable Use) states: *"Bulk automated abuse or
**reselling of our services is strictly prohibited**."* §4 (Prohibited
Activities) lists *"Sharing account credentials or reselling access to our
platform"*, *"Creating fake accounts on third-party platforms for malicious
purposes"*, spam, phishing, financial fraud and identity theft. §3 also bars
attempts to bypass security measures.

This is a contractual restriction, and it applies to whatever account holds
the API key. The code here is provider-agnostic by design — `Provider` is a
plain protocol — so it can be pointed at any supplier, including ones that
explicitly sell reseller and API access. **Confirm your account's terms
actually permit automated resale before pointing it at UOTP in production.**
Nothing in this repo is specific to UOTP beyond the price catalogue and the
default endpoint paths.
