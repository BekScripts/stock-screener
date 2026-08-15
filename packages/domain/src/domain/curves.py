"""Piecewise scoring curves.

Every points-awarding rule in CompounderScore v1 is one of two shapes, and both
live here so no component invents a third.

**Interpolation** turns a list of reference points into a continuous rule. The
specification states scoring as "20% → 6 points, 30% → 8 points"; a company at
25% should score between the two rather than falling off a cliff at 29.99%. The
curve is clamped at both ends, which is what caps a 5,000% grower at the same
points as a 75% one without touching the raw metric anyone reads.

**Bands** are a step rule, used only where a level is genuinely categorical and
the specification gives ranges rather than points — gross margin's "40-50% → 4".

Both are pure functions of their arguments, so every reference point in the
score documentation is directly testable.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

Curve = tuple[tuple[float, float], ...]
"""Reference points as `(observed value, points)`, ordered by observed value."""

Bands = tuple[tuple[float, float], ...]
"""Step rules as `(inclusive lower bound, points)`, ordered highest bound first."""


class CurveError(ValueError):
    """A curve was defined in an order the interpolator cannot use.

    Raised at call time rather than tolerated, because a curve whose points are
    out of order interpolates to plausible-looking wrong numbers — the failure
    mode a scoring system can least afford.
    """


def interpolate(curve: Curve, value: float) -> float:
    """Return the points a value earns on a piecewise-linear curve.

    Args:
        curve: Reference points, ordered by ascending observed value. At least
            two are required.
        value: The observed metric.

    Returns:
        The interpolated points. Values below the first reference point earn its
        points, values above the last earn the last — the curve is clamped, not
        extrapolated, so an extreme observation cannot produce points outside
        the component's range.

    Raises:
        CurveError: If the curve has fewer than two points or its observed
            values are not strictly ascending.
    """
    _validate(curve)

    if value <= curve[0][0]:
        return curve[0][1]
    if value >= curve[-1][0]:
        return curve[-1][1]

    for (low_x, low_y), (high_x, high_y) in pairwise(curve):
        if low_x <= value <= high_x:
            fraction = (value - low_x) / (high_x - low_x)
            return low_y + fraction * (high_y - low_y)

    # Unreachable: the clamps above cover everything outside the segments.
    raise CurveError(f"value {value} fell outside every segment of the curve")


def band(bands: Bands, value: float, *, default: float = 0.0) -> float:
    """Return the points for the first band whose lower bound the value reaches.

    Args:
        bands: `(inclusive lower bound, points)` pairs, ordered highest bound
            first.
        value: The observed metric.
        default: Points awarded when the value is below every band.

    Returns:
        The matching band's points, or `default`.
    """
    for lower, points in bands:
        if value >= lower:
            return points
    return default


def _validate(curve: Sequence[tuple[float, float]]) -> None:
    """Raise if a curve cannot be interpolated."""
    if len(curve) < 2:
        raise CurveError("a curve needs at least two reference points")
    for (low_x, _), (high_x, _) in pairwise(curve):
        if high_x <= low_x:
            raise CurveError(
                f"curve reference points must ascend; {high_x} does not follow {low_x}"
            )
