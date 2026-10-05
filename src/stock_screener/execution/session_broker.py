"""The V1.1 paper broker: spread, slippage, participation and real DAY expiry.

This sits beside the V1 broker rather than replacing it. V1's reference-price
model is frozen with its goldens, and a caller selects V1.1 by supplying a
`PaperExecutionPolicy`, an `ExecutionCalendar` and `PaperMarketSessionSnapshot`s
instead of the flat `ExecutionPrice` snapshots V1 takes. Nothing reinterprets a
V1 run.

A fill under V1.1 is the intersection of three independent limits:

    remaining order quantity        what is still authorised
    remaining session capacity      the shared participation ceiling
    explicit availability           where the snapshot states one

and the quantity is the minimum of whichever apply. All three are computed from
explicit inputs; none is drawn from anywhere.

The price is the touch — ask for a buy, bid for a sell — worsened by the declared
adverse slippage. A buyer never pays less than the ask and a seller never
receives more than the bid, because this engine is not permitted to improve a
fill.

**DAY now means DAY.** When a session closes with quantity outstanding, the order
becomes `EXPIRED`, terminally. Residual quantity reaches the next session as a
*new child order* with a deterministic id derived from the plan, the security,
the session and the generation — never by reviving the expired one, which would
make it something other than a DAY order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import is_zero, quantise_quantity

from .accounting_adapter import allocate_partial_fill
from .errors import Code, ExecutionError
from .events import (
    ExecutionEventType,
    ExecutionFill,
    OrderAcknowledgedEvent,
    OrderCreatedEvent,
    OrderExpiredEvent,
    OrderFilledEvent,
    OrderSubmittedEvent,
)
from .fingerprint import fingerprint_of
from .models import AttributionShare, Order, OrderType, TimeInForce
from .participation import capacity_for
from .session_market import fill_price_for, touch_price_for
from .state_machine import OrderState
from .versions import EXECUTION_ENGINE_VERSION_V1_1, PAPER_SESSION_FILL_MODEL

if TYPE_CHECKING:
    from .authorization import ExecutionPlan
    from .journal import ExecutionJournal
    from .participation import SessionCapacity
    from .realism_policy import PaperExecutionPolicy
    from .session_market import PaperMarketSessionSnapshot

__all__ = [
    "SESSION_VENUE",
    "ChildOrderPlan",
    "SessionPaperBroker",
    "build_child_order",
    "child_order_id",
]

#: What the V1.1 broker calls itself, distinct from V1's so a reader can tell
#: which fill model produced a journal record without inspecting anything else.
SESSION_VENUE = "DETERMINISTIC_SESSION_PAPER_BROKER"


def _session_of(order: Order) -> str:
    """Read a child order's session back out of its deterministic id.

    The id is `LABEL@SESSION#Gn:digest`, so the session and generation it was
    derived for are recoverable from it. That matters because the authorisation
    check needs both, and taking them from the id rather than from a caller
    means a caller cannot claim a child belongs to a session it does not.

    Args:
        order: The child order.

    Returns:
        The session.

    Raises:
        ExecutionError: `UNAUTHORIZED_ORDER` if the id is not a child id.
    """
    _, _, rest = order.order_id.partition("@")
    session, _, _ = rest.partition("#")
    if not session:
        raise ExecutionError(
            Code.UNAUTHORIZED_ORDER,
            f"{order.order_id} is not a child order id of the form "
            "LABEL@SESSION#Gn:digest, so the session it belongs to cannot be "
            "established",
        )
    return session


def _generation_of(order: Order) -> int:
    """Read a child order's generation back out of its deterministic id.

    Args:
        order: The child order.

    Returns:
        The generation.

    Raises:
        ExecutionError: `UNAUTHORIZED_ORDER` if the id is not a child id.
    """
    _, _, rest = order.order_id.partition("#G")
    raw, _, _ = rest.partition(":")
    if not raw.isdigit():
        raise ExecutionError(
            Code.UNAUTHORIZED_ORDER,
            f"{order.order_id} is not a child order id of the form "
            "LABEL@SESSION#Gn:digest, so its generation cannot be established",
        )
    return int(raw)


def child_order_id(
    *,
    plan_fingerprint: str,
    identity_label: str,
    session: str,
    generation: int,
    authorized_residual: Decimal,
) -> str:
    """A deterministic id for one child DAY order.

    Derived from semantic inputs rather than generated, so the same residual in
    the same session under the same plan always produces the same id. That is
    what makes child creation idempotent across a crash: recovery recomputes the
    id, the journal already holds it, and nothing is created twice. A UUID would
    produce a second child for the same residual on every restart.

    Args:
        plan_fingerprint: The authorising plan.
        identity_label: The security's durable label.
        session: The session this child lives for.
        generation: Which child this is, from 1.
        authorized_residual: The quantity it is authorised to trade.

    Returns:
        The child order id.
    """
    digest = fingerprint_of(
        {
            "plan": plan_fingerprint,
            "identity": identity_label,
            "session": session,
            "generation": generation,
            "residual": str(quantise_quantity(authorized_residual)),
        }
    )
    return f"{identity_label}@{session}#G{generation}:{digest[:16]}"


@dataclass(frozen=True)
class ChildOrderPlan:
    """A child DAY order to be created for one session.

    Attributes:
        order: The child order.
        session: The session it lives for.
        generation: Which child this is, from 1.
        residual_before: The residual it was created to work.
    """

    order: Order
    session: str
    generation: int
    residual_before: Decimal

    def as_json(self) -> dict[str, Any]:
        """Return the child plan as a JSON-safe mapping."""
        return {
            "order_id": self.order.order_id,
            "identity": self.order.identity.label,
            "side": str(self.order.side),
            "session": self.session,
            "generation": self.generation,
            "residual_before": str(self.residual_before),
            "quantity": str(self.order.quantity),
        }


class SessionPaperBroker:
    """A deterministic broker that crosses spreads and respects session capacity.

    Produces events; commits nothing. Holds no cash, positions or P&L — the same
    boundary as the V1 broker, for the same reason.
    """

    def __init__(
        self,
        *,
        journal: ExecutionJournal,
        policy: PaperExecutionPolicy,
    ) -> None:
        """Open a broker over a journal, under a friction policy.

        Args:
            journal: The journal to read order state from.
            policy: The declared friction. Every cost comes from here.
        """
        self._journal = journal
        self._policy = policy

    @property
    def venue(self) -> str:
        """What this broker calls itself."""
        return SESSION_VENUE

    @property
    def policy(self) -> PaperExecutionPolicy:
        """The friction policy in force."""
        return self._policy

    def create(
        self, plan: ExecutionPlan, order: Order, *, session: str, at: str
    ) -> OrderCreatedEvent:
        """Record an authorised child order. Nothing is sent.

        Args:
            plan: The authorising plan.
            order: The child order.
            session: The session it lives for.
            at: When.

        Returns:
            The event, for the caller to commit.
        """
        plan.require_authorizes_child(order, session=session, generation=_generation_of(order))
        return OrderCreatedEvent(
            event_id=f"{order.order_id}:CREATED",
            event_type=ExecutionEventType.ORDER_CREATED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            order=order,
            session=session,
        )

    def submit(self, plan: ExecutionPlan, order: Order, *, at: str) -> OrderSubmittedEvent:
        """Hand the child order to this broker.

        Args:
            plan: The authorising plan.
            order: The child order.
            at: When.

        Returns:
            The event, for the caller to commit.
        """
        plan.require_authorizes_child(
            order, session=_session_of(order), generation=_generation_of(order)
        )
        return OrderSubmittedEvent(
            event_id=f"{order.order_id}:SUBMITTED",
            event_type=ExecutionEventType.ORDER_SUBMITTED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            venue=SESSION_VENUE,
        )

    def acknowledge(self, plan: ExecutionPlan, order: Order, *, at: str) -> OrderAcknowledgedEvent:
        """Accept the child order as working for its session.

        Args:
            plan: The authorising plan.
            order: The child order.
            at: When.

        Returns:
            The event, for the caller to commit.
        """
        plan.require_authorizes_child(
            order, session=_session_of(order), generation=_generation_of(order)
        )
        return OrderAcknowledgedEvent(
            event_id=f"{order.order_id}:ACKNOWLEDGED",
            event_type=ExecutionEventType.ORDER_ACKNOWLEDGED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            venue=SESSION_VENUE,
        )

    def capacity(
        self,
        order: Order,
        *,
        snapshot: PaperMarketSessionSnapshot,
        session: str,
        order_sessions: dict[str, str],
    ) -> SessionCapacity:
        """What remains fillable for this order's security in this session.

        Args:
            order: The child order.
            snapshot: The session's quotes and volume.
            session: The session.
            order_sessions: Which session each child order belongs to.

        Returns:
            The capacity, shared across every order touching that security.

        Raises:
            ExecutionError: `PAPER_QUOTE_REQUIRED` if the security is unquoted;
                `PAPER_VOLUME_REQUIRED` if its volume is unknown.
        """
        quote = snapshot.quote_for(order.identity)
        if quote is None:
            raise ExecutionError(
                Code.PAPER_QUOTE_REQUIRED,
                f"{order.identity.label} is not quoted in session {session}, so "
                f"{order.order_id} has no spread to cross and no volume to "
                "participate in. Nothing is substituted.",
            )
        return capacity_for(
            quote,
            session=session,
            policy=self._policy,
            derived=self._journal.derive(),
            order_sessions=order_sessions,
        )

    def fill(
        self,
        plan: ExecutionPlan,
        order: Order,
        *,
        snapshot: PaperMarketSessionSnapshot,
        session: str,
        order_sessions: dict[str, str],
        available_quantity: Decimal | None = None,
    ) -> OrderFilledEvent | None:
        """Fill as much as the order, the capacity and any availability allow.

        Args:
            plan: The authorising plan.
            order: The child order.
            snapshot: The session's quotes and volume.
            session: The session the order belongs to.
            order_sessions: Which session each child order belongs to.
            available_quantity: An optional further explicit cap.

        Returns:
            The fill event, or None when nothing can fill — a session that traded
            nothing, or a ceiling already reached. The order stays working and
            will expire at the close.

        Raises:
            ExecutionError: `PAPER_MARKET_SESSION_MISMATCH` for a snapshot from
                another session; `PAPER_QUOTE_REQUIRED` or
                `PAPER_VOLUME_REQUIRED` for missing inputs;
                `ORDER_ALREADY_TERMINAL` or `INVALID_ORDER_STATE_TRANSITION` if
                the order is not working.
        """
        plan.require_authorizes_child(order, session=session, generation=_generation_of(order))
        snapshot.require_session(session, order_id=order.order_id)

        status = self._journal.derive().status(order.order_id)
        if status.is_terminal:
            raise ExecutionError(
                Code.ORDER_ALREADY_EXPIRED
                if status.state is OrderState.EXPIRED
                else Code.ORDER_ALREADY_TERMINAL,
                f"{order.order_id} is {status.state} and cannot fill further. A DAY "
                "order that has expired is finished; a residual reaches the next "
                "session as a new child order.",
            )
        if status.state not in {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}:
            raise ExecutionError(
                Code.INVALID_ORDER_STATE_TRANSITION,
                f"{order.order_id} is {status.state} and is not working",
            )

        quote = snapshot.quote_for(order.identity)
        if quote is None:
            raise ExecutionError(
                Code.PAPER_QUOTE_REQUIRED,
                f"{order.identity.label} is not quoted in session {session}, so "
                f"{order.order_id} cannot fill",
            )
        bid, ask = quote.require_quote(policy=self._policy)
        capacity = self.capacity(
            order, snapshot=snapshot, session=session, order_sessions=order_sessions
        )

        limits = [status.remaining_quantity, capacity.remaining]
        if available_quantity is not None:
            limits.append(quantise_quantity(available_quantity))
        quantity = quantise_quantity(min(limits))
        if is_zero(quantity) or quantity < Decimal(0):
            # Either the session traded nothing, or the shared ceiling is used
            # up, or an explicit availability is zero. None of those is a
            # refusal: the order stays working and expires at the close.
            return None

        price = fill_price_for(order.side, bid=bid, ask=ask, policy=self._policy)
        index = status.fill_count + 1
        fill = ExecutionFill(
            fill_id=f"{order.order_id}#{index}",
            order_id=order.order_id,
            identity=order.identity,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=Decimal(0),
            effective_at=snapshot.as_of,
            attribution=_fill_attribution(order, quantity),
            source_fingerprint=snapshot.fingerprint(),
            venue=SESSION_VENUE,
        )
        return OrderFilledEvent(
            event_id=f"{order.order_id}:FILL:{index}",
            event_type=ExecutionEventType.ORDER_FILLED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=snapshot.as_of,
            fill=fill,
            session=session,
            bid=bid,
            ask=ask,
            touch_price=touch_price_for(order.side, bid=bid, ask=ask),
            observed_share_volume=quote.observed_share_volume,
            participation_ceiling=capacity.ceiling,
        )

    def expire(
        self, plan: ExecutionPlan, order: Order, *, session: str, at: str
    ) -> OrderExpiredEvent | None:
        """End a child order's DAY life at its session's close.

        Args:
            plan: The authorising plan.
            order: The child order.
            session: The session that closed.
            at: When the session closed.

        Returns:
            The expiry event, or None when the order already filled in full —
            there is nothing left to expire.

        Raises:
            ExecutionError: If the order is terminal for another reason.
        """
        plan.require_authorizes_child(order, session=session, generation=_generation_of(order))
        status = self._journal.derive().status(order.order_id)
        if status.state is OrderState.FILLED:
            return None
        if status.is_terminal:
            raise ExecutionError(
                Code.ORDER_ALREADY_TERMINAL,
                f"{order.order_id} is already {status.state} and cannot expire",
            )
        return OrderExpiredEvent(
            event_id=f"{order.order_id}:EXPIRED",
            event_type=ExecutionEventType.ORDER_EXPIRED,
            order_id=order.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=at,
            session=session,
            residual_quantity=status.remaining_quantity,
        )

    def describe(self) -> dict[str, Any]:
        """Describe what this broker is and is not."""
        return {
            "venue": SESSION_VENUE,
            "engine_version": EXECUTION_ENGINE_VERSION_V1_1,
            "fill_model": PAPER_SESSION_FILL_MODEL,
            "policy_fingerprint": self._policy.fingerprint(),
            "in_process": True,
            "network": "NONE",
            "broker_sdk": "NONE",
            "credentials": "NONE",
            "randomness": "NONE",
            "holds_cash": False,
            "holds_positions": False,
            "holds_pnl": False,
            "buying_power_authority": "accounting cash",
            "position_authority": "accounting holdings",
            "fill_quantity": (
                "min(remaining order, remaining session capacity, explicit "
                "availability where given). Deterministic in every case."
            ),
            "fill_price": (
                "the touch price worsened by the declared adverse slippage. A "
                "buyer never pays less than the ask and a seller never receives "
                "more than the bid."
            ),
            "models": [
                "quoted spread crossing",
                "declared adverse slippage",
                "session share-volume participation",
                "DAY expiry",
            ],
            "does_not_model": [
                "market impact",
                "latency",
                "queue position",
                "hidden liquidity",
                "venue routing",
                "auction behaviour",
                "intraday price path",
            ],
        }


def _fill_attribution(order: Order, quantity: Decimal) -> tuple[AttributionShare, ...]:
    """Divide a fill across the strategies that authorised the child order.

    Args:
        order: The child order.
        quantity: What filled.

    Returns:
        The shares, summing exactly to the filled quantity.
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


