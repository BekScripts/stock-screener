"""P2.3 — the operational control plane, exercised offline.

Every test here is about refusal. The control plane's value is almost entirely
in what it declines to do: trade on a stale figure, run a missed slot, start a
second run for one scheduled decision, keep going after a kill, fall back to a
simulator when the venue is down, or treat an unreadable control file as
permission.

No test makes a network request or submits anything anywhere. The environment is
a stub the test drives, and the clock is injected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.execution import ExecutionCalendar
from stock_screener.paper_operations import (
    CRITICAL_AUTO_KILL_REASONS,
    MUTATION_CAPABLE_MODES,
    REQUIRED_HEALTH_CHECKS,
    RUN_TRANSITIONS,
    ArmingStore,
    Code,
    FakeClock,
    FreshnessPolicy,
    HealthState,
    IncidentSeverity,
    IncidentStatus,
    IncidentStore,
    KillState,
    KillSwitchStore,
    OperationalInput,
    OperationsError,
    OperationsMode,
    OperationsRunJournal,
    PaperOperationsAuthorization,
    PaperOperationsCoordinator,
    PaperOperationsSchedule,
    RunEventType,
    RunLeaseStore,
    RunState,
    RunStatus,
    operations_run_id,
    require_run_transition,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

ACCOUNT = "a" * 64
IN_WINDOW = "2026-10-07T19:35:00+00:00"
AFTER_WINDOW = "2026-10-07T20:30:00+00:00"
BEFORE_DUE = "2026-10-07T19:00:00+00:00"
SESSION = "2026-10-07"


def schedule() -> PaperOperationsSchedule:
    """A daily schedule with an explicit calendar."""
    return PaperOperationsSchedule(
        schedule_id="daily-close",
        timezone="America/New_York",
        run_at_local="15:30",
        calendar=ExecutionCalendar.of(
            ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"],
            name="TEST-SESSIONS",
        ),
        execution_window_minutes=20,
    )


@dataclass
class StubEnvironment:
    """A stand-in for the rest of the system, driven by the test.

    It answers the coordinator's questions and records how many times it was
    asked. Nothing here reaches a network.
    """

    account: str = ACCOUNT
    execution_ok: bool = True
    accounting_ok: bool = True
    provider_reachable: bool = True
    paper_proven: bool = True
    reconciliation_ok: bool = True
    remote_state_unknown: bool = False
    unexpected_remote_orders: int = 0
    unexplained_remote_positions: int = 0
    inputs: tuple[OperationalInput, ...] = ()
    accounting_fingerprint: str = "acct-fp-before"
    reconcile_calls: int = 0

    def verify_local_integrity(self) -> dict[str, Any]:
        """Report whether the local records verify."""
        return {
            "execution_journal_verified": self.execution_ok,
            "accounting_ledger_verified": self.accounting_ok,
            "execution_detail": "" if self.execution_ok else "execution journal is broken",
            "accounting_detail": "" if self.accounting_ok else "accounting ledger is broken",
        }

    def prove_paper_environment(self) -> dict[str, Any]:
        """Prove the venue, or fail as the venue would."""
        if not self.provider_reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        return {
            "proven": self.paper_proven,
            "account_binding_fingerprint": self.account,
            "detail": "" if self.paper_proven else "the environment is not provably paper",
        }

    def reconcile(self) -> dict[str, Any]:
        """Run the three-way reconciliation."""
        self.reconcile_calls += 1
        if not self.provider_reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        return {
            "verdict": "RECONCILED" if self.reconciliation_ok else "BREAKS_BLOCKING",
            "may_execute": self.reconciliation_ok,
            "remote_state_unknown": self.remote_state_unknown,
            "unexpected_remote_orders": self.unexpected_remote_orders,
            "unexplained_remote_positions": self.unexplained_remote_positions,
        }

    def required_inputs(self) -> tuple[OperationalInput, ...]:
        """The inputs the run depends on."""
        return self.inputs

    def accounting_state_fingerprint(self) -> str:
        """The book's state fingerprint."""
        return self.accounting_fingerprint


def fresh_input(
    name: str = "prices", *, as_of: str = "2026-10-07T19:34:00+00:00"
) -> OperationalInput:
    """One input well inside its freshness policy."""
    return OperationalInput(
        name=name,
        as_of=as_of,
        source_fingerprint="src-fp",
        policy=FreshnessPolicy(name=name, max_age_seconds=600),
    )


def stale_input(name: str = "prices") -> OperationalInput:
    """One input well outside its freshness policy."""
    return OperationalInput(
        name=name,
        as_of="2026-10-06T19:34:00+00:00",
        source_fingerprint="src-fp",
        policy=FreshnessPolicy(name=name, max_age_seconds=600),
    )


@dataclass
class Planner:
    """A stand-in for the planning and submission the caller supplies.

    Records what it was asked to do and how many times the submission gate was
    consulted, so a test can assert the gate ran per order rather than per run.
    """

    orders: int = 1
    submitted: int = 0
    gate_checks: int = 0
    raise_on_order: int | None = None
    kill_before_order: int | None = None
    kill_store: KillSwitchStore | None = None
    clock: Any = None
    calls: list[str] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        """Plan and, in a mutation-capable mode, submit."""
        mode = kwargs["mode"]
        gate = kwargs["gate"]
        account = kwargs["account"]
        self.calls.append(str(mode))
        if not mode.may_mutate:
            return {
                "planned_orders": self.orders,
                "orders_submitted": 0,
                "external_mutations": 0,
                "would_submit": tuple(f"order-{i + 1}" for i in range(self.orders)),
                "reconciliation_before": "RECONCILED",
            }
        for index in range(self.orders):
            if self.kill_before_order == index + 1 and self.kill_store and self.clock:
                self.kill_store.kill(
                    clock=self.clock,
                    reason_code="OPERATOR_KILL",
                    account_binding_fingerprint=account,
                )
            gate.require(context=f"submitting order {index + 1}", expected_account=account)
            self.gate_checks = gate.checks_performed
            self.submitted += 1
        return {
            "planned_orders": self.orders,
            "orders_submitted": self.submitted,
            "orders_filled": self.submitted,
            "external_mutations": self.submitted,
            "reconciliation_before": "RECONCILED",
            "reconciliation_after": "RECONCILED",
            "execution_manifest_fingerprint": "exec-mf",
        }


