"""The run pipeline: recover, gate, act, verify — in that order, every time.

This is where the package's rules meet. The sequence is not a convenience; each
step exists because the step after it would be unsafe without it.

**Recover before deciding.** A process that starts and immediately plans has no
idea whether the previous run submitted anything. So the journal is read, the
run id is derived, and an existing run for the same slot is resumed rather than
duplicated.

**Gate before acting, and gate again before every order.** Health at T0 says
nothing about T0 plus ten minutes. The kill switch is re-read before each
submission — not cached — because the entire value of a persistent switch is
that the *next* check sees it. An operator who kills mid-run must not have to
wait for the run to finish.

**Never substitute.** A missing input is not zero, a stale input is not fresh
enough, an unreachable venue is not a reason to use the local simulator, and a
missed slot is not something to run late. Each of those substitutions would make
a run succeed where it should have stopped, which is the only kind of bug that
matters here.

The coordinator has no authority of its own. It does not choose targets, size
orders, or decide what a fill means — it asks the existing layers and records
what they said. The callbacks it takes are how that stays true: planning and
submission are supplied by the caller, so this module cannot invent either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .errors import Code, OperationsError
from .health import HealthCheck, HealthState, PaperOperationsHealthReport
from .incidents import IncidentSeverity, IncidentStore
from .kill_switch import KillState, KillSwitchStore
from .leases import RunLeaseStore
from .models import OperationsMode, RunState, RunStatus
from .reports import PaperOperationsRunReport, paper_operations_manifest
from .run_journal import OperationsRunJournal, RunEventType

if TYPE_CHECKING:
    from .arming import ArmingStore
    from .clock import Clock
    from .models import OperationalInput
    from .schedule import PaperOperationsSchedule, ScheduledSlot

__all__ = [
    "OperationsEnvironment",
    "PaperOperationsCoordinator",
    "RunOutcome",
    "SubmissionGate",
]


class OperationsEnvironment(Protocol):
    """What the coordinator asks the rest of the system.

    A protocol rather than a concrete dependency, so this module cannot reach
    into execution or accounting and cannot become a second authority over
    either. Everything it learns, it learns by asking.
    """

    def verify_local_integrity(self) -> dict[str, Any]:
        """Verify the execution journal and the accounting ledger.

        Returns:
            What was established.

        Raises:
            Exception: If either does not verify. The coordinator treats that as
                critical: a ledger that does not verify means the system has
                lost track of what is true.
        """
        ...

    def prove_paper_environment(self) -> dict[str, Any]:
        """Prove the venue is the certified paper account.

        Returns:
            The proof, including the account binding fingerprint.
        """
        ...

    def reconcile(self) -> dict[str, Any]:
        """Run the three-way reconciliation.

        Returns:
            The verdict and findings.
        """
        ...

    def required_inputs(self) -> tuple[OperationalInput, ...]:
        """The inputs this run depends on, with their freshness declared.

        Returns:
            The inputs. An empty tuple means the run declares none, which is
            different from a missing required input.
        """
        ...

    def accounting_state_fingerprint(self) -> str:
        """The book's current state fingerprint.

        Returns:
            The fingerprint.
        """
        ...


@dataclass
class SubmissionGate:
    """The checks that run immediately before every single order.

    Separate from the run-level health check on purpose. A run-level check
    establishes that the system was fit to begin; this establishes that it is
    still fit *now*, one order later. The two are different claims and the
    second is the one that catches an operator killing a run mid-flight.

    Attributes:
        kill_store: The persistent control state. Re-read, never cached.
        incidents: The incident store, re-read for new blocking findings.
        checks_performed: How many times this gate ran. Reported so a test can
            assert it ran once per order rather than once per run.
    """

    kill_store: KillSwitchStore
    incidents: IncidentStore
    checks_performed: int = 0

    def require(self, *, context: str, expected_account: str) -> None:
        """Refuse a submission unless every immediate condition still holds.

        Args:
            context: What is being submitted.
            expected_account: The account binding the run is bound to.

        Raises:
            OperationsError: `PAPER_OPERATIONS_KILLED`,
                `PAPER_OPERATIONS_PAUSED`,
                `KILLED_WITH_UNRESOLVED_REMOTE_ORDER`,
                `OPERATIONS_ACCOUNT_BINDING_MISMATCH` or
                `BLOCKED_OPEN_INCIDENT`.
        """
        self.checks_performed += 1
        switch = self.kill_store.current()
        switch.require_submission_permitted(context=context)
        if (
            switch.account_binding_fingerprint
            and switch.account_binding_fingerprint != expected_account
        ):
            raise OperationsError(
                Code.OPERATIONS_ACCOUNT_BINDING_MISMATCH,
                f"{context}: the control state governs a different account binding "
                "than this run is bound to",
            )
        blocking = self.incidents.blocking()
        if blocking:
            raise OperationsError(
                Code.BLOCKED_OPEN_INCIDENT,
                f"{context}: {len(blocking)} blocking incident(s) are open "
                f"({[incident.reason_code for incident in blocking]}). A run that was "
                "healthy when it started is not thereby healthy now",
            )


@dataclass
class RunOutcome:
    """What one coordinated run produced.

    Attributes:
        report: The run report.
        manifest: The operational manifest.
        health: The gate's verdict.
        recovered: Whether this invocation resumed an existing run rather than
            starting one.
    """

    report: PaperOperationsRunReport
    manifest: dict[str, Any]
    health: PaperOperationsHealthReport | None = None
    recovered: bool = False

    @property
    def external_mutations(self) -> int:
        """How many mutating requests the run made."""
        return self.report.external_mutations

    def as_json(self) -> dict[str, Any]:
        """Return the outcome as a JSON-safe mapping."""
        return {
            "report": self.report.as_json(),
            "manifest": self.manifest,
            "health": self.health.as_json() if self.health else None,
            "recovered": self.recovered,
        }


@dataclass
class PaperOperationsCoordinator:
    """Runs a scheduled operational slot, gating every step.

    Attributes:
        root: The operational root.
        clock: The operational clock.
        schedule: When runs are due.
        environment: How to ask the rest of the system what is true.
        code_commit: Which build is running.
    """

    root: Any
    clock: Clock
    schedule: PaperOperationsSchedule
    environment: OperationsEnvironment
    arming: ArmingStore
    code_commit: str = ""
    _journal: OperationsRunJournal = field(init=False)
    _kill: KillSwitchStore = field(init=False)
    _incidents: IncidentStore = field(init=False)
    _leases: RunLeaseStore = field(init=False)

    def __post_init__(self) -> None:
        """Open the operational stores."""
        self._journal = OperationsRunJournal(self.root)
        self._kill = KillSwitchStore(self.root)
        self._incidents = IncidentStore(self.root)
        self._leases = RunLeaseStore(self.root)

    # -- accessors -----------------------------------------------------------

    @property
    def journal(self) -> OperationsRunJournal:
        """The append-only operational journal."""
        return self._journal

    @property
    def kill_store(self) -> KillSwitchStore:
        """The persistent control state."""
        return self._kill

    @property
    def incidents(self) -> IncidentStore:
        """The incident store."""
        return self._incidents

    @property
    def leases(self) -> RunLeaseStore:
        """The run lease store."""
        return self._leases

    # -- the pipeline --------------------------------------------------------

    def run_due(
        self,
        *,
        mode: OperationsMode,
        plan_and_submit: Any = None,
    ) -> RunOutcome:
        """Run the slot that is due now, if one is, gating every step.

        Args:
            mode: The mode to run in. `OBSERVE_ONLY` makes zero mutations.
            plan_and_submit: A callable the caller supplies to do the planning
                and submission, given a `SubmissionGate`. Supplied rather than
                implemented here so the coordinator cannot invent a target, an
                order or a fill — it gates, and somebody else decides.

        Returns:
            The outcome. A blocked or missed run is a normal outcome, reported
            rather than raised: a gate refusing is the system working.
        """
        now = self.clock.iso()
        slot = self.schedule.slot_for(now)
        if slot is None:
            return self._missed_or_not_due(now=now, mode=mode)

        account = self._account_fingerprint()
        run_id = slot.run_id(account_binding_fingerprint=account, mode=mode)

        # Idempotency first: a second invocation for the same slot must find the
        # first run, not become a competitor.
        recorded = self._journal.last_state_of(run_id)
        if recorded in {RunState.COMPLETED, RunState.BLOCKED, RunState.KILLED, RunState.MISSED}:
            return self._already_finished(run_id=run_id, slot=slot, mode=mode, state=recorded)

        return self._execute(
            run_id=run_id,
            slot=slot,
            mode=mode,
            account=account,
            now=now,
            resumed=recorded is not None,
            plan_and_submit=plan_and_submit,
        )

    def _account_fingerprint(self) -> str:
        """The account binding this coordinator operates on.

        Returns:
            The fingerprint, from the paper proof rather than from
            configuration.
        """
        try:
            proof = self.environment.prove_paper_environment()
        except Exception:
            return ""
        return str(proof.get("account_binding_fingerprint", ""))

    def _missed_or_not_due(self, *, now: str, mode: OperationsMode) -> RunOutcome:
        """Record a missed slot, or report that nothing is due.

        A missed slot is recorded and never run. An order planned against
        yesterday's prices and risk state is not a late version of yesterday's
        decision — it is a new, unreviewed one wearing an old authorisation.

        Args:
            now: The operational instant.
            mode: The mode that was requested.

        Returns:
            The outcome.
        """
        missed = self.schedule.missed_slot_for(now)
        if missed is None:
            return self._bare_outcome(
                run_id="",
                session="",
                mode=mode,
                status=RunStatus.BLOCKED,
                blockers=(f"{Code.OPERATIONS_RUN_NOT_DUE}: no slot is due at {now}",),
            )
        account = self._account_fingerprint()
        run_id = missed.run_id(account_binding_fingerprint=account, mode=mode)
        if self._journal.last_state_of(run_id) is None:
            self._journal.append(
                clock=self.clock,
                event_type=RunEventType.RUN_SCHEDULED,
                run_id=run_id,
                detail=f"slot {missed.session} was due at {missed.schedule.due_at(missed.session)}",
            )
            self._journal.append(
                clock=self.clock,
                event_type=RunEventType.RUN_STATE_CHANGED,
                run_id=run_id,
                from_state=RunState.SCHEDULED,
                to_state=RunState.MISSED,
                detail=(
                    "the execution window closed before this process looked. The run "
                    "is recorded as missed and is never run late: yesterday's plan is "
                    "not a late version of yesterday's decision"
                ),
            )
        return self._bare_outcome(
            run_id=run_id,
            session=missed.session,
            mode=mode,
            status=RunStatus.MISSED,
            blockers=(
                f"{Code.MISSED_OPERATIONAL_RUN}: the window for {missed.session} closed "
                "before this process looked; nothing is backfilled",
            ),
        )

    def _already_finished(
        self, *, run_id: str, slot: ScheduledSlot, mode: OperationsMode, state: RunState
    ) -> RunOutcome:
        """Report an existing finished run rather than starting another.

        Args:
            run_id: The run.
            slot: Its slot.
            mode: The mode.
            state: The state it finished in.

        Returns:
            The outcome, marked recovered.
        """
        status = {
            RunState.COMPLETED: RunStatus.COMPLETED_NO_ACTION,
            RunState.BLOCKED: RunStatus.BLOCKED,
            RunState.KILLED: RunStatus.KILLED,
            RunState.MISSED: RunStatus.MISSED,
        }[state]
        outcome = self._bare_outcome(
            run_id=run_id,
            session=slot.session,
            mode=mode,
            status=status,
            blockers=(
                f"{Code.RUN_ALREADY_COMPLETED}: run {run_id} already finished as "
                f"{state}; this invocation is a no-op rather than a second run",
            ),
        )
        outcome.recovered = True
        return outcome

    def _bare_outcome(
        self,
        *,
        run_id: str,
        session: str,
        mode: OperationsMode,
        status: RunStatus,
        blockers: tuple[str, ...] = (),
        health: PaperOperationsHealthReport | None = None,
    ) -> RunOutcome:
        """Build an outcome for a run that did not act.

        Args:
            run_id: The run.
            session: Its session.
            mode: The mode.
            status: How it ended.
            blockers: Why.
            health: The gate's verdict, when one was taken.

        Returns:
            The outcome.
        """
        switch = self._kill.current()
        report = PaperOperationsRunReport(
            run_id=run_id,
            schedule_id=self.schedule.schedule_id,
            session=session,
            mode=mode,
            status=status,
            scheduled_for=(self.schedule.due_at(session).isoformat() if session else ""),
            started_at=self.clock.iso(),
            ended_at=self.clock.iso(),
            health_before=str(health.state) if health else "",
            external_mutations=0,
            kill_state=str(switch.state),
            incidents=tuple(incident.incident_id for incident in self._incidents.open_incidents()),
            blockers=blockers,
        )
        return RunOutcome(
            report=report,
            manifest=self._manifest(run_id=run_id, health=health),
            health=health,
        )

    def _manifest(
        self, *, run_id: str, health: PaperOperationsHealthReport | None, **extra: str
    ) -> dict[str, Any]:
        """Build the operational manifest for a run.

        Args:
            run_id: The run.
            health: The gate's verdict.
            extra: Additional bindings.

        Returns:
            The manifest.
        """
        grant = None
        try:
            grant = self.arming.current()
        except OperationsError:
            grant = None
        anchor = self._journal.read_anchor() or {}
        return paper_operations_manifest(
            run_id=run_id,
            schedule_fingerprint=self.schedule.fingerprint(),
            authorization_fingerprint=grant.fingerprint() if grant else "",
            kill_state_fingerprint=self._kill.current().fingerprint(),
            health_fingerprint=health.fingerprint() if health else "",
            incident_set_fingerprint=self._incidents.fingerprint(),
            account_binding_fingerprint=self._account_fingerprint(),
            operations_journal_head=str(anchor.get("head_hash", "")),
            code_commit=self.code_commit,
            **extra,
        )

    def evaluate_health(
        self, *, run_id: str, mode: OperationsMode, account: str, now: str
    ) -> PaperOperationsHealthReport:
        """Evaluate every required gate, collecting failures rather than raising.

        Args:
            run_id: The run.
            mode: The mode.
            account: The expected account binding.
            now: The operational instant.

        Returns:
            The report. Checks that could not run are reported as not reached
            rather than assumed passed.
        """
        checks: list[HealthCheck] = []

        switch = self._kill.current()
        checks.append(
            HealthCheck(
                name="KILL_SWITCH_ACTIVE",
                passed=switch.state is KillState.ACTIVE,
                state=HealthState.BLOCKED,
                reason_code=(
                    Code.PAPER_OPERATIONS_KILLED
                    if switch.state.is_killed
                    else Code.PAPER_OPERATIONS_PAUSED
                    if switch.state is KillState.PAUSED
                    else ""
                ),
                detail=f"control state is {switch.state} ({switch.reason_code})",
            )
        )
        checks.append(
            HealthCheck(
                name="CONTROL_STATE_VERIFIED",
                passed=switch.reason_code != Code.CONTROL_STATE_UNVERIFIABLE,
                state=HealthState.UNHEALTHY,
                reason_code=Code.CONTROL_STATE_UNVERIFIABLE,
                detail=switch.detail,
            )
        )

        armed = False
        grant_detail = "no authorization is present; the safe default is not armed"
        try:
            grant = self.arming.current()
            if grant is not None:
                grant.require(mode=mode, now=now, account_fingerprint=account)
                armed = True
                grant_detail = ""
        except OperationsError as exc:
            grant_detail = str(exc)
        checks.append(
            HealthCheck(
                name="OPERATIONS_ARMED",
                passed=armed,
                state=HealthState.BLOCKED,
                reason_code=Code.PAPER_OPERATIONS_NOT_ARMED,
                detail=grant_detail,
            )
        )

        try:
            integrity = self.environment.verify_local_integrity()
            checks.append(
                HealthCheck(
                    name="EXECUTION_JOURNAL_VERIFIED",
                    passed=bool(integrity.get("execution_journal_verified", False)),
                    state=HealthState.UNHEALTHY,
                    reason_code=Code.BLOCKED_LOCAL_INTEGRITY,
                    detail=str(integrity.get("execution_detail", "")),
                )
            )
            checks.append(
                HealthCheck(
                    name="ACCOUNTING_LEDGER_VERIFIED",
                    passed=bool(integrity.get("accounting_ledger_verified", False)),
                    state=HealthState.UNHEALTHY,
                    reason_code=Code.BLOCKED_LOCAL_INTEGRITY,
                    detail=str(integrity.get("accounting_detail", "")),
                )
            )
        except Exception as exc:
            for name in ("EXECUTION_JOURNAL_VERIFIED", "ACCOUNTING_LEDGER_VERIFIED"):
                checks.append(
                    HealthCheck(
                        name=name,
                        passed=False,
                        state=HealthState.UNHEALTHY,
                        reason_code=Code.BLOCKED_LOCAL_INTEGRITY,
                        detail=f"{type(exc).__name__}: local records did not verify",
                    )
                )

        try:
            self._journal.verify()
            checks.append(HealthCheck(name="OPERATIONS_JOURNAL_VERIFIED", passed=True))
        except OperationsError as exc:
            checks.append(
                HealthCheck(
                    name="OPERATIONS_JOURNAL_VERIFIED",
                    passed=False,
                    state=HealthState.UNHEALTHY,
                    reason_code=exc.code,
                    detail=str(exc),
                )
            )

        try:
            proof = self.environment.prove_paper_environment()
            checks.append(
                HealthCheck(
                    name="PAPER_ENVIRONMENT_PROVEN",
                    passed=bool(proof.get("proven", False)),
                    state=HealthState.BLOCKED,
                    reason_code="BROKER_ENVIRONMENT_NOT_PROVEN_PAPER",
                    detail=str(proof.get("detail", "")),
                )
            )
            checks.append(
                HealthCheck(
                    name="ACCOUNT_BINDING_MATCHED",
                    passed=str(proof.get("account_binding_fingerprint", "")) == account
                    and bool(account),
                    state=HealthState.BLOCKED,
                    reason_code="BROKER_ACCOUNT_MISMATCH",
                    detail="the venue answered for a different account binding",
                )
            )
            checks.append(HealthCheck(name="PROVIDER_REACHABLE", passed=True))
        except Exception as exc:
            checks.append(
                HealthCheck(
                    name="PROVIDER_REACHABLE",
                    passed=False,
                    state=HealthState.BLOCKED,
                    reason_code=Code.BLOCKED_PROVIDER_UNAVAILABLE,
                    detail=(
                        f"{type(exc).__name__}: the paper venue could not be reached. "
                        "The local deterministic broker is NOT used as a fallback — "
                        "the venue is bound for the run"
                    ),
                )
            )
            for name in ("PAPER_ENVIRONMENT_PROVEN", "ACCOUNT_BINDING_MATCHED"):
                checks.append(
                    HealthCheck(
                        name=name,
                        passed=False,
                        state=HealthState.BLOCKED,
                        reason_code=Code.BLOCKED_PROVIDER_UNAVAILABLE,
                        detail="not established: the venue could not be reached",
                    )
                )

        try:
            reconciliation = self.environment.reconcile()
            verdict = str(reconciliation.get("verdict", ""))
            checks.append(
                HealthCheck(
                    name="STARTUP_RECONCILIATION_CLEAN",
                    passed=bool(reconciliation.get("may_execute", False)),
                    state=HealthState.BLOCKED,
                    reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
                    detail=f"three-way reconciliation is {verdict}",
                )
            )
            checks.append(
                HealthCheck(
                    name="NO_REMOTE_STATE_UNKNOWN",
                    passed=not reconciliation.get("remote_state_unknown", False),
                    state=HealthState.BLOCKED,
                    reason_code=Code.BLOCKED_REMOTE_STATE_UNKNOWN,
                    detail=(
                        "a submission's state at the venue is unresolved. Operations "
                        "are blocked for this whole account, not just the affected "
                        "name: there may be a working order nobody is tracking"
                    ),
                )
            )
            checks.append(
                HealthCheck(
                    name="NO_UNEXPECTED_REMOTE_ORDER",
                    passed=int(reconciliation.get("unexpected_remote_orders", 0)) == 0,
                    state=HealthState.BLOCKED,
                    reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
                    detail="the venue holds an order this system did not place",
                )
            )
            checks.append(
                HealthCheck(
                    name="NO_UNEXPLAINED_REMOTE_POSITION",
                    passed=int(reconciliation.get("unexplained_remote_positions", 0)) == 0,
                    state=HealthState.BLOCKED,
                    reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
                    detail="the venue holds a position this book does not account for",
                )
            )
        except Exception as exc:
            for name in (
                "STARTUP_RECONCILIATION_CLEAN",
                "NO_REMOTE_STATE_UNKNOWN",
                "NO_UNEXPECTED_REMOTE_ORDER",
                "NO_UNEXPLAINED_REMOTE_POSITION",
            ):
                checks.append(
                    HealthCheck(
                        name=name,
                        passed=False,
                        state=HealthState.BLOCKED,
                        reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
                        detail=f"{type(exc).__name__}: reconciliation did not complete",
                    )
                )

        inputs = self.environment.required_inputs()
        checks.append(
            HealthCheck(
                name="REQUIRED_INPUTS_PRESENT",
                passed=all(row.source_fingerprint for row in inputs),
                state=HealthState.BLOCKED,
                reason_code=Code.REQUIRED_OPERATIONAL_INPUT_MISSING,
                detail="a required input has no source; no value is guessed",
            )
        )
        stale = [row.name for row in inputs if not row.is_fresh(now)]
        checks.append(
            HealthCheck(
                name="REQUIRED_INPUTS_FRESH",
                passed=not stale,
                state=HealthState.BLOCKED,
                reason_code=Code.STALE_OPERATIONAL_INPUT,
                detail=(
                    f"stale: {stale}. An input existing is not evidence that it still "
                    "describes the market"
                    if stale
                    else ""
                ),
            )
        )

        checks.append(
            HealthCheck(
                name="SESSION_ELIGIBLE",
                passed=self.schedule.slot_for(now) is not None,
                state=HealthState.BLOCKED,
                reason_code=Code.OPERATIONS_SESSION_NOT_ELIGIBLE,
                detail="no scheduled slot is open at this instant",
            )
        )
        holder = self._leases.held_by()
        checks.append(
            HealthCheck(
                name="NO_OVERLAPPING_RUN",
                passed=holder in {None, run_id},
                state=HealthState.BLOCKED,
                reason_code=Code.RUN_ALREADY_ACTIVE,
                detail=f"run {holder} holds the mutation lease",
            )
        )

        return PaperOperationsHealthReport.from_incidents(
            self._incidents.open_incidents(),
            checks=tuple(checks),
            evaluated_at=now,
            run_id=run_id,
            account_binding_fingerprint=account,
        )

    def _escalate(self, *, health: PaperOperationsHealthReport, run_id: str, account: str) -> None:
        """Open incidents for what health found, killing on critical conditions.

        The frozen critical list decides what kills. Everything else blocks.
        Escalating ordinary trouble — a stale input, an unreachable provider — to
        a state that needs operator recovery would train an operator to clear
        kills without reading them, which is worse than not having one.

        Args:
            health: The gate's verdict.
            run_id: The run.
            account: The account binding.
        """
        for check in health.failures:
            if not check.reason_code:
                continue
            severity = IncidentStore.severity_for(check.reason_code)
            self._incidents.record(
                clock=self.clock,
                reason_code=check.reason_code,
                severity=severity,
                subject=check.name,
                account_fingerprint=account,
                detail=check.detail,
                run_id=run_id,
                local_evidence_fingerprint=health.fingerprint(),
            )
            if severity is IncidentSeverity.CRITICAL:
                self._kill.kill(
                    clock=self.clock,
                    reason_code=check.reason_code,
                    detail=(
                        f"automatically killed by {check.name}: the system has lost "
                        "track of what is true. No position is liquidated"
                    ),
                    account_binding_fingerprint=account,
                    changed_by="AUTOMATIC",
                )
                self._journal.append(
                    clock=self.clock,
                    event_type=RunEventType.KILL_OBSERVED,
                    run_id=run_id,
                    detail=f"critical condition {check.reason_code} activated the kill switch",
                )

    def _execute(
        self,
        *,
        run_id: str,
        slot: ScheduledSlot,
        mode: OperationsMode,
        account: str,
        now: str,
        resumed: bool,
        plan_and_submit: Any,
    ) -> RunOutcome:
        """Run the pipeline for one slot.

        Args:
            run_id: The derived run id.
            slot: The slot.
            mode: The mode.
            account: The account binding.
            now: The operational instant.
            resumed: Whether this resumes an existing run.
            plan_and_submit: The caller's planning and submission callable.

        Returns:
            The outcome.
        """
        state = self._journal.last_state_of(run_id) or RunState.SCHEDULED
        if not resumed:
            self._journal.append(
                clock=self.clock,
                event_type=RunEventType.RUN_SCHEDULED,
                run_id=run_id,
                detail=f"slot {slot.session} in mode {mode}",
            )
            state = self._advance(run_id, state, RunState.RECOVERING, "reading back local state")
        accounting_before = ""
        try:
            accounting_before = self.environment.accounting_state_fingerprint()
        except Exception:
            accounting_before = ""

        if state is RunState.RECOVERING:
            state = self._advance(run_id, state, RunState.HEALTH_CHECK, "evaluating gates")

        health = self.evaluate_health(run_id=run_id, mode=mode, account=account, now=now)
        self._journal.append(
            clock=self.clock,
            event_type=RunEventType.HEALTH_EVALUATED,
            run_id=run_id,
            detail=f"health is {health.state}",
            evidence=(("health", health.fingerprint()),),
        )
        self._escalate(health=health, run_id=run_id, account=account)

        switch = self._kill.current()
        if not switch.may_submit:
            state = self._advance(
                run_id, state, RunState.KILLED, f"control state is {switch.state}"
            )
            return self._finish(
                run_id=run_id,
                slot=slot,
                mode=mode,
                status=RunStatus.KILLED,
                health=health,
                accounting_before=accounting_before,
                blockers=(f"{switch.state}: {switch.reason_code}",),
            )

        if not health.may_mutate and mode.may_mutate:
            state = self._advance(run_id, state, RunState.BLOCKED, f"health is {health.state}")
            return self._finish(
                run_id=run_id,
                slot=slot,
                mode=mode,
                status=RunStatus.BLOCKED,
                health=health,
                accounting_before=accounting_before,
                blockers=tuple(f"{check.name}: {check.reason_code}" for check in health.failures),
            )

        state = self._advance(run_id, state, RunState.RECONCILING, "three-way reconciliation")
        state = self._advance(run_id, state, RunState.READY, "every gate passed")

        lease_held = False
        if mode.may_mutate:
            self._leases.acquire(
                run_id=run_id, account_binding_fingerprint=account, clock=self.clock
            )
            lease_held = True

        gate = SubmissionGate(kill_store=self._kill, incidents=self._incidents)
        planned: dict[str, Any] = {}
        try:
            state = self._advance(run_id, state, RunState.PLANNING, "building an authorised plan")
            if plan_and_submit is not None:
                planned = (
                    plan_and_submit(
                        run_id=run_id,
                        mode=mode,
                        gate=gate,
                        account=account,
                        journal=self._journal,
                        clock=self.clock,
                    )
                    or {}
                )
            if mode.may_mutate and int(planned.get("orders_submitted", 0)):
                state = self._advance(run_id, state, RunState.EXECUTING, "submitting")
                state = self._advance(run_id, state, RunState.VERIFYING, "ingesting fills")
            status = (
                RunStatus.COMPLETED_OBSERVE_ONLY
                if not mode.may_mutate
                else RunStatus.COMPLETED_PAPER_EXECUTION
                if int(planned.get("orders_submitted", 0))
                else RunStatus.COMPLETED_NO_ACTION
            )
            state = self._advance(run_id, state, RunState.COMPLETED, f"finished as {status}")
        except OperationsError as exc:
            terminal = (
                RunState.KILLED
                if exc.code
                in {
                    Code.PAPER_OPERATIONS_KILLED,
                    Code.PAPER_OPERATIONS_PAUSED,
                    Code.KILLED_WITH_UNRESOLVED_REMOTE_ORDER,
                }
                else RunState.BLOCKED
            )
            self._advance(run_id, state, terminal, str(exc))
            return self._finish(
                run_id=run_id,
                slot=slot,
                mode=mode,
                status=(RunStatus.KILLED if terminal is RunState.KILLED else RunStatus.BLOCKED),
                health=health,
                accounting_before=accounting_before,
                blockers=(str(exc),),
                planned=planned,
                gate=gate,
                recovered=resumed,
            )
        finally:
            if lease_held:
                self._leases.release(run_id=run_id, clock=self.clock)

        return self._finish(
            run_id=run_id,
            slot=slot,
            mode=mode,
            status=status,
            health=health,
            accounting_before=accounting_before,
            planned=planned,
            gate=gate,
            recovered=resumed,
        )

    def _advance(self, run_id: str, current: RunState, proposed: RunState, detail: str) -> RunState:
        """Record a legal state transition.

        Args:
            run_id: The run.
            current: Where it is.
            proposed: Where it goes.
            detail: Why.

        Returns:
            The new state.

        Raises:
            OperationsError: `INVALID_OPERATIONS_RUN_TRANSITION` if the edge is
                not legal, checked before anything is written.
        """
        self._journal.append(
            clock=self.clock,
            event_type=RunEventType.RUN_STATE_CHANGED,
            run_id=run_id,
            from_state=current,
            to_state=proposed,
            detail=detail,
        )
        return proposed

    def _finish(
        self,
        *,
        run_id: str,
        slot: ScheduledSlot,
        mode: OperationsMode,
        status: RunStatus,
        health: PaperOperationsHealthReport,
        accounting_before: str,
        blockers: tuple[str, ...] = (),
        planned: dict[str, Any] | None = None,
        gate: SubmissionGate | None = None,
        recovered: bool = False,
    ) -> RunOutcome:
        """Build and record the run's final report.

        Args:
            run_id: The run.
            slot: Its slot.
            mode: The mode.
            status: How it ended.
            health: The gate's verdict.
            accounting_before: The book before.
            blockers: Why it stopped, when it did.
            planned: What the caller's planner reported.
            gate: The submission gate, for its check count.
            recovered: Whether this resumed an existing run.

        Returns:
            The outcome.
        """
        rows = planned or {}
        accounting_after = ""
        try:
            accounting_after = self.environment.accounting_state_fingerprint()
        except Exception:
            accounting_after = ""
        switch = self._kill.current()
        report = PaperOperationsRunReport(
            run_id=run_id,
            schedule_id=self.schedule.schedule_id,
            session=slot.session,
            mode=mode,
            status=status,
            scheduled_for=slot.schedule.due_at(slot.session).isoformat(),
            started_at=health.evaluated_at,
            ended_at=self.clock.iso(),
            health_before=str(health.state),
            reconciliation_before=str(rows.get("reconciliation_before", "")),
            reconciliation_after=str(rows.get("reconciliation_after", "")),
            inputs=self.environment.required_inputs(),
            planned_orders=int(rows.get("planned_orders", 0)),
            orders_submitted=int(rows.get("orders_submitted", 0)),
            orders_filled=int(rows.get("orders_filled", 0)),
            would_submit=tuple(rows.get("would_submit", ())),
            external_mutations=int(rows.get("external_mutations", 0)),
            execution_manifest_fingerprint=str(rows.get("execution_manifest_fingerprint", "")),
            accounting_before=accounting_before,
            accounting_after=accounting_after,
            kill_state=str(switch.state),
            incidents=tuple(incident.incident_id for incident in self._incidents.open_incidents()),
            blockers=blockers,
        )
        self._journal.append(
            clock=self.clock,
            event_type=RunEventType.RUN_COMPLETED,
            run_id=run_id,
            detail=f"status {status}",
            evidence=(("run_report", report.fingerprint()),),
        )
        return RunOutcome(
            report=report,
            manifest=self._manifest(
                run_id=run_id,
                health=health,
                accounting_before=accounting_before,
                accounting_after=accounting_after,
                reconciliation_before=str(rows.get("reconciliation_before", "")),
                reconciliation_after=str(rows.get("reconciliation_after", "")),
                execution_manifest_fingerprint=str(rows.get("execution_manifest_fingerprint", "")),
            ),
            health=health,
            recovered=recovered,
        )
