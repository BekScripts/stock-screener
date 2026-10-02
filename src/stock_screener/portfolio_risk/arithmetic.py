"""One arithmetic policy for the whole portfolio layer.

Financial weights invite scattered epsilons. A comparison written `abs(a - b) <
1e-9` in one module and `1e-6` in another produces a layer whose behaviour nobody
can state, and whose fingerprints differ between runs that are supposed to agree.
So the tolerances live here, named, with the reason for each.

**Floats, not Decimal, and here is why.** Every model this layer consumes already
carries floats — `CompanyMetrics`, `PriceBar`, the frozen score. Converting at
this boundary would mean converting back at the next one, and a layer that holds
`Decimal` internally while its inputs and outputs are floats gets the worst of
both: the cost of exact arithmetic without the guarantee, plus a conversion seam
where rounding is invisible. The guarantee is recovered differently: every weight
that reaches a fingerprint or a serialised artifact is first quantised to
`WEIGHT_DECIMALS`, so the digest is a function of the decided portfolio rather
than of the order the sum happened to be accumulated in.

That makes reproducibility explicit: two runs over identical inputs agree to
`WEIGHT_DECIMALS` by construction, and the fingerprint agrees exactly.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "ARITHMETIC_POLICY_ID",
    "EXPOSURE_TOLERANCE",
    "HOLD_TOLERANCE",
    "WEIGHT_DECIMALS",
    "WEIGHT_TOLERANCE",
    "arithmetic_policy",
    "is_close",
    "quantise",
    "quantise_all",
]

#: The policy's identity, recorded in the manifest.
ARITHMETIC_POLICY_ID = "PORTFOLIO_ARITHMETIC_V1"

#: Decimal places every weight is quantised to before it is hashed or written.
#: Ten places is far finer than any position this layer would express and still
#: well inside a double's exact range, so quantising loses nothing real and makes
#: the digest independent of summation order.
WEIGHT_DECIMALS = 10

#: Equality for a single weight. One order of magnitude coarser than the
#: quantisation, so two weights that serialise identically always compare equal.
WEIGHT_TOLERANCE = 1e-9

#: Equality for a sum over many weights, where error accumulates with the number
#: of terms. Deliberately looser than `WEIGHT_TOLERANCE` for that reason.
EXPOSURE_TOLERANCE = 1e-7

#: How close a current weight must be to its target to count as no change. This
#: is a *policy* choice rather than a numerical one — it says what drift is not
#: worth trading — so it is far coarser than the others: one basis point.
HOLD_TOLERANCE = 1e-4


def quantise(value: float) -> float:
    """Round a weight to the canonical number of decimal places.

    Args:
        value: The weight.

    Returns:
        The weight rounded to `WEIGHT_DECIMALS`. `-0.0` is normalised to `0.0`,
        because the two serialise differently and mean the same thing.

    Raises:
        ValueError: If the value is not finite. A NaN weight must never reach a
            fingerprint, where it would silently compare unequal to itself.
    """
    if not math.isfinite(value):
        raise ValueError(f"a portfolio weight must be finite, got {value!r}")
    rounded = round(value, WEIGHT_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


def quantise_all(values: dict[Any, float]) -> dict[Any, float]:
    """Quantise every weight in a mapping.

    Args:
        values: `{key: weight}`.

    Returns:
        The same mapping with canonical weights.
    """
    return {key: quantise(value) for key, value in values.items()}


def is_close(left: float, right: float, *, tolerance: float = WEIGHT_TOLERANCE) -> bool:
    """Whether two weights are equal within a named tolerance.

    Args:
        left: One weight.
        right: The other.
        tolerance: Which tolerance applies. Pass `EXPOSURE_TOLERANCE` for sums
            and `HOLD_TOLERANCE` for the no-change question.

    Returns:
        True when the difference is within the tolerance.
    """
    return abs(left - right) <= tolerance


def arithmetic_policy() -> dict[str, Any]:
    """The arithmetic policy, for the decision manifest.

    Returns:
        A JSON-safe description of every tolerance and the representation choice.
    """
    return {
        "policy_id": ARITHMETIC_POLICY_ID,
        "representation": "float64, quantised before hashing or serialisation",
        "weight_decimals": WEIGHT_DECIMALS,
        "weight_tolerance": WEIGHT_TOLERANCE,
        "exposure_tolerance": EXPOSURE_TOLERANCE,
        "hold_tolerance": HOLD_TOLERANCE,
        "why_not_decimal": (
            "every input model already carries floats, so holding Decimal here "
            "would add a conversion seam at both boundaries without the exactness "
            "guarantee surviving either of them"
        ),
        "reproducibility": (
            "identical inputs produce weights agreeing to weight_decimals by "
            "construction, so the fingerprint agrees exactly rather than nearly"
        ),
        "hold_tolerance_is_a_policy_choice": (
            "it states what drift is not worth trading, not what the arithmetic "
            "can resolve, which is why it is four orders coarser"
        ),
    }
