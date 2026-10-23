"""The small statistics R1.1 needs, written once and tested.

Deliberately local rather than imported from the harness: R1.0's modules are
inputs to this phase and are left untouched, and a private helper is not an
interface to reach through. Nothing here is novel — average-rank Spearman, a
linear-interpolated quantile, a sample standard deviation and a seeded
percentile bootstrap — and each is the same construction R1.0 used, so a
statistic recomputed here is comparable to the one R1.0 recorded.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BOOTSTRAP_SAMPLES",
    "BOOTSTRAP_SEED",
    "Spread",
    "bootstrap_mean",
    "quantile",
    "ranks",
    "spearman",
    "spread",
    "std",
]

#: The same seeded bootstrap R1.0 used, so intervals are comparable.
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260101


def quantile(values: Sequence[float], fraction: float) -> float | None:
    """The linear-interpolated quantile of a sample.

    Args:
        values: The sample. Order does not matter.
        fraction: Between 0 and 1.

    Returns:
        The quantile, or None for an empty sample.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def std(values: Sequence[float]) -> float | None:
    """The sample standard deviation, or None below two observations."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not invent an ordering."""
    order = sorted(range(len(values)), key=lambda position: values[position])
    out = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            out[order[index]] = average
        position = end + 1
    return out


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Rank correlation, or None below three pairs or without variation."""
    if len(left) < 3 or len(left) != len(right):
        return None
    ranked_left, ranked_right = ranks(left), ranks(right)
    mean_left = sum(ranked_left) / len(ranked_left)
    mean_right = sum(ranked_right) / len(ranked_right)
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(ranked_left, ranked_right, strict=True)
    )
    spread_left = math.sqrt(sum((a - mean_left) ** 2 for a in ranked_left))
    spread_right = math.sqrt(sum((b - mean_right) ** 2 for b in ranked_right))
    if spread_left == 0 or spread_right == 0:
        return None
    return covariance / (spread_left * spread_right)


def bootstrap_mean(values: Sequence[float]) -> tuple[float | None, float | None]:
    """A seeded percentile bootstrap of the mean, resampling the sample itself.

    Args:
        values: The sample — months, never security-months.

    Returns:
        The 2.5th and 97.5th percentiles of the resampled means, or `(None,
        None)` below three observations.
    """
    if len(values) < 3:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - a seeded research bootstrap
    size = len(values)
    means = [
        sum(values[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(BOOTSTRAP_SAMPLES)
    ]
    means.sort()
    return quantile(means, 0.025), quantile(means, 0.975)


class Spread:
    """A sample summarised the same way everywhere in this phase.

    Args:
        values: The sample.
    """

    def __init__(self, values: Sequence[float]) -> None:
        self.values = list(values)

    def as_json(self) -> dict[str, object]:
        """Count, moments, quantiles, extremes and sign counts."""
        values = self.values
        deviation = std(values)
        mean = sum(values) / len(values) if values else None
        error = deviation / math.sqrt(len(values)) if deviation and values else None
        low, high = bootstrap_mean(values)
        return {
            "n": len(values),
            "mean": mean,
            "median": quantile(values, 0.5),
            "std": deviation,
            "standard_error": error,
            "t_stat": mean / error if mean is not None and error else None,
            "p10": quantile(values, 0.10),
            "p25": quantile(values, 0.25),
            "p75": quantile(values, 0.75),
            "p90": quantile(values, 0.90),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "positive": sum(1 for value in values if value > 0),
            "negative": sum(1 for value in values if value < 0),
            "positive_share": (
                sum(1 for value in values if value > 0) / len(values) if values else None
            ),
            "ci_low": low,
            "ci_high": high,
        }


def spread(values: Sequence[float]) -> dict[str, object]:
    """Summarise a sample. Shorthand for `Spread(values).as_json()`."""
    return Spread(values).as_json()
