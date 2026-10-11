"""The gate registry: every declared gate bound to code that can evaluate it.

This module exists because of a specific failure. `PAPER_SHADOW_SOAK_PROTOCOL_V2`
declared fifteen zero-tolerance gates and seven promotion gates as JSON data,
was frozen, tagged and armed — and the qualifying commit contained no code able
to evaluate any of them. The protocol was unscoreable from the moment it was
sealed, and nothing noticed because a gate declared as a string looks exactly
like a gate that works.

So a gate is no longer a string. It is a `GateDefinition` with an evaluator
attached, and `require_bijection` refuses a protocol whose declared gates and
available evaluators do not correspond exactly. A gate nobody can evaluate is
now a refusal at arming time rather than a discovery twenty sessions later.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

__all__ = [
    "GATE_REGISTRY_VERSION",
    "EvidenceKind",
    "GateClass",
    "GateDefinition",
    "GateOutcome",
    "SoakEvidence",
    "gate_registry",
    "registry_fingerprint",
    "require_bijection",
]

#: The registry contract.
GATE_REGISTRY_VERSION = "PAPER_SHADOW_SOAK_GATE_REGISTRY_V1"


class GateClass(StrEnum):
    """What a gate failing means.

    The distinction is the one V1 got wrong once and had to correct: a safety
    breach and missing evidence are not the same kind of fact, and scoring them
    the same way either cries wolf or hides a wolf.
    """

    ZERO_TOLERANCE = "ZERO_TOLERANCE"
    """Something that must never happen. One occurrence is `FAIL`."""

    PROMOTION_REQUIRED = "PROMOTION_REQUIRED"
    """Evidence that must accumulate. Absence keeps the verdict at `B`; it is
    not a breach, and reporting it as one is how real breaches get ignored."""


class GateBound(StrEnum):
    """Which direction a gate's threshold binds.

    Separate from `GateClass` on purpose. The two dimensions are genuinely
    independent: "no unresolved CRITICAL incident" is an *at most* rule that is
    nevertheless not a safety breach — an open incident blocks promotion and is
    the system working. Collapsing the two would force every at-most rule to be
    zero-tolerance, and the first draft of this module instead faked it by
    returning negative counts, which is the sort of cleverness that reads fine
    and breaks silently.
    """

    AT_MOST = "AT_MOST"
    """`observed <= limit`. A ceiling."""

    AT_LEAST = "AT_LEAST"
    """`observed >= limit`. A floor."""


class EvidenceKind(StrEnum):
    """Which immutable record a gate reads.

    Declared per gate so the preflight can refuse a protocol that asks for
    evidence the store cannot supply — rather than the scorer discovering it
    mid-soak.
    """

    SESSION_OBSERVATIONS = "SESSION_OBSERVATIONS"
    DRILL_RECORDS = "DRILL_RECORDS"
    INCIDENT_HISTORY = "INCIDENT_HISTORY"
    CALENDAR = "CALENDAR"
    SOAK_STATE = "SOAK_STATE"


@dataclass(frozen=True)
class GateOutcome:
    """What one gate found.

    Attributes:
        gate_id: The stable id.
        gate_class: Whether a failure is a breach or missing evidence.
        observed: What the evidence showed.
        limit: What the protocol allows or requires.
        passed: Whether it met the rule.
        detail: Why, where a reader needs it.
    """

    gate_id: str
    gate_class: GateClass
    bound: GateBound
    observed: int
    limit: int
    passed: bool
    detail: tuple[str, ...] = ()

    @property
    def is_breach(self) -> bool:
        """Whether this failure is a safety breach rather than missing evidence."""
        return not self.passed and self.gate_class is GateClass.ZERO_TOLERANCE

    def as_json(self) -> dict[str, Any]:
        """Return the outcome as a JSON-safe mapping."""
        return {
            "gate_id": self.gate_id,
            "gate_class": str(self.gate_class),
            "bound": str(self.bound),
            "observed": self.observed,
            "limit": self.limit,
            "passed": self.passed,
            "is_breach": self.is_breach,
            "detail": list(self.detail),
        }


class SoakEvidence(Protocol):
    """Everything a gate may read. Immutable records only.

    A Protocol rather than a concrete class so the preflight can check a store
    satisfies it *before* arming — which is precisely the check whose absence
    sealed an unscoreable V2.
    """

    def live_observations(self) -> tuple[Any, ...]:
        """Session observations that are not superseded, by session."""
        ...

    def counted_sessions(self) -> tuple[str, ...]:
        """Each eligible session counted at most once."""
        ...

    def drill_records(self) -> tuple[Any, ...]:
        """Drill evidence recorded for this protocol."""
        ...

    def open_incidents(self) -> tuple[Any, ...]:
        """Incidents still open."""
        ...

    def eligible_elapsed_sessions(self) -> tuple[str, ...]:
        """Eligible sessions whose close has passed."""
        ...

    def soak_state(self) -> str:
        """`ARMED`, `RUNNING`, `HALTED`, `COMPLETED` or `FAILED`."""
        ...


@dataclass(frozen=True)
class GateDefinition:
    """One gate: what it means, and what kind of failure it is.

    It no longer carries an evaluator. A gate used to hold a lambda that read a
    named field off a session observation, and `getattr(row, field, 0)` reported
    fourteen zero-tolerance gates clean against records that never carried the
    field. How a gate is *measured* now lives in
    `SOAK_GATE_EVIDENCE_CONTRACT_V1`, which binds each gate to a versioned
    derivation over authoritative operational records, and `run` is handed the
    derived metric rather than reaching for it.

    Attributes:
        gate_id: A stable id. Never renamed — a renamed gate is a new gate and
            a protocol bound to the old name must stop validating.
        gate_class: Breach or missing evidence.
        bound: Whether the threshold is a ceiling or a floor.
        evidence: Which immutable record family it concerns.
        evaluator_id: A stable name for the rule, recorded in the registry
            fingerprint so a changed rule is visible.
        rule: Human-readable statement of what it checks.
    """

    gate_id: str
    gate_class: GateClass
    bound: GateBound
    evidence: EvidenceKind
    evaluator_id: str
    rule: str

    def run(self, metric: Any, *, limit: int) -> GateOutcome:
        """Evaluate this gate against a derived metric.

        Args:
            metric: The `DerivedMetric` the evidence contract's derivation
                produced for this gate.
            limit: The protocol's threshold — a maximum for an `AT_MOST` gate,
                a minimum for an `AT_LEAST` one.

        Returns:
            The outcome.

        Raises:
            SoakContractError: `SOAK_GATE_EVIDENCE_MISSING` when the metric's
                sources could not answer. Raised rather than defaulted: it is
                the most important behaviour in this module, and a caller that
                wants a verdict anyway has to record a halt to get one.
        """
        observed = metric.require_value()
        passed = observed <= limit if self.bound is GateBound.AT_MOST else observed >= limit
        return GateOutcome(
            gate_id=self.gate_id,
            gate_class=self.gate_class,
            bound=self.bound,
            observed=observed,
            limit=limit,
            passed=passed,
            detail=() if passed else (self.rule, *metric.detail),
        )

    def body(self) -> dict[str, Any]:
        """Return the definition's semantic content, for fingerprinting."""
        return {
            "gate_id": self.gate_id,
            "gate_class": str(self.gate_class),
            "bound": str(self.bound),
            "evidence": str(self.evidence),
            "evaluator_id": self.evaluator_id,
            "rule": self.rule,
        }


