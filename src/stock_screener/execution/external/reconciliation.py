"""Comparing three records of the same trading, and repairing none of them.

Three parties can describe the same day's trading, and in a correct system they
agree:

* the **journal** — which orders this system created and what it believes filled;
* the **accounting** book — the positions and cash those fills produced;
* the **venue** — what the broker says exists.

Two-way reconciliation cannot localise a disagreement. If the book and the venue
differ on a position, the cause might be a fill the venue never reported, a fill
the book never admitted, or an order placed by hand in the provider's console —
and those have different responses. Comparing all three says which.

**Nothing here repairs anything.** There is no function that adopts the venue's
figure, and that absence is deliberate and tested for. The reason is the one P1.3
gave and it has not improved with a third party: a break means one of the records
is wrong, and *which* is an investigation. Overwriting the book with the broker's
number does not resolve the break — it destroys the evidence that there was one,
and if the broker was the wrong one it writes a false position into an
append-only ledger that will carry it forever.

So the output is findings, ranked. `BLOCKING` stops execution: planning a new
order against a book that is missing a trade sizes it against money that is not
there. `WARNING` is a difference that does not threaten the next decision.
`INFO` records an expected difference — the venue's cost basis convention is not
this project's, and saying so once is better than rediscovering it every run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import quantise_cash, quantise_quantity

from ..errors import Code
from .client_ids import client_order_id_for, is_our_client_order_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.portfolio_accounting import PortfolioState

    from ..models import Order
    from .models import ExternalOrderSnapshot, ExternalPaperAccountSnapshot
    from .symbols import SymbolMapping

__all__ = [
    "EXTERNAL_RECONCILIATION_POLICY",
    "ExternalBreak",
    "ExternalBreakSeverity",
    "ExternalReconciliationResult",
    "ReconciliationVerdict",
    "reconcile_external",
]

#: The three-way contract. There is deliberately no auto-repair variant, and no
#: parameter that would enable one.
EXTERNAL_RECONCILIATION_POLICY = "EXTERNAL_PAPER_THREE_WAY_RECONCILIATION_V1"


class ExternalBreakSeverity(StrEnum):
    """How much a finding threatens the next decision."""

    BLOCKING = "BLOCKING"
    """Execution must stop. The book cannot be trusted to size another order."""

    WARNING = "WARNING"
    """A real difference that does not by itself invalidate the next decision."""

    INFO = "INFO"
    """An expected, explained difference. Recorded so it is not rediscovered."""


class ReconciliationVerdict(StrEnum):
    """What the sweep concluded."""

    RECONCILED = "RECONCILED"
    BREAKS_WARNING = "BREAKS_WARNING"
    BREAKS_BLOCKING = "BREAKS_BLOCKING"

    @property
    def may_execute(self) -> bool:
        """Whether execution may continue."""
        return self is not ReconciliationVerdict.BREAKS_BLOCKING


@dataclass(frozen=True)
class ExternalBreak:
    """One finding.

    Attributes:
        code: Which kind of break.
        severity: How much it threatens the next decision.
        subject: What it concerns — an identity label, a venue symbol, an order
            id, or `CASH`.
        journal: What the journal says, where it has a view.
        accounting: What the book says, where it has a view.
        remote: What the venue says.
        difference: The signed difference, where one is meaningful.
        likely_causes: Investigation starting points. Never repair instructions.
    """

    code: str
    severity: ExternalBreakSeverity
    subject: str
    remote: str
    journal: str | None = None
    accounting: str | None = None
    difference: str | None = None
    likely_causes: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the finding as a JSON-safe mapping."""
        return {
            "code": self.code,
            "severity": str(self.severity),
            "subject": self.subject,
            "journal": self.journal,
            "accounting": self.accounting,
            "remote": self.remote,
            "difference": self.difference,
            "likely_causes": list(self.likely_causes),
        }


