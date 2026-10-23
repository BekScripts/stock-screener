"""Turning authorised deltas into a sequenced set of orders.

The planner is pure. It takes an approved portfolio, the execution intents the
risk engine derived from it, a sizing snapshot and the current book, and returns
an `ExecutionPlan`. It fetches nothing — no price lookup, no market data call, no
clock-dependent decision — because a planner that reached for current prices would
produce a different plan each time it ran and no plan could be reproduced from
its inputs.

Three decisions are frozen here.

**The sizing price is not a fill price.** Quantity is derived as
`|notional delta| / sizing price` from an explicit snapshot. What the order
actually fills at comes from the execution venue and may differ. Conflating them
would make every backtest fill at exactly the price that sized it, which is the
most flattering possible assumption and never true.

**Sells precede buys** (`SELLS_BEFORE_BUYS_V1`). A no-margin book cannot fund
purchases until sales have settled cash into it, so exits and reductions are
sequenced ahead of entries and increases. The risk layer's `Action` deliberately
declines to imply an ordering — it says a broker and a settlement regime decide —
and this is that decision, made once and recorded.

**Nothing is prioritised by strategy.** Positions are economic and aggregate: two
strategies wanting the same security hold one position and trade through one
order. Executing one strategy's orders first would give it the better prices for
no reason anybody chose.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import as_decimal, quantise_price, quantise_quantity

from .authorization import BookBinding, ExecutionPlan, intents_fingerprint
from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .models import AttributionShare, Order, OrderSide, OrderType, TimeInForce
from .versions import ORDER_SEQUENCING_RULE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "SEQUENCE_GROUPS",
    "ExecutionSizingSnapshot",
    "SizingPrice",
    "plan_execution",
    "sequence_group_of",
]

#: The order of the sequencing groups. Releasing cash precedes consuming it.
SEQUENCE_GROUPS: tuple[str, ...] = ("RELEASE_CASH", "CONSUME_CASH")

#: Which group each risk-layer action belongs to. `HOLD` is absent because it
#: produces no order at all.
_ACTION_GROUPS: Mapping[str, str] = {
    "EXIT": "RELEASE_CASH",
    "REDUCE": "RELEASE_CASH",
    "ENTER": "CONSUME_CASH",
    "INCREASE": "CONSUME_CASH",
}


def sequence_group_of(action: str) -> str:
    """Which sequencing group an action belongs to.

    Args:
        action: The risk-layer action name.

    Returns:
        The group.

    Raises:
        ExecutionError: `PLAN_INVALID` for an action with no sequencing meaning.
            A new action silently defaulting into the buy group would be funded
            by cash that had not arrived.
    """
    group = _ACTION_GROUPS.get(action)
    if group is None:
        raise ExecutionError(
            Code.PLAN_INVALID,
            f"{action!r} has no sequencing group under {ORDER_SEQUENCING_RULE}. A "
            "new action defaulting into the buy group would be funded by cash "
            "that has not been released yet.",
        )
    return group


@dataclass(frozen=True)
class SizingPrice:
    """One security's price, for deriving a quantity.

    Attributes:
        identity: The security.
        price: The price per share. Must be positive and finite.
        source_fingerprint: Where the price came from, so a plan can say what it
            was sized against.
    """

    identity: SecurityIdentity
    price: Decimal
    source_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Refuse a price no quantity can be derived from.

        Raises:
            ExecutionError: `INVALID_EXECUTION_PRICE` on a non-positive or
                non-finite price. Dividing a notional by zero is not a large
                order, and dividing by a negative price would invert the side.
        """
        price = as_decimal(self.price, field="sizing price")
        if not price.is_finite() or price <= Decimal(0):
            raise ExecutionError(
                Code.INVALID_EXECUTION_PRICE,
                f"{self.identity.label}: a sizing price must be positive and "
                f"finite, got {price}. A zero price yields no quantity and a "
                "negative one would invert the side of the trade.",
            )
        object.__setattr__(self, "price", quantise_price(price))

    def as_json(self) -> dict[str, Any]:
        """Return the price as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "price": str(self.price),
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True)
class ExecutionSizingSnapshot:
    """Everything the planner is allowed to know about prices and capital.

    Attributes:
        as_of: The moment it describes, ISO 8601 with offset.
        nav: Portfolio NAV, from accounting.
        cash: Cash, from accounting.
        prices: One sizing price per security.
        source: Where the snapshot came from.
    """

    as_of: str
    nav: Decimal
    cash: Decimal
    prices: tuple[SizingPrice, ...] = ()
    source: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        """Validate the snapshot.

        Raises:
            ExecutionError: `SIZING_SNAPSHOT_INCOMPLETE` on a missing instant, a
                non-positive NAV, or a duplicated security. Weights and notionals
                are fractions of NAV, so a non-positive NAV makes every quantity
                meaningless rather than merely small.
        """
        if not self.as_of:
            raise ExecutionError(
                Code.SIZING_SNAPSHOT_INCOMPLETE, "a sizing snapshot must state its instant"
            )
        nav = as_decimal(self.nav, field="nav")
        if nav <= Decimal(0):
            raise ExecutionError(
                Code.SIZING_SNAPSHOT_INCOMPLETE,
                f"NAV is {nav}; every notional delta is a fraction of NAV, so "
                "sizing against a non-positive NAV produces meaningless "
                "quantities rather than small ones",
            )
        seen: set[tuple[str, int]] = set()
        for row in self.prices:
            if row.identity.key in seen:
                raise ExecutionError(
                    Code.SIZING_SNAPSHOT_INCOMPLETE,
                    f"{row.identity.label} is priced twice; whichever was read "
                    "last would decide the quantity",
                )
            seen.add(row.identity.key)
        object.__setattr__(self, "nav", quantise_price(nav))
        object.__setattr__(self, "cash", quantise_price(as_decimal(self.cash, field="cash")))

    def price_for(self, identity: SecurityIdentity) -> SizingPrice:
        """The sizing price for one security.

        Args:
            identity: The security.

        Returns:
            Its price.

        Raises:
            ExecutionError: `INVALID_EXECUTION_PRICE` if absent. An unpriced
                security produces no order rather than an order at a guessed
                price.
        """
        for row in self.prices:
            if row.identity.key == identity.key:
                return row
        raise ExecutionError(
            Code.INVALID_EXECUTION_PRICE,
            f"{identity.label} has no sizing price at {self.as_of}, so no quantity "
            "can be derived. The planner does not fetch one: a price obtained "
            "inside planning would make the plan unreproducible from its inputs.",
        )

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "as_of": self.as_of,
            "nav": str(self.nav),
            "cash": str(self.cash),
            "source": self.source,
            "prices": [row.as_json() for row in sorted(self.prices, key=lambda r: r.identity)],
        }

    def fingerprint(self) -> str:
        """The snapshot's deterministic digest."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping."""
        return {**self.body(), "snapshot_fingerprint": self.fingerprint()}