def gate_registry() -> dict[str, GateDefinition]:
    """Every gate this scorer knows, by stable id.

    Returns:
        The registry. A protocol may declare any subset; it may not declare
        anything absent from here, and `require_bijection` is what enforces
        that at arming time. How each one is measured is a separate contract —
        see `evidence_contract` — so that a gate and its measurement can be
        versioned independently.
    """
    definitions = [
        GateDefinition(
            gate_id="live_mutations",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_live_requests_v1",
            rule="no request may ever reach a live endpoint",
        ),
        GateDefinition(
            gate_id="duplicate_remote_orders",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_duplicate_remote_orders_v1",
            rule="one authorised order must never reach the venue twice",
        ),
        GateDefinition(
            gate_id="duplicate_admitted_fills",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_duplicate_admitted_fills_v1",
            rule="one fill must never be admitted to the book twice",
        ),
        GateDefinition(
            gate_id="duplicate_accounting_events",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_duplicate_accounting_events_v1",
            rule="one execution must never post twice to the ledger",
        ),
        GateDefinition(
            gate_id="duplicate_scheduled_runs_causing_execution",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_duplicate_executing_runs_v1",
            rule="two invocations of one slot must never both submit",
        ),
        GateDefinition(
            gate_id="duplicate_counted_sessions",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="counted_session_uniqueness_v1",
            rule=(
                "one eligible session must count once however many runs it held; several "
                "runs in a session are not duplicates, two counted observations for one "
                "session are"
            ),
        ),
        GateDefinition(
            gate_id="stale_data_executions",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_stale_data_executions_v1",
            rule="no order may be sized against data outside its freshness policy",
        ),
        GateDefinition(
            gate_id="missing_data_executions",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_missing_data_executions_v1",
            rule="no order may be sized against an absent input",
        ),
        GateDefinition(
            gate_id="missed_run_backfills",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_missed_run_backfills_v1",
            rule="a missed slot is recorded and never run late",
        ),
        GateDefinition(
            gate_id="ignored_reconciliation_breaks",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_ignored_reconciliation_breaks_v1",
            rule="a three-way break must block, never be traded through",
        ),
        GateDefinition(
            gate_id="completed_sessions_reconcile",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="unreconciled_executions_v1",
            rule="every session that executed must reconcile afterwards",
        ),
        GateDefinition(
            gate_id="ignored_remote_state_unknown",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_ignored_remote_ambiguity_v1",
            rule="an unresolved submission must block the whole account",
        ),
        GateDefinition(
            gate_id="kill_bypasses",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_kill_bypasses_v1",
            rule="nothing may submit while the control state forbids it",
        ),
        GateDefinition(
            gate_id="unknown_remote_order_auto_cancels",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_unknown_order_auto_cancels_v1",
            rule="an order this system cannot prove it placed is never cancelled",
        ),
        GateDefinition(
            gate_id="automatic_accounting_repairs",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_automatic_accounting_repairs_v1",
            rule="broker state never writes to the book after bootstrap",
        ),
        GateDefinition(
            gate_id="reranked_executions",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_reranked_executions_v1",
            rule="an execution must use the frozen decision, never a fresh ranking",
        ),
        GateDefinition(
            gate_id="risk_authority_expansions",
            gate_class=GateClass.ZERO_TOLERANCE,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="sum_risk_authority_expansions_v1",
            rule="risk may reduce a strategy request and may never create one",
        ),
        GateDefinition(
            gate_id="qualifying_sessions_observed",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_LEAST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="counted_sessions_v1",
            rule="the required number of eligible sessions must be observed",
        ),
        GateDefinition(
            gate_id="eligible_sessions_with_no_record",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.CALENDAR,
            evaluator_id="elapsed_sessions_without_record_v1",
            rule=(
                "every elapsed eligible session must have a record; a gap blocks promotion "
                "without being a breach, because missing evidence and a safety failure are "
                "different facts"
            ),
        ),
        GateDefinition(
            gate_id="unresolved_critical_incidents",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.INCIDENT_HISTORY,
            evaluator_id="open_critical_incidents_v1",
            rule="no CRITICAL incident may be open at completion",
        ),
        GateDefinition(
            gate_id="unresolved_blocking_incidents",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.INCIDENT_HISTORY,
            evaluator_id="open_blocking_incidents_v1",
            rule="no BLOCKING incident may be open at completion",
        ),
        GateDefinition(
            gate_id="required_drills_passed",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_LEAST,
            evidence=EvidenceKind.DRILL_RECORDS,
            evaluator_id="admissible_passed_drills_v1",
            rule=(
                "every required drill must have admissible passing evidence bound to this "
                "protocol, this commit and this operations engine"
            ),
        ),
        GateDefinition(
            gate_id="successful_strategy_decision_cycles",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_LEAST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="completed_decision_cycles_v1",
            rule=(
                "at least one scheduled decision must have been taken and its following "
                "execution purpose processed from that frozen decision; a broker order is "
                "not required"
            ),
        ),
        GateDefinition(
            gate_id="eligible_decision_executions_accounted_for",
            gate_class=GateClass.PROMOTION_REQUIRED,
            bound=GateBound.AT_MOST,
            evidence=EvidenceKind.SESSION_OBSERVATIONS,
            evaluator_id="decisions_without_execution_account_v1",
            rule=(
                "every decision that became execution-eligible must have its execution "
                "purpose accounted for; none may silently disappear"
            ),
        ),
    ]
    return {row.gate_id: row for row in definitions}


