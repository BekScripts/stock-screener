"""The endpoints the dashboard reads, and the one it writes.

Two questions run through these. Does the API serve what the pipeline stored,
unchanged? (It must — a dashboard that recalculated anything could disagree with
the CLI about what a company scored today.) And is the watchlist, the only write
in the whole surface, safe to press twice?
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from data_access import CompanyRepository, WatchlistRepository
from domain import CURRENT_SCORE_VERSION
from research import (
    CURRENT_CONTRACT_VERSION,
    Basis,
    Claim,
    ConfidenceLevel,
    ReportSections,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    Section,
)
from stock_screener.api import app, get_session
from stock_screener.research import save_report

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

    from conftest import Make, Seed

SCORE_DATE = date(2026, 6, 30)


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """Yield a test client whose requests use the in-memory session."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _store_report(session: Session, ticker: str) -> None:
    """Store one validated report so the research endpoint has something to serve."""
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    save_report(
        session,
        ResearchReport(
            contract_version=CURRENT_CONTRACT_VERSION,
            prompt_version="RESEARCH_PROMPT_V1",
            model_id="claude-sonnet-5",
            generated_at=datetime(2026, 6, 30, 12, tzinfo=UTC),
            ticker=ticker,
            score_version=CURRENT_SCORE_VERSION,
            score_date=SCORE_DATE,
            brief_fingerprint="f" * 64,
            status=ResearchStatus.COMPLETE,
            sections=ReportSections.from_mapping(
                {
                    Section.COMPANY_SUMMARY: (
                        Claim(
                            text="The company makes municipal water meters.",
                            basis=Basis.EXTRACTED,
                            evidence=("X.a-1.business",),
                        ),
                    )
                }
            ),
            confidence=ResearchConfidence(
                level=ConfidenceLevel.MEDIUM,
                ceiling=ConfidenceLevel.MEDIUM,
                claimed=ConfidenceLevel.MEDIUM,
                rationale="metric coverage 90%",
            ),
        ),
    )
    session.flush()


# --- stock detail ----------------------------------------------------------


@pytest.mark.integration
def test_the_detail_endpoint_serves_the_stored_score(
    client: TestClient, session: Session, scored_company: str
) -> None:
    body = client.get(f"/api/stocks/{scored_company}").json()

    assert body["ticker"] == scored_company
    assert body["score"]["score_version"] == CURRENT_SCORE_VERSION
    assert body["score"]["breakdown"]["growth"]["max_points"] == 35.0


@pytest.mark.integration
def test_the_detail_endpoint_accepts_a_lowercase_ticker(
    client: TestClient, scored_company: str
) -> None:
    assert client.get(f"/api/stocks/{scored_company.lower()}").status_code == 200


@pytest.mark.integration
def test_the_detail_endpoint_carries_the_displayed_metrics(
    client: TestClient, scored_company: str
) -> None:
    metrics = client.get(f"/api/stocks/{scored_company}").json()["metrics"]

    assert [metric["key"] for metric in metrics][:3] == [
        "revenue_growth_yoy",
        "revenue_growth_acceleration",
        "revenue_cagr_3y",
    ]
    assert all("unit" in metric for metric in metrics)


@pytest.mark.integration
def test_an_unknown_metric_is_null_rather_than_zero(
    client: TestClient, scored_company: str
) -> None:
    # The rule that runs through the whole codebase, at the last surface before a
    # screen: a metric the data cannot support must not arrive as 0.0.
    metrics = {
        m["key"]: m["value"] for m in client.get(f"/api/stocks/{scored_company}").json()["metrics"]
    }

    assert any(value is None for value in metrics.values())
    assert not any(value == 0.0 for key, value in metrics.items() if key == "fcf_margin")


@pytest.mark.integration
def test_an_unstored_company_is_a_404(client: TestClient) -> None:
    assert client.get("/api/stocks/NOPE").status_code == 404


@pytest.mark.integration
def test_a_company_stored_but_never_scored_is_served_with_a_null_score(
    client: TestClient, session: Session, make: type[Make], seed: type[Seed]
) -> None:
    # "Stored but unscored" is a real state with a real explanation, and the
    # detail page exists to show it rather than 404 on it.
    seed.company(session, make, "QQQ")
    session.flush()

    body = client.get("/api/stocks/QQQ").json()

    assert body["score"] is None
    assert body["has_research"] is False


# --- research --------------------------------------------------------------


@pytest.mark.integration
def test_the_research_endpoint_serves_the_validated_report(
    client: TestClient, session: Session, scored_company: str
) -> None:
    _store_report(session, scored_company)

    body = client.get(f"/api/stocks/{scored_company}/research").json()

    assert body["status"] == "COMPLETE"
    assert body["confidence"]["level"] == "MEDIUM"


