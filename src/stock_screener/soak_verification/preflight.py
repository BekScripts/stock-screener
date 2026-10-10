"""The pre-arming self-check: proof that a protocol can be scored at all.

This is the module whose absence sealed an unscoreable soak. V2 declared
twenty-two gates, was frozen, tagged, armed against a real paper account, and
could not be evaluated by anything — and nothing objected, because a protocol
is just data and data does not fail to load.

So arming now has a prerequisite that is itself mechanical. The preflight does
not ask whether the protocol looks reasonable. It instantiates the real scorer
against the real store and proves four things by construction:

1. every declared gate has exactly one evaluator, and every evaluator the
   scorer knows about is declared — a check missing in either direction is a
   safety check silently switched off;
2. the store can actually supply every kind of evidence the gates read, by
   scoring an empty store and getting a verdict instead of an `AttributeError`;
3. every drill type the protocol requires can be recorded and evaluated;
4. all four verdicts are reachable under this protocol — including `A`, because
   a protocol whose success state is mechanically impossible is worse than no
   protocol: it collects twenty sessions of evidence toward a conclusion
   nothing can draw.

There is no `--force` and no `--skip-preflight`. An escape hatch here would
re-create exactly the failure this exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .drills_v2 import DRILL_EVIDENCE_V2_VERSION, DrillRecordV2, DrillTypeV2
from .errors import Code, SoakContractError
from .gates import GATE_REGISTRY_VERSION, registry_fingerprint
from .scorer import SCORER_VERSION, SoakVerdictV2, score_soak

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "PREFLIGHT_VERSION",
    "SoakPreflightReport",
    "run_soak_preflight",
]

#: The preflight artefact contract.
PREFLIGHT_VERSION = "PAPER_SHADOW_SOAK_PREFLIGHT_V1"


@dataclass
class _SyntheticObservation:
    """One synthetic session, for proving a verdict is reachable.

    Every counter a gate reads defaults to zero, so a fixture states only what
    it is exercising. That matters: a fixture that had to set twenty fields to
    prove `A` is reachable would mostly be testing the fixture.
    """

    session: str
    decision_created: bool = False
    decision_fingerprint: str = ""
    executed_decision_fingerprint: str = ""
    missed_purposes: tuple[str, ...] = ()
    orders_submitted: int = 0
    reconciliation_after: str = "RECONCILED"
    live_requests: int = 0
    duplicate_remote_orders: int = 0
    duplicate_admitted_fills: int = 0
    duplicate_accounting_events: int = 0
    duplicate_scheduled_runs_causing_execution: int = 0
    stale_data_executions: int = 0
    missing_data_executions: int = 0
    missed_run_backfills: int = 0
    ignored_reconciliation_breaks: int = 0
    ignored_remote_state_unknown: int = 0
    kill_bypasses: int = 0
    unknown_remote_order_auto_cancels: int = 0
    automatic_accounting_repairs: int = 0
    reranked_executions: int = 0
    risk_authority_expansions: int = 0


@dataclass
class _SyntheticEvidence:
    """An immutable evidence set assembled for one reachability proof."""

    observations: tuple[_SyntheticObservation, ...] = ()
    drills: tuple[DrillRecordV2, ...] = ()
    incidents: tuple[Any, ...] = ()
    elapsed: tuple[str, ...] = ()
    state: str = "RUNNING"

    def live_observations(self) -> tuple[Any, ...]:
        """Session observations that are not superseded."""
        return self.observations

    def counted_sessions(self) -> tuple[str, ...]:
        """Each session counted at most once."""
        return tuple(sorted({row.session for row in self.observations}))

    def drill_records(self) -> tuple[Any, ...]:
        """Drill evidence."""
        return self.drills

    def open_incidents(self) -> tuple[Any, ...]:
        """Incidents still open."""
        return self.incidents

    def eligible_elapsed_sessions(self) -> tuple[str, ...]:
        """Eligible sessions whose close has passed."""
        return self.elapsed

    def soak_state(self) -> str:
        """The recorded state."""
        return self.state

    def drill_rejections(self) -> tuple[str, ...]:
        """Why any drill was inadmissible."""
        return ()


@dataclass(frozen=True)
class SoakPreflightReport:
    """What the self-check established.

    Attributes:
        protocol_fingerprint: Which protocol was checked.
        qualifying_commit: Which build.
        passed: Whether arming may proceed.
        checks: Each check and its result.
        failures: Why it refused, where it did.
    """

    protocol_fingerprint: str
    qualifying_commit: str
    passed: bool
    checks: tuple[tuple[str, bool, str], ...] = ()
    failures: tuple[str, ...] = ()
    declared_gates: tuple[str, ...] = ()
    supported_drill_types: tuple[str, ...] = ()
    reachable_verdicts: tuple[str, ...] = ()
    limitations: tuple[str, ...] = field(
        default_factory=lambda: (
            "A reachability proof uses synthetic evidence. It establishes that the "
            "protocol's verdicts are mechanically obtainable, not that the real soak "
            "will obtain them.",
            "It cannot establish that a gate's evaluator measures what its name "
            "claims. That is what the scorer's own tests are for.",
        )
    )

    def body(self) -> dict[str, Any]:
        """Return the artefact's semantic content."""
        return {
            "version": PREFLIGHT_VERSION,
            "scorer_version": SCORER_VERSION,
            "gate_registry_version": GATE_REGISTRY_VERSION,
            "gate_registry_fingerprint": registry_fingerprint(),
            "drill_evidence_version": DRILL_EVIDENCE_V2_VERSION,
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "declared_gates": list(self.declared_gates),
            "supported_drill_types": list(self.supported_drill_types),
            "reachable_verdicts": list(self.reachable_verdicts),
            "checks": [
                {"check": name, "passed": ok, "detail": detail} for name, ok, detail in self.checks
            ],
            "passed": self.passed,
            "failures": list(self.failures),
            "escape_hatch": "NONE — there is no force, skip or ignore option",
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the artefact."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artefact with its fingerprint."""
        return {**self.body(), "preflight_fingerprint": self.fingerprint()}

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal."""
        lines = [
            f"preflight          : {PREFLIGHT_VERSION}",
            f"scorer             : {SCORER_VERSION}",
            f"gate registry      : {registry_fingerprint()[:16]}…",
            f"declared gates     : {len(self.declared_gates)}",
            f"reachable verdicts : {', '.join(self.reachable_verdicts) or 'none'}",
        ]
        lines += [
            f"    [{'pass' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
            for name, ok, detail in self.checks
        ]
        lines.append(f"result             : {'PASSED' if self.passed else 'REFUSED'}")
        return tuple(lines)

    def require(self) -> None:
        """Refuse arming unless the self-check passed.

        Raises:
            SoakContractError: `SOAK_PREFLIGHT_FAILED`.
        """
        if not self.passed:
            raise SoakContractError(
                Code.SOAK_PREFLIGHT_FAILED,
                "the protocol cannot be armed: "
                + "; ".join(self.failures)
                + ". There is no force option — arming an unscoreable protocol is the "
                "failure this check exists to prevent",
            )


def run_soak_preflight(
    *,
    protocol_fingerprint: str,
    qualifying_commit: str,
    declared_gates: dict[str, int],
    required_sessions: int,
    required_drill_types: Sequence[str],
    operations_engine_version: str,
    operations_policy_fingerprint: str,
) -> SoakPreflightReport:
    """Prove a protocol can be scored, before it is frozen and armed.

    Args:
        protocol_fingerprint: The protocol being checked.
        qualifying_commit: The build it judges.
        declared_gates: Its gates, id to threshold.
        required_sessions: How many sessions it requires.
        required_drill_types: Which drills it requires.
        operations_engine_version: The control plane it binds.
        operations_policy_fingerprint: The operational rules it binds.

    Returns:
        The report. `require()` turns a refusal into an exception; the report
        itself is returned either way so a caller can print what failed.
    """
    checks: list[tuple[str, bool, str]] = []
    failures: list[str] = []
    reachable: list[str] = []

    # 1. Gate/evaluator bijection, both directions.
    try:
        from .gates import require_bijection

        require_bijection(declared_gates)
        checks.append(("gate_evaluator_bijection", True, f"{len(declared_gates)} gates"))
    except SoakContractError as exc:
        checks.append(("gate_evaluator_bijection", False, str(exc)))
        failures.append(str(exc))

    # 2. Drill support, before anything depends on it.
    registry_types = {str(row) for row in DrillTypeV2}
    unsupported = sorted(set(map(str, required_drill_types)) - registry_types)
    if unsupported:
        detail = f"unsupported drill types {unsupported}"
        checks.append(("drill_types_supported", False, detail))
        failures.append(detail)
    else:
        checks.append(("drill_types_supported", True, f"{len(required_drill_types)} types"))

    # 3. Scorer/store compatibility: an empty store must score, not raise.
    if not failures:
        try:
            empty = score_soak(
                _SyntheticEvidence(state="ARMED"),
                declared_gates=declared_gates,
                required_sessions=required_sessions,
            )
            ok = empty.verdict == SoakVerdictV2.IN_PROGRESS
            checks.append(
                (
                    "empty_store_scores_in_progress",
                    ok,
                    f"verdict {empty.verdict}, 0 of {required_sessions}",
                )
            )
            if ok:
                reachable.append(SoakVerdictV2.IN_PROGRESS)
            else:
                failures.append(f"an empty soak scored {empty.verdict}, expected in-progress")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            checks.append(("empty_store_scores_in_progress", False, detail))
            failures.append(f"the scorer cannot evaluate an empty store — {detail}")

    # 4. Verdict reachability, including A.
    if not failures:
        for name, builder in (
            (SoakVerdictV2.PASSED, _passing_fixture),
            (SoakVerdictV2.FAILED, _breaching_fixture),
            (SoakVerdictV2.HALTED, _halted_fixture),
        ):
            try:
                score = score_soak(
                    builder(
                        required_sessions=required_sessions,
                        protocol_fingerprint=protocol_fingerprint,
                        qualifying_commit=qualifying_commit,
                        operations_engine_version=operations_engine_version,
                        operations_policy_fingerprint=operations_policy_fingerprint,
                        required_drill_types=required_drill_types,
                    ),
                    declared_gates=declared_gates,
                    required_sessions=required_sessions,
                )
                ok = score.verdict == name
                checks.append((f"verdict_reachable_{name}", ok, f"got {score.verdict}"))
                if ok:
                    reachable.append(name)
                else:
                    failures.append(
                        f"{name} is not mechanically reachable under this protocol; a "
                        f"synthetic fixture built to produce it scored {score.verdict}"
                    )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                checks.append((f"verdict_reachable_{name}", False, detail))
                failures.append(detail)

    return SoakPreflightReport(
        protocol_fingerprint=protocol_fingerprint,
        qualifying_commit=qualifying_commit,
        passed=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
        declared_gates=tuple(sorted(declared_gates)),
        supported_drill_types=tuple(sorted(registry_types)),
        reachable_verdicts=tuple(reachable),
    )


def _drill_set(
    *,
    protocol_fingerprint: str,
    qualifying_commit: str,
    operations_engine_version: str,
    operations_policy_fingerprint: str,
    required_drill_types: Sequence[str],
) -> tuple[DrillRecordV2, ...]:
    """Build a passing, fully bound drill record for each required type."""
    return tuple(
        DrillRecordV2(
            drill_type=DrillTypeV2(str(row)),
            protocol_fingerprint=protocol_fingerprint,
            qualifying_commit=qualifying_commit,
            operations_engine_version=operations_engine_version,
            operations_policy_fingerprint=operations_policy_fingerprint,
            performed_at="2026-01-01T00:00:00+00:00",
            pre_state_fingerprint="synthetic",
            action="synthetic reachability fixture",
            expected="synthetic",
            observed="synthetic",
            post_state_fingerprint="synthetic",
            passed=True,
        )
        for row in required_drill_types
    )


def _passing_fixture(**kwargs: Any) -> _SyntheticEvidence:
    """Evidence that should score `A`: full sessions, a real cycle, all drills.

    The decision cycle is built as one fingerprint created in one session and
    consumed in another, which is what the cycle evaluator actually requires —
    not two independent flags that could both be set without a cycle existing.
    """
    required = int(kwargs["required_sessions"])
    digest = "decision-" + "f" * 56
    sessions = [_SyntheticObservation(session=f"S{index:03d}") for index in range(required)]
    sessions[0] = _SyntheticObservation(
        session="S000", decision_created=True, decision_fingerprint=digest
    )
    sessions[1] = _SyntheticObservation(session="S001", executed_decision_fingerprint=digest)
    return _SyntheticEvidence(
        observations=tuple(sessions),
        drills=_drill_set(**{k: v for k, v in kwargs.items() if k != "required_sessions"}),
        elapsed=tuple(row.session for row in sessions),
        state="RUNNING",
    )


def _breaching_fixture(**kwargs: Any) -> _SyntheticEvidence:
    """Evidence with one duplicate remote order, which must score `FAIL`.

    Otherwise identical to the passing fixture: nineteen clean sessions and one
    breach must not average out to a pass.
    """
    evidence = _passing_fixture(**kwargs)
    observations = list(evidence.observations)
    observations[-1] = _SyntheticObservation(
        session=observations[-1].session, duplicate_remote_orders=1
    )
    evidence.observations = tuple(observations)
    return evidence


def _halted_fixture(**kwargs: Any) -> _SyntheticEvidence:
    """Clean evidence under a halted state, which must score `C`, never `A`."""
    evidence = _passing_fixture(**kwargs)
    evidence.state = "HALTED"
    return evidence
