"""P2.4 V2 — the session-based soak, and the calendar it is scored on.

V1 counted runs, which was adequate when a session held at most one. It no
longer does: an execution near the open, an observation at midday, a decision at
the close and a recovery run can all belong to one day, and counting them
separately would let a single busy session look like four days of evidence. V2
counts the session.

The calendar tests are here rather than in a fixture file because the calendar
is now an operations artefact with real session times, and the holiday rules are
derived rather than typed — so the derivations are what need checking.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.paper_operations import OperationsError
from stock_screener.paper_operations.strategy_schedule import (
    RunPurpose,
    StrategyOperationsSchedule,
)
from stock_screener.paper_runtime.operational_calendar import (
    DECLARED_CLOSURES,
    EARLY_CLOSES,
    OPERATIONAL_CALENDAR_VERSION,
    calendar_fingerprint,
    operational_calendar,
    operational_sessions,
    session_calendar,
)
from stock_screener.paper_runtime.soak_v2 import (
    REQUIRED_SESSIONS_V2,
    PaperShadowSessionObservationV2,
    PaperShadowSoakActivationV2,
    PaperShadowSoakProtocolV2,
    SessionClassification,
    SoakV2Store,
    complete_cycle_in,
    window_for,
)
from stock_screener.strategies import is_decision_session

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

NOW = "2026-10-10T05:30:00+00:00"
COMMIT = "a" * 40


# --------------------------------------------------------------------------
# §4, §5, §38: the forward operational calendar
# --------------------------------------------------------------------------


def test_the_calendar_is_an_operations_artefact_not_a_research_one() -> None:
    from stock_screener.paper_runtime.operational_calendar import calendar_body

    body = calendar_body()

    assert body["kind"] == "OPERATIONS_ARTEFACT_NOT_RESEARCH"
    assert body["benchmark_calendar_used"] is False
    assert body["derived_from_weekday_arithmetic"] is False


def test_the_calendar_covers_two_quarters_not_exactly_twenty_sessions() -> None:
    # §8. A calendar of exactly twenty dates would strand the soak the first
    # time a session was blocked, missed or killed.
    sessions = operational_sessions()

    assert len(sessions) > REQUIRED_SESSIONS_V2 * 3
    assert sessions[0].session == "2026-10-12"
    assert sessions[-1].session == "2027-03-31"


def test_no_declared_closure_appears_as_a_session() -> None:
    dates = {row.session for row in operational_sessions()}

    assert dates & set(DECLARED_CLOSURES) == set()


def test_no_weekend_appears_as_a_session() -> None:
    for row in operational_sessions():
        assert date.fromisoformat(row.session).weekday() < 5, row.session


@pytest.mark.parametrize(
    ("holiday", "rule"),
    [
        ("2026-11-26", "fourth Thursday of November 2026"),
        ("2027-01-18", "third Monday of January 2027"),
        ("2027-02-15", "third Monday of February 2027"),
        ("2027-03-26", "two days before Easter 2027-03-28"),
    ],
)
def test_each_floating_holiday_matches_its_published_rule(holiday: str, rule: str) -> None:
    # The holidays are derived from rules rather than typed, so the rules are
    # what must be checked. A typo in a date list is invisible; a wrong rule
    # here fails.
    day = date.fromisoformat(holiday)
    if "Thursday" in rule:
        assert day.weekday() == 3
        assert 22 <= day.day <= 28
    elif "Monday" in rule:
        assert day.weekday() == 0
        assert 15 <= day.day <= 21
    else:
        assert day.weekday() == 4
        assert day + timedelta(days=2) == date(2027, 3, 28)
    assert holiday in DECLARED_CLOSURES


def test_an_ordinary_session_closes_at_sixteen_hundred() -> None:
    rows = session_calendar()

    assert rows["2026-10-30"].closes_at_local == "16:00"
    assert rows["2026-10-30"].early_close is False


@pytest.mark.parametrize("session", ["2026-11-27", "2026-12-24"])
def test_an_early_close_session_closes_at_thirteen_hundred(session: str) -> None:
    # §5. Encoding these as ordinary 16:00 sessions would make a scheduler wait
    # nearly three hours for a market that had shut.
    rows = session_calendar()

    assert session in rows
    assert rows[session].closes_at_local == "13:00"
    assert rows[session].early_close is True
    assert rows[session].closes_at().strftime("%H:%M") == "13:00"


def test_an_early_close_is_still_an_eligible_session() -> None:
    # It is open. Only its end differs.
    assert set(EARLY_CLOSES) <= {row.session for row in operational_sessions()}


def test_the_execution_slot_precedes_every_declared_close() -> None:
    # §6, §38. 09:35 is before both a 16:00 and a 13:00 close, so no change to
    # OPERATIONAL_EXECUTION_TIMING_V1 is needed for an early close.
    schedule = StrategyOperationsSchedule(
        schedule_id="ALPHATRADER_PAPER_V1_1",
        timezone="America/New_York",
        calendar=operational_calendar(),
    )

    for row in operational_sessions():
        due = schedule.due_at(row.session, RunPurpose.STRATEGY_EXECUTION)
        assert due < row.closes_at(), row.session


def test_a_friday_session_is_followed_by_a_monday() -> None:
    calendar = operational_calendar()

    following = calendar.next_session("2026-10-30")

    assert following == "2026-11-02"
    assert date.fromisoformat("2026-10-30").weekday() == 4
    assert date.fromisoformat(following).weekday() == 0


def test_the_session_after_thanksgiving_is_the_early_close() -> None:
    calendar = operational_calendar()

    assert calendar.next_session("2026-11-25") == "2026-11-27"


def test_the_calendar_crosses_the_year_boundary() -> None:
    calendar = operational_calendar()

    assert calendar.next_session("2026-12-31") == "2027-01-04"


def test_the_calendar_has_a_stable_fingerprint() -> None:
    assert len(calendar_fingerprint()) == 64
    assert OPERATIONAL_CALENDAR_VERSION in operational_calendar().name


# --------------------------------------------------------------------------
# §7, §20, §21: the prospective window
# --------------------------------------------------------------------------


def test_the_window_is_derived_from_the_calendar_and_the_instant() -> None:
    # §20. Nothing is typed into scoring logic.
    window = window_for(calendar=operational_calendar(), now=NOW)

    assert len(window) == REQUIRED_SESSIONS_V2
    assert window[0] == "2026-10-12"
    assert window[-1] == "2026-11-06"


def test_the_window_contains_a_complete_decision_to_execution_cycle() -> None:
    # §21. Twenty sessions that never ask the strategy to decide would prove
    # the scheduler works and nothing about the decision path.
    calendar = operational_calendar()
    window = window_for(calendar=calendar, now=NOW)

    cycle = complete_cycle_in(window, calendar=calendar)

    assert cycle == ("2026-10-30", "2026-11-02")


def test_the_windows_decision_session_is_a_normal_close() -> None:
    # §7. S1.0's decision instant is literally 16:00, so an early-close month
    # end would be a contract conflict. This window's only decision session
    # closes at 16:00, which is why it can be armed.
    calendar = operational_calendar()
    window = window_for(calendar=calendar, now=NOW)
    rows = session_calendar()

    decisions = [s for s in window if is_decision_session(s, calendar=calendar)]
    assert decisions == ["2026-10-30"]
    assert rows["2026-10-30"].early_close is False
    assert rows["2026-10-30"].closes_at_local == "16:00"


def test_both_early_closes_fall_outside_the_armed_window() -> None:
    # Recorded rather than relied upon: the S1.0 16:00 conflict is latent, not
    # resolved, and it would bite a window containing an early-close month end.
    window = window_for(calendar=operational_calendar(), now=NOW)

    assert set(EARLY_CLOSES) & set(window) == set()


def test_a_window_with_no_cycle_is_detectable() -> None:
    # The guard must be able to say no, or §21 is decoration.
    calendar = operational_calendar()
    mid_month = tuple(
        s for s in calendar.sessions if s.startswith("2026-11-0") or s.startswith("2026-11-1")
    )[:5]

    assert complete_cycle_in(mid_month, calendar=calendar) is None


# --------------------------------------------------------------------------
# §9, §10, §27, §39: the session is the counting unit
# --------------------------------------------------------------------------


def protocol() -> PaperShadowSoakProtocolV2:
    """A frozen V2 protocol."""
    return PaperShadowSoakProtocolV2(
        protocol_id="P2_4_V2_SESSION_SOAK",
        qualifying_commit=COMMIT,
        runtime_binding_fingerprint="rb",
        account_binding_fingerprint="acct",
        strategy_fingerprint="sf",
        strategy_authorization_fingerprint="sa",
        risk_policy_fingerprint="rp",
        operations_policy_fingerprint="op",
        calendar_fingerprint=calendar_fingerprint(),
        schedule_fingerprint="sch",
        score_availability_fingerprint="sav",
        execution_timing_fingerprint="et",
        accounting_generation="ag",
        execution_journal_generation="eg",
    )


def armed(tmp_path: Path) -> tuple[SoakV2Store, PaperShadowSoakProtocolV2, tuple[str, ...]]:
    """An armed V2 store over a throwaway root."""
    calendar = operational_calendar()
    window = window_for(calendar=calendar, now=NOW)
    cycle = complete_cycle_in(window, calendar=calendar)
    assert cycle is not None
    store = SoakV2Store(tmp_path / ".operations")
    frozen = protocol()
    store.freeze_protocol(frozen)
    store.activate(
        PaperShadowSoakActivationV2(
            protocol_fingerprint=frozen.fingerprint(),
            qualifying_commit=COMMIT,
            activated_at=NOW,
            first_eligible_session=window[0],
            window=window,
            decision_session=cycle[0],
            execution_session=cycle[1],
        )
    )
    return store, frozen, window


def observation(
    frozen: PaperShadowSoakProtocolV2, session: str, **kwargs: Any
) -> PaperShadowSessionObservationV2:
    """One session observation."""
    return PaperShadowSessionObservationV2(
        protocol_fingerprint=frozen.fingerprint(),
        session=session,
        qualifying_commit=COMMIT,
        observed_at=f"{session}T21:00:00+00:00",
        **kwargs,
    )


def test_three_runs_in_one_session_count_as_one_session(tmp_path: Path) -> None:
    # §39. The whole reason V2 exists.
    store, frozen, window = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            window[0],
            run_ids=(
                ("opsrun-exec", "STRATEGY_EXECUTION"),
                ("opsrun-obs", "SESSION_OBSERVATION"),
                ("opsrun-dec", "STRATEGY_DECISION"),
            ),
        )
    )

    recorded = store.observations()[0]
    assert recorded.body()["run_count"] == 3
    assert recorded.body()["counts_as_sessions"] == 1
    assert store.counted_sessions() == (window[0],)


def test_a_session_with_zero_mutations_still_counts(tmp_path: Path) -> None:
    # §27. The ordinary day is the overwhelming majority of the soak.
    store, frozen, window = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            window[0],
            classification=SessionClassification.OBSERVED_NO_ACTION,
            external_mutations=0,
        )
    )

    assert len(store.counted_sessions()) == 1


def test_recording_the_identical_session_twice_is_idempotent(tmp_path: Path) -> None:
    store, frozen, window = armed(tmp_path)
    row = observation(frozen, window[0], run_ids=(("opsrun-1", "SESSION_OBSERVATION"),))

    store.record_observation(row)
    store.record_observation(row)

    assert len(store.observations()) == 1
    assert len(store.counted_sessions()) == 1


def test_a_conflicting_second_observation_is_refused(tmp_path: Path) -> None:
    # §13, §31. Two runs on one session are not duplicates; two counted
    # observations for one session are.
    store, frozen, window = armed(tmp_path)
    store.record_observation(observation(frozen, window[0]))

    with pytest.raises(OperationsError, match="SOAK_OBSERVATION_INVALID"):
        store.record_observation(
            observation(frozen, window[0], classification=SessionClassification.BLOCKED)
        )


def test_a_correction_preserves_the_original(tmp_path: Path) -> None:
    # §13. A correction is not an edit: both records survive and the original
    # points at what replaced it.
    store, frozen, window = armed(tmp_path)
    original = observation(frozen, window[0])
    store.record_observation(original)
    correction = observation(frozen, window[0], classification=SessionClassification.BLOCKED)

    store.supersede(original, correction, reason="reconciliation re-read")

    rows = store.observations_for(window[0])
    assert len(rows) == 2
    superseded = next(row for row in rows if row.observation_id == original.observation_id)
    assert superseded.superseded_by == correction.observation_id
    # Only the live one counts, so a correction never double-counts a day.
    assert store.counted_sessions() == (window[0],)


def test_no_session_before_activation_can_count(tmp_path: Path) -> None:
    # §58. Not the bootstrap, not the smoke, not one V1 record.
    store, frozen, _ = armed(tmp_path)

    with pytest.raises(OperationsError, match="SOAK_RETROSPECTIVE_OBSERVATION"):
        store.record_observation(observation(frozen, "2026-08-26"))


def test_an_observation_from_another_build_does_not_count(tmp_path: Path) -> None:
    # §50. The observation binds the activation's qualifying commit, not
    # whatever HEAD happens to be later.
    store, frozen, window = armed(tmp_path)

    with pytest.raises(OperationsError, match="SOAK_OBSERVATION_INVALID"):
        store.record_observation(
            PaperShadowSessionObservationV2(
                protocol_fingerprint=frozen.fingerprint(),
                session=window[1],
                qualifying_commit="b" * 40,
                observed_at=NOW,
            )
        )


def test_a_session_may_be_observed_while_a_run_purpose_was_missed(
    tmp_path: Path,
) -> None:
    # §29. The session counts; the miss is named rather than omitted.
    store, frozen, window = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            window[0],
            run_ids=(("opsrun-obs", "SESSION_OBSERVATION"),),
            missed_purposes=("MISSED_STRATEGY_DECISION",),
        )
    )

    recorded = store.observations()[0]
    assert len(store.counted_sessions()) == 1
    assert recorded.missed_purposes == ("MISSED_STRATEGY_DECISION",)


# --------------------------------------------------------------------------
# §22, §23, §32, §40, §41, §42, §43, §44: the decision cycle
# --------------------------------------------------------------------------


def test_an_ordinary_session_with_no_decision_is_a_valid_observation(
    tmp_path: Path,
) -> None:
    # §40. NO_STRATEGY_DECISION_DUE is a successful session, not a gap.
    store, frozen, window = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            window[0],
            decision_due=False,
            classification=SessionClassification.OBSERVED_NO_ACTION,
        )
    )

    recorded = store.observations()[0]
    assert len(store.counted_sessions()) == 1
    assert recorded.exercised_decision_cycle is False


def test_a_month_end_decision_session_makes_no_mutation(tmp_path: Path) -> None:
    # §41. The decision is not executable in its own session.
    store, frozen, _ = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            "2026-10-30",
            decision_due=True,
            decision_created=True,
            decision_fingerprint="d" * 64,
            classification=SessionClassification.STRATEGY_DECISION,
            external_mutations=0,
            orders_submitted=0,
        )
    )

    recorded = store.observations()[0]
    assert recorded.external_mutations == 0
    assert recorded.exercised_decision_cycle is True


def test_the_following_session_consumes_the_frozen_decision(tmp_path: Path) -> None:
    # §26, §42. The execution carries the decision's fingerprint, which is the
    # evidence that it did not rerank.
    store, frozen, _ = armed(tmp_path)
    digest = "d" * 64

    store.record_observation(
        observation(
            frozen,
            "2026-11-02",
            execution_due=True,
            executed_decision_fingerprint=digest,
            execution_outcome="EXECUTED",
            classification=SessionClassification.PAPER_EXECUTION,
        )
    )

    recorded = store.observations()[0]
    assert recorded.executed_decision_fingerprint == digest
    assert recorded.exercised_decision_cycle is True


def test_an_all_risk_zero_execution_still_exercises_the_cycle(tmp_path: Path) -> None:
    # §22, §43. A correct no-trade is still a cycle that happened.
    store, frozen, _ = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            "2026-11-02",
            execution_due=True,
            executed_decision_fingerprint="d" * 64,
            execution_outcome="COMPLETED_NO_ACTION_RISK_CONSTRAINED",
            classification=SessionClassification.EXECUTION_NO_ACTION,
            orders_submitted=0,
            external_mutations=0,
        )
    )

    recorded = store.observations()[0]
    assert recorded.orders_submitted == 0
    assert recorded.exercised_decision_cycle is True


def test_a_stale_score_blocks_without_exercising_the_cycle(tmp_path: Path) -> None:
    # §23, §44. Failing closed is valid evidence of safe behaviour, and it is
    # also not a decision cycle — so it must not satisfy the promotion gate.
    store, frozen, _ = armed(tmp_path)

    store.record_observation(
        observation(
            frozen,
            "2026-10-30",
            decision_due=True,
            decision_created=False,
            classification=SessionClassification.BLOCKED,
            orders_submitted=0,
        )
    )

    recorded = store.observations()[0]
    assert len(store.counted_sessions()) == 1
    assert recorded.exercised_decision_cycle is False


# --------------------------------------------------------------------------
# §30, §31, §33, §34: the gates
# --------------------------------------------------------------------------


def test_every_prior_zero_tolerance_gate_survives() -> None:
    gates = protocol().promotion_gates["zero_tolerance"]

    for name in (
        "live_mutations",
        "duplicate_remote_orders",
        "duplicate_admitted_fills",
        "duplicate_accounting_events",
        "stale_data_trades",
        "missing_data_trades",
        "missed_run_backfills",
        "ignored_reconciliation_breaks",
        "ignored_remote_state_unknown",
        "kill_bypasses",
        "unknown_remote_order_auto_cancels",
        "automatic_accounting_repairs",
    ):
        assert gates[name] == 0, name


def test_session_double_counting_is_a_zero_tolerance_gate() -> None:
    # §31. New in V2, because the failure is new in V2.
    assert protocol().promotion_gates["zero_tolerance"]["duplicate_counted_sessions"] == 0


def test_the_decision_cycle_gate_requires_one_real_cycle() -> None:
    # §32. Twenty quiet sessions would otherwise pass a soak of a strategy
    # pipeline without the strategy ever being asked to decide.
    required = protocol().promotion_gates["required_not_zero_tolerance"]

    assert required["successful_strategy_decision_cycles"] == 1
    assert required["execution_eligible_decisions_accounted_for"] == "ALL"
    assert required["pending_executions_at_completion"] == 0


def test_no_minimum_order_or_fill_count_is_required() -> None:
    # §33. Risk may correctly permit nothing, and P2.2E certified the boundary.
    gates = protocol().promotion_gates
    text = str(gates)

    assert "orders >= 1" not in text
    assert "minimum_orders" not in text
    assert any("minimum order count" in row for row in gates["not_gated_on"])


def test_no_gate_reads_a_performance_measure() -> None:
    # §34.
    # Only the gate dictionaries are scanned. `not_gated_on` names these
    # measures on purpose — it is the disclaimer, not a gate, and including it
    # would make the assertion fail for saying the right thing.
    gates = protocol().promotion_gates
    text = str(
        {
            "counting": gates["counting"],
            "zero_tolerance": gates["zero_tolerance"],
            "required": gates["required_not_zero_tolerance"],
        }
    ).lower()

    for forbidden in ("sharpe", "win_rate", "alpha", "drawdown", "profit", "return"):
        assert forbidden not in text, forbidden


def test_the_gates_live_inside_the_protocol_fingerprint() -> None:
    from dataclasses import replace

    before = protocol().fingerprint()

    assert replace(protocol(), required_sessions=10).fingerprint() != before


def test_the_protocol_declares_no_backtest_equivalence() -> None:
    # §18. The timing disclosure is part of the frozen rules, not a footnote.
    disclosure = protocol().body()["timing_disclosure"]

    assert disclosure["opening_auction_equivalence"] == "NONE"
    assert disclosure["backtest_performance_equivalence"] == "NONE"


def test_v2_supersedes_v1_without_touching_it(tmp_path: Path) -> None:
    # §16, §45, §46. V1 remains historical evidence in its own namespace.
    store, _, _ = armed(tmp_path)

    assert "PAPER_SHADOW_SOAK_PROTOCOL_V1" in protocol().body()["supersedes"]
    assert store.directory.name == "v2"
    assert store.directory.parent.name == "soak"


def test_a_different_v2_protocol_cannot_replace_a_frozen_one(tmp_path: Path) -> None:
    from dataclasses import replace

    store, frozen, _ = armed(tmp_path)

    with pytest.raises(OperationsError, match="SOAK_PROTOCOL_DRIFT"):
        store.freeze_protocol(replace(frozen, required_sessions=5))


def test_v2_cannot_be_activated_twice(tmp_path: Path) -> None:
    store, frozen, window = armed(tmp_path)

    with pytest.raises(OperationsError, match="SOAK_ALREADY_ACTIVATED"):
        store.activate(
            PaperShadowSoakActivationV2(
                protocol_fingerprint=frozen.fingerprint(),
                qualifying_commit=COMMIT,
                activated_at="2026-10-11T00:00:00+00:00",
                first_eligible_session=window[0],
                window=window,
                decision_session="2026-10-30",
                execution_session="2026-11-02",
            )
        )


def test_no_soak_artefact_carries_a_credential(tmp_path: Path) -> None:
    import json

    store, frozen, window = armed(tmp_path)
    store.record_observation(observation(frozen, window[0]))

    text = json.dumps(
        [
            frozen.as_json(),
            store.activation(),
            *[row.as_json() for row in store.observations()],
        ]
    ).lower()
    for forbidden in ("apca", "secret", "bearer", "api_key", "password"):
        assert forbidden not in text, forbidden
