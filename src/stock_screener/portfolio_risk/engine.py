"""The authority chain, end to end, and the invariants it refuses to break.

```text
Strategy -> StrategyIntent -> PortfolioConstructor -> RiskEngine
         -> ApprovedPortfolio -> ExecutionIntent
```

`evaluate` is the only way through. There is no second path, no override
parameter, and nothing in the strategy-facing surface that reaches the risk engine
without passing its decisions. A strategy that wants a larger position can ask;
it cannot insist.

Everything the engine emits binds to all three inputs — the intent set, the
portfolio snapshot and the risk policy — so a changed limit or a changed holding
necessarily changes the output digest. Operational timestamps are recorded beside
the fingerprint, never inside it, because a hash that moves with the clock can
never establish that two runs agreed.

The invariants in `_assert_invariants` are checked rather than documented. An
approved portfolio that breaks one is not returned in a degraded form; it is not
returned at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from .advanced import ADVANCED_STAGE_ORDER, verify_risk_timing
from .arithmetic import EXPOSURE_TOLERANCE, is_close, quantise
from .constructor import CONSTRUCTOR_ID, construct
from .drawdown import derive_effective_policy
from .fingerprint import ENGINE_VERSION, binding, digest
from .intents import Action, ExecutionIntent, classify, intents_fingerprint
from .report import build_risk_report
from .risk import CONSTRAINT_MODEL_ID, authorize
from .validation import ReasonCode, validate_inputs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .intents import StrategyIntent
    from .models import PortfolioSnapshot
    from .policy import RiskPolicy
    from .risk import RiskOutcome
    from .risk_inputs import PortfolioRiskSnapshot

log = structlog.get_logger(__name__)

__all__ = [
    "TURNOVER_DEFINITION_ID",
    "ApprovedPortfolio",
    "InvariantViolationError",
    "PortfolioDecision",
    "evaluate",
]

#: This layer's turnover estimate, named so it is never mistaken for another.
TURNOVER_DEFINITION_ID = "PORTFOLIO_WEIGHT_TURNOVER_ESTIMATE_V1"

#: The engine version when market-risk inputs are supplied. V1 is preserved
#: exactly: with no risk snapshot the engine reports V1 and reproduces a V1
#: decision byte for byte, so P1.0's golden output is still a valid check rather
#: than something that had to be re-recorded.
ADVANCED_ENGINE_VERSION = "PORTFOLIO_RISK_ENGINE_V1_1"


class InvariantViolationError(RuntimeError):
    """Raised when an approved portfolio would break a structural invariant."""


@dataclass(frozen=True)
class ApprovedPortfolio:
    """The portfolio the risk engine authorised. Immutable.

    Attributes:
        decision_id: The decision this authorises.
        as_of: The moment it is authorised for.
        targets: `{identity label: weight}`, in identity order.
        cash_target_weight: What remains uninvested.
        gross_exposure: Sum of absolute target weights.
        net_exposure: Sum of signed target weights.
        bindings: Every input fingerprint this is a function of — under V1.1 that
            includes the risk snapshot and the effective policy, so a tightened
            limit is always traceable to the loss or metric that tightened it.
        risk_decisions: Every decision, including rejections.
        engine_version: V1 with no risk inputs, V1.1 with them.
    """

    decision_id: str
    as_of: str
    targets: dict[str, float]
    cash_target_weight: float
    gross_exposure: float
    net_exposure: float
    bindings: dict[str, str]
    risk_decisions: tuple[dict[str, Any], ...]
    engine_version: str = ENGINE_VERSION

    def body(self) -> dict[str, Any]:
        """The fingerprinted body. No wall clock inside."""
        return {
            "engine_version": self.engine_version,
            "constructor_id": CONSTRUCTOR_ID,
            "constraint_model": CONSTRAINT_MODEL_ID,
            "decision_id": self.decision_id,
            "as_of": self.as_of,
            "targets": dict(sorted(self.targets.items())),
            "cash_target_weight": self.cash_target_weight,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "position_count": len(self.targets),
            "bindings": dict(sorted(self.bindings.items())),
            "risk_decisions": list(self.risk_decisions),
        }

    def fingerprint(self) -> str:
        """The approved portfolio's deterministic digest."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the portfolio with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}


