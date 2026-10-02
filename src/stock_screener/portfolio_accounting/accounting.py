"""The event-to-state machine: the only code that may change a holding.

Every function here takes a state and an event and returns a new state. There is
no in-place mutation, no partial application and no path that changes cash
without also recording why — which is what makes the cash identity in
`PortfolioState.check_cash_identity` enforceable rather than aspirational.

Three refusals are the substance of this module, and each exists because the
permissive alternative fails silently:

- a buy without the cash is `INSUFFICIENT_CASH`, not an overdraft. Allowing it
  would invent leverage the baseline does not have and the risk layer never
  sized for;
- a sell beyond the holding is `INSUFFICIENT_POSITION`, not a short. The
  shortfall is far more likely a fill this ledger never received than a
  deliberate short;
- a corporate action the engine does not model is `UNSUPPORTED_CORPORATE_ACTION`,
  not a best guess. A merger's treatment depends on terms the event cannot carry,
  and a wrong basis is indistinguishable from a right one downstream.

Replay is deterministic: the same ledger always produces the same state, byte for
byte, because nothing here reads a clock, a database or a network.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .events import (
    UNSUPPORTED_ACTIONS,
    CashDividendEvent,
    CashFlowDirection,
    CorporateActionEvent,
    EventType,
    ExternalCashFlowEvent,
    FillEvent,
    OpeningBalanceEvent,
    PortfolioEvent,
    Side,
    SplitEvent,
)
from .money import quantise_cash
from .positions import PositionState, SaleAllocation
from .state import PortfolioState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ledger import LedgerRecord, PortfolioLedger

__all__ = [
    "ACCOUNTING_ENGINE_VERSION",
    "AppliedEvent",
    "apply_event",
    "replay",
]

#: The accounting contract. A change to how an event moves state is a new
#: version, because a replay under different rules is not the same portfolio.
ACCOUNTING_ENGINE_VERSION = "PORTFOLIO_ACCOUNTING_V1"


class AppliedEvent:
    """What applying one event did.

    Attributes:
        state: The state afterwards.
        sequence: The ledger record applied.
        effect: A description of the change, for the audit trail.
        allocation: The realised result, for a sale.
    """

    __slots__ = ("allocation", "effect", "sequence", "state")

    def __init__(
        self,
        *,
        state: PortfolioState,
        sequence: int,
        effect: dict[str, Any],
        allocation: SaleAllocation | None = None,
    ) -> None:
        """Record the application.

        Args:
            state: The state afterwards.
            sequence: The ledger record applied.
            effect: What changed.
            allocation: The realised result, for a sale.
        """
        self.state = state
        self.sequence = sequence
        self.effect = effect
        self.allocation = allocation

    def as_json(self) -> dict[str, Any]:
        """Return the application as a JSON-safe mapping."""
        return {
            "sequence": self.sequence,
            "effect": dict(self.effect),
            "allocation": self.allocation.as_json() if self.allocation else None,
        }


def _apply_opening_balance(
    state: PortfolioState, event: OpeningBalanceEvent
) -> tuple[PortfolioState, dict[str, Any]]:
    """Establish the ledger's starting point.

    Args:
        state: The empty state.
        event: The opening balance.

    Returns:
        The established state and what it recorded.

    Raises:
        AccountingError: If a position is imported twice. Two opening rows for
            one security would make its cost basis depend on which was applied
            last.
    """
    after = replace(
        state,
        cash=event.cash,
        cash_summary=replace(state.cash_summary, opening=event.cash),
    )
    seen: set[tuple[str, int]] = set()
    for opening in event.positions:
        key = opening.identity.key
        if key in seen:
            raise AccountingError(
                Code.EVENT_INVALID,
                f"{opening.identity.label} is imported twice in one opening balance; "
                "the resulting cost basis would depend on ordering",
            )
        seen.add(key)
        after = replace(
            after,
            opening_cost_basis=quantise_cash(
                after.opening_cost_basis + opening.quantity * opening.cost_basis_per_share
            ),
        )
        position = PositionState(
            identity=opening.identity,
            quantity=opening.quantity,
            total_cost=quantise_cash(opening.quantity * opening.cost_basis_per_share),
            attribution={row.strategy_id: row.quantity for row in opening.attribution},
        )
        position.check_attribution()
        after = after.with_position(position)
    return after, {
        "event_type": str(EventType.OPENING_BALANCE),
        "cash": str(event.cash),
        "positions_imported": len(event.positions),
        "cost_basis_imported": str(after.total_cost_basis),
        "note": (
            "an imported position's cost basis is adopted as given and never "
            "inferred from a current price; inferring it would set unrealised "
            "P&L to zero and silently erase whatever gain or loss already existed"
        ),
    }


def _apply_fill(
    state: PortfolioState, event: FillEvent
) -> tuple[PortfolioState, dict[str, Any], SaleAllocation | None]:
    """Apply a fill.

    Args:
        state: The state before.
        event: The fill.

    Returns:
        The state after, what it recorded, and the realised result for a sale.

    Raises:
        AccountingError: `INSUFFICIENT_CASH` on a buy the balance cannot fund;
            `SELL_ATTRIBUTION_REQUIRED` or `INSUFFICIENT_POSITION` on a sale the
            holding cannot support.
    """
    assert event.identity is not None  # noqa: S101 - established by the event
    position = state.position(event.identity)
    if event.side is Side.BUY:
        required = quantise_cash(event.gross_notional + event.fees)
        available = quantise_cash(state.cash)
        if required > available:
            raise AccountingError(
                Code.INSUFFICIENT_CASH,
                f"{event.identity.label}: the fill needs {required} "
                f"({event.gross_notional} plus {event.fees} in fees) but the ledger "
                f"holds {available}. Recording it would create an overdraft, which is "
                "leverage the long-only baseline does not have and the risk layer "
                "never sized for.",
            )
        after_position = position.buy(
            quantity=event.quantity,
            price=event.price,
            fees=event.fees,
            attribution=event.attribution,
        )
        summary = replace(
            state.cash_summary,
            purchases=quantise_cash(state.cash_summary.purchases + event.gross_notional),
            fees=quantise_cash(state.cash_summary.fees + event.fees),
        )
        after = replace(
            state,
            cash=quantise_cash(state.cash - required),
            cash_summary=summary,
            fees_paid=quantise_cash(state.fees_paid + event.fees),
        ).with_position(after_position)
        return (
            after,
            {
                "event_type": str(EventType.FILL),
                "side": str(Side.BUY),
                "identity": event.identity.label,
                "quantity": str(event.quantity),
                "price": str(event.price),
                "cash_out": str(required),
                "average_cost_after": str(after_position.average_cost),
                "note": "fees are expensed, not added to cost basis",
            },
            None,
        )

    after_position, allocation = position.sell(
        quantity=event.quantity,
        price=event.price,
        fees=event.fees,
        attribution=event.attribution,
    )
    net_cash = quantise_cash(allocation.proceeds - event.fees)
    summary = replace(
        state.cash_summary,
        sales=quantise_cash(state.cash_summary.sales + allocation.proceeds),
        fees=quantise_cash(state.cash_summary.fees + event.fees),
    )
    after = replace(
        state,
        cash=quantise_cash(state.cash + net_cash),
        cash_summary=summary,
        realised_pnl=quantise_cash(state.realised_pnl + allocation.realised_pnl),
        fees_paid=quantise_cash(state.fees_paid + event.fees),
    ).with_position(after_position)
    return (
        after,
        {
            "event_type": str(EventType.FILL),
            "side": str(Side.SELL),
            "identity": event.identity.label,
            "quantity": str(event.quantity),
            "price": str(event.price),
            "cash_in": str(net_cash),
            "realised_pnl": str(allocation.realised_pnl),
            "cost_basis_released": str(allocation.cost_basis_released),
        },
        allocation,
    )


def _apply_external_cash_flow(
    state: PortfolioState, event: ExternalCashFlowEvent
) -> tuple[PortfolioState, dict[str, Any]]:
    """Apply a deposit or withdrawal.

    Args:
        state: The state before.
        event: The flow.

    Returns:
        The state after and what it recorded.

    Raises:
        AccountingError: `INSUFFICIENT_CASH` if a withdrawal exceeds the balance.
    """
    if event.direction is CashFlowDirection.WITHDRAWAL and event.amount > quantise_cash(state.cash):
        raise AccountingError(
            Code.INSUFFICIENT_CASH,
            f"the withdrawal is for {event.amount} but the ledger holds "
            f"{quantise_cash(state.cash)}; a withdrawal cannot be funded by selling "
            "a position the ledger has not been told about",
        )
    if event.direction is CashFlowDirection.DEPOSIT:
        summary = replace(
            state.cash_summary,
            deposits=quantise_cash(state.cash_summary.deposits + event.amount),
        )
    else:
        summary = replace(
            state.cash_summary,
            withdrawals=quantise_cash(state.cash_summary.withdrawals + event.amount),
        )
    after = replace(
        state,
        cash=quantise_cash(state.cash + event.signed_amount),
        cash_summary=summary,
    )
    return after, {
        "event_type": str(EventType.EXTERNAL_CASH_FLOW),
        "direction": str(event.direction),
        "amount": str(event.amount),
        "pnl_effect": "NONE",
        "note": (
            "an external flow changes NAV and never P&L; counting a deposit as "
            "performance would make a portfolio look skilful for receiving money"
        ),
    }


def _apply_cash_dividend(
    state: PortfolioState, event: CashDividendEvent
) -> tuple[PortfolioState, dict[str, Any]]:
    """Apply a cash dividend.

    Args:
        state: The state before.
        event: The dividend.

    Returns:
        The state after and what it recorded.
    """
    assert event.identity is not None  # noqa: S101 - established by the event
    position = state.position(event.identity).credit_dividend(event.cash_amount)
    summary = replace(
        state.cash_summary,
        dividends=quantise_cash(state.cash_summary.dividends + event.cash_amount),
    )
    after = replace(
        state,
        cash=quantise_cash(state.cash + event.cash_amount),
        cash_summary=summary,
        dividend_income=quantise_cash(state.dividend_income + event.cash_amount),
    )
    # A dividend on a position already closed is recorded without reopening it:
    # the entitlement was earned while it was held, and resurrecting a holding of
    # zero shares to carry the income would put a phantom position in every
    # exposure calculation.
    after = after.with_position(position)
    return after, {
        "event_type": str(EventType.CASH_DIVIDEND),
        "identity": event.identity.label,
        "cash_amount": str(event.cash_amount),
        "classification": "INCOME",
        "cost_basis_effect": "NONE",
        "note": (
            "entitlement is the producer's responsibility; inferring it from "
            "current holdings would miss a position sold between the ex-date and "
            "the pay date and would credit one bought after it"
        ),
    }


def _apply_split(state: PortfolioState, event: SplitEvent) -> tuple[PortfolioState, dict[str, Any]]:
    """Apply a share split or reverse split.

    Args:
        state: The state before.
        event: The split.

    Returns:
        The state after and what it recorded.
    """
    assert event.identity is not None  # noqa: S101 - established by the event
    before = state.position(event.identity)
    after_position = before.apply_split(numerator=event.numerator, denominator=event.denominator)
    after = state.with_position(after_position) if before.is_open else state
    return after, {
        "event_type": str(EventType.SPLIT),
        "identity": event.identity.label,
        "ratio": f"{event.numerator}/{event.denominator}",
        "quantity_before": str(before.quantity),
        "quantity_after": str(after_position.quantity),
        "total_cost_before": str(before.total_cost),
        "total_cost_after": str(after_position.total_cost),
        "pnl_effect": "NONE",
        "note": (
            "quantity scales and total cost does not, so average cost moves by "
            "exactly the ratio and no gain or loss is created. A fractional "
            "residue from a reverse split is recorded rather than rounded away: "
            "cash in lieu is a separate economic event this engine does not invent."
        ),
    }


def _apply_corporate_action(
    state: PortfolioState, event: CorporateActionEvent
) -> tuple[PortfolioState, dict[str, Any]]:
    """Refuse a corporate action whose economics are not modelled.

    Args:
        state: The state before.
        event: The action.

    Returns:
        Never returns normally for an unsupported action.

    Raises:
        AccountingError: `UNSUPPORTED_CORPORATE_ACTION`, always. This branch
            exists so an action can be refused *by name*: a merger has a real
            treatment depending on terms the event cannot carry, and both
            guessing it and ignoring it misstate the portfolio.
    """
    assert event.identity is not None  # noqa: S101 - established by the event
    named = event.action.upper() in UNSUPPORTED_ACTIONS
    raise AccountingError(
        Code.UNSUPPORTED_CORPORATE_ACTION,
        f"{event.identity.label}: {event.action} is not modelled"
        + (" (a known-unsupported kind)" if named else " (an unrecognised kind)")
        + ". Its economics depend on terms this event cannot carry, so adopting a "
        "guess would misstate cost basis or invent a position, and ignoring it "
        "would leave the ledger describing a security that no longer exists as "
        "recorded. Record the resulting fills, cash and quantity changes as "
        "explicit events instead.",
    )


def apply_event(state: PortfolioState, event: PortfolioEvent, *, sequence: int) -> AppliedEvent:
    """Apply one event to a state.

    Args:
        state: The state before.
        event: The event to apply.
        sequence: The ledger record this event occupies.

    Returns:
        The state afterwards, with a description of the change.

    Raises:
        AccountingError: If the event cannot be applied, or if applying it would
            break an invariant. Nothing partial is returned — a refused event
            leaves the caller's state untouched.
    """
    allocation: SaleAllocation | None = None
    if isinstance(event, OpeningBalanceEvent):
        after, effect = _apply_opening_balance(state, event)
    elif isinstance(event, FillEvent):
        after, effect, allocation = _apply_fill(state, event)
    elif isinstance(event, ExternalCashFlowEvent):
        after, effect = _apply_external_cash_flow(state, event)
    elif isinstance(event, CashDividendEvent):
        after, effect = _apply_cash_dividend(state, event)
    elif isinstance(event, SplitEvent):
        after, effect = _apply_split(state, event)
    elif isinstance(event, CorporateActionEvent):
        after, effect = _apply_corporate_action(state, event)
    else:
        raise AccountingError(
            Code.EVENT_INVALID,
            f"{type(event).__name__} is not an event this engine applies; an event "
            "type the state machine does not know cannot be given a meaning here",
        )
    after = replace(
        after,
        ledger_sequence=sequence,
        last_effective_at=event.effective_at,
    )
    after.check_invariants()
    return AppliedEvent(state=after, sequence=sequence, effect=effect, allocation=allocation)


def replay(
    ledger: PortfolioLedger | Sequence[LedgerRecord],
    *,
    through_sequence: int | None = None,
) -> PortfolioState:
    """Derive state by replaying a ledger.

    This is the only way a `PortfolioState` is produced. A state built any other
    way — from a broker statement, from a cached summary, from a patch — could
    not be traced to the events that justify it.

    Args:
        ledger: The ledger, or its records.
        through_sequence: Replay only up to this record, for an as-of state.

    Returns:
        The state the records imply.

    Raises:
        AccountingError: `LEDGER_HASH_CHAIN_MISMATCH` or `LEDGER_SEQUENCE_GAP` if
            the records do not verify, or whatever an event's application raises.
    """
    from .ledger import verify_chain

    records = tuple(ledger)
    result = verify_chain(records)
    if not result["verified"]:
        failure = result["failure"]
        raise AccountingError(
            str(failure["code"]),
            f"the ledger cannot be replayed: {failure['detail']}",
        )
    state = PortfolioState()
    head = records[0].previous_hash if records else state.ledger_head_hash
    for record in records:
        if through_sequence is not None and record.sequence > through_sequence:
            break
        applied = apply_event(state, record.event, sequence=record.sequence)
        state = applied.state
        head = record.record_hash
    return replace(state, ledger_head_hash=head)
