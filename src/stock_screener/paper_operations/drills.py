"""Running the six required failure drills, deterministically.

The drills exist because a quiet soak proves the wrong thing. Twenty sessions
where nothing went wrong establish that the system works when nothing goes
wrong, which was never in doubt. What matters is the behaviour on the paths a
normal session never touches, and the only way to see those is to cause them.

Every drill here is driven by an **injected** fault against a stub environment
and a fake clock. That is deliberate and worth defending rather than apologising
for: the two most important drills are "the provider became unreachable" and
"a submission's fate is unknown", and arranging either against a real venue
would mean interrupting a real order mid-flight. That experiment can itself
cause the duplicate it is testing for. An injected fault reproduces the
*decision* the system has to make with none of the exposure, and the decision is
the thing under test.

Each drill records what was true before, what was done, what was expected, what
was observed, and how many mutating requests it caused — so a reader can check
the claim rather than take it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .clock import FakeClock
from .errors import Code, OperationsError
from .kill_switch import KillState, KillSwitchStore
from .models import FreshnessPolicy, OperationalInput, OperationsMode, RunStatus
from .soak import DrillRecord, DrillType

if TYPE_CHECKING:
    from pathlib import Path

    from .coordinator import PaperOperationsCoordinator
    from .soak import SoakStore

__all__ = ["DrillHarness", "run_required_drills"]


@dataclass
class _StubEnvironment:
    """A stand-in for the rest of the system, with faults a drill can inject."""

    account: str
    provider_reachable: bool = True
    remote_state_unknown: bool = False
    inputs: tuple[OperationalInput, ...] = ()
    accounting_fingerprint: str = "acct-fp"

    def verify_local_integrity(self) -> dict[str, Any]:
        """Report the local records as verifying."""
        return {
            "execution_journal_verified": True,
            "accounting_ledger_verified": True,
            "execution_detail": "",
            "accounting_detail": "",
        }

    def prove_paper_environment(self) -> dict[str, Any]:
        """Prove the venue, or fail as an unreachable venue would."""
        if not self.provider_reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        return {"proven": True, "account_binding_fingerprint": self.account, "detail": ""}

    def reconcile(self) -> dict[str, Any]:
        """Reconcile, or report the ambiguity a drill injected."""
        if not self.provider_reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        return {
            "verdict": "RECONCILED" if not self.remote_state_unknown else "BREAKS_BLOCKING",
            "may_execute": not self.remote_state_unknown,
            "remote_state_unknown": self.remote_state_unknown,
            "unexpected_remote_orders": 0,
            "unexplained_remote_positions": 0,
        }

    def required_inputs(self) -> tuple[OperationalInput, ...]:
        """The inputs the run depends on."""
        return self.inputs

    def accounting_state_fingerprint(self) -> str:
        """The book's state fingerprint."""
        return self.accounting_fingerprint


@dataclass
class _Planner:
    """A planner that records what the gate let it do."""

    orders: int = 1
    submitted: int = 0
    gate_checks: int = 0
    kill_before_order: int | None = None
    kill_store: KillSwitchStore | None = None
    clock: Any = None
    invoked: int = 0

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        """Plan and, where the mode permits, submit."""
        self.invoked += 1
        mode = kwargs["mode"]
        gate = kwargs["gate"]
        account = kwargs["account"]
        if not mode.may_mutate:
            return {"planned_orders": self.orders, "orders_submitted": 0, "external_mutations": 0}
        for index in range(self.orders):
            if self.kill_before_order == index + 1 and self.kill_store and self.clock:
                self.kill_store.kill(
                    clock=self.clock,
                    reason_code="DRILL_KILL",
                    account_binding_fingerprint=account,
                )
            gate.require(context=f"order {index + 1}", expected_account=account)
            self.gate_checks = gate.checks_performed
            self.submitted += 1
        return {
            "planned_orders": self.orders,
            "orders_submitted": self.submitted,
            "orders_filled": self.submitted,
            "external_mutations": self.submitted,
            "reconciliation_before": "RECONCILED",
            "reconciliation_after": "RECONCILED",
        }