def registry_fingerprint() -> str:
    """Digest the registry, so a changed evaluator is visible in a preflight."""
    from stock_screener.execution import fingerprint_of

    return fingerprint_of(
        {
            "version": GATE_REGISTRY_VERSION,
            "gates": [row.body() for row in gate_registry().values()],
        }
    )


def require_bijection(declared: dict[str, int]) -> None:
    """Refuse unless every declared gate has exactly one known evaluator.

    This is the check whose absence sealed an unscoreable protocol. It runs
    before arming, so a gate that exists only as a string in protocol JSON
    cannot be frozen.

    Args:
        declared: The protocol's gates, id to threshold.

    Raises:
        SoakContractError: `SOAK_GATE_WITHOUT_EVALUATOR` for a declared gate no
            evaluator covers, and `SOAK_EVALUATOR_UNDECLARED` for an evaluator
            the protocol does not declare — the second matters because a gate
            this scorer knows how to check but the protocol never mentioned is a
            safety check silently switched off.
    """
    from .errors import Code, SoakContractError

    known = set(gate_registry())
    requested = set(declared)
    orphans = sorted(requested - known)
    if orphans:
        raise SoakContractError(
            Code.SOAK_GATE_WITHOUT_EVALUATOR,
            f"the protocol declares {orphans} and no evaluator exists for them. A gate "
            "that is only a string in JSON looks exactly like a gate that works, which "
            "is how an unscoreable protocol was frozen, tagged and armed once already",
        )

    undeclared = sorted(known - requested)
    if undeclared:
        raise SoakContractError(
            Code.SOAK_EVALUATOR_UNDECLARED,
            f"this scorer can evaluate {undeclared} and the protocol does not declare "
            "them. A check the scorer knows how to perform but the protocol never "
            "mentions is a safety check silently switched off",
        )
