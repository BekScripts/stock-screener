"""What execution cost, decomposed exactly — and posted to nothing.

Three costs are reported, measured against the midpoint of the quote the fill
traded against:

    spread cost     (touch - mid) * quantity     crossing the quote
    slippage cost   (fill - touch) * quantity    the declared adverse haircut
    shortfall       (fill - mid) * quantity      both of the above

and the identity `shortfall == spread + slippage` is **enforced**, not asserted
in a docstring. It holds by construction — the two parts telescope through the
touch price — so a failure means a quantisation or sign bug, and checking it on
every report is how such a bug is found at the point it happens rather than in
an unexplained total weeks later.

**None of this is posted to the book.** The accounting ledger already received
the actual fill price and the actual fee, so the economic effect is *already*
in cash, cost basis and realised P&L. Posting a spread or slippage expense
alongside would charge it twice — once implicitly in the price paid and once
explicitly as an expense — and the resulting NAV would be wrong by the size of
the cost being reported. These figures exist to *explain* the prices the book
already holds, not to adjust them.

The distinction is worth being blunt about because the opposite instinct is
natural: a cost report that does not appear in the P&L feels incomplete. It
appears there already, inside the price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import is_zero, quantise_price, quantise_quantity

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .models import OrderSide
from .versions import EXECUTION_ENGINE_VERSION_V1_1, PAPER_SESSION_FILL_MODEL

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "COST_REPORT_CONTRACT",
    "ExecutionCostReport",
    "IdentityCost",
    "SessionFillCost",
    "build_cost_report",
    "cost_of_fill",
]

#: The cost-report contract.
COST_REPORT_CONTRACT = "EXECUTION_COST_REPORT_V1"

#: What is reported when a session traded no shares at all, so participation has
#: no denominator. Reported as a word rather than as zero, because zero
#: participation of zero volume is a different statement from taking none of a
#: liquid session.
UNDEFINED = "UNDEFINED"


@dataclass(frozen=True)
class SessionFillCost:
    """What one fill, in one session, cost against that session's quote.

    Attributes:
        identity: The security.
        session: The session.
        side: Buy or sell.
        bid: The best bid at the execution opportunity.
        ask: The best ask.
        mid: The midpoint — the cost reference, never a fill price.
        observed_share_volume: Shares traded in the session.
        participation_ceiling: What the policy permitted this system to take.
        filled_quantity: What actually filled.
        actual_participation: `filled / observed volume`, or `UNDEFINED` when the
            session traded nothing.
        fill_price: What it filled at.
        spread_cost: The cost of crossing the quote.
        slippage_cost: The declared adverse haircut past the touch.
        implementation_shortfall: `spread + slippage`, enforced.
        fee: The fee charged on this fill.
        fill_id: Which fill this describes.
    """

    identity: SecurityIdentity
    session: str
    side: OrderSide
    bid: Decimal
    ask: Decimal
    mid: Decimal
    observed_share_volume: Decimal | None
    participation_ceiling: Decimal | None
    filled_quantity: Decimal
    fill_price: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    implementation_shortfall: Decimal
    fee: Decimal
    fill_id: str

    def check(self) -> None:
        """Verify the decomposition.

        Raises:
            ExecutionError: `EXECUTION_COST_DECOMPOSITION_VIOLATED` if the parts
                do not sum to the shortfall. It holds by construction, so a
                failure is a quantisation or sign bug and is worth catching where
                it happens.
        """
        implied = quantise_price(self.spread_cost + self.slippage_cost)
        if implied != quantise_price(self.implementation_shortfall):
            raise ExecutionError(
                Code.EXECUTION_COST_DECOMPOSITION_VIOLATED,
                f"{self.fill_id}: spread {self.spread_cost} plus slippage "
                f"{self.slippage_cost} is {implied} but the shortfall is "
                f"{quantise_price(self.implementation_shortfall)}. The two parts "
                "telescope through the touch price, so they cannot legitimately "
                "disagree.",
            )

    @property
    def actual_participation(self) -> str:
        """The share of the session's volume this fill took.

        `UNDEFINED` when the session traded nothing: there is no denominator, and
        reporting zero would read as restraint rather than as an absent market.
        """
        if self.observed_share_volume is None or is_zero(self.observed_share_volume):
            return UNDEFINED
        return str(quantise_quantity(self.filled_quantity / self.observed_share_volume))

    @property
    def total_cost(self) -> Decimal:
        """`shortfall + fee`."""
        return quantise_price(self.implementation_shortfall + self.fee)

    def as_json(self) -> dict[str, Any]:
        """Return the cost as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "session": self.session,
            "side": str(self.side),
            "bid": str(self.bid),
            "ask": str(self.ask),
            "mid": str(self.mid),
            "observed_share_volume": (
                None if self.observed_share_volume is None else str(self.observed_share_volume)
            ),
            "participation_ceiling": (
                None if self.participation_ceiling is None else str(self.participation_ceiling)
            ),
            "filled_quantity": str(self.filled_quantity),
            "actual_participation": self.actual_participation,
            "fill_price": str(self.fill_price),
            "spread_cost": str(quantise_price(self.spread_cost)),
            "slippage_cost": str(quantise_price(self.slippage_cost)),
            "implementation_shortfall": str(quantise_price(self.implementation_shortfall)),
            "fee": str(quantise_price(self.fee)),
            "total_execution_cost": str(self.total_cost),
            "fill_id": self.fill_id,
        }


