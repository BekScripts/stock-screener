"""Adversarial tests for the H2 preregistration.

A preregistration is only worth something if it is hard to escape later, so each
test names an escape — a second band width, a relabelled holdout, a rule that
drifted between the document and the executor, a spec run twice, 1.95x promoted
back into a pass criterion, a performance number computed under cover of
definition — and asserts the code refuses it.

The rule itself is exercised on synthetic cross-sections only. R1.4 must not
learn what H2 would have done, so no test here runs it over the frozen record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stock_screener.backtest_harness.registry import digest_document
from stock_screener.membership_hypothesis import (
    AGGREGATE_RULE,
    COMPARATOR,
    CRITERIA,
    ENTRY_WIDTHS,
    H2_ID,
    HISTORICAL_STATUS,
    LEGACY_H1_TURNOVER_REFERENCE,
    PROHIBITED_VARIATIONS,
    RETENTION_WIDTHS,
    RULE,
    RULE_VERSION,
    SINGULAR_FIELDS,
    SPEC_STATUS,
    Candidate,
    H2BindingError,
    experiment_spec,
    hypothesis_record,
    preregister_h2,
    rank,
    retention_limit,
    select,
    simulation_reachable,
    verify_h2,
)

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FROZEN = REPO / ".evidence" / "research"
CONTRACT_SHA = "f7efb001cf79e40680758a3bd29ec2b016ad3d9d5199f363d7ee4f89eb1ad7d1"

needs_frozen = pytest.mark.skipif(
    not (FROZEN / "r1_3" / "r1_3_complete.json").is_file(),
    reason="the frozen R1.3 record is runtime data, not committed",
)


def _line(index: int, score: float = 0.0) -> Candidate:
    return Candidate("COMPANY", index, score or (100.0 - index))


def _cross_section(size: int) -> list[Candidate]:
    return [_line(index) for index in range(size)]


# --------------------------------------------------------------------------
# Exactly one rule, and the band is structural
# --------------------------------------------------------------------------


def test_exactly_one_rule_version_exists() -> None:
    assert RULE_VERSION == "ONE_DECILE_HYSTERESIS_V1"
    assert RULE["rule_version"] == RULE_VERSION


def test_entry_is_one_selection_width_and_retention_is_two() -> None:
    assert ENTRY_WIDTHS == 1
    assert RETENTION_WIDTHS == 2


def test_the_band_is_two_target_counts_not_a_chosen_percentile() -> None:
    assert retention_limit(74) == 148
    assert retention_limit(0) == 0


def test_the_band_tracks_the_target_count_rather_than_a_fixed_percent() -> None:
    assert retention_limit(70) / 700 == pytest.approx(0.20)
    assert retention_limit(101) / 1010 == pytest.approx(0.20)


def test_quarterly_frequency_is_frozen_in_the_rule() -> None:
    assert RULE["rebalance"] == "calendar quarter ends only"


def test_equal_weighting_is_frozen_in_the_rule() -> None:
    assert RULE["weighting"] == "equal weight"


def test_next_open_execution_is_frozen_in_the_rule() -> None:
    assert RULE["execution"] == ("open of the first benchmark session strictly after the signal")


def test_no_other_turnover_mechanism_is_admitted() -> None:
    for name in (
        "minimum_holding_period",
        "cooldown",
        "turnover_penalty",
        "score_smoothing",
        "sector_logic",
        "leverage",
        "shorting",
    ):
        assert RULE[name] == "NONE"


@pytest.mark.parametrize("width", ["12%", "15%", "18%", "25%", "30%"])
def test_no_alternative_band_width_appears_anywhere_in_the_rule(width: str) -> None:
    assert width not in json.dumps(RULE)


def test_the_prohibited_list_names_the_variations_a_later_phase_might_reach_for() -> None:
    joined = " ".join(PROHIBITED_VARIATIONS)
    for forbidden in ("retention band", "frequency", "weighting", "minimum holding"):
        assert forbidden in joined


# --------------------------------------------------------------------------
# The selection algorithm
# --------------------------------------------------------------------------


def test_the_first_rebalance_is_exactly_the_ordinary_top_decile() -> None:
    result = select(_cross_section(100), [], 10)

    assert result.selected == tuple(("COMPANY", index) for index in range(10))
    assert result.retained_in_band == ()
    assert len(result.admitted) == 10


def test_an_incumbent_inside_the_band_is_kept_though_outside_the_decile() -> None:
    result = select(_cross_section(100), [("COMPANY", 15)], 10)

    assert ("COMPANY", 15) in result.selected
    assert result.retained_in_band == (("COMPANY", 15),)


def test_an_incumbent_past_the_band_is_sold() -> None:
    result = select(_cross_section(100), [("COMPANY", 25)], 10)

    assert ("COMPANY", 25) not in result.selected
    assert result.dropped_out_of_band == (("COMPANY", 25),)


def test_an_incumbent_exactly_at_the_band_edge_is_outside_it() -> None:
    result = select(_cross_section(100), [("COMPANY", 20)], 10)

    assert result.dropped_out_of_band == (("COMPANY", 20),)


def test_the_last_position_inside_the_band_is_retained() -> None:
    result = select(_cross_section(100), [("COMPANY", 19)], 10)

    assert result.retained_in_band == (("COMPANY", 19),)


def test_an_ineligible_incumbent_is_forced_out_however_well_ranked() -> None:
    rows = _cross_section(100)
    rows[1] = Candidate("COMPANY", 1, 99.0, eligible=False)

    result = select(rows, [("COMPANY", 1)], 10)

    assert result.forced_out == (("COMPANY", 1),)
    assert ("COMPANY", 1) not in result.selected


def test_an_ineligible_identity_is_never_admitted() -> None:
    rows = _cross_section(100)
    rows[0] = Candidate("COMPANY", 0, 100.0, eligible=False)

    result = select(rows, [], 10)

    assert ("COMPANY", 0) not in result.selected
    assert len(result.selected) == 10


def test_over_capacity_drops_the_worst_ranked_incumbents_first() -> None:
    incumbents = [("COMPANY", index) for index in range(15)]

    result = select(_cross_section(100), incumbents, 10)

    assert result.dropped_over_capacity == tuple(("COMPANY", index) for index in range(10, 15))
    assert len(result.selected) == 10


def test_vacancies_are_filled_with_the_highest_ranked_unheld_identities() -> None:
    result = select(_cross_section(100), [("COMPANY", 15)], 10)

    assert result.admitted == tuple(("COMPANY", index) for index in range(9))


def test_the_portfolio_always_reaches_the_target_count_when_names_exist() -> None:
    for incumbents in ([], [("COMPANY", 15)], [("COMPANY", index) for index in range(19)]):
        assert len(select(_cross_section(100), incumbents, 10).selected) == 10


def test_a_short_cross_section_cannot_reach_the_target_count() -> None:
    result = select(_cross_section(4), [], 10)

    assert len(result.selected) == 4


def test_selection_never_exceeds_the_target_count() -> None:
    incumbents = [("COMPANY", index) for index in range(20)]

    assert len(select(_cross_section(100), incumbents, 10).selected) == 10


def test_ties_break_on_kind_then_identity_ascending() -> None:
    rows = [
        Candidate("HISTORICAL", 5, 50.0),
        Candidate("COMPANY", 9, 50.0),
        Candidate("COMPANY", 2, 50.0),
    ]

    assert [row.key for row in rank(rows)] == [
        ("COMPANY", 2),
        ("COMPANY", 9),
        ("HISTORICAL", 5),
    ]


def test_a_negative_target_count_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        select(_cross_section(10), [], -1)


def test_a_repeated_incumbent_is_counted_once() -> None:
    result = select(_cross_section(100), [("COMPANY", 15), ("COMPANY", 15)], 10)

    assert result.retained_in_band == (("COMPANY", 15),)
    assert len(result.selected) == 10


def test_displacement_pairs_each_retained_incumbent_with_a_passed_over_name() -> None:
    result = select(_cross_section(100), [("COMPANY", 15)], 10)

    assert len(result.displaced) == 1
    retained, displaced = result.displaced[0]
    assert retained == ("COMPANY", 15)
    assert displaced == ("COMPANY", 9)


def test_no_displacement_when_nothing_was_retained_out_of_the_decile() -> None:
    assert select(_cross_section(100), [("COMPANY", 3)], 10).displaced == ()


def test_the_selection_is_deterministic() -> None:
    rows = _cross_section(100)
    incumbents = [("COMPANY", 15), ("COMPANY", 3)]

    first = select(rows, incumbents, 10).as_json()
    second = select(rows, incumbents, 10).as_json()

    assert digest_document(first) == digest_document(second)


def test_the_rule_module_cannot_see_a_forward_return() -> None:
    import ast

    source = (REPO / "src" / "stock_screener" / "membership_hypothesis" / "rule.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }

    for forbidden in ("price", "nav", "forward_return", "load_labels", "outcome"):
        assert not any(forbidden in name.lower() for name in names), forbidden
        assert not any(forbidden in name.lower() for name in imported), forbidden


def test_a_candidate_has_no_field_an_outcome_could_arrive_in() -> None:
    assert set(Candidate.__dataclass_fields__) == {
        "kind",
        "identity_id",
        "final_score",
        "eligible",
    }


# --------------------------------------------------------------------------
# R1.4 runs nothing
# --------------------------------------------------------------------------


def test_no_simulator_or_pricing_path_is_importable() -> None:
    assert simulation_reachable() == []


def test_the_guard_catches_a_pricing_import(tmp_path: Path) -> None:
    (tmp_path / "leak.py").write_text(
        "from stock_screener.backtest_harness import performance\n", encoding="utf-8"
    )

    assert simulation_reachable(tmp_path) == ["leak.py: performance"]


# --------------------------------------------------------------------------
# The criteria, the aggregate rule and the legacy threshold
# --------------------------------------------------------------------------


def _comparator(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "strategy_version": COMPARATOR,
        "annual_turnover": 2.253982668222645,
        "ranking_churn_share": 0.9379464777859601,
        "ranking_churn_annual_turnover": 2.11411510465003,
        "resizing_share": 0.05977996469050918,
        "quarterly_retention": 0.457707,
        "rebalances": 17,
    }
    body.update(overrides)
    return body


def _evidence() -> dict[str, Any]:
    return {
        "quarterly_annual_turnover": 2.253982668222645,
        "membership_floor": 2.124231889531036,
        "per_rebalance_ratio": 1.7963905824492163,
        "ranking_churn_share": 0.9379464777859601,
        "share_moving_under_10_points": 0.3421875,
        "share_moving_over_20_points": 0.37578125,
        "exit_share_not_ranking": 0.13,
        "entry_share_not_ranking": 0.244,
    }


def _record(**overrides: Any) -> dict[str, Any]:
    body = hypothesis_record(
        evidence=_evidence(), comparator=_comparator(), provenance={"code_commit": "0" * 40}
    )
    body.update(overrides)
    return body


def test_there_are_exactly_four_criteria() -> None:
    record = _record()

    assert len(record["falsification_criteria"]["criteria"]) == 4
    assert list(CRITERIA) == [row["id"] for row in record["falsification_criteria"]["criteria"]]


def test_the_aggregate_rule_is_all_four_and_is_stated_in_advance() -> None:
    assert AGGREGATE_RULE["expression"] == "C1 AND C2 AND C3 AND C4"
    assert AGGREGATE_RULE["stated_before_the_experiment"] is True
    assert AGGREGATE_RULE["partial_labels_permitted"] is False


def test_c1_requires_a_strict_improvement_on_the_comparator() -> None:
    criterion = _record()["falsification_criteria"]["criteria"][0]

    assert criterion["direction"] == "STRICTLY_LESS_THAN"
    assert criterion["threshold"] == 2.253982668222645


def test_c2_compares_absolute_churn_turnover_not_a_share() -> None:
    criterion = _record()["falsification_criteria"]["criteria"][1]

    assert criterion["threshold"] == pytest.approx(2.11411510465003)
    assert "not a share" in criterion["measured_on"]


def test_the_legacy_threshold_is_a_diagnostic_and_not_a_criterion() -> None:
    record = _record()

    assert record["legacy_h1_reference"]["value"] == LEGACY_H1_TURNOVER_REFERENCE
    assert record["legacy_h1_reference"]["is_an_h2_criterion"] is False
    assert record["legacy_h1_reference"]["threshold_moved"] is False
    thresholds = [row["threshold"] for row in record["falsification_criteria"]["criteria"]]
    assert LEGACY_H1_TURNOVER_REFERENCE not in thresholds


def test_h2_makes_no_performance_claim() -> None:
    record = _record()

    assert record["claims"]["performance"].startswith("NONE")
    assert record["secondary_diagnostics"]["performance_winner_defined"] is False


def test_performance_diagnostics_are_declared_but_decide_nothing() -> None:
    diagnostics = _record()["secondary_diagnostics"]

    assert "Sharpe" in diagnostics["performance"]
    assert "decides the H2 verdict" in diagnostics["status"]


def test_no_numeric_retention_target_is_set() -> None:
    expectation = _record()["secondary_diagnostics"]["mechanistic_expectation"]

    assert "No numeric retention target" in expectation


def test_the_displacement_diagnostic_forbids_consulting_forward_returns() -> None:
    displacement = _record()["secondary_diagnostics"]["displacement"]

    assert displacement["forward_returns_may_be_consulted_when_pairing"] is False


def test_the_record_forbids_future_inputs_to_the_selection() -> None:
    record = _record()

    assert "any future return" in record["selection_inputs_forbidden"]
    assert "current frozen COMPOUNDER_V1_2 rank" in record["selection_inputs_permitted"]


def test_h1_is_recorded_as_closed_and_not_reopened() -> None:
    superseded = _record()["supersedes"]

    assert superseded["final_status"] == "H1_MECHANISTICALLY_FALSIFIED"
    assert superseded["reopened"] is False


def test_the_record_disclaims_that_a_buffer_would_have_worked() -> None:
    disclaimed = _record()["why_this_hypothesis_exists"]["what_is_not_claimed"]

    assert "would have worked historically" in disclaimed
    assert "never been run" in disclaimed
    assert "structural, not fitted" in disclaimed


def test_the_reasoning_itself_makes_no_historical_performance_claim() -> None:
    for line in _record()["why_this_hypothesis_exists"]["reasoning"]:
        assert "would have" not in line


def test_the_record_carries_the_evidence_that_cuts_against_it() -> None:
    against = _record()["why_this_hypothesis_exists"]["evidence_against_an_easy_result"]

    assert len(against) >= 3
    assert any("availability changes" in line for line in against)


def test_only_one_run_is_permitted() -> None:
    assert _record()["runs_permitted"] == 1


# --------------------------------------------------------------------------
# Contamination: no historical period may be called a clean holdout
# --------------------------------------------------------------------------


def test_the_frozen_dataset_is_hypothesis_generation_data() -> None:
    status = _record()["historical_evidence_status"]

    assert status["the_frozen_dataset_is"] == "HYPOTHESIS_GENERATION_DATA_FOR_H2"
    assert status["r1_3_used_the_full_historical_period"] is True
    assert set(status["blocks_exposed"]) == {"DEVELOPMENT", "VALIDATION", "HOLDOUT"}


def test_no_clean_holdout_remains_and_none_is_created() -> None:
    status = _record()["historical_evidence_status"]

    assert status["clean_h2_holdout_remains"] is False
    assert status["holdout_reset"] is False


def test_a_historical_run_must_be_called_exploratory() -> None:
    status = _record()["historical_evidence_status"]

    assert status["mandatory_classification_of_a_historical_run"] == HISTORICAL_STATUS
    for forbidden in (
        "INDEPENDENT_CONFIRMATION",
        "CLEAN_HOLDOUT_CONFIRMATION",
        "OUT_OF_SAMPLE_CONFIRMATION",
        "PROSPECTIVE_CONFIRMATION",
    ):
        assert forbidden in status["a_later_historical_run_may_not_provide"]


def test_independent_confirmation_is_recorded_as_required() -> None:
    status = _record()["historical_evidence_status"]

    assert status["CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST"] is True
    assert status["prospective_duration_chosen_here"] is None


# --------------------------------------------------------------------------
# The binder R1.5 must pass
# --------------------------------------------------------------------------


def _spec(record_sha: str = "a" * 64, **overrides: Any) -> dict[str, Any]:
    body = experiment_spec(hypothesis_sha256=record_sha, comparator=_comparator(), bindings={})
    body.update(overrides)
    return body


def _verify(record: dict[str, Any], spec: dict[str, Any], **overrides: Any) -> list[str]:
    arguments: dict[str, Any] = {
        "record_sha256": "a" * 64,
        "spec_sha256": "b" * 64,
        "expect_record_sha256": "a" * 64,
        "expect_spec_sha256": "b" * 64,
        "executor_rules": RULE,
    }
    arguments.update(overrides)
    return verify_h2(record, spec, **arguments)


def test_a_matching_record_and_specification_pass_the_binder() -> None:
    checks = _verify(_record(), _spec())

    assert len(checks) == 9


def test_a_record_that_moved_is_refused() -> None:
    with pytest.raises(H2BindingError, match="not the frozen one"):
        _verify(_record(), _spec(), record_sha256="c" * 64)


def test_a_specification_that_moved_is_refused() -> None:
    with pytest.raises(H2BindingError, match="not the frozen one"):
        _verify(_record(), _spec(), spec_sha256="c" * 64)


def test_a_specification_bound_to_a_different_record_is_refused() -> None:
    with pytest.raises(H2BindingError, match="different H2 record"):
        _verify(_record(), _spec(record_sha="d" * 64))


def test_an_already_executed_specification_is_refused() -> None:
    with pytest.raises(H2BindingError, match="already been executed"):
        _verify(_record(), _spec(executed=True))


def test_a_specification_permitting_two_runs_is_refused() -> None:
    with pytest.raises(H2BindingError, match="exactly one run"):
        _verify(_record(), _spec(runs_permitted=2))


@pytest.mark.parametrize("field", SINGULAR_FIELDS)
def test_more_than_one_of_anything_is_refused(field: str) -> None:
    swept: dict[str, Any] = {field: 2}

    with pytest.raises(H2BindingError, match="exactly one parameterisation"):
        _verify(_record(), _spec(**swept))


def test_a_parameter_sweep_is_refused() -> None:
    with pytest.raises(H2BindingError, match="sweep is not H2"):
        _verify(_record(), _spec(parameter_sweeps=["retention band"]))


def test_an_optimisation_is_refused() -> None:
    with pytest.raises(H2BindingError, match="not an optimisation"):
        _verify(_record(), _spec(optimization_performed="GRID"))


def test_a_stored_rule_that_drifted_is_refused() -> None:
    drifted = dict(RULE)
    drifted["retention"] = "top 15%"

    with pytest.raises(H2BindingError, match="stored rule drifted"):
        _verify(_record(), _spec(rules=drifted))


def test_an_executor_applying_different_rules_is_refused() -> None:
    drifted = dict(RULE)
    drifted["rebalance"] = "calendar month ends only"

    with pytest.raises(H2BindingError, match="executor would apply different rules"):
        _verify(_record(), _spec(), executor_rules=drifted)


def test_a_different_rule_version_is_refused() -> None:
    with pytest.raises(H2BindingError, match="rule version is not"):
        _verify(_record(), _spec(rule_version="TWO_DECILE_HYSTERESIS_V1"))


def test_a_retention_band_that_is_not_two_widths_is_refused() -> None:
    with pytest.raises(H2BindingError, match="not two selection widths"):
        _verify(_record(retention_widths=3), _spec())


def test_criteria_that_changed_are_refused() -> None:
    with pytest.raises(H2BindingError, match="not the four frozen ones"):
        _verify(_record(), _spec(criteria=["C1_TURNOVER_DIRECTION"]))


def test_an_aggregate_rule_that_changed_is_refused() -> None:
    loosened = dict(AGGREGATE_RULE)
    loosened["expression"] = "C1 OR C2"

    with pytest.raises(H2BindingError, match="aggregate rule drifted"):
        _verify(_record(), _spec(aggregate_rule=loosened))


def test_promoting_the_legacy_threshold_to_a_criterion_is_refused() -> None:
    with pytest.raises(H2BindingError, match="never an H2 criterion"):
        _verify(
            _record(),
            _spec(legacy_h1_turnover_reference={"value": 1.95, "is_a_criterion": True}),
        )


def test_moving_the_legacy_threshold_is_refused() -> None:
    with pytest.raises(H2BindingError, match="legacy H1 reference was moved"):
        _verify(
            _record(),
            _spec(legacy_h1_turnover_reference={"value": 2.1, "is_a_criterion": False}),
        )


def test_a_different_comparator_is_refused() -> None:
    with pytest.raises(H2BindingError, match="comparator is not"):
        _verify(_record(), _spec(comparator={"strategy_version": "COMPOUNDER_TOP_DECILE_V1"}))


def test_calling_a_historical_run_out_of_sample_is_refused() -> None:
    with pytest.raises(H2BindingError, match="must be classified"):
        _verify(_record(), _spec(result_classification="OUT_OF_SAMPLE_CONFIRMATION"))


def test_claiming_a_clean_holdout_remains_is_refused() -> None:
    status = dict(_record()["historical_evidence_status"])
    status["clean_h2_holdout_remains"] = True

    with pytest.raises(H2BindingError, match="no clean H2 holdout exists"):
        _verify(_record(), _spec(historical_evidence_status=status))


def test_resetting_the_holdout_is_refused() -> None:
    status = dict(_record()["historical_evidence_status"])
    status["holdout_reset"] = True

    with pytest.raises(H2BindingError, match="may not be relabelled"):
        _verify(_record(), _spec(historical_evidence_status=status))


def test_dropping_the_confirmation_requirement_is_refused() -> None:
    status = dict(_record()["historical_evidence_status"])
    status["CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST"] = False

    with pytest.raises(H2BindingError, match="confirmation is required"):
        _verify(_record(), _spec(historical_evidence_status=status))


def test_a_record_that_is_not_h2_is_refused() -> None:
    with pytest.raises(H2BindingError, match="not H2_MEMBERSHIP_STABILITY"):
        _verify(_record(hypothesis_id="H3_SOMETHING"), _spec())


def test_a_specification_testing_another_hypothesis_is_refused() -> None:
    with pytest.raises(H2BindingError, match="does not test"):
        _verify(_record(), _spec(tests_hypothesis="H1_MEDIUM_HORIZON_SELECTION"))


def test_the_binder_has_no_force_parameter() -> None:
    import inspect

    assert "force" not in inspect.signature(verify_h2).parameters


# --------------------------------------------------------------------------
# End to end, against the frozen record, computing nothing
# --------------------------------------------------------------------------


@needs_frozen
def test_the_phase_writes_both_documents_and_no_performance(tmp_path: Path) -> None:
    result = preregister_h2(
        tmp_path / "out",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        FROZEN / "r1_3",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    complete = json.loads((tmp_path / "out" / "r1_4_complete.json").read_text(encoding="utf-8"))

    assert result.record["hypothesis_id"] == H2_ID
    assert result.spec["status"] == SPEC_STATUS
    assert complete["strategies_simulated"] == 0
    assert complete["return_calculations"] == 0
    assert complete["turnover_calculations"] == 0
    assert complete["simulator_reachable_from_this_package"] == []


@needs_frozen
def test_the_written_documents_pass_their_own_binder(tmp_path: Path) -> None:
    result = preregister_h2(
        tmp_path / "out",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        FROZEN / "r1_3",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    record_sha = result.documents["h2_membership_stability.json"]
    spec_sha = result.documents["r1_5_experiment_spec.json"]

    checks = verify_h2(
        result.record,
        result.spec,
        record_sha256=record_sha,
        spec_sha256=spec_sha,
        expect_record_sha256=record_sha,
        expect_spec_sha256=spec_sha,
        executor_rules=RULE,
    )

    assert len(checks) == 9


@needs_frozen
def test_two_runs_agree_on_every_fingerprint(tmp_path: Path) -> None:
    runs = [
        preregister_h2(
            tmp_path / f"run{index}",
            FROZEN / "r1_1",
            FROZEN / "r1_2",
            FROZEN / "r1_3",
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
            code_commit="0" * 40,
        )
        for index in range(2)
    ]

    assert runs[0].fingerprints == runs[1].fingerprints
    assert len(runs[0].fingerprints) == 7


@needs_frozen
def test_a_contract_that_moved_refuses_to_preregister(tmp_path: Path) -> None:
    with pytest.raises(H2BindingError, match="contract is"):
        preregister_h2(
            tmp_path / "out",
            FROZEN / "r1_1",
            FROZEN / "r1_2",
            FROZEN / "r1_3",
            contract_sha256="0" * 64,
            expect_contract_sha256=CONTRACT_SHA,
            code_commit="0" * 40,
        )


@needs_frozen
def test_the_ledger_records_a_preregistration_not_an_experiment(tmp_path: Path) -> None:
    preregister_h2(
        tmp_path / "out",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        FROZEN / "r1_3",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    entry = json.loads(
        (tmp_path / "out" / "research_ledger.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )

    assert entry["mode"] == "HYPOTHESIS_PREREGISTRATION"
    assert entry["variants_tried"] == 0
    assert entry["parameter_sweeps"] == []
    assert entry["holdout_viewed"] is False
    assert entry["outcome"]["new_return_calculations"] == 0
    assert entry["outcome"]["new_holdout_evaluations"] == 0
    assert entry["outcome"]["turnover_calculated"] is False


@needs_frozen
def test_the_frozen_record_is_not_mutated(tmp_path: Path) -> None:
    from stock_screener.signal_review.binding import file_sha256

    before = {path.name: file_sha256(path) for path in sorted((FROZEN / "r1_3").glob("*.json"))}

    preregister_h2(
        tmp_path / "out",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        FROZEN / "r1_3",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )

    after = {path.name: file_sha256(path) for path in sorted((FROZEN / "r1_3").glob("*.json"))}
    assert after == before
