"""The one seam where execution evidence becomes a book entry.

Two facts are deliberately distinct in this system: *a fill happened*, which the
journal records, and *a fill is in the book*, which the accounting ledger records.
Keeping them apart is what makes the handoff recoverable — a crash between them
leaves a fill that is durable evidence and `PENDING` application, which recovery
can finish exactly once. Merging them would make that window ambiguous, and the
two possible wrong answers are a lost trade and a double trade.

Exactly-once rests on one decision: the accounting event id is **derived** from
the fill id (`EXEC_FILL:<fill_id>`) rather than generated. Re-presenting the same
fill therefore produces the same event id, and the accounting layer's own
idempotency does the rest — it returns `written=False` and does not move the
ledger. A generated id would make every retry a second trade, and retries are the
normal consequence of a crash.

The adapter translates and nothing more. It does not decide quantities, choose
prices, or resolve attribution it was not given; accounting remains the final
enforcement, including `INSUFFICIENT_CASH` on a buy the book cannot fund and
`INSUFFICIENT_ATTRIBUTED_POSITION` on a sale from a pool that does not hold it.
Execution does not get to argue with either.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import (
    ACCOUNTING_V2,
    AccountingError,
    EventType,
    FillEvent,
    PortfolioLedger,
    PortfolioStore,
    Side,
    StrategySlice,
    quantise_quantity,
    replay,
    state_fingerprint,
)

from .authorization import require_supported_accounting_version
from .errors import Code, ExecutionError
from .models import OrderSide

if TYPE_CHECKING:
    from .events import ExecutionFill

__all__ = [
    "AdmissionResult",
    "admit_fill",
    "allocate_partial_fill",
    "to_accounting_fill_event",
]


def to_accounting_fill_event(
    fill: ExecutionFill, *, source_id: str = "EXECUTION_ENGINE_V1"
) -> FillEvent:
    """Translate execution evidence into an accounting fill event.

    Args:
        fill: The execution fill.
        source_id: What produced it, for the ledger's provenance.

    Returns:
        The accounting `FillEvent`, carrying the same identity, side, quantity,
        price, fee, effective time and attribution, plus the order and fill
        references so a ledger entry can be traced back to the trade.

    Raises:
        ExecutionError: `FILL_INVALID` if the fill carries no fingerprint to
            record as provenance. An accounting entry that cannot name the
            evidence behind it is exactly what the ledger's
            `source_fingerprint` requirement exists to prevent.
    """
    fingerprint = fill.fingerprint()
    if not fingerprint:  # pragma: no cover - fingerprint is always computed
        raise ExecutionError(
            Code.FILL_INVALID, f"{fill.fill_id} has no fingerprint to record as provenance"
        )
    return FillEvent(
        event_id=fill.accounting_event_id,
        event_type=EventType.FILL,
        effective_at=fill.effective_at,
        source_id=source_id,
        source_fingerprint=fingerprint,
        identity=fill.identity,
        side=Side.BUY if fill.side is OrderSide.BUY else Side.SELL,
        quantity=fill.quantity,
        price=fill.price,
        fees=fill.fee,
        attribution=tuple(
            StrategySlice(strategy_id=row.strategy_id, quantity=row.quantity)
            for row in sorted(fill.attribution, key=lambda row: row.strategy_id)
        ),
        execution_reference=f"{fill.order_id}/{fill.fill_id}",
    )


class AdmissionResult(dict[str, Any]):
    """What admitting one fill to the book did.

    `written` is False when the fill was already in the ledger — the ordinary
    answer after a retry, and the one that makes the handoff exactly-once rather
    than merely usually-once.
    """


def admit_fill(
    fill: ExecutionFill,
    *,
    ledger: PortfolioLedger,
    store: PortfolioStore,
) -> AdmissionResult:
    """Admit execution evidence to the book, exactly once.

    Args:
        fill: The execution fill.
        ledger: The in-memory accounting ledger.
        store: The durable accounting store, which writes the record and its
            head anchor before considering the event committed.

    Returns:
        The result: whether anything was written, the accounting event id and
        sequence, and the book's state fingerprint afterwards.

    Raises:
        ExecutionError: `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION` if the store
            is not a V2 book; `FILL_ID_CONFLICT` if the id is already in the
            ledger with different content; `ACCOUNTING_ADMISSION_FAILED`
            wrapping whatever accounting refused — a buy the cash cannot fund, a
            sale beyond an owner's pool, an attribution that does not reconcile.
            Each is surfaced rather than worked around: execution has no standing
            to overrule the book.
    """
    require_supported_accounting_version(
        store.accounting_version, context="the execution accounting store"
    )
    event = to_accounting_fill_event(fill)
    try:
        result = store.commit(ledger, event)
    except AccountingError as exc:
        if exc.code == "EVENT_ID_CONFLICT":
            raise ExecutionError(
                Code.FILL_ID_CONFLICT,
                f"fill {fill.fill_id!r} maps to accounting event "
                f"{event.event_id!r}, which the ledger already holds with "
                f"different content. {exc.detail}",
            ) from exc
        raise ExecutionError(
            Code.ACCOUNTING_ADMISSION_FAILED,
            f"the book refused fill {fill.fill_id!r} [{exc.code}]: {exc.detail} "
            "Execution does not override the book: there is no resize, no "
            "overdraft and no partial admission.",
        ) from exc
    state = replay(ledger, accounting_version=ACCOUNTING_V2)
    return AdmissionResult(
        fill_id=fill.fill_id,
        accounting_event_id=event.event_id,
        accounting_sequence=result.record.sequence,
        written=result.written,
        accounting_state_fingerprint=state_fingerprint(state),
        ledger_head=result.record.record_hash,
        cash_after=str(state.cash),
    )


def allocate_partial_fill(
    *,
    authorized: tuple[tuple[str, Decimal], ...],
    filled_quantity: Decimal,
) -> tuple[StrategySlice, ...]:
    """Divide a partial fill across the strategies that authorised the order.

    `PROPORTIONAL_FILL_ATTRIBUTION_V1`: each owner receives the filled quantity
    in proportion to what it authorised, in strategy order, with the last share
    taking the exact remainder so the parts sum to the whole. Proportional
    because it is the only division derivable from what the plan already states —
    any other rule would need a preference nobody expressed, and filling one
    strategy's share first would hand it the whole of a small fill.

    Args:
        authorized: `[(strategy_id, authorised quantity)]` from the order.
        filled_quantity: What actually traded.

    Returns:
        The slices, summing exactly to `filled_quantity`.

    Raises:
        ExecutionError: `FILL_ATTRIBUTION_DOES_NOT_SUM` if the authorised
            quantities carry nothing to apportion by.
    """
    ordered = sorted(authorized, key=lambda row: row[0])
    total = quantise_quantity(sum((value for _, value in ordered), Decimal(0)))
    if total <= Decimal(0):
        raise ExecutionError(
            Code.FILL_ATTRIBUTION_DOES_NOT_SUM,
            "the order's authorised attribution carries no quantity to apportion "
            "a partial fill across",
        )
    filled = quantise_quantity(filled_quantity)
    slices: list[StrategySlice] = []
    running = Decimal(0)
    for index, (name, authorised) in enumerate(ordered):
        if index == len(ordered) - 1:
            part = quantise_quantity(filled - running)
        else:
            part = quantise_quantity(filled * authorised / total)
            running = quantise_quantity(running + part)
        if part > Decimal(0):
            slices.append(StrategySlice(strategy_id=name, quantity=part))
    return tuple(slices)