def cost_of_fill(
    *,
    identity: SecurityIdentity,
    session: str,
    side: OrderSide,
    bid: Decimal,
    ask: Decimal,
    filled_quantity: Decimal,
    fill_price: Decimal,
    fee: Decimal,
    fill_id: str,
    observed_share_volume: Decimal | None = None,
    participation_ceiling: Decimal | None = None,
) -> SessionFillCost:
    """Decompose one fill's cost against its session's quote.

    Both costs are signed so that a positive figure is money lost. A buyer paying
    above the mid and a seller receiving below it both produce positive costs,
    which is what makes the totals addable across sides.

    Args:
        identity: The security.
        session: The session.
        side: Buy or sell.
        bid: The best bid.
        ask: The best ask.
        filled_quantity: What filled.
        fill_price: What it filled at.
        fee: The fee charged.
        fill_id: Which fill.
        observed_share_volume: Shares traded in the session.
        participation_ceiling: What the policy permitted.

    Returns:
        The decomposed cost, with its identity already verified.
    """
    mid = quantise_price((bid + ask) / Decimal(2))
    touch = ask if side is OrderSide.BUY else bid
    if side is OrderSide.BUY:
        spread = quantise_price((touch - mid) * filled_quantity)
        slippage = quantise_price((fill_price - touch) * filled_quantity)
        shortfall = quantise_price((fill_price - mid) * filled_quantity)
    else:
        spread = quantise_price((mid - touch) * filled_quantity)
        slippage = quantise_price((touch - fill_price) * filled_quantity)
        shortfall = quantise_price((mid - fill_price) * filled_quantity)
    cost = SessionFillCost(
        identity=identity,
        session=session,
        side=side,
        bid=bid,
        ask=ask,
        mid=mid,
        observed_share_volume=observed_share_volume,
        participation_ceiling=participation_ceiling,
        filled_quantity=quantise_quantity(filled_quantity),
        fill_price=fill_price,
        spread_cost=spread,
        slippage_cost=slippage,
        implementation_shortfall=shortfall,
        fee=quantise_price(fee),
        fill_id=fill_id,
    )
    cost.check()
    return cost


