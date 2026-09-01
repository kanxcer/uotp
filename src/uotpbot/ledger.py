"""Double-entry ledger.

A reseller bot's reported profit is worthless unless it can be reconstructed
from movements. This module therefore does not store a "profit" number at all
-- it stores postings, and profit is always *derived*, so it can never drift
away from reality the way a hand-maintained balance does.

Invariant, enforced on every post and re-checked by :meth:`Ledger.verify`:

    total debits == total credits

Accounts follow the normal-balance convention::

    asset:cash            bank / UPI float          debit-normal
    asset:wallet          provider wallet credit    debit-normal
    cogs:numbers          number purchase cost      debit-normal
    expense:gateway       PSP fees                  debit-normal
    expense:topup_fee     fees paid to top up       debit-normal
    liability:gst         GST collected, not yours  credit-normal
    equity:owner          owner capital / draws     credit-normal
    revenue:sales         what customers paid       credit-normal
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from .money import Money

__all__ = ["Account", "Posting", "Ledger", "LedgerError", "PnL", "SCHEMA"]


class LedgerError(Exception):
    """Raised when the ledger would be left unbalanced or corrupt."""


class Account(str):
    """An account name. ``str`` subclass so it works as a dict key and in SQL."""

    __slots__ = ()

    @property
    def kind(self) -> str:
        return self.split(":", 1)[0]

    @property
    def is_debit_normal(self) -> bool:
        return self.kind in ("asset", "cogs", "expense")


CASH = Account("asset:cash")
WALLET = Account("asset:wallet")
COGS = Account("cogs:numbers")
GATEWAY = Account("expense:gateway")
TOPUP_FEE = Account("expense:topup_fee")
GST = Account("liability:gst")
OWNER = Account("equity:owner")
SALES = Account("revenue:sales")

#: Every account the bot may touch. Anything else is a typo, and a typo in an
#: account name silently creates a phantom balance, so unknowns are rejected.
KNOWN_ACCOUNTS: frozenset[Account] = frozenset(
    {CASH, WALLET, COGS, GATEWAY, TOPUP_FEE, GST, OWNER, SALES}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    ref        TEXT    NOT NULL,
    account    TEXT    NOT NULL,
    debit_p    INTEGER NOT NULL DEFAULT 0,
    credit_p   INTEGER NOT NULL DEFAULT 0,
    memo       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_postings_ref ON postings(ref);
CREATE INDEX IF NOT EXISTS idx_postings_account ON postings(account);
CREATE INDEX IF NOT EXISTS idx_postings_ts ON postings(ts);
"""


@dataclass(frozen=True, slots=True)
class Posting:
    """One debit/credit pair. Amount is always positive; sides say direction."""

    ts: str
    ref: str
    debit: Account
    credit: Account
    amount: Money
    memo: str = ""

    def __post_init__(self) -> None:
        if self.amount.is_negative:
            raise LedgerError(
                f"posting amount must be positive (use the debit/credit sides to "
                f"express direction), got {self.amount}"
            )
        if self.amount.is_zero:
            raise LedgerError("refusing to record a zero posting")
        if self.debit == self.credit:
            raise LedgerError(f"posting debits and credits the same account {self.debit}")
        for acct in (self.debit, self.credit):
            if acct not in KNOWN_ACCOUNTS:
                raise LedgerError(
                    f"unknown account {acct!r}; known: {sorted(KNOWN_ACCOUNTS)}"
                )

    @property
    def row(self) -> tuple[str, str, str, int, int, str]:
        return (
            self.ts, self.ref, str(self.debit), self.amount.paise, 0, self.memo
        )

    @property
    def rows(self) -> tuple[tuple, tuple]:
        """The two SQL rows this posting expands into."""
        return (
            (self.ts, self.ref, str(self.debit), self.amount.paise, 0, self.memo),
            (self.ts, self.ref, str(self.credit), 0, self.amount.paise, self.memo),
        )


