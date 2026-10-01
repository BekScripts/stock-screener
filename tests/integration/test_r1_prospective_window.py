"""Adversarial tests for the V2 window certification and re-arm.

The September miss had two causes. R1.9 built the sources and left the timing
hole: a current-only snapshot taken after the close satisfied the old
admissibility check, because its semantic time *is* its retrieval time and
nothing compared that to the instant. These tests close it and keep it closed —
the fourth timing case is the one that was missing, and it must stay refused by
name.

The governance tests exist because a protocol that quietly altered H2 would be
worse than no protocol: V1 must stay byte-identical, V2 must change only
operations, and the missed session must stay missed.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from stock_screener.prospective_source import (
    ACTIVATION_V2_FILE,
    ARM_V2_FILE,
    CAPTURE_SEQUENCE_ID,
    FRESHNESS_POLICY,
    MISSED_SESSIONS,
    PROTOCOL_V2_ID,
    SOURCE_TEMPORAL_CLASS,
    Completeness,
    Sealed,
    SnapshotEnvelope,
    TemporalClass,
    WindowError,
    activate_v2,
    admissible_for,
    arm_v2,
    calendar_derivation,
    capture_sequence,
    certify_timing,
    content_digest,
    decision_day_runbook,
    derive_window,
    protocol_v2,
    refuse_missed_session,
    timing_canary,
    v1_v2_diff,
)

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
R1_6 = REPO / ".evidence" / "research" / "r1_6"
R1_9 = REPO / ".evidence" / "research" / "r1_9"
EASTERN = ZoneInfo("America/New_York")

#: The armed window's instant, used throughout.
DEC_31 = datetime(2026, 12, 31, 16, 0, tzinfo=EASTERN)

needs_frozen = pytest.mark.skipif(
    not (R1_6 / "h2_prospective_protocol_v1.json").is_file(),
    reason="the frozen R1.6 protocol is runtime data, not committed",
)


def _env(
    temporal: TemporalClass,
    retrieved: datetime,
    semantic: datetime | None = None,
    effective: datetime | None = None,
    completeness: Completeness = Completeness.COMPLETE,
) -> SnapshotEnvelope:
    return SnapshotEnvelope(
        source_type="probe",
        source_name="fixture",
        source_version="v1",
        operation="probe",
        retrieved_at=retrieved,
        semantic_as_of=semantic or retrieved,
        content_sha256=content_digest([1]),
        request="r",
        normalizer_version="v1",
        record_count=1,
        completeness=completeness,
        temporal_class=temporal,
        effective_at=effective,
    )


# --------------------------------------------------------------------------
# The timing hole R1.10 exists to close
# --------------------------------------------------------------------------


def test_a_current_only_snapshot_read_after_the_instant_is_refused_by_name() -> None:
    late = _env(TemporalClass.CURRENT_AT_RETRIEVAL, DEC_31 + timedelta(hours=1))

    verdict = admissible_for(late, DEC_31)

    assert verdict["admissible"] is False
    assert "CURRENT_ONLY_SOURCE_TOO_LATE" in verdict["reasons"]


def test_a_current_only_snapshot_read_before_the_instant_is_admissible() -> None:
    early = _env(TemporalClass.CURRENT_AT_RETRIEVAL, DEC_31 - timedelta(hours=2))

    verdict = admissible_for(early, DEC_31, max_age=FRESHNESS_POLICY["universe"])

    assert verdict["admissible"] is True
    assert verdict["retrieval_time_is_load_bearing"] is True


def test_retrieval_time_is_load_bearing_only_for_current_only_sources() -> None:
    late = DEC_31 + timedelta(hours=3)

    for temporal in (TemporalClass.EVENT_TIME, TemporalClass.SESSION_BOUNDED):
        verdict = admissible_for(_env(temporal, late, semantic=DEC_31), DEC_31)
        assert verdict["admissible"] is True
        assert verdict["retrieval_time_is_load_bearing"] is False


def test_an_event_time_source_may_be_read_long_after_the_instant() -> None:
    verdict = admissible_for(
        _env(TemporalClass.EVENT_TIME, DEC_31 + timedelta(days=5), semantic=DEC_31),
        DEC_31,
    )

    assert verdict["admissible"] is True


def test_a_session_bounded_source_may_be_read_after_the_close() -> None:
    verdict = admissible_for(
        _env(TemporalClass.SESSION_BOUNDED, DEC_31 + timedelta(hours=4), semantic=DEC_31),
        DEC_31,
    )

    assert verdict["admissible"] is True


def test_a_provider_effective_time_before_the_instant_rescues_a_late_snapshot() -> None:
    verdict = admissible_for(
        _env(
            TemporalClass.CURRENT_AT_RETRIEVAL,
            DEC_31 + timedelta(hours=1),
            semantic=DEC_31,
            effective=DEC_31 - timedelta(minutes=10),
        ),
        DEC_31,
    )

    assert verdict["admissible"] is True


def test_a_provider_effective_time_after_the_instant_does_not() -> None:
    verdict = admissible_for(
        _env(
            TemporalClass.CURRENT_AT_RETRIEVAL,
            DEC_31 + timedelta(hours=1),
            semantic=DEC_31,
            effective=DEC_31 + timedelta(minutes=10),
        ),
        DEC_31,
    )

    assert verdict["admissible"] is False
    assert "PROVIDER_EFFECTIVE_TIME_AFTER_DECISION_INSTANT" in verdict["reasons"]


def test_an_undeclared_source_defaults_to_the_most_restrictive_class() -> None:
    # A source that forgets to declare itself is treated as current-only rather
    # than trusted, so forgetting fails closed.
    envelope = SnapshotEnvelope(
        source_type="probe",
        source_name="fixture",
        source_version="v1",
        operation="probe",
        retrieved_at=DEC_31 + timedelta(hours=1),
        semantic_as_of=DEC_31,
        content_sha256=content_digest([1]),
        request="r",
        normalizer_version="v1",
        record_count=1,
        completeness=Completeness.COMPLETE,
    )

    assert envelope.temporal_class is TemporalClass.CURRENT_AT_RETRIEVAL
    assert admissible_for(envelope, DEC_31)["admissible"] is False


def test_the_timing_canary_covers_all_five_cases() -> None:
    canary = timing_canary()

    assert canary["all_as_expected"] is True
    assert canary["the_case_that_was_missing"] == "current_only_post_decision_retrieval"
    assert canary["its_failure_code"] == "CURRENT_ONLY_SOURCE_TOO_LATE"
    assert canary["creates_an_observation"] is False
    assert len(canary["cases"]) == 5


# --------------------------------------------------------------------------
# Source classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("sec_availability", TemporalClass.EVENT_TIME),
        ("market", TemporalClass.SESSION_BOUNDED),
        ("universe", TemporalClass.CURRENT_AT_RETRIEVAL),
        ("tradability", TemporalClass.CURRENT_AT_RETRIEVAL),
    ],
)
def test_every_source_is_classified(source: str, expected: TemporalClass) -> None:
    assert SOURCE_TEMPORAL_CLASS[source] is expected


def test_the_audit_refuses_a_post_close_current_only_snapshot() -> None:
    audit = certify_timing()

    assert audit["post_close_current_only_snapshot_admissible"] is False
    assert audit["sources"]["universe"]["failure_code"] == "CURRENT_ONLY_SOURCE_TOO_LATE"
    assert (
        audit["sources"]["universe"]["latest_permitted_retrieval"]
        == "AT OR BEFORE the decision instant"
    )


def test_the_audit_permits_late_retrieval_for_the_other_two_classes() -> None:
    audit = certify_timing()

    for source in ("sec_availability", "market"):
        assert "after the instant" in audit["sources"][source]["latest_permitted_retrieval"]


def test_the_audit_records_what_it_closes() -> None:
    assert "semantic_as_of <= T" in certify_timing()["closes_the_r1_9_gap"]


def test_an_effective_time_must_come_from_the_provider() -> None:
    audit = certify_timing()

    assert "retrojection" in audit["provider_effective_time_must_come_from_the_provider"]


# --------------------------------------------------------------------------
# September 30 stays missed
# --------------------------------------------------------------------------


def test_the_missed_session_is_recorded() -> None:
    assert "2026-09-30" in MISSED_SESSIONS


def test_a_decision_for_the_missed_session_is_refused() -> None:
    with pytest.raises(WindowError, match="MISSED_DECISION_NOT_BACKFILLABLE"):
        refuse_missed_session("2026-09-30")


def test_a_decision_for_any_other_session_is_permitted() -> None:
    refuse_missed_session("2026-12-31")


def test_the_derived_window_is_never_the_missed_session() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))

    assert window["decision_session"] != "2026-09-30"
    assert window["decision_session"] == "2026-12-31"


# --------------------------------------------------------------------------
# Window derivation
# --------------------------------------------------------------------------


def test_the_window_is_derived_not_supplied() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))

    assert window["derived_automatically"] is True
    assert window["manually_supplied"] is False
    assert window["timezone"] == "America/New_York"


def test_the_window_derives_its_own_execution_reference() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))

    assert window["execution_reference_session"] == "2027-01-04"
    assert window["execution_reference_derived"] is True


def test_the_arm_cutoff_is_the_preceding_session_close() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))

    assert window["preceding_session"] == "2026-12-30"
    assert window["arm_cutoff"].startswith("2026-12-30T16:00")
    assert window["cutoff_satisfied"] is True


def test_arming_after_the_cutoff_is_refused() -> None:
    late = datetime(2026, 12, 30, 16, 1, tzinfo=EASTERN)

    with pytest.raises(WindowError, match="would claim a readiness"):
        derive_window(after=date(2026, 9, 30), armed_at=late)


def test_arming_exactly_at_the_cutoff_is_permitted() -> None:
    window = derive_window(
        after=date(2026, 9, 30), armed_at=datetime(2026, 12, 30, 16, 0, tzinfo=EASTERN)
    )

    assert window["cutoff_satisfied"] is True
    assert window["lead_time_seconds"] == 0


def test_the_calendar_derivation_records_that_it_is_rule_based() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))

    calendar = calendar_derivation(window)

    assert calendar["table_bound"] is False
    assert "ends with the research dataset" in calendar["derivation_method"]
    assert calendar["next_eligible_decision_session"] == "2026-12-31"


# --------------------------------------------------------------------------
# The capture sequence
# --------------------------------------------------------------------------


def _sequence() -> dict[str, Any]:
    return capture_sequence(derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC)))


def test_the_sequence_puts_current_only_sources_before_the_instant() -> None:
    stage = next(s for s in _sequence()["stages"] if s["stage"] == "A_PRE_DECISION")

    assert set(stage["sources"]) == {"universe", "tradability"}
    assert stage["requirement"] == "retrieved_at <= decision instant"
    assert stage["failure_code"] == "REQUIRED_PREDECISION_SOURCE_UNAVAILABLE"


def test_the_sequence_puts_the_other_sources_after_the_close() -> None:
    stage = next(s for s in _sequence()["stages"] if s["stage"] == "C_POST_CLOSE_COMPLETION")

    assert set(stage["sources"]) == {"market", "sec_availability"}
    assert "never weakened" in stage["retry"]


def test_stage_a_failure_means_missing_the_observation_honestly() -> None:
    stage = next(s for s in _sequence()["stages"] if s["stage"] == "A_PRE_DECISION")

    assert "miss the observation honestly" in stage["on_failure"]
    assert "substitute a current snapshot" in stage["on_failure"]


def test_the_sequence_does_not_execute_the_decision_or_execution_stages() -> None:
    sequence = _sequence()
    for name in ("D_DECISION", "E_EXECUTION_REFERENCE"):
        stage = next(s for s in sequence["stages"] if s["stage"] == name)
        assert stage["executed_by_r1_10"] is False


def test_the_sequence_is_operational_and_alters_no_h2_semantics() -> None:
    sequence = _sequence()

    assert sequence["alters_h2_semantics"] is False
    assert sequence["operational_only"] is True
    assert sequence["sequence_id"] == CAPTURE_SEQUENCE_ID


def test_the_score_may_be_computed_after_the_instant() -> None:
    sequence = _sequence()

    assert sequence["score_may_be_computed_after_the_instant"] is True
    assert "adds no information" in sequence["why"]


def test_the_runbook_names_the_one_unrecoverable_stage() -> None:
    runbook = decision_day_runbook(
        derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))
    )

    assert "Stage A" in runbook["the_one_thing_that_cannot_be_recovered"]
    assert all(c["consumes_observation"] is False for c in runbook["checkpoints"])
    assert "proves nothing about December" in runbook["readiness_must_be_rechecked"]


# --------------------------------------------------------------------------
# V1 → V2 governance
# --------------------------------------------------------------------------


def _v2() -> dict[str, Any]:
    disposition = {
        "disposition": "P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED",
        "september_30_remains_lost": True,
    }
    return protocol_v2(disposition=disposition, code_commit="0" * 40)


@needs_frozen
def test_v2_changes_no_h2_scientific_semantics() -> None:
    v1 = json.loads((R1_6 / "h2_prospective_protocol_v1.json").read_text(encoding="utf-8"))

    diff = v1_v2_diff(v1, _v2())

    assert diff["h2_scientific_semantics_changed"] is False
    assert diff["changed_fields"] == []
    assert diff["window_requirement_changed"] is False


@needs_frozen
def test_the_prospective_aggregate_rule_is_over_p1_to_p4() -> None:
    # R1.9's draft carried H2's historical C1..C4 rule by mistake. This is the
    # regression that catches it.
    assert _v2()["aggregate_rule"]["expression"] == "P1 AND P2 AND P3 AND P4"


@needs_frozen
def test_the_criteria_and_the_aggregate_rule_name_the_same_things() -> None:
    v2 = _v2()
    expression = v2["aggregate_rule"]["expression"]

    for criterion in v2["criteria"]:
        label = criterion.split("_")[0]
        assert label in expression, criterion


@needs_frozen
def test_v1_remains_byte_identical() -> None:
    import hashlib

    digest = hashlib.sha256((R1_6 / "h2_prospective_protocol_v1.json").read_bytes()).hexdigest()

    assert digest == "60c171369407bac9827e0b4146874b774a4b2dba69b79006ca84c727c8320fa3"


@needs_frozen
def test_the_diff_reports_what_v2_adds_without_changing_h2() -> None:
    v1 = json.loads((R1_6 / "h2_prospective_protocol_v1.json").read_text(encoding="utf-8"))

    diff = v1_v2_diff(v1, _v2())

    assert "missed_observation_disposition" in diff["added_in_v2"]
    assert "readiness_precondition" in diff["added_in_v2"]
    assert "V2 changes only prospective operational governance" in diff["verdict"]


def test_a_v2_that_shortened_the_window_would_be_caught() -> None:
    v1 = {
        "candidate_id": "c",
        "rule_version": "r",
        "rule": {
            "entry": "e",
            "retention": "t",
            "target_size": "s",
            "weighting": "w",
            "rebalance": "b",
            "execution": "x",
        },
        "criteria": [{"id": "P1"}],
        "aggregate_rule": {"expression": "P1"},
        "window": {"required_decisions": 5, "required_transitions": 4},
    }
    tampered = {**_v2(), "required_transitions": 2}
    tampered.update(
        {
            "candidate_id": "c",
            "rule_version": "r",
            "rule": v1["rule"],
            "criteria": ["P1"],
            "aggregate_rule": {"expression": "P1"},
            "required_decisions": 5,
        }
    )

    diff = v1_v2_diff(v1, tampered)

    assert diff["h2_scientific_semantics_changed"] is True
    assert "required_transitions" in diff["changed_fields"]
    assert diff["verdict"].startswith("STOP")


# --------------------------------------------------------------------------
# Activation and arm
# --------------------------------------------------------------------------


COMMIT = "0" * 40


def _activate(root: Path, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "code_commit": COMMIT,
        "candidate_sha256": "a" * 64,
        "protocol_v2_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "runner_version": "PROSPECTIVE_SHADOW_RUNNER_V1",
        "capture_sequence_sha256": "d" * 64,
        "readiness_sha256": "e" * 64,
    }
    arguments.update(overrides)
    return activate_v2(root, **arguments)


def test_a_v2_activation_binds_to_a_full_commit(tmp_path: Path) -> None:
    sealed = _activate(tmp_path)

    assert sealed.written is True
    assert sealed.body["code_commit"] == COMMIT
    assert sealed.body["protocol_id"] == PROTOCOL_V2_ID
    assert sealed.body["observations_at_activation"] == 0


def test_a_v2_activation_refuses_a_worktree(tmp_path: Path) -> None:
    with pytest.raises(WindowError, match="a worktree is not a commit"):
        _activate(tmp_path, code_commit="dirty")


def test_an_identical_v2_activation_reads_idempotently(tmp_path: Path) -> None:
    first = _activate(tmp_path)
    second = _activate(tmp_path)

    assert second.written is False
    assert second.sha256 == first.sha256


def test_a_different_v2_activation_is_refused(tmp_path: Path) -> None:
    _activate(tmp_path)

    with pytest.raises(WindowError, match="append-only"):
        _activate(tmp_path, candidate_sha256="f" * 64)


def test_the_v2_activation_explains_why_v1s_will_not_do(tmp_path: Path) -> None:
    sealed = _activate(tmp_path)

    assert "cannot authorise semantics it never saw" in sealed.body["why_a_new_activation"]
    assert sealed.body["supersedes_activation_for"] == ("H2_PROSPECTIVE_SHADOW_PROTOCOL_V1")


def _arm(root: Path, **overrides: Any) -> Any:
    activation = _activate(root)
    arguments: dict[str, Any] = {
        "activation": activation,
        "window": derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC)),
        "candidate_sha256": "a" * 64,
        "protocol_v2_sha256": "b" * 64,
        "capture_sequence_sha256": "d" * 64,
        "readiness_sha256": "e" * 64,
        "missed_opportunities": 1,
    }
    arguments.update(overrides)
    return arm_v2(root, **arguments)


def test_a_v2_arm_requires_a_v2_activation(tmp_path: Path) -> None:
    # A Sealed whose activation file was never written: the arm must refuse it
    # rather than trust the object it was handed.
    fake = Sealed(
        path=tmp_path / ACTIVATION_V2_FILE,
        sha256="0" * 64,
        written=False,
        body={"semantic_code_fingerprint": "x", "code_commit": COMMIT},
    )

    with pytest.raises(WindowError, match="requires a V2 activation"):
        arm_v2(
            tmp_path,
            activation=fake,
            window=derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC)),
            candidate_sha256="a" * 64,
            protocol_v2_sha256="b" * 64,
            capture_sequence_sha256="d" * 64,
            readiness_sha256="e" * 64,
            missed_opportunities=1,
        )


def test_a_v2_arm_creates_no_observation(tmp_path: Path) -> None:
    sealed = _arm(tmp_path)

    assert sealed.body["valid_observations_at_arm"] == 0
    assert sealed.body["creates_an_observation"] is False
    assert sealed.body["state"] == "ARMED_WAITING_FOR_DECISION"


def test_a_v2_arm_keeps_the_missed_opportunity_separate(tmp_path: Path) -> None:
    sealed = _arm(tmp_path)

    assert sealed.body["valid_observations_at_arm"] == 0
    assert sealed.body["missed_eligible_opportunities"] == 1
    assert sealed.body["missed_sessions"] == ["2026-09-30"]
    assert "not renumbered away" in sealed.body["valid_observation_numbering"]


def test_a_v2_arm_does_not_shorten_the_window(tmp_path: Path) -> None:
    sealed = _arm(tmp_path)

    assert sealed.body["required_decisions"] == 5
    assert sealed.body["required_transitions"] == 4
    assert sealed.body["window_requirement_unchanged"] is True


def test_the_v1_arm_cannot_authorise_v2(tmp_path: Path) -> None:
    sealed = _arm(tmp_path)

    assert sealed.body["v1_arm_cannot_authorise_v2"] is True
    assert sealed.path.name == ARM_V2_FILE
    assert (tmp_path / ACTIVATION_V2_FILE).is_file()


def test_an_identical_v2_arm_reads_idempotently(tmp_path: Path) -> None:
    first = _arm(tmp_path)
    second = _arm(tmp_path)

    assert second.written is False
    assert second.sha256 == first.sha256


def test_the_arm_records_the_derived_window(tmp_path: Path) -> None:
    sealed = _arm(tmp_path)

    assert sealed.body["decision_session"] == "2026-12-31"
    assert sealed.body["execution_reference_session"] == "2027-01-04"
    assert sealed.body["derived_automatically"] is True
    assert sealed.body["manually_supplied"] is False


# --------------------------------------------------------------------------
# Timezone and DST
# --------------------------------------------------------------------------


def test_every_derived_timestamp_is_timezone_aware() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))

    for key in ("decision_instant", "arm_cutoff", "armed_at"):
        assert datetime.fromisoformat(window[key]).tzinfo is not None


def test_the_december_instant_carries_standard_time() -> None:
    window = derive_window(after=date(2026, 9, 30), armed_at=datetime.now(UTC))
    instant = datetime.fromisoformat(window["decision_instant"])

    # December is EST, five hours behind UTC.
    assert instant.utcoffset() == timedelta(hours=-5)


def test_a_summer_instant_carries_daylight_time() -> None:
    window = derive_window(after=date(2027, 3, 31), armed_at=datetime.now(UTC))
    instant = datetime.fromisoformat(window["decision_instant"])

    # June is EDT, four hours behind UTC.
    assert window["decision_session"] == "2027-06-30"
    assert instant.utcoffset() == timedelta(hours=-4)