def _quantity_for(notional_delta: Decimal, price: Decimal) -> Decimal:
    """Derive an order quantity from a notional delta and a sizing price.

    Args:
        notional_delta: The signed change in market value.
        price: The sizing price.

    Returns:
        The absolute quantity, quantised to the frozen 8-place policy. Fractional
        quantities are kept rather than floored: flooring a 0.4-share delta to
        zero would silently drop a rebalance, and rounding it to one would trade
        more than was authorised.
    """
    return quantise_quantity(abs(notional_delta) / price)


def plan_execution(
    *,
    execution_plan_id: str,
    as_of: str,
    intents: Sequence[Any],
    sizing: ExecutionSizingSnapshot,
    book: BookBinding,
    approved_portfolio_fingerprint: str,
    valued_snapshot_fingerprint: str,
    attribution: Mapping[str, Sequence[tuple[str, Decimal]]] | None = None,
    created_at: str | None = None,
) -> ExecutionPlan:
    """Build an authorised, sequenced execution plan.

    Pure: no network, no clock-dependent branch, no price lookup. The same inputs
    always produce the same plan and the same fingerprint, however the intents
    were ordered on the way in.

    Args:
        execution_plan_id: Stable identifier for the plan.
        as_of: The moment the plan is for.
        intents: `portfolio_risk.ExecutionIntent` values. A `HOLD` produces no
            order; so does an effectively zero delta.
        sizing: The explicit prices and capital to size against.
        book: Which accounting book this is authorised against. Must be V2.
        approved_portfolio_fingerprint: The targets being executed.
        valued_snapshot_fingerprint: What the portfolio was marked against.
        attribution: Optional `{identity label: [(strategy_id, quantity)]}`
            giving which strategies authorised which part of each order. Where
            absent, an order carries the intent's own `strategy_id` if it has one
            and no attribution otherwise — accounting then applies its own rules,
            including refusing an ambiguous sale.
        created_at: When the plan was created. Operational.

    Returns:
        The plan, with orders in execution sequence.

    Raises:
        ExecutionError: `INVALID_EXECUTION_PRICE` for an unpriced security with a
            non-zero delta; `PLAN_INVALID` for an action with no sequencing
            meaning; `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION` for a non-V2 book.
    """
    planned: list[tuple[str, str, Order]] = []
    for intent in intents:
        action = str(intent.action)
        if action == "HOLD":
            continue
        delta = as_decimal(intent.notional_delta, field="notional delta")
        if quantise_price(delta) == Decimal(0):
            # An authorised target that already matches the holding. Emitting a
            # zero-quantity order would put a no-op through the whole lifecycle
            # and into the journal, where it would look like a trade that did
            # nothing rather than a decision not to trade.
            continue
        group = sequence_group_of(action)
        price = sizing.price_for(intent.identity)
        quantity = _quantity_for(delta, price.price)
        if quantity <= Decimal(0):
            continue
        side = OrderSide.SELL if group == "RELEASE_CASH" else OrderSide.BUY
        shares = _shares_for(intent, quantity, attribution)
        order = Order(
            order_id=f"{execution_plan_id}-{intent.identity.label}-{side}",
            execution_plan_id=execution_plan_id,
            authorization_fingerprint="PENDING",
            identity=intent.identity,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            sizing_price=price.price,
            attribution=shares,
            created_at=created_at or "",
            symbol=intent.identity.symbol,
            sequence_group=group,
        )
        planned.append((group, intent.identity.label, order))

    # The frozen sequence: group first, then durable identity. Input order is
    # discarded entirely, which is what makes the plan's fingerprint a property
    # of the portfolio delta rather than of how a caller assembled the list.
    planned.sort(key=lambda row: (SEQUENCE_GROUPS.index(row[0]), row[1]))

    draft = ExecutionPlan(
        execution_plan_id=execution_plan_id,
        as_of=as_of,
        approved_portfolio_fingerprint=approved_portfolio_fingerprint,
        execution_intents_fingerprint=intents_fingerprint(intents),
        sizing_snapshot_fingerprint=sizing.fingerprint(),
        valued_snapshot_fingerprint=valued_snapshot_fingerprint,
        book=book,
        orders=tuple(order for _, _, order in planned),
        created_at=created_at,
    )
    # Each order binds the plan's fingerprint, so a broker can check an order
    # against the authorisation it claims. The plan is fingerprinted with the
    # placeholder in place and the orders are then stamped, which keeps the
    # digest a function of the plan's content rather than of itself.
    stamped = tuple(
        Order(
            order_id=order.order_id,
            execution_plan_id=order.execution_plan_id,
            authorization_fingerprint=draft.fingerprint(),
            identity=order.identity,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            sizing_price=order.sizing_price,
            attribution=order.attribution,
            created_at=order.created_at,
            symbol=order.symbol,
            sequence_group=order.sequence_group,
        )
        for order in draft.orders
    )
    return ExecutionPlan(
        execution_plan_id=draft.execution_plan_id,
        as_of=draft.as_of,
        approved_portfolio_fingerprint=draft.approved_portfolio_fingerprint,
        execution_intents_fingerprint=draft.execution_intents_fingerprint,
        sizing_snapshot_fingerprint=draft.sizing_snapshot_fingerprint,
        valued_snapshot_fingerprint=draft.valued_snapshot_fingerprint,
        book=draft.book,
        orders=stamped,
        created_at=draft.created_at,
    )


