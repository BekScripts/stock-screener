"""Adversarial tests for the R1.1 signal review.

R1.1 interprets a record it must not be able to change, and proposes an
experiment it must not be able to run. Each test names a way that could go
wrong — a moved R1.0 artifact accepted anyway, a strategy quietly executed, a
frequency quietly searched, a specification written without a hypothesis behind
it, an already-observed interval described as untouched — and asserts the code
refuses or records it.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from stock_screener.backtest_harness import PRIMARY_HORIZON, CrossSectionRow
from stock_screener.signal_review import (
    CLASSIFICATIONS,
    HOLDOUT_STATUS,
    RUBRIC,
    IdentityPnL,
    ReviewBindingError,
    bind_r1_0,
    classify_signal,
    concentration,
    cost_break_even,
    evaluate_h1,
    experiment_spec,
    holdout_status,
    hypothesis_record,
    identity_contributions,
    leave_one_out,
    monthly_signal,
    rank_persistence,
    subset_diagnostics,
    turnover_sources,
)
from stock_screener.signal_review import review as review_module
from stock_screener.signal_review import run as run_module
from stock_screener.signal_review import signal as signal_module
from stock_screener.signal_review.binding import ledger_fingerprint
from stock_screener.signal_review.review import R1_2_STRATEGY

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------------
# A tiny frozen record
# ----------------------------------------------------------------------------


def _row(
    identity_id: int,
    month: str,
    score: float,
    *,
    kind: str = "COMPANY",
    stage: str = "EXECUTABLE",
    adv: float | None = 1_000_000.0,
) -> CrossSectionRow:
    return CrossSectionRow(
        kind=kind,
        identity_id=identity_id,
        symbol=f"S{identity_id}",
        month=month,
        decision=f"{month}-28",
        execution=f"{month}-28",
        universe_source="FROZEN_VERDICT",
        evidence_date=None,
        tradability="TRADABLE",
        score_status="SCORED",
        final_score=score,
        freshness="CURRENT",
        rank_eligible=True,
        execution_price=10.0,
        adv_dollar=adv,
        stage=stage,
        exclusion=None,
        classification_is_current=kind == "COMPANY",
    )


MONTHS = ("2024-01", "2024-02", "2024-03", "2024-04")


def _world(slope: float = 1.0) -> tuple[list[CrossSectionRow], dict[Any, Any], dict[str, str]]:
    """A cross-section whose outcome rises with the score by `slope`."""
    rows = [_row(i, month, float(i)) for month in MONTHS for i in range(1, 101)]
    labels = {
        (f"{month}-28", "COMPANY", i, PRIMARY_HORIZON): ("REALISED", slope * i / 1000.0)
        for month in MONTHS
        for i in range(1, 101)
    }
    split_of = dict.fromkeys(MONTHS[:2], "DEVELOPMENT")
    split_of[MONTHS[2]] = "VALIDATION"
    split_of[MONTHS[3]] = "HOLDOUT"
    return rows, labels, split_of


class TestTheBaselineBinding:
    def _record(self, tmp_path: Path) -> Path:
        directory = tmp_path / "r1_0"
        directory.mkdir()
        rows = [{"a": 1}, {"a": 2}]
        (directory / "cross_sections.jsonl").write_text("", encoding="utf-8")
        documents = {
            "research_split_v1.json": {"version": "RESEARCH_SPLIT_V1", "decision_dates": []},
            "baseline_registry.json": {"x": 1},
            "r1_0_preregistration.json": {"x": 2},
            "monthly_coverage.json": {"x": 3},
            "signal_diagnostics_compounder_v1.json": {"x": 4},
            "spot_audit.json": {"x": 5},
            "bias_audit.json": {"x": 6},
            "r1_0_summary.json": {"x": 7},
        }
        fingerprints: dict[str, str] = {}
        import hashlib

        for name, body in documents.items():
            text = json.dumps(body, indent=2, sort_keys=True) + "\n"
            (directory / name).write_text(text, encoding="utf-8")
        from stock_screener.signal_review.binding import (
            DOCUMENT_FINGERPRINTS,
            LEDGER_FINGERPRINTS,
            cross_section_fingerprint,
            file_sha256,
        )

        for key, name in DOCUMENT_FINGERPRINTS.items():
            fingerprints[key] = file_sha256(directory / name)
        for key, name in LEDGER_FINGERPRINTS.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
            )
            fingerprints[key] = ledger_fingerprint(rows)
        fingerprints["cross_sections"] = cross_section_fingerprint([])
        marker = {
            "stage": "R1_0_COMPLETE",
            "dataset_id": "PIT_RESEARCH_DATASET_V2",
            "contract_version": "BACKTEST_DATA_CONTRACT_V1",
            "result_fingerprints": fingerprints,
            "contract_bindings": {},
        }
        (directory / "r1_0_complete.json").write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (directory / "r1_0_fingerprints.json").write_text(
            json.dumps({"fingerprints": fingerprints}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert hashlib  # keep the import meaningful for readers
        return directory

    def _shas(self, directory: Path) -> tuple[str, str, str]:
        from stock_screener.signal_review.binding import file_sha256

        return (
            file_sha256(directory / "r1_0_complete.json"),
            file_sha256(directory / "research_split_v1.json"),
            file_sha256(directory / "r1_0_preregistration.json"),
        )

    def test_an_intact_record_binds(self, tmp_path: Path) -> None:
        directory = self._record(tmp_path)
        marker, split, prereg = self._shas(directory)

        binding = bind_r1_0(
            directory,
            expect_marker_sha=marker,
            expect_split_sha=split,
            expect_preregistration_sha=prereg,
        )

        assert binding.dataset_id == "PIT_RESEARCH_DATASET_V2"
        assert "cross_sections" in binding.verified
        assert len(binding.verified) >= 18

    def test_an_edited_document_is_refused(self, tmp_path: Path) -> None:
        directory = self._record(tmp_path)
        marker, split, prereg = self._shas(directory)
        (directory / "bias_audit.json").write_text("{}\n", encoding="utf-8")

        with pytest.raises(ReviewBindingError, match="bias_audit"):
            bind_r1_0(
                directory,
                expect_marker_sha=marker,
                expect_split_sha=split,
                expect_preregistration_sha=prereg,
            )

    def test_an_edited_ledger_is_refused(self, tmp_path: Path) -> None:
        directory = self._record(tmp_path)
        marker, split, prereg = self._shas(directory)
        path = directory / "run_COMPOUNDER_TOP_DECILE_V1" / "trades.jsonl"
        path.write_text('{"a": 99}\n', encoding="utf-8")

        with pytest.raises(ReviewBindingError, match="re-derives"):
            bind_r1_0(
                directory,
                expect_marker_sha=marker,
                expect_split_sha=split,
                expect_preregistration_sha=prereg,
            )

    def test_a_marker_that_moved_is_refused(self, tmp_path: Path) -> None:
        directory = self._record(tmp_path)
        _, split, prereg = self._shas(directory)

        with pytest.raises(ReviewBindingError, match="completion marker"):
            bind_r1_0(
                directory,
                expect_marker_sha="00" * 32,
                expect_split_sha=split,
                expect_preregistration_sha=prereg,
            )

    def test_a_marker_and_fingerprint_file_that_disagree_are_refused(self, tmp_path: Path) -> None:
        directory = self._record(tmp_path)
        (directory / "r1_0_fingerprints.json").write_text(
            json.dumps({"fingerprints": {"x": "y"}}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker, split, prereg = self._shas(directory)

        with pytest.raises(ReviewBindingError, match="disagree"):
            bind_r1_0(
                directory,
                expect_marker_sha=marker,
                expect_split_sha=split,
                expect_preregistration_sha=prereg,
            )


class TestHoldoutContamination:
    def test_the_status_says_the_interval_is_already_observed(self) -> None:
        status = holdout_status()

        assert status["R1_0_HOLDOUT_STATUS"] == "VALID_FOR_R1_0_PREREGISTERED_STRATEGIES"
        assert status["NEW_DERIVED_HYPOTHESES"] == "HOLDOUT_ALREADY_OBSERVED"
        assert status["valid_untouched_holdout_for_new_hypotheses"] == (
            "NONE EXISTS IN THE FROZEN DATASET"
        )
        assert (
            status["policy_for_r1_2"]["r1_0_holdout"]
            == "REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION"
        )

    def test_the_constant_and_the_document_agree(self) -> None:
        assert holdout_status()["R1_0_HOLDOUT_STATUS"] == HOLDOUT_STATUS["R1_0_HOLDOUT_STATUS"]


class TestSignalDiagnostics:
    def test_a_rising_relationship_gives_a_positive_contrast_in_every_split(self) -> None:
        rows, labels, split_of = _world()

        monthly = monthly_signal(rows, labels, split_of)
        splits = subset_diagnostics(monthly, key="split")

        assert len(monthly) == len(MONTHS)
        for block in ("DEVELOPMENT", "VALIDATION", "HOLDOUT"):
            assert splits[block]["d10_minus_d1"]["mean"] > 0
            assert splits[block]["rank_ic"]["mean"] == pytest.approx(1.0)

    def test_the_long_only_contrast_is_reported_beside_the_long_short_one(self) -> None:
        rows, labels, split_of = _world()

        splits = subset_diagnostics(monthly_signal(rows, labels, split_of), key="split")

        overall = splits["ALL"]
        assert overall["d10_minus_cross_section_mean_pooled"] is not None
        assert overall["d10_minus_cross_section_mean_pooled"] < overall["d10_minus_d1_pooled"]
        assert overall["decile_profile"]["10"] > overall["decile_profile"]["1"]
        assert overall["shape"]["classification"] in {"RISING", "U_SHAPED", "FLAT"}

    def test_a_u_shaped_profile_is_named_as_such(self) -> None:
        rows = [_row(i, MONTHS[0], float(i)) for i in range(1, 101)]
        labels = {
            (f"{MONTHS[0]}-28", "COMPANY", i, PRIMARY_HORIZON): (
                "REALISED",
                0.02 if i <= 20 or i > 80 else 0.0,
            )
            for i in range(1, 101)
        }

        splits = subset_diagnostics(
            monthly_signal(rows, labels, {MONTHS[0]: "DEVELOPMENT"}), key="split"
        )

        assert splits["ALL"]["shape"]["classification"] == "U_SHAPED"
        assert splits["ALL"]["d10_minus_d1_pooled"] == pytest.approx(0.0)
        assert splits["ALL"]["d10_minus_cross_section_mean_pooled"] > 0

    def test_the_diagnostics_are_deterministic(self) -> None:
        rows, labels, split_of = _world()

        first = subset_diagnostics(monthly_signal(rows, labels, split_of), key="year")
        second = subset_diagnostics(monthly_signal(rows, labels, split_of), key="year")

        assert first == second

    def test_leave_one_out_reports_every_removal(self) -> None:
        rows, labels, split_of = _world()
        monthly = monthly_signal(rows, labels, split_of)

        by_month = leave_one_out(monthly)
        by_year = leave_one_out(monthly, key="year")

        assert by_month["removals"] == len(MONTHS)
        assert by_month["d10_minus_d1"]["positive_after_removal"] == len(MONTHS)
        assert by_year["removals"] == 1

    def test_persistence_sees_a_stable_ranking(self) -> None:
        rows, _labels, _split = _world()

        persistence = rank_persistence(rows)

        assert persistence["rank_correlation"]["mean"] == pytest.approx(1.0)
        assert persistence["top_decile_retention_1m"]["mean"] == pytest.approx(1.0)

    def test_persistence_sees_a_reshuffled_ranking(self) -> None:
        rows = [
            _row(i, month, float(i if index % 2 == 0 else 101 - i))
            for index, month in enumerate(MONTHS)
            for i in range(1, 101)
        ]

        persistence = rank_persistence(rows)

        assert persistence["rank_correlation"]["mean"] == pytest.approx(-1.0)
        assert persistence["top_decile_retention_1m"]["mean"] == pytest.approx(0.0)


class TestPortfolioForensics:
    def test_identity_contributions_reconcile_to_the_profit(self) -> None:
        trades = [
            {
                "kind": "COMPANY",
                "identity_id": 1,
                "symbol": "A",
                "side": "BUY",
                "shares": 10.0,
                "price": 10.0,
                "notional": 100.0,
                "cost": 0.1,
                "month": "2024-01",
                "execution": "2024-02-01",
            },
            {
                "kind": "COMPANY",
                "identity_id": 1,
                "symbol": "A",
                "side": "SELL",
                "shares": 10.0,
                "price": 12.0,
                "notional": 120.0,
                "cost": 0.12,
                "month": "2024-02",
                "execution": "2024-03-01",
            },
            {
                "kind": "COMPANY",
                "identity_id": 2,
                "symbol": "B",
                "side": "BUY",
                "shares": 5.0,
                "price": 20.0,
                "notional": 100.0,
                "cost": 0.1,
                "month": "2024-02",
                "execution": "2024-03-01",
            },
        ]

        contributions = identity_contributions(trades, {("COMPANY", 2): 18.0})

        by_id = {record.identity_id: record for record in contributions}
        assert by_id[1].pnl == pytest.approx(120.0 - 100.0 - 0.22)
        assert by_id[2].pnl == pytest.approx(5.0 * 18.0 - 100.0 - 0.1)
        assert by_id[2].residual_shares == pytest.approx(5.0)

    def test_concentration_measures_the_largest_contributors(self) -> None:
        records = [
            IdentityPnL("COMPANY", i, f"S{i}", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, float(10 - i), 1)
            for i in range(10)
        ]

        shares = concentration(records, sum(r.pnl for r in records))

        assert shares["share_of_profit"]["top_1"] == pytest.approx(10 / 55)
        assert shares["winners"] == 10
        assert shares["losers"] == 0

    def test_turnover_is_split_into_why_it_happened(self) -> None:
        holdings = [
            {"month": "2024-01", "kind": "COMPANY", "identity_id": 1},
            {"month": "2024-02", "kind": "COMPANY", "identity_id": 2},
        ]
        trades = [
            {
                "month": "2024-01",
                "kind": "COMPANY",
                "identity_id": 1,
                "side": "BUY",
                "notional": 100.0,
            },
            {
                "month": "2024-02",
                "kind": "COMPANY",
                "identity_id": 1,
                "side": "SELL",
                "notional": 110.0,
            },
            {
                "month": "2024-02",
                "kind": "COMPANY",
                "identity_id": 2,
                "side": "BUY",
                "notional": 110.0,
            },
        ]

        sources = turnover_sources(trades, holdings)

        assert sources["by_cause"]["ENTRANT"] == pytest.approx(210.0)
        assert sources["by_cause"]["EXIT"] == pytest.approx(110.0)
        assert sources["ranking_churn_share"] == pytest.approx(1.0)

    def test_the_cost_break_even_reproduces_the_one_level_that_was_run(self) -> None:
        trades = [
            {"execution": "2024-02-01", "notional": 50_000.0},
            {"execution": "2024-03-01", "notional": 50_000.0},
        ]
        nav = [
            {"session": "2024-02-01", "nav": 100_000.0},
            {"session": "2024-03-01", "nav": 100_000.0},
        ]

        result = cost_break_even(
            trades,
            nav,
            strategy_total_return=1.0,
            benchmark_total_return=0.5,
            observed_cost_bps=10.0,
            observed_total_return=0.998,
            years=1.0,
        )

        assert result["traded_notional_over_nav"] == pytest.approx(1.0)
        assert result["break_even_cost_per_side_bps"] == pytest.approx(2876.8, rel=1e-3)
        assert abs(result["validation"]["residual"]) < 0.01
        assert "market impact" in result["not_modelled"]


class TestTheDecision:
    def _inputs(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "splits": {
                block: {
                    "d10_minus_d1": {"mean": 0.008, "positive_share": 0.6},
                    "rank_ic": {"mean": 0.025, "positive_share": 0.63},
                }
                for block in ("DEVELOPMENT", "VALIDATION", "HOLDOUT", "ALL")
            },
            "persistence": {"top_decile_retention_1m": {"mean": 0.71}},
            "leave_month": {
                "removals": 51,
                "d10_minus_d1": {"positive_after_removal": 51},
                "rank_ic": {"positive_after_removal": 51},
            },
            "leave_year": {
                "removals": 5,
                "d10_minus_d1": {"positive_after_removal": 5},
                "rank_ic": {"positive_after_removal": 5},
            },
            "name_concentration": {"share_of_profit": {"top_5": 0.20}},
            "month_concentration": {"share_of_profit": {"top_1": 0.18}},
            "cost": {"break_even_cost_per_side_bps": 78.0},
        }
        base.update(overrides)
        return base

    def test_every_condition_passing_is_the_strongest_classification(self) -> None:
        verdict = classify_signal(**self._inputs())

        assert verdict["classification"] == "STRONG_ENOUGH_FOR_HYPOTHESIS_TEST"
        assert verdict["failed_conditions"] == []
        assert set(verdict["conditions"]) == set(RUBRIC)

    def test_one_failing_condition_makes_it_fragile(self) -> None:
        verdict = classify_signal(
            **self._inputs(
                name_concentration={"share_of_profit": {"top_5": 0.80}},
            )
        )

        assert verdict["classification"] == "PROMISING_BUT_FRAGILE"
        assert verdict["failed_conditions"] == ["name_concentration"]

    def test_a_negative_split_is_inconclusive(self) -> None:
        splits = self._inputs()["splits"]
        splits["HOLDOUT"] = {
            "d10_minus_d1": {"mean": -0.001, "positive_share": 0.3},
            "rank_ic": {"mean": -0.01, "positive_share": 0.4},
        }

        verdict = classify_signal(**self._inputs(splits=splits))

        assert verdict["classification"] == "INCONCLUSIVE"

    def test_a_negative_overall_result_is_no_signal(self) -> None:
        splits = self._inputs()["splits"]
        splits["ALL"] = {
            "d10_minus_d1": {"mean": -0.002, "positive_share": 0.4},
            "rank_ic": {"mean": -0.01, "positive_share": 0.4},
        }

        verdict = classify_signal(**self._inputs(splits=splits))

        assert verdict["classification"] == "NO_MEANINGFUL_SIGNAL"

    def test_every_classification_is_one_of_the_declared_four(self) -> None:
        verdict = classify_signal(**self._inputs())

        assert verdict["classification"] in CLASSIFICATIONS

    def test_significance_is_not_a_condition_and_is_not_claimed(self) -> None:
        verdict = classify_signal(**self._inputs())

        assert "significance is not a condition" in verdict["statistical_note"]
        assert not any("significan" in text for text in RUBRIC.values())


class TestTheHypothesisAndTheExperiment:
    def _premises(self, verdict: str = "SUPPORTED") -> dict[str, Any]:
        return {
            name: {"claim": name, "evidence": {}, "verdict": verdict}
            for name in (
                "A_medium_horizon_stronger",
                "B_membership_persists",
                "C_turnover_is_material",
                "D_costs_bite_but_do_not_destroy",
                "E_not_a_single_event",
            )
        }

    def _record(self, verdict: str = "SUPPORTED") -> dict[str, Any]:
        return hypothesis_record(
            premises=self._premises(verdict),
            classification={"classification": "STRONG_ENOUGH_FOR_HYPOTHESIS_TEST"},
            binding={
                "marker_sha256": "ab" * 32,
                "dataset_id": "PIT_RESEARCH_DATASET_V2",
                "contract_version": "BACKTEST_DATA_CONTRACT_V1",
            },
            code_commit="c" * 40,
            limitations=["sector history unavailable"],
            evidence_against=["the interval was already observed"],
        )

    def test_the_hypothesis_binds_to_the_r1_0_record_it_came_from(self) -> None:
        record = self._record()

        assert record["derived_from"]["completion_marker_sha256"] == "ab" * 32
        assert record["derived_from"]["dataset_id"] == "PIT_RESEARCH_DATASET_V2"
        assert "derived from observed data" in record["derived_from"]["acknowledgement"]
        assert record["holdout_contamination"] == HOLDOUT_STATUS

    def test_an_unsupported_premise_rejects_the_hypothesis(self) -> None:
        record = self._record("NOT_SUPPORTED")

        assert record["status"] == "REJECTED"
        assert len(record["unsupported_premises"]) == 5

    def test_a_specification_cannot_exist_without_an_accepted_hypothesis(self) -> None:
        record = self._record("NOT_SUPPORTED")

        with pytest.raises(ValueError, match="no accepted hypothesis"):
            experiment_spec(hypothesis=record, binding={}, code_commit="c" * 40)

    def test_the_specification_fixes_exactly_one_of_everything(self) -> None:
        spec = experiment_spec(hypothesis=self._record(), binding={}, code_commit="c" * 40)

        assert spec["strategy_version"] == R1_2_STRATEGY
        assert spec["rules"]["rebalance"] == "calendar quarter ends only"
        assert spec["frequencies_tested"] == 1
        assert spec["selection_fractions_tested"] == 1
        assert spec["weightings_tested"] == 1
        assert spec["cost_levels_tested"] == 1
        assert spec["optimization_performed"] == "NONE"
        assert spec["parameter_sweeps"] == []
        assert spec["executed_in_r1_1"] is False

    def test_the_specification_carries_falsification_criteria_fixed_in_advance(self) -> None:
        spec = experiment_spec(hypothesis=self._record(), binding={}, code_commit="c" * 40)

        criteria = spec["falsification_criteria"]
        assert criteria["stated_before_the_experiment"] is True
        assert len(criteria["criteria"]) >= 4

    def test_the_specification_never_calls_the_seen_interval_untouched(self) -> None:
        spec = experiment_spec(hypothesis=self._record(), binding={}, code_commit="c" * 40)

        assert spec["historical_holdout_status"] == HOLDOUT_STATUS
        assert "never described as untouched" in spec["reporting_rule"]

    def test_the_premises_are_scored_from_evidence(self) -> None:
        premises = evaluate_h1(
            horizons={
                "1": {"d10_minus_d1": {"mean": 0.001}, "months_overlapped": 1},
                "5": {"d10_minus_d1": {"mean": 0.0003}, "months_overlapped": 1},
                "60": {"d10_minus_d1": {"mean": 0.024}, "months_overlapped": 2},
                "120": {"d10_minus_d1": {"mean": 0.056}, "months_overlapped": 5},
            },
            persistence={
                "top_decile_retention_1m": {"mean": 0.71},
                "top_decile_retention_3m": {"mean": 0.46},
                "rank_correlation": {"mean": 0.91},
            },
            turnover_annual=3.9,
            cost={
                "break_even_cost_per_side_bps": 78.0,
                "validation": {"observed_total_return": 1.11, "predicted_total_return": 1.106},
            },
            name_concentration={"share_of_profit": {"top_5": 0.20}},
            month_concentration={"share_of_profit": {"top_1": 0.18}},
            leave_month={"removals": 51, "d10_minus_d1": {"positive_after_removal": 51}},
            leave_year={"removals": 5, "d10_minus_d1": {"positive_after_removal": 5}},
        )

        assert premises["A_medium_horizon_stronger"]["verdict"] == "SUPPORTED"
        assert premises["B_membership_persists"]["verdict"] == "SUPPORTED"
        assert premises["C_turnover_is_material"]["verdict"] == "SUPPORTED"
        assert premises["D_costs_bite_but_do_not_destroy"]["verdict"] == "SUPPORTED"
        assert premises["E_not_a_single_event"]["verdict"] == "SUPPORTED"
        assert "overlap" in premises["A_medium_horizon_stronger"]["caveat"]


class TestWhatTheReviewMayNotDo:
    def _sources(self) -> list[tuple[str, str]]:
        directory = Path(signal_module.__file__ or "").parent
        return [
            (path.name, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.py"))
        ]

    def test_no_module_executes_a_strategy(self) -> None:
        for name, body in self._sources():
            assert "simulate(" not in body, name
            assert "Rebalance(" not in body, name
            assert "PriceBook" not in body, name

    def test_no_module_ranks_or_constrains_on_sector(self) -> None:
        offending = []
        for name, body in self._sources():
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                if ".sector" in stripped or ".industry" in stripped:
                    offending.append(f"{name}: {stripped}")
        assert offending == []

    def test_the_experiment_specification_is_never_executed_here(self) -> None:
        body = Path(run_module.__file__ or "").read_text(encoding="utf-8")

        assert '"executed_in_r1_1": False' in Path(review_module.__file__ or "").read_text(
            encoding="utf-8"
        )
        assert "run_r1_0" not in body

    def test_no_function_accepts_a_set_of_frequencies_or_fractions_to_try(self) -> None:
        import inspect

        for module in (signal_module, review_module, run_module):
            for name, member in vars(module).items():
                if not inspect.isfunction(member) or member.__module__ != module.__name__:
                    continue
                parameters = set(inspect.signature(member).parameters)
                forbidden = {
                    "frequencies",
                    "fractions",
                    "thresholds",
                    "top_n",
                    "grid",
                    "candidates",
                    "sweep",
                }
                assert not (parameters & forbidden), f"{module.__name__}.{name}"

    def test_the_score_change_diagnostic_is_skipped_rather_than_invented(self) -> None:
        body = Path(run_module.__file__ or "").read_text(encoding="utf-8")

        assert '"status": "SKIPPED"' in body
        assert "no canonical notion of a score change" in body

    def test_no_market_regime_is_invented(self) -> None:
        body = Path(run_module.__file__ or "").read_text(encoding="utf-8")

        assert "REGIME_DIAGNOSTIC_NOT_AVAILABLE" in body


# ----------------------------------------------------------------------------
# The whole review, end to end, over a synthetic R1.0 record
# ----------------------------------------------------------------------------


REVIEW_MONTHS = (
    "2022-01",
    "2022-02",
    "2022-03",
    "2022-04",
    "2022-05",
    "2022-06",
    "2023-01",
    "2023-02",
    "2023-03",
    "2023-04",
    "2023-05",
    "2023-06",
    "2024-01",
    "2024-02",
    "2024-03",
    "2024-04",
    "2024-05",
    "2024-06",
    "2025-01",
    "2025-02",
    "2025-03",
    "2025-04",
    "2025-05",
    "2025-06",
)


def _split_for(month: str) -> str:
    if month < "2024":
        return "DEVELOPMENT"
    return "VALIDATION" if month < "2025" else "HOLDOUT"


def _metrics(total: float, benchmark: float) -> dict[str, Any]:
    block = {
        name: {
            "strategy": {"total_return": total / 3.0},
            "benchmark": {"total_return": benchmark / 3.0},
        }
        for name in ("DEVELOPMENT", "VALIDATION", "HOLDOUT")
    }
    return {
        "window": {
            "total_return": total,
            "cagr": 0.2,
            "sessions": 505,
            "max_drawdown": -0.2,
            "sharpe": 0.9,
        },
        "floor_window": {"total_return": total},
        "benchmark": {"total_return": benchmark, "cagr": 0.13},
        "benchmark_relative_return": total - benchmark,
        "turnover": {"annual_estimate": 3.9},
        "splits": block,
        "diagnostics": {"settled_identities": 0, "unknown_terminal_identities": 0},
    }


@pytest.fixture
def frozen_record(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A complete, self-consistent R1.0 record and the database it read."""
    import sqlite3

    from stock_screener.signal_review.binding import (
        DOCUMENT_FINGERPRINTS,
        LEDGER_FINGERPRINTS,
        cross_section_fingerprint,
        file_sha256,
    )

    directory = tmp_path / "r1_0"
    (directory / "run_COMPOUNDER_TOP_DECILE_V1").mkdir(parents=True)
    (directory / "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1").mkdir(parents=True)
    (directory / "run_BASELINE_PASSIVE_V1").mkdir(parents=True)

    cross: list[dict[str, Any]] = []
    for month in REVIEW_MONTHS:
        for identity in range(1, 121):
            stage = "EXECUTABLE" if identity <= 100 else "TRADABLE"
            kind = "HISTORICAL" if identity > 115 else "COMPANY"
            row = _row(identity, month, float(identity), kind=kind, stage=stage)
            body = row.as_json()
            body["exclusion"] = None if stage == "EXECUTABLE" else "NO_EXECUTION_PRICE"
            cross.append(body)
    (directory / "cross_sections.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in cross), encoding="utf-8"
    )

    sessions = [f"{month}-28" for month in REVIEW_MONTHS]
    nav_rows = [
        {
            "session": session,
            "nav": 100_000.0 * (1.01**index),
            "cash": 0.0,
            "floor_nav": 100_000.0 * (1.01**index),
            "holdings": 10,
        }
        for index, session in enumerate(sessions)
    ]
    bench_rows = [
        {
            "session": session,
            "nav": 100_000.0 * (1.005**index),
            "cash": 0.0,
            "floor_nav": 100_000.0 * (1.005**index),
            "holdings": 1,
        }
        for index, session in enumerate(sessions)
    ]
    trades: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    selection: list[dict[str, Any]] = []
    for index, month in enumerate(REVIEW_MONTHS):
        for identity in range(91, 101):
            trades.append(
                {
                    "cost": 0.0,
                    "decision": f"{month}-28",
                    "execution": f"{month}-28",
                    "identity_id": identity,
                    "kind": "COMPANY",
                    "month": month,
                    "notional": 1_000.0,
                    "price": 10.0,
                    "reason": _split_for(month),
                    "shares": 100.0,
                    "side": "BUY" if index % 2 == 0 else "SELL",
                    "symbol": f"S{identity}",
                }
            )
            holdings.append(
                {
                    "actual_weight": 0.1,
                    "basis": "BAR",
                    "execution": f"{month}-28",
                    "identity_id": identity,
                    "kind": "COMPANY",
                    "market_value": 1_000.0,
                    "month": month,
                    "price": 10.0,
                    "shares": 100.0,
                    "symbol": f"S{identity}",
                    "target_weight": 0.1,
                }
            )
        selection.append(
            {
                "month": month,
                "decision": f"{month}-28",
                "eligible": 100,
                "selected": 10,
                "executed": 10,
                "split": _split_for(month),
            }
        )

    ledgers = {
        "compounder_deciles.jsonl": [{"decile": 1, "mean": 0.0}],
        "run_COMPOUNDER_TOP_DECILE_V1/trades.jsonl": trades,
        "run_COMPOUNDER_TOP_DECILE_V1/holdings.jsonl": holdings,
        "run_COMPOUNDER_TOP_DECILE_V1/nav.jsonl": nav_rows,
        "run_COMPOUNDER_TOP_DECILE_V1/selection.jsonl": selection,
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/nav.jsonl": bench_rows,
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/trades.jsonl": trades,
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/holdings.jsonl": holdings,
        "run_BASELINE_PASSIVE_V1/nav.jsonl": bench_rows,
    }
    for name, rows in ledgers.items():
        (directory / name).write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

    documents: dict[str, dict[str, Any]] = {
        "research_split_v1.json": {
            "version": "RESEARCH_SPLIT_V1",
            "decision_dates": [
                {"month": month, "decision": f"{month}-28", "split": _split_for(month)}
                for month in REVIEW_MONTHS
            ],
        },
        "baseline_registry.json": {"strategies": {}},
        "r1_0_preregistration.json": {"phase": "R1.0"},
        "monthly_coverage.json": {"months": []},
        "signal_diagnostics_compounder_v1.json": {"observations": 0},
        "spot_audit.json": {"failures": []},
        "bias_audit.json": {"universe": {}},
        "r1_0_summary.json": {"phase": "R1.0"},
    }
    for name, body in documents.items():
        (directory / name).write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for name, body in (
        ("run_COMPOUNDER_TOP_DECILE_V1/metrics.json", _metrics(1.2, 0.7)),
        ("run_COMPOUNDER_TOP_DECILE_V1/metrics.SIMPLE_10BPS_PER_SIDE_V1.json", _metrics(1.1, 0.7)),
        ("run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/metrics.json", _metrics(0.4, 0.7)),
    ):
        (directory / name).write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    fingerprints = {
        key: file_sha256(directory / name) for key, name in DOCUMENT_FINGERPRINTS.items()
    }
    for key, name in LEDGER_FINGERPRINTS.items():
        fingerprints[key] = ledger_fingerprint(ledgers[name])
    fingerprints["cross_sections"] = cross_section_fingerprint(cross)
    marker = {
        "stage": "R1_0_COMPLETE",
        "dataset_id": "PIT_RESEARCH_DATASET_V2",
        "contract_version": "BACKTEST_DATA_CONTRACT_V1",
        "contract_bindings": {"database": "20" * 32},
        "result_fingerprints": fingerprints,
    }
    (directory / "r1_0_complete.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / "r1_0_fingerprints.json").write_text(
        json.dumps({"fingerprints": fingerprints}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    database = tmp_path / "research.db"
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "CREATE TABLE forward_returns (company_id INTEGER, historical_security_id INTEGER,"
            " observation_date TEXT, horizon_sessions INTEGER, status TEXT, total_return REAL)"
        )
        db.execute("CREATE TABLE price_history (company_id INTEGER, date TEXT, close REAL)")
        db.execute(
            "CREATE TABLE historical_prices (historical_security_id INTEGER, date TEXT, close REAL)"
        )
        for month in REVIEW_MONTHS:
            for identity in range(1, 121):
                for horizon in (1, 5, 20, 60, 120):
                    db.execute(
                        "INSERT INTO forward_returns VALUES (?,?,?,?,?,?)",
                        (
                            identity,
                            None,
                            f"{month}-28",
                            horizon,
                            "REALISED",
                            identity / 1000.0 * (horizon / 20.0),
                        ),
                    )
        db.execute("INSERT INTO price_history VALUES (?,?,?)", (100, sessions[-1], 12.0))
        db.commit()

    shas = {
        "marker": file_sha256(directory / "r1_0_complete.json"),
        "split": file_sha256(directory / "research_split_v1.json"),
        "prereg": file_sha256(directory / "r1_0_preregistration.json"),
    }
    return directory, database, shas


def _review(record: tuple[Path, Path, dict[str, str]], out: Path) -> Any:
    from stock_screener.signal_review import review_r1_0

    directory, database, shas = record
    return review_r1_0(
        out,
        directory,
        database,
        expect_marker_sha=shas["marker"],
        expect_split_sha=shas["split"],
        expect_preregistration_sha=shas["prereg"],
        code_commit="a" * 40,
    )


class TestTheReviewEndToEnd:
    def test_it_writes_every_artifact_and_decides(
        self, frozen_record: tuple[Path, Path, dict[str, str]], tmp_path: Path
    ) -> None:
        result = _review(frozen_record, tmp_path / "r1_1")

        for name in (
            "holdout_status.json",
            "compounder_signal_review.json",
            "compounder_split_diagnostics.json",
            "compounder_year_diagnostics.json",
            "compounder_persistence.json",
            "compounder_concentration.json",
            "compounder_turnover_analysis.json",
            "compounder_cost_break_even.json",
            "sample_attrition.json",
            "r1_1_hypothesis.json",
            "r1_1_summary.json",
            "r1_1_fingerprints.json",
            "r1_1_complete.json",
        ):
            assert (tmp_path / "r1_1" / name).is_file(), name
        assert result.marker["stage"] == "R1_1_COMPLETE"
        assert result.marker["strategy_executed"] is False
        assert result.classification["classification"] in CLASSIFICATIONS

    def test_two_reviews_fingerprint_identically(
        self, frozen_record: tuple[Path, Path, dict[str, str]], tmp_path: Path
    ) -> None:
        first = _review(frozen_record, tmp_path / "a")
        second = _review(frozen_record, tmp_path / "b")

        assert first.fingerprints == second.fingerprints
        assert first.fingerprints

    def test_a_rising_relationship_is_detected_end_to_end(
        self, frozen_record: tuple[Path, Path, dict[str, str]], tmp_path: Path
    ) -> None:
        _review(frozen_record, tmp_path / "r1_1")

        splits = json.loads(
            (tmp_path / "r1_1" / "compounder_split_diagnostics.json").read_text(encoding="utf-8")
        )["splits"]
        assert splits["ALL"]["rank_ic"]["mean"] == pytest.approx(1.0)
        assert splits["ALL"]["d10_minus_d1_pooled"] > 0
        assert splits["ALL"]["shape"]["classification"] == "RISING"

    def test_the_specification_is_written_but_never_executed(
        self, frozen_record: tuple[Path, Path, dict[str, str]], tmp_path: Path
    ) -> None:
        result = _review(frozen_record, tmp_path / "r1_1")

        if result.spec is not None:
            spec = json.loads(
                (tmp_path / "r1_1" / "r1_2_experiment_spec.json").read_text(encoding="utf-8")
            )
            assert spec["executed_in_r1_1"] is False
            assert spec["frequencies_tested"] == 1
        assert result.marker["strategy_executed"] is False

    def test_a_tampered_record_is_refused_before_anything_is_computed(
        self, frozen_record: tuple[Path, Path, dict[str, str]], tmp_path: Path
    ) -> None:
        directory, _database, _shas = frozen_record
        (directory / "spot_audit.json").write_text("{}\n", encoding="utf-8")

        with pytest.raises(ReviewBindingError):
            _review(frozen_record, tmp_path / "r1_1")

        assert not (tmp_path / "r1_1" / "r1_1_complete.json").exists()

    def test_the_summary_carries_the_holdout_status_and_the_limitations(
        self, frozen_record: tuple[Path, Path, dict[str, str]], tmp_path: Path
    ) -> None:
        _review(frozen_record, tmp_path / "r1_1")

        summary = json.loads((tmp_path / "r1_1" / "r1_1_summary.json").read_text(encoding="utf-8"))
        assert summary["holdout_status"]["NEW_DERIVED_HYPOTHESES"] == "HOLDOUT_ALREADY_OBSERVED"
        assert len(summary["limitations"]) >= 10
