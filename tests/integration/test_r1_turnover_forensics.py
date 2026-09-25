"""Adversarial tests for the R1.3 turnover forensics.

R1.3 explains a number it must not be able to change, so each test names a way
that could go wrong — a ledger that moved, a stored metric trusted instead of
rebuilt, a decomposition that does not close, availability churn counted as a
score decision, a buffer width quietly chosen from the outcome, a strategy
simulated under cover of analysis — and asserts the code refuses it or records
it.

The turnover reconstruction is checked against the real frozen record where it
exists, because the whole claim of the phase is that an independent path lands
on the same figure. Everything else runs against a small synthetic record, so
the tests say what the code does rather than what one dataset happens to hold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stock_screener.backtest_harness.registry import digest_document
from stock_screener.signal_review.binding import file_sha256, ledger_fingerprint
from stock_screener.turnover_forensics import (
    CAUSE_BY_EXCLUSION,
    FORBIDDEN_IMPORTS,
    H1_STATUSES,
    MECHANISMS,
    ForensicBindingError,
    bind_r1_2,
    boundary_churn,
    cause_of,
    classify,
    counterfactual,
    cutoffs,
    decomposition,
    h1_status,
    holding_episodes,
    lower_bound,
    membership_half_life,
    next_dimension,
    portfolios_by_decision,
    pre_trade_nav,
    rank_percentiles,
    reconstruct,
    required_reduction,
    retention,
    run_forensics,
    simulation_reachable,
    stability,
    survival_curve,
    threshold_provenance,
    transitions,
)
from stock_screener.turnover_forensics import binding as binding_module

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FROZEN = REPO / ".evidence" / "research"
CONTRACT_SHA = "f7efb001cf79e40680758a3bd29ec2b016ad3d9d5199f363d7ee4f89eb1ad7d1"

#: What R1.2 stored, and what R1.3 must reach without reading it.
STORED_QUARTERLY_ANNUAL = 2.253982668222645
STORED_MONTHLY_ANCHOR = 3.8957629888825784
FROZEN_THRESHOLD = 1.95


def _frozen_available() -> bool:
    """Whether the real frozen record is present in this checkout."""
    return (FROZEN / "r1_2" / "quarterly_trade_ledger.jsonl").is_file()


needs_frozen = pytest.mark.skipif(
    not _frozen_available(), reason="the frozen R1.2 record is runtime data, not committed"
)


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------------------
# The guard that makes this a forensic package rather than another harness
# --------------------------------------------------------------------------


def test_no_simulator_is_importable_from_the_forensic_package() -> None:
    assert simulation_reachable() == []


def test_the_guard_finds_a_simulator_that_was_imported(tmp_path: Path) -> None:
    (tmp_path / "sneaky.py").write_text(
        "from stock_screener.backtest_harness import simulate\n", encoding="utf-8"
    )

    assert simulation_reachable(tmp_path) == ["sneaky.py: simulate"]


def test_the_guard_ignores_a_forbidden_name_that_is_not_imported(tmp_path: Path) -> None:
    (tmp_path / "prose.py").write_text(
        '"""We do not simulate here."""\nMESSAGE = "simulate nothing"\n', encoding="utf-8"
    )

    assert simulation_reachable(tmp_path) == []


def test_every_forbidden_name_names_something_that_could_execute_a_strategy() -> None:
    assert "simulate" in FORBIDDEN_IMPORTS
    assert "run_experiment" in FORBIDDEN_IMPORTS


# --------------------------------------------------------------------------
# Turnover: rebuilt, never trusted
# --------------------------------------------------------------------------


@needs_frozen
def test_quarterly_turnover_reproduces_the_stored_figure_from_ledgers_alone() -> None:
    trades = _read(FROZEN / "r1_2" / "quarterly_trade_ledger.jsonl")
    holdings = _read(FROZEN / "r1_2" / "quarterly_holdings_ledger.jsonl")
    nav = _read(FROZEN / "r1_2" / "quarterly_nav.jsonl")

    built = reconstruct(trades, holdings, label="Q", nominal_per_year=4.0, sessions=len(nav))

    assert built.annual == pytest.approx(STORED_QUARTERLY_ANNUAL, abs=1e-15)
    assert len(built.rebalances) == 17


@needs_frozen
def test_monthly_anchor_reproduces_the_value_the_frozen_criterion_is_built_on() -> None:
    directory = FROZEN / "r1_0" / "run_COMPOUNDER_TOP_DECILE_V1"
    built = reconstruct(
        _read(directory / "trades.jsonl"),
        _read(directory / "holdings.jsonl"),
        label="M",
        nominal_per_year=12.0,
        sessions=len(_read(directory / "nav.jsonl")),
    )

    assert built.annual == pytest.approx(STORED_MONTHLY_ANCHOR, abs=1e-12)


@needs_frozen
def test_the_matched_monthly_window_slices_the_frozen_ledger_and_simulates_nothing() -> None:
    directory = FROZEN / "r1_0" / "run_COMPOUNDER_TOP_DECILE_V1"
    sessions = {str(row["session"]) for row in _read(FROZEN / "r1_2" / "quarterly_nav.jsonl")}

    matched = reconstruct(
        _read(directory / "trades.jsonl"),
        _read(directory / "holdings.jsonl"),
        label="M",
        nominal_per_year=12.0,
        sessions=len(sessions),
        restrict_to=sessions,
    )

    assert {row.execution for row in matched.rebalances} <= sessions
    assert len(matched.rebalances) == 49


@needs_frozen
def test_the_decomposition_closes_and_leaves_nothing_unexplained() -> None:
    built = reconstruct(
        _read(FROZEN / "r1_2" / "quarterly_trade_ledger.jsonl"),
        _read(FROZEN / "r1_2" / "quarterly_holdings_ledger.jsonl"),
        label="Q",
        nominal_per_year=4.0,
        sessions=1023,
    )

    split = decomposition(built)

    assert split["shares_sum"] == pytest.approx(1.0, abs=1e-12)
    assert split["unexplained_notional"] == 0.0
    assert split["ranking_churn_share"] == pytest.approx(0.9379, abs=5e-4)


@needs_frozen
def test_the_membership_floor_stays_above_the_frozen_ceiling() -> None:
    built = reconstruct(
        _read(FROZEN / "r1_2" / "quarterly_trade_ledger.jsonl"),
        _read(FROZEN / "r1_2" / "quarterly_holdings_ledger.jsonl"),
        label="Q",
        nominal_per_year=4.0,
        sessions=1023,
    )

    floor = lower_bound(built, FROZEN_THRESHOLD)

    assert floor["floor_above_threshold"] is True
    assert floor["could_same_selections_satisfy_threshold"] is False
    assert floor["membership_change_floor_annual"] > FROZEN_THRESHOLD


def test_pre_trade_nav_is_recovered_by_the_weight_identity() -> None:
    holdings = [
        {
            "execution": "2022-04-01",
            "kind": "COMPANY",
            "identity_id": 1,
            "market_value": 250.0,
            "actual_weight": 0.25,
        }
    ]

    assert pre_trade_nav(holdings) == {"2022-04-01": 1000.0}


def test_pre_trade_nav_skips_a_zero_weight_holding() -> None:
    holdings = [
        {
            "execution": "2022-04-01",
            "kind": "COMPANY",
            "identity_id": 1,
            "market_value": 0.0,
            "actual_weight": 0.0,
        },
        {
            "execution": "2022-04-01",
            "kind": "COMPANY",
            "identity_id": 2,
            "market_value": 500.0,
            "actual_weight": 0.5,
        },
    ]

    assert pre_trade_nav(holdings) == {"2022-04-01": 1000.0}


def test_turnover_is_one_way_so_replacing_every_holding_scores_one() -> None:
    holdings = [
        {
            "execution": "A",
            "kind": "COMPANY",
            "identity_id": 1,
            "market_value": 100.0,
            "actual_weight": 1.0,
        },
        {
            "execution": "B",
            "kind": "COMPANY",
            "identity_id": 2,
            "market_value": 100.0,
            "actual_weight": 1.0,
        },
    ]
    trades = [
        {
            "execution": "A",
            "side": "BUY",
            "kind": "COMPANY",
            "identity_id": 1,
            "notional": 100.0,
            "month": "m1",
        },
        {
            "execution": "B",
            "side": "SELL",
            "kind": "COMPANY",
            "identity_id": 1,
            "notional": 100.0,
            "month": "m2",
        },
        {
            "execution": "B",
            "side": "BUY",
            "kind": "COMPANY",
            "identity_id": 2,
            "notional": 100.0,
            "month": "m2",
        },
    ]

    built = reconstruct(trades, holdings, label="T", nominal_per_year=4.0, sessions=2)

    assert built.rebalances[1].turnover == pytest.approx(1.0)


def test_a_terminal_leg_stays_outside_the_turnover_numerator() -> None:
    holdings = [
        {
            "execution": "A",
            "kind": "COMPANY",
            "identity_id": 1,
            "market_value": 100.0,
            "actual_weight": 1.0,
        }
    ]
    trades = [
        {
            "execution": "A",
            "side": "BUY",
            "kind": "COMPANY",
            "identity_id": 1,
            "notional": 100.0,
            "month": "m1",
        },
        {
            "execution": "Z",
            "side": "TERMINAL",
            "kind": "COMPANY",
            "identity_id": 1,
            "notional": 50.0,
            "month": "",
        },
    ]

    built = reconstruct(trades, holdings, label="T", nominal_per_year=4.0, sessions=2)

    assert built.rebalances[0].turnover == pytest.approx(0.5)
    assert built.rebalances[-1].terminal == pytest.approx(50.0)


def test_the_counterfactual_separates_frequency_from_per_event_turnover() -> None:
    holdings = [
        {
            "execution": "A",
            "kind": "COMPANY",
            "identity_id": 1,
            "market_value": 100.0,
            "actual_weight": 1.0,
        }
    ]
    trades = [
        {
            "execution": "A",
            "side": "BUY",
            "kind": "COMPANY",
            "identity_id": 1,
            "notional": 80.0,
            "month": "m1",
        }
    ]
    built = reconstruct(trades, holdings, label="Q", nominal_per_year=4.0, sessions=2)

    result = counterfactual(built, 0.2, reference="M")

    assert result["counterfactual_annual_if_per_event_unchanged"] == pytest.approx(0.8)
    assert result["per_rebalance_ratio"] == pytest.approx(built.mean / 0.2)


def test_required_reduction_reports_both_absolute_and_relative_gaps() -> None:
    gap = required_reduction(2.25, 1.95)

    assert gap["absolute_reduction_required"] == pytest.approx(0.30)
    assert gap["relative_reduction_required"] == pytest.approx(0.30 / 2.25)


# --------------------------------------------------------------------------
# Membership: a change of availability is not a decision of the score
# --------------------------------------------------------------------------


def test_an_executable_row_that_merely_ranked_low_is_ranking_churn() -> None:
    assert cause_of({"exclusion": None}) == "RANKING"


def test_a_missing_row_is_not_blamed_on_the_ranking() -> None:
    assert cause_of(None) == "NOT_IN_UNIVERSE"


@pytest.mark.parametrize(
    ("exclusion", "cause"),
    [
        ("NOT_TRADABLE_UNVERIFIED", "TRADABILITY"),
        ("SCORE_INSUFFICIENT_DATA", "SCORE_READINESS"),
        ("NO_EXECUTION_PRICE", "EXECUTION_AVAILABILITY"),
        ("STALE_FUNDAMENTALS", "FEATURE_FRESHNESS"),
    ],
)
def test_each_exclusion_maps_to_an_availability_cause(exclusion: str, cause: str) -> None:
    assert cause_of({"exclusion": exclusion}) == cause


def test_an_unrecognised_exclusion_is_other_rather_than_ranking() -> None:
    assert cause_of({"exclusion": "SOMETHING_NEW"}) == "OTHER"


def test_the_cause_table_never_maps_anything_to_ranking() -> None:
    assert "RANKING" not in set(CAUSE_BY_EXCLUSION.values())


def test_transitions_reconcile_against_the_portfolios_they_describe() -> None:
    portfolios = {
        "d1": {("COMPANY", 1), ("COMPANY", 2)},
        "d2": {("COMPANY", 2), ("COMPANY", 3)},
    }
    rows: dict[str, dict[tuple[str, int], dict[str, Any]]] = {
        "d1": {("COMPANY", 3): {"exclusion": "NOT_TRADABLE_UNVERIFIED"}},
        "d2": {("COMPANY", 1): {"exclusion": None}},
    }

    report = transitions(portfolios, ["d1", "d2"], rows, portfolios)

    assert report["counts"]["IN_TO_IN"] == 1
    assert report["counts"]["IN_TO_OUT_RANKING"] == 1
    assert report["counts"]["OUT_TO_IN_TRADABILITY"] == 1
    assert report["total_exits"] == 1
    assert report["total_entries"] == 1
    assert sum(report["exit_shares"].values()) == pytest.approx(1.0)


def test_retention_uses_the_earlier_portfolio_as_its_denominator() -> None:
    portfolios = {
        "d1": {("COMPANY", 1), ("COMPANY", 2), ("COMPANY", 3), ("COMPANY", 4)},
        "d2": {("COMPANY", 1), ("COMPANY", 5)},
    }

    report = retention(portfolios, ["d1", "d2"])

    assert report["mean"] == pytest.approx(0.25)
    assert report["steps"][0]["retained"] == 1


def test_retention_is_deterministic_across_repeated_calls() -> None:
    portfolios = {
        "d1": {("COMPANY", 1), ("COMPANY", 2)},
        "d2": {("COMPANY", 2), ("COMPANY", 3)},
        "d3": {("COMPANY", 3)},
    }
    order = ["d1", "d2", "d3"]

    first = retention(portfolios, order)
    second = retention(portfolios, order)

    assert digest_document(first) == digest_document(second)


def test_survival_follows_a_portfolio_forward_without_reselecting_it() -> None:
    portfolios = {
        "d1": {("COMPANY", 1), ("COMPANY", 2)},
        "d2": {("COMPANY", 1)},
        "d3": {("COMPANY", 2)},
    }

    curve = survival_curve(portfolios, ["d1", "d2", "d3"], (1, 2))

    assert curve["lag_1"]["mean"] == pytest.approx(0.25)
    assert curve["lag_2"]["mean"] == pytest.approx(0.5)
    assert curve["lag_1"]["observations"] == 2


def test_a_half_life_is_reported_as_unsupported_when_survival_never_halves() -> None:
    curve = {"lag_1": {"mean": 0.9}, "lag_2": {"mean": 0.8}}

    result = membership_half_life(curve, (1, 2))

    assert result["supported"] is False


def test_a_half_life_is_interpolated_between_the_observed_lags() -> None:
    curve = {"lag_1": {"mean": 0.8}, "lag_2": {"mean": 0.6}, "lag_3": {"mean": 0.4}}

    result = membership_half_life(curve, (1, 2, 3))

    assert result["interpolated_half_life"] == pytest.approx(2.5)
    assert result["geometric_from_first_step"] == pytest.approx(3.1063, abs=1e-4)
    assert result["decay_is_slower_than_geometric"] is False


def test_holding_episodes_count_a_return_as_a_second_episode() -> None:
    portfolios = {
        "d1": {("COMPANY", 1)},
        "d2": set(),
        "d3": {("COMPANY", 1)},
    }

    episodes = holding_episodes(portfolios, ["d1", "d2", "d3"])

    assert episodes["episodes"] == 2
    assert episodes["identities"] == 1
    assert episodes["re_entry"]["re_entry_events"] == 1
    assert episodes["re_entry"]["median_decisions_outside_before_return"] == 1


def test_a_continuously_held_position_is_one_episode() -> None:
    portfolios = {"d1": {("COMPANY", 1)}, "d2": {("COMPANY", 1)}}

    episodes = holding_episodes(portfolios, ["d1", "d2"])

    assert episodes["episodes"] == 1
    assert episodes["maximum"] == 2
    assert episodes["re_entry"]["re_entry_events"] == 0


def test_portfolios_group_on_the_requested_field() -> None:
    holdings = [
        {"execution": "e1", "month": "m1", "kind": "COMPANY", "identity_id": 1},
        {"execution": "e2", "month": "m1", "kind": "COMPANY", "identity_id": 2},
    ]

    assert portfolios_by_decision(holdings, key="month") == {"m1": {("COMPANY", 1), ("COMPANY", 2)}}


# --------------------------------------------------------------------------
# Stability: ranks and scores only, never a label
# --------------------------------------------------------------------------


def test_the_stability_modules_read_no_forward_return() -> None:
    for name in ("stability.py", "membership.py", "turnover.py", "forensic.py"):
        source = (REPO / "src" / "stock_screener" / "turnover_forensics" / name).read_text(
            encoding="utf-8"
        )
        assert "load_labels" not in source
        assert "forward_return" not in source


def test_rank_percentiles_put_the_highest_score_first() -> None:
    rows = [
        {"kind": "COMPANY", "identity_id": 1, "final_score": 10.0},
        {"kind": "COMPANY", "identity_id": 2, "final_score": 90.0},
    ]

    percentiles, scores = rank_percentiles(rows)

    assert percentiles[("COMPANY", 2)] == 0.0
    assert percentiles[("COMPANY", 1)] == pytest.approx(0.5)
    assert scores[("COMPANY", 2)] == 90.0


def test_rank_percentiles_drop_an_unscored_row() -> None:
    rows = [
        {"kind": "COMPANY", "identity_id": 1, "final_score": None},
        {"kind": "COMPANY", "identity_id": 2, "final_score": 5.0},
    ]

    percentiles, _ = rank_percentiles(rows)

    assert ("COMPANY", 1) not in percentiles


def test_a_perfectly_stable_ranking_correlates_at_one() -> None:
    ranked = {
        "d1": rank_percentiles(
            [
                {"kind": "COMPANY", "identity_id": 1, "final_score": 1.0},
                {"kind": "COMPANY", "identity_id": 2, "final_score": 2.0},
                {"kind": "COMPANY", "identity_id": 3, "final_score": 3.0},
            ]
        ),
        "d2": rank_percentiles(
            [
                {"kind": "COMPANY", "identity_id": 1, "final_score": 10.0},
                {"kind": "COMPANY", "identity_id": 2, "final_score": 20.0},
                {"kind": "COMPANY", "identity_id": 3, "final_score": 30.0},
            ]
        ),
    }

    result = stability(ranked, ["d1", "d2"], (1,))

    assert result["lag_1"]["mean"] == pytest.approx(1.0)


def test_the_cutoff_is_the_lowest_selected_score() -> None:
    ranked = {
        "d1": rank_percentiles(
            [
                {"kind": "COMPANY", "identity_id": 1, "final_score": 90.0},
                {"kind": "COMPANY", "identity_id": 2, "final_score": 80.0},
                {"kind": "COMPANY", "identity_id": 3, "final_score": 70.0},
            ]
        )
    }

    report = cutoffs(ranked, {"d1": {("COMPANY", 1), ("COMPANY", 2)}})

    assert report["d1"]["cutoff_score"] == 80.0
    assert report["d1"]["highest_score_below_cutoff"] == 70.0
    assert report["d1"]["boundary_gap"] == pytest.approx(10.0)


def test_a_decision_with_no_selection_reports_no_cutoff() -> None:
    ranked = {"d1": rank_percentiles([{"kind": "COMPANY", "identity_id": 1, "final_score": 5.0}])}

    assert cutoffs(ranked, {"d1": set()})["d1"] is None


def test_boundary_churn_bands_a_small_move_separately_from_a_large_one() -> None:
    scores_early = [
        {"kind": "COMPANY", "identity_id": index, "final_score": 100.0 - index}
        for index in range(20)
    ]
    scores_late = [
        {"kind": "COMPANY", "identity_id": index, "final_score": 100.0 - index}
        for index in range(20)
    ]
    scores_late[19]["final_score"] = 200.0
    ranked = {"d1": rank_percentiles(scores_early), "d2": rank_percentiles(scores_late)}
    portfolios = {"d1": {("COMPANY", 0)}, "d2": {("COMPANY", 19)}}

    report = boundary_churn(["d1", "d2"], portfolios, ranked)

    assert report["rank_move_band"]["OVER_20_POINTS"] >= 1
    assert "this_does_not_authorise_a_buffer" in report


# --------------------------------------------------------------------------
# Classification: a working mechanism is not a defect, and no rule is chosen
# --------------------------------------------------------------------------


def _classification(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "decomposition": {
            "share_of_all_traded_notional": {
                "entrants": 0.5,
                "exits": 0.43,
                "resize": 0.06,
                "terminal": 0.01,
            },
            "shares_sum": 1.0,
            "unexplained_notional": 0.0,
        },
        "counterfactual": {
            "per_rebalance_ratio": 1.8,
            "frequency_benefit_offset_share": 0.4,
        },
        "lower_bound": {
            "could_same_selections_satisfy_threshold": False,
            "floor_above_threshold": True,
            "membership_change_floor_annual": 2.12,
            "frozen_threshold": 1.95,
        },
        "transitions": {"exit_shares": {"RANKING": 0.87, "TRADABILITY": 0.13}},
        "quarterly_retention": 0.4577,
        "monthly_three_step_survival": 0.4598,
        "reconciliation_residual": 0.0,
        "accounting_violations": 0,
        "metric_reproduced_exactly": True,
        "reporting_defects": [],
    }
    arguments.update(overrides)
    return arguments


def test_a_mechanism_that_works_as_specified_is_not_called_a_defect() -> None:
    result = classify(**_classification())

    assert result["primary"] == "M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT"
    assert result["implementation_defect"] is False
    assert result["why_not_a_defect"]


def test_a_figure_that_does_not_reproduce_is_called_a_defect() -> None:
    result = classify(**_classification(metric_reproduced_exactly=False))

    assert result["primary"] == "M1_IMPLEMENTATION_OR_ACCOUNTING_DEFECT"
    assert result["implementation_defect"] is True


def test_an_accounting_violation_is_called_a_defect() -> None:
    result = classify(**_classification(accounting_violations=1))

    assert result["accounting_defect"] is True
    assert result["primary"] == "M1_IMPLEMENTATION_OR_ACCOUNTING_DEFECT"


def test_a_decomposition_that_does_not_close_is_called_a_defect() -> None:
    result = classify(**_classification(reconciliation_residual=1.0))

    assert result["primary"] == "M1_IMPLEMENTATION_OR_ACCOUNTING_DEFECT"


def test_every_primary_mechanism_is_one_the_brief_allows() -> None:
    result = classify(**_classification())

    assert result["primary"] in MECHANISMS
    for entry in result["secondary"]:
        assert entry["mechanism"] in MECHANISMS


def test_h1_is_falsified_when_the_floor_exceeds_the_ceiling() -> None:
    mechanism = classify(**_classification())

    status = h1_status(mechanism, _classification()["lower_bound"])

    assert status["status"] == "H1_MECHANISTICALLY_FALSIFIED"
    assert status["confirmed"] is False


def test_h1_is_inconclusive_when_the_floor_sits_below_the_ceiling() -> None:
    floor = {
        "floor_above_threshold": False,
        "membership_change_floor_annual": 1.5,
        "frozen_threshold": 1.95,
    }
    mechanism = classify(**_classification(lower_bound=floor))

    assert h1_status(mechanism, floor)["status"] == "H1_INCONCLUSIVE_MECHANISM"


def test_a_defect_routes_h1_to_the_defect_status() -> None:
    mechanism = classify(**_classification(metric_reproduced_exactly=False))

    status = h1_status(mechanism, _classification()["lower_bound"])

    assert status["status"] == "H1_FAILURE_DUE_TO_IMPLEMENTATION_DEFECT"


def test_no_h1_status_is_confirmation() -> None:
    assert all("CONFIRM" not in status for status in H1_STATUSES)


def test_the_next_step_names_a_dimension_and_chooses_no_number() -> None:
    step = next_dimension(classify(**_classification()))

    assert step["numeric_rule_chosen_here"] is None
    assert step["parameters_selected_from_these_data"] == 0
    assert any("buffer" in item for item in step["withheld_deliberately"])


def test_the_threshold_provenance_records_both_halves_of_the_bound() -> None:
    hypothesis = {
        "falsification_criteria": {
            "criteria": ["annual turnover does not fall by at least half against 3.90x"]
        },
        "premises": {"C_turnover_is_material": {"evidence": {"annual_turnover": 3.9}}},
    }

    provenance = threshold_provenance(hypothesis)

    assert provenance["reduction_factor_class"] == "ARBITRARY_PREREGISTRATION_BOUND"
    assert provenance["reduction_factor_justification_in_record"] is None
    assert provenance["not_altered"] is True


# --------------------------------------------------------------------------
# Binding: the record R1.3 explains cannot have moved
# --------------------------------------------------------------------------


def _synthetic_record(root: Path) -> tuple[Path, Path, Path]:
    """Write a minimal R1.0/R1.1/R1.2 record R1.3 can bind to."""
    r1_0, r1_1, r1_2 = root / "r1_0", root / "r1_1", root / "r1_2"
    for directory in (r1_0, r1_1, r1_2):
        directory.mkdir(parents=True, exist_ok=True)
    (r1_0 / "r1_0_fingerprints.json").write_text(json.dumps({"fingerprints": {}}), encoding="utf-8")
    evaluation = {
        "interpretation": {"outcome": "PARTIALLY_SUPPORTED_BY_R1_2"},
        "criteria": [
            {"number": 1, "met": True},
            {"number": 2, "met": True},
            {"number": 3, "met": False},
            {"number": 4, "met": True},
            {"number": 5, "met": True},
        ],
        "criteria_changed_after_results": False,
    }
    (r1_2 / "r1_2_hypothesis_evaluation.json").write_text(
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (r1_2 / "r1_2_fingerprints.json").write_text(
        json.dumps(
            {
                "fingerprints": {
                    "hypothesis_evaluation": file_sha256(r1_2 / "r1_2_hypothesis_evaluation.json")
                }
            }
        ),
        encoding="utf-8",
    )
    (r1_1 / "r1_1_hypothesis.json").write_text(
        json.dumps({"status": "ACCEPTED_FOR_ONE_EXPERIMENT"}), encoding="utf-8"
    )
    (r1_1 / "r1_2_experiment_spec.json").write_text("{}", encoding="utf-8")
    return r1_0, r1_1, r1_2


def _bind(root: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    r1_0, r1_1, r1_2 = _synthetic_record(root)
    monkeypatch.setattr(
        binding_module, "H1_HYPOTHESIS_SHA256", file_sha256(r1_1 / "r1_1_hypothesis.json")
    )
    monkeypatch.setattr(
        binding_module,
        "R1_2_EXPERIMENT_SPEC_SHA256",
        file_sha256(r1_1 / "r1_2_experiment_spec.json"),
    )
    monkeypatch.setattr(binding_module, "R1_0_LEDGERS", {})
    monkeypatch.setattr(
        binding_module,
        "R1_2_ARTEFACTS",
        {"hypothesis_evaluation": "r1_2_hypothesis_evaluation.json"},
    )
    monkeypatch.setattr(binding_module, "R1_2_LEDGERS", {})
    arguments: dict[str, Any] = {
        "contract_sha256": CONTRACT_SHA,
        "expect_contract_sha256": CONTRACT_SHA,
    }
    arguments.update(overrides)
    return bind_r1_2(r1_0, r1_2, r1_1, **arguments)


def test_a_verified_record_binds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _bind(tmp_path, monkeypatch)

    assert binding.h1_status == "PARTIALLY_SUPPORTED_BY_R1_2"
    assert binding.failed_criterion == "C3_TURNOVER_HALVES"
    assert binding.as_json()["phase_kind"] == "FORENSIC_ANALYSIS"


def test_a_contract_that_moved_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ForensicBindingError, match="contract is"):
        _bind(tmp_path, monkeypatch, contract_sha256="0" * 64)


def test_a_hypothesis_record_that_moved_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    monkeypatch.setattr(binding_module, "H1_HYPOTHESIS_SHA256", "0" * 64)

    with pytest.raises(ForensicBindingError, match="H1 record is"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def test_an_r1_2_artifact_that_moved_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    monkeypatch.setattr(
        binding_module, "H1_HYPOTHESIS_SHA256", file_sha256(r1_1 / "r1_1_hypothesis.json")
    )
    monkeypatch.setattr(
        binding_module,
        "R1_2_EXPERIMENT_SPEC_SHA256",
        file_sha256(r1_1 / "r1_2_experiment_spec.json"),
    )
    monkeypatch.setattr(binding_module, "R1_0_LEDGERS", {})
    monkeypatch.setattr(
        binding_module,
        "R1_2_ARTEFACTS",
        {"hypothesis_evaluation": "r1_2_hypothesis_evaluation.json"},
    )
    monkeypatch.setattr(binding_module, "R1_2_LEDGERS", {})
    (r1_2 / "r1_2_hypothesis_evaluation.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ForensicBindingError, match="hypothesis_evaluation"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def test_a_missing_artifact_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    monkeypatch.setattr(
        binding_module, "H1_HYPOTHESIS_SHA256", file_sha256(r1_1 / "r1_1_hypothesis.json")
    )
    monkeypatch.setattr(
        binding_module,
        "R1_2_EXPERIMENT_SPEC_SHA256",
        file_sha256(r1_1 / "r1_2_experiment_spec.json"),
    )
    monkeypatch.setattr(binding_module, "R1_0_LEDGERS", {})
    monkeypatch.setattr(binding_module, "R1_2_ARTEFACTS", {"absent": "not_here.json"})
    monkeypatch.setattr(binding_module, "R1_2_LEDGERS", {})

    with pytest.raises(ForensicBindingError, match="is missing at"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def test_an_outcome_other_than_partial_support_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    body = json.loads((r1_2 / "r1_2_hypothesis_evaluation.json").read_text(encoding="utf-8"))
    body["interpretation"]["outcome"] = "SUPPORTED_BY_R1_2"
    (r1_2 / "r1_2_hypothesis_evaluation.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _refresh_fingerprints(r1_2)
    _patch(monkeypatch, r1_1)

    with pytest.raises(ForensicBindingError, match=r"R1\.3 explains a partial result"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def test_a_different_failed_criterion_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    body = json.loads((r1_2 / "r1_2_hypothesis_evaluation.json").read_text(encoding="utf-8"))
    body["criteria"][0]["met"] = False
    (r1_2 / "r1_2_hypothesis_evaluation.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _refresh_fingerprints(r1_2)
    _patch(monkeypatch, r1_1)

    with pytest.raises(ForensicBindingError, match="turnover premise"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def test_criteria_changed_after_the_result_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    body = json.loads((r1_2 / "r1_2_hypothesis_evaluation.json").read_text(encoding="utf-8"))
    body["criteria_changed_after_results"] = True
    (r1_2 / "r1_2_hypothesis_evaluation.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _refresh_fingerprints(r1_2)
    _patch(monkeypatch, r1_1)

    with pytest.raises(ForensicBindingError, match="changed after the results"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def test_an_unaccepted_hypothesis_refuses_to_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1_0, r1_1, r1_2 = _synthetic_record(tmp_path)
    (r1_1 / "r1_1_hypothesis.json").write_text(json.dumps({"status": "DRAFT"}), encoding="utf-8")
    _patch(monkeypatch, r1_1)

    with pytest.raises(ForensicBindingError, match="not the accepted one"):
        bind_r1_2(
            r1_0,
            r1_2,
            r1_1,
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
        )


def _refresh_fingerprints(r1_2: Path) -> None:
    (r1_2 / "r1_2_fingerprints.json").write_text(
        json.dumps(
            {
                "fingerprints": {
                    "hypothesis_evaluation": file_sha256(r1_2 / "r1_2_hypothesis_evaluation.json")
                }
            }
        ),
        encoding="utf-8",
    )


def _patch(monkeypatch: pytest.MonkeyPatch, r1_1: Path) -> None:
    monkeypatch.setattr(
        binding_module, "H1_HYPOTHESIS_SHA256", file_sha256(r1_1 / "r1_1_hypothesis.json")
    )
    monkeypatch.setattr(
        binding_module,
        "R1_2_EXPERIMENT_SPEC_SHA256",
        file_sha256(r1_1 / "r1_2_experiment_spec.json"),
    )
    monkeypatch.setattr(binding_module, "R1_0_LEDGERS", {})
    monkeypatch.setattr(
        binding_module,
        "R1_2_ARTEFACTS",
        {"hypothesis_evaluation": "r1_2_hypothesis_evaluation.json"},
    )
    monkeypatch.setattr(binding_module, "R1_2_LEDGERS", {})


# --------------------------------------------------------------------------
# End to end, against the frozen record, leaving it untouched
# --------------------------------------------------------------------------


@needs_frozen
def test_the_phase_runs_end_to_end_and_reaches_the_frozen_verdict(tmp_path: Path) -> None:
    before = {path.name: file_sha256(path) for path in sorted((FROZEN / "r1_2").glob("*.json"))}

    result = run_forensics(
        tmp_path / "out",
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )

    assert result.status["status"] == "H1_MECHANISTICALLY_FALSIFIED"
    assert result.mechanism["primary"] == (
        "M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT"
    )
    assert result.summary["numeric_rule_chosen"] is None
    assert result.summary["holdout_upgraded"] is False
    after = {path.name: file_sha256(path) for path in sorted((FROZEN / "r1_2").glob("*.json"))}
    assert after == before, "R1.3 must not mutate the record it explains"


@needs_frozen
def test_two_runs_agree_on_every_fingerprint(tmp_path: Path) -> None:
    runs = [
        run_forensics(
            tmp_path / f"run{index}",
            FROZEN / "r1_0",
            FROZEN / "r1_1",
            FROZEN / "r1_2",
            contract_sha256=CONTRACT_SHA,
            expect_contract_sha256=CONTRACT_SHA,
            code_commit="0" * 40,
        )
        for index in range(2)
    ]

    assert runs[0].fingerprints == runs[1].fingerprints
    assert len(runs[0].fingerprints) == 11


@needs_frozen
def test_the_research_ledger_records_a_forensic_analysis_not_an_experiment(
    tmp_path: Path,
) -> None:
    run_forensics(
        tmp_path / "out",
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )

    entry = _read(tmp_path / "out" / "research_ledger.jsonl")[0]

    assert entry["mode"] == "FORENSIC_ANALYSIS"
    assert entry["variants_tried"] == 0
    assert entry["parameter_sweeps"] == []
    assert entry["outcome"]["strategies_simulated"] == 0
    assert entry["outcome"]["new_holdout_strategy_evaluations"] == 0


@needs_frozen
def test_the_evaluation_refuses_to_read_four_of_five_as_a_pass(tmp_path: Path) -> None:
    run_forensics(
        tmp_path / "out",
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    body = json.loads(
        (tmp_path / "out" / "r1_3_h1_forensic_evaluation.json").read_text(encoding="utf-8")
    )

    assert body["aggregate_rule_invented"] is False
    assert body["criteria_unchanged"] is True
    assert body["status_entering_r1_3"] == "PARTIALLY_SUPPORTED_BY_R1_2"
    assert body["holdout_status"]["upgraded"] is False
    assert body["research_protocol"]["new_frequency_variants"] == 0
    assert body["research_protocol"]["strategies_simulated"] == 0


@needs_frozen
def test_quarterly_retention_matches_the_monthly_three_month_survival(tmp_path: Path) -> None:
    run_forensics(
        tmp_path / "out",
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    body = json.loads(
        (tmp_path / "out" / "r1_3_membership_transitions.json").read_text(encoding="utf-8")
    )
    comparison = body["retention_comparison"]

    assert abs(comparison["difference"]) < 0.01
    assert comparison["monthly_three_months_forward"] == pytest.approx(
        comparison["premise_b_recorded_value"]
    )


@needs_frozen
def test_the_executable_population_reproduces_the_r1_1_rank_correlation(
    tmp_path: Path,
) -> None:
    run_forensics(
        tmp_path / "out",
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )
    body = json.loads((tmp_path / "out" / "r1_3_rank_stability.json").read_text(encoding="utf-8"))
    reconciled = body["executable_only"]["reconciles_with_r1_1"]

    assert reconciled["measured_here"] == pytest.approx(
        reconciled["r1_1_month_to_month_rank_correlation"], abs=1e-12
    )


@needs_frozen
def test_the_ledgers_fingerprint_the_same_before_and_after_the_analysis(
    tmp_path: Path,
) -> None:
    ledger = FROZEN / "r1_2" / "quarterly_trade_ledger.jsonl"
    before = ledger_fingerprint(_read(ledger))

    run_forensics(
        tmp_path / "out",
        FROZEN / "r1_0",
        FROZEN / "r1_1",
        FROZEN / "r1_2",
        contract_sha256=CONTRACT_SHA,
        expect_contract_sha256=CONTRACT_SHA,
        code_commit="0" * 40,
    )

    assert ledger_fingerprint(_read(ledger)) == before
