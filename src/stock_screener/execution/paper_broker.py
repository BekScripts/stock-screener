"""A deterministic, in-process broker. No network, no credentials, no randomness.

This is the one component most likely to be mistaken for something it is not, so
the boundaries are worth stating directly.

**It is not a second set of books.** The broker tracks order state and nothing
else. It holds no cash balance, no positions and no P&L of its own, and it cannot
be asked for any: buying power *is* accounting's cash, and current holdings *are*
accounting's holdings. A simulated broker with its own balances is two books that
agree until they do not, and the disagreement arrives as a reconciliation break
whose cause is the simulator.

**It is not a realistic fill simulator.** A market order fills at the one price
the execution snapshot states. There is no spread, no slippage, no impact, no
participation limit, no latency and no queue. Determinism is the point — the same
inputs produce the same fills every time, which is what lets a test establish
that a change to the lifecycle is safe. Realism is a later phase, and the gap is
disclosed in the manifest rather than left for a reader to assume away.

**It cannot be made to place an unauthorised order.** Every method takes an
`ExecutionPlan` and checks the order against it. There is no `force`, no
`unsafe_submit` and no arbitrary-order entry point, because the absence of the
capability is the only version of that guarantee that survives a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import is_zero

from .accounting_adapter import allocate_partial_fill
from .errors import Code, ExecutionError
from .events import (
    ExecutionEventType,
    ExecutionFill,
    OrderAcknowledgedEvent,
    OrderCancelledEvent,
    OrderCreatedEvent,
    OrderFilledEvent,
    OrderRejectedEvent,
    OrderSubmittedEvent,
)
from .models import AttributionShare
from .state_machine import OrderState
from .versions import EXECUTION_ENGINE_VERSION, PAPER_FILL_MODEL

if TYPE_CHECKING:
    from decimal import Decimal

    from .authorization import ExecutionPlan
    from .journal import ExecutionJournal
    from .models import Order
    from .paper_market import PaperExecutionSnapshot

__all__ = ["VENUE", "DeterministicPaperBroker"]

#: What this broker calls itself in the journal. Named so a later remote adapter
#: produces distinguishable records and a reader can tell simulated fills from
#: real ones without inspecting anything else.
VENUE = "DETERMINISTIC_PAPER_BROKER"


@dataclass(frozen=True)
class _EventIds:
    """Deterministic event ids for one order's lifecycle.

    Derived from the order id rather than generated, so a retried submission
    produces the same event id and the journal's idempotency absorbs it. Random
    ids would make every retry a new event and the lifecycle unreplayable.
    """

    order_id: str

    @property
    def created(self) -> str:
        """The creation event's id."""
        return f"{self.order_id}:CREATED"

    @property
    def submitted(self) -> str:
        """The submission event's id."""
        return f"{self.order_id}:SUBMITTED"

    @property
    def acknowledged(self) -> str:
        """The acknowledgement event's id."""
        return f"{self.order_id}:ACKNOWLEDGED"

    def fill(self, index: int) -> str:
        """The id of the nth fill event.

        Args:
            index: 1-based fill number for this order.

        Returns:
            The event id.
        """
        return f"{self.order_id}:FILL:{index}"

    def fill_id(self, index: int) -> str:
        """The id of the nth fill itself.

        Args:
            index: 1-based fill number for this order.

        Returns:
            The fill id, which maps deterministically to an accounting event id.
        """
        return f"{self.order_id}#{index}"

    @property
    def cancelled(self) -> str:
        """The cancellation event's id."""
        return f"{self.order_id}:CANCELLED"

    @property
    def rejected(self) -> str:
        """The rejection event's id."""
        return f"{self.order_id}:REJECTED"


