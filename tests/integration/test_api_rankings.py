"""The ranking endpoints, served from stored snapshots.

The API calculates nothing. These tests write score snapshots directly and then
assert the endpoints serve them, which is the contract that keeps the API and
the CLI from ever disagreeing about what a company scored today.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from data_access import CompanyRepository, ScoreRecord, ScoreSnapshotRepository
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyMetrics,
    CompanyProfile,
    CompanyScore,
    ComponentScore,
    ComponentStatus,
    Freshness,
    RiskAssessment,
    RiskLevel,
    ScoreCategory,
    ScoringStatus,
    SubScore,
    ValuationBasis,
)
from stock_screener.api import app, get_session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

TODAY = date(2026, 6, 30)


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """Yield a test client whose requests use the in-memory session."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _component(name: str, score: float, maximum: float) -> ComponentScore:
    """Build a scored component with one sub-score."""
    return ComponentScore(
        name=name,
        status=ComponentStatus.SCORED,
        score=score,
        max_points=maximum,
        coverage=1.0,
        subscores=(
            SubScore(name=f"{name}_metric", points=score, max_points=maximum, observed=0.35),
        ),
    )


def _score(
    ticker: str, *, final: float, growth: float = 30.0, valuation: float = 18.0
) -> CompanyScore:
    """Build a complete score for one company."""
    raw = growth + 21.0 + valuation + 12.0
    return CompanyScore(
        ticker=ticker,
        score_version=CURRENT_SCORE_VERSION,
        status=ScoringStatus.SCORED,
        growth=_component("growth", growth, 35.0),
        quality=_component("quality", 21.0, 25.0),
        valuation=_component("valuation", valuation, 25.0),
        momentum=_component("momentum", 12.0, 15.0),
        raw_score=raw,
        risk=RiskAssessment(total_penalty=round(final - raw, 2), level=RiskLevel.LOW, coverage=1.0),
        final_score=final,
        category=ScoreCategory.STRONG_RESEARCH_CANDIDATE,
        data_coverage=1.0,
        valuation_basis=ValuationBasis.EV_TO_REVENUE,
    )


def _store(
    session: Session,
    ticker: str,
    *,
    final: float,
    score_date: date = TODAY,
    market_cap: float = 2_000_000_000.0,
    growth_metric: float = 0.35,
    freshness: Freshness = Freshness.CURRENT,
    **score_fields: Any,
) -> None:
    """Store one company and one snapshot for it."""
    companies = CompanyRepository(session)
    companies.upsert_profile(
        CompanyProfile(
            ticker=ticker, name=f"{ticker} Corp", exchange="NASDAQ", market_cap=market_cap
        )
    )
    session.flush()
    stored = companies.get_by_ticker(ticker)
    assert stored is not None

    metrics = CompanyMetrics(ticker=ticker, market_cap=market_cap, revenue_growth_yoy=growth_metric)
    ScoreSnapshotRepository(session).upsert_scores(
        [
            ScoreRecord(
                stored.id,
                _score(ticker, final=final, **score_fields),
                metrics,
                freshness=freshness,
            )
        ],
        score_date,
    )
    session.flush()


@pytest.mark.integration
def test_rankings_return_companies_best_first(session: Session, client: TestClient) -> None:
    _store(session, "AAA", final=70.0)
    _store(session, "BBB", final=88.0)

    payload = client.get("/api/rankings").json()

    assert payload["count"] == 2
    assert [row["ticker"] for row in payload["rows"]] == ["BBB", "AAA"]
    assert payload["rows"][0]["rank"] == 1


@pytest.mark.integration
def test_rankings_apply_a_minimum_score(session: Session, client: TestClient) -> None:
    _store(session, "AAA", final=70.0)
    _store(session, "BBB", final=88.0)

    payload = client.get("/api/rankings", params={"min_score": 80}).json()

    assert [row["ticker"] for row in payload["rows"]] == ["BBB"]


@pytest.mark.integration
def test_rankings_are_empty_before_anything_is_scored(client: TestClient) -> None:
    payload = client.get("/api/rankings").json()

    assert payload == {"count": 0, "rows": []}


