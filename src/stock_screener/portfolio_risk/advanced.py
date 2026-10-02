"""The P1.1 risk stages: liquidity capacity, missing metrics, volatility ceiling.

Each stage here produces a *ceiling* per identity rather than mutating a weight
directly. That shape is deliberate and it is what makes §39's monotonicity
guarantee hold: a ceiling, once lowered, bounds every later stage including the
budget redistribution. Without it a liquidity cap on one name would be defeated by
the normalising mode handing the freed budget to another name — or worse, back to
the capped one.

Three properties follow:

- **Risk controls only reduce.** A ceiling never rises. The portfolio-volatility
  stage scales down and never up, and the budget fit cannot lift a weight past its
  ceiling.
- **Existing risk and new risk are different.** A position already above its
  capacity limit is not an instruction to buy up to the limit, and it is never
  prevented from being reduced. Liquidity constrains the *increase*.
- **Ignorance is not permission.** A missing metric produces a ceiling at the
  current weight — no new risk, reduction still available — unless the policy
  explicitly says to ignore the metric or to refuse the decision outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .arithmetic import EXPOSURE_TOLERANCE, quantise
from .policy import MissingMetricPolicy
from .risk_inputs import MetricState, RiskInputError
from .validation import ReasonCode, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .policy import RiskPolicy
    from .risk_inputs import PortfolioRiskSnapshot

__all__ = [
    "ADVANCED_STAGE_ORDER",
    "Ceilings",
    "apply_liquidity_ceilings",
    "scale_to_volatility_ceiling",
    "verify_risk_timing",
]

#: The frozen evaluation order. Reordering these changes results, so it is a
#: semantic change requiring an engine version bump — not a refactor.
ADVANCED_STAGE_ORDER: tuple[str, ...] = (
    "validate_inputs_and_risk_timing",
    "seed_ceilings_at_pre_risk_weights",
    "derive_effective_policy_from_drawdown",
    "position_count_cap",
    "per_name_weight_cap",
    "position_liquidity_capacity",
    "trade_liquidity_capacity",
    "missing_risk_metric_ceilings",
    "effective_gross_and_cash_budget",
    "portfolio_volatility_ceiling",
    "dust_floor",
    "trade_notional_cap",
    "effective_operating_mode",
    "forced_exits",
    "invariants",
)


@dataclass
class Ceilings:
    """Per-identity upper bounds, and why each was imposed.

    Attributes:
        bounds: `{identity key: greatest permitted weight}`.
        reasons: `{identity key: [reason codes, in the order imposed]}`.
        blocked: Identities that may take no new risk at all.
    """

    bounds: dict[tuple[str, int], float] = field(default_factory=dict)
    reasons: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    blocked: set[tuple[str, int]] = field(default_factory=set)

    def limit(self, key: tuple[str, int], ceiling: float, code: str) -> None:
        """Lower a ceiling, recording why. Never raises one.

        A ceiling seeded at the pre-risk weight and only ever lowered is what
        makes the whole staged model monotonic: every later stage, including the
        budget redistribution, is bounded by it.

        Args:
            key: The identity.
            ceiling: The proposed bound.
            code: The reason code to record if it binds.
        """
        current = self.bounds.get(key)
        if current is None or ceiling < current - EXPOSURE_TOLERANCE:
            self.bounds[key] = quantise(max(ceiling, 0.0))
            self.reasons.setdefault(key, [])
            if code not in self.reasons[key]:
                self.reasons[key].append(code)

    def of(self, key: tuple[str, int], default: float) -> float:
        """The ceiling for an identity, or a default when none was imposed.

        Args:
            key: The identity.
            default: The fallback, normally the policy's per-name cap.

        Returns:
            The tighter of the two.
        """
        return min(default, self.bounds.get(key, default))

    def codes(self, key: tuple[str, int]) -> list[str]:
        """The reason codes recorded for an identity, in order."""
        return list(self.reasons.get(key, ()))


def verify_risk_timing(
    risk: PortfolioRiskSnapshot, *, decision_as_of: str, max_age_seconds: float | None
) -> dict[str, Any]:
    """Refuse risk inputs that postdate or outlive the decision.

    A metric dated after the decision it constrains is the leakage problem in a new
    costume: it would size today's portfolio using information that did not exist.

    Args:
        risk: The risk snapshot.
        decision_as_of: The decision instant, ISO 8601.
        max_age_seconds: The greatest permitted metric age, or None for no bound.
            P1.1 defines no production freshness threshold and will not invent one.

    Returns:
        A summary of what was checked.

    Raises:
        ValidationError: If the snapshot or any metric postdates the decision, or
            exceeds an explicitly configured age.
    """
    from datetime import datetime

    if risk.as_of > decision_as_of:
        raise ValidationError(
            ReasonCode.RISK_SNAPSHOT_AFTER_DECISION,
            f"the risk snapshot is dated {risk.as_of} and the decision {decision_as_of}. "
            "Constraining a decision with information that postdates it would size the "
            "portfolio against a market it could not have seen.",
        )
    late = sorted(row.identity.label for row in risk.metrics if row.metric_as_of > decision_as_of)
    if late:
        raise ValidationError(
            ReasonCode.RISK_METRIC_AFTER_DECISION,
            f"{late} carry metrics dated after {decision_as_of}",
        )
    stale: list[str] = []
    if max_age_seconds is not None:
        decided = datetime.fromisoformat(decision_as_of)
        for row in sorted(risk.metrics, key=lambda m: m.identity):
            age = (decided - datetime.fromisoformat(row.metric_as_of)).total_seconds()
            if age > max_age_seconds:
                stale.append(f"{row.identity.label} ({age:.0f}s)")
    if stale:
        raise ValidationError(
            ReasonCode.RISK_METRIC_STALE,
            f"{stale} exceed the configured maximum age of {max_age_seconds}s",
        )
    return {
        "risk_snapshot_as_of": risk.as_of,
        "decision_as_of": decision_as_of,
        "metrics_checked": len(risk.metrics),
        "max_age_seconds": max_age_seconds,
        "freshness_enforced": max_age_seconds is not None,
    }


def apply_liquidity_ceilings(
    ceilings: Ceilings,
    *,
    requested: Mapping[tuple[str, int], float],
    current: Mapping[tuple[str, int], float],
    risk: PortfolioRiskSnapshot,
    policy: RiskPolicy,
    nav: float,
) -> dict[str, Any]:
    """Impose position- and trade-capacity ceilings from average dollar volume.

    Position capacity asks *how much of this security may we hold* and trade
    capacity asks *how much may we change in one decision*. They are different
    risks — a large holding acquired over months is not the same as acquiring one
    today — so they are separate limits rather than one conflated number.

    Neither is a market-impact model. Nothing here predicts a fill, a spread or a
    price move; they are capacity limits on the size of the bet and the size of the
    step.

    Args:
        ceilings: The ceilings to lower.
        requested: The constructed target weights.
        current: The weights currently held.
        risk: The risk snapshot supplying ADV.
        policy: Supplies the fractions and the missing-metric behaviour.
        nav: Portfolio net asset value, for converting notional to weight.

    Returns:
        Which names each limit touched.

    Raises:
        ValidationError: If a metric is missing and the policy requires it.
    """
    metrics = risk.by_identity
    position_limited: list[str] = []
    trade_limited: list[str] = []
    unknown: list[str] = []
    for key in sorted(set(requested) | set(current)):
        held = current.get(key, 0.0)
        label = f"{key[0]}:{key[1]}"
        row = metrics.get(key)
        state = row.liquidity_state if row is not None else MetricState.UNKNOWN
        adv = row.average_daily_dollar_volume if row is not None else None

        if state is not MetricState.KNOWN or adv is None:
            if state is MetricState.NOT_APPLICABLE or (
                policy.max_position_adv_fraction is None and policy.max_trade_adv_fraction is None
            ):
                continue
            behaviour = policy.missing_liquidity_policy
            if behaviour is MissingMetricPolicy.IGNORE_METRIC:
                continue
            if behaviour is MissingMetricPolicy.REQUIRE_METRIC:
                raise ValidationError(
                    ReasonCode.LIQUIDITY_UNKNOWN,
                    f"{label} has no average daily dollar volume and the policy "
                    "requires the metric. No portfolio is authorised.",
                )
            # REJECT_INCREASE: hold at the current weight. Reduction stays available,
            # because being ignorant of liquidity is never a reason to forbid exiting.
            unknown.append(label)
            ceilings.limit(key, held, ReasonCode.LIQUIDITY_UNKNOWN)
            if held <= EXPOSURE_TOLERANCE:
                ceilings.blocked.add(key)
            continue

        if policy.max_position_adv_fraction is not None:
            allowed = quantise(adv * policy.max_position_adv_fraction / nav)
            # An existing position above capacity is not an instruction to buy up to
            # the limit, and never a reason it cannot be reduced: the ceiling is the
            # greater of the limit and what is already held.
            ceiling = max(allowed, held)
            if requested.get(key, 0.0) > ceiling + EXPOSURE_TOLERANCE:
                position_limited.append(label)
                ceilings.limit(key, ceiling, ReasonCode.POSITION_LIQUIDITY_LIMIT)

        if policy.max_trade_adv_fraction is not None:
            step = quantise(adv * policy.max_trade_adv_fraction / nav)
            if requested.get(key, 0.0) > held + step + EXPOSURE_TOLERANCE:
                trade_limited.append(label)
                ceilings.limit(key, held + step, ReasonCode.TRADE_LIQUIDITY_LIMIT)

    return {
        "position_capacity_limited": position_limited,
        "trade_capacity_limited": trade_limited,
        "liquidity_unknown": unknown,
        "max_position_adv_fraction": policy.max_position_adv_fraction,
        "max_trade_adv_fraction": policy.max_trade_adv_fraction,
        "missing_liquidity_policy": str(policy.missing_liquidity_policy),
        "is_a_market_impact_model": False,
    }


def apply_volatility_ceilings(
    ceilings: Ceilings,
    *,
    requested: Mapping[tuple[str, int], float],
    current: Mapping[tuple[str, int], float],
    risk: PortfolioRiskSnapshot,
    policy: RiskPolicy,
) -> dict[str, Any]:
    """Block new risk in names whose volatility or covariance row is missing.

    Args:
        ceilings: The ceilings to lower.
        requested: The constructed target weights.
        current: The weights currently held.
        risk: The risk snapshot.
        policy: Supplies the missing-metric behaviour.

    Returns:
        Which names were affected.

    Raises:
        ValidationError: If a metric is missing and the policy requires it.
    """
    if policy.max_portfolio_volatility is None:
        return {
            "volatility_constraint_evaluated": False,
            "reason": "the policy sets no max_portfolio_volatility",
        }
    metrics = risk.by_identity
    covariance = risk.covariance
    missing: list[str] = []
    for key in sorted(requested):
        label = f"{key[0]}:{key[1]}"
        row = metrics.get(key)
        has_vol = row is not None and row.volatility_state is MetricState.KNOWN
        has_row = covariance is not None and key in covariance.index
        if has_row and has_vol:
            continue
        behaviour = policy.missing_volatility_policy
        if behaviour is MissingMetricPolicy.IGNORE_METRIC:
            continue
        code = (
            ReasonCode.COVARIANCE_UNAVAILABLE
            if covariance is None
            else ReasonCode.RISK_METRIC_MISSING
        )
        if behaviour is MissingMetricPolicy.REQUIRE_METRIC:
            raise ValidationError(
                code,
                f"{label} lacks the volatility or covariance data the portfolio "
                "volatility limit needs, and the policy requires the metric.",
            )
        missing.append(label)
        ceilings.limit(key, current.get(key, 0.0), code)
        if current.get(key, 0.0) <= EXPOSURE_TOLERANCE:
            ceilings.blocked.add(key)
    return {
        "volatility_constraint_evaluated": True,
        "missing_risk_metric": missing,
        "covariance_available": covariance is not None,
        "missing_volatility_policy": str(policy.missing_volatility_policy),
    }


def scale_to_volatility_ceiling(
    weights: dict[tuple[str, int], float],
    *,
    risk: PortfolioRiskSnapshot,
    policy: RiskPolicy,
) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    """Scale risky weights down until estimated portfolio volatility is within the cap.

    `sigma_p = sqrt(w' * Sigma * w)` over the risky weights, with cash contributing zero — this is
    a market-risk model and cash carries no market exposure in it.

    The rule is one multiplication: `scale = limit / sigma_p` when it exceeds the
    limit. Because
    volatility is homogeneous of degree one in the weights, that single scale lands
    exactly on the limit, preserves every relative weight, and cannot raise any
    target. When it is already within the cap nothing happens — the limit is a
    ceiling, not a target, so exposure is never increased to reach it.

    Args:
        weights: The target weights after every ceiling has been applied.
        risk: Supplies the covariance snapshot.
        policy: Supplies the ceiling.

    Returns:
        The scaled weights and a diagnostic record.
    """
    limit = policy.max_portfolio_volatility
    covariance = risk.covariance
    if limit is None or covariance is None or not weights:
        return weights, {
            "applied": False,
            "limit": limit,
            "covariance_available": covariance is not None,
            "portfolio_volatility_before": None,
            "portfolio_volatility_after": None,
        }
    priced = {key: weight for key, weight in weights.items() if key in covariance.index}
    if len(priced) != len(weights):
        # Every unpriced name was already ceilinged to its current weight by
        # `apply_volatility_ceilings`, so a residue here means the policy chose to
        # ignore the metric. Scaling on a partial matrix would understate risk.
        return weights, {
            "applied": False,
            "limit": limit,
            "covariance_available": True,
            "reason": "some weighted identities have no covariance row; the policy "
            "ignores the metric, so no portfolio-volatility scaling is attempted",
            "portfolio_volatility_before": None,
            "portfolio_volatility_after": None,
        }
    try:
        before = covariance.volatility_of(priced)
    except RiskInputError:  # pragma: no cover - guarded by the ceiling stage
        return weights, {"applied": False, "limit": limit, "covariance_available": True}
    if before <= limit + EXPOSURE_TOLERANCE:
        return weights, {
            "applied": False,
            "limit": limit,
            "covariance_available": True,
            "portfolio_volatility_before": before,
            "portfolio_volatility_after": before,
            "note": "within the ceiling; a ceiling is not a target, so exposure is "
            "not raised to reach it",
        }
    scale = limit / before
    scaled = {key: quantise(weight * scale) for key, weight in weights.items()}
    after = covariance.volatility_of({key: scaled[key] for key in priced})
    return scaled, {
        "applied": True,
        "limit": limit,
        "covariance_available": True,
        "scale": quantise(scale),
        "portfolio_volatility_before": before,
        "portfolio_volatility_after": after,
        "relative_weights_preserved": True,
        "note": "volatility is homogeneous of degree one in the weights, so one "
        "proportional scale lands on the limit exactly and preserves every ratio",
    }