@pytest.mark.integration
def test_research_arrives_grouped_into_all_thirteen_sections(
    client: TestClient, session: Session, scored_company: str
) -> None:
    _store_report(session, scored_company)

    sections = client.get(f"/api/stocks/{scored_company}/research").json()["sections"]

    assert len(sections) == len(Section)
    assert sections[0]["key"] == Section.COMPANY_SUMMARY.value
    assert sections[0]["claims"][0]["basis"] == "EXTRACTED"


@pytest.mark.integration
def test_research_serves_no_draft_fields(
    client: TestClient, session: Session, scored_company: str
) -> None:
    # The dashboard must have no route to unvalidated output. Only a stored
    # ResearchReport is served, and a draft has no way of becoming one.
    _store_report(session, scored_company)

    body = client.get(f"/api/stocks/{scored_company}/research").json()

    assert "confidence_rationale" not in body
    assert "draft" not in body


@pytest.mark.integration
def test_a_company_without_research_is_a_404(client: TestClient, scored_company: str) -> None:
    assert client.get(f"/api/stocks/{scored_company}/research").status_code == 404


# --- watchlist -------------------------------------------------------------


@pytest.mark.integration
def test_adding_a_company_puts_it_on_the_watchlist(client: TestClient, scored_company: str) -> None:
    assert client.post(f"/api/watchlist/{scored_company}").status_code == 201

    body = client.get("/api/watchlist").json()
    assert [entry["ticker"] for entry in body["entries"]] == [scored_company]
    assert body["entries"][0]["final_score"] is not None


@pytest.mark.integration
def test_adding_twice_is_idempotent(client: TestClient, scored_company: str) -> None:
    client.post(f"/api/watchlist/{scored_company}", json={"note": "first"})
    client.post(f"/api/watchlist/{scored_company}")

    body = client.get("/api/watchlist").json()

    assert body["count"] == 1
    assert body["entries"][0]["note"] == "first"


@pytest.mark.integration
def test_a_note_can_be_stored_and_replaced(client: TestClient, scored_company: str) -> None:
    client.post(f"/api/watchlist/{scored_company}", json={"note": "watch the buyback"})
    client.post(f"/api/watchlist/{scored_company}", json={"note": "watch the separation"})

    entries = client.get("/api/watchlist").json()["entries"]

    assert entries[0]["note"] == "watch the separation"


@pytest.mark.integration
def test_removing_takes_a_company_off_the_list(client: TestClient, scored_company: str) -> None:
    client.post(f"/api/watchlist/{scored_company}")

    assert client.delete(f"/api/watchlist/{scored_company}").json()["removed"] is True
    assert client.get("/api/watchlist").json()["count"] == 0


@pytest.mark.integration
def test_removing_a_company_that_was_never_watched_is_not_an_error(
    client: TestClient, scored_company: str
) -> None:
    response = client.delete(f"/api/watchlist/{scored_company}")

    assert response.status_code == 200
    assert response.json()["removed"] is False


@pytest.mark.integration
def test_watching_an_unknown_company_is_a_404(client: TestClient) -> None:
    assert client.post("/api/watchlist/NOPE").status_code == 404
    assert client.delete("/api/watchlist/NOPE").status_code == 404


@pytest.mark.integration
def test_a_ranking_row_says_whether_the_company_is_watched(
    client: TestClient, scored_company: str
) -> None:
    # Resolved server-side: a dashboard that joined this client-side would show
    # every row unwatched for as long as a second request took.
    before = client.get("/api/rankings").json()["rows"]
    assert all(row["watched"] is False for row in before)

    client.post(f"/api/watchlist/{scored_company}")
    after = client.get("/api/rankings").json()["rows"]

    assert [row["ticker"] for row in after if row["watched"]] == [scored_company]


@pytest.mark.integration
def test_the_detail_endpoint_says_whether_the_company_is_watched(
    client: TestClient, scored_company: str
) -> None:
    assert client.get(f"/api/stocks/{scored_company}").json()["watched"] is False

    client.post(f"/api/watchlist/{scored_company}")

    assert client.get(f"/api/stocks/{scored_company}").json()["watched"] is True


@pytest.mark.integration
def test_a_watched_company_that_lost_its_score_stays_on_the_list(
    client: TestClient, session: Session, make: type[Make], seed: type[Seed]
) -> None:
    # Watching is a judgement, not a ranking position. A company that dropped out
    # of coverage is exactly when remembering it matters.
    seed.company(session, make, "QQQ")
    session.flush()
    client.post("/api/watchlist/QQQ")

    entries = client.get("/api/watchlist").json()["entries"]

    assert entries[0]["ticker"] == "QQQ"
    assert entries[0]["final_score"] is None


@pytest.mark.integration
def test_the_watchlist_holds_one_row_per_company(
    session: Session, scored_company: str, seed: type[Seed]
) -> None:
    company = CompanyRepository(session).get_by_ticker(scored_company)
    assert company is not None
    watchlist = WatchlistRepository(session)

    watchlist.add(company.id)
    watchlist.add(company.id, note="again")

    assert watchlist.count() == 1
