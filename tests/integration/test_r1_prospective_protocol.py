"""Adversarial tests for the R1.6 candidate freeze and prospective protocol.

A protocol written before the data exists is only worth something if it cannot
be bent once the data arrives. Each test names a bend — a verdict declared
early, a window extended, a missed quarter reconstructed and counted, a frozen
packet rewritten, the historical effect size promoted into a threshold,
performance admitted to the verdict, a broker quietly authorised — and asserts
the protocol refuses it or records the refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stock_screener.backtest_harness.registry import digest_document
from stock_screener.membership_hypothesis import H2_ID, RULE, RULE_VERSION, H2BindingError
from stock_screener.prospective_protocol import (
    AGGREGATE_RULE,
    CANDIDATE_ID,
    CANDIDATE_STATE,
    COMPARATOR,
    CRITERIA,
    HISTORICAL_CLASSIFICATION,
    PROHIBITED_MODIFICATIONS,
    PROMOTION_LADDER,
    PROSPECTIVE_CONTRACT_ID,
    PROTOCOL_ID,
    REQUIRED_DECISIONS,
    REQUIRED_TRANSITIONS,
    STREAM_ID,
    bind_r1_5_record,
    candidate_freeze,
    capability_reachable,
    decision_packet_schema,
    evaluate_prospective,
    evaluation_schema,
    freeze_candidate_and_protocol,
    interpret_prospective,
    ledger_schema,
    prospective_contract,
    prospective_protocol,
)
from stock_screener.prospective_protocol import binding as binding_module

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FROZEN = REPO / ".evidence" / "research"
CONTRACT_SHA = "f7efb001cf79e40680758a3bd29ec2b016ad3d9d5199f363d7ee4f89eb1ad7d1"

HISTORICAL_TURNOVER = 1.6177403954997098
HISTORICAL_COMPARATOR_TURNOVER = 2.253982668222645
HISTORICAL_REDUCTION = 0.2823

needs_frozen = pytest.mark.skipif(
    not (FROZEN / "r1_5" / "r1_5_complete.json").is_file(),
    reason="the frozen R1.5 record is runtime data, not committed",
)


def _protocol() -> dict[str, Any]:
    return prospective_protocol(code_commit="0" * 40)


def _binding_json() -> dict[str, Any]:
    return {
        "contract_sha256": CONTRACT_SHA,
        "h2_record_sha256": "a" * 64,
        "r1_5_experiment_spec_sha256": "b" * 64,
        "r1_5_evaluation_sha256": "c" * 64,
        "r1_5_completion_marker_sha256": "d" * 64,
        "r1_5_outcome": "SUPPORTED",
        "historical_measurements": {
            "h2_annual_turnover": HISTORICAL_TURNOVER,
            "comparator_annual_turnover": HISTORICAL_COMPARATOR_TURNOVER,
            "h2_churn_annual_turnover": 1.411598539883737,
            "comparator_churn_annual_turnover": 2.11411510465003,
            "h2_retention": 0.643969,
            "comparator_retention": 0.457707,
            "turnover_reduction_fraction": HISTORICAL_REDUCTION,
            "retention_change_absolute": 0.186262,
            "h2_total_return": 1.279,
            "comparator_total_return": 1.2655,
            "h2_sharpe": 0.9793,
            "comparator_sharpe": 0.9810,
            "criteria_met": [1, 2, 3, 4],
            "criteria_failed": [],
            "rule": dict(RULE),
        },
    }


def _candidate() -> dict[str, Any]:
    return candidate_freeze(binding=_binding_json(), protocol_id=PROTOCOL_ID, code_commit="0" * 40)


# --------------------------------------------------------------------------
# This phase cannot act
# --------------------------------------------------------------------------


def test_no_simulator_data_client_or_broker_is_importable() -> None:
    assert capability_reachable() == []


@pytest.mark.parametrize(
    "line",
    [
        "from stock_screener.backtest_harness import simulate\n",
        "import httpx\n",
        "from api_clients.alpaca import AlpacaClient\n",
    ],
)
def test_the_guard_catches_an_acting_import(tmp_path: Path, line: str) -> None:
    (tmp_path / "leak.py").write_text(line, encoding="utf-8")

    assert capability_reachable(tmp_path) != []


# --------------------------------------------------------------------------
# The candidate freeze
# --------------------------------------------------------------------------


def test_the_candidate_has_one_durable_identity() -> None:
    assert CANDIDATE_ID == "H2_MEMBERSHIP_STABILITY_CANDIDATE_V1"
    assert _candidate()["candidate_id"] == CANDIDATE_ID
    assert _candidate()["rule_version"] == RULE_VERSION


def test_every_state_word_is_carried_so_none_is_read_alone() -> None:
    state = _candidate()["state"]

    for word in (
        "FROZEN_CANDIDATE",
        "PROSPECTIVE_CONFIRMATION_PENDING",
        "NOT_PRODUCTION",
        "NO_LIVE_CAPITAL",
    ):
        assert word in state
    assert set(state) == set(CANDIDATE_STATE)


def test_the_candidate_is_frozen_on_the_mechanism_not_on_performance() -> None:
    why = _candidate()["why_this_is_frozen"]

    assert "preregistered mechanism passed" in why["reason"]
    assert "not why anything is frozen" in why["not_the_reason"]
    assert why["performance_entered_the_verdict"] is False


def test_historical_performance_is_recorded_as_deciding_nothing() -> None:
    performance = _candidate()["historical_performance_references"]

    assert performance["is_a_criterion"] is False
    assert performance["validated"] is False


def test_the_candidate_binds_to_every_frozen_input() -> None:
    bound = _candidate()["bound_to"]

    for key in (
        "h2_record_sha256",
        "r1_5_experiment_spec_sha256",
        "r1_5_evaluation_sha256",
        "r1_5_completion_marker_sha256",
        "contract_version",
        "score_version",
        "execution_rule",
        "terminal_policy",
        "cost_convention",
        "comparator",
    ):
        assert bound[key]
    assert bound["score_version"] == "COMPOUNDER_V1_2"
    assert bound["execution_rule"] == "CLOSE_SIGNAL_NEXT_SESSION_OPEN"


def test_historical_development_is_closed() -> None:
    development = _candidate()["historical_development"]

    assert development["parameter_search"] == "CLOSED"
    assert development["additional_retention_widths"] == "PROHIBITED"
    assert development["additional_frequencies"] == "PROHIBITED"
    assert development["additional_weighting_rules"] == "PROHIBITED"
    assert development["additional_holding_period_rules"] == "PROHIBITED"
    assert development["historical_clean_holdout"] == "NONE"


def test_a_new_historical_variant_could_not_supply_confirmation() -> None:
    required = _candidate()["historical_development"]["a_new_variant_would_require"]

    assert any("new hypothesis identifier" in line for line in required)
    assert any("EXPLORATORY_SUPPORT_ONLY" in line for line in required)


def test_the_candidate_claims_no_confirmation() -> None:
    candidate = _candidate()

    assert candidate["claims_confirmation"] is False
    assert candidate["historical_result"]["classification"] == HISTORICAL_CLASSIFICATION
    assert candidate["prospective_status"]["independent_confirmation"] == "PENDING"
    assert candidate["prospective_status"]["observations_collected"] == 0


def test_no_live_or_paper_authorisation_exists() -> None:
    authorisations = _candidate()["authorisations"]

    assert authorisations["broker_connectivity"] == "NOT AUTHORISED"
    assert authorisations["paper_orders"] == "NOT AUTHORISED"
    assert authorisations["live_orders"] == "NOT AUTHORISED"
    assert authorisations["real_capital"] == "NOT AUTHORISED"


def test_the_ladder_authorises_only_the_first_step() -> None:
    ladder = {step["stage"]: step["status"] for step in PROMOTION_LADDER}

    assert ladder["FROZEN_H2_CANDIDATE"] == "CURRENT"
    assert "R1.7" in ladder["PROSPECTIVE_SHADOW"]
    for stage in ("BROKER_PAPER", "SUPERVISED_SMALL_LIVE", "CONSTRAINED_AUTONOMOUS"):
        assert ladder[stage].startswith("NOT AUTHORISED")


@pytest.mark.parametrize(
    "forbidden", ["retention band", "frequency", "weighting", "MARKET_SIGNALS", "sector"]
)
def test_the_prohibited_list_names_the_obvious_temptations(forbidden: str) -> None:
    assert any(forbidden in line for line in PROHIBITED_MODIFICATIONS)


# --------------------------------------------------------------------------
# The prospective protocol
# --------------------------------------------------------------------------


def test_the_protocol_is_frozen_and_not_active() -> None:
    status = _protocol()["status_at_freeze"]

    assert status["protocol_definition"] == "FROZEN"
    assert status["prospective_runner"] == "NOT_YET_ACTIVE"
    assert status["first_eligible_decision"] == "NOT_YET_ASSIGNED"
    assert status["observations"] == 0
    assert status["completed_transitions"] == 0
    assert status["prospective_verdict"] == "PENDING"


def test_no_first_decision_date_is_hardcoded() -> None:
    first = _protocol()["first_eligible_decision"]

    assert first["assigned"] is False
    assert first["value"] == "NOT_YET_ASSIGNED"
    assert first["hardcoded_date"] is None


def test_the_first_decision_requires_the_runner_armed_before_the_prior_close() -> None:
    rule = _protocol()["first_eligible_decision"]["rule"]

    assert "strictly after PROSPECTIVE_PROTOCOL_ACTIVE_AT" in rule
    assert "immediately preceding benchmark session" in rule


def test_a_missed_quarter_is_skipped_rather_than_reconstructed() -> None:
    first = _protocol()["first_eligible_decision"]

    assert "skip that quarter" in first["if_the_arming_requirement_is_missed"]


def test_a_retrospective_reconstruction_never_counts() -> None:
    retrospective = _protocol()["retrospective_reconstruction"]

    assert retrospective["counts_toward_confirmation"] is False
    assert set(retrospective["mandatory_labels"]) == {
        "RETROSPECTIVE",
        "NOT_PROSPECTIVE_CONFIRMATION",
    }
    assert "backtest" in retrospective["rule"]


def test_the_window_is_five_decisions_and_four_transitions() -> None:
    window = _protocol()["window"]

    assert REQUIRED_DECISIONS == 5
    assert REQUIRED_TRANSITIONS == 4
    assert window["required_decisions"] == 5
    assert window["required_transitions"] == 4


def test_the_window_cannot_move_in_either_direction() -> None:
    window = _protocol()["window"]

    for flag in (
        "stop_early_on_success",
        "extend_on_unfavourable_result",
        "choose_best_four_afterwards",
        "drop_an_unfavourable_transition",
        "replace_an_outage_transition_retrospectively",
    ):
        assert window[flag] is False


def test_initial_formation_must_match_the_comparator_exactly() -> None:
    formation = _protocol()["initial_formation"]

    assert "equals the comparator's exactly" in formation["requirement"]
    assert formation["a_difference_is"].startswith("A PROTOCOL DEFECT")


def test_the_design_is_paired_and_only_membership_differs() -> None:
    paired = _protocol()["paired_design"]

    assert paired["mandatory"] is True
    assert paired["differs"].startswith("membership persistence")
    for shared in ("universe", "scores", "rank order", "execution-price source"):
        assert shared in paired["shared"]


def test_the_protocol_is_shadow_and_authorises_no_broker() -> None:
    execution = _protocol()["execution"]

    assert execution["mode"] == "SHADOW"
    assert execution["broker_connectivity"] == "NOT AUTHORISED"
    assert execution["paper_orders"] == "NOT AUTHORISED"
    assert execution["live_orders"] == "NOT AUTHORISED"
    assert "broker paper account" in execution["terminology"]["paper"]


def test_the_stream_is_separate_from_the_historical_dataset() -> None:
    protocol = _protocol()
    contract = prospective_contract()

    assert protocol["data_stream"] == STREAM_ID
    assert contract["storage"]["must_not_write_into"] == "PIT_RESEARCH_DATASET_V2"


def test_the_comparator_is_unchanged() -> None:
    assert _protocol()["comparator"] == COMPARATOR == "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"


def test_a_semantic_change_ends_the_test() -> None:
    changes = _protocol()["changes_during_the_window"]

    assert changes["a_semantic_change_ends_the_test"] is True
    assert changes["code_change_classification"]["on_doubt"].startswith("FAIL CLOSED")


def test_poor_results_are_not_an_early_termination_reason() -> None:
    termination = _protocol()["early_termination"]

    assert "poor returns" in termination["not_reasons"]
    assert "high turnover" in termination["not_reasons"]
    assert "semantic pipeline defect" in termination["legitimate_reasons"]


def test_a_defect_invalidates_rather_than_rewrites() -> None:
    defect = _protocol()["defect_discovered_later"]

    assert defect["rule"] == "PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT"
    assert "never rewritten" in defect["affected_decisions"]


def test_confirmation_language_is_bounded_in_advance() -> None:
    meaning = _protocol()["what_confirmation_would_mean"]

    assert "membership-stability mechanism" in meaning["permitted_statement"]
    for forbidden in (
        "the strategy has proven alpha",
        "the strategy is ready for real money",
        "the historical returns are validated",
    ):
        assert forbidden in meaning["not_permitted"]


def test_failure_does_not_permit_changing_the_band_and_continuing() -> None:
    failure = _protocol()["what_failure_would_mean"]

    assert failure["label"] == "H2_PROSPECTIVE_MECHANISM_NOT_CONFIRMED"
    assert "new hypothesis and a new protocol" in failure["not_permitted"]


# --------------------------------------------------------------------------
# The criteria
# --------------------------------------------------------------------------


def _closed(**overrides: Any) -> list[dict[str, Any]]:
    arguments: dict[str, Any] = {
        "completed_transitions": REQUIRED_TRANSITIONS,
        "h2_turnover": 1.5,
        "comparator_turnover": 2.0,
        "h2_churn": 1.2,
        "comparator_churn": 1.8,
    }
    arguments.update(overrides)
    return evaluate_prospective(**arguments)


def test_there_are_exactly_four_criteria() -> None:
    assert len(CRITERIA) == 4
    assert [row["id"] for row in _closed()] == list(CRITERIA)


def test_p1_and_p2_are_paired_and_directional() -> None:
    criteria = _closed()

    for row in criteria[:2]:
        assert row["comparison"] == "PAIRED_AND_DIRECTIONAL"
        assert row["historical_magnitude_required"] is False


def test_p1_is_a_strict_comparison_against_the_paired_comparator() -> None:
    assert _closed(h2_turnover=2.0, comparator_turnover=2.0)[0]["met"] is False
    assert _closed(h2_turnover=1.999, comparator_turnover=2.0)[0]["met"] is True


def test_p2_compares_the_absolute_churn_quantity() -> None:
    criteria = _closed(h2_churn=1.8, comparator_churn=1.8)

    assert criteria[1]["met"] is False
    assert "never a share" in criteria[1]["measured_as"]


def test_the_historical_reduction_is_not_a_threshold() -> None:
    # A prospective reduction far smaller than the historical 28.2% still passes.
    criteria = _closed(h2_turnover=1.999, comparator_turnover=2.0)

    assert criteria[0]["met"] is True
    assert interpret_prospective(criteria)["outcome"] == ("PROSPECTIVE_MECHANISM_CONFIRMED")


def test_the_historical_values_are_labelled_references_only() -> None:
    references = _protocol()["historical_values_are_references_only"]

    assert references["any_of_these_is_a_prospective_threshold"] is False
    assert references["h2_annual_turnover"] == HISTORICAL_TURNOVER
    assert references["legacy_h1_turnover_reference"] == 1.95


def test_p3_fails_on_a_target_count_mismatch() -> None:
    assert _closed(target_count_mismatches=["2027-03-31"])[2]["met"] is False


def test_p3_fails_on_hysteresis_caused_cash() -> None:
    assert _closed(hysteresis_cash_quarters=1)[2]["met"] is False


def test_p3_fails_on_hysteresis_caused_exposure_reduction() -> None:
    assert _closed(hysteresis_exposure_reduction=True)[2]["met"] is False


@pytest.mark.parametrize(
    "field",
    [
        "accounting_violations",
        "identity_violations",
        "leakage_violations",
        "same_close_violations",
        "retrospective_replacements",
    ],
)
def test_p4_fails_on_any_single_finding(field: str) -> None:
    criteria = _closed(**{field: ["something happened"]})

    assert criteria[3]["met"] is False
    assert interpret_prospective(criteria)["outcome"] == ("PROSPECTIVE_MECHANISM_NOT_CONFIRMED")


def test_a_rewritten_packet_fails_p4_rather_than_being_absorbed() -> None:
    criteria = _closed(retrospective_replacements=["2027-06-30 packet replaced"])

    assert criteria[3]["met"] is False


def test_the_aggregate_rule_is_all_four() -> None:
    assert AGGREGATE_RULE["expression"] == "P1 AND P2 AND P3 AND P4"
    assert AGGREGATE_RULE["majority_vote"] is False
    assert AGGREGATE_RULE["weighting"] is False
    assert AGGREGATE_RULE["qualitative_override"] is False
    assert AGGREGATE_RULE["partial_labels_permitted"] is False
    assert AGGREGATE_RULE["stated_before_any_observation"] is True


@pytest.mark.parametrize("failing", [0, 1, 2, 3])
def test_any_one_failure_is_not_confirmed(failing: int) -> None:
    criteria = _closed()
    criteria[failing]["met"] = False

    assert interpret_prospective(criteria)["outcome"] == ("PROSPECTIVE_MECHANISM_NOT_CONFIRMED")


def test_all_four_met_is_confirmed() -> None:
    verdict = interpret_prospective(_closed())

    assert verdict["outcome"] == "PROSPECTIVE_MECHANISM_CONFIRMED"
    assert verdict["performance_entered_this_verdict"] is False


def test_the_evaluation_takes_no_performance_argument() -> None:
    import inspect

    parameters = set(inspect.signature(evaluate_prospective).parameters)

    for forbidden in ("return", "sharpe", "performance", "nav", "drawdown", "benchmark"):
        assert not any(forbidden in name for name in parameters), forbidden


def test_a_criterion_set_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(ValueError, match="combines 4 criteria, got 3"):
        interpret_prospective(_closed()[:3])


# --------------------------------------------------------------------------
# No early verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("transitions", [0, 1, 2, 3])
def test_no_verdict_exists_before_the_window_closes(transitions: int) -> None:
    criteria = _closed(completed_transitions=transitions)
    verdict = interpret_prospective(criteria)

    assert verdict["outcome"] == "PENDING"
    assert verdict["candidate_status"] == "PROSPECTIVE_CONFIRMATION_PENDING"
    for row in criteria:
        assert row["status"] == "PENDING"
        assert row["met"] is None


def test_an_interim_window_reports_progress_not_success() -> None:
    verdict = interpret_prospective(_closed(completed_transitions=3))

    assert verdict["completed_transitions"] == 3
    assert verdict["required_transitions"] == 4
    assert "CONFIRMED" not in verdict["outcome"]


def test_interim_policy_forbids_the_success_words() -> None:
    interim = _protocol()["interim_policy"]

    for forbidden in ("CONFIRMED", "FAILED", "SUPPORTED PROSPECTIVELY"):
        assert forbidden in interim["forbidden_before_the_window_closes"]
    assert interim["verdict_before_completion"] == "PENDING"
    assert interim["promotion_before_completion"].startswith("NOT PERMITTED")


def test_performance_is_never_a_confirmation_criterion() -> None:
    diagnostics = _protocol()["diagnostics"]

    assert diagnostics["performance_is_a_criterion"] is False
    assert diagnostics["performance_status_label"] == "PROSPECTIVE_PERFORMANCE_OBSERVATION"
    assert diagnostics["retention_has_a_pass_threshold"] is False


# --------------------------------------------------------------------------
# The schemas
# --------------------------------------------------------------------------


def test_a_decision_packet_cannot_carry_an_outcome() -> None:
    schema = decision_packet_schema()

    assert schema["additionalProperties"] is False
    assert "forward_return" not in schema["properties"]
    assert "any forward return" in schema["forbidden_content"]


def test_a_decision_packet_records_who_and_when_and_under_what() -> None:
    required = decision_packet_schema()["required"]

    for field in (
        "decision_timestamp",
        "runner_armed_at",
        "code_commit",
        "protocol_id",
        "candidate_id",
        "contract_id",
        "fingerprints",
        "comparator_target_set",
        "h2_target_set",
        "observation_kind",
    ):
        assert field in required


def test_a_packet_is_immutable_by_key_and_digest() -> None:
    assert "never silently overwritten" in decision_packet_schema()["immutability"]


def test_a_packet_distinguishes_prospective_from_retrospective() -> None:
    kinds = decision_packet_schema()["properties"]["observation_kind"]["enum"]

    assert set(kinds) == {"PROSPECTIVE", "RETROSPECTIVE"}


def test_the_ledger_records_semantic_change_status_per_decision() -> None:
    entry = ledger_schema()["properties"]["entries"]["items"]

    assert "semantic_change_status" in entry["required"]
    assert "SEMANTIC_RESEARCH_CHANGE" in (entry["properties"]["semantic_change_status"]["enum"])


def test_a_correction_may_never_rewrite_a_frozen_packet() -> None:
    corrections = ledger_schema()["properties"]["post_decision_corrections"]["items"]

    assert corrections["properties"]["rewrote_a_frozen_packet"] == {"const": False}


def test_the_ledger_caps_completed_transitions_at_four() -> None:
    progress = ledger_schema()["properties"]["window_progress"]["properties"]

    assert progress["completed_transitions"]["maximum"] == 4


def test_the_evaluation_schema_forbids_performance_entering_the_verdict() -> None:
    schema = evaluation_schema()

    assert schema["properties"]["performance_entered_verdict"] == {"const": False}
    assert schema["properties"]["criteria"]["minItems"] == 4
    assert schema["properties"]["criteria"]["maxItems"] == 4


def test_the_contract_bridges_rather_than_assumes() -> None:
    contract = prospective_contract()

    assert contract["contract_id"] == PROSPECTIVE_CONTRACT_ID
    assert contract["derived_from"] == "BACKTEST_DATA_CONTRACT_V1"
    assert "direction_of_error" in contract["decision_time_availability"]
    assert contract["missingness"]["rule"].startswith("missing is not zero")


def test_the_contract_forbids_price_substitution() -> None:
    execution = prospective_contract()["execution_reference"]

    assert "a previous-close substitution" in execution["forbidden"]
    assert "a same-close fill" in execution["forbidden"]


def test_the_contract_excludes_market_signals_and_sector() -> None:
    excluded = prospective_contract()["excluded_inputs"]

    assert "MARKET_SIGNALS_V1" in excluded
    assert excluded["sector_or_industry"].startswith("NONE")


def test_the_contract_keeps_the_frozen_score_and_benchmark() -> None:
    contract = prospective_contract()

    assert contract["feature_timestamp_rules"]["score_version"] == "COMPOUNDER_V1_2, frozen"
    assert contract["benchmark"]["identity"] == "SPY"


# --------------------------------------------------------------------------
# Binding, against the frozen record
# --------------------------------------------------------------------------


def _bind(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "contract_sha256": CONTRACT_SHA,
        "expect_contract_sha256": CONTRACT_SHA,
    }
    arguments.update(overrides)
    return bind_r1_5_record(FROZEN / "r1_4", FROZEN / "r1_5", **arguments)


@needs_frozen
def test_the_closed_historical_record_binds() -> None:
    binding = _bind()

    assert binding.outcome == "SUPPORTED"
    assert binding.as_json()["observations_collected"] == 0


@needs_frozen
@pytest.mark.parametrize(
    "field",
    [
        "expect_h2_record_sha256",
        "expect_spec_sha256",
        "expect_evaluation_sha256",
        "expect_marker_sha256",
    ],
)
def test_any_moved_upstream_digest_refuses_to_freeze(field: str) -> None:
    with pytest.raises(H2BindingError, match="not the frozen one"):
        _bind(**{field: "0" * 64})


@needs_frozen
def test_a_moved_contract_refuses_to_freeze() -> None:
    with pytest.raises(H2BindingError, match="contract is"):
        _bind(contract_sha256="0" * 64)


@needs_frozen
def test_an_unsupported_historical_outcome_refuses_to_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    r1_5 = tmp_path / "r1_5"
    shutil.copytree(FROZEN / "r1_5", r1_5)
    body = json.loads((r1_5 / "r1_5_h2_evaluation.json").read_text(encoding="utf-8"))
    body["interpretation"]["outcome"] = "NOT_SUPPORTED"
    (r1_5 / "r1_5_h2_evaluation.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    from stock_screener.signal_review.binding import file_sha256

    monkeypatch.setattr(
        binding_module,
        "R1_5_EVALUATION_SHA256",
        file_sha256(r1_5 / "r1_5_h2_evaluation.json"),
    )

    with pytest.raises(H2BindingError, match="a candidate is frozen only on SUPPORTED"):
        bind_r1_5_record(
            FROZEN / "r1_4",
            r1_5,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
            expect_evaluation_sha256=file_sha256(r1_5 / "r1_5_h2_evaluation.json"),
        )


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@needs_frozen
def test_the_phase_writes_the_protocol_and_no_observation(tmp_path: Path) -> None:
    result = freeze_candidate_and_protocol(
        tmp_path / "out",
        FROZEN / "r1_4",
        FROZEN / "r1_5",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    complete = json.loads((tmp_path / "out" / "r1_6_complete.json").read_text(encoding="utf-8"))

    assert result.candidate["candidate_id"] == CANDIDATE_ID
    assert result.protocol["protocol_id"] == PROTOCOL_ID
    assert complete["observations_collected"] == 0
    assert complete["completed_transitions"] == 0
    assert complete["prospective_runner"] == "NOT_YET_ACTIVE"
    assert complete["first_eligible_decision"] == "NOT_YET_ASSIGNED"
    assert complete["capability_reachable_from_this_package"] == []
    assert not list((tmp_path / "out").glob("*decision_packet_2*"))


@needs_frozen
def test_two_runs_agree_on_every_fingerprint(tmp_path: Path) -> None:
    runs = [
        freeze_candidate_and_protocol(
            tmp_path / f"run{index}",
            FROZEN / "r1_4",
            FROZEN / "r1_5",
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
            code_commit="0" * 40,
        )
        for index in range(2)
    ]

    assert runs[0].fingerprints == runs[1].fingerprints
    assert len(runs[0].fingerprints) == 8


@needs_frozen
def test_the_ledger_records_a_freeze_not_an_experiment(tmp_path: Path) -> None:
    freeze_candidate_and_protocol(
        tmp_path / "out",
        FROZEN / "r1_4",
        FROZEN / "r1_5",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    entry = json.loads(
        (tmp_path / "out" / "research_ledger.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )

    assert entry["mode"] == "CANDIDATE_FREEZE_AND_PROTOCOL_DEFINITION"
    assert entry["variants_tried"] == 0
    assert entry["outcome"]["observations_collected"] == 0
    assert entry["outcome"]["new_historical_variants"] == 0
    assert entry["outcome"]["broker_connections"] == 0
    assert entry["outcome"]["orders_placed"] == 0
    assert entry["outcome"]["data_requests"] == 0


@needs_frozen
def test_the_historical_record_is_not_mutated(tmp_path: Path) -> None:
    from stock_screener.signal_review.binding import file_sha256

    before = {path.name: file_sha256(path) for path in sorted((FROZEN / "r1_5").glob("*.json"))}

    freeze_candidate_and_protocol(
        tmp_path / "out",
        FROZEN / "r1_4",
        FROZEN / "r1_5",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )

    after = {path.name: file_sha256(path) for path in sorted((FROZEN / "r1_5").glob("*.json"))}
    assert after == before


def test_the_documents_are_deterministic() -> None:
    assert digest_document(_protocol()) == digest_document(_protocol())
    assert digest_document(_candidate()) == digest_document(_candidate())
    assert digest_document(prospective_contract()) == digest_document(prospective_contract())


def test_the_hypothesis_identity_is_carried_forward_unchanged() -> None:
    assert _candidate()["hypothesis_id"] == H2_ID
    assert _protocol()["rule_version"] == RULE_VERSION
