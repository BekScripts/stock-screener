"""The order contract, and the one invariant the whole layer rests on.

**An order is not a position.** It does not change holdings, cash or NAV. It is a
statement of intent to trade, and the only thing in this system that changes the
book is an admitted accounting `FillEvent`. Every type in this module is inert in
exactly that sense: constructing one, submitting one, acknowledging one — none of
it touches a ledger.

That is worth stating as a design property rather than a convention, because the
permissive alternative is so easy to write. An execution engine that decremented
cash when it sent a buy would look right in every happy path and would diverge
from the book the first time an order was rejected, cancelled or partially
filled — and the divergence would be a second set of numbers claiming to be the
portfolio.

The baseline is narrow on purpose: market orders, day only, long-only, and a
durable `(kind, identity_id)` identity with the ticker as metadata. Anything
outside that is refused by name rather than approximated, because an order type
this engine does not implement is not a worse order — it is a different one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import quantise_price, quantise_quantity

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .versions import EXECUTION_ENGINE_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "SUPPORTED_ORDER_TYPES",
    "SUPPORTED_TIME_IN_FORCE",
    "AttributionShare",
    "Order",
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "shares_from",
]

#: What this engine implements. A type outside this set is refused rather than
#: silently treated as a market order — a limit order executed at market is not
#: a near-miss, it is a trade nobody authorised.
SUPPORTED_ORDER_TYPES: frozenset[str] = frozenset({"MARKET"})

#: Likewise for time in force. There is no GTC here, and pretending otherwise
#: would mean an order silently expiring at a session boundary the caller
#: believed it would survive.
SUPPORTED_TIME_IN_FORCE: frozenset[str] = frozenset({"DAY"})


class OrderType(StrEnum):
    """How an order is to be priced."""

    MARKET = "MARKET"
    """Fills at whatever price the execution venue supplies."""


class TimeInForce(StrEnum):
    """How long an order remains live."""

    DAY = "DAY"
    """Expires at the end of the session it was placed in."""


class OrderSide(StrEnum):
    """Which way an order trades.

    There is no `SELL_SHORT` and no `BUY_TO_COVER`. A sell may reduce or close a
    holding and may never intentionally open a short; accounting refuses one
    regardless, and leaving the vocabulary out keeps the intent unexpressible
    rather than merely unenforced.
    """

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class AttributionShare:
    """How much of an order one strategy authorised.

    Carried on the order so a partial fill can be divided across the strategies
    that asked for it. Without this the division would have to be guessed at fill
    time, and accounting's per-attribution pools would receive a number nobody
    decided.

    Attributes:
        strategy_id: The owner, or `UNATTRIBUTED_EXISTING`.
        quantity: The quantity this owner authorised.
    """

    strategy_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        """Normalise the quantity and refuse an unusable share.

        Raises:
            ExecutionError: On a missing owner or a non-positive quantity. A
                zero-quantity share is noise in the attribution and would make
                the proportional split ambiguous.
        """
        if not self.strategy_id:
            raise ExecutionError(Code.ORDER_INVALID, "an attribution share must name its strategy")
        quantity = quantise_quantity(self.quantity)
        if quantity <= Decimal(0):
            raise ExecutionError(
                Code.ORDER_INVALID,
                f"{self.strategy_id}: an attribution share must be positive, got {quantity}",
            )
        object.__setattr__(self, "quantity", quantity)

    def as_json(self) -> dict[str, Any]:
        """Return the share as a JSON-safe mapping."""
        return {"strategy_id": self.strategy_id, "quantity": str(self.quantity)}


@dataclass(frozen=True)
class Order:
    """One authorised instruction to trade. Inert.

    Attributes:
        order_id: Stable identifier, deterministic within a plan.
        execution_plan_id: The plan that authorised it.
        authorization_fingerprint: The plan's fingerprint. A broker compares this
            against the plan it is executing and refuses a mismatch, so an order
            cannot be replayed against a different authorisation.
        identity: The security, by durable key.
        side: Buy or sell.
        quantity: Shares to trade. Positive; `side` carries the direction.
        order_type: `MARKET` only in V1.
        time_in_force: `DAY` only in V1.
        sizing_price: The price the quantity was derived from. Informational, and
            deliberately **not** a fill price — see `paper_market`.
        attribution: Which strategies authorised which part of the quantity.
        created_at: When the order was created, ISO 8601 with offset.
        symbol: The ticker, for display only. Never a key.
        sequence_group: Which sequencing group it belongs to, for the audit trail.
    """

    order_id: str
    execution_plan_id: str
    authorization_fingerprint: str
    identity: SecurityIdentity
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    sizing_price: Decimal = Decimal(0)
    attribution: tuple[AttributionShare, ...] = ()
    created_at: str = ""
    symbol: str | None = field(default=None, compare=False)
    sequence_group: str = ""

    def __post_init__(self) -> None:
        """Validate the order against the frozen contract.

        Raises:
            ExecutionError: `UNSUPPORTED_ORDER_TYPE` or
                `UNSUPPORTED_TIME_IN_FORCE` for anything outside the V1 baseline;
                `ORDER_INVALID` on a missing id, a non-positive quantity, or an
                attribution that does not sum to the quantity.
        """
        if str(self.order_type) not in SUPPORTED_ORDER_TYPES:
            raise ExecutionError(
                Code.UNSUPPORTED_ORDER_TYPE,
                f"{self.order_type} is not implemented by "
                f"{EXECUTION_ENGINE_VERSION} (supported: "
                f"{', '.join(sorted(SUPPORTED_ORDER_TYPES))}). Executing it as a "
                "market order would be a trade on terms nobody authorised.",
            )
        if str(self.time_in_force) not in SUPPORTED_TIME_IN_FORCE:
            raise ExecutionError(
                Code.UNSUPPORTED_TIME_IN_FORCE,
                f"{self.time_in_force} is not implemented by "
                f"{EXECUTION_ENGINE_VERSION} (supported: "
                f"{', '.join(sorted(SUPPORTED_TIME_IN_FORCE))}). A caller "
                "believing an order survives the session would be wrong.",
            )
        for name in ("order_id", "execution_plan_id", "authorization_fingerprint"):
            if not getattr(self, name):
                raise ExecutionError(
                    Code.ORDER_INVALID,
                    f"an order must carry {name}; an order that cannot name the "
                    "plan authorising it cannot be checked against one",
                )
        quantity = quantise_quantity(self.quantity)
        if quantity <= Decimal(0):
            raise ExecutionError(
                Code.ORDER_INVALID,
                f"{self.identity.label}: an order must trade a positive quantity, "
                f"got {quantity}. Direction is carried by `side`, and a "
                "zero-quantity order is not an order — a HOLD produces none.",
            )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "sizing_price", quantise_price(self.sizing_price))
        if self.attribution:
            total = quantise_quantity(sum((row.quantity for row in self.attribution), Decimal(0)))
            if total != quantity:
                raise ExecutionError(
                    Code.FILL_ATTRIBUTION_DOES_NOT_SUM,
                    f"{self.identity.label}: the order's attribution sums to "
                    f"{total} but its quantity is {quantity}. The shares are not "
                    "scaled to fit: a mismatch means the planner disagrees with "
                    "itself about who authorised what.",
                )

    @property
    def notional(self) -> Decimal:
        """Quantity times the sizing price. Intended, not realised."""
        return quantise_price(self.quantity * self.sizing_price)

    @property
    def owners(self) -> tuple[str, ...]:
        """The strategies that authorised this order, in a deterministic order."""
        return tuple(sorted(row.strategy_id for row in self.attribution))

    def content_body(self) -> dict[str, Any]:
        """What this order instructs, without its back-reference to the plan.

        The authorisation fingerprint is deliberately absent. A plan's digest
        covers its orders, and an order's authorisation *is* that digest — so
        including it here would make each depend on the other and neither
        computable. The binding is still checked, separately and explicitly, by
        `ExecutionPlan.require_authorizes`.

        Keeping the two apart also means an order's identity is what it
        instructs: the same trade re-authorised by a revised plan has the same
        content fingerprint, and the revision shows up where it belongs, in the
        authorisation check.
        """
        return {
            "engine_version": EXECUTION_ENGINE_VERSION,
            "order_id": self.order_id,
            "execution_plan_id": self.execution_plan_id,
            "identity": self.identity.label,
            "side": str(self.side),
            "quantity": str(self.quantity),
            "order_type": str(self.order_type),
            "time_in_force": str(self.time_in_force),
            "sizing_price": str(self.sizing_price),
            "attribution": [
                row.as_json() for row in sorted(self.attribution, key=lambda row: row.strategy_id)
            ],
            "sequence_group": self.sequence_group,
        }

    def body(self) -> dict[str, Any]:
        """The order's content plus the authorisation it claims."""
        return {
            **self.content_body(),
            "authorization_fingerprint": self.authorization_fingerprint,
        }

    def fingerprint(self) -> str:
        """The order's deterministic digest, over what it instructs."""
        return fingerprint_of(self.content_body())

    def as_json(self) -> dict[str, Any]:
        """Return the order as a JSON-safe mapping."""
        return {
            **self.body(),
            "symbol": self.symbol,
            "created_at": self.created_at,
            "notional": str(self.notional),
            "order_fingerprint": self.fingerprint(),
            "changes_holdings": False,
            "changes_cash": False,
            "note": (
                "an order is inert. Only an admitted accounting FillEvent changes "
                "holdings, cash or NAV."
            ),
        }


def shares_from(rows: Sequence[Mapping[str, Any]]) -> tuple[AttributionShare, ...]:
    """Build attribution shares from their JSON form.

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
