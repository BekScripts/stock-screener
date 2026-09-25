"""Adversarial tests for the R1.5 H2 execution.

R1.5 spends a preregistration, and a preregistration is only worth something if
the execution cannot quietly depart from it. Each test names a departure — a
moved record, a drifted rule, a second band, a second run, a target count
rebuilt slightly differently, a criterion softened, performance leaking into the
verdict, a historical result relabelled as confirmation — and asserts the code
refuses it or records it.

The selection rule is exercised on synthetic cross-sections; the end-to-end
behaviour against the frozen record is checked where that record exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stock_screener.backtest_harness import CrossSectionRow
from stock_screener.hysteresis_experiment import (
    EXECUTION_MARKER,
    STRATEGY,
    ExecutionLock,
    SelectionError,
    build_decisions,
    evaluate,
    interpret,
    ordinary_top_decile,
)
from stock_screener.hysteresis_experiment import binding as binding_module
from stock_screener.membership_hypothesis import (
    AGGREGATE_RULE,
    COMPARATOR,
    H2_ID,
    LEGACY_H1_TURNOVER_REFERENCE,
    RULE,
    RULE_VERSION,
    H2BindingError,
)
from stock_screener.turnover_forensics import ForensicBindingError

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FROZEN = REPO / ".evidence" / "research"
CONTRACT_SHA = "f7efb001cf79e40680758a3bd29ec2b016ad3d9d5199f363d7ee4f89eb1ad7d1"
H2_SHA = "56f2e51fb74e0eec2a3296431bca80a5ca2dea29729d9568b540c00f690e34ec"
SPEC_SHA = "6d2664485bffab32260b940f6d532a451139cdb47c5a11b1bec7698040ebb14a"

COMPARATOR_TURNOVER = 2.253982668222645
COMPARATOR_CHURN = 2.11411510465003

needs_frozen = pytest.mark.skipif(
    not (FROZEN / "r1_5" / "r1_5_complete.json").is_file(),
    reason="the frozen R1.5 record is runtime data, not committed",
)


def _Row(  # noqa: N802 - a row factory, named for what it builds
    identity_id: int, score: float, *, kind: str = "COMPANY"
) -> CrossSectionRow:
    """One executable cross-section row, carrying only what selection may see."""
    return CrossSectionRow(
        kind=kind,
        identity_id=identity_id,
        symbol=f"T{identity_id}",
        month="2022-03",
        decision="2022-03-31",
        execution="2022-04-01",
        universe_source="A3_EVIDENCE",
        evidence_date="2022-02-02",
        tradability="TRADABLE",
        score_status="SCORED",
        final_score=score,
        freshness="CURRENT",
        rank_eligible=True,
        execution_price=10.0,
        adv_dollar=1_000_000.0,
        stage="EXECUTABLE",
        exclusion=None,
        classification_is_current=True,
    )


def _pool(size: int) -> list[CrossSectionRow]:
    return [_Row(index, 100.0 - index) for index in range(size)]


def _calendar(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "decision": f"2022-0{3 + index}-31",
            "execution": f"2022-0{4 + index}-01",
            "month": f"2022-0{3 + index}",
            "quarter": f"2022Q{index + 1}",
            "split": "DEVELOPMENT",
        }
        for index in range(count)
    ]


# --------------------------------------------------------------------------
# The rule as executed
# --------------------------------------------------------------------------


def test_the_first_selection_is_the_ordinary_top_decile() -> None:
    calendar = _calendar(1)
    pool = _pool(100)
    ordinary = ordinary_top_decile(pool, 10)

    decisions = build_decisions(
        calendar, {calendar[0]["decision"]: pool}, {}, {calendar[0]["decision"]: 10}, ordinary
    )

    assert set(decisions[0].selected) == set(ordinary)
    assert decisions[0].outcome.retained_in_band == ()


def test_a_first_selection_that_differs_from_the_comparator_stops_the_run() -> None:
    calendar = _calendar(1)
    pool = _pool(100)

    with pytest.raises(SelectionError, match="does not reproduce the comparator"):
        build_decisions(
            calendar,
            {calendar[0]["decision"]: pool},
            {},
            {calendar[0]["decision"]: 10},
            [("COMPANY", 99)],
        )


def test_a_missing_target_count_stops_the_run() -> None:
    calendar = _calendar(1)
    pool = _pool(100)

    with pytest.raises(SelectionError, match="no frozen target count"):
        build_decisions(calendar, {calendar[0]["decision"]: pool}, {}, {}, [])


def test_an_incumbent_inside_two_widths_is_retained_on_the_second_decision() -> None:
    calendar = _calendar(2)
    first, second = str(calendar[0]["decision"]), str(calendar[1]["decision"])
    early = _pool(100)
    late = _pool(100)
    late[0] = _Row(0, 100.0 - 15.5)  # the incumbent slips to rank 15 of 100
    pools = {first: early, second: late}

    decisions = build_decisions(
        calendar, pools, {}, {first: 10, second: 10}, ordinary_top_decile(early, 10)
    )

    assert ("COMPANY", 0) in decisions[1].selected
    assert ("COMPANY", 0) in decisions[1].outcome.retained_in_band


def test_an_incumbent_beyond_two_widths_is_dropped() -> None:
    calendar = _calendar(2)
    first, second = str(calendar[0]["decision"]), str(calendar[1]["decision"])
    early = _pool(100)
    late = _pool(100)
    late[0] = _Row(0, 1.0)  # falls to the bottom
    pools = {first: early, second: late}

    decisions = build_decisions(
        calendar, pools, {}, {first: 10, second: 10}, ordinary_top_decile(early, 10)
    )

    assert ("COMPANY", 0) not in decisions[1].selected
    assert ("COMPANY", 0) in decisions[1].outcome.dropped_out_of_band


def test_an_incumbent_absent_from_the_executable_pool_is_forced_out() -> None:
    calendar = _calendar(2)
    first, second = str(calendar[0]["decision"]), str(calendar[1]["decision"])
    early = _pool(100)
    late = [row for row in _pool(100) if row.identity_id != 0]
    universe = {second: {("COMPANY", 0): {"exclusion": "NOT_TRADABLE_UNVERIFIED"}}}

    decisions = build_decisions(
        calendar,
        {first: early, second: late},
        universe,
        {first: 10, second: 10},
        ordinary_top_decile(early, 10),
    )

    assert ("COMPANY", 0) in decisions[1].outcome.forced_out
    assert decisions[1].forced_reasons[("COMPANY", 0)] == "TRADABILITY"
    assert ("COMPANY", 0) not in decisions[1].selected


def test_the_portfolio_holds_the_frozen_target_count_every_decision() -> None:
    calendar = _calendar(2)
    first, second = str(calendar[0]["decision"]), str(calendar[1]["decision"])
    early = _pool(100)

    decisions = build_decisions(
        calendar,
        {first: early, second: _pool(100)},
        {},
        {first: 10, second: 10},
        ordinary_top_decile(early, 10),
    )

    assert all(len(entry.selected) == entry.target_count for entry in decisions)


def test_vacancies_fill_from_the_highest_ranked_unheld_names() -> None:
    calendar = _calendar(2)
    first, second = str(calendar[0]["decision"]), str(calendar[1]["decision"])
    early = _pool(100)
    late = _pool(100)
    late[0] = _Row(0, 1.0)
    decisions = build_decisions(
        calendar,
        {first: early, second: late},
        {},
        {first: 10, second: 10},
        ordinary_top_decile(early, 10),
    )

    assert decisions[1].outcome.admitted == (("COMPANY", 10),)


def test_displacement_pairs_are_recorded_without_reading_a_label() -> None:
    calendar = _calendar(2)
    first, second = str(calendar[0]["decision"]), str(calendar[1]["decision"])
    early = _pool(100)
    late = _pool(100)
    late[0] = _Row(0, 100.0 - 15.5)
    decisions = build_decisions(
        calendar,
        {first: early, second: late},
        {},
        {first: 10, second: 10},
        ordinary_top_decile(early, 10),
    )
    pair = decisions[1].displacement[0]

    assert pair["retained_identity_id"] == 0
    assert pair["rank_difference"] is not None
    assert "no forward return" in pair["paired_on"]
    assert "return" not in {key.split("_")[-1] for key in pair}


def test_the_selection_modules_read_no_forward_label() -> None:
    source = (REPO / "src" / "stock_screener" / "hysteresis_experiment" / "selection.py").read_text(
        encoding="utf-8"
    )

    assert "load_labels" not in source
    assert "forward_return" not in source


def test_a_decision_reports_its_overlap_with_the_ordinary_rule() -> None:
    calendar = _calendar(1)
    pool = _pool(100)
    decisions = build_decisions(
        calendar,
        {calendar[0]["decision"]: pool},
        {},
        {calendar[0]["decision"]: 10},
        ordinary_top_decile(pool, 10),
    )

    assert decisions[0].jaccard == pytest.approx(1.0)
    assert decisions[0].as_json()["common_identities"] == 10


# --------------------------------------------------------------------------
# The criteria, and their independence from performance
# --------------------------------------------------------------------------


def _comparator() -> dict[str, Any]:
    return {
        "strategy_version": COMPARATOR,
        "annual_turnover": COMPARATOR_TURNOVER,
        "ranking_churn_annual_turnover": COMPARATOR_CHURN,
        "quarterly_retention": 0.457707,
    }


def _evaluate(**overrides: Any) -> list[dict[str, Any]]:
    arguments: dict[str, Any] = {
        "comparator": _comparator(),
        "annual_turnover": 1.6177403954997098,
        "churn_turnover": 1.411598539883737,
        "target_counts_match": True,
        "target_count_mismatches": [],
        "intentional_cash_quarters": 0,
        "exposure": {"hysteresis_caused_exposure_reduction": False},
        "accounting_violations": [],
        "identity_violations": [],
        "leakage_violations": [],
        "execution_violations": [],
    }
    arguments.update(overrides)
    return list(evaluate(**arguments))


def test_the_evaluation_takes_no_performance_argument() -> None:
    import inspect

    parameters = set(inspect.signature(evaluate).parameters)

    for forbidden in ("return", "sharpe", "performance", "nav", "benchmark", "drawdown"):
        assert not any(forbidden in name for name in parameters), forbidden


def test_all_four_met_is_supported() -> None:
    verdict = interpret(_evaluate())

    assert verdict["outcome"] == "SUPPORTED"
    assert verdict["criteria_failed"] == []
    assert verdict["performance_entered_this_verdict"] is False


def test_c1_is_a_strict_comparison() -> None:
    criteria = _evaluate(annual_turnover=COMPARATOR_TURNOVER)

    assert criteria[0]["met"] is False


def test_c1_fails_when_turnover_rises() -> None:
    assert _evaluate(annual_turnover=2.4)[0]["met"] is False


def test_c2_compares_the_absolute_churn_quantity() -> None:
    criteria = _evaluate(churn_turnover=COMPARATOR_CHURN + 0.001)

    assert criteria[1]["met"] is False
    assert "not a share" in criteria[1]["detail"]["compared_as"]


def test_c2_can_fail_while_c1_passes() -> None:
    criteria = _evaluate(annual_turnover=1.0, churn_turnover=2.5)

    assert criteria[0]["met"] is True
    assert criteria[1]["met"] is False
    assert interpret(criteria)["outcome"] == "NOT_SUPPORTED"


def test_c3_fails_on_a_target_count_mismatch() -> None:
    criteria = _evaluate(target_counts_match=False, target_count_mismatches=["2023-01-03"])

    assert criteria[2]["met"] is False


def test_c3_fails_on_an_intentional_cash_quarter() -> None:
    assert _evaluate(intentional_cash_quarters=1)[2]["met"] is False


def test_c3_fails_when_hysteresis_shrank_the_portfolio() -> None:
    criteria = _evaluate(exposure={"hysteresis_caused_exposure_reduction": True})

    assert criteria[2]["met"] is False


@pytest.mark.parametrize(
    "field",
    ["accounting_violations", "identity_violations", "leakage_violations", "execution_violations"],
)
def test_c4_fails_on_any_single_violation(field: str) -> None:
    criteria = _evaluate(**{field: ["something happened"]})

    assert criteria[3]["met"] is False
    assert interpret(criteria)["outcome"] == "NOT_SUPPORTED"


@pytest.mark.parametrize("failing", [0, 1, 2, 3])
def test_any_one_failure_makes_the_aggregate_not_supported(failing: int) -> None:
    criteria = _evaluate()
    criteria[failing]["met"] = False

    assert interpret(criteria)["outcome"] == "NOT_SUPPORTED"


def test_three_of_four_is_not_supported() -> None:
    criteria = _evaluate()
    criteria[1]["met"] = False

    verdict = interpret(criteria)

    assert verdict["outcome"] == "NOT_SUPPORTED"
    assert verdict["outcome"] != "PARTIALLY_SUPPORTED"
    assert verdict["partial_label_used"] is False


def test_the_aggregate_rule_is_the_frozen_one() -> None:
    verdict = interpret(_evaluate())

    assert verdict["aggregate_rule"] == dict(AGGREGATE_RULE)
    assert verdict["aggregate_rule"]["expression"] == "C1 AND C2 AND C3 AND C4"


def test_a_criterion_set_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(ValueError, match="four criteria"):
        interpret(_evaluate()[:3])


def test_the_legacy_threshold_never_enters_the_verdict() -> None:
    verdict = interpret(_evaluate())

    assert verdict["legacy_h1_turnover_reference"]["value"] == LEGACY_H1_TURNOVER_REFERENCE
    assert verdict["legacy_h1_turnover_reference"]["is_an_h2_criterion"] is False
    assert verdict["legacy_h1_turnover_reference"]["used_in_this_verdict"] is False
    assert LEGACY_H1_TURNOVER_REFERENCE not in [row["threshold"] for row in _evaluate()]


def test_a_turnover_below_the_legacy_threshold_does_not_change_the_rule() -> None:
    below = interpret(_evaluate(annual_turnover=1.0))
    above = interpret(_evaluate(annual_turnover=2.0))

    assert below["outcome"] == "SUPPORTED"
    assert above["outcome"] == "SUPPORTED"


def test_the_verdict_is_classified_exploratory_and_confirms_nothing() -> None:
    verdict = interpret(_evaluate())

    assert verdict["historical_test_classification"] == "EXPLORATORY_HISTORICAL_TEST"
    assert verdict["independent_confirmation"] == "NOT_PROVIDED_BY_R1_5"
    assert "not confirmation" in verdict["language"]


# --------------------------------------------------------------------------
# The execution lock
# --------------------------------------------------------------------------


def test_a_specification_can_be_claimed_once(tmp_path: Path) -> None:
    lock = ExecutionLock(tmp_path / EXECUTION_MARKER)

    state = lock.claim(spec_sha256=SPEC_SHA, code_commit="0" * 40)

    assert state["status"] == "EXECUTED"
    assert state["runs_permitted"] == 1


def test_a_second_claim_is_refused(tmp_path: Path) -> None:
    lock = ExecutionLock(tmp_path / EXECUTION_MARKER)
    lock.claim(spec_sha256=SPEC_SHA, code_commit="0" * 40)

    with pytest.raises(H2BindingError, match="already executed"):
        lock.claim(spec_sha256=SPEC_SHA, code_commit="0" * 40)


def test_releasing_the_lock_allows_a_retry_after_a_failure(tmp_path: Path) -> None:
    lock = ExecutionLock(tmp_path / EXECUTION_MARKER)
    lock.claim(spec_sha256=SPEC_SHA, code_commit="0" * 40)

    lock.release()

    assert lock.claim(spec_sha256=SPEC_SHA, code_commit="0" * 40)["runs_executed"] == 1


def test_the_lock_does_not_rewrite_the_preregistered_specification(tmp_path: Path) -> None:
    lock = ExecutionLock(tmp_path / EXECUTION_MARKER)

    state = lock.claim(spec_sha256=SPEC_SHA, code_commit="0" * 40)

    assert "results" not in state
    assert "not rewritten" in state["note"]


# --------------------------------------------------------------------------
# Binding, against the frozen record
# --------------------------------------------------------------------------


def _bind(**overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "contract_sha256": CONTRACT_SHA,
        "expect_contract_sha256": CONTRACT_SHA,
        "expect_h2_record_sha256": H2_SHA,
        "expect_spec_sha256": SPEC_SHA,
    }
    arguments.update(overrides)
    return binding_module.bind_r1_5(
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        FROZEN / "r1_3",
        FROZEN / "r1_4",
        **arguments,
    )


@needs_frozen
def test_the_frozen_preregistration_binds() -> None:
    binding = _bind()

    assert binding.h2_record_sha256 == H2_SHA
    assert binding.spec_sha256 == SPEC_SHA
    assert binding.as_json()["executable_variants"] == 1
    assert binding.rules == dict(RULE)


@needs_frozen
def test_a_wrong_h2_record_digest_refuses_to_bind() -> None:
    with pytest.raises(H2BindingError, match="not the frozen one"):
        _bind(expect_h2_record_sha256="0" * 64)


@needs_frozen
def test_a_wrong_spec_digest_refuses_to_bind() -> None:
    with pytest.raises(H2BindingError, match="not the frozen one"):
        _bind(expect_spec_sha256="0" * 64)


@needs_frozen
def test_a_wrong_contract_refuses_to_bind() -> None:
    # The upstream binder catches this first and raises its own type. Both fail
    # closed, and which layer notices is not the point.
    with pytest.raises((H2BindingError, ForensicBindingError), match="contract is"):
        _bind(contract_sha256="0" * 64)


@needs_frozen
def test_the_binding_carries_the_comparator_and_verifies_its_turnover() -> None:
    binding = _bind()

    assert binding.comparator["strategy_version"] == COMPARATOR
    assert binding.comparator["annual_turnover"] == pytest.approx(COMPARATOR_TURNOVER, abs=1e-15)


# --------------------------------------------------------------------------
# The executed record
# --------------------------------------------------------------------------


@needs_frozen
def test_the_executed_record_reports_one_variant_and_no_sweep() -> None:
    complete = json.loads((FROZEN / "r1_5" / "r1_5_complete.json").read_text(encoding="utf-8"))
    entry = json.loads(
        (FROZEN / "r1_5" / "research_ledger.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )

    assert complete["variants_executed"] == 1
    assert complete["rule_version"] == RULE_VERSION
    assert entry["variants_tried"] == 1
    assert entry["parameter_sweeps"] == []
    assert entry["outcome"]["alternative_hysteresis_widths"] == 0
    assert entry["outcome"]["alternative_frequencies"] == 0
    assert entry["outcome"]["clean_holdout_evaluations"] == 0
    assert entry["outcome"]["historical_confirmation_claims"] == 0


@needs_frozen
def test_the_executed_record_never_claims_confirmation() -> None:
    body = json.loads((FROZEN / "r1_5" / "r1_5_h2_evaluation.json").read_text(encoding="utf-8"))

    assert body["historical_test_classification"] == "EXPLORATORY_HISTORICAL_TEST"
    assert body["independent_confirmation"] == "NOT_PROVIDED_BY_R1_5"
    assert body["clean_holdout_claimed"] is False
    assert body["criteria_changed_after_results"] is False


@needs_frozen
def test_the_legacy_holdout_block_stays_contaminated() -> None:
    body = json.loads((FROZEN / "r1_5" / "r1_5_performance.json").read_text(encoding="utf-8"))
    blocks = body["legacy_blocks"]

    assert blocks["LEGACY_HOLDOUT_BLOCK"]["contamination"] == ("CONTAMINATED_FOR_H2_CONFIRMATION")
    for block in blocks.values():
        assert block["status"] == "DESCRIPTIVE_ONLY_FOR_H2"
    assert body["classification"] == "EXPLORATORY_FULL_HISTORY_RESULT"


@needs_frozen
def test_the_turnover_decomposition_reconciles_exactly() -> None:
    body = json.loads((FROZEN / "r1_5" / "r1_5_turnover.json").read_text(encoding="utf-8"))
    reconciliation = body["h2"]["reconciliation"]

    assert reconciliation["unexplained_turnover"] == pytest.approx(0.0, abs=1e-12)
    assert reconciliation["shares_sum"] == pytest.approx(1.0, abs=1e-12)


@needs_frozen
def test_the_comparator_turnover_rebuilds_to_its_stored_value() -> None:
    body = json.loads((FROZEN / "r1_5" / "r1_5_turnover.json").read_text(encoding="utf-8"))

    assert body["comparator"]["annual_turnover"] == pytest.approx(COMPARATOR_TURNOVER, abs=1e-15)


@needs_frozen
def test_every_decision_used_the_comparators_target_count() -> None:
    ours = [
        json.loads(line)
        for line in (FROZEN / "r1_5" / "r1_5_selection_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    theirs = {
        str(json.loads(line)["decision"]): int(json.loads(line)["selected"])
        for line in (FROZEN / "r1_2" / "quarterly_selection.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    }

    assert len(ours) == 17
    for row in ours:
        assert row["target_count"] == theirs[row["decision"]]
        assert row["selected"] == row["target_count"]


@needs_frozen
def test_the_run_manifest_does_not_relabel_h2_as_production() -> None:
    manifest = json.loads((FROZEN / "r1_5" / "r1_5_run_manifest.json").read_text(encoding="utf-8"))
    parameters = manifest["parameters"]

    assert parameters["production_status"].startswith("NOT_PRODUCTION")
    assert parameters["research_classification"] == "EXPLORATORY_HISTORICAL_TEST"
    assert manifest["strategy_version"] == STRATEGY


@needs_frozen
def test_the_summary_keeps_performance_secondary() -> None:
    summary = json.loads((FROZEN / "r1_5" / "r1_5_summary.json").read_text(encoding="utf-8"))

    assert summary["performance_is_secondary"] is True
    assert summary["turnover"]["legacy_reference_used_in_verdict"] is False
    assert summary["hypothesis_id"] == H2_ID
    assert summary["clean_holdout_remains"] is False


# --------------------------------------------------------------------------
# Computational reproducibility
#
# Re-running the identical frozen computation is what the brief calls for and is
# not a second experiment: same rule, same inputs, same outputs, and the
# preregistration is not claimed again. These assert that the committed record
# reproduces from the code as it stands.
# --------------------------------------------------------------------------


DATABASE = REPO / "stage9k.db"
CONTRACT_DIR = REPO / ".evidence" / "contracts" / "backtest_v1"

needs_database = pytest.mark.skipif(
    not DATABASE.is_file()
    or not (CONTRACT_DIR / "backtest_run_manifest_schema.json").is_file()
    or not (FROZEN / "r1_5" / "r1_5_complete.json").is_file(),
    reason="the frozen database and R1.5 record are runtime data, not committed",
)


def _rerun(target: Path) -> Any:
    from stock_screener.hysteresis_experiment import run_h2

    return run_h2(
        target,
        FROZEN / "r1_2",
        DATABASE,
        _bind(),
        CONTRACT_DIR,
        contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
        claim_execution=False,
    )


@pytest.mark.slow
@needs_database
def test_the_committed_result_reproduces_from_the_code(tmp_path: Path) -> None:
    stored = json.loads((FROZEN / "r1_5" / "r1_5_fingerprints.json").read_text(encoding="utf-8"))[
        "fingerprints"
    ]

    result = _rerun(tmp_path / "again")

    for name in ("selection_ledger", "trade_ledger", "holdings_ledger", "nav", "turnover"):
        assert result.fingerprints[name] == stored[name], name


@pytest.mark.slow
@needs_database
def test_the_rerun_reaches_the_same_verdict_and_writes_no_lock(tmp_path: Path) -> None:
    result = _rerun(tmp_path / "again")

    assert result.evaluation["interpretation"]["outcome"] == "SUPPORTED"
    assert result.violations == []
    assert result.manifest_problems == []
    assert not (tmp_path / "again" / EXECUTION_MARKER).exists()


@pytest.mark.slow
@needs_database
def test_the_rerun_holds_the_comparators_target_count_every_quarter(tmp_path: Path) -> None:
    result = _rerun(tmp_path / "again")

    assert len(result.decisions) == 17
    for entry in result.decisions:
        assert len(entry.selected) == entry.target_count