@dataclass
class DrillHarness:
    """Builds coordinators over throwaway roots so drills cannot touch real state.

    Attributes:
        root: Where drill roots are created.
        account: The account binding the drills use.
        now: The operational instant the fake clock reads.
        schedule: The schedule producing the drill slot.
    """

    root: Path
    account: str
    now: str
    schedule: Any
    _counter: int = field(default=0)

    def coordinator(
        self,
        *,
        armed: OperationsMode | None = OperationsMode.EXTERNAL_PAPER,
        kill: KillState | None = KillState.ACTIVE,
        env: _StubEnvironment | None = None,
        reuse: Path | None = None,
    ) -> tuple[PaperOperationsCoordinator, _StubEnvironment, FakeClock, Path]:
        """Build a coordinator over an isolated operational root.

        Args:
            armed: Which mode to arm, or `None` to leave unarmed.
            kill: The control state to set, or `None` to leave absent.
            env: The environment, with any injected faults.
            reuse: An existing root, for a restart drill.

        Returns:
            The coordinator, environment, clock and root.
        """
        from .arming import ArmingStore, PaperOperationsAuthorization
        from .coordinator import PaperOperationsCoordinator

        self._counter += 1
        drill_root = reuse or (self.root / f"drill-{self._counter}" / ".operations")
        clock = FakeClock.at(self.now)
        environment = env or _StubEnvironment(
            account=self.account,
            inputs=(
                OperationalInput(
                    name="prices",
                    as_of=self.now,
                    source_fingerprint="drill-src",
                    policy=FreshnessPolicy(name="prices", max_age_seconds=600),
                ),
            ),
        )
        arming = ArmingStore(drill_root)
        if armed is not None and reuse is None:
            arming.arm(
                PaperOperationsAuthorization(
                    authorization_id="drill-grant",
                    provider="ALPACA",
                    account_binding_fingerprint=self.account,
                    operations_policy_fingerprint="drill-pol",
                    code_commit="drill",
                    created_at=self.now,
                    allowed_mode=armed,
                )
            )
        kill_store = KillSwitchStore(drill_root)
        if reuse is None:
            if kill is KillState.ACTIVE:
                kill_store.activate(clock=clock, account_binding_fingerprint=self.account)
            elif kill is KillState.KILLED:
                kill_store.kill(
                    clock=clock,
                    reason_code="DRILL_KILL",
                    account_binding_fingerprint=self.account,
                )
        return (
            PaperOperationsCoordinator(
                root=drill_root,
                clock=clock,
                schedule=self.schedule,
                environment=environment,
                arming=arming,
                code_commit="drill",
            ),
            environment,
            clock,
            drill_root,
        )


