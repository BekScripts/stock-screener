"""P2.4 — the frozen soak protocol and its mechanical scoring.

These tests are about the integrity of the *evidence*, not the behaviour of the
system under test. P2.3's suite established that the control plane refuses the
right things; this one establishes that a soak cannot be made to look like it
passed when it did not.

The three failures worth most of the effort: a session from before the rules
were frozen counting as evidence, a threshold being edited after the data
arrived, and a hand-written file standing in for a run. Each is closed in code
and each has a test here.

No test makes a network request or submits anything.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.execution import ExecutionCalendar
from stock_screener.paper_operations import (
    REQUIRED_DRILLS,
    REQUIRED_ELIGIBLE_SESSIONS,
    Code,
    DrillRecord,
    DrillType,
    OperationsError,
    OperationsMode,
    PaperOperationsSchedule,
    PaperShadowSessionObservation,
    PaperShadowSoakActivation,
    PaperShadowSoakProtocol,
    RunStatus,
    SoakState,
    SoakStore,
    SoakVerdict,
    evaluate_soak,
)
from stock_screener.paper_operations.soak_report import evaluate_soak as _evaluate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

ACCOUNT = "a" * 64
COMMIT = "68709d22429da3e57473afe36165137cd72df684"
SESSIONS = [
    "2026-10-08",
    "2026-10-09",
    "2026-10-12",
    "2026-10-13",
    "2026-10-14",
    "2026-10-15",
    "2026-10-16",
    "2026-10-19",
    "2026-10-20",
    "2026-10-21",
    "2026-10-22",
    "2026-10-23",
    "2026-10-26",
    "2026-10-27",
    "2026-10-28",
    "2026-10-29",
    "2026-10-30",
    "2026-11-02",
    "2026-11-03",
    "2026-11-04",
    "2026-11-05",
    "2026-11-06",
]


def calendar() -> ExecutionCalendar:
    """The frozen session calendar."""
    return ExecutionCalendar.of(SESSIONS, name="SOAK-TEST")


def schedule() -> PaperOperationsSchedule:
    """The schedule producing soak slots."""
    return PaperOperationsSchedule(
        schedule_id="soak-daily",
        timezone="America/New_York",
        run_at_local="15:30",
        calendar=calendar(),
        execution_window_minutes=20,
    )


def protocol(**overrides: Any) -> PaperShadowSoakProtocol:
    """The frozen soak protocol."""
    base: dict[str, Any] = {
        "protocol_id": "p2-4-shadow-soak",
        "code_commit": COMMIT,
        "provider": "ALPACA",
        "account_binding_fingerprint": ACCOUNT,
        "schedule_fingerprint": schedule().fingerprint(),
        "calendar_fingerprint": calendar().fingerprint(),
        "health_policy_fingerprint": "health-fp",
        "freshness_policy_fingerprint": "fresh-fp",
    }
    return PaperShadowSoakProtocol(**{**base, **overrides})


def activation(proto: PaperShadowSoakProtocol) -> PaperShadowSoakActivation:
    """The activation artefact."""
    return PaperShadowSoakActivation(
        protocol_fingerprint=proto.fingerprint(),
        code_commit=COMMIT,
        account_binding_fingerprint=ACCOUNT,
        activated_at="2026-10-07T20:30:00+00:00",
        first_eligible_session="2026-10-08",
        calendar_fingerprint=calendar().fingerprint(),
        schedule_fingerprint=schedule().fingerprint(),
        starting_reconciliation="RECONCILED",
        starting_health="HEALTHY",
    )


def observation(
    session: str,
    *,
    proto: PaperShadowSoakProtocol | None = None,
    status: RunStatus = RunStatus.COMPLETED_OBSERVE_ONLY,
    mode: OperationsMode = OperationsMode.OBSERVE_ONLY,
    submitted: int = 0,
    order_ids: tuple[str, ...] = (),
    fills: int = 0,
    events: tuple[str, ...] = (),
    fresh: bool = True,
    recon_before: str = "RECONCILED",
    recon_after: str = "",
    day_orders: tuple[str, ...] = (),
    positions: tuple[str, ...] = (),
    commit: str = COMMIT,
) -> PaperShadowSessionObservation:
    """One session's evidence."""
    return PaperShadowSessionObservation(
        protocol_fingerprint=(proto or protocol()).fingerprint(),
        session=session,
        scheduled_for=f"{session}T15:30:00-04:00",
        observed_at=f"{session}T19:40:00+00:00",
        operations_run_id=f"opsrun-{session}",
        code_commit=commit,
        account_binding_fingerprint=ACCOUNT,
        mode=mode,
        run_status=status,
        health_before="HEALTHY",
        reconciliation_before=recon_before,
        reconciliation_after=recon_after,
        inputs_fresh=fresh,
        orders_submitted=submitted,
        provider_order_ids=order_ids,
        fills_observed=fills,
        accounting_events=events,
        kill_state="ACTIVE",
        unexpected_open_day_orders=day_orders,
        unexpected_positions=positions,
    )