@dataclass(frozen=True)
class IdentityCost:
    """What one security cost to trade, across every session and child order.

    Attributes:
        identity: The security.
        planned_quantity: What the plan authorised.
        filled_quantity: What actually filled.
        remaining_quantity: What never did.
        sessions_used: How many sessions it took.
        vwap_fill_price: The volume-weighted average fill price.
        spread_cost: Summed across fills.
        slippage_cost: Summed across fills.
        fees: Summed across fills.
        implementation_shortfall: Summed across fills.
    """

    identity: SecurityIdentity
    planned_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    sessions_used: int
    vwap_fill_price: Decimal | None
    spread_cost: Decimal
    slippage_cost: Decimal
    fees: Decimal
    implementation_shortfall: Decimal

    @property
    def total_cost(self) -> Decimal:
        """`shortfall + fees`."""
        return quantise_price(self.implementation_shortfall + self.fees)

    def as_json(self) -> dict[str, Any]:
        """Return the cost as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "planned_quantity": str(self.planned_quantity),
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "sessions_used": self.sessions_used,
            "vwap_fill_price": (
                None if self.vwap_fill_price is None else str(self.vwap_fill_price)
            ),
            "spread_cost": str(quantise_price(self.spread_cost)),
            "slippage_cost": str(quantise_price(self.slippage_cost)),
            "fees": str(quantise_price(self.fees)),
            "implementation_shortfall": str(quantise_price(self.implementation_shortfall)),
            "total_execution_cost": str(self.total_cost),
        }


@dataclass(frozen=True)
class ExecutionCostReport:
    """What a whole plan cost to execute. Posted to nothing.

    Attributes:
        execution_plan_id: Which plan.
        policy_fingerprint: The friction it ran under.
        session_costs: One entry per fill.
        identity_costs: One entry per security.
        sessions: Every session used, in order.
    """

    execution_plan_id: str
    policy_fingerprint: str
    session_costs: tuple[SessionFillCost, ...] = ()
    identity_costs: tuple[IdentityCost, ...] = ()
    sessions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def spread_cost(self) -> Decimal:
        """Spread cost across every fill."""
        return quantise_price(sum((row.spread_cost for row in self.session_costs), Decimal(0)))

    @property
    def slippage_cost(self) -> Decimal:
        """Slippage cost across every fill."""
        return quantise_price(sum((row.slippage_cost for row in self.session_costs), Decimal(0)))

    @property
    def fees(self) -> Decimal:
        """Fees across every fill."""
        return quantise_price(sum((row.fee for row in self.session_costs), Decimal(0)))

    @property
    def implementation_shortfall(self) -> Decimal:
        """Shortfall across every fill."""
        return quantise_price(
            sum((row.implementation_shortfall for row in self.session_costs), Decimal(0))
        )

    @property
    def total_execution_cost(self) -> Decimal:
        """`shortfall + fees`, which is also `spread + slippage + fees`."""
        return quantise_price(self.implementation_shortfall + self.fees)

    def check(self) -> None:
        """Verify the plan-level decomposition.

        Raises:
            ExecutionError: `EXECUTION_COST_DECOMPOSITION_VIOLATED` if the
                summed parts do not reach the summed shortfall, or if the two
                equivalent routes to the total disagree.
        """
        for row in self.session_costs:
            row.check()
        implied = quantise_price(self.spread_cost + self.slippage_cost)
        if implied != self.implementation_shortfall:
            raise ExecutionError(
                Code.EXECUTION_COST_DECOMPOSITION_VIOLATED,
                f"{self.execution_plan_id}: spread plus slippage is {implied} but "
                f"the shortfall sums to {self.implementation_shortfall}",
            )
        route_a = quantise_price(self.implementation_shortfall + self.fees)
        route_b = quantise_price(self.spread_cost + self.slippage_cost + self.fees)
        if route_a != route_b:
            raise ExecutionError(
                Code.EXECUTION_COST_DECOMPOSITION_VIOLATED,
                f"{self.execution_plan_id}: the total is {route_a} by shortfall and "
                f"{route_b} by components; one of them double-counts",
            )

    def fingerprint(self) -> str:
        """The report's deterministic digest."""
        return fingerprint_of(self.body())

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "contract": COST_REPORT_CONTRACT,
            "engine_version": EXECUTION_ENGINE_VERSION_V1_1,
            "fill_model": PAPER_SESSION_FILL_MODEL,
            "execution_plan_id": self.execution_plan_id,
            "policy_fingerprint": self.policy_fingerprint,
            "sessions": list(self.sessions),
            "per_session": [row.as_json() for row in self.session_costs],
            "per_identity": [row.as_json() for row in self.identity_costs],
            "totals": {
                "spread_cost": str(self.spread_cost),
                "slippage_cost": str(self.slippage_cost),
                "fees": str(self.fees),
                "implementation_shortfall": str(self.implementation_shortfall),
                "total_execution_cost": str(self.total_execution_cost),
            },
        }

    def as_json(self) -> dict[str, Any]:
        """Return the report as a JSON-safe mapping."""
        return {
            **self.body(),
            "report_fingerprint": self.fingerprint(),
            "identities": [
                "implementation_shortfall = spread_cost + slippage_cost",
                "total_execution_cost = implementation_shortfall + fees",
                "total_execution_cost = spread_cost + slippage_cost + fees",
            ],
            "accounting_effect": "NONE",
            "accounting_note": (
                "the book already received the actual fill price and the actual "
                "fee, so this cost is already inside cash, cost basis and realised "
                "P&L. Posting a spread or slippage expense alongside would charge "
                "it twice and make NAV wrong by exactly the amount reported here. "
                "These figures explain the prices the book holds; they do not "
                "adjust them."
            ),
        }