@dataclass(frozen=True)
class ExternalReconciliationResult:
    """What comparing the three records established.

    Attributes:
        breaks: Every finding, most severe first.
        orders_compared: How many local orders were checked against the venue.
        positions_compared: How many holdings were compared.
        cash_compared: Whether cash could be compared at all.
        cash_tolerance: The tolerance that was applied, as declared.
        policy: The reconciliation contract.
    """

    breaks: tuple[ExternalBreak, ...]
    orders_compared: int = 0
    positions_compared: int = 0
    cash_compared: bool = False
    cash_tolerance: Decimal = field(default_factory=lambda: Decimal(0))
    policy: str = EXTERNAL_RECONCILIATION_POLICY

    @property
    def blocking(self) -> tuple[ExternalBreak, ...]:
        """Every finding that must stop execution."""
        return tuple(row for row in self.breaks if row.severity is ExternalBreakSeverity.BLOCKING)

    @property
    def verdict(self) -> ReconciliationVerdict:
        """What the sweep concluded."""
        if self.blocking:
            return ReconciliationVerdict.BREAKS_BLOCKING
        if any(row.severity is ExternalBreakSeverity.WARNING for row in self.breaks):
            return ReconciliationVerdict.BREAKS_WARNING
        return ReconciliationVerdict.RECONCILED

    @property
    def may_execute(self) -> bool:
        """Whether execution may continue after this sweep."""
        return self.verdict.may_execute

    def as_json(self) -> dict[str, Any]:
        """Return the result as a JSON-safe mapping."""
        return {
            "policy": self.policy,
            "verdict": str(self.verdict),
            "may_execute": self.may_execute,
            "breaks": [row.as_json() for row in self.breaks],
            "blocking_count": len(self.blocking),
            "orders_compared": self.orders_compared,
            "positions_compared": self.positions_compared,
            "cash_compared": self.cash_compared,
            "cash_tolerance": str(self.cash_tolerance),
            "repair": "NONE — this module reports breaks and repairs nothing",
        }


_SEVERITY_ORDER = {
    ExternalBreakSeverity.BLOCKING: 0,
    ExternalBreakSeverity.WARNING: 1,
    ExternalBreakSeverity.INFO: 2,
}


