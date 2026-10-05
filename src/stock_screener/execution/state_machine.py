"""The order lifecycle, as an explicit table rather than a set of conditions.

Order state is where execution engines accumulate quiet bugs. The states look
obvious, so the transitions get written as scattered `if` checks, and then one
path allows a cancelled order to fill, or a rejected order to be acknowledged, or
a fill to arrive before submission. Each of those is a trade the caller did not
authorise, and none of them is visible by reading any single function.

So the transitions are a frozen table, every move is checked against it, and the
terminal states are terminal by construction:

    CREATED          -> SUBMITTED
    SUBMITTED        -> ACKNOWLEDGED | REJECTED | CANCELLED
    ACKNOWLEDGED     -> PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED
    PARTIALLY_FILLED -> PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED
    FILLED           -> (terminal)
    CANCELLED        -> (terminal)
    REJECTED         -> (terminal)
    EXPIRED          -> (terminal)

`EXPIRED` arrived in V1.1. A `FILLED` order never expires — there is nothing left
to expire — and an `EXPIRED` order never fills again, which is the whole content
of "DAY".

The lifecycle is walked even when the broker is in-process and answers
instantly. Collapsing `CREATED -> FILLED` because a local simulation can do it in
one step would mean the journal no longer records the sequence a real broker will
force, and the first remote adapter would be exercising transitions nothing had
ever tested.

Nothing in this module touches a ledger. A state change is a statement about an
order, not about a portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import is_zero, quantise_price, quantise_quantity

from .errors import Code, ExecutionError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .models import Order

__all__ = [
    "TERMINAL_STATES",
    "V1_STATES",
    "VALID_TRANSITIONS",
    "OrderState",
    "OrderStatus",
    "transition_table",
]


class OrderState(StrEnum):
    """Where an order is in its life."""

    CREATED = "CREATED"
    """Authorised and recorded. Not yet sent anywhere."""

    SUBMITTED = "SUBMITTED"
    """Handed to a broker. Its fate is not yet known."""

    ACKNOWLEDGED = "ACKNOWLEDGED"
    """The broker has accepted it as working."""

    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    """Some quantity has traded; some remains."""

    FILLED = "FILLED"
    """The whole quantity has traded. Terminal."""

    CANCELLED = "CANCELLED"
    """Stopped before the whole quantity traded. Terminal."""

    REJECTED = "REJECTED"
    """Never worked. Terminal, and nothing traded."""

    EXPIRED = "EXPIRED"
    """The session it lived for has closed with quantity outstanding. Terminal.

    Kept distinct from `CANCELLED` deliberately. A cancellation is a decision
    somebody made; an expiry is the DAY lifetime ending, which happens to every
    unfilled DAY order whether anyone intervenes or not. Reporting them as one
    state would make an unattended rollover indistinguishable from an operator
    pulling an order, and only one of those needs looking into.

    Residual quantity reaches a later session as a *new* child DAY order, never
    by reviving this one — a DAY order that survived overnight would not be a DAY
    order.
    """


#: The only moves this engine permits. Anything absent is refused by name.
VALID_TRANSITIONS: Mapping[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.SUBMITTED}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.CANCELLED}
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}

#: States nothing may leave. A terminal order is settled history.
TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


#: The states and moves the V1 engine had, frozen with P2.0. V1 has no expiry
#: because nothing in it ends a session, so a V1 manifest that listed `EXPIRED`
#: would describe a transition that version can never take.
V1_STATES: tuple[OrderState, ...] = (
    OrderState.CREATED,
    OrderState.SUBMITTED,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
)


def transition_table(*, include_expiry: bool = True) -> dict[str, Any]:
    """Describe the lifecycle, for the manifest and for documentation.

    Args:
        include_expiry: Whether to describe V1.1's `EXPIRED` state. False gives
            the V1 table exactly as P2.0 froze it.

    Returns:
        The states, the permitted moves and the terminal set.
    """
    if not include_expiry:
        allowed = set(V1_STATES)
        return {
            "states": [str(state) for state in V1_STATES],
            "transitions": {
                str(source): sorted(str(target) for target in VALID_TRANSITIONS[source] & allowed)
                for source in V1_STATES
            },
            "terminal": sorted(str(state) for state in TERMINAL_STATES if state in allowed),
            "note": (
                "the lifecycle is walked even when the broker is in-process. "
                "Collapsing states because a local simulation could would leave the "
                "first remote adapter exercising transitions nothing had tested."
            ),
        }
    return {
        "states": [str(state) for state in OrderState],
        "transitions": {
            str(source): sorted(str(target) for target in targets)
            for source, targets in VALID_TRANSITIONS.items()
        },
        "terminal": sorted(str(state) for state in TERMINAL_STATES),
        "expiry": (
            "EXPIRED is distinct from CANCELLED: a cancellation is a decision, an "
            "expiry is the DAY lifetime ending. A FILLED order never expires, and "
            "an EXPIRED order never fills again."
        ),
        "note": (
            "the lifecycle is walked even when the broker is in-process. "
            "Collapsing states because a local simulation could would leave the "
            "first remote adapter exercising transitions nothing had tested."
        ),
    }


@dataclass(frozen=True)
class OrderStatus:
    """An order's state and how much of it has traded.

    Attributes:
        order: The order this describes.
        state: Where it is in its life.
        cumulative_filled_quantity: How much has traded, across every fill.
        cumulative_fee: Fees charged across every fill.
        cumulative_notional: `sum of quantity times price` across every fill, for VWAP.
        fill_count: How many fills have been admitted.
        reason: Why it was rejected or cancelled, where it was.
    """

    order: Order
    state: OrderState = OrderState.CREATED
    cumulative_filled_quantity: Decimal = Decimal(0)
    cumulative_fee: Decimal = Decimal(0)
    cumulative_notional: Decimal = Decimal(0)
    fill_count: int = 0
    reason: str | None = None

    def __post_init__(self) -> None:
        """Verify the fill arithmetic could describe this order.

        Raises:
            ExecutionError: `ORDER_OVERFILL` if more has traded than was ordered,
                or if a negative quantity has somehow been recorded.
        """
        filled = quantise_quantity(self.cumulative_filled_quantity)
        if filled < Decimal(0):
            raise ExecutionError(
                Code.ORDER_OVERFILL,
                f"{self.order.order_id}: cumulative filled quantity is {filled}; a "
                "negative fill is not a sale, it is a bug",
            )
        if filled > self.order.quantity:
            raise ExecutionError(
                Code.ORDER_OVERFILL,
                f"{self.order.order_id}: {filled} has filled against an order for "
                f"{self.order.quantity}",
            )
        object.__setattr__(self, "cumulative_filled_quantity", filled)

    @property
    def remaining_quantity(self) -> Decimal:
        """How much of the order has yet to trade."""
        return quantise_quantity(self.order.quantity - self.cumulative_filled_quantity)

    @property
    def is_terminal(self) -> bool:
        """Whether the order can no longer change."""
        return self.state in TERMINAL_STATES

    @property
    def is_fully_filled(self) -> bool:
        """Whether the whole quantity has traded."""
        return is_zero(self.remaining_quantity)

    @property
    def vwap(self) -> Decimal | None:
        """The volume-weighted average fill price, or None if nothing filled.

        Weighted, not a plain mean of fill prices: averaging a 1-share fill at 50
        with a 999-share fill at 10 as `30` would misreport the trade by an order
        of magnitude. None rather than zero when nothing filled, because an
        unfilled order has no price and zero would read as a free trade.
        """
        if is_zero(self.cumulative_filled_quantity):
            return None
        return quantise_price(self.cumulative_notional / self.cumulative_filled_quantity)

    def may_transition_to(self, target: OrderState) -> bool:
        """Whether the table permits a move.

        Args:
            target: The proposed state.

        Returns:
            True if the move is in the table.
        """
        return target in VALID_TRANSITIONS[self.state]

    def require_transition(self, target: OrderState) -> None:
        """Refuse a move the table does not permit.

        Args:
            target: The proposed state.

        Raises:
            ExecutionError: `ORDER_ALREADY_TERMINAL` when leaving a terminal
                state, which is distinguished because it means something is
                re-processing settled history rather than mis-sequencing a live
                order; `INVALID_ORDER_STATE_TRANSITION` otherwise.
        """
        if self.may_transition_to(target):
            return
        if self.is_terminal:
            raise ExecutionError(
                Code.ORDER_ALREADY_TERMINAL,
                f"{self.order.order_id} is {self.state} and cannot become "
                f"{target}. A terminal order is settled history; something is "
                "re-processing it rather than mis-sequencing a live order.",
            )
        raise ExecutionError(
            Code.INVALID_ORDER_STATE_TRANSITION,
            f"{self.order.order_id} cannot go from {self.state} to {target}. "
            f"Permitted from {self.state}: "
            f"{', '.join(sorted(str(s) for s in VALID_TRANSITIONS[self.state])) or 'nothing'}.",
        )

    def with_state(self, target: OrderState, *, reason: str | None = None) -> OrderStatus:
        """Move to a new state, if the table permits it.

        Args:
            target: The new state.
            reason: Why, for a rejection or cancellation.

        Returns:
            The status afterwards.

        Raises:
            ExecutionError: If the move is not permitted.
        """
        self.require_transition(target)
        return replace(self, state=target, reason=reason or self.reason)

    def with_fill(self, *, quantity: Decimal, price: Decimal, fee: Decimal) -> OrderStatus:
        """Record a fill and move to the state its quantity implies.

        The resulting state is derived rather than supplied: an order whose
        remaining quantity reaches zero is `FILLED`, and one with quantity left is
        `PARTIALLY_FILLED`. Letting a caller name the state would allow an order
        to be marked `FILLED` with quantity outstanding.

        Args:
            quantity: The quantity that traded.
            price: The price it traded at.
            fee: The fee charged.

        Returns:
            The status afterwards.

        Raises:
            ExecutionError: `ORDER_OVERFILL` if the fill exceeds what remains;
                `FILL_INVALID` on a non-positive quantity or price;
                `INVALID_ORDER_STATE_TRANSITION` if the order is not working.
        """
        filled = quantise_quantity(quantity)
        if filled <= Decimal(0):
            raise ExecutionError(
                Code.FILL_INVALID,
                f"{self.order.order_id}: a fill must move a positive quantity, got "
                f"{filled}. A zero fill is the absence of a fill and is recorded "
                "by leaving the order working.",
            )
        if quantise_price(price) <= Decimal(0):
            raise ExecutionError(
                Code.FILL_INVALID,
                f"{self.order.order_id}: a fill price must be positive, got {price}",
            )
        if filled > self.remaining_quantity:
            raise ExecutionError(
                Code.ORDER_OVERFILL,
                f"{self.order.order_id}: a fill of {filled} would take the "
                f"cumulative filled quantity to "
                f"{quantise_quantity(self.cumulative_filled_quantity + filled)} "
                f"against an order for {self.order.quantity}. The excess is "
                "refused outright rather than clipped to the remainder: a broker "
                "reporting more than was ordered is a reconciliation problem, not "
                "a larger trade.",
            )
        target = (
            OrderState.FILLED if filled == self.remaining_quantity else OrderState.PARTIALLY_FILLED
        )
        self.require_transition(target)
        return replace(
            self,
            state=target,
            cumulative_filled_quantity=quantise_quantity(self.cumulative_filled_quantity + filled),
            cumulative_fee=quantise_price(self.cumulative_fee + fee),
            cumulative_notional=quantise_price(
                self.cumulative_notional + filled * quantise_price(price)
            ),
            fill_count=self.fill_count + 1,
        )

    def as_json(self) -> dict[str, Any]:
        """Return the status as a JSON-safe mapping."""
        return {
            "order_id": self.order.order_id,
            "identity": self.order.identity.label,
            "side": str(self.order.side),
            "state": str(self.state),
            "is_terminal": self.is_terminal,
            "ordered_quantity": str(self.order.quantity),
            "cumulative_filled_quantity": str(self.cumulative_filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "fill_count": self.fill_count,
            "vwap_fill_price": str(self.vwap) if self.vwap is not None else None,
            "cumulative_fee": str(quantise_price(self.cumulative_fee)),
            "reason": self.reason,
        }