@dataclass(frozen=True)
class PortfolioDecision:
    """Everything one evaluation produced.

    Attributes:
        approved: The authorised portfolio.
        execution_intents: Inert descriptions of the required changes.
        risk_summary: Counts and exposures.
        delta_summary: What changed, and the weight-turnover estimate.
        manifest: The decision manifest, binding every fingerprint.
        risk_report: Pre-risk and post-risk state. None under V1.
    """

    approved: ApprovedPortfolio
    execution_intents: tuple[ExecutionIntent, ...]
    risk_summary: dict[str, Any]
    delta_summary: dict[str, Any]
    manifest: dict[str, Any]
    risk_report: dict[str, Any] | None = None

    @property
    def execution_fingerprint(self) -> str:
        """The execution intent set's deterministic digest."""
        return digest([intent.as_json() for intent in self.execution_intents])

    def as_json(self) -> dict[str, Any]:
        """Return the whole decision as a JSON-safe mapping."""
        body = {
            "approved_portfolio": self.approved.as_json(),
            "execution_intents": [intent.as_json() for intent in self.execution_intents],
            "execution_intents_fingerprint": self.execution_fingerprint,
            "risk_summary": self.risk_summary,
            "delta_summary": self.delta_summary,
            "manifest": self.manifest,
        }
        if self.risk_report is not None:
            body["risk_report"] = self.risk_report
        return body


def _assert_monotonicity(outcome: RiskOutcome) -> list[str]:
    """Check no risk control increased a target above its pre-risk value.

    This is the property that makes the staged model trustworthy: a constraint may
    reduce exposure and may refuse it, but no later stage may hand back risk an
    earlier one removed. Without it a liquidity cap could be undone by the budget
    redistribution and the limit would reduce nothing in aggregate.

    Args:
        outcome: The risk outcome, carrying the constructed weights.

    Returns:
        A failure message per identity whose approved weight exceeds its
        constructed weight. Empty when the engine only reduced risk.
    """
    failures: list[str] = []
    for key, approved in sorted(outcome.approved.items()):
        # The pre-risk reference is what the strategy asked for where it asked, and
        # otherwise what is already held — a position retained but never requested
        # is the existing book, not a risk increase. HALTED produces exactly that.
        reference = outcome.constructed.get(key)
        if reference is None:
            reference = outcome.current.get(key)
        if reference is None:
            failures.append(f"{key[0]}:{key[1]} was approved while neither requested nor held")
        elif approved > reference + EXPOSURE_TOLERANCE:
            failures.append(
                f"{key[0]}:{key[1]} was approved at {approved} above its pre-risk "
                f"{reference}: a risk control increased exposure"
            )
    return failures


def _assert_invariants(outcome: RiskOutcome, policy: RiskPolicy) -> None:
    """Refuse an approved portfolio that breaks a structural invariant.

    Args:
        outcome: The risk outcome.
        policy: The limits.

    Raises:
        InvariantViolationError: On any violation. Financial authority does not
            degrade gracefully, so there is no partial result.
    """
    approved = outcome.approved
    failures: list[str] = []
    for key, weight in approved.items():
        label = f"{key[0]}:{key[1]}"
        if policy.long_only and weight < 0:
            failures.append(f"{label} is negative ({weight}) under a long-only policy")
        if weight > policy.max_position_weight + EXPOSURE_TOLERANCE:
            failures.append(
                f"{label} at {weight} exceeds max_position_weight {policy.max_position_weight}"
            )
    if len(approved) > policy.max_positions:
        failures.append(f"{len(approved)} positions exceeds max_positions {policy.max_positions}")
    gross_limit = (
        outcome.effective.max_gross_exposure
        if outcome.effective is not None
        else policy.max_gross_exposure
    )
    if outcome.gross_exposure > gross_limit + EXPOSURE_TOLERANCE:
        failures.append(
            f"gross exposure {outcome.gross_exposure} exceeds the effective "
            f"max_gross_exposure {gross_limit}"
        )
    if outcome.advanced:
        failures.extend(_assert_monotonicity(outcome))
        volatility = outcome.advanced.get("portfolio_volatility") or {}
        after = volatility.get("portfolio_volatility_after")
        limit = policy.max_portfolio_volatility
        if after is not None and limit is not None and after > limit + EXPOSURE_TOLERANCE:
            failures.append(
                f"estimated portfolio volatility {after} exceeds max_portfolio_volatility {limit}"
            )
    if (
        policy.max_net_exposure is not None
        and outcome.net_exposure > policy.max_net_exposure + EXPOSURE_TOLERANCE
    ):
        failures.append(
            f"net exposure {outcome.net_exposure} exceeds "
            f"max_net_exposure {policy.max_net_exposure}"
        )
    if outcome.cash_weight < policy.min_cash_weight - EXPOSURE_TOLERANCE:
        failures.append(
            f"cash {outcome.cash_weight} is below min_cash_weight {policy.min_cash_weight}"
        )
    total = sum(approved.values()) + outcome.cash_weight
    if not is_close(total, 1.0, tolerance=EXPOSURE_TOLERANCE):
        failures.append(f"positions plus cash total {total}, not 1")
    if len(approved) != len(set(approved)):
        failures.append("an identity appears more than once")
    if failures:
        raise InvariantViolationError(
            "the approved portfolio breaks "
            + str(len(failures))
            + " invariant(s): "
            + "; ".join(failures)
        )


