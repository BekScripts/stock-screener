"""NAV, the P&L decomposition, and the identities that make both checkable.

Two identities are enforced here, and neither is decoration.

**The NAV identity.** `NAV = cash + market value of holdings`. Checking it
catches the whole class of bug where a position was valued but its cash leg was
not, or a fee left cash without leaving the summary.

**The P&L decomposition.** `total P&L = realised + unrealised + income - fees`
with `realised` gross of fees, and separately
`NAV change = P&L + net external flows - the cost of anything no mark could
price`, where that last term is zero for the complete valuations this engine
defaults to requiring. The second is the one
that matters commercially: without it, a deposit arriving on a bad day looks like
a good day. `PNL_DECOMPOSITION_VIOLATED` fires when the parts do not sum to the
whole, which means a component was double-counted or lost.

Both are computed in `Decimal` and compared exactly. There is no tolerance,
because every figure entering them was quantised by `money`, so a residue is a
real error rather than floating-point noise. That is the entire reason the
accounting layer does not use floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .money import ZERO, quantise_cash
from .positions import COST_BASIS_POLICY
from .valuation import MARK_POLICY_ID, MarkPriceSnapshot, ValuationCoverage, value_positions

if TYPE_CHECKING:
    from decimal import Decimal

    from stock_screener.portfolio_risk import SecurityIdentity

    from .state import PortfolioState

__all__ = [
    "PNL_POLICY_ID",
    "PnlDecomposition",
    "PositionValuation",
    "ValuedPortfolioSnapshot",
    "value_portfolio",
]

#: The P&L contract.
PNL_POLICY_ID = "PORTFOLIO_PNL_DECOMPOSITION_V1"


@dataclass(frozen=True)
class PositionValuation:
    """One holding, valued.

    Attributes:
        identity: The security.
        quantity: Shares held.
        average_cost: Cost per share.
        mark_price: The price used.
        mark_as_of: When that price was good as of.
        market_value: `quantity * mark_price`.
        cost_basis: `quantity * average_cost`, as the ledger holds it.
        unrealised_pnl: `market_value - cost_basis`.
        realised_pnl: Realised to date on this security.
        weight: Share of NAV, for reporting only.
        is_stale: Whether the mark is older than the staleness bound.
    """

    identity: SecurityIdentity
    quantity: Decimal
    average_cost: Decimal
    mark_price: Decimal
    mark_as_of: str
    market_value: Decimal
    cost_basis: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    weight: Decimal
    is_stale: bool

    def as_json(self) -> dict[str, Any]:
        """Return the valuation as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "label": self.identity.label,
            "quantity": str(self.quantity),
            "average_cost": str(self.average_cost),
            "mark_price": str(self.mark_price),
            "mark_as_of": self.mark_as_of,
            "market_value": str(self.market_value),
            "cost_basis": str(self.cost_basis),
            "unrealised_pnl": str(self.unrealised_pnl),
            "realised_pnl": str(self.realised_pnl),
            "weight": str(self.weight),
            "is_stale": self.is_stale,
        }


