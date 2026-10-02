"""One monetary arithmetic policy, in `Decimal`.

The risk layer deliberately uses floats: its inputs are floats, its outputs are
weights, and it quantises before hashing. Accounting is the opposite case. Cash is
a balance that accumulates over thousands of events, and a float balance drifts in
a way that is invisible until a reconciliation break appears months later and
nobody can say which trade caused it. `Decimal` removes that class of bug
entirely, so this package holds every money and quantity figure as `Decimal`.

The seam is explicit and lives in one place. `portfolio_accounting.adapter`
converts to the float weights the risk engine expects, and nothing else in either
package crosses the boundary — which is what keeps the two representations from
being mixed silently.

Quantisation is applied at the recorded boundary rather than after every
operation: intermediate products keep full precision, and the figures that reach
a ledger record, a state fingerprint or a report are quantised to their declared
scale. That way a digest is a function of the decided balance rather than of the
order a sum was accumulated in.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .errors import AccountingError, Code

__all__ = [
    "CASH_SCALE",
    "MONEY_POLICY_ID",
    "PRICE_SCALE",
    "QUANTITY_SCALE",
    "RATIO_SCALE",
    "ZERO",
    "as_decimal",
    "is_zero",
    "money_policy",
    "quantise_cash",
    "quantise_price",
    "quantise_quantity",
]

#: The policy's identity, recorded in the manifest.
MONEY_POLICY_ID = "PORTFOLIO_ACCOUNTING_DECIMAL_V1"

#: Cash and every money figure: hundredths of a currency unit are not enough for
#: an average cost, so money carries four places and prices eight.
CASH_SCALE = Decimal("0.0001")

#: Prices and average costs. Eight places survives a 1-for-5 reverse split of a
#: penny stock without the cost basis collapsing into the rounding.
PRICE_SCALE = Decimal("0.00000001")

#: Share quantities. Fractional shares are ordinary, and a reverse split produces
#: them whether or not anyone wanted them.
QUANTITY_SCALE = Decimal("0.00000001")

#: Split ratios, kept wider than prices so a ratio is never the lossy step.
RATIO_SCALE = Decimal("0.0000000001")

ZERO = Decimal("0")

#: Working precision for intermediate products. Generous: the cost of extra
#: digits is nothing, and the cost of a silently truncated product is a balance
#: nobody can reconcile.
_PRECISION = 50


def as_decimal(value: Any, *, field: str = "value") -> Decimal:
    """Convert an input to `Decimal` without going through binary float.

    A float is converted via its `repr`, so `0.1` becomes `Decimal("0.1")` rather
    than the binary expansion. That is the honest reading of a number someone
    wrote down as 0.1, and it keeps a JSON fixture from acquiring drift the moment
    it is loaded.

    Args:
        value: A `Decimal`, `int`, `str`, or `float`.
        field: What is being converted, for the error message.

    Returns:
        The value as a `Decimal`.

    Raises:
        AccountingError: If the value is not a finite number.
    """
    try:
        converted = (
            value
            if isinstance(value, Decimal)
            else Decimal(repr(value))
            if isinstance(value, float)
            else Decimal(value)
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountingError(
            Code.EVENT_INVALID, f"{field} is not a usable number: {value!r}"
        ) from exc
    if not converted.is_finite():
        raise AccountingError(Code.EVENT_INVALID, f"{field} must be finite, got {value!r}")
    return converted


def _quantise(value: Decimal, scale: Decimal) -> Decimal:
    """Round a value to a declared scale at the recorded boundary."""
    with localcontext() as context:
        context.prec = _PRECISION
        quantised = value.quantize(scale)
    return quantised + ZERO if quantised != 0 else ZERO


def quantise_cash(value: Decimal) -> Decimal:
    """Round a money figure to the cash scale.

    Args:
        value: The amount.

    Returns:
        The amount at `CASH_SCALE`, with negative zero normalised.
    """
    return _quantise(value, CASH_SCALE)


def quantise_price(value: Decimal) -> Decimal:
    """Round a price or average cost to the price scale.

    Args:
        value: The price.

    Returns:
        The price at `PRICE_SCALE`.
    """
    return _quantise(value, PRICE_SCALE)


def quantise_quantity(value: Decimal) -> Decimal:
    """Round a share quantity to the quantity scale.

    Args:
        value: The quantity.

    Returns:
        The quantity at `QUANTITY_SCALE`.
    """
    return _quantise(value, QUANTITY_SCALE)


def is_zero(value: Decimal, *, scale: Decimal = QUANTITY_SCALE) -> bool:
    """Whether a value is zero once quantised to its scale.

    Used for closing a position: a residue below the recorded scale is not a
    holding, and leaving it would accumulate dust that no reconciliation could
    explain.

    Args:
        value: The value.
        scale: The scale it is recorded at.

    Returns:
        True when the quantised value is exactly zero.
    """
    return _quantise(value, scale) == ZERO


def money_policy() -> dict[str, Any]:
    """The arithmetic policy, for the manifest.

    Returns:
        A JSON-safe description of the representation and every scale.
    """
    return {
        "policy_id": MONEY_POLICY_ID,
        "representation": "decimal.Decimal throughout; never binary float",
        "working_precision": _PRECISION,
        "cash_scale": str(CASH_SCALE),
        "price_scale": str(PRICE_SCALE),
        "quantity_scale": str(QUANTITY_SCALE),
        "ratio_scale": str(RATIO_SCALE),
        "float_conversion": "via repr, so 0.1 is Decimal('0.1') and not its binary expansion",
        "why_decimal_here": (
            "cash is a balance accumulated over thousands of events. Float drift in "
            "a balance is invisible until a reconciliation break appears months "
            "later and nobody can say which trade caused it."
        ),
        "why_the_risk_layer_differs": (
            "the risk engine consumes float inputs and emits weights, and quantises "
            "before hashing. The two representations meet in exactly one place, the "
            "accounting-to-risk adapter, so they are never mixed silently."
        ),
        "quantisation_boundary": (
            "intermediate products keep full precision; figures reaching a ledger "
            "record, a fingerprint or a report are quantised to their declared scale"
        ),
    }