def armed_store(tmp_path: Path, proto: PaperShadowSoakProtocol | None = None) -> SoakStore:
    """A store with a frozen, activated protocol."""
    proto = proto or protocol()
    store = SoakStore(tmp_path / ".operations")
    store.freeze_protocol(proto)
    store.activate(activation(proto))
    return store


def all_drills_passed(store: SoakStore) -> None:
    """Record a passing record for every required drill."""
    for drill in REQUIRED_DRILLS:
        store.record_drill(
            DrillRecord(
                drill_type=drill,
                performed_at="2026-10-08T12:00:00+00:00",
                expected="fail closed",
                observed="failed closed",
                passed=True,
                external_mutations=0,
            )
        )


def report_for(store: SoakStore, *, now: str = "2026-11-07T00:00:00+00:00") -> Any:
    """Score a store."""
    return evaluate_soak(store, calendar=calendar(), schedule=schedule(), now=now)


# ---------------------------------------------------------------------------
# §2, §49 the prospective boundary
# ---------------------------------------------------------------------------


def test_a_session_before_activation_cannot_count(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    with pytest.raises(OperationsError, match=Code.SOAK_RETROSPECTIVE_OBSERVATION) as caught:
        store.record_observation(observation("2026-10-06"))

    assert "chosen after the fact" in str(caught.value)


def test_an_observation_needs_an_activated_soak(tmp_path: Path) -> None:
    store = SoakStore(tmp_path / ".operations")

    with pytest.raises(OperationsError, match=Code.SOAK_NOT_ACTIVATED):
        store.record_observation(observation("2026-10-08"))


def test_the_first_eligible_session_counts(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    store.record_observation(observation("2026-10-08"))

    assert len(store.qualifying_observations()) == 1


def test_eligible_sessions_are_computed_from_the_calendar(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    elapsed = store.eligible_sessions(
        calendar=calendar(), schedule=schedule(), now="2026-10-16T20:00:00+00:00"
    )

    # 2026-10-16T20:00Z is 16:00 ET, after that session's 15:30-15:50 window
    assert elapsed == (
        "2026-10-08",
        "2026-10-09",
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
    )
    assert "2026-10-10" not in elapsed, "Saturday is not a session"
    assert "2026-10-11" not in elapsed, "Sunday is not a session"


def test_a_session_whose_window_has_not_closed_is_not_yet_eligible(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    elapsed = store.eligible_sessions(
        calendar=calendar(), schedule=schedule(), now="2026-10-08T19:35:00+00:00"
    )

    assert elapsed == ()


# ---------------------------------------------------------------------------
# §4, §16, §62 no moving the goalposts
# ---------------------------------------------------------------------------


def test_the_gates_live_inside_the_protocol_fingerprint() -> None:
    twenty = protocol()
    five = protocol(required_sessions=5)

    assert twenty.fingerprint() != five.fingerprint()
    assert twenty.promotion_gates["required"]["eligible_sessions_observed"] == 20


def test_a_frozen_protocol_cannot_be_replaced(tmp_path: Path) -> None:
    store = SoakStore(tmp_path / ".operations")
    store.freeze_protocol(protocol())

    with pytest.raises(OperationsError, match=Code.SOAK_PROTOCOL_DRIFT) as caught:
        store.freeze_protocol(protocol(required_sessions=5))

    assert "restart the qualifying count from zero" in str(caught.value)


def test_refreezing_the_same_protocol_is_a_no_op(tmp_path: Path) -> None:
    store = SoakStore(tmp_path / ".operations")
    store.freeze_protocol(protocol())

    store.freeze_protocol(protocol())

    assert store.protocol() is not None


def test_an_edited_protocol_file_is_refused(tmp_path: Path) -> None:
    store = SoakStore(tmp_path / ".operations")
    store.freeze_protocol(protocol())
    body = json.loads(store.protocol_path.read_text())
    body["required_sessions"] = 3
    store.protocol_path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(OperationsError, match=Code.SOAK_PROTOCOL_DRIFT) as caught:
        store.protocol()

    assert "no longer scores against the rules that were frozen" in str(caught.value)


def test_an_observation_from_another_protocol_does_not_count(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    with pytest.raises(OperationsError, match=Code.SOAK_PROTOCOL_DRIFT):
        store.record_observation(observation("2026-10-08", proto=protocol(required_sessions=5)))


def test_an_observation_from_another_build_does_not_count(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    with pytest.raises(OperationsError, match=Code.SOAK_PROTOCOL_DRIFT) as caught:
        store.record_observation(observation("2026-10-08", commit="f" * 40))

    assert "restarts the qualifying count" in str(caught.value)


def test_a_protocol_must_name_the_build_it_judges() -> None:
    with pytest.raises(OperationsError, match=Code.INVALID_SOAK_PROTOCOL) as caught:
        protocol(code_commit="")

    assert "could claim this evidence as its own" in str(caught.value)


def test_a_soak_cannot_be_activated_twice(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    with pytest.raises(OperationsError, match=Code.SOAK_ALREADY_ACTIVATED) as caught:
        store.activate(activation(protocol()))

    assert "discard the observations already" in str(caught.value)


# ---------------------------------------------------------------------------
# §12, §43 evidence is append-only and not hand-written
# ---------------------------------------------------------------------------


def test_an_observation_is_not_overwritten(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    store.record_observation(observation("2026-10-08"))

    with pytest.raises(OperationsError, match=Code.SOAK_OBSERVATION_INVALID) as caught:
        store.record_observation(observation("2026-10-08", submitted=1, order_ids=("x",)))

    assert "so the original evidence survives" in str(caught.value)


def test_recording_the_identical_observation_twice_is_a_no_op(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    store.record_observation(observation("2026-10-08"))
    store.record_observation(observation("2026-10-08"))

    assert len(store.qualifying_observations()) == 1


def test_a_correction_preserves_the_original(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    original = store.record_observation(observation("2026-10-08"))
    correction = observation("2026-10-08", submitted=1, order_ids=("remote-1",), fills=1)

    store.supersede(original, correction, reason="fill observed after the window")

    assert len(store.observations()) == 2
    assert len(store.qualifying_observations()) == 1
    assert store.qualifying_observations()[0].orders_submitted == 1


def test_a_hand_edited_observation_is_refused(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    recorded = store.record_observation(
        observation("2026-10-08", submitted=1, order_ids=("a", "a"))
    )
    path = store.observations_directory / f"{recorded.observation_id}.json"
    body = json.loads(path.read_text())
    body["provider_order_ids"] = ["a"]
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(OperationsError, match=Code.SOAK_OBSERVATION_INVALID) as caught:
        store.observations()

    assert "not from a hand-edited file" in str(caught.value)


# ---------------------------------------------------------------------------
# §13 soak state fails safe
# ---------------------------------------------------------------------------


def test_a_missing_soak_state_is_not_started(tmp_path: Path) -> None:
    assert SoakStore(tmp_path / ".operations").state() is SoakState.NOT_STARTED


@pytest.mark.parametrize("corruption", ["{not json", '{"state": "WHATEVER"}', ""])
def test_a_corrupt_soak_state_never_reads_as_completed(tmp_path: Path, corruption: str) -> None:
    store = armed_store(tmp_path)
    store.state_path.write_text(corruption, encoding="utf-8")

    assert store.state() is SoakState.NOT_STARTED
    assert store.state() is not SoakState.COMPLETED


def test_activation_arms_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    assert store.state() is SoakState.ARMED


def test_the_first_observation_moves_the_soak_to_running(tmp_path: Path) -> None:
    store = armed_store(tmp_path)

    store.record_observation(observation("2026-10-08"))

    assert store.state() is SoakState.RUNNING


# ---------------------------------------------------------------------------
# §38, §72 mechanical scoring, no success rate
# ---------------------------------------------------------------------------


def test_nineteen_clean_sessions_and_one_duplicate_order_fails(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:19]:
        store.record_observation(observation(session))
    store.record_observation(
        observation(
            SESSIONS[19],
            mode=OperationsMode.EXTERNAL_PAPER,
            status=RunStatus.COMPLETED_PAPER_EXECUTION,
            submitted=2,
            order_ids=("remote-1", "remote-1"),
            fills=2,
            events=("e1", "e2"),
            recon_after="RECONCILED",
        )
    )

    report = report_for(store)

    assert report.verdict == SoakVerdict.FAILED
    assert report.observed == 20
    assert "duplicate_remote_orders" in [gate.name for gate in report.failed_gates]
    # 19 of 20 clean is not 95%: the soak failed. The absence of any rate field
    # is asserted in test_the_report_has_no_pass_rate_field.
    assert report.failed_zero_tolerance != ()


def test_the_report_has_no_pass_rate_field(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(observation("2026-10-08"))

    body = json.dumps(report_for(store).as_json()).lower()

    for forbidden in ("pass_rate", "success_rate", "percent", "pct"):
        assert forbidden not in body


def test_a_clean_twenty_session_soak_with_all_drills_passes(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:20]:
        store.record_observation(observation(session))

    report = report_for(store, now="2026-11-05T20:00:00+00:00")

    assert report.verdict == SoakVerdict.PASSED
    assert report.observed == 20
    assert report.failed_gates == ()


def test_an_incomplete_soak_is_in_progress(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:5]:
        store.record_observation(observation(session))

    report = report_for(store, now="2026-10-15T20:00:00+00:00")

    assert report.verdict == SoakVerdict.IN_PROGRESS
    assert report.observed == 5
    assert report.remaining == 15


def test_outstanding_drills_prevent_a_pass(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    for session in SESSIONS[:20]:
        store.record_observation(observation(session))

    report = report_for(store, now="2026-11-05T20:00:00+00:00")

    assert report.verdict == SoakVerdict.IN_PROGRESS
    assert "required_drills_passed" in [gate.name for gate in report.failed_gates]
    assert len(report.drills_passed) == 0


def test_a_failed_drill_does_not_count_as_passed(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    store.record_drill(
        DrillRecord(
            drill_type=DrillType.KILL_RESTART,
            performed_at="2026-10-08T12:00:00+00:00",
            expected="no submission after kill",
            observed="a submission occurred",
            passed=False,
        )
    )

    assert DrillType.KILL_RESTART not in store.passed_drills()


def test_a_halted_soak_reports_halted(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:3]:
        store.record_observation(observation(session))
    store.set_state(SoakState.HALTED, detail="semantic defect found")

    report = report_for(store, now="2026-10-13T20:00:00+00:00")

    assert report.verdict == SoakVerdict.HALTED


# ---------------------------------------------------------------------------
# §18-§33 the individual gates
# ---------------------------------------------------------------------------


def test_a_stale_data_execution_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(
        observation(
            "2026-10-08",
            mode=OperationsMode.EXTERNAL_PAPER,
            status=RunStatus.COMPLETED_PAPER_EXECUTION,
            submitted=1,
            order_ids=("remote-1",),
            fresh=False,
            recon_after="RECONCILED",
        )
    )

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "stale_data_executions" in [gate.name for gate in report.failed_gates]


def test_a_backfilled_missed_session_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(
        observation(
            "2026-10-08",
            status=RunStatus.MISSED,
            mode=OperationsMode.EXTERNAL_PAPER,
            submitted=1,
            order_ids=("remote-1",),
        )
    )

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "missed_run_backfills" in [gate.name for gate in report.failed_gates]


def test_a_missed_session_without_a_trade_is_not_a_failure(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(observation("2026-10-08", status=RunStatus.MISSED))

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert "missed_run_backfills" not in [gate.name for gate in report.failed_gates]
    assert report.missed == 1


def test_an_execution_that_did_not_reconcile_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(
        observation(
            "2026-10-08",
            mode=OperationsMode.EXTERNAL_PAPER,
            status=RunStatus.COMPLETED_PAPER_EXECUTION,
            submitted=1,
            order_ids=("remote-1",),
            recon_after="BREAKS_BLOCKING",
        )
    )

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "ignored_reconciliation_breaks" in [gate.name for gate in report.failed_gates]


def test_a_duplicate_accounting_event_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(
        observation(
            "2026-10-08",
            mode=OperationsMode.EXTERNAL_PAPER,
            status=RunStatus.COMPLETED_PAPER_EXECUTION,
            submitted=1,
            order_ids=("remote-1",),
            fills=1,
            events=("e1", "e1"),
            recon_after="RECONCILED",
        )
    )

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "duplicate_accounting_events" in [gate.name for gate in report.failed_gates]


def test_more_accounting_events_than_fills_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(
        observation(
            "2026-10-08",
            mode=OperationsMode.EXTERNAL_PAPER,
            status=RunStatus.COMPLETED_PAPER_EXECUTION,
            submitted=1,
            order_ids=("remote-1",),
            fills=1,
            events=("e1", "e2"),
            recon_after="RECONCILED",
        )
    )

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "duplicate_admitted_fills" in [gate.name for gate in report.failed_gates]


def test_an_unexpected_open_day_order_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(observation("2026-10-08", day_orders=("remote-9",)))

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "unexpected_open_day_orders" in [gate.name for gate in report.failed_gates]


def test_an_unexpected_remote_position_fails_the_soak(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(observation("2026-10-08", positions=("COMPANY:99",)))

    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    assert report.verdict == SoakVerdict.FAILED
    assert "unexpected_remote_positions" in [gate.name for gate in report.failed_gates]


def test_an_eligible_session_with_no_record_blocks_a_pass(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in ("2026-10-08", "2026-10-12"):
        store.record_observation(observation(session))

    report = report_for(store, now="2026-10-13T20:00:00+00:00")

    assert "2026-10-09" in report.missing_sessions
    assert "eligible_sessions_with_no_record" in [gate.name for gate in report.failed_gates]
    # a closed window with no record is missing evidence, not a safety breach:
    # it blocks an A without being reported as a violation
    assert report.verdict == SoakVerdict.IN_PROGRESS
    assert report.failed_zero_tolerance == ()


def test_an_unresolved_blocking_incident_blocks_a_pass(tmp_path: Path) -> None:
    from stock_screener.paper_operations import (
        IncidentEvent,
        IncidentSeverity,
        IncidentStatus,
        OperationalIncident,
    )

    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:20]:
        store.record_observation(observation(session))
    incident = OperationalIncident(
        incident_id="inc-1",
        reason_code=Code.BLOCKED_RECONCILIATION_BREAK,
        severity=IncidentSeverity.BLOCKING,
        detected_at="2026-10-20T19:40:00+00:00",
        history=(IncidentEvent(status=IncidentStatus.OPEN, at="2026-10-20T19:40:00+00:00"),),
    )

    report = evaluate_soak(
        store,
        calendar=calendar(),
        schedule=schedule(),
        now="2026-11-05T20:00:00+00:00",
        incidents=(incident,),
    )

    assert "unresolved_blocking_incidents" in [gate.name for gate in report.failed_gates]
    # an open incident is the system refusing to trade, which blocks an A
    # without being a safety violation
    assert report.verdict == SoakVerdict.IN_PROGRESS


# ---------------------------------------------------------------------------
# §34 performance is not a gate
# ---------------------------------------------------------------------------


def test_the_protocol_states_what_it_does_not_gate_on() -> None:
    not_gated = protocol().promotion_gates["not_gated_on"]

    for metric in ("profit", "Sharpe", "win rate", "return", "alpha", "drawdown"):
        assert metric in not_gated


def test_a_soak_with_no_trades_at_all_can_pass(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:20]:
        store.record_observation(observation(session))

    report = report_for(store, now="2026-11-05T20:00:00+00:00")

    assert report.total_orders == 0
    assert report.verdict == SoakVerdict.PASSED


# ---------------------------------------------------------------------------
# §46, §47 rebuildability and the soak fingerprint
# ---------------------------------------------------------------------------


def test_the_report_is_rebuilt_from_observations_not_a_summary_file(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    for session in SESSIONS[:4]:
        store.record_observation(observation(session))

    first = report_for(store, now="2026-10-14T20:00:00+00:00")
    reopened = SoakStore(tmp_path / ".operations")
    second = evaluate_soak(
        reopened, calendar=calendar(), schedule=schedule(), now="2026-10-14T20:00:00+00:00"
    )

    assert first.fingerprint() == second.fingerprint()
    assert not (store.directory / "summary.json").exists()


def test_the_soak_fingerprint_binds_every_observation(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(observation("2026-10-08"))
    before = report_for(store, now="2026-10-09T20:00:00+00:00").fingerprint()

    store.record_observation(observation("2026-10-09"))
    after = report_for(store, now="2026-10-12T20:00:00+00:00").fingerprint()

    assert before != after


def test_the_soak_fingerprint_binds_the_protocol_and_commit(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    body = report_for(store, now="2026-10-08T20:00:00+00:00").as_json()

    assert body["protocol_fingerprint"] == protocol().fingerprint()
    assert body["qualifying_code_commit"] == COMMIT


# ---------------------------------------------------------------------------
# §44, §60 namespace and drill evidence
# ---------------------------------------------------------------------------


def test_soak_evidence_lives_in_its_own_namespace(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    store.record_observation(observation("2026-10-08"))

    assert store.directory.name == "soak"
    assert store.observations_directory.parent == store.directory
    assert list(store.observations_directory.glob("*.json"))


@pytest.mark.parametrize(
    "forbidden", [".evidence", "pit_research_dataset", "stage9k", ".portfolio"]
)
def test_soak_evidence_cannot_be_written_into_another_domain(
    tmp_path: Path, forbidden: str
) -> None:
    with pytest.raises(OperationsError, match=Code.FORBIDDEN_OPERATIONS_NAMESPACE):
        SoakStore(tmp_path / forbidden / ".operations")


def test_a_drill_record_carries_its_full_evidence(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    drill = store.record_drill(
        DrillRecord(
            drill_type=DrillType.PROVIDER_UNAVAILABLE,
            performed_at="2026-10-08T12:00:00+00:00",
            run_id="opsrun-x",
            pre_state="ACTIVE, HEALTHY",
            action="injected a provider connection failure",
            expected="BLOCKED, 0 mutations, no simulator fallback",
            observed="BLOCKED, 0 mutations, no fallback",
            external_mutations=0,
            post_state="ACTIVE, BLOCKED",
            reconciliation="not reached",
            passed=True,
            injected=True,
        )
    )
    body = drill.as_json()

    for key in ("pre_state", "action", "expected", "observed", "post_state"):
        assert body[key]
    assert body["external_mutations"] == 0
    assert body["injected_fault"] is True
    assert body["drill_fingerprint"]


def test_every_required_drill_is_a_failure_path() -> None:
    assert set(REQUIRED_DRILLS) == {
        DrillType.KILL_RESTART,
        DrillType.DUPLICATE_SCHEDULER,
        DrillType.CRASH_RECOVERY,
        DrillType.PROVIDER_UNAVAILABLE,
        DrillType.REMOTE_STATE_UNKNOWN,
        DrillType.STALE_INPUT,
    }


# ---------------------------------------------------------------------------
# §5, §19, §71 constants and live safety
# ---------------------------------------------------------------------------


def test_the_required_session_count_is_twenty() -> None:
    assert REQUIRED_ELIGIBLE_SESSIONS == 20
    assert protocol().required_sessions == 20


def test_the_protocol_declares_live_trading_unauthorised() -> None:
    assert protocol().body()["live_trading"] == "NOT_AUTHORIZED"
    assert protocol().promotion_gates["zero_tolerance"]["live_order_requests"] == 0
    assert protocol().promotion_gates["zero_tolerance"]["live_endpoint_requests"] == 0


def test_the_protocol_declares_the_soak_prospective_only() -> None:
    body = protocol().body()

    assert body["prospective_only"] is True
    assert body["backfill"].startswith("NONE")
    assert "resets the qualifying count" in body["code_freeze"]


def test_no_soak_artefact_carries_a_credential(tmp_path: Path) -> None:
    from stock_screener.execution.external import check_no_credentials

    store = armed_store(tmp_path)
    all_drills_passed(store)
    store.record_observation(observation("2026-10-08"))
    report = report_for(store, now="2026-10-09T20:00:00+00:00")

    for name, payload in (
        ("protocol", protocol().as_json()),
        ("activation", activation(protocol()).as_json()),
        ("observation", observation("2026-10-08").as_json()),
        ("report", report.as_json()),
    ):
        check_no_credentials(payload, context=name)
        assert "APCA" not in json.dumps(payload)


def test_scoring_an_unactivated_soak_is_refused(tmp_path: Path) -> None:
    store = SoakStore(tmp_path / ".operations")

    with pytest.raises(OperationsError, match=Code.SOAK_NOT_ACTIVATED):
        _evaluate(store, calendar=calendar(), schedule=schedule(), now="2026-10-08T20:00:00+00:00")


def test_the_report_renders_its_outstanding_work(tmp_path: Path) -> None:
    store = armed_store(tmp_path)
    store.record_observation(observation("2026-10-08"))
    lines = "\n".join(report_for(store, now="2026-10-09T20:00:00+00:00").summary_lines())

    assert "DRILL OUTSTANDING" in lines
    assert "NO RECORD: 2026-10-09" in lines
    assert "live trading         : NOT_AUTHORIZED" in lines
