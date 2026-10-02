"""Pairwise positive-correlation concentration, applied deterministically.

A portfolio can satisfy a volatility ceiling and still be a single bet wearing ten
names. The volatility cap addresses *total* covariance risk; this addresses the
*shape* of it. Both apply, and neither substitutes for the other.

P1.2 deliberately implements the simplest control that is well defined: for every
pair whose correlation is at or above a threshold, the two positions together may
not exceed a limit. What it is **not** is a factor model, a clustering optimiser,
hierarchical risk parity or a diversification optimiser — each needs its own design
and its own evidence, and a graph-clustering rule whose output depends on tie-break
order would be worse than no rule at all.

**Positive correlation, never absolute.** Under a long-only book two names moving
together concentrate risk; two moving oppositely diversify it. Using `abs(rho)`
would penalise the hedge, so the threshold is signed.

**Correlation can be undefined.** A zero-variance security has no correlation with
anything — not a zero one. Treating it as uncorrelated would silently exempt it
from a concentration rule, so an undefined pair follows the policy's missing-metric
behaviour.

The reduction procedure is order-independent by construction. Each round computes
every violating pair's implied ceiling from the *current* weights, takes the most
binding ceiling per identity, and applies them all simultaneously. Within a pair
the reduction is proportional, so the larger position gives up more. Nothing is
ever scaled up, and freed exposure becomes cash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .arithmetic import EXPOSURE_TOLERANCE, quantise
from .policy import MissingMetricPolicy
from .validation import ReasonCode, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .risk_inputs import CovarianceSnapshot

__all__ = [
    "CONCENTRATION_ALGORITHM_ID",
    "ConcentrationOutcome",
    "apply_correlation_concentration",
    "correlation_of",
]

#: The algorithm's identity. Changing the procedure changes results, so it is a
#: semantic change requiring an engine version bump.
CONCENTRATION_ALGORITHM_ID = "SIMULTANEOUS_PAIRWISE_CEILING_FIXED_POINT_V1"

#: How many reduction rounds are attempted before the engine refuses. Overlapping
#: pairs can need several rounds; a portfolio still violating after this many has
#: not converged and must not be returned as if it had.
_MAX_ROUNDS = 64


def correlation_of(
    covariance: CovarianceSnapshot, left: tuple[str, int], right: tuple[str, int]
) -> float | None:
    """The correlation of two identities, or None when it is undefined.

    Args:
        covariance: The validated covariance snapshot.
        left: One durable identity key.
        right: The other.

    Returns:
        The correlation, or None when either variance is zero — undefined, not
        zero. A zero-variance security does not co-move with anything, and calling
        that "uncorrelated" would exempt it from a concentration rule it should
        simply not be subject to.
    """
    index = covariance.index
    if left not in index or right not in index:
        return None
    left_variance = covariance.matrix[index[left]][index[left]]
    right_variance = covariance.matrix[index[right]][index[right]]
    if left_variance <= 0 or right_variance <= 0:
        return None
    return covariance.matrix[index[left]][index[right]] / math.sqrt(left_variance * right_variance)


@dataclass
class ConcentrationOutcome:
    """What the concentration control did.

    Attributes:
        weights: The weights after reduction.
        reasons: `{identity key: [reason codes]}`.
        counterparts: `{identity key: [the labels it was concentrated against]}`.
        diagnostics: Pair counts and extremes, before and after.
        rounds: How many reduction rounds were needed.
    """

    weights: dict[tuple[str, int], float]
    reasons: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    counterparts: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    rounds: int = 0


def _survey(
    weights: Mapping[tuple[str, int], float],
    covariance: CovarianceSnapshot,
    *,
    threshold: float,
    limit: float,
) -> tuple[list[tuple[tuple[str, int], tuple[str, int], float]], dict[str, Any]]:
    """Find every violating correlated pair and summarise the correlation structure.

    Args:
        weights: The current weights.
        covariance: The validated covariance.
        threshold: The correlation at or above which a pair is concentrated.
        limit: The greatest combined weight such a pair may carry.

    Returns:
        The violating pairs in identity order, and the diagnostics.
    """
    keys = sorted(weights)
    defined = 0
    undefined = 0
    above = 0
    violations: list[tuple[tuple[str, int], tuple[str, int], float]] = []
    largest_correlation: float | None = None
    largest_pair_exposure: float | None = None
    for position, left in enumerate(keys):
        for right in keys[position + 1 :]:
            correlation = correlation_of(covariance, left, right)
            if correlation is None:
                undefined += 1
                continue
            defined += 1
            if largest_correlation is None or correlation > largest_correlation:
                largest_correlation = correlation
            if correlation + EXPOSURE_TOLERANCE < threshold:
                continue
            above += 1
            combined = weights[left] + weights[right]
            if largest_pair_exposure is None or combined > largest_pair_exposure:
                largest_pair_exposure = combined
            if combined > limit + EXPOSURE_TOLERANCE:
                violations.append((left, right, combined))
    return violations, {
        "defined_pairs": defined,
        "undefined_pairs": undefined,
        "pairs_above_threshold": above,
        "pairs_violating_exposure_limit": len(violations),
        "largest_positive_correlation": (
            quantise(largest_correlation) if largest_correlation is not None else None
        ),
        "largest_correlated_pair_exposure": (
            quantise(largest_pair_exposure) if largest_pair_exposure is not None else None
        ),
    }


def apply_correlation_concentration(
    weights: Mapping[tuple[str, int], float],
    covariance: CovarianceSnapshot | None,
    *,
    threshold: float | None,
    limit: float | None,
    missing_policy: MissingMetricPolicy,
) -> ConcentrationOutcome:
    """Reduce exposures until no correlated pair exceeds its combined limit.

    Args:
        weights: The aggregate approved weights, before concentration.
        covariance: The validated covariance, or None.
        threshold: The correlation at or above which a pair concentrates. None
            disables the control.
        limit: The greatest combined weight a concentrated pair may carry.
        missing_policy: What an undefined correlation permits.

    Returns:
        The outcome, with the weights after reduction and why each moved.

    Raises:
        ValidationError: If a correlation needed by the control is undefined and
            the policy requires the metric, or if the reduction does not converge.
    """
    current = dict(weights)
    outcome = ConcentrationOutcome(weights=current)
    if threshold is None or limit is None:
        outcome.diagnostics = {
            "applied": False,
            "reason": "the policy sets no correlation threshold or pair limit",
        }
        return outcome
    if covariance is None:
        if missing_policy is MissingMetricPolicy.REQUIRE_METRIC:
            raise ValidationError(
                ReasonCode.COVARIANCE_UNAVAILABLE,
                "the correlation concentration control needs a covariance snapshot "
                "and the policy requires the metric",
            )
        outcome.diagnostics = {"applied": False, "reason": "no covariance snapshot"}
        return outcome

    violations, before = _survey(current, covariance, threshold=threshold, limit=limit)
    if before["undefined_pairs"] and missing_policy is MissingMetricPolicy.REQUIRE_METRIC:
        raise ValidationError(
            ReasonCode.CORRELATION_UNDEFINED,
            f"{before['undefined_pairs']} pair(s) have an undefined correlation — a "
            "zero-variance security does not co-move with anything — and the policy "
            "requires the metric.",
        )

    rounds = 0
    while violations and rounds < _MAX_ROUNDS:
        rounds += 1
        # Every ceiling is computed from the same snapshot of weights and applied
        # together, so no pair's treatment depends on which pair was considered
        # first. Within a pair the cut is proportional: the larger position gives
        # up more, which is the only split that treats the pair symmetrically.
        ceilings: dict[tuple[str, int], float] = {}
        for left, right, combined in violations:
            factor = limit / combined
            for key in (left, right):
                proposed = quantise(current[key] * factor)
                if key not in ceilings or proposed < ceilings[key]:
                    ceilings[key] = proposed
            for key, other in ((left, right), (right, left)):
                label = f"{other[0]}:{other[1]}"
                against = outcome.counterparts.setdefault(key, [])
                if label not in against:
                    against.append(label)
        for key, ceiling in sorted(ceilings.items()):
            if ceiling < current[key] - EXPOSURE_TOLERANCE:
                current[key] = ceiling
                codes = outcome.reasons.setdefault(key, [])
                if ReasonCode.CORRELATION_CONCENTRATION_LIMIT not in codes:
                    codes.append(ReasonCode.CORRELATION_CONCENTRATION_LIMIT)
        violations, _ = _survey(current, covariance, threshold=threshold, limit=limit)

    if violations:
        raise ValidationError(
            ReasonCode.CORRELATION_CONCENTRATION_LIMIT,
            f"the correlation concentration reduction did not converge after "
            f"{_MAX_ROUNDS} rounds with {len(violations)} pair(s) still violating. A "
            "portfolio that still breaches the limit is not returned as though it "
            "complied.",
        )

    after = _survey(current, covariance, threshold=threshold, limit=limit)[1]
    outcome.weights = current
    outcome.rounds = rounds
    outcome.diagnostics = {
        "applied": rounds > 0,
        "algorithm": CONCENTRATION_ALGORITHM_ID,
        "threshold": threshold,
        "max_correlated_pair_exposure": limit,
        "rounds": rounds,
        "before": before,
        "after": after,
        "positive_correlation_only": True,
        "uses_absolute_correlation": False,
        "freed_exposure_becomes_cash": True,
        "is_not_a_factor_model": True,
        "is_not_diversification_alpha": True,
    }
    for key in sorted(outcome.counterparts):
        outcome.counterparts[key] = sorted(outcome.counterparts[key])
    return outcome