def authorization(
    *, mode: OperationsMode = OperationsMode.EXTERNAL_PAPER, expires_at: str | None = None
) -> PaperOperationsAuthorization:
    """An operator grant."""
    return PaperOperationsAuthorization(
        authorization_id="grant-1",
        provider="ALPACA",
        account_binding_fingerprint=ACCOUNT,
        operations_policy_fingerprint="pol-fp",
        code_commit="deadbeef",
        created_at="2026-10-07T18:00:00+00:00",
        allowed_mode=mode,
        expires_at=expires_at,
    )


def build(
    tmp_path: Path,
    *,
    now: str = IN_WINDOW,
    env: StubEnvironment | None = None,
    armed: OperationsMode | None = OperationsMode.EXTERNAL_PAPER,
    kill: KillState | None = KillState.ACTIVE,
) -> tuple[PaperOperationsCoordinator, StubEnvironment, FakeClock]:
    """A coordinator over a fresh operational root."""
    root = tmp_path / ".operations"
    clock = FakeClock.at(now)
    environment = env or StubEnvironment(inputs=(fresh_input(),))
    arming = ArmingStore(root)
    if armed is not None:
        arming.arm(authorization(mode=armed))
    kill_store = KillSwitchStore(root)
    if kill is KillState.ACTIVE:
        kill_store.activate(clock=clock, account_binding_fingerprint=ACCOUNT)
    elif kill is KillState.PAUSED:
        kill_store.pause(
            clock=clock, reason_code="OPERATOR_PAUSE", account_binding_fingerprint=ACCOUNT
        )
    elif kill is KillState.KILLED:
        kill_store.kill(
            clock=clock, reason_code="OPERATOR_KILL", account_binding_fingerprint=ACCOUNT
        )
    coordinator = PaperOperationsCoordinator(
        root=root,
        clock=clock,
        schedule=schedule(),
        environment=environment,
        arming=arming,
        code_commit="deadbeef",
    )
    return coordinator, environment, clock


# ---------------------------------------------------------------------------
# §4, §5, §61 no live mode, safe default
# ---------------------------------------------------------------------------


def test_there_are_exactly_two_modes_and_neither_is_live() -> None:
    assert sorted(str(mode) for mode in OperationsMode) == ["EXTERNAL_PAPER", "OBSERVE_ONLY"]
    assert not any(mode.is_live for mode in OperationsMode)
    assert len(MUTATION_CAPABLE_MODES) == 1


def test_a_fresh_install_is_not_armed(tmp_path: Path) -> None:
    store = ArmingStore(tmp_path / ".operations")

    assert store.current() is None
    assert store.is_armed(mode=OperationsMode.EXTERNAL_PAPER, now=IN_WINDOW) is False


def test_a_fresh_install_is_killed(tmp_path: Path) -> None:
    switch = KillSwitchStore(tmp_path / ".operations").current()

    assert switch.state is KillState.KILLED
    assert switch.may_submit is False
    assert switch.reason_code == Code.CONTROL_STATE_UNVERIFIABLE


def test_an_authorization_cannot_name_a_live_environment() -> None:
    with pytest.raises(OperationsError, match=Code.LIVE_OPERATIONS_NOT_AUTHORIZED):
        PaperOperationsAuthorization(
            authorization_id="x",
            provider="ALPACA",
            account_binding_fingerprint=ACCOUNT,
            operations_policy_fingerprint="p",
            code_commit="c",
            created_at="2026-10-07T18:00:00+00:00",
            environment="LIVE",
        )


