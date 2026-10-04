"""Execution events: the only things that change an order's state.

The journal is to orders what the accounting ledger is to holdings — the sole
authority, append-only, and reconstructible by replay. An order's state is never
stored as a field somebody updates; it is derived from the events that happened
to it. That is what makes "how did this order come to be cancelled" a question
with an answer.

`ExecutionFill` is the pivot of the whole phase. It is *execution evidence* — a
statement that a quantity traded at a price — and it is deliberately not an
accounting event. The accounting `FillEvent` is derived from it by the adapter,
and the two are kept distinct because "the fill happened" and "the fill is in the
book" are different facts that can be true at different times, and a crash
between them must be recoverable rather than ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import as_decimal, quantise_price, quantise_quantity

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .models import AttributionShare
from .state_machine import OrderState
from .versions import EXECUTION_ENGINE_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.portfolio_risk import SecurityIdentity

    from .models import Order, OrderSide

__all__ = [
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionFill",
    "FillApplication",
    "FillAppliedEvent",
    "OrderAcknowledgedEvent",
    "OrderCancelledEvent",
    "OrderCreatedEvent",
    "OrderFilledEvent",
    "OrderRejectedEvent",
    "OrderSubmittedEvent",
    "shares_from_json",
]


class ExecutionEventType(StrEnum):
    """What kind of fact an execution event records."""

    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    ORDER_FILLED = "ORDER_FILLED"
    """A fill, whether partial or complete. The resulting order state is derived
    from the quantity rather than named, so no event can claim an order is
    `FILLED` while quantity remains."""

    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    FILL_APPLIED = "FILL_APPLIED"
    """The accounting layer has durably admitted a fill. Recorded separately from
    the fill itself: this is what makes the handoff recoverable."""


class FillApplication(StrEnum):
    """Whether a fill has reached the book."""

    PENDING = "PENDING"
    """Durable as execution evidence; not yet in the book. Recovery applies it."""

    APPLIED = "APPLIED"
    """Admitted to accounting and confirmed durable."""

    FAILED = "FAILED"
    """Accounting refused it. The order does not advance, and the plan stops."""


@dataclass(frozen=True)
class ExecutionEvent:
    """The envelope every execution event carries.

    Attributes:
        event_id: The caller's idempotency key. The same id with the same
            payload is a no-op; with a different payload it is a conflict.
        event_type: What kind of fact this is.
        order_id: Which order it concerns.
        execution_plan_id: Which plan authorised that order.
        occurred_at: When it happened, ISO 8601 with offset. Semantic.
        recorded_at: When it reached the journal. Operational, and outside the
            fingerprint — a digest that moved with the clock could never
            establish that two journals agree.
    """

    event_id: str
    event_type: ExecutionEventType
    order_id: str
    execution_plan_id: str
    occurred_at: str
    recorded_at: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Refuse an envelope that could not be audited.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` on a missing field.
        """
        for name in ("event_id", "order_id", "execution_plan_id", "occurred_at"):
            if not getattr(self, name):
                raise ExecutionError(
                    Code.EXECUTION_EVENT_INVALID,
                    f"an execution event must carry {name}; one that cannot name "
                    "its order or its moment cannot be replayed into a lifecycle",
                )

    def envelope(self) -> dict[str, Any]:
        """The semantic envelope, without the recording clock."""
        return {
            "engine_version": EXECUTION_ENGINE_VERSION,
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "order_id": self.order_id,
            "execution_plan_id": self.execution_plan_id,
            "occurred_at": self.occurred_at,
        }

    def payload(self) -> dict[str, Any]:  # pragma: no cover - overridden
        """The event-specific semantic payload."""
        raise NotImplementedError

    def body(self) -> dict[str, Any]:
        """The full fingerprinted body."""
        return {**self.envelope(), "payload": self.payload()}

    def fingerprint(self) -> str:
        """The event's deterministic digest."""
        return fingerprint_of(self.body())

    def resulting_state(self) -> OrderState | None:  # pragma: no cover - overridden
        """The order state this event implies, or None if it implies none."""
        return None

    def as_json(self) -> dict[str, Any]:
        """Return the event with its fingerprint and recording time."""
        return {
            **self.body(),
            "fingerprint": self.fingerprint(),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class OrderCreatedEvent(ExecutionEvent):
    """An authorised order has been recorded. Nothing has been sent.

    Attributes:
        order: The order. Carried in full so the journal alone can reconstruct
            every order without the plan being present.
    """

    order: Order | None = None

    def __post_init__(self) -> None:
        """Refuse a creation event with no order.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` if the order is absent.
        """
        super().__post_init__()
        if self.order is None:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID, "an order-created event must carry its order"
            )

    def payload(self) -> dict[str, Any]:
        """The order's content and the authorisation it claims."""
        assert self.order is not None  # noqa: S101 - established above
        return {"order": self.order.body()}

    def resulting_state(self) -> OrderState:
        """`CREATED`."""
        return OrderState.CREATED


