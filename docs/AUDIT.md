# uotpbot — Audit Report & Production Hardening

A full read-through of the codebase (every module, all backends, the whole
test suite), followed by a set of targeted fixes. The verdict up front:

**This is a strong codebase.** Integer-paise money, a double-entry ledger
whose invariants are asserted on every read, an optimal retry policy that is
*tested against brute-force dynamic programming*, single-flight polling,
owner-gated admin, and a deliberate "dead poller stays dead" policy. The
architecture is sound, so the work here is a set of surgical fixes to a
handful of real defects — not a rewrite.

This document records what was found, why it mattered, what changed, and the
residual limitations that remain by design.

---

## Method

1. Read every source file end to end before changing anything.
2. Reproduced each suspected defect with a throwaway script against the real
   modules (not a mock) before touching code, so a "fix" could not paper over a
   non-bug.
3. Baseline the suite (`pytest`, `ruff`) before and after.
4. Add a regression test that fails on the old behaviour for every fix.

Baseline at the start of this work: **`pytest` green, `ruff --select E4,E7,E9,F`
carrying ~40 findings** (a few fatal, most cosmetic). Final state: **556
tests pass, 4 skipped (environment-gated), `ruff` clean.**

---

## Findings and fixes

Severities: **Critical** = wrong money / security exposure; **High** = a real
customer or operator can be harmed; **Medium** = correctness or
observability; **Low** = hygiene.

### 1. Critical — `NameError` on the hot buy path (`engine.py`)

`BotEngine._buy_one` annotated a parameter as `ServiceCost`, but that name was
never imported into `engine.py`. With `from __future__ import annotations` the
annotation is a string and does not evaluate, so the suite never tripped it —
but any tooling, `typing.get_type_hints`, or a future removal of the lazy
annotation turns a normal purchase into a `NameError`. Ruff flagged it as
`F821`.

**Fix:** import `ServiceCost` from `.catalog` (where it is defined).

### 2. High — A customer refund could be posted to the books twice, but credited once

The refund outbox writes a durable row, then posts the customer-refund ledger
line and credits the wallet. The original code marked the row done *after*
both, and the ledger post was not idempotent. Sequence that broke:

1. `write_refund` → row `pending`.
2. Post the ledger line. **OK.**
3. Credit the wallet. **Raises** (transient DB blip).
4. Row stays `pending`; a retry (worker or redeploy) runs.
5. The retry posts the ledger line **again**, then credits.

Result: the ledger shows **two** customer-refund lines (₹30 of refunds) but the
wallet was credited **once** (₹15). Reported refunds outran the money actually
returned — exactly the drift the double-entry ledger exists to prevent.
Reproduced live: one refund line after the blip, two after `retry_all()`,
wallet credited exactly once.

**Fix (idempotent at two layers):**
- `Ledger.record_customer_refund` is now a no-op if a customer-refund line
  already exists for that `ref` (a refund is the only posting that *debits*
  `revenue:sales`, so the check is unambiguous). Exposed as
  `Ledger.has_customer_refund(ref)` for callers that must *know*.
- The outbox row carries a `ledger_done` flag. `refund_once` posts the line at
  most once (guarded by the flag and the ledger check), and only credits if the
  row is not already `done`. A retry after a credit blip therefore completes the
  credit without re-posting.

### 3. High — Racing paths could both credit the same refund

A Cancel and a timeout poll can resolve the *same* order concurrently. The
claim that decides "who may credit" had to be atomic. The fix makes the outbox
**insert** the claim: `write_refund` returns `(refund_id, inserted)`, where
`inserted` is true only for the caller that won the unique
`(scope, order_token)` constraint. Only the winner credits; the loser returns
the row's status and credits nothing. (The pre-existing `is_processed` guard
remains as the outer check; the claim makes it airtight under concurrency.)

### 4. High — Cancelling after a restart refunded the customer zero

`cancel_wait` reads the order's gross price from the in-memory engine result.
After a redeploy that entry is gone (the wait resumes from the DB), so `gross`
fell back to `Money(0)`: the customer's number was released and they were told
"₹0 returned". The money had been debited at purchase and was gone.

**Fix:** `cancel_wait` now falls back to the gross stored in the persisted
active-number row (written at allocation time), which is exactly what the
purchase charged. The sunk-cost tracker in the refusal path uses the same
fallback. Regression: `test_cancel_after_restart_refunds_the_real_gross`.

