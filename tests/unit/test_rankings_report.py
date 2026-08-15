"""Rendering a ranking and an explanation.

The rule under test throughout: a missing value is never rendered as zero. A
blank cell in a spreadsheet is left out of an average; a zero drags it down, and
the manual review these exports exist for would be reading a distortion.
"""

from __future__ import annotations

import csv
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from domain import (
    COMPOUNDER_V1,
    CompanyScore,
    ComponentScore,
    ComponentStatus,
    MetricUnit,
    RiskAssessment,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    SubScore,
    ValuationBasis,
)
from stock_screener.scoring import RankingRow, ScoreDetail
from stock_screener.scoring.report import (
    format_change,
    format_explanation,
    format_observed,
    format_rankings_table,
    format_score,
    write_rankings_csv,
)

if TYPE_CHECKING:
    from pathlib import Path


def _row(**overrides: Any) -> RankingRow:
    """Build a ranking row, overriding only the fields under test."""
    defaults: dict[str, Any] = {
        "rank": 1,
        "ticker": "XYZ",
        "name": "Example Corp",
        "sector": "Technology",
        "scoring_status": "SCORED",
        "final_score": 88.4,
        "raw_score": 93.0,
        "growth_score": 32.1,
        "quality_score": 22.4,
        "valuation_score": 22.7,
        "momentum_score": 15.0,
        "risk_penalty": -4.6,
        "risk_level": "MEDIUM",
        "score_category": "EXCEPTIONAL_RESEARCH_CANDIDATE",
        "data_coverage": 0.91,
        "market_cap": 1_200_000_000.0,
        "revenue_growth_yoy": 0.51,
        "revenue_growth_acceleration": 0.13,
        "enterprise_value": 1_100_000_000.0,
        "valuation_basis": "EV_TO_REVENUE",
        "score_change_7d": 1.2,
        "score_change_30d": 8.0,
        "warnings": ("WEIGHT_REDISTRIBUTED",),
    }
    return RankingRow(**{**defaults, **overrides})


@pytest.mark.unit
def test_the_table_shows_one_line_per_company() -> None:
    table = format_rankings_table([_row(), _row(rank=2, ticker="ABC", final_score=84.7)])

    lines = table.splitlines()
    assert lines[0].startswith("Rank")
    assert "XYZ" in lines[2]
    assert "ABC" in lines[3]


@pytest.mark.unit
def test_an_empty_ranking_says_so_rather_than_printing_a_bare_header() -> None:
    assert "No companies matched." in format_rankings_table([])


@pytest.mark.unit
def test_a_missing_score_change_renders_as_a_dash_not_a_zero() -> None:
    table = format_rankings_table([_row(score_change_30d=None)])

    assert "+0.0" not in table
    assert table.splitlines()[2].rstrip().endswith("-")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"), [(8.0, "+8.0"), (-3.5, "-3.5"), (0.0, "+0.0"), (None, "-")]
)
def test_score_changes_carry_an_explicit_sign(value: float | None, expected: str) -> None:
    assert format_change(value) == expected


@pytest.mark.unit
def test_an_absent_score_renders_as_a_dash() -> None:
    assert format_score(None) == "-"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (0.35, MetricUnit.PERCENT, "35.0%"),
        (0.154, MetricUnit.POINTS, "+15.4pp"),
        (-0.154, MetricUnit.POINTS, "-15.4pp"),
        (3.2, MetricUnit.MULTIPLE, "3.2x"),
        (4.0, MetricUnit.COUNT, "4"),
        (7.5, MetricUnit.MONTHS, "7.5 months"),
        (1_200_000_000.0, MetricUnit.MONEY, "$1.2B"),
        (None, MetricUnit.PERCENT, "-"),
    ],
)
def test_observed_values_are_formatted_by_their_unit(
    value: float | None, unit: MetricUnit, expected: str
) -> None:
    assert format_observed(value, unit) == expected


@pytest.mark.unit
def test_the_csv_contains_every_ranking_column(tmp_path: Path) -> None:
    path = tmp_path / "rankings.csv"

    written = write_rankings_csv([_row()], path)

    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert written == 1
    assert rows[0]["ticker"] == "XYZ"
    assert rows[0]["final_score"] == "88.4"
    assert rows[0]["revenue_growth"] == "0.51"
    assert rows[0]["valuation_basis"] == "EV_TO_REVENUE"
    assert rows[0]["warnings"] == "WEIGHT_REDISTRIBUTED"