@dataclass(frozen=True)
class OrderSubmittedEvent(ExecutionEvent):
    """The order has been handed to a broker."""

    venue: str = "DETERMINISTIC_PAPER_BROKER"

    def payload(self) -> dict[str, Any]:
        """The venue it went to."""
        return {"venue": self.venue}

    def resulting_state(self) -> OrderState:
        """`SUBMITTED`."""
        return OrderState.SUBMITTED


@dataclass(frozen=True)
class OrderAcknowledgedEvent(ExecutionEvent):
    """The broker has accepted the order as working."""

    venue: str = "DETERMINISTIC_PAPER_BROKER"

    def payload(self) -> dict[str, Any]:
        """The venue that accepted it."""
        return {"venue": self.venue}

    def resulting_state(self) -> OrderState:
        """`ACKNOWLEDGED`."""
        return OrderState.ACKNOWLEDGED


@dataclass(frozen=True)
class ExecutionFill:
    """Evidence that a quantity traded at a price. Broker-neutral.

    Attributes:
        fill_id: Stable identifier. Maps deterministically to one accounting
            event id, which is what makes the handoff exactly-once.
        order_id: The order this filled.
        identity: The security.
        side: Buy or sell.
        quantity: What traded. Positive.
        price: What it traded at. Positive. This is the *fill* price and need not
            equal the price that sized the order.
        fee: What it cost to trade. Expensed by accounting, not capitalised.
        effective_at: When it traded, ISO 8601 with offset.
        attribution: Which strategies the filled quantity belongs to. Must sum to
            the quantity exactly.
        source_fingerprint: The execution snapshot it came from.
        venue: Which broker produced it.
    """

    fill_id: str
    order_id: str
    identity: SecurityIdentity
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal(0)
    effective_at: str = ""
    attribution: tuple[AttributionShare, ...] = ()
    source_fingerprint: str = ""
    venue: str = "DETERMINISTIC_PAPER_BROKER"

    def __post_init__(self) -> None:
        """Validate the fill.

        Raises:
            ExecutionError: `FILL_INVALID` on a missing id, a non-positive
                quantity or price, or a negative fee;
                `FILL_ATTRIBUTION_DOES_NOT_SUM` if the shares disagree with the
                quantity. The shares are not scaled to fit — a mismatch means the
                producer disagrees with itself about what traded.
        """
        for name in ("fill_id", "order_id", "effective_at"):
            if not getattr(self, name):
                raise ExecutionError(Code.FILL_INVALID, f"a fill must carry {name}")
        quantity = quantise_quantity(as_decimal(self.quantity, field="fill quantity"))
        price = quantise_price(as_decimal(self.price, field="fill price"))
        fee = quantise_price(as_decimal(self.fee, field="fill fee"))
        if quantity <= Decimal(0):
            raise ExecutionError(
                Code.FILL_INVALID,
                f"{self.identity.label}: a fill must move a positive quantity, got {quantity}",
            )
        if price <= Decimal(0):
            raise ExecutionError(
                Code.FILL_INVALID,
                f"{self.identity.label}: a fill price must be positive, got {price}",
            )
        if fee < Decimal(0):
            raise ExecutionError(
                Code.FILL_INVALID, f"{self.identity.label}: a fee may not be negative"
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fee", fee)
        if self.attribution:
            total = quantise_quantity(sum((row.quantity for row in self.attribution), Decimal(0)))
            if total != quantity:
                raise ExecutionError(
                    Code.FILL_ATTRIBUTION_DOES_NOT_SUM,
                    f"{self.identity.label}: the fill's attribution sums to {total} "
                    f"but {quantity} traded. The shares are not scaled to fit.",
                )

    @property
    def accounting_event_id(self) -> str:
        """The accounting event id this fill maps to.

        Derived from the fill id rather than generated, so re-presenting the same
        fill produces the same accounting event id and the accounting layer's own
        exactly-once check does the rest. A generated id would make every retry a
        second trade.
        """
        return f"EXEC_FILL:{self.fill_id}"

    @property
    def notional(self) -> Decimal:
        """Quantity times price, before fees."""
        return quantise_price(self.quantity * self.price)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "identity": self.identity.label,
            "side": str(self.side),
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fee": str(self.fee),
            "effective_at": self.effective_at,
            "attribution": [
                row.as_json() for row in sorted(self.attribution, key=lambda row: row.strategy_id)
            ],
            "source_fingerprint": self.source_fingerprint,
            "venue": self.venue,
        }

    def fingerprint(self) -> str:
        """The fill's deterministic digest."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the fill as a JSON-safe mapping."""
        return {
            **self.body(),
            "notional": str(self.notional),
            "fill_fingerprint": self.fingerprint(),
            "accounting_event_id": self.accounting_event_id,
        }