@pytest.mark.parametrize("wildcard", ["", "*", "any", "ANY"])
def test_an_authorization_cannot_cover_any_account(wildcard: str) -> None:
    with pytest.raises(OperationsError, match=Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID):
        PaperOperationsAuthorization(
            authorization_id="x",
            provider="ALPACA",
            account_binding_fingerprint=wildcard,
            operations_policy_fingerprint="p",
            code_commit="c",
            created_at="2026-10-07T18:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# §7, §8 time-bounded arming, and arming is not order authorization
# ---------------------------------------------------------------------------


def test_an_expired_authorization_authorises_nothing() -> None:
    grant = authorization(expires_at="2026-10-07T18:30:00+00:00")

    assert grant.is_expired(IN_WINDOW) is True
    with pytest.raises(OperationsError, match=Code.PAPER_OPERATIONS_AUTHORIZATION_EXPIRED):
        grant.require(
            mode=OperationsMode.EXTERNAL_PAPER, now=IN_WINDOW, account_fingerprint=ACCOUNT
        )


def test_an_authorization_with_no_expiry_does_not_invent_one() -> None:
    assert authorization().is_expired("2030-01-01T00:00:00+00:00") is False


def test_an_authorization_for_another_account_is_refused() -> None:
    with pytest.raises(OperationsError, match=Code.OPERATIONS_ACCOUNT_BINDING_MISMATCH):
        authorization().require(
            mode=OperationsMode.EXTERNAL_PAPER, now=IN_WINDOW, account_fingerprint="b" * 64
        )


def test_an_observe_only_grant_does_not_permit_paper_execution() -> None:
    grant = authorization(mode=OperationsMode.OBSERVE_ONLY)

    assert grant.permits(OperationsMode.OBSERVE_ONLY) is True
    assert grant.permits(OperationsMode.EXTERNAL_PAPER) is False
    with pytest.raises(OperationsError, match=Code.PAPER_OPERATIONS_NOT_ARMED):
        grant.require(
            mode=OperationsMode.EXTERNAL_PAPER, now=IN_WINDOW, account_fingerprint=ACCOUNT
        )


def test_arming_states_it_authorises_no_order() -> None:
    body = authorization().body()

    assert "never a security, quantity or order" in body["authorises"]


def test_a_tampered_authorization_is_refused(tmp_path: Path) -> None:
    store = ArmingStore(tmp_path / ".operations")
    store.arm(authorization())
    body = json.loads(store.path.read_text())
    body["allowed_mode"] = "EXTERNAL_PAPER"
    body["account_binding_fingerprint"] = "b" * 64
    store.path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(OperationsError, match=Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID):
        store.current()


def test_disarm_is_not_a_kill(tmp_path: Path) -> None:
    root = tmp_path / ".operations"
    store = ArmingStore(root)
    clock = FakeClock.at(IN_WINDOW)
    store.arm(authorization())
    KillSwitchStore(root).activate(clock=clock, account_binding_fingerprint=ACCOUNT)

    store.disarm(clock=clock, reason="end of day")

    assert store.current() is None
    assert KillSwitchStore(root).current().state is KillState.ACTIVE
    body = json.loads(store.path.read_text())
    assert "Nothing is cancelled" in body["effect"]


# ---------------------------------------------------------------------------
# §9-§15, §84-§86 the kill switch
# ---------------------------------------------------------------------------


def test_a_kill_blocks_submission_and_liquidates_nothing(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)

    switch = store.kill(clock=clock, reason_code="OPERATOR_KILL")

    assert switch.may_submit is False
    assert switch.body()["liquidates_positions"] is False
    assert switch.body()["cancels_unknown_orders"] is False


def test_a_pause_blocks_submission_without_cancelling(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")

    switch = store.pause(clock=FakeClock.at(IN_WINDOW), reason_code="OPERATOR_PAUSE")

    assert switch.state is KillState.PAUSED
    assert switch.may_submit is False
    assert switch.state.is_killed is False
    with pytest.raises(OperationsError, match=Code.PAPER_OPERATIONS_PAUSED):
        switch.require_submission_permitted(context="submitting")


def test_a_kill_survives_a_restart(tmp_path: Path) -> None:
    root = tmp_path / ".operations"
    KillSwitchStore(root).kill(clock=FakeClock.at(IN_WINDOW), reason_code="OPERATOR_KILL")

    assert KillSwitchStore(root).current().state is KillState.KILLED


@pytest.mark.parametrize("corruption", ["{not json", '{"state": "WHATEVER"}', ""])
def test_corrupt_control_state_fails_closed(tmp_path: Path, corruption: str) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(corruption, encoding="utf-8")

    switch = store.current()

    assert switch.state is KillState.KILLED
    assert switch.reason_code == Code.CONTROL_STATE_UNVERIFIABLE


def test_a_tampered_kill_state_fails_closed(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    store.kill(clock=FakeClock.at(IN_WINDOW), reason_code="OPERATOR_KILL")
    body = json.loads(store.path.read_text())
    body["state"] = "ACTIVE"
    store.path.write_text(json.dumps(body), encoding="utf-8")

    assert store.current().state is KillState.KILLED


def test_an_unresolved_owned_order_is_worse_than_a_plain_kill(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    store.kill(clock=clock, reason_code="OPERATOR_KILL")

    switch = store.record_cancellation_outcome(
        clock=clock, cancelled=("ord-1",), unresolved=("ord-2",)
    )

    assert switch.state is KillState.KILLED_WITH_UNRESOLVED_REMOTE_ORDER
    assert switch.unresolved_remote_orders == ("ord-2",)
    with pytest.raises(OperationsError, match=Code.KILLED_WITH_UNRESOLVED_REMOTE_ORDER):
        switch.require_submission_permitted(context="submitting")


def test_every_owned_order_proven_terminal_is_a_plain_kill(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    store.kill(clock=clock, reason_code="OPERATOR_KILL")

    switch = store.record_cancellation_outcome(clock=clock, cancelled=("ord-1",), unresolved=())

    assert switch.state is KillState.KILLED
    assert switch.cancelled_remote_orders == ("ord-1",)


@pytest.mark.parametrize(("reconciled", "incidents"), [(False, 0), (True, 1), (False, 1)])
def test_resuming_after_a_kill_needs_recovery_not_a_restart(
    tmp_path: Path, reconciled: bool, incidents: int
) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    store.kill(clock=FakeClock.at(IN_WINDOW), reason_code="OPERATOR_KILL")

    with pytest.raises(OperationsError, match=Code.KILL_RESUME_NOT_AUTHORIZED):
        store.require_resume_authorized(reconciled=reconciled, open_blocking_incidents=incidents)


def test_a_clean_recovery_permits_re_arming(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    store.kill(clock=FakeClock.at(IN_WINDOW), reason_code="OPERATOR_KILL")

    store.require_resume_authorized(reconciled=True, open_blocking_incidents=0)


def test_an_unresolved_order_blocks_re_arming(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    store.kill(clock=clock, reason_code="OPERATOR_KILL")
    store.record_cancellation_outcome(clock=clock, cancelled=(), unresolved=("ord-2",))

    with pytest.raises(OperationsError, match=Code.KILL_RESUME_NOT_AUTHORIZED):
        store.require_resume_authorized(reconciled=True, open_blocking_incidents=0)


def test_the_critical_auto_kill_list_is_frozen_and_short() -> None:
    assert (
        frozenset(
            {
                "BROKER_ACCOUNT_MISMATCH",
                "BROKER_ENVIRONMENT_NOT_PROVEN_PAPER",
                "LIVE_TRADING_NOT_AUTHORIZED",
                "LEDGER_INTEGRITY_FAILURE",
                "EXECUTION_JOURNAL_INTEGRITY_FAILURE",
                "DUPLICATE_ACCOUNTING_FILL",
                "CONTROL_STATE_UNVERIFIABLE",
            }
        )
        == CRITICAL_AUTO_KILL_REASONS
    )


@pytest.mark.parametrize(
    "ordinary",
    [
        Code.STALE_OPERATIONAL_INPUT,
        Code.BLOCKED_PROVIDER_UNAVAILABLE,
        Code.BLOCKED_RECONCILIATION_BREAK,
    ],
)
def test_ordinary_trouble_blocks_rather_than_kills(ordinary: str) -> None:
    assert IncidentStore.severity_for(ordinary) is IncidentSeverity.BLOCKING
    assert ordinary not in CRITICAL_AUTO_KILL_REASONS


def test_an_unclassified_condition_blocks_rather_than_being_waved_through() -> None:
    assert IncidentStore.severity_for("SOMETHING_NOBODY_HAS_REASONED_ABOUT") is (
        IncidentSeverity.BLOCKING
    )


# ---------------------------------------------------------------------------
# §20-§22, §81-§83 run identity, idempotency and overlap
# ---------------------------------------------------------------------------


def test_a_run_id_is_derived_not_generated() -> None:
    first = operations_run_id(
        schedule_fingerprint="s",
        session=SESSION,
        account_binding_fingerprint=ACCOUNT,
        mode=OperationsMode.EXTERNAL_PAPER,
    )
    second = operations_run_id(
        schedule_fingerprint="s",
        session=SESSION,
        account_binding_fingerprint=ACCOUNT,
        mode=OperationsMode.EXTERNAL_PAPER,
    )

    assert first == second
    assert first.startswith("opsrun-")


@pytest.mark.parametrize(
    "changed",
    [
        {"session": "2026-10-08"},
        {"account_binding_fingerprint": "b" * 64},
        {"mode": OperationsMode.OBSERVE_ONLY},
        {"schedule_fingerprint": "other"},
    ],
)
def test_a_different_slot_is_a_different_run(changed: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "schedule_fingerprint": "s",
        "session": SESSION,
        "account_binding_fingerprint": ACCOUNT,
        "mode": OperationsMode.EXTERNAL_PAPER,
    }

    assert operations_run_id(**base) != operations_run_id(**{**base, **changed})


def test_a_second_invocation_for_the_same_slot_is_a_no_op(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)
    planner = Planner(orders=1)

    first = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    second = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert first.report.run_id == second.report.run_id
    assert second.recovered is True
    assert planner.submitted == 1


def test_an_overlapping_run_is_refused(tmp_path: Path) -> None:
    root = tmp_path / ".operations"
    leases = RunLeaseStore(root)
    leases.acquire(
        run_id="other-run", account_binding_fingerprint=ACCOUNT, clock=FakeClock.at(IN_WINDOW)
    )

    with pytest.raises(OperationsError, match=Code.RUN_ALREADY_ACTIVE):
        leases.acquire(
            run_id="my-run", account_binding_fingerprint=ACCOUNT, clock=FakeClock.at(IN_WINDOW)
        )


def test_a_stale_lease_is_not_taken_on_age(tmp_path: Path) -> None:
    leases = RunLeaseStore(tmp_path / ".operations")
    old = FakeClock.at("2026-10-01T10:00:00+00:00")
    leases.acquire(run_id="crashed-run", account_binding_fingerprint=ACCOUNT, clock=old)
    much_later = FakeClock.at("2026-10-07T19:35:00+00:00")

    with pytest.raises(OperationsError, match=Code.RUN_ALREADY_ACTIVE) as caught:
        leases.acquire(run_id="new-run", account_binding_fingerprint=ACCOUNT, clock=much_later)

    assert "may already have submitted" in str(caught.value)


def test_the_same_run_may_refresh_its_own_lease(tmp_path: Path) -> None:
    leases = RunLeaseStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    leases.acquire(run_id="r1", account_binding_fingerprint=ACCOUNT, clock=clock)

    again = leases.acquire(run_id="r1", account_binding_fingerprint=ACCOUNT, clock=clock)

    assert again.run_id == "r1"
    assert again.is_held is True


def test_a_run_never_releases_another_runs_lease(tmp_path: Path) -> None:
    leases = RunLeaseStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    leases.acquire(run_id="r1", account_binding_fingerprint=ACCOUNT, clock=clock)

    with pytest.raises(OperationsError, match=Code.RUN_LEASE_HELD_ELSEWHERE):
        leases.release(run_id="r2", clock=clock)


# ---------------------------------------------------------------------------
# §24, §26 the run state machine
# ---------------------------------------------------------------------------


def test_a_killed_run_has_no_legal_exit() -> None:
    assert RUN_TRANSITIONS[RunState.KILLED] == frozenset()
    with pytest.raises(OperationsError, match=Code.INVALID_OPERATIONS_RUN_TRANSITION) as caught:
        require_run_transition(RunState.KILLED, RunState.EXECUTING)

    assert "deliberate operator recovery" in str(caught.value)


def test_an_executing_run_cannot_go_back_to_planning() -> None:
    for target in (RunState.READY, RunState.PLANNING):
        with pytest.raises(OperationsError, match=Code.INVALID_OPERATIONS_RUN_TRANSITION):
            require_run_transition(RunState.EXECUTING, target)


def test_a_blocked_run_is_not_reported_as_a_success() -> None:
    assert RunStatus.BLOCKED.is_success is False
    assert RunStatus.MISSED.is_success is False
    assert RunStatus.KILLED.is_success is False
    assert RunStatus.COMPLETED_NO_ACTION.is_success is True


# ---------------------------------------------------------------------------
# §31-§33, §92 freshness
# ---------------------------------------------------------------------------


def test_a_stale_input_is_refused() -> None:
    with pytest.raises(OperationsError, match=Code.STALE_OPERATIONAL_INPUT) as caught:
        stale_input().require_fresh(IN_WINDOW)

    assert "not evidence that it still describes the market" in str(caught.value)


def test_an_input_exactly_at_the_limit_is_fresh() -> None:
    row = OperationalInput(
        name="prices",
        as_of="2026-10-07T19:25:00+00:00",
        source_fingerprint="src",
        policy=FreshnessPolicy(name="prices", max_age_seconds=600),
    )

    assert row.age_seconds(IN_WINDOW) == 600
    assert row.is_fresh(IN_WINDOW) is True


def test_an_input_one_second_past_the_limit_is_stale() -> None:
    row = OperationalInput(
        name="prices",
        as_of="2026-10-07T19:24:59+00:00",
        source_fingerprint="src",
        policy=FreshnessPolicy(name="prices", max_age_seconds=600),
    )

    assert row.is_fresh(IN_WINDOW) is False


def test_a_future_dated_input_is_not_fresh() -> None:
    row = OperationalInput(
        name="prices",
        as_of="2026-10-07T20:00:00+00:00",
        source_fingerprint="src",
        policy=FreshnessPolicy(name="prices", max_age_seconds=600),
    )

    assert row.is_fresh(IN_WINDOW) is False
    with pytest.raises(OperationsError, match=Code.STALE_OPERATIONAL_INPUT) as caught:
        row.require_fresh(IN_WINDOW)

    assert "future" in str(caught.value)


@pytest.mark.parametrize("limit", [0, -1, float("inf")])
def test_an_unbounded_freshness_policy_is_refused(limit: float) -> None:
    with pytest.raises(OperationsError, match=Code.INVALID_FRESHNESS_POLICY):
        FreshnessPolicy(name="prices", max_age_seconds=limit)


# ---------------------------------------------------------------------------
# §69-§75 the golden runs
# ---------------------------------------------------------------------------


def test_a_healthy_observe_only_run_makes_zero_mutations(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, armed=OperationsMode.OBSERVE_ONLY)
    planner = Planner(orders=2)

    outcome = coordinator.run_due(mode=OperationsMode.OBSERVE_ONLY, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.COMPLETED_OBSERVE_ONLY
    assert outcome.external_mutations == 0
    assert planner.submitted == 0
    assert outcome.report.planned_orders == 2


def test_an_observe_only_run_marks_its_plan_not_submitted(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, armed=OperationsMode.OBSERVE_ONLY)

    outcome = coordinator.run_due(
        mode=OperationsMode.OBSERVE_ONLY, plan_and_submit=Planner(orders=2)
    )

    assert all("NOT_SUBMITTED" in row for row in outcome.report.as_json()["would_submit"])


def test_a_healthy_paper_run_executes_and_reconciles(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.COMPLETED_PAPER_EXECUTION
    assert outcome.report.orders_submitted == 1
    assert outcome.report.reconciliation_after == "RECONCILED"
    assert outcome.health is not None
    assert outcome.health.state is HealthState.HEALTHY


def test_a_provider_outage_blocks_and_never_falls_back(tmp_path: Path) -> None:
    env = StubEnvironment(inputs=(fresh_input(),), provider_reachable=False)
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.BLOCKED
    assert outcome.external_mutations == 0
    assert planner.submitted == 0
    assert outcome.health is not None
    assert outcome.health.state is HealthState.BLOCKED
    detail = " ".join(check.detail for check in outcome.health.failures)
    assert "NOT used as a fallback" in detail


def test_a_stale_input_blocks_the_run(tmp_path: Path) -> None:
    env = StubEnvironment(inputs=(stale_input(),))
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.BLOCKED
    assert planner.submitted == 0
    assert any(
        check.reason_code == Code.STALE_OPERATIONAL_INPUT
        for check in (outcome.health.failures if outcome.health else ())
    )


def test_a_kill_before_the_run_submits_nothing(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, kill=KillState.KILLED)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.KILLED
    assert planner.submitted == 0
    assert planner.calls == []
    assert outcome.external_mutations == 0


def test_a_kill_during_the_run_stops_before_the_first_order(tmp_path: Path) -> None:
    coordinator, _, clock = build(tmp_path)
    planner = Planner(orders=1, kill_before_order=1, kill_store=coordinator.kill_store, clock=clock)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.report.status is RunStatus.KILLED
    assert outcome.external_mutations == 0


def test_a_kill_between_two_orders_keeps_the_first_and_stops_the_second(
    tmp_path: Path,
) -> None:
    coordinator, _, clock = build(tmp_path)
    planner = Planner(orders=2, kill_before_order=2, kill_store=coordinator.kill_store, clock=clock)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 1
    assert outcome.report.status is RunStatus.KILLED
    assert coordinator.kill_store.current().state is KillState.KILLED


def test_the_submission_gate_runs_once_per_order(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)
    planner = Planner(orders=3)

    coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.gate_checks == 3


# ---------------------------------------------------------------------------
# §56, §57, §83 missed runs
# ---------------------------------------------------------------------------


def test_a_missed_slot_is_recorded_and_never_run(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, now=AFTER_WINDOW)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.MISSED
    assert planner.submitted == 0
    assert outcome.external_mutations == 0
    assert any("backfill" in row or "never run late" in row for row in outcome.report.blockers)


def test_a_missed_slot_is_journalled(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, now=AFTER_WINDOW)

    coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=Planner())

    states = [entry.to_state for entry in coordinator.journal.entries() if entry.to_state]
    assert RunState.MISSED in states


def test_nothing_runs_before_the_slot_is_due(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, now=BEFORE_DUE)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert any(Code.OPERATIONS_RUN_NOT_DUE in row for row in outcome.report.blockers)


# ---------------------------------------------------------------------------
# §29, §30, §87-§91 health gating
# ---------------------------------------------------------------------------


def test_every_required_check_must_be_reached(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)

    health = coordinator.evaluate_health(
        run_id="r1", mode=OperationsMode.EXTERNAL_PAPER, account=ACCOUNT, now=IN_WINDOW
    )

    assert health.not_reached == ()
    assert set(REQUIRED_HEALTH_CHECKS) <= {check.name for check in health.checks}


def test_health_fails_closed_when_a_check_did_not_run() -> None:
    from stock_screener.paper_operations import PaperOperationsHealthReport

    report = PaperOperationsHealthReport(checks=())

    assert report.state is HealthState.BLOCKED
    assert report.may_mutate is False


def test_there_is_no_degraded_path_to_a_mutation() -> None:
    assert HealthState.HEALTHY.may_mutate is True
    assert HealthState.DEGRADED.may_mutate is False
    assert HealthState.BLOCKED.may_mutate is False
    assert HealthState.UNHEALTHY.may_mutate is False


def test_a_broken_ledger_is_unhealthy_and_kills(tmp_path: Path) -> None:
    env = StubEnvironment(inputs=(fresh_input(),), accounting_ok=False)
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.report.status in {RunStatus.BLOCKED, RunStatus.KILLED}
    assert outcome.health is not None
    assert outcome.health.state is HealthState.UNHEALTHY


def test_an_account_mismatch_kills_and_submits_nothing(tmp_path: Path) -> None:
    env = StubEnvironment(inputs=(fresh_input(),), account="b" * 64)
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.report.status in {RunStatus.BLOCKED, RunStatus.KILLED}


def test_a_paper_proof_failure_blocks_and_kills(tmp_path: Path) -> None:
    env = StubEnvironment(inputs=(fresh_input(),), paper_proven=False)
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert coordinator.kill_store.current().state.is_killed is True


def test_remote_state_unknown_blocks_the_whole_account(tmp_path: Path) -> None:
    env = StubEnvironment(inputs=(fresh_input(),), remote_state_unknown=True)
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.report.status is RunStatus.BLOCKED
    detail = " ".join(check.detail for check in (outcome.health.failures if outcome.health else ()))
    assert "not just the affected" in detail


@pytest.mark.parametrize(
    "contamination",
    [
        {"unexpected_remote_orders": 1},
        {"unexplained_remote_positions": 1},
        {"reconciliation_ok": False},
    ],
)
def test_a_reconciliation_break_blocks_and_is_never_repaired(
    tmp_path: Path, contamination: dict[str, Any]
) -> None:
    env = StubEnvironment(inputs=(fresh_input(),), **contamination)
    coordinator, _, _ = build(tmp_path, env=env)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.report.status is RunStatus.BLOCKED
    assert outcome.manifest["policies"]["incidents"].endswith("NO_AUTO_REPAIR_V1")


def test_an_unarmed_system_submits_nothing(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, armed=None)
    planner = Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.report.status is RunStatus.BLOCKED


# ---------------------------------------------------------------------------
# §39-§42 incidents
# ---------------------------------------------------------------------------


def test_an_incident_is_append_only(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    incident = store.record(
        clock=clock,
        reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
        severity=IncidentSeverity.BLOCKING,
        subject="COMPANY:11",
        account_fingerprint=ACCOUNT,
    )
    store.acknowledge(incident.incident_id, clock=clock, note="looking")
    resolved = store.resolve(incident.incident_id, clock=clock, note="cleared by hand")

    statuses = [event.status for event in resolved.history]
    assert statuses == [IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED]


def test_a_blocking_incident_cannot_resolve_itself(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / ".operations")
    incident = store.record(
        clock=FakeClock.at(IN_WINDOW),
        reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
        severity=IncidentSeverity.BLOCKING,
        subject="COMPANY:11",
        account_fingerprint=ACCOUNT,
    )

    from stock_screener.paper_operations import IncidentEvent

    with pytest.raises(OperationsError, match=Code.INCIDENT_AUTO_REPAIR_NOT_AUTHORIZED):
        incident.with_event(
            IncidentEvent(status=IncidentStatus.RESOLVED, at=IN_WINDOW, actor="AUTOMATIC")
        )


def test_re_detecting_a_condition_touches_one_incident(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    for _ in range(3):
        store.record(
            clock=clock,
            reason_code=Code.BLOCKED_PROVIDER_UNAVAILABLE,
            severity=IncidentSeverity.BLOCKING,
            subject="PROVIDER_REACHABLE",
            account_fingerprint=ACCOUNT,
        )

    assert len(store.all()) == 1
    assert len(store.all()[0].history) == 3


def test_the_incident_store_exposes_no_repair_surface() -> None:
    from stock_screener.paper_operations import incidents

    forbidden = {"repair", "auto_repair", "adopt_remote", "import_remote", "clear_unknown"}

    assert forbidden.isdisjoint(set(dir(incidents)))


# ---------------------------------------------------------------------------
# §25, §27, §93 journal and determinism
# ---------------------------------------------------------------------------


def test_the_run_journal_detects_truncation(tmp_path: Path) -> None:
    journal = OperationsRunJournal(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    for _ in range(3):
        journal.append(clock=clock, event_type=RunEventType.HEALTH_EVALUATED, run_id="r1")
    journal.record_paths()[-1].unlink()

    with pytest.raises(OperationsError, match=Code.OPERATIONS_JOURNAL_TRUNCATED):
        journal.verify()


def test_the_run_journal_detects_tampering(tmp_path: Path) -> None:
    journal = OperationsRunJournal(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    journal.append(clock=clock, event_type=RunEventType.HEALTH_EVALUATED, run_id="r1")
    journal.append(clock=clock, event_type=RunEventType.RECONCILED, run_id="r1")
    path = journal.record_paths()[0]
    body = json.loads(path.read_text())
    body["detail"] = "altered"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(OperationsError, match=Code.OPERATIONS_JOURNAL_CHAIN_BROKEN):
        journal.verify()


def test_the_journal_states_what_it_cannot_detect(tmp_path: Path) -> None:
    journal = OperationsRunJournal(tmp_path / ".operations")
    journal.append(
        clock=FakeClock.at(IN_WINDOW), event_type=RunEventType.HEALTH_EVALUATED, run_id="r1"
    )

    assert "coordinated local rollback" in journal.verify()["not_detectable"]


def test_an_illegal_transition_is_refused_before_it_is_written(tmp_path: Path) -> None:
    journal = OperationsRunJournal(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    journal.append(clock=clock, event_type=RunEventType.RUN_SCHEDULED, run_id="r1")
    before = len(journal.entries())

    with pytest.raises(OperationsError, match=Code.INVALID_OPERATIONS_RUN_TRANSITION):
        journal.append(
            clock=clock,
            event_type=RunEventType.RUN_STATE_CHANGED,
            run_id="r1",
            from_state=RunState.KILLED,
            to_state=RunState.EXECUTING,
        )

    assert len(journal.entries()) == before


def test_the_same_clock_and_inputs_give_the_same_fingerprints(tmp_path: Path) -> None:
    first, _, _ = build(tmp_path / "a")
    second, _, _ = build(tmp_path / "b")

    a = first.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=Planner(orders=1))
    b = second.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=Planner(orders=1))

    assert a.report.run_id == b.report.run_id
    assert a.report.fingerprint() == b.report.fingerprint()


# ---------------------------------------------------------------------------
# §94, §100 safety of artefacts and namespaces
# ---------------------------------------------------------------------------


def test_no_operational_artefact_carries_a_credential(tmp_path: Path) -> None:
    from stock_screener.execution.external import check_no_credentials

    coordinator, _, _ = build(tmp_path)
    outcome = coordinator.run_due(
        mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=Planner(orders=1)
    )

    for name, payload in (
        ("run report", outcome.report.as_json()),
        ("manifest", outcome.manifest),
        ("health", outcome.health.as_json() if outcome.health else {}),
    ):
        check_no_credentials(payload, context=name)
        assert "APCA" not in json.dumps(payload)


@pytest.mark.parametrize(
    "forbidden",
    [".evidence", "pit_research_dataset", "stage9k", ".portfolio", ".execution/journal"],
)
def test_operational_state_cannot_be_written_into_another_domain(
    tmp_path: Path, forbidden: str
) -> None:
    with pytest.raises(OperationsError, match=Code.FORBIDDEN_OPERATIONS_NAMESPACE):
        KillSwitchStore(tmp_path / forbidden / ".operations")


def test_a_control_write_is_read_back_before_it_is_trusted(tmp_path: Path) -> None:
    store = KillSwitchStore(tmp_path / ".operations")

    store.kill(clock=FakeClock.at(IN_WINDOW), reason_code="OPERATOR_KILL")

    assert json.loads(store.path.read_text())["state"] == "KILLED"


def test_the_manifest_binds_what_the_run_operated_under(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)

    outcome = coordinator.run_due(
        mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=Planner(orders=1)
    )
    bindings = outcome.manifest["bindings"]

    for key in (
        "run_id",
        "schedule_fingerprint",
        "authorization_fingerprint",
        "kill_state_fingerprint",
        "health_fingerprint",
        "incident_set_fingerprint",
        "account_binding_fingerprint",
        "operations_journal_head",
    ):
        assert bindings[key], f"{key} is not bound"
    assert outcome.manifest["manifest_fingerprint"]


def test_the_manifest_states_operations_invents_nothing(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)

    outcome = coordinator.run_due(
        mode=OperationsMode.OBSERVE_ONLY, plan_and_submit=Planner(orders=1)
    )

    assert outcome.manifest["authority"]["operations"] == "observes and gates; invents nothing"
    assert outcome.manifest["live_trading"] == "NOT_AUTHORIZED"


def test_the_operations_policy_forbids_the_dangerous_shortcuts() -> None:
    from stock_screener.paper_operations import operations_policy

    never = " ".join(operations_policy()["may_never"])

    for forbidden in (
        "invent a target",
        "repair a reconciliation break",
        "liquidate a position",
        "cancel an order it cannot prove it owns",
        "fall back to the local simulator",
        "backfill a missed run",
        "trade on stale input",
        "reach a live venue",
    ):
        assert forbidden in never


def test_the_execution_and_accounting_versions_are_not_bumped() -> None:
    from stock_screener.paper_operations import (
        REQUIRED_ACCOUNTING_VERSION,
        REQUIRED_EXECUTION_VERSION,
    )

    assert REQUIRED_EXECUTION_VERSION == "EXECUTION_ENGINE_V1_2"
    assert REQUIRED_ACCOUNTING_VERSION == "PORTFOLIO_ACCOUNTING_V2"


# ---------------------------------------------------------------------------
# §45, §67, §68 reporting
# ---------------------------------------------------------------------------


def test_a_status_report_carries_no_account_number(tmp_path: Path) -> None:
    from stock_screener.execution.external import check_no_credentials
    from stock_screener.paper_operations import OperationsStatusReport

    report = OperationsStatusReport(
        mode=OperationsMode.EXTERNAL_PAPER,
        armed=True,
        kill_state=str(KillState.ACTIVE),
        account_binding_fingerprint=ACCOUNT,
        authorization_id="grant-1",
        open_incidents=(("inc-1", "BLOCKING"),),
    )

    check_no_credentials(report.as_json(), context="a status report")
    assert report.as_json()["auto_liquidation"] == "NONE"
    assert report.as_json()["provider_outage_fallback"] == "NONE"
    assert report.as_json()["scheduled_run_backfill"] == "NONE"


def test_a_status_report_separates_last_run_from_last_success() -> None:
    from stock_screener.paper_operations import OperationsStatusReport

    report = OperationsStatusReport(
        mode=OperationsMode.EXTERNAL_PAPER,
        armed=True,
        kill_state=str(KillState.ACTIVE),
        account_binding_fingerprint=ACCOUNT,
        last_run="opsrun-blocked",
        last_successful_run="opsrun-ok",
    )
    lines = "\n".join(report.summary_lines())

    assert "last successful run   : opsrun-ok" in lines
    assert "last run              : opsrun-blocked" in lines


def test_a_run_report_renders_blockers_and_would_submit(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, armed=OperationsMode.OBSERVE_ONLY)

    outcome = coordinator.run_due(
        mode=OperationsMode.OBSERVE_ONLY, plan_and_submit=Planner(orders=2)
    )
    lines = "\n".join(outcome.report.summary_lines())

    assert "WOULD SUBMIT (NOT_SUBMITTED)" in lines
    assert "external mutations : 0" in lines


def test_a_blocked_run_report_names_why(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, env=StubEnvironment(inputs=(stale_input(),)))

    outcome = coordinator.run_due(
        mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=Planner(orders=1)
    )
    lines = "\n".join(outcome.report.summary_lines())

    assert "BLOCKED:" in lines
    assert outcome.report.succeeded is False


def test_a_health_report_renders_its_checks(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path, env=StubEnvironment(inputs=(stale_input(),)))

    health = coordinator.evaluate_health(
        run_id="r1", mode=OperationsMode.EXTERNAL_PAPER, account=ACCOUNT, now=IN_WINDOW
    )
    lines = "\n".join(health.summary_lines())

    assert "[FAIL] REQUIRED_INPUTS_FRESH" in lines
    assert "[pass] KILL_SWITCH_ACTIVE" in lines


def test_health_require_mutation_names_every_failure(tmp_path: Path) -> None:
    coordinator, _, _ = build(
        tmp_path, env=StubEnvironment(inputs=(stale_input(),), provider_reachable=False)
    )

    health = coordinator.evaluate_health(
        run_id="r1", mode=OperationsMode.EXTERNAL_PAPER, account=ACCOUNT, now=IN_WINDOW
    )

    with pytest.raises(OperationsError) as caught:
        health.require_mutation(context="submitting")

    message = str(caught.value)
    assert "REQUIRED_INPUTS_FRESH" in message
    assert "PROVIDER_REACHABLE" in message


def test_health_permits_a_mutation_when_everything_passes(tmp_path: Path) -> None:
    coordinator, _, _ = build(tmp_path)

    health = coordinator.evaluate_health(
        run_id="r1", mode=OperationsMode.EXTERNAL_PAPER, account=ACCOUNT, now=IN_WINDOW
    )

    health.require_mutation(context="submitting")
    assert health.state is HealthState.HEALTHY


# ---------------------------------------------------------------------------
# §16, §17, §93 clock and schedule validation
# ---------------------------------------------------------------------------


def test_a_naive_timestamp_is_refused() -> None:
    from stock_screener.paper_operations import parse_instant

    with pytest.raises(ValueError, match="carries no UTC offset"):
        parse_instant("2026-10-07T19:35:00")


def test_the_fake_clock_counts_reads_and_advances() -> None:
    clock = FakeClock.at(IN_WINDOW)
    clock.now()
    clock.advance(minutes=5)

    assert clock.reads == 1
    assert clock.iso().startswith("2026-10-07T19:40:00")


def test_the_system_clock_is_timezone_aware() -> None:
    from stock_screener.paper_operations import SystemClock

    assert SystemClock().now().tzinfo is not None


def test_an_empty_calendar_is_refused_one_layer_down() -> None:
    """The calendar refuses to be empty before a schedule can hold one.

    The schedule carries its own empty-calendar check as defence in depth, but
    it is unreachable through `ExecutionCalendar.of` — which is the better
    place for the guarantee to live, since every caller of the calendar gets it
    rather than only this one.
    """
    from stock_screener.execution import ExecutionError as ExecError

    with pytest.raises(ExecError, match="UNKNOWN_EXECUTION_SESSION"):
        ExecutionCalendar.of([], name="EMPTY")


def test_the_schedule_states_sessions_are_never_derived_by_arithmetic() -> None:
    assert schedule().body()["session_derivation"] == "explicit calendar; never date + 1"


@pytest.mark.parametrize("bad", ["", "not-a-time", "99:99"])
def test_a_schedule_refuses_an_unreadable_time(bad: str) -> None:
    with pytest.raises(OperationsError, match=Code.INVALID_OPERATIONS_SCHEDULE):
        PaperOperationsSchedule(
            schedule_id="x",
            timezone="UTC",
            run_at_local=bad,
            calendar=ExecutionCalendar.of(["2026-10-07"], name="T"),
        )


def test_a_schedule_without_an_id_is_refused() -> None:
    with pytest.raises(OperationsError, match=Code.INVALID_OPERATIONS_SCHEDULE):
        PaperOperationsSchedule(
            schedule_id="  ",
            timezone="UTC",
            run_at_local="15:30",
            calendar=ExecutionCalendar.of(["2026-10-07"], name="T"),
        )


def test_the_schedule_fingerprint_states_its_policies() -> None:
    body = schedule().body()

    assert body["backfill"].startswith("NONE")
    assert body["session_derivation"] == "explicit calendar; never date + 1"
    assert body["holds_no_strategy"] is True


def test_a_slot_renders_its_window() -> None:
    slot = schedule().slot_for(IN_WINDOW)

    assert slot is not None
    assert slot.as_json()["session"] == SESSION
    assert slot.as_json()["due_at"].startswith("2026-10-07T15:30")


# ---------------------------------------------------------------------------
# §54, §55 lease rendering and recovery
# ---------------------------------------------------------------------------


def test_a_released_lease_frees_the_account(tmp_path: Path) -> None:
    leases = RunLeaseStore(tmp_path / ".operations")
    clock = FakeClock.at(IN_WINDOW)
    leases.acquire(run_id="r1", account_binding_fingerprint=ACCOUNT, clock=clock)
    leases.release(run_id="r1", clock=clock)

    assert leases.held_by() is None
    leases.acquire(run_id="r2", account_binding_fingerprint=ACCOUNT, clock=clock)
    assert leases.held_by() == "r2"


def test_a_heartbeat_requires_holding_the_lease(tmp_path: Path) -> None:
    leases = RunLeaseStore(tmp_path / ".operations")

    with pytest.raises(OperationsError, match=Code.RUN_LEASE_NOT_HELD):
        leases.heartbeat(run_id="r1", clock=FakeClock.at(IN_WINDOW))


def test_a_lease_states_its_stale_policy(tmp_path: Path) -> None:
    leases = RunLeaseStore(tmp_path / ".operations")
    lease = leases.acquire(
        run_id="r1", account_binding_fingerprint=ACCOUNT, clock=FakeClock.at(IN_WINDOW)
    )

    assert "age never grants a lease" in lease.body()["stale_lease_policy"]


# ---------------------------------------------------------------------------
# §23, §78-§80 crash recovery
# ---------------------------------------------------------------------------


def test_a_crashed_run_is_resumed_not_duplicated(tmp_path: Path) -> None:
    root = tmp_path / ".operations"
    coordinator, _, clock = build(tmp_path)
    run_id = (
        schedule()
        .slot_for(IN_WINDOW)
        .run_id(  # type: ignore[union-attr]
            account_binding_fingerprint=ACCOUNT, mode=OperationsMode.EXTERNAL_PAPER
        )
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

    planner = Planner(orders=1)
    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.run_id == run_id
    assert outcome.recovered is True
    assert planner.submitted == 1


def test_a_finished_run_is_never_restarted(tmp_path: Path) -> None:
    coordinator, _, clock = build(tmp_path)
    run_id = (
        schedule()
        .slot_for(IN_WINDOW)
        .run_id(  # type: ignore[union-attr]
            account_binding_fingerprint=ACCOUNT, mode=OperationsMode.EXTERNAL_PAPER
        )
    )
    journal = coordinator.journal
    journal.append(clock=clock, event_type=RunEventType.RUN_SCHEDULED, run_id=run_id)
    journal.append(
        clock=clock,
        event_type=RunEventType.RUN_STATE_CHANGED,
        run_id=run_id,
        from_state=RunState.SCHEDULED,
        to_state=RunState.BLOCKED,
    )

    planner = Planner(orders=1)
    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0
    assert outcome.recovered is True
    assert outcome.report.status is RunStatus.BLOCKED
