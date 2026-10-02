"""The one seam between Decimal accounting and float risk arithmetic.

These two layers use different number types on purpose. Accounting is exact: a
cash balance that is off by a cent is wrong, and `Decimal` makes that statement
enforceable. Risk arithmetic is statistical: a covariance eigenvalue to fifteen
digits is noise, and `float64` is both faster and honest about the precision
actually available.

Mixing them anywhere else produces the worst of both — a NAV that drifts and a
risk figure that pretends to be exact. So the conversion happens here, in one
function, and the direction is one-way:

**Accounting → risk, never back.** The risk engine receives a portfolio and
returns *targets*. A target is a statement about what should be held; it is not
evidence that anything was traded, and this module has no function that writes a
target into a holding. The only thing that changes a holding is a `FillEvent` on
the ledger.

That asymmetry is the whole point. A system where approving a target also
recorded it would report a portfolio that was never bought.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_risk import PortfolioSnapshot, Position

from .errors import AccountingError, Code
from .money import ZERO, quantise_cash

if TYPE_CHECKING:
    from decimal import Decimal

    from .pnl import ValuedPortfolioSnapshot

__all__ = [
    "DECIMAL_TO_FLOAT_SCALE",
    "RISK_ADAPTER_VERSION",
    "seam_policy",
    "to_float",
    "to_risk_snapshot",
]

#: The conversion contract.
RISK_ADAPTER_VERSION = "PORTFOLIO_ACCOUNTING_TO_RISK_V1"

#: Decimal figures are rendered to this many places before becoming floats, which
#: is the same quantisation the risk layer applies to its own arithmetic. Matching
#: it means a round trip through the seam is stable rather than drifting by a
#: digit each time.
DECIMAL_TO_FLOAT_SCALE = 10


def to_float(value: Decimal) -> float:
    """Convert an exact figure to the risk layer's arithmetic.

    Args:
        value: The exact amount.

    Returns:
        The same figure as a float, quantised to the risk layer's precision.

    Raises:
        AccountingError: If the value is not finite. An infinity or NaN reaching
            the risk layer would propagate silently through every weight.
    """
    if not value.is_finite():
        raise AccountingError(
            Code.MARK_INVALID,
            f"{value} is not finite and cannot cross into risk arithmetic, where it "
            "would propagate through every weight without raising",
        )
    return round(float(value), DECIMAL_TO_FLOAT_SCALE)


def to_risk_snapshot(valued: ValuedPortfolioSnapshot) -> PortfolioSnapshot:
    """Present a valued portfolio to the risk engine.

    The risk layer's `PortfolioSnapshot` refuses a non-positive NAV, because
    weights are undefined against one. That refusal is left in place rather than
    worked around: a portfolio with nothing in it has no weights to constrain,
    and a caller asking for risk decisions about one has a different problem.

    Args:
        valued: The accounting snapshot.

    Returns:
        The risk layer's view of the same portfolio.

    Raises:
        AccountingError: If the portfolio has no positive NAV, or if a figure
            cannot cross the seam.
    """
    if quantise_cash(valued.nav) <= ZERO:
        raise AccountingError(
            Code.VALUATION_INCOMPLETE,
            f"NAV is {quantise_cash(valued.nav)}; the risk layer defines every "
            "weight as a fraction of NAV, so there is nothing for it to size "
            "against. This is a question about an empty portfolio rather than a "
            "risk decision.",
        )
    positions = tuple(
        Position(
            identity=row.identity,
            quantity=to_float(row.quantity),
            mark_price=to_float(row.mark_price),
            cost_basis=to_float(row.average_cost),
        )
        for row in valued.positions
    )
    return PortfolioSnapshot(
        as_of=valued.valuation_time,
        cash=to_float(valued.cash),
        positions=positions,
        price_fingerprint=valued.ledger_head_hash,
    )


def seam_policy() -> dict[str, Any]:
    """Describe the Decimal/float boundary.

    Returns:
        The policy, for the manifest.
    """
    return {
        "version": RISK_ADAPTER_VERSION,
        "accounting_arithmetic": "Decimal, exact, quantised by money.py",
        "risk_arithmetic": f"float64, quantised to {DECIMAL_TO_FLOAT_SCALE} places",
        "direction": "ACCOUNTING_TO_RISK_ONLY",
        "targets_never_become_holdings": (
            "the risk engine returns targets; only a FillEvent on the ledger "
            "changes a holding. There is no function here that writes a target "
            "into a position, because a system where approving a target also "
            "recorded it would report a portfolio that was never bought."
        ),
    }