class DeterministicPaperBroker:
    """An in-process broker that *produces* lifecycle events.

    It reads order state from the journal and returns the events its decisions
    imply. It deliberately does not commit them: durability, the hash chain, the
    anchor and the pre-write validation all belong to the store, and a broker
    that also appended would mean every event was written twice — once
    unvalidated and unanchored, and once as what looked like a retry.

    It stores nothing of its own, which is what keeps accounting the sole
    authority over the book.
    """

    def __init__(self, *, journal: ExecutionJournal) -> None:
        """Open a broker over a journal.

        Args:
            journal: The journal to read order state from. The broker owns no
                state; every decision is derived from these records.
        """
        self._journal = journal

    @property
    def venue(self) -> str:
        """What this broker calls itself."""
        return VENUE

    def _authorize(self, plan: ExecutionPlan, order: Order) -> None:
        """Refuse an order the active plan does not authorise.

        Args:
            plan: The plan being executed.
            order: The order offered.

        Raises:
            ExecutionError: Whatever the plan refuses.
        """
        plan.require_authorizes(order)

    def create(self, plan: ExecutionPlan, order: Order, *, at: str) -> OrderCreatedEvent:
        """Record an authorised order. Nothing is sent and nothing is held.

        Args:
            plan: The authorising plan.
            order: The order.
            at: When, ISO 8601 with offset.

        Returns:
            The event, for the caller to commit.

        Raises:
            ExecutionError: If the plan does not authorise the order.
        """
        self._authorize(plan, order)
        event = OrderCreatedEvent(
            event_id=_EventIds(order.order_id).created,
            event_type=ExecutionEventType.ORDER_CREATED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            order=order,
        )
        return event

    def submit(self, plan: ExecutionPlan, order: Order, *, at: str) -> OrderSubmittedEvent:
        """Hand the order to this broker.

        Recorded as its own event even though the broker is in-process and will
        answer immediately. Collapsing it would mean the journal never records
        the sequence a real venue forces, and the first remote adapter would be
        exercising transitions nothing had tested.

        Args:
            plan: The authorising plan.
            order: The order.
            at: When.

        Returns:
            The event, for the caller to commit.

        Raises:
            ExecutionError: If unauthorised, or if the order is not `CREATED`.
        """
        self._authorize(plan, order)
        event = OrderSubmittedEvent(
            event_id=_EventIds(order.order_id).submitted,
            event_type=ExecutionEventType.ORDER_SUBMITTED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            venue=VENUE,
        )
        return event

    def acknowledge(self, plan: ExecutionPlan, order: Order, *, at: str) -> OrderAcknowledgedEvent:
        """Accept the order as working.

        Args:
            plan: The authorising plan.
            order: The order.
            at: When.

        Returns:
            The event, for the caller to commit.

        Raises:
            ExecutionError: If unauthorised or not `SUBMITTED`.
        """
        self._authorize(plan, order)
        event = OrderAcknowledgedEvent(
            event_id=_EventIds(order.order_id).acknowledged,
            event_type=ExecutionEventType.ORDER_ACKNOWLEDGED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            venue=VENUE,
        )
        return event

    def reject(
        self, plan: ExecutionPlan, order: Order, *, at: str, reason: str
    ) -> OrderRejectedEvent:
        """Refuse the order. Nothing trades and the book is untouched.

        Args:
            plan: The authorising plan.
            order: The order.
            at: When.
            reason: Why. A deterministic input, not a simulated condition — this
                broker invents no venue-specific reject codes.

        Returns:
            The event, for the caller to commit.

        Raises:
            ExecutionError: If unauthorised or the transition is not permitted.
        """
        self._authorize(plan, order)
        event = OrderRejectedEvent(
            event_id=_EventIds(order.order_id).rejected,
            event_type=ExecutionEventType.ORDER_REJECTED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            reason=reason,
        )
        return event

    def cancel(
        self, plan: ExecutionPlan, order: Order, *, at: str, reason: str = "CANCELLED"
    ) -> OrderCancelledEvent:
        """Stop the order. Prior fills stand; the remainder never fills.

        Cancelling a partially filled order keeps the filled quantity in the
        book. Cancellation applies to what has not traded, and un-trading what
        has is not something a cancel can do.

        Args:
            plan: The authorising plan.
            order: The order.
            at: When.
            reason: Why.

        Returns:
            The event, for the caller to commit.

        Raises:
            ExecutionError: If unauthorised, or if the order is already terminal
                — a filled order cannot be cancelled.
        """
        self._authorize(plan, order)
        event = OrderCancelledEvent(
            event_id=_EventIds(order.order_id).cancelled,
            event_type=ExecutionEventType.ORDER_CANCELLED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            reason=reason,
        )
        return event

    def fill(
        self,
        plan: ExecutionPlan,
        order: Order,
        *,
        snapshot: PaperExecutionSnapshot,
        acknowledged_at: str,
    ) -> OrderFilledEvent | None:
        """Fill as much of the order as the snapshot allows.

        Args:
            plan: The authorising plan.
            order: The order.
            snapshot: The explicit prices and availability to fill against.
            acknowledged_at: When the order became working, so a snapshot from
                before that can be refused.

        Returns:
            The fill event appended, or None when the snapshot states zero
            availability — the order stays working, because a venue with nothing
            on the other side has not refused anything.

        Raises:
            ExecutionError: `PAPER_EXECUTION_PRICE_MISSING` if the snapshot does
                not price the security — the sizing price is never substituted;
                `PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT` if it predates the
                order; whatever the state machine refuses.
        """
        self._authorize(plan, order)
        snapshot.require_not_before(acknowledged_at, order_id=order.order_id)
        status = self._journal.derive().status(order.order_id)
        if status.is_terminal:
            raise ExecutionError(
                Code.ORDER_ALREADY_TERMINAL,
                f"{order.order_id} is {status.state} and cannot fill further",
            )
        if status.state is not OrderState.ACKNOWLEDGED and (
            status.state is not OrderState.PARTIALLY_FILLED
        ):
            raise ExecutionError(
                Code.INVALID_ORDER_STATE_TRANSITION,
                f"{order.order_id} is {status.state} and is not working; only an "
                "acknowledged or partially filled order can fill",
            )

        price = snapshot.price_for(order.identity)
        if price is None:
            raise ExecutionError(
                Code.PAPER_EXECUTION_PRICE_MISSING,
                f"{order.identity.label} has no execution price at "
                f"{snapshot.as_of}, so {order.order_id} cannot fill. The sizing "
                f"price ({order.sizing_price}) is deliberately not substituted: "
                "filling at the price that sized the order would make every "
                "rebalance execute at exactly its planned level and the "
                "simulation could never show a cost.",
            )

        quantity = price.fillable(status.remaining_quantity)
        if is_zero(quantity):
            # Explicitly no liquidity. The order remains working rather than
            # being rejected — nothing refused it, there was simply nothing to
            # trade against.
            return None

        index = status.fill_count + 1
        ids = _EventIds(order.order_id)
        fill = ExecutionFill(
            fill_id=ids.fill_id(index),
            order_id=order.order_id,
            identity=order.identity,
            side=order.side,
            quantity=quantity,
            price=price.price,
            fee=price.fee,
            effective_at=snapshot.as_of,
            attribution=_fill_attribution(order, quantity),
            source_fingerprint=snapshot.fingerprint(),
            venue=VENUE,
        )
        event = OrderFilledEvent(
            event_id=ids.fill(index),
            event_type=ExecutionEventType.ORDER_FILLED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=snapshot.as_of,
            fill=fill,
        )
        return event

    def describe(self) -> dict[str, Any]:
        """Describe what this broker is and is not.

        Returns:
            The description, for the manifest. The absent capabilities are listed
            because a reader who assumes they are present will misread every
            result the engine produces.
        """
        return {
            "venue": VENUE,
            "engine_version": EXECUTION_ENGINE_VERSION,
            "fill_model": PAPER_FILL_MODEL,
            "in_process": True,
            "network": "NONE",
            "broker_sdk": "NONE",
            "credentials": "NONE",
            "randomness": "NONE",
            "holds_cash": False,
            "holds_positions": False,
            "holds_pnl": False,
            "buying_power_authority": (
                "accounting cash. The broker has no balance of its own, so there "
                "is no second number that could disagree with the book."
            ),
            "position_authority": "accounting holdings",
            "unauthorised_order_path": (
                "NONE — every method takes the plan and checks the order against "
                "it. There is no force, no unsafe_submit and no arbitrary-order "
                "entry point."
            ),
        }


def _fill_attribution(order: Order, quantity: Decimal) -> tuple[AttributionShare, ...]:
    """Divide a fill across the strategies that authorised the order.

    A full fill inherits the order's own shares exactly. A partial fill is
    divided proportionally by what each strategy authorised, which is the only
    division derivable from what the plan already states.

    Args:
        order: The order.
        quantity: What filled.

    Returns:
        The shares, summing exactly to the filled quantity. Empty when the order
        carries no attribution, which leaves accounting to apply its own rules —
        including refusing an ambiguous sale.
    """
    if not order.attribution:
        return ()
    if quantity == order.quantity:
        return order.attribution
    slices = allocate_partial_fill(
        authorized=tuple((row.strategy_id, row.quantity) for row in order.attribution),
        filled_quantity=quantity,
    )
    return tuple(
        AttributionShare(strategy_id=row.strategy_id, quantity=row.quantity) for row in slices
    )
