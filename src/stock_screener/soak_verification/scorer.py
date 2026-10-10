"""`PAPER_SHADOW_SOAK_SCORER_V1` — the evaluator a protocol must have before arming.

Session-based, because that is the model V2 introduced and V1's run-oriented
evaluator cannot express. It is a new scorer rather than a retrofit for a
concrete reason: V1's `evaluate_soak` reaches for `qualifying_observations`,
`eligible_sessions`, `missing_sessions`, `passed_drills` and `state`, none of
which a session store has, and bending one to look like the other would leave
two scorers disagreeing about what a session is.

Every verdict here is mechanical. Nothing reads prose, nothing takes a threshold
argument, and the four states come out of the evidence and the frozen gates:

    FAIL  a zero-tolerance gate was breached. Never a percentage.
    C     the soak was halted. A halted protocol can never become A.
    B     nothing is wrong and there is not yet enough evidence.
    A     every gate met, prospectively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .gates import GateClass, gate_registry, registry_fingerprint

if TYPE_CHECKING:
    from .gates import GateOutcome, SoakEvidence

__all__ = ["SCORER_VERSION", "SoakScore", "SoakVerdictV2", "score_soak"]

#: The scorer contract.
SCORER_VERSION = "PAPER_SHADOW_SOAK_SCORER_V1"


class SoakVerdictV2:
    """The four states a soak can be in."""

    PASSED = "A_SOAK_PASSED"
    IN_PROGRESS = "B_SOAK_IN_PROGRESS"
    HALTED = "C_SOAK_HALTED_SEMANTIC_DEFECT_REQUIRES_RESTART"
    FAILED = "FAIL_SAFETY_VIOLATION"


@dataclass(frozen=True)
class SoakScore:
    """One mechanical evaluation of a soak.

    Attributes:
        verdict: One of `SoakVerdictV2`.
        gates: Every gate's outcome.
        counted_sessions: Sessions counted, each at most once.
        required_sessions: How many are needed.
        completed_cycles: Successful strategy decision cycles.
        state: The soak's recorded state.
        drill_rejections: Why any drill evidence was inadmissible.
        scorer_version: Which evaluator produced this.
        registry_fingerprint: Which gate registry it used.
    """

    verdict: str
    gates: tuple[GateOutcome, ...]
    counted_sessions: int
    required_sessions: int
    completed_cycles: int
    state: str
    drill_rejections: tuple[str, ...] = ()
    scorer_version: str = SCORER_VERSION
    registry_digest: str = field(default_factory=registry_fingerprint)

    @property
    def breaches(self) -> tuple[GateOutcome, ...]:
        """Zero-tolerance gates that failed. Any one of these means `FAIL`."""
        return tuple(row for row in self.gates if row.is_breach)

    @property
    def outstanding(self) -> tuple[GateOutcome, ...]:
        """Promotion gates not yet met. These keep the verdict at `B`."""
        return tuple(
            row
            for row in self.gates
            if not row.passed and row.gate_class is GateClass.PROMOTION_REQUIRED
        )

    @property
    def remaining_sessions(self) -> int:
        """How many more eligible sessions are needed."""
        return max(0, self.required_sessions - self.counted_sessions)

    def body(self) -> dict[str, Any]:
        """Return the score's semantic content."""
        return {
            "scorer_version": self.scorer_version,
            "gate_registry_fingerprint": self.registry_digest,
            "verdict": self.verdict,
            "state": self.state,
            "counted_sessions": self.counted_sessions,
            "required_sessions": self.required_sessions,
            "remaining_sessions": self.remaining_sessions,
            "completed_decision_cycles": self.completed_cycles,
            "gates": [row.as_json() for row in self.gates],
            "breaches": [row.gate_id for row in self.breaches],
            "outstanding": [row.gate_id for row in self.outstanding],
            "drill_rejections": list(self.drill_rejections),
            # Named rather than merely absent: a soak scored as a percentage is
            # the specific failure these gates exist to prevent.
            "pass_rate": None,
            "performance_measures_read": [],
        }

    def as_json(self) -> dict[str, Any]:
        """Return the score as a JSON-safe mapping."""
        return self.body()

    def summary_lines(self) -> tuple[str, ...]:
        """Render the score for a terminal."""
        lines = [
            f"verdict            : {self.verdict}",
            f"state              : {self.state}",
            f"scorer             : {self.scorer_version}",
            f"sessions           : {self.counted_sessions} of {self.required_sessions} "
            f"({self.remaining_sessions} remaining)",
            f"decision cycles    : {self.completed_cycles}",
        ]
        if self.breaches:
            lines.append("SAFETY BREACHES    :")
            lines += [
                f"    {row.gate_id}: observed {row.observed}, limit {row.limit}"
                for row in self.breaches
            ]
        if self.outstanding:
            lines.append("outstanding        :")
            lines += [
                f"    {row.gate_id}: observed {row.observed}, "
                f"{'at most' if row.bound.value == 'AT_MOST' else 'at least'} {row.limit}"
                for row in self.outstanding
            ]
        if self.drill_rejections:
            lines.append("inadmissible drills:")
            lines += [f"    {row}" for row in self.drill_rejections]
        return tuple(lines)


def score_soak(
    evidence: SoakEvidence,
    *,
    declared_gates: dict[str, int],
    required_sessions: int,
) -> SoakScore:
    """Score a soak mechanically against its frozen gates.

    Takes the declared gates as data and the evidence as immutable records.
    There is no threshold argument and no way to pass a kinder limit: every
    bound comes from the protocol that was frozen.

    Args:
        evidence: The immutable records.
        declared_gates: The protocol's gates, id to threshold.
        required_sessions: How many eligible sessions are needed.

    Returns:
        The score.

    Raises:
        SoakContractError: `SOAK_GATE_WITHOUT_EVALUATOR` or
            `SOAK_EVALUATOR_UNDECLARED` — the bijection is checked here as well
            as at arming, because a protocol frozen by an older build could
            otherwise be scored against a registry that has since drifted.
    """
    from .gates import require_bijection

    require_bijection(declared_gates)
    registry = gate_registry()
    outcomes = tuple(
        registry[gate_id].run(evidence, limit=limit)
        for gate_id, limit in sorted(declared_gates.items())
    )
    counted = len(evidence.counted_sessions())
    cycles = next(
        (row.observed for row in outcomes if row.gate_id == "successful_strategy_decision_cycles"),
        0,
    )
    state = str(evidence.soak_state())
    rejections = tuple(getattr(evidence, "drill_rejections", lambda: ())())

    breached = [row for row in outcomes if row.is_breach]
    if breached:
        verdict = SoakVerdictV2.FAILED
    elif state in {"HALTED", "FAILED"}:
        # Checked after breaches and before success: a halted soak can never
        # become A however clean the evidence collected before the halt was,
        # because the defect that halted it is a statement about the semantics
        # the evidence was gathered under.
        verdict = SoakVerdictV2.HALTED
    elif all(row.passed for row in outcomes):
        verdict = SoakVerdictV2.PASSED
    else:
        verdict = SoakVerdictV2.IN_PROGRESS

    return SoakScore(
        verdict=verdict,
        gates=outcomes,
        counted_sessions=counted,
        required_sessions=required_sessions,
        completed_cycles=cycles,
        state=state,
        drill_rejections=rejections,
    )
