"""Assembling the four components, the risk penalty and the final score."""

from __future__ import annotations

import pytest

from domain import (
    CURRENT_SCORE_VERSION,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    StatementProfile,
    categorise,
    score_company,
)


@pytest.mark.unit
def test_a_complete_company_is_scored(make) -> None:
    score = score_company(make.profile(), make.metrics(), make.benchmark())

    assert score.status is ScoringStatus.SCORED
    assert score.final_score is not None
    assert score.score_version == CURRENT_SCORE_VERSION


@pytest.mark.unit
def test_the_raw_score_is_the_four_components_summed(make) -> None:
    score = score_company(make.profile(), make.metrics(), make.benchmark())

    components = [score.growth, score.quality, score.valuation, score.momentum]
    total = sum(component.score for component in components if component and component.score)
    assert score.raw_score == pytest.approx(total, abs=0.01)


@pytest.mark.unit
def test_the_final_score_is_the_raw_score_less_the_risk_penalty(make) -> None:
    score = score_company(
        make.profile(), make.metrics(share_count_growth_yoy=0.20), make.benchmark()
    )

    assert score.risk is not None
    assert score.risk.total_penalty == -6.0
    assert score.final_score == pytest.approx((score.raw_score or 0.0) - 6.0, abs=0.01)


@pytest.mark.unit
def test_every_component_stays_inside_its_range(make) -> None:
    score = score_company(make.profile(), make.metrics(), make.benchmark())

    assert score.growth and 0 <= (score.growth.score or 0) <= 35
    assert score.quality and 0 <= (score.quality.score or 0) <= 25
    assert score.valuation and 0 <= (score.valuation.score or 0) <= 25
    assert score.momentum and 0 <= (score.momentum.score or 0) <= 15
    assert 0 <= (score.raw_score or 0) <= 100


@pytest.mark.unit
def test_the_final_score_is_clamped_at_zero(make) -> None:
    weak = make.metrics(
        revenue_growth_yoy=-0.30,
        revenue_growth_acceleration=-0.40,
        revenue_cagr_3y=-0.10,
        gross_profit_growth_yoy=0.0,
        recent_revenue_growth_yoy=(-0.1, -0.2, -0.3, -0.4),
        gross_margin=0.05,
        gross_margin_change=-0.10,
        fcf_margin=-0.60,
        fcf_margin_change=-0.20,
        operating_margin_change=-0.30,
        market_cap=100_000_000.0,
        cash=5_000_000.0,
        debt=200_000_000.0,
        net_cash=-195_000_000.0,
        ttm_free_cash_flow=-60_000_000.0,
        ttm_revenue=4_000_000.0,
        enterprise_value=295_000_000.0,
        share_count_growth_yoy=0.50,
        return_6m=-0.60,
        return_12m=-0.70,
        distance_from_52w_high=-0.75,
    )

    score = score_company(make.profile(), weak, make.benchmark())

    assert score.final_score == 0.0
    assert score.category is ScoreCategory.LOW_PRIORITY


@pytest.mark.unit
def test_dilution_lowers_the_final_score_without_touching_the_growth_score(make) -> None:
    fast_growing = {
        "revenue_growth_yoy": 0.70,
        "revenue_growth_acceleration": 0.25,
        "recent_revenue_growth_yoy": (0.7, 0.6, 0.5, 0.4),
    }
    clean = score_company(
        make.profile(), make.metrics(**fast_growing, share_count_growth_yoy=0.01), make.benchmark()
    )
    dilutive = score_company(
        make.profile(), make.metrics(**fast_growing, share_count_growth_yoy=0.40), make.benchmark()
    )

    assert dilutive.growth is not None and clean.growth is not None
    assert dilutive.growth.score == clean.growth.score
    assert dilutive.raw_score == clean.raw_score
    assert dilutive.risk is not None
    assert dilutive.risk.dilution_penalty == -10.0
    assert dilutive.final_score is not None and clean.final_score is not None
    assert dilutive.final_score == pytest.approx(clean.final_score - 10.0, abs=0.01)


@pytest.mark.unit
def test_an_ineligible_security_is_not_scored(make) -> None:
    score = score_company(make.profile(), make.metrics(), make.benchmark(), eligible=False)

    assert score.status is ScoringStatus.NOT_ELIGIBLE
    assert score.final_score is None
    assert score.growth is None


@pytest.mark.unit
def test_a_bank_is_marked_unsupported_rather_than_scored(make) -> None:
    profile = make.profile(sector="Financial Services", industry="Banks - Regional")

    score = score_company(profile, make.metrics(), make.benchmark())

    assert score.status is ScoringStatus.UNSUPPORTED_SECTOR
    assert score.final_score is None


@pytest.mark.unit
def test_a_bank_shaped_filer_is_unsupported_despite_a_clean_sector_label(make) -> None:
    # Kaspi.kz scored 74.78 and ranked twenty-first on exactly this pair of
    # labels. The statements are what overrule them.
    profile = make.profile(
        sector="Technology",
        industry="Software - Infrastructure",
        statement_profile=StatementProfile.FINANCIAL_INSTITUTION,
    )

    score = score_company(profile, make.metrics(), make.benchmark())

    assert score.status is ScoringStatus.UNSUPPORTED_SECTOR
    assert score.final_score is None


