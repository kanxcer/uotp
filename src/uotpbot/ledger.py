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
    revenue:platform_fee  the platform's cut of a white-label sale
"""

from __future__ import annotations

import sqlite3
import threading
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
#: The platform's cut of a white-label sale. Kept separate from ``revenue:sales``
#: so a sub-bot owner's books show exactly what they kept and what the platform
#: took -- a fee that is invisible in the ledger cannot be audited, and an
#: unauditable fee is indistinguishable from a hidden one.
PLATFORM_FEE = Account("revenue:platform_fee")

#: Every account the bot may touch. Anything else is a typo, and a typo in an
#: account name silently creates a phantom balance, so unknowns are rejected.
KNOWN_ACCOUNTS: frozenset[Account] = frozenset(
    {CASH, WALLET, COGS, GATEWAY, TOPUP_FEE, GST, OWNER, SALES, PLATFORM_FEE}
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
    #: The platform's cut of white-label sales. Reported separately so it can
    #: never be folded into the sub-bot owner's revenue by accident.
    platform_fee: Money = Money(0)

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

        ``revenue`` already excludes any platform fee, so a sub-bot owner's
        figure is what they actually kept.
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
            "platform_fee": self.platform_fee.to_plain(),
            "gst_collected": self.gst_collected.to_plain(),
            "wallet_balance": self.wallet_balance.to_plain(),
            "cash_balance": self.cash_balance.to_plain(),
        }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    """Append-only double-entry ledger backed by SQLite.

    Storage-independence note: every SQL statement routes through :meth:`_q`
    (table name + placeholder style) and the two primitives :meth:`_execute`
    and :meth:`_write`. That is the entire seam a different backend has to
    provide; see :class:`pgstore.PostgresLedger`. The double-entry logic --
    which is the part where a bug costs real money -- lives only here, so it
    is tested once and shared by every backend.
    """

    #: Row shape of one posting after expansion. Placeholders are ``?`` here;
    #: :meth:`_q` translates for other drivers.
    _INSERT_SQL = (
        "INSERT INTO {t} (ts, ref, account, debit_p, credit_p, memo) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )

    def __init__(self, path: Optional[Path | str] = None) -> None:
        # check_same_thread=False is required, not a convenience: an HTTP
        # server handles each request on a fresh thread, and without it every
        # ledger read from a handler raises ProgrammingError. That is not a
        # cosmetic failure -- it makes /readyz return 500 forever, so the
        # platform never marks the service healthy.
        #
        # Sharing the connection across threads means the caller must
        # serialise access, hence the lock below. Every operation takes it.
        self._table = "postings"
        self._ph = "?"
        self._conn = sqlite3.connect(
            str(path) if path else ":memory:", check_same_thread=False
        )
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- backend seam ------------------------------------------------------
    def _q(self, sql: str) -> str:
        """Translate canonical SQL (``{t}`` table, ``?`` placeholders)."""
        return sql.replace("{t}", self._table).replace("?", self._ph)

    def _tx(self):
        """A write transaction context. sqlite: the connection itself."""
        return self._conn

    # -- locked access ---------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return list(self._conn.execute(self._q(sql), params))

    def _write(self, rows: list[tuple]) -> None:
        with self._lock, self._tx():
            self._conn.executemany(self._q(self._INSERT_SQL), rows)

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
        self._write(list(p.rows))
        return p

    def post_many(self, postings: Iterable[Posting]) -> None:
        """Record a batch atomically -- all of it or none of it."""
        rows: list[tuple] = []
        for p in postings:
            rows.extend(p.rows)
        self._write(rows)

    def close(self) -> None:
        self._conn.close()

    # -- reading ---------------------------------------------------------
    def balance(self, account: Account) -> Money:
        """Net balance, positive in the account's normal direction."""
        row = self._execute(
            "SELECT COALESCE(SUM(debit_p),0), COALESCE(SUM(credit_p),0) "
            "FROM {t} WHERE account = ?",
            (str(account),),
        )[0]
        debit, credit = int(row[0]), int(row[1])
        return Money(debit - credit) if account.is_debit_normal else Money(credit - debit)

    def trial_balance(self) -> dict[Account, Money]:
        """Balance of every account that has activity."""
        accounts = {Account(a) for (a,) in self._execute("SELECT DISTINCT account FROM {t}")}
        return {a: self.balance(a) for a in sorted(accounts)}

    def verify(self) -> None:
        """Assert the fundamental invariant. Raises if the ledger is broken."""
        row = self._execute(
            "SELECT COALESCE(SUM(debit_p),0), COALESCE(SUM(credit_p),0) FROM {t}"
        )[0]
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
            platform_fee=max(self.balance(PLATFORM_FEE), Money.zero()),
        )

    def ledger_for(self, ref: str) -> list[Posting]:
        """Every posting sharing a reference -- the audit trail of one order."""
        rows = self._execute(
            "SELECT ts, ref, account, debit_p, credit_p, memo FROM {t} "
            "WHERE ref = ? ORDER BY id",
            (ref,),
        )
        out: list[Posting] = []
        for ts, r, acct, d, c, memo in rows:
            if d:
                out.append(Posting(ts, r, Account(acct), WALLET, Money(d), memo))
            else:
                out.append(Posting(ts, r, CASH, Account(acct), Money(c), memo))
        return out

    def history(self, limit: int = 200) -> list[tuple]:
        return self._execute(
            "SELECT ts, ref, account, debit_p, credit_p, memo FROM {t} "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

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

    def record_sale_split(
        self,
        gross: Money,
        gateway_fee: Money,
        gst: Money,
        platform_fee: Money,
        *,
        ref: str,
        memo: str = "",
    ) -> None:
        """A white-label sale, split at the moment of sale.

        Of the gross: GST goes to a liability, the platform's cut goes to
        ``revenue:platform_fee``, the PSP fee goes to expense, and the
        remainder to the sub-bot owner's revenue. Splitting at sale time means
        the owner's reported revenue is already net of the platform fee, so the
        two can never silently disagree.

        The PSP fee is booked as an expense rather than netted out of revenue,
        matching :meth:`record_sale`. Netting it would double-count: once
        against revenue and again against cash.
        """
        if platform_fee.is_negative:
            raise LedgerError("platform_fee cannot be negative")
        owner_revenue = gross - gst - platform_fee
        if owner_revenue.is_negative:
            raise LedgerError(
                f"sale of {gross} cannot absorb {gst} GST + {platform_fee} platform fee"
            )
        if (gross - gateway_fee).is_negative:
            raise LedgerError(f"sale of {gross} cannot absorb {gateway_fee} of fees")
        postings = [Posting(_utcnow(), ref, CASH, SALES, owner_revenue, memo or "sale")]
        if not platform_fee.is_zero:
            postings.append(
                Posting(_utcnow(), ref, CASH, PLATFORM_FEE, platform_fee, "platform fee")
            )
        if not gst.is_zero:
            postings.append(Posting(_utcnow(), ref, CASH, GST, gst, "GST collected"))
        if not gateway_fee.is_zero:
            postings.append(Posting(_utcnow(), ref, GATEWAY, CASH, gateway_fee, "PSP fee"))
        self.post_many(postings)

    def platform_revenue(self) -> Money:
        """Total the platform has earned from white-label sales."""
        return max(self.balance(PLATFORM_FEE), Money.zero())

    def record_customer_refund(self, amount: Money, *, ref: str, memo: str = "") -> None:
        """Refund a customer. Reduces revenue; the number cost stays in COGS.

        Idempotent per ``ref``: the customer refund for a given order ref is
        posted at most ONCE, no matter how many code paths (engine timeout,
        a cancel, a retry worker) race to post it. A duplicate posting would
        make reported refunds exceed the money actually returned -- exactly
        the drift this ledger exists to prevent. Callers that must *know*
        whether a refund line already exists (to skip their own post) use
        :meth:`has_customer_refund`.
        """
        if self.has_customer_refund(ref):
            return
        self.post(SALES, CASH, amount, ref=ref, memo=memo or "customer refund")

    def has_customer_refund(self, ref: str) -> bool:
        """True when a customer-refund line (Dr sales / Cr cash) exists for ``ref``.

        The sale postings credit ``revenue:sales``; only a customer refund
        *debits* it, so a debit row on that account is unambiguous.
        """
        row = self._execute(
            "SELECT COUNT(*) FROM {t} WHERE ref = ? AND account = ? AND debit_p > 0",
            (ref, str(SALES)),
        )[0]
        return int(row[0]) > 0
