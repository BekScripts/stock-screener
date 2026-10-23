"""Adversarial tests for the prospective H2 shadow runner.

The runner's job is to be hard to misuse a year from now, when nobody
remembers why a rule was there. Each test names a misuse — a backdated
activation, a second activation, a manually chosen quarter, a decision captured
early, a packet rewritten, an execution price written into a decision, an order
path added — and asserts the runner refuses it.

Nothing here touches the real prospective stream: every test runs in a fixture
namespace.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_screener.membership_hypothesis import Candidate
from stock_screener.prospective_protocol import CANDIDATE_ID, PROTOCOL_ID, STREAM_ID
from stock_screener.prospective_runner import (
    ACTIVATION_FILE,
    CRITERIA_SEAL,
    FORBIDDEN_CAPABILITIES,
    LEGAL_TRANSITIONS,
    SEMANTIC_MODULES,
    STATE_FILE,
    ActivationError,
    PacketConflictError,
    ProspectiveState,
    RunnerBindingError,
    StateError,
    activate,
    arm,
    bind_runner,
    calendar_descriptor,
    compare,
    correction_record,
    decision_packet,
    derive_first_eligible_decision,
    execution_packet,
    finalise,
    is_session,
    load_state,
    next_session,
    order_capability_reachable,
    ordinary_target_count,
    packet_digest,
    paired_targets,
    previous_session,
    quarter_end_session,
    record_incident,
    semantic_fingerprint,
    status,
    upcoming_quarter_ends,
    verify_against_history,
)
from stock_screener.prospective_runner import calendar as calendar_module

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
R1_6 = REPO / ".evidence" / "research" / "r1_6"
DATABASE = REPO / "stage9k.db"
EASTERN = ZoneInfo("America/New_York")

needs_frozen = pytest.mark.skipif(
    not (R1_6 / "r1_6_complete.json").is_file(),
    reason="the frozen R1.6 record is runtime data, not committed",
)
needs_database = pytest.mark.skipif(
    not DATABASE.is_file(), reason="the frozen database is runtime data, not committed"
)

COMMIT = "0" * 40


def _binding() -> Any:
    return bind_runner(R1_6)


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------


@needs_database
def test_the_calendar_reproduces_every_frozen_benchmark_session() -> None:
    with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as db:
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
        ]

    result = verify_against_history(sessions)

    assert result["agrees"] is True
    assert result["calendar_would_drop"] == []
    assert result["calendar_would_add"] == []
    assert result["checked"] > 1000


@pytest.mark.parametrize(
    ("day", "session"),
    [
        (date(2026, 1, 1), False),  # New Year's Day
        (date(2026, 12, 25), False),  # Christmas
        (date(2026, 9, 7), False),  # Labor Day
        (date(2026, 11, 26), False),  # Thanksgiving
        (date(2026, 9, 26), False),  # Saturday
        (date(2026, 9, 30), True),  # ordinary Wednesday
    ],
)
def test_holidays_and_weekends_are_not_sessions(day: date, session: bool) -> None:
    assert is_session(day) is session


def test_an_observed_holiday_moves_off_the_weekend() -> None:
    # 4 July 2026 is a Saturday, observed Friday 3 July.
    assert is_session(date(2026, 7, 3)) is False
    assert is_session(date(2026, 7, 6)) is True


def test_a_saturday_new_year_is_not_observed() -> None:
    # 1 January 2022 was a Saturday; the exchange did not close on 31 December.
    assert is_session(date(2021, 12, 31)) is True


def test_good_friday_is_derived_rather_than_listed() -> None:
    assert is_session(date(2026, 4, 3)) is False  # Good Friday 2026
    assert is_session(date(2027, 3, 26)) is False  # Good Friday 2027


def test_juneteenth_is_observed_only_from_2022() -> None:
    assert is_session(date(2021, 6, 18)) is True
    assert is_session(date(2022, 6, 20)) is False


def test_a_quarter_end_falls_back_to_the_previous_session() -> None:
    # 31 December 2022 was a Saturday; the last session was the 30th.
    assert quarter_end_session(2022, 4) == date(2022, 12, 30)
    assert quarter_end_session(2026, 3) == date(2026, 9, 30)


def test_an_irregular_closure_is_recorded_not_derived() -> None:
    assert is_session(date(2025, 1, 9)) is False
    descriptor = calendar_descriptor()
    assert date(2025, 1, 9).isoformat() in descriptor["known_irregular_closures"]
    assert "cannot be derived from any rule" in descriptor["irregular_closure_risk"]


def test_upcoming_quarter_ends_are_strictly_after_the_date() -> None:
    ends = upcoming_quarter_ends(date(2026, 9, 30), 3)

    assert all(day > date(2026, 9, 30) for day in ends)
    assert ends[0] == date(2026, 12, 31)


def test_sessions_neighbour_correctly_across_a_weekend() -> None:
    assert next_session(date(2026, 9, 25)) == date(2026, 9, 28)
    assert previous_session(date(2026, 9, 28)) == date(2026, 9, 25)


# --------------------------------------------------------------------------
# The runner cannot trade
# --------------------------------------------------------------------------


def test_no_order_capability_exists_in_the_runner() -> None:
    assert order_capability_reachable() == []


def test_the_guard_detects_a_real_order_path(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from alpaca.trading import TradingClient\ndef go(client):\n    client.submit_order(1)\n",
        encoding="utf-8",
    )

    found = order_capability_reachable(tmp_path)

    assert any("submit_order" in entry for entry in found)
    assert any("TradingClient" in entry for entry in found)


def test_the_guard_ignores_a_string_that_merely_mentions_a_broker(tmp_path: Path) -> None:
    (tmp_path / "prose.py").write_text(
        'STATUS = {"broker_connectivity": "NONE"}\n', encoding="utf-8"
    )

    assert order_capability_reachable(tmp_path) == []


def test_every_forbidden_capability_names_an_ability_not_a_word() -> None:
    assert "submit_order" in FORBIDDEN_CAPABILITIES
    assert "TradingClient" in FORBIDDEN_CAPABILITIES


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------


@needs_frozen
def test_the_frozen_protocol_binds() -> None:
    binding = _binding()

    assert binding.as_json()["order_capability"] == "NONE"
    assert binding.as_json()["force_override"] == "NONE"
    assert binding.as_json()["stream_id"] == STREAM_ID


@needs_frozen
@pytest.mark.parametrize(
    "field",
    [
        "expect_candidate_sha256",
        "expect_protocol_sha256",
        "expect_contract_sha256",
        "expect_marker_sha256",
    ],
)
def test_any_moved_r1_6_artifact_refuses_to_bind(field: str) -> None:
    moved: dict[str, Any] = {field: "0" * 64}

    with pytest.raises(RunnerBindingError, match="not the frozen one"):
        bind_runner(R1_6, **moved)


@needs_frozen
def test_a_calendar_that_disagrees_with_history_refuses_to_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calendar_module, "KNOWN_IRREGULAR_CLOSURES", (date(2024, 3, 1),))

    with pytest.raises(RunnerBindingError, match="does not reproduce"):
        bind_runner(R1_6, historical_sessions=[date(2024, 2, 28), date(2024, 3, 1)])


# --------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------


@needs_frozen
def test_activation_writes_an_immutable_record(tmp_path: Path) -> None:
    result = activate(tmp_path, _binding(), code_commit=COMMIT)

    assert result.written is True
    assert result.record["activation_state"] == "ACTIVATED"
    assert result.record["observations_at_activation"] == 0
    assert result.record["backdating"].startswith("REFUSED")
    assert (tmp_path / ACTIVATION_FILE).is_file()


@needs_frozen
def test_activation_cannot_be_backdated(tmp_path: Path) -> None:
    with pytest.raises(ActivationError, match="runtime clock"):
        activate(
            tmp_path,
            _binding(),
            code_commit=COMMIT,
            supplied_timestamp="2020-01-01T00:00:00+00:00",
        )


@needs_frozen
def test_activation_requires_an_immutable_commit(tmp_path: Path) -> None:
    with pytest.raises(ActivationError, match="immutable commit"):
        activate(tmp_path, _binding(), code_commit="dirty")


@needs_frozen
def test_an_identical_second_activation_reads_idempotently(tmp_path: Path) -> None:
    first = activate(tmp_path, _binding(), code_commit=COMMIT)

    second = activate(tmp_path, _binding(), code_commit=COMMIT)

    assert second.written is False
    assert second.sha256 == first.sha256


@needs_frozen
def test_a_second_activation_with_different_bytes_conflicts(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)

    with pytest.raises(ActivationError, match="already activated"):
        activate(tmp_path, _binding(), code_commit="1" * 40)


@needs_frozen
def test_activation_is_not_an_observation(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)

    state = load_state(tmp_path / STATE_FILE)

    assert state.observations == 0
    assert state.completed_transitions == 0
    assert state.state is ProspectiveState.ACTIVATED


# --------------------------------------------------------------------------
# Arming and the derived first decision
# --------------------------------------------------------------------------


@needs_frozen
def test_arming_derives_the_first_eligible_decision(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)

    result = arm(tmp_path, _binding(), code_commit=COMMIT)

    assert result.record["derived_automatically"] is True
    assert result.record["manually_supplied"] is False
    assert result.record["cutoff_satisfied"] is True
    assert result.first_eligible_decision == result.record["first_eligible_decision"]


@needs_frozen
def test_arming_before_activation_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ActivationError, match="not activated"):
        arm(tmp_path, _binding(), code_commit=COMMIT)


@needs_frozen
def test_arming_under_a_different_commit_is_refused(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)

    with pytest.raises(ActivationError, match="bound to"):
        arm(tmp_path, _binding(), code_commit="1" * 40)


@needs_frozen
def test_an_identical_second_arm_reads_idempotently(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    first = arm(tmp_path, _binding(), code_commit=COMMIT)

    second = arm(tmp_path, _binding(), code_commit=COMMIT)

    assert second.written is False
    assert second.sha256 == first.sha256


def _moment(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=EASTERN).astimezone(UTC)


def test_the_first_decision_is_the_next_quarter_end_when_armed_in_time() -> None:
    result = derive_first_eligible_decision(
        activated_at=_moment(date(2026, 9, 25)),
        armed_at=_moment(date(2026, 9, 25)),
    )

    assert result["first_eligible_decision"] == "2026-09-30"
    assert result["preceding_session"] == "2026-09-29"
    assert result["cutoff_satisfied"] is True
    assert result["skipped_quarters"] == []


def test_a_missed_arm_cutoff_skips_the_quarter() -> None:
    # Armed on the quarter-end morning itself: too late for that quarter.
    result = derive_first_eligible_decision(
        activated_at=_moment(date(2026, 9, 25)),
        armed_at=_moment(date(2026, 9, 30), hour=9),
    )

    assert result["first_eligible_decision"] == "2026-12-31"
    assert [row["quarter_end"] for row in result["skipped_quarters"]] == ["2026-09-30"]
    assert result["skipped_quarters"][0]["cutoff_satisfied"] is False


def test_arming_exactly_at_the_preceding_close_satisfies_the_cutoff() -> None:
    result = derive_first_eligible_decision(
        activated_at=_moment(date(2026, 9, 25)),
        armed_at=_moment(date(2026, 9, 29), hour=16),
    )

    assert result["first_eligible_decision"] == "2026-09-30"
    assert result["cutoff_satisfied"] is True


def test_arming_one_minute_after_the_preceding_close_skips_the_quarter() -> None:
    late = _moment(date(2026, 9, 29), hour=16) + timedelta(minutes=1)

    result = derive_first_eligible_decision(activated_at=_moment(date(2026, 9, 25)), armed_at=late)

    assert result["first_eligible_decision"] == "2026-12-31"


def test_a_skipped_quarter_is_never_reconstructed() -> None:
    result = derive_first_eligible_decision(
        activated_at=_moment(date(2026, 9, 25)),
        armed_at=_moment(date(2026, 9, 30), hour=9),
    )
    skipped = result["skipped_quarters"][0]

    assert skipped["quarter_end"] == "2026-09-30"
    assert skipped["cutoff_satisfied"] is False
    assert skipped["quarter_end"] != result["first_eligible_decision"]


def test_the_first_decision_is_strictly_after_activation() -> None:
    # Activating on a quarter-end day cannot make that day the first decision.
    result = derive_first_eligible_decision(
        activated_at=_moment(date(2026, 9, 30)),
        armed_at=_moment(date(2026, 9, 30)),
    )

    assert result["first_eligible_decision"] == "2026-12-31"


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


def test_no_state_may_be_skipped() -> None:
    state = load_state(Path("/nonexistent"))

    with pytest.raises(StateError, match="may not become"):
        state.transition(ProspectiveState.DECISION_CAPTURED)


def test_a_decision_cannot_be_captured_straight_from_armed() -> None:
    assert ProspectiveState.DECISION_CAPTURED not in LEGAL_TRANSITIONS[ProspectiveState.ARMED]
    assert LEGAL_TRANSITIONS[ProspectiveState.ARMED] == frozenset(
        {ProspectiveState.WAITING_FOR_DECISION}
    )


def test_an_evaluated_window_is_terminal() -> None:
    assert LEGAL_TRANSITIONS[ProspectiveState.EVALUATED] == frozenset()
    assert LEGAL_TRANSITIONS[ProspectiveState.INVALIDATED_BY_DEFECT] == frozenset()


def test_observations_are_derived_from_packets_not_counted() -> None:
    state = load_state(Path("/nonexistent"))
    state.decisions = ["2026-09-30", "2026-12-31", "2027-03-31"]

    assert state.observations == 3
    assert state.completed_transitions == 2


@needs_frozen
def test_state_survives_a_restart(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    armed = arm(tmp_path, _binding(), code_commit=COMMIT)

    # A fresh process reads nothing but the directory.
    recovered = load_state(tmp_path / STATE_FILE)

    assert recovered.state is ProspectiveState.WAITING_FOR_DECISION
    assert recovered.activated_at is not None
    assert recovered.armed_at is not None
    assert recovered.first_eligible_decision == armed.first_eligible_decision
    assert recovered.observations == 0
    assert recovered.code_commit == COMMIT


@needs_frozen
def test_a_restart_creates_no_duplicate_records(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    arm(tmp_path, _binding(), code_commit=COMMIT)

    activate(tmp_path, _binding(), code_commit=COMMIT)
    arm(tmp_path, _binding(), code_commit=COMMIT)

    ledger = (tmp_path / "prospective_research_ledger.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in ledger.splitlines() if line]

    assert events.count("PROTOCOL_ACTIVATED") == 1
    assert events.count("RUNNER_ARMED") == 1
    assert load_state(tmp_path / STATE_FILE).observations == 0


@needs_frozen
def test_an_incident_is_recorded_and_never_removed(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)

    record_incident(tmp_path, kind="SOURCE_UNAVAILABLE", detail="provider timeout")

    assert load_state(tmp_path / STATE_FILE).incidents[0]["kind"] == "SOURCE_UNAVAILABLE"


# --------------------------------------------------------------------------
# Semantic code fingerprint
# --------------------------------------------------------------------------


def test_the_semantic_fingerprint_covers_the_decision_path() -> None:
    result = semantic_fingerprint()

    assert "membership_hypothesis/rule.py" in result["modules"]
    assert "prospective_runner/selection.py" in result["modules"]
    assert "backtest_harness/portfolio.py" in result["modules"]
    assert result["module_count"] == len(SEMANTIC_MODULES)


def test_every_semantic_module_exists() -> None:
    from stock_screener.prospective_runner.fingerprint import source_root

    for relative in SEMANTIC_MODULES:
        assert (source_root() / relative).is_file(), relative


def test_a_missing_semantic_module_is_an_error_not_a_skip(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="semantic module missing"):
        semantic_fingerprint(tmp_path)


def test_an_unchanged_fingerprint_permits_capture() -> None:
    result = compare("a" * 64, "a" * 64)

    assert result["unchanged"] is True
    assert result["classification"] == "NONE"
    assert result["confirmatory_capture_permitted"] is True


def test_a_moved_fingerprint_halts_confirmatory_capture() -> None:
    result = compare("a" * 64, "b" * 64)

    assert result["classification"] == "SEMANTIC_RESEARCH_CHANGE"
    assert result["confirmatory_capture_permitted"] is False
    assert "new candidate, protocol and clock" in result["consequence"]


@needs_frozen
def test_arming_after_a_semantic_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    body = json.loads((tmp_path / ACTIVATION_FILE).read_text(encoding="utf-8"))
    body["semantic_code_fingerprint"] = "f" * 64
    (tmp_path / ACTIVATION_FILE).write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ActivationError, match="semantic code fingerprint moved"):
        arm(tmp_path, _binding(), code_commit=COMMIT)


# --------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------


def _targets(size: int = 100, incumbents: Any = ()) -> Any:
    rows = [Candidate("COMPANY", index, 100.0 - index) for index in range(size)]
    return paired_targets(rows, incumbents)


def test_the_target_count_matches_the_frozen_rule() -> None:
    assert ordinary_target_count(746) == 74
    assert ordinary_target_count(1010) == 101
    assert ordinary_target_count(0) == 0


def test_one_cross_section_feeds_both_strategies() -> None:
    targets = _targets()

    assert targets["target_count"] == 10
    assert len(targets["comparator_target_set"]) == 10
    assert len(targets["h2_target_set"]) == 10


def test_the_initial_selection_invariant_holds_with_no_incumbents() -> None:
    targets = _targets()

    assert targets["sets_are_equal"] is True
    assert sorted(targets["comparator_target_set"]) == sorted(targets["h2_target_set"])


def test_with_incumbents_the_sets_may_differ() -> None:
    targets = _targets(incumbents=[("COMPANY", 15)])

    assert targets["sets_are_equal"] is False
    assert ("COMPANY", 15) in targets["h2_target_set"]
    assert ("COMPANY", 15) not in targets["comparator_target_set"]


def _packet(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "packet_key": "2026-09-30",
        "decision_timestamp": "2026-09-30T16:00:00-04:00",
        "benchmark_session": "2026-09-30",
        "execution_session": "2026-10-01",
        "code_commit": COMMIT,
        "runner_armed_at": "2026-09-25T12:00:00+00:00",
        "protocol_version": "V1",
        "fingerprints": {
            "universe_snapshot": "a" * 64,
            "feature_snapshot": "b" * 64,
            "price_as_of": "c" * 64,
            "evidence_availability": "d" * 64,
            "ranked_cross_section": "e" * 64,
        },
        "targets": _targets(),
        "holdings_before": [],
        "forced_exits": [],
    }
    body.update(overrides)
    return decision_packet(**body)


def test_a_decision_packet_carries_no_outcome_field() -> None:
    packet = _packet()

    for forbidden in ("forward_return", "execution_price", "reference_open", "pnl"):
        assert forbidden not in packet


def test_a_decision_packet_binds_the_code_and_the_arm_time() -> None:
    packet = _packet()

    assert packet["code_commit"] == COMMIT
    assert packet["runner_armed_at"]
    assert packet["candidate_id"] == CANDIDATE_ID
    assert packet["protocol_id"] == PROTOCOL_ID


def test_a_retrospective_packet_must_carry_both_labels() -> None:
    with pytest.raises(ValueError, match="NOT_PROSPECTIVE_CONFIRMATION"):
        _packet(observation_kind="RETROSPECTIVE")

    packet = _packet(
        observation_kind="RETROSPECTIVE",
        labels=["RETROSPECTIVE", "NOT_PROSPECTIVE_CONFIRMATION"],
    )

    assert packet["observation_kind"] == "RETROSPECTIVE"
    assert "NOT_PROSPECTIVE_CONFIRMATION" in packet["labels"]


def test_a_packet_is_written_once(tmp_path: Path) -> None:
    packet = _packet()
    path = tmp_path / "decision.json"

    first = finalise(path, packet)
    second = finalise(path, packet)

    assert first.written is True
    assert second.written is False
    assert first.sha256 == second.sha256


def test_rewriting_a_packet_with_different_bytes_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "decision.json"
    finalise(path, _packet())

    with pytest.raises(PacketConflictError, match="refusing to overwrite"):
        finalise(path, _packet(packet_key="2026-12-31"))


def test_an_execution_packet_is_separate_and_links_by_digest() -> None:
    decision = _packet()
    execution = execution_packet(
        packet_key="2026-09-30",
        decision_packet_sha256=packet_digest(decision),
        execution_session="2026-10-01",
        captured_at="2026-10-01T09:35:00-04:00",
        reference_opens={"COMPANY:0": 10.0},
        unpriced=[],
        source={"provider": "alpaca", "feed": "iex"},
        capture_status="COMPLETE",
    )

    assert execution["decision_packet_sha256"] == packet_digest(decision)
    assert execution["modifies_decision_packet"] is False
    assert "reference_opens" not in decision


def test_an_execution_capture_leaves_the_decision_digest_unchanged() -> None:
    decision = _packet()
    before = packet_digest(decision)

    execution_packet(
        packet_key="2026-09-30",
        decision_packet_sha256=before,
        execution_session="2026-10-01",
        captured_at="2026-10-01T09:35:00-04:00",
        reference_opens={"COMPANY:0": 10.0},
        unpriced=[],
        source={"provider": "alpaca"},
        capture_status="COMPLETE",
    )

    assert packet_digest(decision) == before


def test_a_correction_never_rewrites_a_frozen_packet() -> None:
    correction = correction_record(
        affected_packet_sha256="a" * 64,
        discovered_at="2026-11-01T00:00:00+00:00",
        corrected_fact="a restated revenue figure",
        reason="issuer filed an amendment",
        original_value=100,
        new_value=95,
        material=False,
    )

    assert correction["rewrote_frozen_packet"] is False
    assert correction["consequence"].startswith("recorded")


def test_a_material_correction_invalidates_the_test() -> None:
    correction = correction_record(
        affected_packet_sha256="a" * 64,
        discovered_at="2026-11-01T00:00:00+00:00",
        corrected_fact="a filing was accepted after the decision instant",
        reason="acceptance timestamp was wrong",
        original_value=None,
        new_value=None,
        material=True,
    )

    assert correction["consequence"] == "PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT"


# --------------------------------------------------------------------------
# Status keeps the criteria sealed
# --------------------------------------------------------------------------


@needs_frozen
def test_status_seals_the_criteria(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    arm(tmp_path, _binding(), code_commit=COMMIT)

    view = status(tmp_path, _binding())

    assert view["criteria"] == CRITERIA_SEAL
    for criterion in ("P1", "P2", "P3", "P4"):
        assert view[criterion] == "NOT_YET_EVALUATED"
    assert view["prospective_verdict"] == "PENDING"
    assert view["candidate_status"] == "PROSPECTIVE_CONFIRMATION_PENDING"


@needs_frozen
def test_status_reports_no_turnover_or_performance_reading(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    view = status(tmp_path, _binding())

    assert view["performance_interpretation"] == "NOT_EVALUATED_FOR_RESEARCH_DECISION"
    assert not any("turnover" in key for key in view)
    assert view["fetches_data_to_display_status"] is False


@needs_frozen
def test_status_reports_no_broker(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)

    view = status(tmp_path, _binding())

    assert view["broker"].startswith("DISCONNECTED")
    assert view["order_capability"] == "NONE"


@needs_frozen
def test_a_zero_observation_state_is_valid(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    arm(tmp_path, _binding(), code_commit=COMMIT)

    view = status(tmp_path, _binding())

    assert view["decisions_captured"] == 0
    assert view["completed_transitions"] == 0
    assert view["armed"] is True
    assert view["first_eligible_decision"] is not None


@needs_frozen
def test_recovery_from_durable_state_alone_is_sufficient(tmp_path: Path) -> None:
    activate(tmp_path, _binding(), code_commit=COMMIT)
    arm(tmp_path, _binding(), code_commit=COMMIT)

    view = status(tmp_path, _binding())

    for field in (
        "candidate_id",
        "protocol_id",
        "activated_at",
        "armed_at",
        "first_eligible_decision",
        "decisions_captured",
        "completed_transitions",
        "prospective_verdict",
    ):
        assert view[field] is not None, field
    assert view["semantic_health"]["unchanged"] is True


# --------------------------------------------------------------------------
# The historical record stays put
# --------------------------------------------------------------------------


@needs_frozen
def test_activation_does_not_touch_the_frozen_research_record(tmp_path: Path) -> None:
    from stock_screener.signal_review.binding import file_sha256

    before = {path.name: file_sha256(path) for path in sorted(R1_6.glob("*.json"))}

    activate(tmp_path, _binding(), code_commit=COMMIT)
    arm(tmp_path, _binding(), code_commit=COMMIT)

    after = {path.name: file_sha256(path) for path in sorted(R1_6.glob("*.json"))}
    assert after == before


def test_the_prospective_root_is_not_under_research() -> None:
    from stock_screener.prospective_runner import PROSPECTIVE_ROOT

    assert "research/r1_" not in PROSPECTIVE_ROOT
    assert PROSPECTIVE_ROOT.startswith("prospective/")