@dataclass(frozen=True)
class PnlDecomposition:
    """Performance, split into the parts that explain it.

    Attributes:
        realised: Trading gains and losses on closed quantity, gross of fees.
        unrealised: Mark-to-market on open holdings.
        income: Dividends received.
        fees: Transaction costs paid, on purchases as well as sales. Its own
            term, so a strategy whose edge is eaten by costs is visible rather
            than merely unprofitable.
        total: `realised + unrealised + income - fees`.
        external_flow_net: Net deposits. In NAV, never in P&L.
        unpriced_cost_basis: What the holdings no mark could price originally
            cost. Zero for a complete valuation. It appears as its own term
            because an unpriced holding's cost is inside the inception basis
            while its value is absent from NAV, so the identity cannot close
            without naming the gap — and naming it is better than either
            suppressing the check or reporting a NAV that quietly treats the
            holding as worthless.
        nav_change_since_inception: `total + external_flow_net`, which must equal
            `NAV - inception NAV at cost`. That equality is checked against the
            replayed state rather than asserted, because it is the one identity
            that catches a handler moving cash without a matching P&L term.
    """

    realised: Decimal = ZERO
    unrealised: Decimal = ZERO
    income: Decimal = ZERO
    fees: Decimal = ZERO
    total: Decimal = ZERO
    external_flow_net: Decimal = ZERO
    unpriced_cost_basis: Decimal = ZERO
    nav_change_since_inception: Decimal = ZERO

    def check(self) -> None:
        """Verify the decomposition sums.

        Raises:
            AccountingError: `PNL_DECOMPOSITION_VIOLATED` if the components do
                not sum to the total, or if the total plus external flows does
                not equal the NAV change.
        """
        implied = quantise_cash(self.realised + self.unrealised + self.income - self.fees)
        if implied != quantise_cash(self.total):
            raise AccountingError(
                Code.PNL_DECOMPOSITION_VIOLATED,
                f"P&L components sum to {implied} but the total is "
                f"{quantise_cash(self.total)}: a component was double-counted or lost",
            )
        nav_implied = quantise_cash(self.total + self.external_flow_net - self.unpriced_cost_basis)
        if nav_implied != quantise_cash(self.nav_change_since_inception):
            raise AccountingError(
                Code.PNL_DECOMPOSITION_VIOLATED,
                f"P&L plus external flows less unpriced cost is {nav_implied} but "
                f"the NAV change is "
                f"{quantise_cash(self.nav_change_since_inception)}: either a deposit "
                "leaked into performance or performance leaked out of NAV",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the decomposition as a JSON-safe mapping."""
        return {
            "policy": PNL_POLICY_ID,
            "realised": str(quantise_cash(self.realised)),
            "unrealised": str(quantise_cash(self.unrealised)),
            "income": str(quantise_cash(self.income)),
            "fees": str(quantise_cash(self.fees)),
            "total": str(quantise_cash(self.total)),
            "external_flow_net": str(quantise_cash(self.external_flow_net)),
            "unpriced_cost_basis": str(quantise_cash(self.unpriced_cost_basis)),
            "nav_change_since_inception": str(quantise_cash(self.nav_change_since_inception)),
            "identity": "total = realised + unrealised + income - fees",
            "note": (
                "realised is gross of fees and fees are subtracted once, so a "
                "purchase commission is charged exactly as a sale commission is; "
                "external flows change NAV and never P&L"
            ),
        }


@dataclass(frozen=True)
class ValuedPortfolioSnapshot:
    """The portfolio, valued at one instant, with its own provenance.

    This is the object anything downstream should consume. It binds the state to
    the ledger that produced it and the marks that valued it, so a NAV can always
    be traced to both — a figure that cannot name its marks is not auditable.

    Attributes:
        valuation_time: The instant valued.
        cash: The cash balance.
        market_value: Total market value of holdings.
        nav: `cash + market_value`.
        inception_nav_at_cost: Opening cash plus imported cost basis.
        positions: Each holding, valued.
        pnl: The decomposition.
        coverage: What could and could not be priced.
        ledger_sequence: The last ledger record included.
        ledger_head_hash: That record's chain hash.
        gross_exposure: Market value over NAV.
        stale_marks: Labels of holdings priced by a stale mark.
    """

    valuation_time: str
    cash: Decimal
    market_value: Decimal
    nav: Decimal
    positions: tuple[PositionValuation, ...]
    pnl: PnlDecomposition
    coverage: ValuationCoverage
    ledger_sequence: int
    ledger_head_hash: str
    inception_nav_at_cost: Decimal = ZERO
    gross_exposure: Decimal = ZERO
    stale_marks: tuple[str, ...] = field(default_factory=tuple)

    def check_nav_identity(self) -> None:
        """Verify NAV equals cash plus holdings.

        Raises:
            AccountingError: `NAV_IDENTITY_VIOLATED` if it does not, or if the
                positions' market values do not sum to the total.
        """
        implied = quantise_cash(self.cash + self.market_value)
        if implied != quantise_cash(self.nav):
            raise AccountingError(
                Code.NAV_IDENTITY_VIOLATED,
                f"NAV is {quantise_cash(self.nav)} but cash plus holdings is {implied}",
            )
        summed = quantise_cash(sum((row.market_value for row in self.positions), ZERO))
        if summed != quantise_cash(self.market_value):
            raise AccountingError(
                Code.NAV_IDENTITY_VIOLATED,
                f"the positions value to {summed} but the total market value is "
                f"{quantise_cash(self.market_value)}",
            )

    def check_nav_change_identity(self) -> None:
        """Verify performance explains the whole move in NAV.

        `NAV - inception NAV at cost == total P&L + net external flows`. This is
        the strongest check in the package, because it is the only one that
        compares the P&L story against the balance sheet rather than against
        itself: any handler that moved cash without recording a matching P&L
        term fails here, however internally consistent its own figures look.

        Raises:
            AccountingError: `PNL_DECOMPOSITION_VIOLATED` if the two sides differ.
        """
        observed = quantise_cash(self.nav - self.inception_nav_at_cost)
        explained = quantise_cash(
            self.pnl.total + self.pnl.external_flow_net - self.pnl.unpriced_cost_basis
        )
        if observed != explained:
            raise AccountingError(
                Code.PNL_DECOMPOSITION_VIOLATED,
                f"NAV moved {observed} from its inception cost basis "
                f"({quantise_cash(self.inception_nav_at_cost)} to "
                f"{quantise_cash(self.nav)}) but P&L and external flows explain only "
                f"{explained}. The difference of "
                f"{quantise_cash(observed - explained)} is a change to the portfolio "
                "that no recorded component accounts for.",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping."""
        return {
            "valuation_time": self.valuation_time,
            "mark_policy": MARK_POLICY_ID,
            "cost_basis_policy": COST_BASIS_POLICY,
            "cash": str(quantise_cash(self.cash)),
            "market_value": str(quantise_cash(self.market_value)),
            "nav": str(quantise_cash(self.nav)),
            "inception_nav_at_cost": str(quantise_cash(self.inception_nav_at_cost)),
            "gross_exposure": str(self.gross_exposure),
            "position_count": len(self.positions),
            "positions": [row.as_json() for row in self.positions],
            "pnl": self.pnl.as_json(),
            "coverage": self.coverage.as_json(),
            "stale_marks": list(self.stale_marks),
            "ledger_sequence": self.ledger_sequence,
            "ledger_head_hash": self.ledger_head_hash,
        }


def value_portfolio(
    state: PortfolioState,
    snapshot: MarkPriceSnapshot,
    *,
    require_complete: bool = True,
) -> ValuedPortfolioSnapshot:
    """Value a portfolio at an instant and check both identities.

    Args:
        state: The state to value, derived by replaying a ledger.
        snapshot: The marks.
        require_complete: Refuse a partial valuation.

    Returns:
        The valued snapshot.

    Raises:
        AccountingError: `VALUATION_INCOMPLETE` if a holding cannot be priced;
            `NAV_IDENTITY_VIOLATED` or `PNL_DECOMPOSITION_VIOLATED` if an
            identity does not hold, which would mean a bug in this layer rather
            than bad input — and is checked on every valuation for that reason.
    """
    coverage = value_positions(state, snapshot, require_complete=require_complete)
    cash = quantise_cash(state.cash)
    nav = quantise_cash(cash + coverage.market_value)
    stale = set(coverage.stale)

    rows: list[PositionValuation] = []
    unrealised = ZERO
    for key in sorted(coverage.valued):
        position = state.positions[key]
        mark = snapshot.mark_for(position.identity)
        assert mark is not None  # noqa: S101 - established by value_positions
        market_value = coverage.valued[key]
        cost_basis = quantise_cash(position.total_cost)
        row_unrealised = position.unrealised_pnl(mark.price)
        unrealised = quantise_cash(unrealised + row_unrealised)
        rows.append(
            PositionValuation(
                identity=position.identity,
                quantity=position.quantity,
                average_cost=position.average_cost,
                mark_price=mark.price,
                mark_as_of=mark.as_of,
                market_value=market_value,
                cost_basis=cost_basis,
                unrealised_pnl=row_unrealised,
                realised_pnl=quantise_cash(position.realised_pnl),
                weight=(quantise_cash(market_value / nav) if nav > ZERO else ZERO),
                is_stale=position.identity.label in stale,
            )
        )

    realised = quantise_cash(state.realised_pnl)
    income = quantise_cash(state.dividend_income)
    external = state.cash_summary.external_flow_net
    fees = quantise_cash(state.fees_paid)
    total = quantise_cash(realised + unrealised + income - fees)
    unpriced = quantise_cash(
        sum(
            (state.positions[identity.key].total_cost for identity in coverage.missing),
            ZERO,
        )
    )
    pnl = PnlDecomposition(
        realised=realised,
        unrealised=unrealised,
        income=income,
        fees=fees,
        total=total,
        external_flow_net=external,
        unpriced_cost_basis=unpriced,
        nav_change_since_inception=quantise_cash(total + external - unpriced),
    )
    pnl.check()

    valued = ValuedPortfolioSnapshot(
        valuation_time=snapshot.valuation_time,
        cash=cash,
        market_value=coverage.market_value,
        nav=nav,
        positions=tuple(rows),
        pnl=pnl,
        coverage=coverage,
        ledger_sequence=state.ledger_sequence,
        ledger_head_hash=state.ledger_head_hash,
        inception_nav_at_cost=state.inception_nav_at_cost,
        gross_exposure=(quantise_cash(coverage.market_value / nav) if nav > ZERO else ZERO),
        stale_marks=coverage.stale,
    )
    valued.check_nav_identity()
    valued.check_nav_change_identity()
    state.check_cash_identity()
    return valued