def _execution_intents(
    outcome: RiskOutcome, snapshot: PortfolioSnapshot
) -> tuple[ExecutionIntent, ...]:
    """Describe the difference between the current and approved portfolios.

    Args:
        outcome: The risk outcome.
        snapshot: The portfolio as it stands.

    Returns:
        One inert intent per identity that is held, targeted, or both — in
        identity order. Nothing here is an order.
    """
    current = snapshot.weights
    nav = snapshot.nav
    by_key = {decision.identity.key: decision for decision in outcome.decisions}
    intents: list[ExecutionIntent] = []
    for key in sorted(set(current) | set(outcome.approved)):
        held = current.get(key, 0.0)
        target = outcome.approved.get(key, 0.0)
        identity = outcome.identities.get(key) or snapshot.identity_for(key)
        if identity is None:  # pragma: no cover - defended by construction
            continue
        decision = by_key.get(key)
        intents.append(
            ExecutionIntent(
                identity=identity,
                action=classify(held, target),
                current_weight=held,
                target_weight=target,
                notional_delta=quantise((target - held) * nav),
                reason_codes=decision.reason_codes if decision else (ReasonCode.NOT_REQUESTED,),
                strategy_id=None,
            )
        )
    return tuple(intents)


def _delta_summary(intents: Sequence[ExecutionIntent]) -> dict[str, Any]:
    """Summarise what changed, including the weight-turnover estimate.

    Args:
        intents: The execution intents.

    Returns:
        Counts per action and the turnover estimate.
    """
    counts = {str(action): 0 for action in Action}
    for intent in intents:
        counts[str(intent.action)] += 1
    turnover = quantise(
        0.5 * sum(abs(intent.target_weight - intent.current_weight) for intent in intents)
    )
    return {
        "positions_entered": counts[str(Action.ENTER)],
        "positions_exited": counts[str(Action.EXIT)],
        "positions_increased": counts[str(Action.INCREASE)],
        "positions_reduced": counts[str(Action.REDUCE)],
        "positions_held": counts[str(Action.HOLD)],
        "weight_turnover_estimate": turnover,
        "turnover_definition": TURNOVER_DEFINITION_ID,
        "turnover_formula": "0.5 * sum(abs(target_weight - current_weight))",
        "is_realised_execution_turnover": False,
        "turnover_caveat": (
            "a target-weight estimate only. It models no spread, no impact, no "
            "partial fill and no commission, so it is not comparable with a "
            "realised execution turnover or with the research layer's one-way "
            "rebalance turnover."
        ),
    }