@pytest.mark.unit
def test_the_statement_gate_leaves_an_operating_company_untouched(make) -> None:
    # The gate decides whether a company is scored, never what it scores. A
    # classified-GENERAL profile must produce the identical number to one nobody
    # has classified, or this became a scoring change.
    unclassified = score_company(make.profile(), make.metrics(), make.benchmark())
    classified = score_company(
        make.profile(statement_profile=StatementProfile.GENERAL),
        make.metrics(),
        make.benchmark(),
    )

    assert classified.status is ScoringStatus.SCORED
    assert classified.final_score == unclassified.final_score
    assert classified.raw_score == unclassified.raw_score


@pytest.mark.unit
def test_a_company_missing_a_whole_component_has_no_final_score(make) -> None:
    score = score_company(make.profile(), make.metrics(ttm_revenue=None), make.benchmark())

    assert score.status is ScoringStatus.INSUFFICIENT_DATA
    assert score.final_score is None
    assert score.raw_score is None
    # The breakdown survives, so the reason is visible without re-running.
    assert score.valuation is not None
    assert score.growth is not None and score.growth.score is not None


@pytest.mark.unit
def test_a_company_without_benchmark_returns_is_not_scored(make) -> None:
    score = score_company(make.profile(), make.metrics(), benchmark=None)

    assert score.status is ScoringStatus.INSUFFICIENT_DATA
    assert ScoreWarning.NO_BENCHMARK in score.warnings


@pytest.mark.unit
def test_data_coverage_reports_the_share_of_metrics_available(make) -> None:
    complete = score_company(make.profile(), make.metrics(), make.benchmark())
    partial = score_company(
        make.profile(),
        make.metrics(revenue_cagr_3y=None, ttm_free_cash_flow=None),
        make.benchmark(),
    )

    assert complete.data_coverage == 1.0
    # Six of the hundred points' worth of metrics are missing, plus three.
    assert partial.data_coverage == pytest.approx(0.91)


@pytest.mark.unit
def test_incomplete_data_does_not_by_itself_lower_the_score(make) -> None:
    complete = score_company(make.profile(), make.metrics(), make.benchmark())
    partial = score_company(make.profile(), make.metrics(revenue_cagr_3y=None), make.benchmark())

    assert partial.final_score is not None and complete.final_score is not None
    assert partial.final_score > complete.final_score - 2.0
    assert ScoreWarning.WEIGHT_REDISTRIBUTED in partial.warnings


@pytest.mark.unit
def test_the_valuation_fallback_is_reported_as_a_warning(make) -> None:
    score = score_company(
        make.profile(),
        make.metrics(debt=None, net_cash=None, enterprise_value=None),
        make.benchmark(),
    )

    assert ScoreWarning.VALUATION_FALLBACK_PRICE_TO_SALES in score.warnings


@pytest.mark.unit
def test_risk_warnings_are_carried_onto_the_score(make) -> None:
    score = score_company(
        make.profile(), make.metrics(share_count_growth_yoy=None), make.benchmark()
    )

    assert ScoreWarning.DILUTION_NOT_ASSESSED in score.warnings


@pytest.mark.unit
def test_missing_metrics_are_listed_by_name(make) -> None:
    score = score_company(make.profile(), make.metrics(revenue_cagr_3y=None), make.benchmark())

    assert "revenue_cagr_3y" in score.missing_metrics


@pytest.mark.unit
@pytest.mark.parametrize(
    ("final_score", "expected"),
    [
        (100.0, ScoreCategory.EXCEPTIONAL_RESEARCH_CANDIDATE),
        (85.0, ScoreCategory.EXCEPTIONAL_RESEARCH_CANDIDATE),
        (84.99, ScoreCategory.STRONG_RESEARCH_CANDIDATE),
        (75.0, ScoreCategory.STRONG_RESEARCH_CANDIDATE),
        (74.99, ScoreCategory.WORTH_WATCHING),
        (65.0, ScoreCategory.WORTH_WATCHING),
        (64.99, ScoreCategory.MIXED),
        (50.0, ScoreCategory.MIXED),
        (49.99, ScoreCategory.LOW_PRIORITY),
        (0.0, ScoreCategory.LOW_PRIORITY),
    ],
)
def test_categories_follow_the_final_score(final_score: float, expected: ScoreCategory) -> None:
    assert categorise(final_score) == expected


@pytest.mark.unit
def test_an_excellent_company_scores_near_the_top(make) -> None:
    excellent = make.metrics(
        revenue_growth_yoy=0.60,
        revenue_growth_acceleration=0.25,
        revenue_cagr_3y=0.40,
        gross_profit_growth_yoy=0.70,
        recent_revenue_growth_yoy=(0.6, 0.5, 0.45, 0.4),
        gross_margin=0.72,
        gross_margin_change=0.04,
        fcf_margin=0.25,
        operating_margin_change=0.08,
        market_cap=1_000_000_000.0,
        cash=300_000_000.0,
        debt=0.0,
        net_cash=300_000_000.0,
        ttm_revenue=500_000_000.0,
        enterprise_value=700_000_000.0,
        ttm_free_cash_flow=80_000_000.0,
        share_count_growth_yoy=0.0,
        return_6m=0.45,
        return_12m=0.60,
        distance_from_52w_high=-0.02,
    )

    score = score_company(make.profile(), excellent, make.benchmark())

    assert score.final_score is not None and score.final_score >= 85.0
    assert score.category is ScoreCategory.EXCEPTIONAL_RESEARCH_CANDIDATE
    assert score.risk is not None and score.risk.level is RiskLevel.LOW