def _order_breaks(
    orders: Sequence[Order],
    remote_orders: Sequence[ExternalOrderSnapshot],
    local_filled: Mapping[str, Decimal],
) -> list[ExternalBreak]:
    """Compare the journal's orders against the venue's.

    Args:
        orders: Local orders that were submitted.
        remote_orders: What the venue reports.
        local_filled: Filled quantity per local order id, from the journal.

    Returns:
        The findings.
    """
    findings: list[ExternalBreak] = []
    by_client_id = {client_order_id_for(order.order_id): order for order in orders}
    seen: set[str] = set()

    for snapshot in remote_orders:
        client_id = snapshot.client_order_id.strip()
        local = by_client_id.get(client_id)
        if local is None:
            ours = is_our_client_order_id(client_id)
            traded = snapshot.filled_quantity > 0
            findings.append(
                ExternalBreak(
                    code=Code.UNEXPECTED_REMOTE_ORDER,
                    severity=(
                        ExternalBreakSeverity.BLOCKING
                        if traded or ours
                        else ExternalBreakSeverity.WARNING
                    ),
                    subject=snapshot.remote_order_id,
                    remote=(
                        f"{snapshot.side} {snapshot.quantity} {snapshot.symbol}, "
                        f"{snapshot.filled_quantity} filled, {snapshot.reading.provider_status}"
                    ),
                    journal="no local order carries this client order id",
                    likely_causes=(
                        "an order placed by hand in the provider's console"
                        if not ours
                        else "a local journal that is missing an order this system placed",
                        "a credential shared with another process or experiment",
                        "a journal restored from an older generation than the venue",
                    ),
                )
            )
            continue
        seen.add(client_id)
        local_qty = local_filled.get(local.order_id, Decimal(0))
        if snapshot.filled_quantity != local_qty:
            missing_locally = snapshot.filled_quantity > local_qty
            findings.append(
                ExternalBreak(
                    code=(
                        Code.REMOTE_FILL_MISSING_LOCAL
                        if missing_locally
                        else Code.LOCAL_FILL_MISSING_REMOTE
                    ),
                    severity=ExternalBreakSeverity.BLOCKING,
                    subject=local.order_id,
                    journal=f"{quantise_quantity(local_qty)} filled",
                    remote=f"{quantise_quantity(snapshot.filled_quantity)} filled",
                    difference=str(quantise_quantity(snapshot.filled_quantity - local_qty)),
                    likely_causes=(
                        (
                            "a fill that executed at the venue and was never read back"
                            if missing_locally
                            else "a fill posted locally that the venue has no record of"
                        ),
                        "a sweep that ran while the order was still executing",
                        ("a local journal and a venue account that are not the same book"),
                    ),
                )
            )
        elif snapshot.state is not None and snapshot.is_terminal and local_qty < local.quantity:
            findings.append(
                ExternalBreak(
                    code=Code.REMOTE_ORDER_MISMATCH,
                    severity=ExternalBreakSeverity.WARNING,
                    subject=local.order_id,
                    journal=(
                        f"{quantise_quantity(local_qty)} of "
                        f"{quantise_quantity(local.quantity)} filled"
                    ),
                    remote=f"terminal as {snapshot.reading.provider_status}",
                    likely_causes=(
                        "the order ended at the venue with quantity outstanding",
                        "a residual that needs a new child order rather than a revival",
                    ),
                )
            )

    for client_id, local in by_client_id.items():
        if client_id in seen:
            continue
        if local_filled.get(local.order_id, Decimal(0)) > 0:
            findings.append(
                ExternalBreak(
                    code=Code.LOCAL_FILL_MISSING_REMOTE,
                    severity=ExternalBreakSeverity.BLOCKING,
                    subject=local.order_id,
                    journal=f"{quantise_quantity(local_filled[local.order_id])} filled",
                    remote="the venue reports no order under this client order id",
                    likely_causes=(
                        "a fill posted against an order the venue never accepted",
                        "a venue query that did not cover the window the order is in",
                        "a local journal pointed at a different account than the venue",
                    ),
                )
            )
    return findings


def _position_breaks(
    state: PortfolioState,
    remote: ExternalPaperAccountSnapshot,
    mapping: SymbolMapping,
) -> tuple[list[ExternalBreak], int]:
    """Compare the book's holdings against the venue's.

    Args:
        state: The accounting book.
        remote: What the venue reports.
        mapping: The declared symbol mapping.

    Returns:
        The findings, and how many holdings were compared.
    """
    findings: list[ExternalBreak] = []
    compared = 0
    remote_by_key: dict[tuple[str, int], Decimal] = {}

    for position in remote.positions:
        if not mapping.knows_symbol(position.symbol):
            findings.append(
                ExternalBreak(
                    code=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
                    severity=(
                        ExternalBreakSeverity.BLOCKING
                        if position.quantity != 0
                        else ExternalBreakSeverity.WARNING
                    ),
                    subject=position.symbol,
                    remote=f"{quantise_quantity(position.quantity)} held",
                    accounting="this run mapped no identity to that symbol",
                    likely_causes=(
                        "a holding from an earlier run under a different mapping",
                        "a position opened by hand at the venue",
                        "a symbol this run deliberately did not map",
                    ),
                )
            )
            continue
        remote_by_key[mapping.identity_key_for(position.symbol)] = position.quantity

    for key, held in sorted(state.positions.items()):
        compared += 1
        remote_quantity = remote_by_key.pop(key, Decimal(0))
        if held.quantity != remote_quantity:
            findings.append(
                ExternalBreak(
                    code=Code.REMOTE_POSITION_MISMATCH,
                    severity=ExternalBreakSeverity.BLOCKING,
                    subject=f"{key[0]}:{key[1]}",
                    accounting=str(quantise_quantity(held.quantity)),
                    remote=str(quantise_quantity(remote_quantity)),
                    difference=str(quantise_quantity(remote_quantity - held.quantity)),
                    likely_causes=(
                        "a fill on one side that the other has not recorded",
                        "a corporate action the venue applied and this engine refuses",
                        "two books that were never aligned at inception",
                    ),
                )
            )

    for key, quantity in sorted(remote_by_key.items()):
        if quantity == 0:
            continue
        compared += 1
        findings.append(
            ExternalBreak(
                code=Code.REMOTE_POSITION_MISMATCH,
                severity=ExternalBreakSeverity.BLOCKING,
                subject=f"{key[0]}:{key[1]}",
                accounting="0 — the book holds none",
                remote=str(quantise_quantity(quantity)),
                difference=str(quantise_quantity(quantity)),
                likely_causes=(
                    "a fill the book never admitted",
                    "a position opened by another process using the same credential",
                ),
            )
        )
    return findings, compared


