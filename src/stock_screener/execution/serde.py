"""Rebuilding execution events from their persisted form.

An unrecognised event type is refused rather than loaded as a generic record. A
record the state machine cannot apply must not sit quietly in a journal that
replays cleanly without it — that is a lifecycle with a hole in it, and the hole
would be invisible.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypedDict

from .errors import Code, ExecutionError
from .events import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionFill,
    FillAppliedEvent,
    OrderAcknowledgedEvent,
    OrderCancelledEvent,
    OrderCreatedEvent,
    OrderExpiredEvent,
    OrderFilledEvent,
    OrderRejectedEvent,
    OrderSubmittedEvent,
    shares_from_json,
)
from .models import Order, OrderSide, OrderType, TimeInForce

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["event_from_json", "identity_from_label", "order_from_json"]


class _Envelope(TypedDict):
    """The envelope fields every event shares, so they can be splatted typed."""

    event_id: str
    order_id: str
    execution_plan_id: str
    occurred_at: str
    recorded_at: str | None


def identity_from_label(label: str) -> Any:
    """Rebuild a durable identity from its `KIND:id` label.

    The display symbol is deliberately not restored, for the reason the
    accounting ledger does not persist one: a ticker is a dated display
    attribute, and storing it invites a later reader to treat a reassigned symbol
    as the same company.

    Args:
        label: The label.

    Returns:
        The identity, without a display symbol.

    Raises:
        ExecutionError: If the label is malformed.
    """
    from stock_screener.portfolio_risk import SecurityIdentity

    kind, _, raw = str(label).partition(":")
    if not kind or not raw.isdigit():
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            f"{label!r} is not a durable identity label of the form KIND:id",
        )
    return SecurityIdentity(kind=kind, identity_id=int(raw))


def _optional_decimal(value: Any) -> Decimal | None:
    """Parse an optional Decimal from its persisted form.

    Args:
        value: The string, or None.

    Returns:
        The Decimal, or None. None is preserved rather than becoming zero: a
        missing quote is not a quote of nothing.
    """
    return None if value is None else Decimal(str(value))


def order_from_json(payload: Mapping[str, Any]) -> Order:
    """Rebuild an order from its persisted body.

    Args:
        payload: The order as `Order.body` wrote it.

    Returns:
        The order.
    """
    return Order(
        order_id=str(payload["order_id"]),
        execution_plan_id=str(payload["execution_plan_id"]),
        authorization_fingerprint=str(payload["authorization_fingerprint"]),
        identity=identity_from_label(str(payload["identity"])),
        side=OrderSide(str(payload["side"])),
        quantity=Decimal(str(payload["quantity"])),
        order_type=OrderType(str(payload["order_type"])),
        time_in_force=TimeInForce(str(payload["time_in_force"])),
        sizing_price=Decimal(str(payload["sizing_price"])),
        attribution=shares_from_json(payload.get("attribution", ())),
        sequence_group=str(payload.get("sequence_group", "")),
    )


def _fill_from_json(payload: Mapping[str, Any]) -> ExecutionFill:
    """Rebuild an execution fill from its persisted body."""
    return ExecutionFill(
        fill_id=str(payload["fill_id"]),
        order_id=str(payload["order_id"]),
        identity=identity_from_label(str(payload["identity"])),
        side=OrderSide(str(payload["side"])),
        quantity=Decimal(str(payload["quantity"])),
        price=Decimal(str(payload["price"])),
        fee=Decimal(str(payload["fee"])),
        effective_at=str(payload["effective_at"]),
        attribution=shares_from_json(payload.get("attribution", ())),
        source_fingerprint=str(payload.get("source_fingerprint", "")),
        venue=str(payload.get("venue", "")),
    )


def event_from_json(payload: Mapping[str, Any]) -> ExecutionEvent:
    """Rebuild an execution event from its persisted form.

    Args:
        payload: The event as `ExecutionEvent.as_json` wrote it.

    Returns:
        The event.

    Raises:
        ExecutionError: `EXECUTION_EVENT_INVALID` for an unknown event type. A
            record the state machine cannot apply must not sit in a journal that
            would otherwise replay cleanly without it.
    """
    event_type = str(payload["event_type"])
    body = payload.get("payload", {})
    recorded = payload.get("recorded_at")
    common: _Envelope = {
        "event_id": str(payload["event_id"]),
        "order_id": str(payload["order_id"]),
        "execution_plan_id": str(payload["execution_plan_id"]),
        "occurred_at": str(payload["occurred_at"]),
        "recorded_at": None if recorded is None else str(recorded),
    }
    if event_type == ExecutionEventType.ORDER_CREATED:
        return OrderCreatedEvent(
            event_type=ExecutionEventType.ORDER_CREATED,
            order=order_from_json(body["order"]),
            session=body.get("session"),
            **common,
        )
    if event_type == ExecutionEventType.ORDER_SUBMITTED:
        return OrderSubmittedEvent(
            event_type=ExecutionEventType.ORDER_SUBMITTED,
            venue=str(body.get("venue", "")),
            **common,
        )
    if event_type == ExecutionEventType.ORDER_ACKNOWLEDGED:
        return OrderAcknowledgedEvent(
            event_type=ExecutionEventType.ORDER_ACKNOWLEDGED,
            venue=str(body.get("venue", "")),
            **common,
        )
    if event_type == ExecutionEventType.ORDER_FILLED:
        market = body.get("market", {})
        return OrderFilledEvent(
            event_type=ExecutionEventType.ORDER_FILLED,
            fill=_fill_from_json(body["fill"]),
            session=market.get("session"),
            bid=_optional_decimal(market.get("bid")),
            ask=_optional_decimal(market.get("ask")),
            touch_price=_optional_decimal(market.get("touch_price")),
            observed_share_volume=_optional_decimal(market.get("observed_share_volume")),
            participation_ceiling=_optional_decimal(market.get("participation_ceiling")),
            **common,
        )
    if event_type == ExecutionEventType.ORDER_EXPIRED:
        return OrderExpiredEvent(
            event_type=ExecutionEventType.ORDER_EXPIRED,
            session=str(body["session"]),
            residual_quantity=Decimal(str(body.get("residual_quantity", "0"))),
            **common,
        )
    if event_type == ExecutionEventType.ORDER_CANCELLED:
        return OrderCancelledEvent(
            event_type=ExecutionEventType.ORDER_CANCELLED,
            reason=str(body.get("reason", "")),
            **common,
        )
    if event_type == ExecutionEventType.ORDER_REJECTED:
        return OrderRejectedEvent(
            event_type=ExecutionEventType.ORDER_REJECTED,
            reason=str(body.get("reason", "")),
            **common,
        )
    if event_type == ExecutionEventType.FILL_APPLIED:
        return FillAppliedEvent(
            event_type=ExecutionEventType.FILL_APPLIED,
            fill_id=str(body["fill_id"]),
            accounting_event_id=str(body["accounting_event_id"]),
            accounting_sequence=int(body.get("accounting_sequence", 0)),
            accounting_state_fingerprint=str(body["accounting_state_fingerprint"]),
            **common,
        )
    raise ExecutionError(
        Code.EXECUTION_EVENT_INVALID,
        f"{event_type!r} is not an event type this engine can load. A record the "
        "state machine cannot apply must not sit in a journal that would "
        "otherwise replay cleanly without it.",
    )