@dataclass(frozen=True, slots=True)
class PnL:
    """Derived profit and loss. Never stored -- always recomputed."""

    revenue: Money
    cogs: Money
    gateway: Money
    topup_fees: Money
    #: GST collected from customers; a liability, not revenue.
    gst_collected: Money
    wallet_balance: Money
    cash_balance: Money
    owner_equity: Money

    @property
    def gross_profit(self) -> Money:
        return self.revenue - self.cogs

    @property
    def operating_expenses(self) -> Money:
        return self.gateway + self.topup_fees

    @property
    def net_profit(self) -> Money:
        """Revenue less cost of numbers less operating expenses.

        GST is deliberately excluded: it was never the business's money.
        Including it would overstate profit by 18% on every sale.
        """
        return self.gross_profit - self.operating_expenses

    @property
    def gross_margin_ratio(self) -> Decimal:
        if self.revenue.is_zero:
            return Decimal(0)
        return Decimal(self.gross_profit.paise) / Decimal(self.revenue.paise)

    @property
    def net_margin_ratio(self) -> Decimal:
        if self.revenue.is_zero:
            return Decimal(0)
        return Decimal(self.net_profit.paise) / Decimal(self.revenue.paise)

    def as_dict(self) -> dict[str, str]:
        return {
            "revenue": self.revenue.to_plain(),
            "cogs": self.cogs.to_plain(),
            "gross_profit": self.gross_profit.to_plain(),
            "gateway_fees": self.gateway.to_plain(),
            "topup_fees": self.topup_fees.to_plain(),
            "net_profit": self.net_profit.to_plain(),
            "gross_margin": f"{self.gross_margin_ratio:.2%}",
            "net_margin": f"{self.net_margin_ratio:.2%}",
            "gst_collected": self.gst_collected.to_plain(),
            "wallet_balance": self.wallet_balance.to_plain(),
            "cash_balance": self.cash_balance.to_plain(),
        }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    """Append-only double-entry ledger backed by SQLite."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self._conn = sqlite3.connect(str(path) if path else ":memory:")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- writing ---------------------------------------------------------
    def post(
        self,
        debit: Account,
        credit: Account,
        amount: Money,
        *,
        ref: str,
        memo: str = "",
        ts: Optional[str] = None,
    ) -> Posting:
        """Record one balanced movement."""
        p = Posting(ts or _utcnow(), ref, debit, credit, amount, memo)
        self._conn.executemany(
            "INSERT INTO postings (ts, ref, account, debit_p, credit_p, memo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            p.rows,
        )
        self._conn.commit()
        return p

    def post_many(self, postings: Iterable[Posting]) -> None:
        """Record a batch atomically -- all of it or none of it."""
        rows: list[tuple] = []
        for p in postings:
            rows.extend(p.rows)
        with self._conn:
            self._conn.executemany(
                "INSERT INTO postings (ts, ref, account, debit_p, credit_p, memo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def close(self) -> None:
        self._conn.close()

    # -- reading ---------------------------------------------------------
    def balance(self, account: Account) -> Money:
        """Net balance, positive in the account's normal direction."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(debit_p),0), COALESCE(SUM(credit_p),0) "
            "FROM postings WHERE account = ?",
            (str(account),),
        ).fetchone()
        debit, credit = int(row[0]), int(row[1])
        return Money(debit - credit) if account.is_debit_normal else Money(credit - debit)

    def trial_balance(self) -> dict[Account, Money]:
        """Balance of every account that has activity."""
        accounts = {
            Account(a)
            for (a,) in self._conn.execute("SELECT DISTINCT account FROM postings")
        }
        return {a: self.balance(a) for a in sorted(accounts)}

    def verify(self) -> None:
        """Assert the fundamental invariant. Raises if the ledger is broken."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(debit_p),0), COALESCE(SUM(credit_p),0) FROM postings"
        ).fetchone()
        total_debit, total_credit = int(row[0]), int(row[1])
        if total_debit != total_credit:
            raise LedgerError(
                f"ledger is out of balance: debits {Money(total_debit)} != "
                f"credits {Money(total_credit)} (off by {Money(total_debit - total_credit)})"
            )

    def profit_and_loss(self) -> PnL:
        self.verify()
        return PnL(
            revenue=max(self.balance(SALES), Money.zero()),
            cogs=self.balance(COGS),
            gateway=self.balance(GATEWAY),
            topup_fees=self.balance(TOPUP_FEE),
            gst_collected=self.balance(GST),
            wallet_balance=self.balance(WALLET),
            cash_balance=self.balance(CASH),
            owner_equity=self.balance(OWNER),
        )

    def ledger_for(self, ref: str) -> list[Posting]:
        """Every posting sharing a reference -- the audit trail of one order."""
        rows = self._conn.execute(
            "SELECT ts, ref, account, debit_p, credit_p, memo FROM postings "
            "WHERE ref = ? ORDER BY id",
            (ref,),
        ).fetchall()
        out: list[Posting] = []
        for ts, r, acct, d, c, memo in rows:
            if d:
                out.append(Posting(ts, r, Account(acct), WALLET, Money(d), memo))
            else:
                out.append(Posting(ts, r, CASH, Account(acct), Money(c), memo))
        return out

    def history(self, limit: int = 200) -> list[tuple]:
        return self._conn.execute(
            "SELECT ts, ref, account, debit_p, credit_p, memo FROM postings "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # -- convenience flows ----------------------------------------------
    def record_topup(
        self,
        paid: Money,
        credited: Money,
        rail_fee: Money,
        *,
        ref: str,
        memo: str = "",
    ) -> None:
        """Owner funds the wallet.

        ``paid`` leaves the bank; ``credited`` lands in the wallet. Any bonus
        (credited > paid) is owner capital injected as free credit, and the
        rail fee is a real expense -- so a bonus pack's true discount shows up
        in the books instead of being hand-waved.
        """
        if credited.paise < paid.paise - rail_fee.paise:
            raise LedgerError(
                f"top-up credited {credited} is less than paid {paid} less fee {rail_fee}"
            )
        # Cash leaves for the paid amount plus the rail fee; the wallet is
        # credited the full spendable amount, with any bonus attributed to
        # owner capital rather than appearing from nowhere.
        postings: list[Posting] = []
        if not paid.is_zero:
            postings.append(Posting(_utcnow(), ref, WALLET, CASH, paid, f"topup {memo}"))
        bonus = credited - paid
        if bonus.paise > 0:
            postings.append(Posting(_utcnow(), ref, WALLET, OWNER, bonus, "pack bonus credit"))
        if not rail_fee.is_zero:
            postings.append(Posting(_utcnow(), ref, TOPUP_FEE, CASH, rail_fee, "payment rail fee"))
        if not postings:
            raise LedgerError("top-up had no money to record")
        self.post_many(postings)

    def record_number_purchase(self, cost: Money, *, ref: str, memo: str = "") -> None:
        """A number is bought out of the wallet."""
        self.post(COGS, WALLET, cost, ref=ref, memo=memo or "number purchase")

    def record_number_refund(self, amount: Money, *, ref: str, memo: str = "") -> None:
        """Provider credits a failed number back to the wallet."""
        self.post(WALLET, COGS, amount, ref=ref, memo=memo or "provider refund")

    def record_sale(
        self,
        gross: Money,
        gateway_fee: Money,
        gst: Money,
        *,
        ref: str,
        memo: str = "",
    ) -> None:
        """Customer pays.

        The gross splits three ways *at the moment of sale*: GST into a
        liability (never revenue), the PSP fee into expense, the remainder
        into revenue. Splitting at sale time rather than at reconciliation is
        what keeps revenue honest -- a business that books the gross as
        revenue overstates profit by the GST on every single order.

        Posting shape, for gross G, fee F, GST T::

            Dr cash  (G - T)   Cr revenue:sales (G - T)
            Dr cash  T         Cr liability:gst T
            Dr expense:gateway F   Cr cash F

        Net cash is G - F: the PSP remits the gross less its fee, and the GST
        sits in that cash until it is paid over.
        """
        after_gst = gross - gst
        if after_gst.is_negative:
            raise LedgerError(f"sale of {gross} cannot absorb {gst} of GST")
        if (gross - gateway_fee).is_negative:
            raise LedgerError(f"sale of {gross} cannot absorb {gateway_fee} of fees")
        # The binding constraint: GST plus fees must not exceed the price.
        if (gross - gst - gateway_fee).is_negative:
            raise LedgerError(
                f"sale of {gross} cannot absorb {gateway_fee} of fees + {gst} of GST"
            )
        postings = [Posting(_utcnow(), ref, CASH, SALES, after_gst, memo or "sale")]
        if not gst.is_zero:
            postings.append(Posting(_utcnow(), ref, CASH, GST, gst, "GST collected"))
        if not gateway_fee.is_zero:
            postings.append(Posting(_utcnow(), ref, GATEWAY, CASH, gateway_fee, "PSP fee"))
        self.post_many(postings)

    def record_customer_refund(self, amount: Money, *, ref: str, memo: str = "") -> None:
        """Refund a customer. Reduces revenue; the number cost stays in COGS."""
        self.post(SALES, CASH, amount, ref=ref, memo=memo or "customer refund")