@pytest.mark.unit
def test_a_missing_csv_value_is_an_empty_cell(tmp_path: Path) -> None:
    path = tmp_path / "rankings.csv"

    write_rankings_csv([_row(enterprise_value=None, score_change_30d=None)], path)

    with path.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["enterprise_value"] == ""
    assert row["score_change_30d"] == ""


@pytest.mark.unit
def test_the_csv_creates_its_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "exports" / "rankings.csv"

    write_rankings_csv([_row()], path)

    assert path.is_file()


def _score() -> CompanyScore:
    """A scored company with one component of each kind of sub-score."""
    growth = ComponentScore(
        name="growth",
        status=ComponentStatus.SCORED,
        score=30.4,
        max_points=35.0,
        coverage=0.83,
        redistributed=True,
        subscores=(
            SubScore(
                name="revenue_growth",
                points=9.1,
                max_points=12.0,
                observed=0.382,
                unit=MetricUnit.PERCENT,
            ),
            SubScore(
                name="revenue_cagr_3y",
                points=None,
                max_points=6.0,
                observed=None,
                unit=MetricUnit.PERCENT,
            ),
        ),
    )
    return CompanyScore(
        ticker="XYZ",
        score_version=COMPOUNDER_V1,
        status=ScoringStatus.SCORED,
        growth=growth,
        raw_score=93.0,
        risk=RiskAssessment(
            dilution_penalty=-4.6,
            runway_penalty=0.0,
            balance_sheet_penalty=0.0,
            total_penalty=-4.6,
            level=RiskLevel.MEDIUM,
            coverage=1.0,
            share_count_growth_yoy=0.16,
        ),
        final_score=88.4,
        category=ScoreCategory.EXCEPTIONAL_RESEARCH_CANDIDATE,
        data_coverage=0.91,
        valuation_basis=ValuationBasis.EV_TO_REVENUE,
        warnings=(ScoreWarning.WEIGHT_REDISTRIBUTED,),
    )


def _detail(**overrides: Any) -> ScoreDetail:
    """Build a score detail carrying a stored breakdown."""
    defaults: dict[str, Any] = {
        "ticker": "XYZ",
        "name": "Example Corp",
        "score_date": date(2026, 6, 30),
        "score_version": COMPOUNDER_V1,
        "scoring_status": "SCORED",
        "final_score": 88.4,
        "score_change_7d": 1.2,
        "score_change_30d": 8.0,
        "breakdown": _score().model_dump(mode="json"),
    }
    return ScoreDetail(**{**defaults, **overrides})


@pytest.mark.unit
def test_the_explanation_shows_the_points_and_the_value_behind_them() -> None:
    text = format_explanation(_detail())

    assert "Final score: 88.4 / 100" in text
    assert "Growth: 30.4 / 35" in text
    assert "revenue_growth" in text
    assert "9.1 / 12" in text
    assert "38.2%" in text


@pytest.mark.unit
def test_the_explanation_marks_an_unavailable_metric_as_unavailable() -> None:
    text = format_explanation(_detail())

    assert "unavailable / 6" in text
    assert "revenue_cagr_3y" in text


@pytest.mark.unit
def test_the_explanation_reports_redistribution() -> None:
    assert "weight redistributed" in format_explanation(_detail())


@pytest.mark.unit
def test_the_explanation_shows_the_risk_penalties_separately() -> None:
    text = format_explanation(_detail())

    assert "Risk: -4.6" in text
    assert "MEDIUM" in text
    assert "dilution" in text


@pytest.mark.unit
def test_a_company_without_a_stored_breakdown_still_explains_its_status() -> None:
    text = format_explanation(
        _detail(scoring_status="UNSUPPORTED_SECTOR", final_score=None, breakdown={})
    )

    assert "UNSUPPORTED_SECTOR" in text
    assert "No breakdown was stored" in text


@pytest.mark.unit
def test_an_unreadable_breakdown_does_not_raise() -> None:
    text = format_explanation(_detail(breakdown={"nonsense": True}))

    assert "No breakdown was stored" in text


@pytest.mark.unit
def test_the_csv_records_which_window_the_change_covers(tmp_path: Path) -> None:
    # `improving --window 90` fills the same column, so the window travels with
    # it rather than a 90-day change being read as a 30-day one.
    path = tmp_path / "rankings.csv"

    write_rankings_csv([_row(score_change_window_days=90)], path)

    with path.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["score_change_window_days"] == "90"
