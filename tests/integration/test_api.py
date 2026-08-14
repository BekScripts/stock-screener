"""Tests for the minimal FastAPI surface.

The session dependency is overridden with an in-memory database, which is the
whole reason it is a dependency rather than a module-level engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from data_access import CompanyRepository
from domain import CompanyProfile
from stock_screener.api import app, get_session
from stock_screener.config import Settings
from stock_screener.scanning import update_fundamentals, update_market_data, update_universe

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

    from api_clients import MockFundamentals, MockMarketData
    from conftest import Make

SETTINGS = Settings(environment="test")


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """Yield a test client whose requests use the in-memory session."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def populated(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
    make: type[Make],
) -> Session:
    """Run the pipeline so the endpoints have something to return."""
    market_data, fundamentals = providers
    update_universe(session, market_data)
    session.flush()
    update_market_data(session, market_data, SETTINGS, today=make.latest_session)
    update_fundamentals(session, fundamentals, SETTINGS)
    session.flush()
    return session


@pytest.mark.integration
def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
def test_health_does_not_require_the_database() -> None:
    # An unreachable database must not make an orchestrator restart a process
    # that is working fine, so this deliberately uses no session override.
    with TestClient(app) as bare_client:
        assert bare_client.get("/health").status_code == 200


@pytest.mark.integration
def test_companies_are_listed_in_ticker_order(client: TestClient, session: Session) -> None:
    repo = CompanyRepository(session)
    for ticker in ("ZZZ", "AAA", "MMM"):
        repo.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    session.flush()

    payload = client.get("/api/companies").json()

    assert [row["ticker"] for row in payload] == ["AAA", "MMM", "ZZZ"]


@pytest.mark.integration
def test_the_company_list_respects_the_limit(client: TestClient, session: Session) -> None:
    repo = CompanyRepository(session)
    for ticker in ("AAA", "BBB", "CCC"):
        repo.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    session.flush()

    assert len(client.get("/api/companies", params={"limit": 2}).json()) == 2


@pytest.mark.integration
def test_an_out_of_range_limit_is_rejected(client: TestClient) -> None:
    assert client.get("/api/companies", params={"limit": 0}).status_code == 422
    assert client.get("/api/companies", params={"limit": 10_000}).status_code == 422


@pytest.mark.integration
def test_the_scan_endpoint_returns_summary_counts_and_rows(
    client: TestClient, populated: Session
) -> None:
    payload = client.get("/api/scan").json()

    assert payload["processed"] == 1
    assert payload["eligible"] == 1
    assert payload["rows"][0]["ticker"] == "XYZ"
    assert payload["rows"][0]["eligible"] is True


@pytest.mark.integration
def test_the_scan_endpoint_carries_no_score(client: TestClient, populated: Session) -> None:
    # Scoring is Phase 2. If a `score` key ever appears here without that work
    # being done, something has been faked.
    metrics = client.get("/api/scan").json()["rows"][0]["metrics"]

    assert "score" not in metrics
    assert metrics["revenue_growth_yoy"] is not None


@pytest.mark.integration
def test_excluded_companies_appear_only_when_asked_for(
    client: TestClient, session: Session
) -> None:
    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker="DARK", name="Darkwater Inc", exchange="NYSE")
    )
    session.flush()

    default = client.get("/api/scan").json()
    with_excluded = client.get("/api/scan", params={"include_ineligible": True}).json()

    # No price history means no market cap *and* no liquidity window, so both
    # reasons are reported rather than the screen stopping at the first.
    assert default["rows"] == []
    assert with_excluded["rows"][0]["reasons"] == ["MISSING_REQUIRED_DATA", "LOW_LIQUIDITY"]


@pytest.mark.integration
def test_a_missing_metric_serialises_as_null_not_zero(client: TestClient, session: Session) -> None:
    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker="DARK", name="Darkwater Inc", exchange="NYSE")
    )
    session.flush()

    row = client.get("/api/scan", params={"include_ineligible": True}).json()["rows"][0]

    assert row["metrics"]["revenue_growth_yoy"] is None
    assert row["metrics"]["market_cap"] is None