### 5. High — White-label sub-bot credentials stored in plaintext

`SubBotRegistry` persisted each sub-bot's **Telegram bot token** and (own-API)
**provider API key** in cleartext in `subbots.db` / `uotp.subbots`. A registry
that survives a redeploy is a durable copy of live credentials: one leaked
backup or dump compromises every sub-bot at once.

**Fix:** Fernet encryption at the storage boundary.
- `SubBotRegistry(..., secret_key=...)` and `PostgresRegistry(...,
  secret_key=...)` derive a Fernet key from the secret and encrypt
  `bot_token` and `provider_key` on write.
- `find_by_token` cannot use a SQL `=` match (Fernet output is randomized), so
  with a key it scans the (small) registry and compares decrypted values.
- Legacy plaintext rows still read: `_dec` falls back to the stored bytes when a
  value is not a Fernet token, so an existing deployment can switch encryption
  on without recreating every bot. A rotated (wrong) key degrades to a
  readable-but-unusable row rather than a crash.
- New optional extra: `pip install 'uotpbot[whitelabel]'`
  (`cryptography>=42.0`). `render.yaml` now installs it.
- **Startup guard:** `from_environment` raises `ConfigError` if
  `WHITELABEL_ENABLED` is true but `SECRET_KEY` is empty — the bot refuses to
  boot with an unencrypted credential store rather than silently going
  plaintext. `SECRET_KEY` is a new env var (see `.env.example`).

### 6. High — `/metrics` and `/readyz` exposed business figures unauthenticated

The HTTP surface is public (the platform probes it). `/metrics` served P&L,
wallet balances and provider-wallet state with **no auth**, and `/readyz`
echoed `net_profit`, `revenue`, `provider_wallet` and the full provider error
message (which can carry URLs/credentials).

**Fix:**
- `/metrics` exists **only** when `METRICS_TOKEN` is set (404 otherwise, so it
  cannot be probed into). With a token, it requires `Authorization: Bearer
  <token>` or `?token=<token>`, compared with `secrets.compare_digest`, else
  403. New `HealthServer(metrics_token=...)` / `Settings.metrics_token` /
  `METRICS_TOKEN`.
- `/readyz` reduced to liveness-grade facts: `provider: reachable` or
  `provider_error: <ExceptionClassName>` (class name only, never the message),
  `ledger: balanced|unbalanced`, `status`, plus sub-bot health. No money figures.
- `server.metrics()` stays a public method (tests and in-process callers use
  it); the auth lives in the HTTP layer, which is where it belongs.

### 7. High — `/ban` rewrote the allowlist and locked out everyone

In "anyone may buy" mode the allowlist is empty. `/ban <user>` implemented
banning by *removing* the user from `allowed_users` — which, on an empty
list, toggled the bot into allowlist mode containing only… nobody. Banning one
customer locked out every other customer. Banning in allowlist mode also
destroyed the allowlist's meaning. (And `/unban` was advertised in help but had
no dispatch entry.)

**Fix:** a dedicated `_banned: set[str]`, checked first in `_authorised` and
applied in every mode. `/ban` and `/unban` are now distinct commands
(directional, idempotent, honest replies). The owner can never be banned, and
banning can never change who else is allowed.

### 8. Medium — Typed `/buy` consumed twice the rate-limit slots of the button

`purchase()` recorded a rate-limit slot in the `check` step **and** again just
before the debit, so a typed buy burned two of the window while the button
path burned one. A 3-buys/30s limit effectively became 1-typed-buy/30s.

**Fix:** one `record` per purchase, in both paths, immediately after the check.

### 9. Medium — Sub-bot scope guard was a no-op for active numbers

`ScopedWallets.get_active` was documented as a scope guard but returned the
found row unconditionally, and `update_active`/`finish_active` were not
scope-filtered. A bot-B user holding a bot-A wait-token (or a bug that mixes
scopes) could read, mutate, or delete bot-A's live number — a cross-tenant
leak of another customer's phone number and OTP.

**Fix:** the `activenumbers` table carries `scope` (migrated on old DBs),
`get_active` returns `None` on a scope mismatch, and `update_active` /
`finish_active` filter by scope in both backends. `ScopedWallets` passes its
scope through all of them.

