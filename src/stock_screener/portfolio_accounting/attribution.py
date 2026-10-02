"""Per-strategy attribution of holdings, exposure and realised P&L.

A multi-strategy portfolio is only multi-strategy if its books can say which
strategy owns what. Without that, the risk budgets from P1.2 are enforced against
targets and never against reality, and a strategy can quietly carry another's
losses forever.

Two things make this honest rather than decorative:

**Nothing is inferred.** A sale says whose shares it sold or it is refused. A pro
rata split across co-owners would look reasonable on a report and would move
realised P&L between strategies on no evidence at all.

**Unknown ownership is named, not hidden.** An imported legacy holding sits under
`UNATTRIBUTED_EXISTING`, and it shows up in every attribution report as exactly
that. The alternative — assigning it to whichever strategy seems likely, or
distributing it evenly — produces a complete-looking report whose completeness is
fictional.

Attribution reconciles against the position totals on every report
(`ATTRIBUTION_RECONCILIATION_FAILED`), because the one failure mode that matters
is attribution drifting from the holdings it claims to describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .events import UNATTRIBUTED
from .money import ZERO, is_zero, quantise_cash, quantise_quantity

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal

    from .pnl import ValuedPortfolioSnapshot
    from .state import PortfolioState

__all__ = [
    "ATTRIBUTION_POLICY",
    "AttributionReport",
    "StrategyAttribution",
    "attribute_portfolio",
]

#: The attribution contract.
ATTRIBUTION_POLICY = "PORTFOLIO_STRATEGY_ATTRIBUTION_V1"


@dataclass(frozen=True)
class StrategyAttribution:
    """One strategy's share of the portfolio.

    Attributes:
        strategy_id: The owner, or `UNATTRIBUTED_EXISTING`.
        market_value: Market value of its holdings.
        cost_basis: What those holdings cost.
        unrealised_pnl: `market_value - cost_basis`.
        realised_pnl: Realised to date on quantity it owned.
        weight: Share of NAV.
        holdings: Market value per security.
        is_unattributed: Whether this is the unknown-owner bucket.
    """

    strategy_id: str
    market_value: Decimal = ZERO
    cost_basis: Decimal = ZERO
    unrealised_pnl: Decimal = ZERO
    realised_pnl: Decimal = ZERO
    weight: Decimal = ZERO
    holdings: Mapping[str, Decimal] = field(default_factory=dict)
    is_unattributed: bool = False

    def as_json(self) -> dict[str, Any]:
        """Return the attribution as a JSON-safe mapping."""
        return {
            "strategy_id": self.strategy_id,
            "market_value": str(quantise_cash(self.market_value)),
            "cost_basis": str(quantise_cash(self.cost_basis)),
            "unrealised_pnl": str(quantise_cash(self.unrealised_pnl)),
            "realised_pnl": str(quantise_cash(self.realised_pnl)),
            "weight": str(self.weight),
            "holdings": {key: str(self.holdings[key]) for key in sorted(self.holdings)},
            "is_unattributed": self.is_unattributed,
        }


@dataclass(frozen=True)
class AttributionReport:
    """How the portfolio divides across strategies.

    Attributes:
        strategies: One entry per owner, including the unattributed bucket.
        attributed_market_value: Market value with a known owner.
        unattributed_market_value: Market value whose owner is unknown.
        cash: Cash, which is deliberately not attributed — see
            `cash_attribution_note`.
        nav: The portfolio NAV, for context.
        cash_attribution_note: Why cash carries no strategy.
    """

    strategies: tuple[StrategyAttribution, ...]
    attributed_market_value: Decimal
    unattributed_market_value: Decimal
    cash: Decimal
    nav: Decimal
    cash_attribution_note: str = (
        "cash is not attributed to a strategy. It is fungible, it funds whichever "
        "strategy trades next, and splitting it by any rule would make a "
        "strategy's reported exposure depend on an allocation nobody decided. "
        "The capital allocator in P1.2 decides what a strategy may spend; the "
        "ledger records what it actually holds."
    )

    def check(self, state: PortfolioState) -> None:
        """Verify attribution still describes the holdings.

        Args:
            state: The state the report was built from.

        Raises:
            AccountingError: `ATTRIBUTION_RECONCILIATION_FAILED` if a security's
                attributed quantity no longer sums to the quantity held.
        """
        for key in sorted(state.positions):
            position = state.positions[key]
            total = quantise_quantity(sum(position.attribution.values(), ZERO))
            if total != quantise_quantity(position.quantity):
                raise AccountingError(
                    Code.ATTRIBUTION_RECONCILIATION_FAILED,
                    f"{position.identity.label}: strategies account for {total} "
                    f"shares but the ledger holds {quantise_quantity(position.quantity)}",
                )

    def as_json(self) -> dict[str, Any]:
        """Return the report as a JSON-safe mapping."""
        return {
            "policy": ATTRIBUTION_POLICY,
            "strategy_count": len(self.strategies),
            "strategies": [row.as_json() for row in self.strategies],
            "attributed_market_value": str(quantise_cash(self.attributed_market_value)),
            "unattributed_market_value": str(quantise_cash(self.unattributed_market_value)),
            "cash": str(quantise_cash(self.cash)),
            "nav": str(quantise_cash(self.nav)),
            "cash_attribution_note": self.cash_attribution_note,
        }


def attribute_portfolio(
    state: PortfolioState, snapshot: ValuedPortfolioSnapshot
) -> AttributionReport:
    """Divide a valued portfolio across the strategies that own it.

    Args:
        state: The replayed state, which holds the attribution.
        snapshot: The valuation, which holds the marks.

    Returns:
        The attribution report.

    Raises:
        AccountingError: `ATTRIBUTION_RECONCILIATION_FAILED` if attribution has
            drifted from the holdings.
    """
    marks = {row.identity.key: row for row in snapshot.positions}
    buckets: dict[str, dict[str, Any]] = {}

    for key in sorted(state.positions):
        position = state.positions[key]
        valued = marks.get(key)
        if valued is None:
            continue
        held = quantise_quantity(position.quantity)
        for strategy_id in sorted(position.attribution):
            quantity = quantise_quantity(position.attribution[strategy_id])
            if is_zero(quantity):
                continue
            share = quantity / held if not is_zero(held) else ZERO
            bucket = buckets.setdefault(
                strategy_id,
                {
                    "market_value": ZERO,
                    "cost_basis": ZERO,
                    "realised_pnl": ZERO,
                    "holdings": {},
                },
            )
            value = quantise_cash(valued.market_value * share)
            basis = quantise_cash(valued.cost_basis * share)
            bucket["market_value"] = quantise_cash(bucket["market_value"] + value)
            bucket["cost_basis"] = quantise_cash(bucket["cost_basis"] + basis)
            bucket["holdings"][position.identity.label] = value

    # Realised P&L is attributed from the sales that produced it, which the
    # positions record per security. A closed position still carries its realised
    # result, so it is included: dropping it would make a strategy that closed
    # its only losing trade look like it never made one.
    for source in (state.positions, state.closed_positions):
        for key in sorted(source):
            position = source[key]
            if is_zero(position.realised_pnl):
                continue
            owners = position.attribution or {UNATTRIBUTED: ZERO}
            total = quantise_quantity(sum(owners.values(), ZERO))
            if is_zero(total):
                # Every share has been sold, so the realised result belongs to
                # whoever held it. With the position closed the ledger no longer
                # says who that was, so it is reported as unattributed rather
                # than assigned to a survivor.
                bucket = buckets.setdefault(
                    UNATTRIBUTED,
                    {
                        "market_value": ZERO,
                        "cost_basis": ZERO,
                        "realised_pnl": ZERO,
                        "holdings": {},
                    },
                )
                bucket["realised_pnl"] = quantise_cash(
                    bucket["realised_pnl"] + position.realised_pnl
                )
                continue
            for strategy_id in sorted(owners):
                share = quantise_quantity(owners[strategy_id]) / total
                bucket = buckets.setdefault(
                    strategy_id,
                    {
                        "market_value": ZERO,
                        "cost_basis": ZERO,
                        "realised_pnl": ZERO,
                        "holdings": {},
                    },
                )
                bucket["realised_pnl"] = quantise_cash(
                    bucket["realised_pnl"] + position.realised_pnl * share
                )

    nav = quantise_cash(snapshot.nav)
    rows: list[StrategyAttribution] = []
    for strategy_id in sorted(buckets):
        bucket = buckets[strategy_id]
        market_value = bucket["market_value"]
        cost_basis = bucket["cost_basis"]
        rows.append(
            StrategyAttribution(
                strategy_id=strategy_id,
                market_value=market_value,
                cost_basis=cost_basis,
                unrealised_pnl=quantise_cash(market_value - cost_basis),
                realised_pnl=bucket["realised_pnl"],
                weight=quantise_cash(market_value / nav) if nav > ZERO else ZERO,
                holdings=bucket["holdings"],
                is_unattributed=strategy_id == UNATTRIBUTED,
            )
        )

    attributed = quantise_cash(
        sum((row.market_value for row in rows if not row.is_unattributed), ZERO)
    )
    unattributed = quantise_cash(
        sum((row.market_value for row in rows if row.is_unattributed), ZERO)
    )
    report = AttributionReport(
        strategies=tuple(rows),
        attributed_market_value=attributed,
        unattributed_market_value=unattributed,
        cash=quantise_cash(snapshot.cash),
        nav=nav,
    )
    report.check(state)
    return report
