"""Scoring the soak, mechanically, against gates frozen before the data existed.

Everything here is derived. The cumulative state is computed from the immutable
per-session observations plus the frozen protocol — there is no mutable summary
file that could drift from its own evidence, and no field anybody could edit to
make a soak look finished.

The scoring is deliberately joyless. `evaluate` counts violations and compares
each count to zero; it takes no arguments that could soften a threshold, and it
has no notion of a pass rate. Nineteen clean sessions and one duplicate order is
a failed gate. That is not pedantry: a duplicate order means the mechanism that
prevents duplicate orders does not work, and the nineteen days it happened not to
fire tell you nothing about the twentieth.

The verdict is likewise mechanical. `A` requires every zero-tolerance count at
zero, the full session requirement met, every drill passed, and no unresolved
blocking or critical incident. Anything short of that is `B` while sessions
remain, `C` when a defect halted it, and `FAIL` for a safety violation. There is
no path through this module that reaches `A` by judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .models import OperationsMode, RunStatus
from .soak import (
    REQUIRED_DRILLS,
    DrillType,
    PaperShadowSoakProtocol,
    SoakState,
)

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

    from .incidents import OperationalIncident
    from .schedule import PaperOperationsSchedule
    from .soak import PaperShadowSessionObservation, SoakStore

__all__ = [
    "PAPER_SHADOW_SOAK_REPORT_VERSION",
    "GateResult",
    "PaperShadowSoakReport",
    "SoakVerdict",
    "evaluate_soak",
]

#: The cumulative report contract.
PAPER_SHADOW_SOAK_REPORT_VERSION = "PAPER_SHADOW_SOAK_REPORT_V1"


class SoakVerdict:
    """The four possible soak verdicts, as stable strings."""

    PASSED = "A_PAPER_SHADOW_SOAK_PASSED"
    IN_PROGRESS = "B_SOAK_IN_PROGRESS"
    HALTED = "C_SOAK_HALTED_DEFECT_REQUIRES_RESTART"
    FAILED = "FAIL_SAFETY_VIOLATION"


@dataclass(frozen=True)
class GateResult:
    """One promotion gate and what the evidence said.

    Attributes:
        name: Which gate.
        limit: The frozen threshold. Zero for every zero-tolerance gate.
        observed: What the evidence counted.
        passed: Whether it holds.
        zero_tolerance: Whether a single violation fails the soak outright.
        detail: Which sessions or drills contributed.
    """

    name: str
    limit: int
    observed: int
    passed: bool
    zero_tolerance: bool = True
    detail: tuple[str, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        """Return the gate as a JSON-safe mapping."""
        return {
            "name": self.name,
            "limit": self.limit,
            "observed": self.observed,
            "passed": self.passed,
            "zero_tolerance": self.zero_tolerance,
            "detail": list(self.detail),
        }


@dataclass(frozen=True)
class PaperShadowSoakReport:
    """The cumulative soak status, derived from immutable evidence.

    Attributes:
        protocol: The frozen rules.
        state: The durable soak state.
        activated_at: When the rules became binding.
        first_eligible_session: The prospective boundary.
        eligible_sessions: Eligible sessions whose windows have closed.
        observations: The qualifying per-session evidence.
        missing_sessions: Eligible sessions with no record at all.
        gates: Every promotion gate and its result.
        drills_passed: Which required drills have passed.
        open_critical: Unresolved critical incidents.
        open_blocking: Unresolved blocking incidents.
        verdict: The mechanical verdict.
    """

    protocol: PaperShadowSoakProtocol
    state: SoakState
    activated_at: str
    first_eligible_session: str
    eligible_sessions: tuple[str, ...]
    observations: tuple[PaperShadowSessionObservation, ...]
    missing_sessions: tuple[str, ...]
    gates: tuple[GateResult, ...]
    drills_passed: frozenset[DrillType]
    open_critical: tuple[str, ...]
    open_blocking: tuple[str, ...]
    verdict: str

    # -- derived session counts ---------------------------------------------

    @property
    def observed(self) -> int:
        """How many qualifying sessions have evidence."""
        return len(self.observations)

    @property
    def remaining(self) -> int:
        """How many qualifying sessions are still needed."""
        return max(0, self.protocol.required_sessions - self.observed)

    def _count(self, *statuses: RunStatus) -> int:
        """Count observations ending in any of some statuses.

        Args:
            statuses: Which statuses.

        Returns:
            The count.
        """
        return sum(1 for row in self.observations if row.run_status in statuses)

    @property
    def paper_executions(self) -> int:
        """Sessions that submitted at least one order."""
        return sum(1 for row in self.observations if row.orders_submitted)

    @property
    def observe_only(self) -> int:
        """Sessions that ran in observe-only mode."""
        return sum(1 for row in self.observations if row.mode is OperationsMode.OBSERVE_ONLY)

    @property
    def blocked(self) -> int:
        """Sessions a gate refused."""
        return self._count(RunStatus.BLOCKED)

    @property
    def missed(self) -> int:
        """Sessions recorded as missed."""
        return self._count(RunStatus.MISSED)

    @property
    def recovered(self) -> int:
        """Sessions that resumed an earlier run."""
        return self._count(RunStatus.RECOVERED)

    @property
    def killed(self) -> int:
        """Sessions the kill switch stopped."""
        return self._count(RunStatus.KILLED)

    @property
    def total_orders(self) -> int:
        """Remote orders submitted across the soak."""
        return sum(row.orders_submitted for row in self.observations)

    @property
    def total_fills(self) -> int:
        """Executions observed across the soak."""
        return sum(row.fills_observed for row in self.observations)

    @property
    def failed_gates(self) -> tuple[GateResult, ...]:
        """Every gate that did not hold."""
        return tuple(gate for gate in self.gates if not gate.passed)

    @property
    def failed_zero_tolerance(self) -> tuple[GateResult, ...]:
        """Every zero-tolerance gate that was violated."""
        return tuple(gate for gate in self.failed_gates if gate.zero_tolerance)

    def body(self) -> dict[str, Any]:
        """Return the report's semantic content."""
        return {
            "version": PAPER_SHADOW_SOAK_REPORT_VERSION,
            "protocol_fingerprint": self.protocol.fingerprint(),
            "protocol_id": self.protocol.protocol_id,
            "qualifying_code_commit": self.protocol.code_commit,
            "state": str(self.state),
            "verdict": self.verdict,
            "activated_at": self.activated_at,
            "first_eligible_session": self.first_eligible_session,
            "required_sessions": self.protocol.required_sessions,
            "eligible_sessions_elapsed": len(self.eligible_sessions),
            "qualifying_observations": self.observed,
            "remaining": self.remaining,
            "sessions": {
                "paper_execution": self.paper_executions,
                "observe_only": self.observe_only,
                "blocked": self.blocked,
                "missed": self.missed,
                "recovered": self.recovered,
                "killed": self.killed,
            },
            "missing_sessions": list(self.missing_sessions),
            "execution_totals": {
                "remote_orders": self.total_orders,
                "fills_observed": self.total_fills,
            },
            "drills": {
                "required": [str(drill) for drill in REQUIRED_DRILLS],
                "passed": sorted(str(drill) for drill in self.drills_passed),
                "outstanding": sorted(
                    str(drill) for drill in REQUIRED_DRILLS if drill not in self.drills_passed
                ),
            },
            "incidents": {
                "open_critical": list(self.open_critical),
                "open_blocking": list(self.open_blocking),
            },
            "gates": [gate.as_json() for gate in self.gates],
            "failed_gates": [gate.name for gate in self.failed_gates],
            "observation_fingerprints": [row.fingerprint() for row in self.observations],
            "not_gated_on": self.protocol.promotion_gates["not_gated_on"],
            "counting": self.protocol.promotion_gates["counting"],
            "live_trading": "NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the soak: protocol, every observation, drills and gates."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the report with its own fingerprint."""
        return {**self.body(), "soak_fingerprint": self.fingerprint()}

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal."""
        lines = [
            f"verdict              : {self.verdict}",
            f"state                : {self.state}",
            f"protocol             : {self.protocol.protocol_id} "
            f"({self.protocol.fingerprint()[:16]})",
            f"qualifying commit    : {self.protocol.code_commit[:12]}",
            f"activated            : {self.activated_at}",
            f"first eligible       : {self.first_eligible_session}",
            f"sessions             : {self.observed} of "
            f"{self.protocol.required_sessions} ({self.remaining} remaining)",
            f"eligible elapsed     : {len(self.eligible_sessions)}",
            f"  paper execution    : {self.paper_executions}",
            f"  observe-only       : {self.observe_only}",
            f"  blocked            : {self.blocked}",
            f"  missed             : {self.missed}",
            f"  recovered          : {self.recovered}",
            f"remote orders        : {self.total_orders}",
            f"fills observed       : {self.total_fills}",
            f"drills passed        : {len(self.drills_passed)} of {len(REQUIRED_DRILLS)}",
            f"open critical        : {len(self.open_critical)}",
            f"open blocking        : {len(self.open_blocking)}",
        ]
        for session in self.missing_sessions:
            lines.append(f"  NO RECORD: {session} — an eligible session with no observation")
        for gate in self.failed_gates:
            lines.append(f"  GATE FAILED: {gate.name} observed {gate.observed}, limit {gate.limit}")
        for drill in sorted(
            str(drill) for drill in REQUIRED_DRILLS if drill not in self.drills_passed
        ):
            lines.append(f"  DRILL OUTSTANDING: {drill}")
        lines.append("live trading         : NOT_AUTHORIZED")
        return tuple(lines)


def evaluate_soak(
    store: SoakStore,
    *,
    calendar: ExecutionCalendar,
    schedule: PaperOperationsSchedule,
    now: str,
    incidents: tuple[OperationalIncident, ...] = (),
) -> PaperShadowSoakReport:
    """Score the soak mechanically against its frozen gates.

    Takes no thresholds: every limit comes from the protocol, which was frozen
    before any observation existed. There is deliberately no parameter that
    could waive a gate, and no pass-rate anywhere — a single duplicate order
    means the mechanism that prevents duplicate orders does not work, and the
    clean sessions around it are not evidence that it does.

    Args:
        store: The soak evidence.
        calendar: The frozen session calendar.
        schedule: The schedule that produces slots.
        now: The operational instant.
        incidents: The current incident set.

    Returns:
        The cumulative report, derived entirely from immutable evidence.

    Raises:
        OperationsError: `SOAK_NOT_ACTIVATED` if no protocol is frozen or no
            activation exists — there is nothing to score.
    """
    from .errors import Code, OperationsError

    protocol = store.protocol()
    activation = store.activation()
    if protocol is None or activation is None:
        raise OperationsError(
            Code.SOAK_NOT_ACTIVATED,
            "no soak protocol is frozen and activated, so there is nothing to score",
        )

    observations = store.qualifying_observations()
    eligible = store.eligible_sessions(calendar=calendar, schedule=schedule, now=now)
    missing = store.missing_sessions(calendar=calendar, schedule=schedule, now=now)
    drills_passed = store.passed_drills()

    open_critical = tuple(
        incident.incident_id
        for incident in incidents
        if incident.is_open and str(incident.severity) == "CRITICAL"
    )
    open_blocking = tuple(
        incident.incident_id
        for incident in incidents
        if incident.is_open and str(incident.severity) == "BLOCKING"
    )

    # Every violation is counted from the observations themselves, so a gate
    # cannot be satisfied by a summary that disagrees with its own evidence.
    def sessions_where(predicate: Any) -> tuple[str, ...]:
        return tuple(row.session for row in observations if predicate(row))

    duplicate_orders = sessions_where(
        lambda row: len(row.provider_order_ids) != len(set(row.provider_order_ids))
    )
    duplicate_events = sessions_where(
        lambda row: len(row.accounting_events) != len(set(row.accounting_events))
    )
    duplicate_fills = sessions_where(
        lambda row: row.fills_observed > 0 and len(row.accounting_events) > row.fills_observed
    )
    stale_executions = sessions_where(lambda row: not row.inputs_fresh and row.orders_submitted > 0)
    backfilled = sessions_where(
        lambda row: row.run_status is RunStatus.MISSED and row.orders_submitted > 0
    )
    ignored_breaks = sessions_where(
        lambda row: row.orders_submitted > 0 and row.reconciliation_after != "RECONCILED"
    )
    unreconciled_completed = sessions_where(
        lambda row: (
            row.run_status in {RunStatus.COMPLETED_PAPER_EXECUTION, RunStatus.COMPLETED_NO_ACTION}
            and not row.reconciled_cleanly
        )
    )
    duplicate_runs = tuple(
        session
        for session in {row.session for row in observations}
        if sum(1 for row in observations if row.session == session) > 1
    )
    unexpected_day_orders = sessions_where(lambda row: bool(row.unexpected_open_day_orders))
    unexpected_positions = sessions_where(lambda row: bool(row.unexpected_positions))

    gates: list[GateResult] = [
        GateResult(
            name="duplicate_remote_orders",
            limit=0,
            observed=len(duplicate_orders),
            passed=not duplicate_orders,
            detail=duplicate_orders,
        ),
        GateResult(
            name="duplicate_accounting_events",
            limit=0,
            observed=len(duplicate_events),
            passed=not duplicate_events,
            detail=duplicate_events,
        ),
        GateResult(
            name="duplicate_admitted_fills",
            limit=0,
            observed=len(duplicate_fills),
            passed=not duplicate_fills,
            detail=duplicate_fills,
        ),
        GateResult(
            name="duplicate_scheduled_runs_causing_execution",
            limit=0,
            observed=len(duplicate_runs),
            passed=not duplicate_runs,
            detail=duplicate_runs,
        ),
        GateResult(
            name="stale_data_executions",
            limit=0,
            observed=len(stale_executions),
            passed=not stale_executions,
            detail=stale_executions,
        ),
        GateResult(
            name="missed_run_backfills",
            limit=0,
            observed=len(backfilled),
            passed=not backfilled,
            detail=backfilled,
        ),
        GateResult(
            name="ignored_reconciliation_breaks",
            limit=0,
            observed=len(ignored_breaks),
            passed=not ignored_breaks,
            detail=ignored_breaks,
        ),
        GateResult(
            name="completed_sessions_reconcile",
            limit=0,
            observed=len(unreconciled_completed),
            passed=not unreconciled_completed,
            detail=unreconciled_completed,
        ),
        GateResult(
            name="unexpected_open_day_orders",
            limit=0,
            observed=len(unexpected_day_orders),
            passed=not unexpected_day_orders,
            detail=unexpected_day_orders,
        ),
        GateResult(
            name="unexpected_remote_positions",
            limit=0,
            observed=len(unexpected_positions),
            passed=not unexpected_positions,
            detail=unexpected_positions,
        ),
        # The next three block an A without being safety violations. A closed
        # window with no record is missing evidence, and an open incident is the
        # system correctly refusing to trade — neither is the system having done
        # something unsafe, which is what FAIL is reserved for. Marking them
        # zero-tolerance would report an in-progress soak with one unrecorded
        # session as a safety breach, which is both wrong and the kind of noise
        # that gets real breaches ignored.
        GateResult(
            name="eligible_sessions_with_no_record",
            limit=0,
            observed=len(missing),
            passed=not missing,
            zero_tolerance=False,
            detail=missing,
        ),
        GateResult(
            name="unresolved_critical_incidents",
            limit=0,
            observed=len(open_critical),
            passed=not open_critical,
            zero_tolerance=False,
            detail=open_critical,
        ),
        GateResult(
            name="unresolved_blocking_incidents",
            limit=0,
            observed=len(open_blocking),
            passed=not open_blocking,
            zero_tolerance=False,
            detail=open_blocking,
        ),
        GateResult(
            name="required_drills_passed",
            limit=len(REQUIRED_DRILLS),
            observed=len(drills_passed),
            passed=drills_passed >= frozenset(REQUIRED_DRILLS),
            zero_tolerance=False,
            detail=tuple(
                sorted(str(drill) for drill in REQUIRED_DRILLS if drill not in drills_passed)
            ),
        ),
        GateResult(
            name="qualifying_sessions_observed",
            limit=protocol.required_sessions,
            observed=len(observations),
            passed=len(observations) >= protocol.required_sessions,
            zero_tolerance=False,
            detail=(),
        ),
    ]

    state = store.state()
    failed_zero = [gate for gate in gates if not gate.passed and gate.zero_tolerance]
    if failed_zero:
        verdict = SoakVerdict.FAILED
    elif state is SoakState.HALTED:
        verdict = SoakVerdict.HALTED
    elif all(gate.passed for gate in gates):
        verdict = SoakVerdict.PASSED
    else:
        verdict = SoakVerdict.IN_PROGRESS

    return PaperShadowSoakReport(
        protocol=protocol,
        state=state,
        activated_at=activation.activated_at,
        first_eligible_session=activation.first_eligible_session,
        eligible_sessions=eligible,
        observations=observations,
        missing_sessions=missing,
        gates=tuple(gates),
        drills_passed=drills_passed,
        open_critical=open_critical,
        open_blocking=open_blocking,
        verdict=verdict,
    )
