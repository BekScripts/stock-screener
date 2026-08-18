"""Reaching a company that is not near the top of a ranking, and taking a view away.

The rankings cap at 500 rows over a universe of thousands, so search is the only
route to most companies. Export is the same rows the screen shows, in a file —
built by the API for the reason everything else is, that a file assembled
client-side could disagree with the page it came from.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from data_access import CompanyRepository
from domain import CompanyProfile
from stock_screener.api import app, get_session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """Yield a test client whose requests use the in-memory session."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def companies(session: Session) -> None:
    """Store companies with deliberately overlapping tickers and names.

    `MU` and `MUX` both start with the fragment, and `Micron` and `Microsoft`
    both contain it — which is what makes the ordering worth asserting on.
    """
    repository = CompanyRepository(session)
    for ticker, name in [
        ("MU", "Micron Technology"),
        ("MUX", "McEwen Mining"),
        ("AAPL", "Apple"),
        ("MSFT", "Microsoft Corporation"),
    ]:
        repository.upsert_profile(CompanyProfile(ticker=ticker, name=name))
    session.flush()


@pytest.mark.integration
@pytest.mark.usefixtures("companies")
def test_an_exact_ticker_ranks_first(client: TestClient) -> None:
    hits = client.get("/api/companies/search?q=MU").json()["hits"]

    assert hits[0]["ticker"] == "MU"


@pytest.mark.integration
@pytest.mark.usefixtures("companies")
def test_search_matches_a_name_as_well_as_a_ticker(client: TestClient) -> None:
    tickers = [hit["ticker"] for hit in client.get("/api/companies/search?q=micro").json()["hits"]]

    assert "MU" in tickers
    assert "MSFT" in tickers


@pytest.mark.integration
@pytest.mark.usefixtures("companies")
def test_search_is_case_insensitive(client: TestClient) -> None:
    hits = client.get("/api/companies/search?q=aapl").json()["hits"]

    assert [hit["ticker"] for hit in hits] == ["AAPL"]


@pytest.mark.integration
@pytest.mark.usefixtures("companies")
def test_an_unscored_company_is_findable_and_says_it_has_no_score(client: TestClient) -> None:
    hit = client.get("/api/companies/search?q=AAPL").json()["hits"][0]

    assert hit["final_score"] is None


@pytest.mark.integration
@pytest.mark.usefixtures("companies")
def test_a_fragment_matching_nothing_returns_no_hits(client: TestClient) -> None:
    assert client.get("/api/companies/search?q=zzzz").json()["hits"] == []


@pytest.mark.integration
def test_an_empty_query_is_rejected(client: TestClient) -> None:
    assert client.get("/api/companies/search?q=").status_code == 422


@pytest.mark.integration
def test_exporting_a_ranking_serves_a_csv_attachment(
    client: TestClient, scored_company: str
) -> None:
    response = client.get("/api/rankings/top/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert scored_company in response.text


@pytest.mark.integration
def test_the_exported_rows_carry_the_stored_score(client: TestClient, scored_company: str) -> None:
    rows = list(csv.DictReader(io.StringIO(client.get("/api/rankings/top/export").text)))

    assert rows[0]["ticker"] == scored_company
    assert rows[0]["final_score"] != ""


@pytest.mark.integration
def test_a_missing_metric_exports_as_an_empty_cell_never_zero(
    client: TestClient, scored_company: str
) -> None:
    rows = list(csv.DictReader(io.StringIO(client.get("/api/rankings/top/export").text)))

    # `score_change_30d` has no prior snapshot to compare against on a first run.
    assert rows[0]["score_change_30d"] == ""


@pytest.mark.integration
def test_an_unknown_ranking_view_is_a_404(client: TestClient) -> None:
    assert client.get("/api/rankings/nonsense/export").status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("view", ["top", "hidden-gems", "wrong-price", "improving"])
def test_every_ranking_view_exports(client: TestClient, view: str) -> None:
    assert client.get(f"/api/rankings/{view}/export").status_code == 200