def build_child_order(
    *,
    plan: ExecutionPlan,
    parent: Order,
    session: str,
    generation: int,
    residual: Decimal,
    sizing_price: Decimal,
    created_at: str,
) -> Order:
    """Build the child DAY order that works a residual in one session.

    The child inherits the parent's security, side and authorisation, and its
    attribution is scaled down to the residual so the shares still sum to what it
    is authorised to trade.

    Args:
        plan: The authorising plan.
        parent: The order whose residual this works.
        session: The session this child lives for.
        generation: Which child this is, from 1.
        residual: The quantity it is authorised to trade.
        sizing_price: The parent's sizing price, carried for reporting.
        created_at: When it was created.

    Returns:
        The child order.

    Raises:
        ExecutionError: `ORDER_INVALID` if the residual is not positive — a child
            for nothing is not an order.
    """
    quantity = quantise_quantity(residual)
    if quantity <= Decimal(0):
        raise ExecutionError(
            Code.ORDER_INVALID,
            f"{parent.identity.label}: a child order needs a positive residual, "
            f"got {quantity}. A fully filled parent produces no child.",
        )
    shares = (
        tuple(
            AttributionShare(strategy_id=row.strategy_id, quantity=row.quantity)
            for row in allocate_partial_fill(
                authorized=tuple((row.strategy_id, row.quantity) for row in parent.attribution),
                filled_quantity=quantity,
            )
        )
        if parent.attribution
        else ()
    )
    return Order(
        order_id=child_order_id(
            plan_fingerprint=plan.fingerprint(),
            identity_label=parent.identity.label,
            session=session,
            generation=generation,
            authorized_residual=quantity,
        ),
        execution_plan_id=plan.execution_plan_id,
        authorization_fingerprint=plan.fingerprint(),
        identity=parent.identity,
        side=parent.side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        sizing_price=sizing_price,
        attribution=shares,
        created_at=created_at,
        symbol=parent.symbol,
        sequence_group=parent.sequence_group,
    )