def run_required_drills(
    store: SoakStore, *, harness: DrillHarness, performed_at: str
) -> tuple[DrillRecord, ...]:
    """Perform every required drill and record its evidence.

    Each drill is an injected fault against a throwaway root. Nothing here
    touches the real operational state, the real account, or a network.

    Args:
        store: Where the evidence is recorded.
        harness: Builds the isolated coordinators.
        performed_at: When, ISO-8601.

    Returns:
        The drill records, one per required drill.

    Raises:
        OperationsError: `SOAK_PROMOTION_GATE_FAILED` if any drill's observed
            behaviour differs from what was expected. A failing drill is not
            recorded-and-shrugged-at: it means the control plane does not do
            what the soak is about to assume it does.
    """
    records: list[DrillRecord] = []

    # 1. kill, then restart the process and prove the kill survived.
    coordinator, _, clock, root = harness.coordinator()
    planner = _Planner(
        orders=1, kill_before_order=1, kill_store=coordinator.kill_store, clock=clock
    )
    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    reopened = KillSwitchStore(root).current()
    records.append(
        DrillRecord(
            drill_type=DrillType.KILL_RESTART,
            performed_at=performed_at,
            run_id=outcome.report.run_id,
            pre_state="ACTIVE, armed EXTERNAL_PAPER",
            action="activated the kill switch after planning, then reopened the store",
            expected="0 submissions; KILLED survives the reopen",
            observed=(f"{planner.submitted} submissions; reopened state {reopened.state}"),
            external_mutations=planner.submitted,
            post_state=str(reopened.state),
            passed=planner.submitted == 0 and reopened.state is KillState.KILLED,
            detail="a restart is not a recovery: the switch is read from disk, not inherited",
        )
    )

    # 2. invoke the same scheduled slot twice.
    coordinator, _, _, _ = harness.coordinator()
    planner = _Planner(orders=1)
    first = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    second = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    records.append(
        DrillRecord(
            drill_type=DrillType.DUPLICATE_SCHEDULER,
            performed_at=performed_at,
            run_id=first.report.run_id,
            pre_state="ACTIVE, armed, no prior run",
            action="called run_due twice for the same slot",
            expected="one semantic run; one submission; the second invocation recovers",
            observed=(
                f"run ids equal: {first.report.run_id == second.report.run_id}; "
                f"{planner.submitted} submissions; second recovered: {second.recovered}"
            ),
            external_mutations=planner.submitted,
            post_state=str(second.report.status),
            passed=(
                first.report.run_id == second.report.run_id
                and planner.submitted == 1
                and second.recovered
            ),
            detail=(
                "the run id is derived, so a retry recovers rather than competing. "
                "The submission counted here is a stub planner call, not a venue order"
            ),
        )
    )

    # 3. crash mid-run, then recover the same run.
    coordinator, _, clock, root = harness.coordinator()
    from .models import RunState
    from .run_journal import OperationsRunJournal, RunEventType

    slot = harness.schedule.slot_for(harness.now)
    assert slot is not None  # noqa: S101 - the harness instant is inside the window
    run_id = slot.run_id(
        account_binding_fingerprint=harness.account, mode=OperationsMode.EXTERNAL_PAPER
    )
    journal = OperationsRunJournal(root)
    journal.append(clock=clock, event_type=RunEventType.RUN_SCHEDULED, run_id=run_id)
    journal.append(
        clock=clock,
        event_type=RunEventType.RUN_STATE_CHANGED,
        run_id=run_id,
        from_state=RunState.SCHEDULED,
        to_state=RunState.RECOVERING,
    )
    planner = _Planner(orders=1)
    recovered = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    records.append(
        DrillRecord(
            drill_type=DrillType.CRASH_RECOVERY,
            performed_at=performed_at,
            run_id=run_id,
            pre_state="a run left in RECOVERING by a simulated crash",
            action="restarted and invoked the same slot",
            expected="the same run id resumed; one submission; no second run",
            observed=(
                f"run id {recovered.report.run_id == run_id}; "
                f"recovered {recovered.recovered}; {planner.submitted} submissions"
            ),
            external_mutations=planner.submitted,
            post_state=str(recovered.report.status),
            reconciliation=recovered.report.reconciliation_after,
            passed=(
                recovered.report.run_id == run_id and recovered.recovered and planner.submitted == 1
            ),
            detail=(
                "recovery resumes the recorded run rather than starting a competitor. "
                "The submission counted here is a stub planner call, not a venue order"
            ),
        )
    )

    # 4. the provider is unreachable.
    coordinator, _, _, _ = harness.coordinator(
        env=_StubEnvironment(
            account=harness.account,
            provider_reachable=False,
            inputs=(
                OperationalInput(
                    name="prices",
                    as_of=harness.now,
                    source_fingerprint="drill-src",
                    policy=FreshnessPolicy(name="prices", max_age_seconds=600),
                ),
            ),
        )
    )
    planner = _Planner(orders=1)
    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    fallback_text = " ".join(
        check.detail for check in (outcome.health.failures if outcome.health else ())
    )
    records.append(
        DrillRecord(
            drill_type=DrillType.PROVIDER_UNAVAILABLE,
            performed_at=performed_at,
            run_id=outcome.report.run_id,
            pre_state="ACTIVE, armed, provider unreachable",
            action="injected a connection failure at the venue",
            expected="BLOCKED; 0 submissions; no fallback to the local simulator",
            observed=(
                f"{outcome.report.status}; {planner.submitted} submissions; "
                f"fallback refused: {'NOT used as a fallback' in fallback_text}"
            ),
            external_mutations=planner.submitted,
            post_state=str(outcome.report.status),
            passed=(
                outcome.report.status is RunStatus.BLOCKED
                and planner.submitted == 0
                and "NOT used as a fallback" in fallback_text
            ),
            detail="the venue is bound for the run; an outage blocks rather than substitutes",
        )
    )

    # 5. a submission's state at the venue is unknown.
    coordinator, _, _, _ = harness.coordinator(
        env=_StubEnvironment(
            account=harness.account,
            remote_state_unknown=True,
            inputs=(
                OperationalInput(
                    name="prices",
                    as_of=harness.now,
                    source_fingerprint="drill-src",
                    policy=FreshnessPolicy(name="prices", max_age_seconds=600),
                ),
            ),
        )
    )
    planner = _Planner(orders=1)
    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    ambiguity_text = " ".join(
        check.detail for check in (outcome.health.failures if outcome.health else ())
    )
    records.append(
        DrillRecord(
            drill_type=DrillType.REMOTE_STATE_UNKNOWN,
            performed_at=performed_at,
            run_id=outcome.report.run_id,
            pre_state="ACTIVE, armed, one submission unresolved at the venue",
            action="injected REMOTE_STATE_UNKNOWN into reconciliation",
            expected="BLOCKED for the whole account; 0 submissions",
            observed=(
                f"{outcome.report.status}; {planner.submitted} submissions; "
                f"account-wide: {'not just the affected' in ambiguity_text}"
            ),
            external_mutations=planner.submitted,
            post_state=str(outcome.report.status),
            passed=(
                outcome.report.status is RunStatus.BLOCKED
                and planner.submitted == 0
                and "not just the affected" in ambiguity_text
            ),
            detail="ambiguity is not isolated so other names keep trading",
        )
    )

    # 6. a required input is stale.
    coordinator, _, _, _ = harness.coordinator(
        env=_StubEnvironment(
            account=harness.account,
            inputs=(
                OperationalInput(
                    name="prices",
                    as_of="2026-01-01T00:00:00+00:00",
                    source_fingerprint="drill-src",
                    policy=FreshnessPolicy(name="prices", max_age_seconds=600),
                ),
            ),
        )
    )
    planner = _Planner(orders=1)
    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    stale_codes = {
        check.reason_code for check in (outcome.health.failures if outcome.health else ())
    }
    records.append(
        DrillRecord(
            drill_type=DrillType.STALE_INPUT,
            performed_at=performed_at,
            run_id=outcome.report.run_id,
            pre_state="ACTIVE, armed, required input far outside its freshness policy",
            action="injected an input nine months old against a 600s limit",
            expected="BLOCKED with STALE_OPERATIONAL_INPUT; 0 submissions",
            observed=(
                f"{outcome.report.status}; {planner.submitted} submissions; "
                f"codes {sorted(code for code in stale_codes if code)}"
            ),
            external_mutations=planner.submitted,
            post_state=str(outcome.report.status),
            passed=(
                outcome.report.status is RunStatus.BLOCKED
                and planner.submitted == 0
                and Code.STALE_OPERATIONAL_INPUT in stale_codes
            ),
            detail="an input existing is not evidence that it still describes the market",
        )
    )

    for record in records:
        store.record_drill(record)

    failed = [record for record in records if not record.passed]
    if failed:
        raise OperationsError(
            Code.SOAK_PROMOTION_GATE_FAILED,
            "these drills did not behave as expected: "
            + "; ".join(
                f"{record.drill_type} expected {record.expected!r} but observed {record.observed!r}"
                for record in failed
            )
            + ". A failing drill means the control plane does not do what the soak "
            "is about to assume it does",
        )
    return tuple(records)
