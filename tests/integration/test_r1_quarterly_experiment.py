"""Adversarial tests for the R1.2 quarterly experiment.

R1.2 runs one frozen experiment and judges it by criteria it may not touch.
Each test names a way that could go wrong — a specification that drifted, a
second frequency slipped in, a selection fraction changed, a criterion
threshold moved after the result, a comparator quietly re-simulated, an
already-observed interval called confirmation — and asserts the code refuses
it or records it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.quarterly_experiment import (
    EXPECTED_RULES,
    MONTHLY_TURNOVER_REFERENCE,
    OUTCOMES,
    PRIMARY_HORIZON,
    STRATEGY,
    ExperimentBindingError,
    bind_experiment,
    evaluate_criteria,
    interpret,
    quarter_of,
)
from stock_screener.quarterly_experiment import calendar as calendar_module
from stock_screener.quarterly_experiment import evaluate as evaluate_module
from stock_screener.quarterly_experiment import run as run_module
from stock_screener.signal_review.binding import file_sha256

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]

FROZEN_CRITERIA = (
    "the quarterly cross-section's mean D10-D1 at the 60-session horizon is not positive over "
    "the full window",
    "top-decile selection at quarter ends loses the monotone top-end ordering that monthly "
    "selection shows, measured as a non-positive decile rank association",
    "annual turnover does not fall by at least half against the monthly baseline's 3.90x",
    "the benchmark-relative total return is not positive in at least two of the three blocks",
    "the five largest identity contributors account for half or more of the profit",
)


def _write(path: Path, body: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return file_sha256(path)


def _spec(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "experiment_id": "R1.2",
        "strategy_version": STRATEGY,
        "tests_hypothesis": "H1_MEDIUM_HORIZON_SELECTION",
        "rules": dict(EXPECTED_RULES),
        "optimization_performed": "NONE",
        "variants_tried": 1,
        "parameter_sweeps": [],
        "frequencies_tested": 1,
        "selection_fractions_tested": 1,
        "weightings_tested": 1,
        "execution_rules_tested": 1,
        "cost_levels_tested": 1,
        "executed_in_r1_1": False,
    }
    body.update(overrides)
    return body


def _hypothesis(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "hypothesis_id": "H1_MEDIUM_HORIZON_SELECTION",
        "status": "ACCEPTED_FOR_ONE_EXPERIMENT",
        "statement": "a lower-frequency top-decile portfolio may preserve the selection effect",
        "falsification_criteria": {
            "stated_before_the_experiment": True,
            "criteria": list(FROZEN_CRITERIA),
            "note": "fixed before the experiment runs",
        },
    }
    body.update(overrides)
    return body


class TestTheExperimentBinding:
    def _record(self, tmp_path: Path, **kinds: Any) -> tuple[Path, Path, dict[str, str]]:
        r1_1 = tmp_path / "r1_1"
        shas = {
            "spec": _write(r1_1 / "r1_2_experiment_spec.json", kinds.get("spec") or _spec()),
            "hypothesis": _write(
                r1_1 / "r1_1_hypothesis.json", kinds.get("hypothesis") or _hypothesis()
            ),
            "marker": _write(r1_1 / "r1_1_complete.json", {"stage": "R1_1_COMPLETE"}),
        }
        return r1_1, tmp_path / "r1_0", shas

    def test_a_specification_that_drifted_from_the_executor_is_refused(
        self, tmp_path: Path
    ) -> None:
        rules = dict(EXPECTED_RULES)
        rules["rebalance"] = "calendar month ends only"
        r1_1, r1_0, shas = self._record(tmp_path, spec=_spec(rules=rules))

        with pytest.raises(ExperimentBindingError, match="disagree on"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha=shas["spec"],
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )

    def test_a_changed_selection_fraction_is_refused(self, tmp_path: Path) -> None:
        rules = dict(EXPECTED_RULES)
        rules["selection"] = "top 20% of the executable cross-section"
        r1_1, r1_0, shas = self._record(tmp_path, spec=_spec(rules=rules))

        with pytest.raises(ExperimentBindingError, match="disagree on"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha=shas["spec"],
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )

    def test_more_than_one_frequency_is_refused(self, tmp_path: Path) -> None:
        r1_1, r1_0, shas = self._record(tmp_path, spec=_spec(frequencies_tested=3))

        with pytest.raises(ExperimentBindingError, match="frequencies_tested"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha=shas["spec"],
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )

    def test_a_parameter_sweep_is_refused(self, tmp_path: Path) -> None:
        r1_1, r1_0, shas = self._record(tmp_path, spec=_spec(parameter_sweeps=["top_n in 5,10,20"]))

        with pytest.raises(ExperimentBindingError, match="parameter sweeps"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha=shas["spec"],
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )

    def test_a_rejected_hypothesis_is_refused(self, tmp_path: Path) -> None:
        r1_1, r1_0, shas = self._record(tmp_path, hypothesis=_hypothesis(status="REJECTED"))

        with pytest.raises(ExperimentBindingError, match="REJECTED"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha=shas["spec"],
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )

    def test_a_moved_specification_digest_is_refused(self, tmp_path: Path) -> None:
        r1_1, r1_0, shas = self._record(tmp_path)

        with pytest.raises(ExperimentBindingError, match="r1_2_experiment_spec"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha="11" * 32,
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )

    def test_a_different_number_of_criteria_is_refused(self, tmp_path: Path) -> None:
        criteria = {"stated_before_the_experiment": True, "criteria": list(FROZEN_CRITERIA[:3])}
        r1_1, r1_0, shas = self._record(
            tmp_path, hypothesis=_hypothesis(falsification_criteria=criteria)
        )

        with pytest.raises(ExperimentBindingError, match=r"turnover reference|five"):
            bind_experiment(
                r1_1,
                r1_0,
                expect_spec_sha=shas["spec"],
                expect_hypothesis_sha=shas["hypothesis"],
                expect_r1_1_marker_sha=shas["marker"],
                expect_r1_0_marker_sha="00" * 32,
                expect_split_sha="00" * 32,
                expect_preregistration_sha="00" * 32,
            )


class TestTheQuarterlyCalendar:
    def test_a_quarter_end_is_the_last_session_of_its_quarter(self) -> None:
        assert quarter_of(date(2024, 1, 31)) == (2024, 1)
        assert quarter_of(date(2024, 3, 31)) == (2024, 1)
        assert quarter_of(date(2024, 4, 1)) == (2024, 2)
        assert quarter_of(date(2024, 12, 31)) == (2024, 4)

    def test_every_live_quarter_end_is_one_of_the_frozen_monthly_decisions(self) -> None:
        split = json.loads(
            (REPO / ".evidence" / "research" / "r1_0" / "research_split_v1.json").read_text(
                encoding="utf-8"
            )
        )
        monthly = {str(entry["decision"]) for entry in split["decision_dates"]}
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(f"file:{REPO / 'stage9k.db'}?mode=ro", uri=True)) as db:
            calendar = calendar_module.quarterly_calendar(
                db,
                {str(e["decision"]): str(e["split"]) for e in split["decision_dates"]},
            )

        assert calendar
        assert all(entry.decision.isoformat() in monthly for entry in calendar)
        assert all(entry.execution > entry.decision for entry in calendar)
        assert [entry.quarter for entry in calendar] == sorted(entry.quarter for entry in calendar)

    def test_the_calendar_covers_every_block(self) -> None:
        split = json.loads(
            (REPO / ".evidence" / "research" / "r1_0" / "research_split_v1.json").read_text(
                encoding="utf-8"
            )
        )
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(f"file:{REPO / 'stage9k.db'}?mode=ro", uri=True)) as db:
            calendar = calendar_module.quarterly_calendar(
                db,
                {str(e["decision"]): str(e["split"]) for e in split["decision_dates"]},
            )

        counts = calendar_module.quarters_by_block(calendar)
        assert set(counts) == {"DEVELOPMENT", "VALIDATION", "HOLDOUT"}
        assert all(value > 0 for value in counts.values())


class TestTheFrozenCriteria:
    def _evaluate(self, **overrides: Any) -> Sequence[Any]:
        base: dict[str, Any] = {
            "contrast_60": 0.02,
            "decile_association": 0.48,
            "annual_turnover": 1.80,
            "monthly_turnover_reference": MONTHLY_TURNOVER_REFERENCE,
            "block_relative": {"DEVELOPMENT": 0.10, "VALIDATION": -0.07, "HOLDOUT": 0.44},
            "top_five_share": 0.21,
        }
        base.update(overrides)
        return evaluate_criteria(FROZEN_CRITERIA, **base)

    def test_all_five_met_is_supported(self) -> None:
        results = self._evaluate()

        assert all(item.met for item in results)
        assert interpret(results, None)["outcome"] == "SUPPORTED_BY_R1_2"

    def test_the_turnover_threshold_is_exactly_half_the_frozen_reference(self) -> None:
        results = self._evaluate(annual_turnover=1.95)
        just_over = self._evaluate(annual_turnover=1.9500001)

        assert results[2].threshold == pytest.approx(MONTHLY_TURNOVER_REFERENCE / 2.0)
        assert results[2].met is True
        assert just_over[2].met is False

    def test_a_missed_turnover_threshold_is_partial_not_supported(self) -> None:
        results = self._evaluate(annual_turnover=2.25)

        interpretation = interpret(results, None)
        assert results[2].met is False
        assert interpretation["outcome"] == "PARTIALLY_SUPPORTED_BY_R1_2"
        assert interpretation["criteria_failed"] == [3]

    def test_two_positive_blocks_are_enough_and_one_is_not(self) -> None:
        two = self._evaluate(block_relative={"A": 0.1, "B": -0.1, "C": 0.2})
        one = self._evaluate(block_relative={"A": 0.1, "B": -0.1, "C": -0.2})

        assert two[3].met is True
        assert one[3].met is False

    def test_a_negative_primary_contrast_fails_the_first_criterion(self) -> None:
        results = self._evaluate(contrast_60=-0.001)

        assert results[0].met is False
        assert results[0].threshold == 0.0

    def test_concentration_at_or_above_half_fails(self) -> None:
        assert self._evaluate(top_five_share=0.50)[4].met is False
        assert self._evaluate(top_five_share=0.4999)[4].met is True

    def test_nothing_met_is_not_supported(self) -> None:
        results = self._evaluate(
            contrast_60=-0.01,
            decile_association=-0.2,
            annual_turnover=4.0,
            block_relative={"A": -0.1, "B": -0.1, "C": -0.2},
            top_five_share=0.9,
        )

        assert interpret(results, None)["outcome"] == "NOT_SUPPORTED_BY_R1_2"

    def test_every_outcome_is_one_of_the_declared_three(self) -> None:
        assert interpret(self._evaluate(), None)["outcome"] in OUTCOMES

    def test_no_aggregate_rule_is_invented_when_the_spec_states_none(self) -> None:
        interpretation = interpret(self._evaluate(annual_turnover=2.25), None)

        assert interpretation["aggregate_rule_in_frozen_spec"] is None
        assert "applies no weights" in interpretation["how_this_label_was_reached"]
        assert "treats no criterion as decisive" in interpretation["how_this_label_was_reached"]

    def test_a_frozen_aggregate_rule_would_be_used_verbatim(self) -> None:
        interpretation = interpret(self._evaluate(), "all five must hold")

        assert "applied the frozen aggregate rule" in interpretation["how_this_label_was_reached"]

    def test_the_criteria_must_number_five(self) -> None:
        with pytest.raises(ValueError, match="five frozen criteria"):
            evaluate_criteria(
                FROZEN_CRITERIA[:4],
                contrast_60=0.02,
                decile_association=0.4,
                annual_turnover=1.5,
                monthly_turnover_reference=MONTHLY_TURNOVER_REFERENCE,
                block_relative={},
                top_five_share=0.2,
            )

    def test_each_result_carries_the_frozen_text_it_was_derived_from(self) -> None:
        results = self._evaluate()

        assert [item.frozen_text for item in results] == list(FROZEN_CRITERIA)
        assert all(item.requirement for item in results)


class TestWhatTheExperimentMayNotDo:
    def _sources(self) -> list[tuple[str, str]]:
        directory = Path(run_module.__file__ or "").parent
        return [
            (path.name, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.py"))
        ]

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

    def test_no_module_combines_the_market_signal_layer(self) -> None:
        for name, body in self._sources():
            assert "MARKET_SIGNALS" not in body, name
            assert "derive_market_signals" not in body, name

    def test_the_comparator_never_re_simulates_the_monthly_strategy(self) -> None:
        body = Path(run_module.__file__ or "").read_text(encoding="utf-8")
        matched = body[body.index("def _matched_monthly") : body.index("def run_experiment")]

        assert "simulate(" not in matched
        assert "Rebalance(" not in matched
        assert "nav.jsonl" in matched

    def test_the_primary_horizon_is_the_one_h1_froze(self) -> None:
        assert PRIMARY_HORIZON == 60
        assert run_module.REPORTED_HORIZONS == (20, 60, 120)

    def test_the_research_status_never_claims_confirmation(self) -> None:
        status = run_module._research_status()

        assert status["independent_out_of_sample_confirmation"] is False
        assert (
            status["r1_0_holdout_block"]
            == "REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION"
        )
        assert status["replacement_holdout_manufactured"] is False

    def test_the_outcome_language_is_research_state_only(self) -> None:
        body = Path(evaluate_module.__file__ or "").read_text(encoding="utf-8")

        assert "proven" not in body.lower().replace("not 'proven'", "")
        assert all(name.endswith("_BY_R1_2") for name in OUTCOMES)

    def test_no_function_accepts_a_set_of_frequencies_to_try(self) -> None:
        import inspect

        for module in (run_module, evaluate_module, calendar_module):
            for name, member in vars(module).items():
                if not inspect.isfunction(member) or member.__module__ != module.__name__:
                    continue
                parameters = set(inspect.signature(member).parameters)
                forbidden = {"frequencies", "fractions", "thresholds", "top_n", "grid", "sweep"}
                assert not (parameters & forbidden), f"{module.__name__}.{name}"


# ----------------------------------------------------------------------------
# The whole experiment, end to end, over a synthetic frozen record
# ----------------------------------------------------------------------------


def _weekdays(first: date, count: int) -> list[date]:
    from datetime import timedelta

    out: list[date] = []
    day = first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


EXPERIMENT_SESSIONS = _weekdays(date(2022, 1, 3), 800)
IDENTITIES = 40


def _month_ends(sessions: list[date]) -> list[date]:
    last: dict[str, date] = {}
    for session in sessions:
        last[session.strftime("%Y-%m")] = session
    return sorted(last.values())


def _block(month: str) -> str:
    if month < "2023":
        return "DEVELOPMENT"
    return "VALIDATION" if month < "2024" else "HOLDOUT"


@pytest.fixture
def frozen_r1_0(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    """A complete synthetic R1.0 record, its database, and a contract directory."""
    import sqlite3
    from contextlib import closing

    from stock_screener.backtest_harness import CrossSectionRow
    from stock_screener.signal_review.binding import (
        DOCUMENT_FINGERPRINTS,
        LEDGER_FINGERPRINTS,
        cross_section_fingerprint,
        ledger_fingerprint,
    )

    r1_0 = tmp_path / "r1_0"
    for name in (
        "run_COMPOUNDER_TOP_DECILE_V1",
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1",
        "run_BASELINE_PASSIVE_V1",
    ):
        (r1_0 / name).mkdir(parents=True)

    decisions = _month_ends(EXPERIMENT_SESSIONS)
    index = {session: position for position, session in enumerate(EXPERIMENT_SESSIONS)}
    successor = {
        session: EXPERIMENT_SESSIONS[index[session] + 1]
        for session in decisions
        if index[session] + 1 < len(EXPERIMENT_SESSIONS)
    }
    decisions = [session for session in decisions if session in successor]

    cross: list[dict[str, Any]] = []
    for session in decisions:
        month = session.strftime("%Y-%m")
        for identity in range(1, IDENTITIES + 1):
            row = CrossSectionRow(
                kind="COMPANY",
                identity_id=identity,
                symbol=f"S{identity}",
                month=month,
                decision=session.isoformat(),
                execution=successor[session].isoformat(),
                universe_source="FROZEN_VERDICT",
                evidence_date=None,
                tradability="TRADABLE",
                score_status="SCORED",
                final_score=float(identity),
                freshness="CURRENT",
                rank_eligible=True,
                execution_price=10.0,
                adv_dollar=1_000_000.0,
                stage="EXECUTABLE",
                exclusion=None,
                classification_is_current=True,
            )
            cross.append(row.as_json())
    (r1_0 / "cross_sections.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in cross), encoding="utf-8"
    )

    monthly_nav = [
        {
            "session": s.isoformat(),
            "nav": 100_000.0 * (1.002**i),
            "cash": 0.0,
            "floor_nav": 100_000.0 * (1.002**i),
            "holdings": 4,
        }
        for i, s in enumerate(EXPERIMENT_SESSIONS)
    ]
    monthly_net: list[dict[str, Any]] = [
        {**row, "nav": 100_000.0 * (1.002**i) * 0.99} for i, row in enumerate(monthly_nav)
    ]
    monthly_trades = [
        {
            "cost": 0.0,
            "decision": s.isoformat(),
            "execution": successor[s].isoformat(),
            "identity_id": 40,
            "kind": "COMPANY",
            "month": s.strftime("%Y-%m"),
            "notional": 5_000.0,
            "price": 10.0,
            "reason": _block(s.strftime("%Y-%m")),
            "shares": 500.0,
            "side": "BUY",
            "symbol": "S40",
        }
        for s in decisions
    ]
    monthly_holdings = [
        {
            "actual_weight": 0.25,
            "basis": "BAR",
            "execution": successor[s].isoformat(),
            "identity_id": 40,
            "kind": "COMPANY",
            "market_value": 5_000.0,
            "month": s.strftime("%Y-%m"),
            "price": 10.0,
            "shares": 500.0,
            "symbol": "S40",
            "target_weight": 0.25,
        }
        for s in decisions
    ]
    ledgers: dict[str, list[dict[str, Any]]] = {
        "compounder_deciles.jsonl": [{"decile": 1}],
        "run_COMPOUNDER_TOP_DECILE_V1/trades.jsonl": monthly_trades,
        "run_COMPOUNDER_TOP_DECILE_V1/holdings.jsonl": monthly_holdings,
        "run_COMPOUNDER_TOP_DECILE_V1/nav.jsonl": monthly_nav,
        "run_COMPOUNDER_TOP_DECILE_V1/selection.jsonl": [{"month": "2022-01"}],
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/nav.jsonl": monthly_nav,
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/trades.jsonl": monthly_trades,
        "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/holdings.jsonl": monthly_holdings,
        "run_BASELINE_PASSIVE_V1/nav.jsonl": monthly_nav,
    }
    for name, rows in ledgers.items():
        (r1_0 / name).write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )
    (r1_0 / "run_COMPOUNDER_TOP_DECILE_V1" / "nav.SIMPLE_10BPS_PER_SIDE_V1.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in monthly_net), encoding="utf-8"
    )

    documents: dict[str, dict[str, Any]] = {
        "research_split_v1.json": {
            "version": "RESEARCH_SPLIT_V1",
            "blocks": {},
            "decision_dates": [
                {
                    "month": s.strftime("%Y-%m"),
                    "decision": s.isoformat(),
                    "split": _block(s.strftime("%Y-%m")),
                }
                for s in decisions
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
        _write(r1_0 / name, body)

    fingerprints = {key: file_sha256(r1_0 / name) for key, name in DOCUMENT_FINGERPRINTS.items()}
    for key, name in LEDGER_FINGERPRINTS.items():
        fingerprints[key] = ledger_fingerprint(ledgers[name])
    fingerprints["cross_sections"] = cross_section_fingerprint(cross)
    marker = {
        "stage": "R1_0_COMPLETE",
        "dataset_id": "PIT_RESEARCH_DATASET_V2",
        "contract_version": "BACKTEST_DATA_CONTRACT_V1",
        "contract_bindings": {
            "universe": "ab" * 32,
            "price_history": "cd" * 32,
            "forward_return_labels": "ef" * 32,
            "signal_observations": "12" * 32,
        },
        "result_fingerprints": fingerprints,
    }
    marker_sha = _write(r1_0 / "r1_0_complete.json", marker)
    _write(r1_0 / "r1_0_fingerprints.json", {"fingerprints": fingerprints})

    database = tmp_path / "research.db"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE benchmark_prices (symbol TEXT, date TEXT, open REAL, close REAL)")
        db.execute(
            "CREATE TABLE price_history (company_id INTEGER, date TEXT, open REAL, close REAL)"
        )
        db.execute(
            "CREATE TABLE historical_prices (historical_security_id INTEGER, date TEXT,"
            " open REAL, close REAL)"
        )
        db.execute(
            "CREATE TABLE forward_returns (company_id INTEGER, historical_security_id INTEGER,"
            " observation_date TEXT, horizon_sessions INTEGER, status TEXT, total_return REAL)"
        )
        db.execute("CREATE TABLE historical_securities (id INTEGER PRIMARY KEY, name TEXT)")
        for position, session in enumerate(EXPERIMENT_SESSIONS):
            level = 400.0 * (1.001**position)
            db.execute(
                "INSERT INTO benchmark_prices VALUES (?,?,?,?)",
                ("SPY", session.isoformat(), level, level),
            )
            for identity in range(1, IDENTITIES + 1):
                price = 10.0 * (1.0 + identity / 10_000.0) ** position
                db.execute(
                    "INSERT INTO price_history VALUES (?,?,?,?)",
                    (identity, session.isoformat(), price, price),
                )
        for session in decisions:
            for identity in range(1, IDENTITIES + 1):
                for horizon in (20, 60, 120):
                    db.execute(
                        "INSERT INTO forward_returns VALUES (?,?,?,?,?,?)",
                        (
                            identity,
                            None,
                            session.isoformat(),
                            horizon,
                            "REALISED",
                            identity / 1000.0 * (horizon / 60.0),
                        ),
                    )
        db.commit()

    contract = tmp_path / "contract"
    contract.mkdir()
    schema = json.loads(
        (
            REPO / ".evidence" / "contracts" / "backtest_v1" / "backtest_run_manifest_schema.json"
        ).read_text(encoding="utf-8")
    )
    _write(contract / "backtest_run_manifest_schema.json", schema)
    return (
        r1_0,
        database,
        contract,
        {
            "marker": marker_sha,
            "split": file_sha256(r1_0 / "research_split_v1.json"),
            "prereg": file_sha256(r1_0 / "r1_0_preregistration.json"),
        },
    )


@pytest.fixture
def frozen_experiment(
    tmp_path: Path, frozen_r1_0: tuple[Path, Path, Path, dict[str, str]]
) -> tuple[Any, Path, Path, Path]:
    r1_0, database, contract, shas = frozen_r1_0
    r1_1 = tmp_path / "r1_1"
    spec_sha = _write(r1_1 / "r1_2_experiment_spec.json", _spec())
    hypothesis_sha = _write(r1_1 / "r1_1_hypothesis.json", _hypothesis())
    marker_sha = _write(r1_1 / "r1_1_complete.json", {"stage": "R1_1_COMPLETE"})
    binding = bind_experiment(
        r1_1,
        r1_0,
        expect_spec_sha=spec_sha,
        expect_hypothesis_sha=hypothesis_sha,
        expect_r1_1_marker_sha=marker_sha,
        expect_r1_0_marker_sha=shas["marker"],
        expect_split_sha=shas["split"],
        expect_preregistration_sha=shas["prereg"],
    )
    return binding, r1_0, database, contract


def _execute(frozen: tuple[Any, Path, Path, Path], out: Path) -> Any:
    from stock_screener.quarterly_experiment import run_experiment

    binding, r1_0, database, contract = frozen
    return run_experiment(
        out,
        r1_0,
        database,
        binding,
        contract,
        contract_sha256="f7" * 32,
        code_commit="a" * 40,
    )


class TestTheExperimentEndToEnd:
    def test_it_rebalances_only_at_quarter_ends(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        result = _execute(frozen_experiment, tmp_path / "r1_2")

        calendar = [
            json.loads(line)
            for line in (tmp_path / "r1_2" / "quarterly_decision_calendar.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert calendar
        assert all(entry["decision"][5:7] in {"03", "06", "09", "12"} for entry in calendar)
        assert result.marker["stage"] == "R1_2_COMPLETE"

    def test_the_selection_is_the_top_tenth_and_equally_weighted(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        _execute(frozen_experiment, tmp_path / "r1_2")

        holdings = [
            json.loads(line)
            for line in (tmp_path / "r1_2" / "quarterly_holdings_ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        per_quarter: dict[str, list[dict[str, Any]]] = {}
        for row in holdings:
            per_quarter.setdefault(str(row["month"]), []).append(row)
        for rows in per_quarter.values():
            assert len(rows) == IDENTITIES // 10
            weights = {round(float(row["target_weight"]), 9) for row in rows}
            assert weights == {round(1.0 / len(rows), 9)}
            # the top decile by score is the highest identity ids
            assert min(int(row["identity_id"]) for row in rows) > IDENTITIES - len(rows) - 1

    def test_accounting_reconciles_and_nothing_is_violated(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        result = _execute(frozen_experiment, tmp_path / "r1_2")

        metrics = json.loads(
            (tmp_path / "r1_2" / "quarterly_metrics.json").read_text(encoding="utf-8")
        )
        assert result.violations == []
        assert abs(float(metrics["profit_reconciliation_residual"])) < 1e-6
        assert result.manifest_problems == []

    def test_the_comparator_is_the_frozen_monthly_ledger_sliced(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        _execute(frozen_experiment, tmp_path / "r1_2")

        comparison = json.loads(
            (tmp_path / "r1_2" / "monthly_matched_window_comparison.json").read_text(
                encoding="utf-8"
            )
        )
        comparator = comparison["comparator"]
        assert comparator["comparator_id"] == "R1_0_MONTHLY_TOP_DECILE_MATCHED_WINDOW"
        assert comparator["source_strategy_version"] == "COMPOUNDER_TOP_DECILE_V1"
        assert "not a re-simulation" in comparator["what_this_is"]
        assert comparison["delta_quarterly_minus_monthly"]["total_return"] is not None

    def test_every_artifact_is_written_and_the_holdout_status_is_carried(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        result = _execute(frozen_experiment, tmp_path / "r1_2")

        for name in (
            "r1_2_experiment_binding.json",
            "quarterly_decision_calendar.jsonl",
            "quarterly_cross_sections.jsonl",
            "quarterly_trade_ledger.jsonl",
            "quarterly_holdings_ledger.jsonl",
            "quarterly_nav.jsonl",
            "quarterly_metrics.json",
            "quarterly_decile_diagnostics.json",
            "monthly_matched_window_comparison.json",
            "r1_2_hypothesis_evaluation.json",
            "r1_2_run_manifest.json",
            "r1_2_summary.json",
            "r1_2_complete.json",
        ):
            assert (tmp_path / "r1_2" / name).is_file(), name
        status = result.marker["holdout_contamination"]
        assert status["independent_out_of_sample_confirmation"] is False
        assert result.evaluation["interpretation"]["outcome"] in OUTCOMES

    def test_two_executions_fingerprint_identically(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        first = _execute(frozen_experiment, tmp_path / "a")
        second = _execute(frozen_experiment, tmp_path / "b")

        assert first.fingerprints == second.fingerprints
        assert first.fingerprints

    def test_the_cost_sensitivity_never_beats_the_gross_path(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        _execute(frozen_experiment, tmp_path / "r1_2")

        metrics = json.loads(
            (tmp_path / "r1_2" / "quarterly_metrics.json").read_text(encoding="utf-8")
        )
        assert (
            metrics["net_10bps"]["window"]["total_return"]
            <= metrics["gross"]["window"]["total_return"]
        )
        assert metrics["cost_drag_10bps"] >= 0

    def test_the_research_ledger_records_both_runs_and_hides_nothing(
        self, frozen_experiment: tuple[Any, Path, Path, Path], tmp_path: Path
    ) -> None:
        _execute(frozen_experiment, tmp_path / "r1_2")

        entries = [
            json.loads(line)
            for line in (tmp_path / "r1_2" / "research_ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert len(entries) == 2
        assert all(entry["variants_tried"] == 1 for entry in entries)
        assert all(entry["parameter_sweeps"] == [] for entry in entries)
