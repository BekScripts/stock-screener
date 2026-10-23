"""Opening a new paper book, once, deliberately, with its provenance recorded.

The runtime needs three records that do not yet exist: an accounting ledger, an
execution journal, and a binding that says which account and which strategy they
belong to. This creates them.

The distinction this module exists to protect is between **initialisation** and
**repair**, because the code for each would look almost identical and only one
of them is allowed:

- *Initialisation* reads the venue once, with an operator watching, to establish
  what a brand-new internal book starts from. It happens exactly once per
  ledger generation and it is recorded with provenance to the snapshot it came
  from.
- *Repair* would be the system noticing a disagreement between its book and the
  venue later, and quietly adopting the venue's version. That is forbidden
  everywhere in this programme, because the venue is evidence and the book is
  the authority — and a system that silently adopts the venue's numbers cannot
  detect the one thing reconciliation exists to detect.

So after bootstrap, broker state never writes to accounting again. The boundary
between the two is the `bootstrap_epoch`, and it is a recorded fact rather than
an implicit consequence of when the directory happened to be created.

An account with existing positions is refused outright. Importing them would
mean inventing a cost basis — the venue reports an average entry price, not the
lots behind it, and a guessed basis becomes a wrong realised P&L on the first
sale and stays wrong forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "BOOTSTRAP_ARTEFACT_VERSION",
    "CASH_FIELD",
    "PaperBookBootstrapReport",
    "bootstrap_paper_book",
]

#: The bootstrap artefact contract.
BOOTSTRAP_ARTEFACT_VERSION = "PAPER_BOOK_BOOTSTRAP_V1"

#: Which provider field opens the book, named explicitly because the account
#: payload offers several numbers and only one of them means what internal
#: accounting cash means.
#:
#: `cash` is the unlevered settled balance. `buying_power` is a margin-derived
#: figure the venue computes and this system deliberately ignores — it already
#: carries that field as "recorded but unused", because what this system can
#: afford is decided from its own book. `equity` and `portfolio_value` include
#: the marked value of positions, so opening a book from either would record
#: position value as cash and double-count it the moment the positions were
#: also recorded.
CASH_FIELD = "cash"


@dataclass(frozen=True)
class PaperBookBootstrapReport:
    """What a bootstrap established, safe to print and to keep.

    Attributes:
        runtime_id: The runtime opened.
        account_binding_fingerprint: Which paper account, digested.
        bootstrap_epoch: The instant the book begins.
        opening_cash: The cash the book opens with.
        cash_field: Which provider field supplied it.
        positions_at_venue: How many holdings the venue reported. Must be zero.
        open_orders_at_venue: How many working orders. Must be zero.
        accounting_generation: The ledger lineage created.
        execution_journal_generation: The journal lineage created.
        opening_event_id: The accounting event that opened the book.
        account_evidence_fingerprint: The digest of the snapshot it came from.
        binding_fingerprint: The runtime binding written.
        mutating_requests: Always zero. A bootstrap reads.
        limitations: What this does not establish.
    """

    runtime_id: str
    account_binding_fingerprint: str
    bootstrap_epoch: str
    opening_cash: Decimal
    accounting_generation: str
    execution_journal_generation: str
    opening_event_id: str
    account_evidence_fingerprint: str
    binding_fingerprint: str
    cash_field: str = CASH_FIELD
    positions_at_venue: int = 0
    open_orders_at_venue: int = 0
    mutating_requests: int = 0
    limitations: tuple[str, ...] = field(
        default_factory=lambda: (
            "This opened an internal book from one authenticated snapshot. It is "
            "initialisation, not reconciliation: after this instant the venue is "
            "evidence and the ledger is authority, and no provider figure ever "
            "writes to accounting again.",
            "Activity at the venue before the bootstrap epoch — the P2.2E "
            "certification canary's two orders among it — is historical evidence "
            "and is deliberately absent from this book. It is not deleted or "
            "hidden; it simply predates this ledger's existence.",
            "A single cash figure is not an audited opening balance. It is what "
            "the venue reported at one instant, with an operator confirming it.",
        )
    )

    def body(self) -> dict[str, Any]:
        """Return the artefact's semantic content. Carries no credential."""
        return {
            "artefact_version": BOOTSTRAP_ARTEFACT_VERSION,
            "runtime_id": self.runtime_id,
            "provider": "ALPACA",
            "environment": "PAPER",
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "bootstrap_epoch": self.bootstrap_epoch,
            "opening_cash": str(self.opening_cash),
            "cash_field": self.cash_field,
            "cash_field_rationale": (
                "the unlevered settled balance; buying_power is margin-derived and "
                "ignored, equity and portfolio_value include position value"
            ),
            "positions_at_venue": self.positions_at_venue,
            "open_orders_at_venue": self.open_orders_at_venue,
            "accounting_engine_version": "PORTFOLIO_ACCOUNTING_V2",
            "accounting_generation": self.accounting_generation,
            "execution_journal_generation": self.execution_journal_generation,
            "opening_event_id": self.opening_event_id,
            "account_evidence_fingerprint": self.account_evidence_fingerprint,
            "binding_fingerprint": self.binding_fingerprint,
            "mutating_requests": self.mutating_requests,
            "classification": [
                "PAPER_BOOK_INITIALISATION",
                "NOT_RECONCILIATION_REPAIR",
                "NOT_LIVE_TRADING",
            ],
            "historical_activity_imported": False,
            "positions_imported": False,
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the artefact."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artefact with its fingerprint."""
        return {**self.body(), "bootstrap_fingerprint": self.fingerprint()}

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal. Carries no credential."""
        return (
            f"runtime            : {self.runtime_id}",
            f"account binding    : {self.account_binding_fingerprint[:16]}…",
            f"bootstrap epoch    : {self.bootstrap_epoch}",
            f"opening cash       : {self.opening_cash} (provider field {self.cash_field!r})",
            f"positions at venue : {self.positions_at_venue}",
            f"open orders        : {self.open_orders_at_venue}",
            f"accounting         : PORTFOLIO_ACCOUNTING_V2 / {self.accounting_generation}",
            f"execution journal  : {self.execution_journal_generation}",
            f"opening event      : {self.opening_event_id}",
            f"mutating requests  : {self.mutating_requests}",
            f"binding            : {self.binding_fingerprint[:32]}…",
            "positions imported : no — a guessed cost basis is wrong forever",
            "history imported   : no — pre-epoch activity is historical evidence",
            "repair             : none; this is initialisation",
        )


def bootstrap_paper_book(
    *,
    root: Path,
    runtime_id: str,
    account: Any,
    account_evidence_fingerprint: str,
    open_orders: int,
    strategy_id: str,
    strategy_version: str,
    strategy_fingerprint: str,
    strategy_authorization_fingerprint: str,
    operations_policy_fingerprint: str,
    calendar_fingerprint: str,
    schedule_fingerprint: str,
    code_commit: str,
    now: str,
    confirmed: bool = False,
) -> PaperBookBootstrapReport:
    """Open a new paper book, once, with an operator confirming.

    Args:
        root: The operations root this runtime will own.
        runtime_id: A stable name for the runtime.
        account: The proven `ExternalAccountSnapshot`.
        account_evidence_fingerprint: The digest of the response it came from.
        open_orders: How many working orders the venue reported.
        strategy_id: The strategy this runtime operates.
        strategy_version: Its version.
        strategy_fingerprint: Its exact policy digest.
        strategy_authorization_fingerprint: The grant approving it for paper.
        operations_policy_fingerprint: The operational rules in force.
        calendar_fingerprint: The sessions.
        schedule_fingerprint: The slots.
        code_commit: The build.
        now: The operational instant, ISO-8601 with offset.
        confirmed: The operator's explicit confirmation.

    Returns:
        The bootstrap report.

    Raises:
        RuntimeBindingError: `BOOTSTRAP_NOT_CONFIRMED` without confirmation;
            `BOOTSTRAP_ALREADY_DONE` if this root already holds a book;
            `BOOTSTRAP_POSITION_STATE_NOT_EMPTY` if the venue holds positions;
            `BOOTSTRAP_OPEN_ORDERS_PRESENT` if it holds working orders;
            `BOOTSTRAP_CASH_FIELD_AMBIGUOUS` if the snapshot carries no cash.
            Every one of these fails before anything durable is written.
    """
    from stock_screener.execution import ExecutionStore
    from stock_screener.paper_operations import atomic_write_verified
    from stock_screener.portfolio_accounting import (
        ACCOUNTING_V2,
        EventType,
        OpeningBalanceEvent,
        PortfolioStore,
    )

    from .binding import (
        RUNTIME_DIRECTORY,
        PaperOperationsRuntimeBinding,
        RuntimeBindingStore,
    )

    if not confirmed:
        raise RuntimeBindingError(
            Code.BOOTSTRAP_NOT_CONFIRMED,
            "a bootstrap opens an internal accounting book from a provider snapshot "
            "and needs an operator to confirm it. Nothing was written",
        )

    bindings = RuntimeBindingStore(root)
    if bindings.read() is not None:
        raise RuntimeBindingError(
            Code.BOOTSTRAP_ALREADY_DONE,
            f"{root} already holds a bound runtime. A book is opened once per ledger "
            "generation; re-opening it would create a second opening balance and the "
            "ledger's history would no longer explain its cash",
        )

    positions = tuple(getattr(account, "positions", ()) or ())
    if positions:
        raise RuntimeBindingError(
            Code.BOOTSTRAP_POSITION_STATE_NOT_EMPTY,
            f"the venue reports {len(positions)} position(s). They are not imported: "
            "the venue reports an average entry price, not the lots behind it, so any "
            "cost basis taken from here would be a guess — and a guessed basis becomes "
            "a wrong realised P&L on the first sale and stays wrong. Start from a "
            "clean account, or reconstruct the basis through an explicit mechanism "
            "that can prove it",
        )
    if open_orders:
        raise RuntimeBindingError(
            Code.BOOTSTRAP_OPEN_ORDERS_PRESENT,
            f"the venue holds {open_orders} working order(s). A book opened while an "
            "order is live would miss its fill: the fill would arrive against a ledger "
            "that has no record of the order that caused it",
        )

    cash = getattr(account, CASH_FIELD, None)
    if cash is None:
        raise RuntimeBindingError(
            Code.BOOTSTRAP_CASH_FIELD_AMBIGUOUS,
            f"the account snapshot carries no {CASH_FIELD!r}. No other field is "
            "substituted: buying_power is margin-derived, and equity and "
            "portfolio_value include the marked value of positions, so opening from "
            "either would record position value as cash",
        )

    generation = f"alphatrader-paper-{runtime_id}"
    portfolio = PortfolioStore(
        root / RUNTIME_DIRECTORY / "portfolio",
        accounting_version=ACCOUNTING_V2,
        generation=generation,
    )
    execution = ExecutionStore(
        root / RUNTIME_DIRECTORY / "execution",
        generation=generation,
        execution_plan_id=f"bootstrap-{runtime_id}",
    )
    # Materialise the journal lineage now. The marker is normally written with
    # the first head anchor, so a runtime that has not traded would otherwise
    # have no execution journal on disk at all — and "bound but empty" must be
    # distinguishable from "absent" before a soak is armed against it.
    execution.ensure_generation()
    execution.load_journal()

    opening = OpeningBalanceEvent(
        event_id=f"opening-{generation}",
        event_type=EventType.OPENING_BALANCE,
        effective_at=now,
        source_id=f"ALPACA_PAPER_ACCOUNT_SNAPSHOT:{CASH_FIELD}",
        source_fingerprint=account_evidence_fingerprint,
        cash=Decimal(str(cash)),
    )
    # `commit` rather than a bare append: it writes the record, replays the
    # whole verified ledger, then anchors — in that order — so a crash between
    # the record and the anchor leaves a tail `recover` can resolve rather than
    # a book whose cash nothing explains.
    ledger = portfolio.load_ledger()
    portfolio.commit(ledger, opening)

    binding = PaperOperationsRuntimeBinding(
        runtime_id=runtime_id,
        runtime_root=str(root),
        account_binding_fingerprint=str(getattr(account, "account_binding_fingerprint", "") or "")
        or account_evidence_fingerprint,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_fingerprint=strategy_fingerprint,
        strategy_authorization_fingerprint=strategy_authorization_fingerprint,
        accounting_generation=generation,
        execution_journal_generation=generation,
        operations_policy_fingerprint=operations_policy_fingerprint,
        calendar_fingerprint=calendar_fingerprint,
        schedule_fingerprint=schedule_fingerprint,
        bootstrap_epoch=now,
        bootstrap_evidence_fingerprint=account_evidence_fingerprint,
        code_commit=code_commit,
        created_at=now,
    )
    bindings.write(binding)

    report = PaperBookBootstrapReport(
        runtime_id=runtime_id,
        account_binding_fingerprint=binding.account_binding_fingerprint,
        bootstrap_epoch=now,
        opening_cash=Decimal(str(cash)),
        accounting_generation=generation,
        execution_journal_generation=generation,
        opening_event_id=opening.event_id,
        account_evidence_fingerprint=account_evidence_fingerprint,
        binding_fingerprint=binding.fingerprint(),
    )
    atomic_write_verified(root / RUNTIME_DIRECTORY / "bootstrap.json", report.as_json())
    return report


def read_bootstrap(root: Path) -> dict[str, Any] | None:
    """Read a runtime's bootstrap artefact.

    Args:
        root: The operations root.

    Returns:
        The artefact, or `None` when this root has not been bootstrapped.
    """
    from .binding import RUNTIME_DIRECTORY

    path = root / RUNTIME_DIRECTORY / "bootstrap.json"
    if not path.exists():
        return None
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body