def evaluate(
    intents: Sequence[StrategyIntent],
    snapshot: PortfolioSnapshot | None,
    policy: RiskPolicy | None,
    *,
    risk: PortfolioRiskSnapshot | None = None,
    code_commit: str | None = None,
) -> PortfolioDecision:
    """Run the authority chain and return what is actually authorised.

    Pure: no network, no database, no clock inside any fingerprint.

    Args:
        intents: The strategy's requests.
        snapshot: The portfolio as it stands.
        policy: The limits to authorise under.
        risk: Market-risk inputs — volatility, liquidity, covariance, drawdown.
            None keeps V1 semantics exactly, so a V1 decision still reproduces.
        code_commit: The commit evaluating, recorded on the manifest.

    Returns:
        The decision: approved portfolio, execution intents, summaries, manifest,
        and under V1.1 the risk report.

    Raises:
        ValidationError: If the inputs cannot authorise anything, including risk
            inputs that postdate the decision.
        InvariantViolationError: If the result would break an invariant.
    """
    validated = validate_inputs(intents, snapshot, policy)
    assert snapshot is not None and policy is not None  # noqa: S101 - validated above

    timing: dict[str, Any] | None = None
    effective = None
    if risk is not None:
        timing = verify_risk_timing(
            risk,
            decision_as_of=str(validated["as_of"] or snapshot.as_of),
            max_age_seconds=policy.max_risk_metric_age_seconds,
        )
        effective = derive_effective_policy(policy, risk)

    constructed = construct(intents, snapshot, policy)
    outcome = authorize(constructed, snapshot, policy, risk=risk, effective=effective)
    _assert_invariants(outcome, policy)

    bindings = binding(
        intents_fingerprint=intents_fingerprint(intents),
        snapshot_fingerprint=snapshot.fingerprint(),
        policy_fingerprint=policy.fingerprint(),
    )
    if risk is not None:
        bindings = {
            **bindings,
            "engine_version": ADVANCED_ENGINE_VERSION,
            "risk_snapshot_fingerprint": risk.fingerprint(),
            "effective_risk_policy_fingerprint": (
                effective.fingerprint() if effective is not None else ""
            ),
            "covariance_fingerprint": (
                risk.covariance.fingerprint() if risk.covariance is not None else "NONE"
            ),
        }
    approved = ApprovedPortfolio(
        engine_version=ADVANCED_ENGINE_VERSION if risk is not None else ENGINE_VERSION,
        decision_id=str(validated["decision_id"]),
        as_of=str(validated["as_of"] or snapshot.as_of),
        targets={outcome.identities[key].label: weight for key, weight in outcome.approved.items()},
        cash_target_weight=outcome.cash_weight,
        gross_exposure=outcome.gross_exposure,
        net_exposure=outcome.net_exposure,
        bindings=bindings,
        risk_decisions=tuple(decision.as_json() for decision in outcome.decisions),
    )
    execution = _execution_intents(outcome, snapshot)
    risk_summary = {
        **outcome.summary,
        "requested_gross_exposure": validated["requested_gross_exposure"],
        "requested_position_count": validated["intents"],
        "constraint_model": CONSTRAINT_MODEL_ID,
    }
    delta = _delta_summary(execution)
    report = None
    if risk is not None:
        report = build_risk_report(
            outcome,
            risk=risk,
            requested_gross=float(validated["requested_gross_exposure"]),
            requested_count=int(validated["intents"]),
            current_cash_weight=snapshot.cash_weight,
        ).as_json()
        risk_summary = {
            **risk_summary,
            "pre_risk_portfolio_volatility": report["portfolio_volatility_before_risk"],
            "post_risk_portfolio_volatility": report["portfolio_volatility_after_risk"],
            "constructed_gross_exposure": report["exposure"]["constructed_gross"],
            "liquidity_resizes": len(report["liquidity_constrained_positions"]),
            "drawdown_resizes": (1 if effective is not None and effective.tightened else 0),
            "missing_risk_metrics": len(report["missing_risk_metric"]),
            "drawdown": report["drawdown"],
            "effective_mode": str(effective.mode) if effective is not None else None,
        }
    manifest: dict[str, Any] = {
        "manifest_id": (
            "PORTFOLIO_DECISION_MANIFEST_V1_1"
            if risk is not None
            else "PORTFOLIO_DECISION_MANIFEST_V1"
        ),
        "engine_version": ADVANCED_ENGINE_VERSION if risk is not None else ENGINE_VERSION,
        "decision_id": approved.decision_id,
        "as_of": approved.as_of,
        "code_commit": code_commit,
        "evaluated_at_utc": datetime.now(UTC).isoformat(),
        "strategy_intents_fingerprint": bindings["strategy_intents_fingerprint"],
        "portfolio_snapshot_fingerprint": bindings["portfolio_snapshot_fingerprint"],
        "risk_policy_fingerprint": bindings["risk_policy_fingerprint"],
        "constructed_portfolio_fingerprint": constructed.fingerprint(),
        "risk_decisions_fingerprint": outcome.fingerprint(),
        "approved_portfolio_fingerprint": approved.fingerprint(),
        "execution_intents_fingerprint": digest([intent.as_json() for intent in execution]),
        "policy_production_authorized": policy.production_authorized,
        "broker_requests": 0,
        "orders_submitted": 0,
        "timestamp_excluded_from_semantic_hashes": True,
    }
    if risk is not None:
        # The V1.1 block appears only when market-risk inputs were supplied, so a
        # V1 manifest is the V1 manifest rather than a V1.1 one with nulls in it.
        manifest |= {
            "risk_snapshot_fingerprint": risk.fingerprint(),
            "covariance_fingerprint": (
                risk.covariance.fingerprint() if risk.covariance is not None else None
            ),
            "effective_risk_policy_fingerprint": (
                effective.fingerprint() if effective is not None else None
            ),
            "risk_report_fingerprint": report["fingerprint"] if report is not None else None,
            "risk_timing": timing,
            "advanced_risk_stage_order": list(ADVANCED_STAGE_ORDER),
        }
    log.info(
        "portfolio.authorised",
        decision=approved.decision_id,
        approved=len(approved.targets),
        gross=approved.gross_exposure,
        cash=approved.cash_target_weight,
    )
    return PortfolioDecision(
        approved=approved,
        execution_intents=execution,
        risk_summary=risk_summary,
        delta_summary=delta,
        manifest=manifest,
        risk_report=report,
    )
