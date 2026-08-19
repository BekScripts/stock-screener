"""Rendering a preparation run for a person at a terminal."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from deep_research import DataFreshness, DeepResearchBrief, MarketRanking
from domain import RiskLevel, ScoreCategory, ScoringStatus
from research import RankingState, ScoreEvidence, ScoreItem
from stock_screener.deep_research import (
    PreparationResult,
    Stage,
    StageOutcome,
    StageState,
    format_preparation,
)

AS_OF = date(2026, 6, 30)
RAN_AT = datetime(2026, 6, 30, 18, 0, tzinfo=UTC)


def _brief(*, freshness: DataFreshness | None = None) -> DeepResearchBrief:
    """A scored company with one score line and no external evidence."""
    return DeepResearchBrief(
        ticker="ACME",
        name="Acme Corporation",
        as_of=AS_OF,
        assembled_at=RAN_AT,
        score=ScoreEvidence(
            score_version="COMPOUNDER_V1_1",
            score_date=AS_OF,
            scoring_status=ScoringStatus.SCORED,
            final_score=77.4,
            raw_score=82.0,
            risk_level=RiskLevel.MEDIUM,
            category=ScoreCategory.STRONG_RESEARCH_CANDIDATE,
            data_coverage=0.85,
            ranking_state=RankingState.FINAL,
            items=(ScoreItem(id="S.growth", label="Growth", points=30.4, max_points=35.0),),
        ),
        ranking=MarketRanking(rank=12, universe_size=4300, percentile=0.997),
        freshness=freshness or DataFreshness(price_as_of=AS_OF),
    )


def _result(*outcomes: StageOutcome) -> PreparationResult:
    """A preparation record carrying the given outcomes."""
    return PreparationResult(
        ticker="ACME",
        company_id=1,
        started_at=RAN_AT,
        finished_at=RAN_AT,
        outcomes=outcomes,
    )


@pytest.mark.unit
def test_shows_the_score_and_where_it_ranks() -> None:
    rendered = format_preparation(
        _result(StageOutcome(Stage.SCORE, StageState.REFRESHED)), _brief()
    )

    assert "SCORED" in rendered
    assert "77.40" in rendered
    assert "12 of 4300" in rendered


@pytest.mark.unit
def test_shows_every_stage_and_what_it_did() -> None:
    rendered = format_preparation(
        _result(
            StageOutcome(Stage.MARKET_DATA, StageState.REFRESHED, "rows=42"),
            StageOutcome(Stage.FUNDAMENTALS, StageState.REUSED, "already current"),
        ),
        _brief(),
    )

    assert "market_data: refreshed — rows=42" in rendered
    assert "fundamentals: reused — already current" in rendered


@pytest.mark.unit
def test_shows_the_three_fingerprints() -> None:
    brief = _brief()

    rendered = format_preparation(_result(), brief)

    assert brief.deterministic_fingerprint() in rendered
    assert brief.external_fingerprint() in rendered
    assert brief.evidence_fingerprint() in rendered


@pytest.mark.unit
def test_reports_no_external_evidence_in_phase_6b() -> None:
    rendered = format_preparation(_result(), _brief())

    assert "external (W.)  0" in rendered


@pytest.mark.unit
def test_lists_what_remains_stale() -> None:
    freshness = DataFreshness(
        price_as_of=AS_OF, stale=("fundamentals: degraded — provider failed for ACME",)
    )

    rendered = format_preparation(_result(), _brief(freshness=freshness))

    assert "provider failed for ACME" in rendered


@pytest.mark.unit
def test_an_unranked_company_says_so_rather_than_showing_a_position() -> None:
    brief = _brief().model_copy(update={"ranking": MarketRanking(rank=None, universe_size=4300)})

    rendered = format_preparation(_result(), brief)

    assert "unranked, of 4300 scored" in rendered


@pytest.mark.unit
def test_a_summary_counts_stage_states() -> None:
    result = _result(
        StageOutcome(Stage.MARKET_DATA, StageState.REFRESHED),
        StageOutcome(Stage.FUNDAMENTALS, StageState.REUSED),
        StageOutcome(Stage.FILINGS, StageState.DEGRADED, "down"),
    )

    assert "refreshed=1" in result.summary()
    assert "degraded=1" in result.summary()