@pytest.mark.integration
def test_hidden_gems_exclude_the_large_companies(session: Session, client: TestClient) -> None:
    _store(session, "SMALL", final=78.0, market_cap=1_000_000_000.0)
    _store(session, "MEGA", final=88.0, market_cap=90_000_000_000.0)

    payload = client.get("/api/rankings/hidden-gems").json()

    assert [row["ticker"] for row in payload["rows"]] == ["SMALL"]


@pytest.mark.integration
def test_wrong_price_finds_strong_companies_with_poor_valuation_scores(
    session: Session, client: TestClient
) -> None:
    _store(session, "RICH", final=70.0, growth=30.0, valuation=6.0)
    _store(session, "FAIR", final=80.0, growth=30.0, valuation=20.0)

    payload = client.get("/api/rankings/wrong-price").json()

    assert [row["ticker"] for row in payload["rows"]] == ["RICH"]


@pytest.mark.integration
def test_improving_ranks_by_the_change_over_the_window(
    session: Session, client: TestClient
) -> None:
    _store(session, "AAA", final=60.0, score_date=TODAY - timedelta(days=30))
    _store(session, "AAA", final=75.0)
    _store(session, "BBB", final=70.0, score_date=TODAY - timedelta(days=30))
    _store(session, "BBB", final=73.0)

    payload = client.get("/api/rankings/improving").json()

    assert [row["ticker"] for row in payload["rows"]] == ["AAA", "BBB"]
    assert payload["rows"][0]["score_change_30d"] == 15.0


@pytest.mark.integration
def test_a_company_score_includes_its_breakdown(session: Session, client: TestClient) -> None:
    _store(session, "XYZ", final=82.0)

    payload = client.get("/api/companies/xyz/score").json()

    assert payload["ticker"] == "XYZ"
    assert payload["final_score"] == 82.0
    assert payload["score_date"] == TODAY.isoformat()
    assert payload["breakdown"]["growth"]["subscores"][0]["name"] == "growth_metric"


@pytest.mark.integration
def test_an_unscored_company_returns_not_found(client: TestClient) -> None:
    response = client.get("/api/companies/NOPE/score")

    assert response.status_code == 404
    assert "NOPE" in response.json()["detail"]


@pytest.mark.integration
def test_the_ranking_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/api/rankings", params={"limit": 0}).status_code == 422
    assert client.get("/api/rankings", params={"limit": 10_000}).status_code == 422


@pytest.mark.integration
def test_the_rankings_endpoint_excludes_a_stale_score(session: Session, client: TestClient) -> None:
    # The API is where the policy has to hold: a ranking served to a screen is
    # the thing a reader acts on.
    _store(session, "FRESH", final=70.0)
    _store(session, "STALE", final=90.0, freshness=Freshness.STALE)

    body = client.get("/api/rankings").json()

    # Highest score in the table, and absent — because it is not current.
    assert [row["ticker"] for row in body["rows"]] == ["FRESH"]


@pytest.mark.integration
def test_every_ranking_view_endpoint_excludes_a_stale_score(
    session: Session, client: TestClient
) -> None:
    _store(session, "STALE", final=90.0, market_cap=300_000_000.0, freshness=Freshness.STALE)

    for view in ("/api/rankings", "/api/rankings/hidden-gems", "/api/rankings/wrong-price"):
        assert client.get(view).json()["rows"] == [], view


@pytest.mark.integration
def test_the_stock_endpoint_serves_a_stale_score_and_says_so(
    session: Session, client: TestClient
) -> None:
    # Excluded from rankings, not withheld. The number is a real description of
    # the company as it last reported, and the page has to be able to say why no
    # ranking contains it.
    _store(session, "STALE", final=90.0, freshness=Freshness.STALE)

    body = client.get("/api/stocks/STALE").json()

    assert body["score"]["final_score"] == 90.0
    assert body["score"]["scoring_status"] == "SCORED"
    assert body["score"]["freshness"] == "STALE"
    assert body["score"]["rank_eligible"] is False