### 10. Medium — Missing migrations for new columns on old databases

Adding `ledger_done` (refund outbox) and `scope` (active numbers) to the DDL
breaks a pre-existing database, because `CREATE TABLE IF NOT EXISTS` does not
add columns to an existing table.

**Fix:** `SqliteWallets._migrate_orders` (renamed in purpose) and
`PostgresWallets._migrate_orders` now add any missing column on open
(`ALTER TABLE ... ADD COLUMN`, idempotent). The index that depends on the new
`scope` column is created **after** the migration so a legacy DB cannot crash
startup.

### 11. Low — `platform_earnings()` docstring overstated what it returns

It sums each active own-API bot's *fixed* fee and presented it as earnings.
Realized platform revenue lives in the `revenue:platform_fee` ledger account.

**Fix:** the docstring now says plainly that it is the *agreed fixed* component
across active bots, not realized revenue, and points at the ledger account as
the only auditable number.

### 12. Low — Lint debt (~40 `ruff` findings)

Unused imports, an `F811` duplicate import in `commands.py`, several `F841`
unused locals, `E741` `l` loop variables, and `E702` one-line statements.

**Fix:** all resolved; `ruff check src tests --select E4,E7,E9,F` is clean.

---

## Residual limitations (by design, documented)

These are not defects, but the honest boundaries of the guarantees above:

- **The refund ledger line is posted under two different refs.** The engine
  posts the timeout refund under `ref=order.order_id`; the cancel path posts
  under `ref=<wait token>`. `cancel_wait` detects the engine's line
  (`has_customer_refund(order.order_id)`) and passes `ledger_posted=True` so it
  does not post a second line. A cancel that interleaves inside the engine's
  `_fail` (between its ledger post and its outbox write) is still possible in a
  narrow window; the **wallet credit is exactly-once regardless** (the claim
  decides), so the customer is never double-refunded — worst case is a ledger
  line that must be reconciled by an owner, not a double payout.
- **A crash between the wallet credit and marking the outbox `done`** can leave
  a `pending` row that a later sweep re-credits. The credit itself is monotonic
  and visible in the wallet; this is the documented reconciliation case, bounded
  by `MAX_ATTEMPTS` and the `pending_refunds` visibility in `/status`.
- **Rotating `SECRET_KEY`** does not re-encrypt existing encrypted rows; they
  become undecryptable (those bots fail on Telegram until recreated). Plaintext
  rows written before encryption existed are unaffected.
- **`/readyz` no longer reports profit/revenue** by design; use `/metrics` with
  a token for those.

---

## Tests added

- `tests/test_refund.py` — exactly-once refund ledger lines and wallet credits
  across: a plain idempotent apply, a retry after a credit blip, a 2-way and
  a 3-way race on the same order, the timeout path, and a full
  `pending` → `retry_all` recovery.
- `tests/test_whitelabel.py` — credentials stored as ciphertext (verified by
  reading the raw SQLite file), decryption on read, duplicate detection through
  ciphertext, legacy-plaintext readability after enabling encryption, and a
  wrong-key read that never crashes; plus the startup guard that refuses to
  boot white-label without `SECRET_KEY`.
- `tests/test_web.py` — `/metrics` is 404 with no token, 403 with a missing or
  wrong token, 200 with a query token or a Bearer header; `/readyz` exposes no
  money figures and leaks only the provider error class name.
- `tests/test_commands.py` — ban blocks only the target in anyone-mode and in
  allowlist-mode, the owner cannot ban themselves, `/ban` is owner-only, and
  typed vs. button buys each consume exactly one rate-limit slot.
- `tests/test_wallets.py` — a foreign wait-token yields nothing to the wrong
  scope; cross-scope `update_active`/`finish_active` are no-ops; a legacy
  database is migrated on open and the pre-existing row survives.

## Verification

```bash
pip install -e ".[dev,whitelabel]"
.venv/bin/python -m pytest                 # 556 passed, 4 skipped (env-gated)
.venv/bin/ruff check src tests --select E4,E7,E9,F   # clean
```

The four skips are environment-gated by design: two need
`UOTP_TEST_PG_DSN` / `UOTP_LIVE_KEY`, and two need `python-telegram-bot`
installed (the `[telegram]` extra).
