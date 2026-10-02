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

from .arithmetic import EXPOSURE_TOLERANCE, is_close, quantise
from .constructor import CONSTRUCTOR_ID, construct
from .fingerprint import ENGINE_VERSION, binding, digest
from .intents import Action, ExecutionIntent, classify, intents_fingerprint
from .risk import CONSTRAINT_MODEL_ID, authorize
from .validation import ReasonCode, validate_inputs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .intents import StrategyIntent
    from .models import PortfolioSnapshot
    from .policy import RiskPolicy
    from .risk import RiskOutcome

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
        bindings: The three input fingerprints this is a function of.
        risk_decisions: Every decision, including rejections.
    """

    decision_id: str
    as_of: str
    targets: dict[str, float]
    cash_target_weight: float
    gross_exposure: float
    net_exposure: float
    bindings: dict[str, str]
    risk_decisions: tuple[dict[str, Any], ...]

    def body(self) -> dict[str, Any]:
        """The fingerprinted body. No wall clock inside."""
        return {
            "engine_version": ENGINE_VERSION,
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
    """

    approved: ApprovedPortfolio
    execution_intents: tuple[ExecutionIntent, ...]
    risk_summary: dict[str, Any]
    delta_summary: dict[str, Any]
    manifest: dict[str, Any]

    @property
    def execution_fingerprint(self) -> str:
        """The execution intent set's deterministic digest."""
        return digest([intent.as_json() for intent in self.execution_intents])

    def as_json(self) -> dict[str, Any]:
        """Return the whole decision as a JSON-safe mapping."""
        return {
            "approved_portfolio": self.approved.as_json(),
            "execution_intents": [intent.as_json() for intent in self.execution_intents],
            "execution_intents_fingerprint": self.execution_fingerprint,
            "risk_summary": self.risk_summary,
            "delta_summary": self.delta_summary,
            "manifest": self.manifest,
        }


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
    if outcome.gross_exposure > policy.max_gross_exposure + EXPOSURE_TOLERANCE:
        failures.append(
            f"gross exposure {outcome.gross_exposure} exceeds "
            f"max_gross_exposure {policy.max_gross_exposure}"
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
    code_commit: str | None = None,
) -> PortfolioDecision:
    """Run the authority chain and return what is actually authorised.

    Pure: no network, no database, no clock inside any fingerprint.

    Args:
        intents: The strategy's requests.
        snapshot: The portfolio as it stands.
        policy: The limits to authorise under.
        code_commit: The commit evaluating, recorded on the manifest.

    Returns:
        The decision: approved portfolio, execution intents, summaries, manifest.

    Raises:
        ValidationError: If the inputs cannot authorise anything.
        InvariantViolationError: If the result would break an invariant.
    """
    validated = validate_inputs(intents, snapshot, policy)
    assert snapshot is not None and policy is not None  # noqa: S101 - validated above

    constructed = construct(intents, snapshot, policy)
    outcome = authorize(constructed, snapshot, policy)
    _assert_invariants(outcome, policy)

    bindings = binding(
        intents_fingerprint=intents_fingerprint(intents),
        snapshot_fingerprint=snapshot.fingerprint(),
        policy_fingerprint=policy.fingerprint(),
    )
    approved = ApprovedPortfolio(
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
    manifest = {
        "manifest_id": "PORTFOLIO_DECISION_MANIFEST_V1",
        "engine_version": ENGINE_VERSION,
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
    )