def build_cost_report(
    *,
    execution_plan_id: str,
    policy_fingerprint: str,
    session_costs: Sequence[SessionFillCost],
    planned: dict[tuple[str, int], tuple[SecurityIdentity, Decimal]],
) -> ExecutionCostReport:
    """Aggregate per-fill costs into per-identity and plan totals.

    Args:
        execution_plan_id: Which plan.
        policy_fingerprint: The friction it ran under.
        session_costs: One entry per fill.
        planned: `{identity key: (identity, planned quantity)}` from the plan.

    Returns:
        The report, with every identity verified.
    """
    by_identity: dict[tuple[str, int], list[SessionFillCost]] = {}
    for row in session_costs:
        by_identity.setdefault(row.identity.key, []).append(row)

    identity_costs: list[IdentityCost] = []
    for key in sorted(set(planned) | set(by_identity)):
        rows = by_identity.get(key, [])
        identity, planned_quantity = planned.get(
            key, (rows[0].identity if rows else None, Decimal(0))
        )
        if identity is None:  # pragma: no cover - planned always carries one
            continue
        filled = quantise_quantity(sum((row.filled_quantity for row in rows), Decimal(0)))
        notional = quantise_price(
            sum((row.filled_quantity * row.fill_price for row in rows), Decimal(0))
        )
        identity_costs.append(
            IdentityCost(
                identity=identity,
                planned_quantity=quantise_quantity(planned_quantity),
                filled_quantity=filled,
                remaining_quantity=quantise_quantity(planned_quantity - filled),
                sessions_used=len({row.session for row in rows}),
                vwap_fill_price=(None if is_zero(filled) else quantise_price(notional / filled)),
                spread_cost=quantise_price(sum((row.spread_cost for row in rows), Decimal(0))),
                slippage_cost=quantise_price(sum((row.slippage_cost for row in rows), Decimal(0))),
                fees=quantise_price(sum((row.fee for row in rows), Decimal(0))),
                implementation_shortfall=quantise_price(
                    sum((row.implementation_shortfall for row in rows), Decimal(0))
                ),
            )
        )

    report = ExecutionCostReport(
        execution_plan_id=execution_plan_id,
        policy_fingerprint=policy_fingerprint,
        session_costs=tuple(session_costs),
        identity_costs=tuple(identity_costs),
        sessions=tuple(sorted({row.session for row in session_costs})),
    )
    report.check()
    return report
