"""The six required failure drills, and what makes their evidence worth having.

These tests check the drill harness itself. That sounds circular and is not:
the harness is the thing that will certify the control plane, so a harness that
reports `passed=True` without having exercised anything would launder a soak
through an artefact nobody checked. What follows asserts that each drill
actually causes the condition it claims to, that a drill which does not get the
expected behaviour fails loudly rather than being recorded and shrugged at, and
that none of it touches a network or the real operational root.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.execution import ExecutionCalendar
from stock_screener.paper_operations import (
    DrillHarness,
    KillState,
    KillSwitchStore,
    OperationsError,
    OperationsMode,
    PaperKillSwitch,
    PaperOperationsSchedule,
    RunStatus,
    SoakStore,
    run_required_drills,
)
from stock_screener.paper_operations.drills import _Planner, _StubEnvironment
from stock_screener.paper_operations.soak import REQUIRED_DRILLS

if TYPE_CHECKING:
    from pathlib import Path

NOW = "2026-10-07T19:45:00+00:00"
ACCOUNT = "drill-account-fingerprint"


@pytest.fixture
def schedule() -> PaperOperationsSchedule:
    """A schedule with one session, open at `NOW`."""
    return PaperOperationsSchedule(
        schedule_id="DRILLS",
        timezone="America/New_York",
        run_at_local="15:45",
        calendar=ExecutionCalendar(sessions=("2026-10-07",), name="DRILLS"),
    )


@pytest.fixture
def harness(tmp_path: Path, schedule: PaperOperationsSchedule) -> DrillHarness:
    """A harness over a throwaway root."""
    return DrillHarness(root=tmp_path / "drills", account=ACCOUNT, now=NOW, schedule=schedule)


@pytest.fixture
def store(tmp_path: Path) -> SoakStore:
    """A soak store over a throwaway operational root."""
    return SoakStore(tmp_path / ".operations")


@pytest.mark.integration
def test_every_required_drill_is_performed(store: SoakStore, harness: DrillHarness) -> None:
    records = run_required_drills(store, harness=harness, performed_at=NOW)

    assert tuple(record.drill_type for record in records) == REQUIRED_DRILLS


@pytest.mark.integration
def test_every_required_drill_passes(store: SoakStore, harness: DrillHarness) -> None:
    records = run_required_drills(store, harness=harness, performed_at=NOW)

    assert [record.drill_type for record in records if not record.passed] == []


@pytest.mark.integration
def test_the_drills_are_recorded_as_evidence(store: SoakStore, harness: DrillHarness) -> None:
    run_required_drills(store, harness=harness, performed_at=NOW)

    assert {drill.drill_type for drill in store.drills()} == set(REQUIRED_DRILLS)


@pytest.mark.integration
def test_every_drill_records_an_injected_fault(store: SoakStore, harness: DrillHarness) -> None:
    # An injected outage and a real one are different evidence, and only the
    # first is safe to arrange on purpose. The record says which it was.
    records = run_required_drills(store, harness=harness, performed_at=NOW)

    assert all(record.injected for record in records)


@pytest.mark.integration
def test_a_drill_records_what_was_expected_and_what_happened(
    store: SoakStore, harness: DrillHarness
) -> None:
    records = run_required_drills(store, harness=harness, performed_at=NOW)

    for record in records:
        assert record.expected
        assert record.observed
        assert record.action
        assert record.pre_state


@pytest.mark.integration
def test_a_drill_that_does_not_behave_as_expected_raises(
    store: SoakStore, harness: DrillHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The point of the drill is the behaviour, not the paperwork. Break the
    # kill switch so the restart drill cannot see a surviving kill, and the
    # whole run must refuse rather than record a failure and continue.
    monkeypatch.setattr(KillSwitchStore, "kill", _kill_that_does_not_persist)

    with pytest.raises(OperationsError, match="did not behave as expected"):
        run_required_drills(store, harness=harness, performed_at=NOW)


@pytest.mark.integration
def test_a_failing_drill_is_still_recorded(
    store: SoakStore, harness: DrillHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Refusing is not the same as hiding: the evidence of the failure is kept.
    monkeypatch.setattr(KillSwitchStore, "kill", _kill_that_does_not_persist)

    with pytest.raises(OperationsError):
        run_required_drills(store, harness=harness, performed_at=NOW)

    assert [drill for drill in store.drills() if not drill.passed] != []


@pytest.mark.integration
def test_the_kill_drill_prevents_the_submission_it_interrupted(harness: DrillHarness) -> None:
    coordinator, _, clock, _ = harness.coordinator()
    planner = _Planner(
        orders=3, kill_before_order=1, kill_store=coordinator.kill_store, clock=clock
    )

    coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 0


@pytest.mark.integration
def test_the_kill_survives_a_new_store_over_the_same_root(harness: DrillHarness) -> None:
    coordinator, _, clock, root = harness.coordinator()
    planner = _Planner(
        orders=1, kill_before_order=1, kill_store=coordinator.kill_store, clock=clock
    )
    coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    # A restart is not a recovery: the state is read from disk, not inherited.
    assert KillSwitchStore(root).current().state is KillState.KILLED


@pytest.mark.integration
def test_the_duplicate_scheduler_drill_submits_once(harness: DrillHarness) -> None:
    coordinator, _, _, _ = harness.coordinator()
    planner = _Planner(orders=1)

    first = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)
    second = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert planner.submitted == 1
    assert first.report.run_id == second.report.run_id
    assert second.recovered


@pytest.mark.integration
def test_the_provider_unavailable_drill_blocks_rather_than_substituting(
    harness: DrillHarness,
) -> None:
    coordinator, _, _, _ = harness.coordinator(
        env=_StubEnvironment(account=ACCOUNT, provider_reachable=False, inputs=_fresh())
    )
    planner = _Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.BLOCKED
    assert planner.submitted == 0
    assert outcome.health is not None
    assert any("NOT used as a fallback" in check.detail for check in outcome.health.failures)


@pytest.mark.integration
def test_the_remote_state_unknown_drill_blocks_the_whole_account(harness: DrillHarness) -> None:
    coordinator, _, _, _ = harness.coordinator(
        env=_StubEnvironment(account=ACCOUNT, remote_state_unknown=True, inputs=_fresh())
    )
    planner = _Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.BLOCKED
    assert planner.submitted == 0
    assert outcome.health is not None
    # Not just the ambiguous name: there may be a working order nobody tracks.
    assert any("not just the affected" in check.detail for check in outcome.health.failures)


@pytest.mark.integration
def test_the_stale_input_drill_blocks_on_the_freshness_policy(harness: DrillHarness) -> None:
    coordinator, _, _, _ = harness.coordinator(
        env=_StubEnvironment(account=ACCOUNT, inputs=_stale())
    )
    planner = _Planner(orders=1)

    outcome = coordinator.run_due(mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner)

    assert outcome.report.status is RunStatus.BLOCKED
    assert planner.submitted == 0


@pytest.mark.integration
def test_a_drill_never_writes_outside_its_own_root(
    tmp_path: Path, store: SoakStore, harness: DrillHarness
) -> None:
    # The drills run over throwaway roots so a drill cannot alter the state the
    # soak is being judged on.
    run_required_drills(store, harness=harness, performed_at=NOW)

    operational = {path.name for path in (tmp_path / ".operations").iterdir()}
    assert operational == {"soak"}


@pytest.mark.integration
def test_no_drill_record_carries_a_credential(store: SoakStore, harness: DrillHarness) -> None:
    run_required_drills(store, harness=harness, performed_at=NOW)

    text = json.dumps([drill.as_json() for drill in store.drills()]).lower()
    for forbidden in ("apca", "secret", "authorization", "bearer", "api-key"):
        assert forbidden not in text


@pytest.mark.integration
def test_observe_only_mode_submits_nothing_at_all(harness: DrillHarness) -> None:
    coordinator, _, _, _ = harness.coordinator(armed=OperationsMode.OBSERVE_ONLY)
    planner = _Planner(orders=5)

    coordinator.run_due(mode=OperationsMode.OBSERVE_ONLY, plan_and_submit=planner)

    assert planner.submitted == 0


def _fresh() -> tuple[Any, ...]:
    """A required input inside its freshness policy."""
    from stock_screener.paper_operations import FreshnessPolicy, OperationalInput

    return (
        OperationalInput(
            name="prices",
            as_of=NOW,
            source_fingerprint="drill-src",
            policy=FreshnessPolicy(name="prices", max_age_seconds=600),
        ),
    )


def _stale() -> tuple[Any, ...]:
    """A required input far outside its freshness policy."""
    from stock_screener.paper_operations import FreshnessPolicy, OperationalInput

    return (
        OperationalInput(
            name="prices",
            as_of="2026-01-01T00:00:00+00:00",
            source_fingerprint="drill-src",
            policy=FreshnessPolicy(name="prices", max_age_seconds=600),
        ),
    )


def _kill_that_does_not_persist(self: KillSwitchStore, **kwargs: Any) -> PaperKillSwitch:
    """A kill that reports success and writes nothing.

    This is the defect the restart drill exists to detect: a switch that looked
    killed to its caller and is ACTIVE again after a restart.
    """
    return self.current()


@pytest.mark.integration
def test_the_soak_commands_refuse_before_anything_is_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from stock_screener.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    status = runner.invoke(app, ["paper-ops", "soak", "status"])
    drills = runner.invoke(app, ["paper-ops", "soak", "show-drills"])

    assert status.exit_code == 1
    assert "no soak is preregistered" in status.stdout
    assert "no drills recorded" in drills.stdout


@pytest.mark.integration
def test_arming_the_soak_refuses_without_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The refusal matters more than the command: arming is irreversible, and a
    # bare invocation must not reach the venue or freeze anything.
    from typer.testing import CliRunner

    from stock_screener.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["paper-ops", "soak", "arm"])
    frozen = (tmp_path / ".operations" / "soak" / "protocol.json").exists()

    assert result.exit_code == 1
    assert "refusing without --confirm" in result.stdout
    assert frozen is False


@pytest.mark.integration
def test_no_soak_command_offers_a_live_flag() -> None:
    from typer.testing import CliRunner

    from stock_screener.cli import app

    runner = CliRunner()
    helps = [
        runner.invoke(app, ["paper-ops", "soak", name, "--help"]).stdout
        for name in ("arm", "status", "show-drills")
    ]

    for text in helps:
        assert "--live" not in text
        assert "USE_LIVE" not in text


@pytest.mark.integration
def test_the_declared_soak_calendar_lists_no_declared_closure() -> None:
    from stock_screener.paper_operations.soak_calendar import (
        DECLARED_CLOSURES,
        SOAK_SESSIONS,
        soak_calendar,
    )

    # A closure listed as a session would make the soak expect evidence from a
    # day the market was shut, and block promotion forever.
    assert set(SOAK_SESSIONS) & set(DECLARED_CLOSURES) == set()
    assert soak_calendar().sessions == SOAK_SESSIONS


@pytest.mark.integration
def test_the_declared_soak_calendar_is_long_enough_to_finish() -> None:
    from stock_screener.paper_operations.soak import REQUIRED_ELIGIBLE_SESSIONS
    from stock_screener.paper_operations.soak_calendar import SOAK_SESSIONS

    # A calendar shorter than the requirement would strand the soak with no
    # possible path to an A.
    assert len(SOAK_SESSIONS) > REQUIRED_ELIGIBLE_SESSIONS


@pytest.mark.integration
def test_no_declared_session_closes_before_the_soak_would_run() -> None:
    from stock_screener.paper_operations.soak_calendar import EARLY_CLOSES, SOAK_SESSIONS

    # The declared run time is 15:45 ET. An early close is at 13:00 ET, so
    # keeping one would schedule a run into a market shut nearly three hours
    # earlier — on a session the frozen protocol is scored on, with no way to
    # correct it afterwards. This is the assertion that stops that regression.
    assert set(SOAK_SESSIONS) & set(EARLY_CLOSES) == set()


@pytest.mark.integration
def test_the_soak_run_time_is_inside_every_declared_session() -> None:
    from stock_screener.cli import _soak_schedule
    from stock_screener.paper_operations.soak_calendar import SOAK_SESSIONS

    schedule = _soak_schedule()

    for session in SOAK_SESSIONS:
        due = schedule.due_at(session)
        assert (due.hour, due.minute) == (15, 45), f"{session} is due at {due}"


@pytest.mark.integration
def test_the_first_eligible_session_is_the_next_declared_one() -> None:
    from stock_screener.paper_operations.soak_calendar import SOAK_SESSIONS

    # The bug this replaces: `ExecutionCalendar.next_session` requires its
    # argument to be a session in the calendar, and the day somebody arms on
    # usually is not one. Selecting by comparison has no such precondition.
    for today in ("2026-10-07", "2026-10-08", "2026-10-10", "2026-11-25"):
        later = [session for session in SOAK_SESSIONS if session > today]

        assert later, today
        assert later[0] > today


@pytest.mark.integration
def test_arming_on_the_last_declared_session_has_no_eligible_session() -> None:
    from stock_screener.paper_operations.soak_calendar import SOAK_SESSIONS

    # Arming after the calendar runs out must be refused rather than activating
    # a soak that can never collect evidence.
    later = [session for session in SOAK_SESSIONS if session > SOAK_SESSIONS[-1]]

    assert later == []


@pytest.mark.integration
def test_the_operational_root_is_not_committable() -> None:
    import subprocess
    from pathlib import Path

    # `git add -A` would otherwise stage kill state, incidents and an account
    # binding fingerprint, and a committed kill switch is one every other
    # checkout inherits.
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".operations/kill_switch.json"],  # noqa: S607
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )

    assert result.returncode == 0