def reconcile_external(
    *,
    state: PortfolioState,
    orders: Sequence[Order],
    remote_orders: Sequence[ExternalOrderSnapshot],
    remote_account: ExternalPaperAccountSnapshot,
    mapping: SymbolMapping,
    local_filled: Mapping[str, Decimal],
    cash_tolerance: Decimal = Decimal(0),
) -> ExternalReconciliationResult:
    """Compare the journal, the book and the venue.

    Args:
        state: The accounting book, which is authoritative over positions and
            cash and is never modified here.
        orders: The local orders that were submitted.
        remote_orders: What the venue says about its orders.
        remote_account: What the venue says about the account.
        mapping: The declared symbol mapping.
        local_filled: Filled quantity per local order id, from the journal.
        cash_tolerance: How large a cash difference is reported as `INFO` rather
            than `BLOCKING`. Defaults to exactly zero, and widening it is a
            decision someone has to make and record — a default tolerance is how
            a drift becomes invisible.

    Returns:
        The findings and the verdict. Never a repair, and never a modified book.
    """
    findings = _order_breaks(orders, remote_orders, local_filled)
    position_findings, positions_compared = _position_breaks(state, remote_account, mapping)
    findings.extend(position_findings)

    cash_compared = remote_account.cash is not None
    if not cash_compared:
        findings.append(
            ExternalBreak(
                code=Code.REMOTE_STATE_UNKNOWN,
                severity=ExternalBreakSeverity.WARNING,
                subject="CASH",
                accounting=str(quantise_cash(state.cash)),
                remote="the venue stated no cash figure",
                likely_causes=(
                    "an account read that did not include the cash field",
                    "a partial account payload",
                ),
            )
        )
    else:
        remote_cash = remote_account.require_cash()
        difference = quantise_cash(remote_cash - state.cash)
        if difference != 0:
            findings.append(
                ExternalBreak(
                    code=Code.REMOTE_CASH_MISMATCH,
                    severity=(
                        ExternalBreakSeverity.INFO
                        if abs(difference) <= cash_tolerance
                        else ExternalBreakSeverity.BLOCKING
                    ),
                    subject="CASH",
                    accounting=str(quantise_cash(state.cash)),
                    remote=str(quantise_cash(remote_cash)),
                    difference=str(difference),
                    likely_causes=(
                        "a fee the venue charged and the book has not been told about",
                        "a fill on one side the other has not recorded",
                        "an interest or dividend credit this engine refuses by name",
                        "two books that were never aligned at inception",
                    ),
                )
            )

    if remote_account.trading_blocked:
        findings.append(
            ExternalBreak(
                code=Code.EXTERNAL_SUBMISSION_BLOCKED,
                severity=ExternalBreakSeverity.BLOCKING,
                subject="ACCOUNT",
                remote=f"trading blocked, status {remote_account.status!r}",
                likely_causes=(
                    "an account restriction at the venue",
                    "a paper account that has been reset",
                ),
            )
        )

    ordered = tuple(
        sorted(findings, key=lambda row: (_SEVERITY_ORDER[row.severity], row.code, row.subject))
    )
    return ExternalReconciliationResult(
        breaks=ordered,
        orders_compared=len(orders),
        positions_compared=positions_compared,
        cash_compared=cash_compared,
        cash_tolerance=cash_tolerance,
    )