def _shares_for(
    intent: Any,
    quantity: Decimal,
    attribution: Mapping[str, Sequence[tuple[str, Decimal]]] | None,
) -> tuple[AttributionShare, ...]:
    """Decide which strategies authorised an order's quantity.

    Args:
        intent: The execution intent.
        quantity: The order quantity.
        attribution: Caller-supplied shares, if any.

    Returns:
        The shares, scaled to the order quantity and summing to it exactly.

    Raises:
        ExecutionError: `FILL_ATTRIBUTION_DOES_NOT_SUM` if supplied shares carry
            no quantity to apportion by.
    """
    supplied = (attribution or {}).get(intent.identity.label)
    if supplied:
        weights = [
            (name, as_decimal(value, field="attribution weight")) for name, value in supplied
        ]
        total = sum((value for _, value in weights), Decimal(0))
        if total <= Decimal(0):
            raise ExecutionError(
                Code.FILL_ATTRIBUTION_DOES_NOT_SUM,
                f"{intent.identity.label}: the supplied attribution carries no "
                "quantity to apportion the order across",
            )
        ordered = sorted(weights, key=lambda row: row[0])
        shares: list[AttributionShare] = []
        running = Decimal(0)
        for index, (name, weight) in enumerate(ordered):
            if index == len(ordered) - 1:
                part = quantise_quantity(quantity - running)
            else:
                part = quantise_quantity(quantity * weight / total)
                running = quantise_quantity(running + part)
            if part > Decimal(0):
                shares.append(AttributionShare(strategy_id=name, quantity=part))
        return tuple(shares)
    if intent.strategy_id:
        return (AttributionShare(strategy_id=intent.strategy_id, quantity=quantity),)
    return ()