@dataclass(frozen=True)
class OrderFilledEvent(ExecutionEvent):
    """A fill happened. The order's new state follows from the quantity.

    Attributes:
        fill: The execution evidence.
    """

    fill: ExecutionFill | None = None

    def __post_init__(self) -> None:
        """Refuse a fill event with no fill.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` if the fill is absent, or
                if it names a different order than the envelope.
        """
        super().__post_init__()
        if self.fill is None:
            raise ExecutionError(Code.EXECUTION_EVENT_INVALID, "a fill event must carry its fill")
        if self.fill.order_id != self.order_id:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"the event names order {self.order_id!r} and its fill names "
                f"{self.fill.order_id!r}",
            )

    def payload(self) -> dict[str, Any]:
        """The fill's body."""
        assert self.fill is not None  # noqa: S101 - established above
        return {"fill": self.fill.body()}


@dataclass(frozen=True)
class OrderCancelledEvent(ExecutionEvent):
    """The order was stopped. Prior fills stand.

    Attributes:
        reason: Why.
    """

    reason: str = "CANCELLED"

    def payload(self) -> dict[str, Any]:
        """The reason."""
        return {"reason": self.reason}

    def resulting_state(self) -> OrderState:
        """`CANCELLED`."""
        return OrderState.CANCELLED


@dataclass(frozen=True)
class OrderRejectedEvent(ExecutionEvent):
    """The order never worked. Nothing traded.

    Attributes:
        reason: Why.
    """

    reason: str = "REJECTED"

    def payload(self) -> dict[str, Any]:
        """The reason."""
        return {"reason": self.reason}

    def resulting_state(self) -> OrderState:
        """`REJECTED`."""
        return OrderState.REJECTED


@dataclass(frozen=True)
class FillAppliedEvent(ExecutionEvent):
    """Accounting has durably admitted a fill.

    Attributes:
        fill_id: Which fill.
        accounting_event_id: The ledger event it became.
        accounting_sequence: Where it landed in the ledger.
        accounting_state_fingerprint: The book afterwards, so the journal and the
            ledger can be checked against each other without replaying both.
    """

    fill_id: str = ""
    accounting_event_id: str = ""
    accounting_sequence: int = 0
    accounting_state_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Refuse an application that cannot name what it applied.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` on a missing reference.
        """
        super().__post_init__()
        for name in ("fill_id", "accounting_event_id", "accounting_state_fingerprint"):
            if not getattr(self, name):
                raise ExecutionError(
                    Code.EXECUTION_EVENT_INVALID,
                    f"a fill-applied event must carry {name}; without it the "
                    "handoff cannot be verified on recovery",
                )

    def payload(self) -> dict[str, Any]:
        """What was applied, and where."""
        return {
            "fill_id": self.fill_id,
            "accounting_event_id": self.accounting_event_id,
            "accounting_sequence": self.accounting_sequence,
            "accounting_state_fingerprint": self.accounting_state_fingerprint,
        }


def shares_from_json(rows: Sequence[Mapping[str, Any]]) -> tuple[AttributionShare, ...]:
    """Build attribution shares from their persisted form.

    Args:
        rows: `[{"strategy_id": ..., "quantity": ...}]`.

    Returns:
        The shares, in strategy order.
    """
    return tuple(
        sorted(
            (
                AttributionShare(
                    strategy_id=str(row["strategy_id"]),
                    quantity=Decimal(str(row["quantity"])),
                )
                for row in rows
            ),
            key=lambda row: row.strategy_id,
        )
    )
