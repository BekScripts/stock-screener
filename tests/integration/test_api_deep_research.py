"""The deep research read surface: validated content only, and nothing else.

`integration`: these run against a real database through the real app, because
the property that matters — no path from an endpoint to a draft or a rejected
claim — is a property of the wiring, not of a function.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from data_access import EXTERNAL_FRESH, DeepResearchReportRepository
from deep_research import (
    DeepBasis,
    DeepClaim,
    DeepConfidence,
    DeepResearchReport,
    DeepSections,
    ExternalEvidence,
    ExternalSourceType,
    SourceTier,
    UnknownReason,
)
from research import ConfidenceLevel, ResearchStatus
from stock_screener.api import app, get_session
from stock_screener.jobs import DEEP_KINDS, JOB_KINDS, MUTATING_KINDS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

    from conftest import Make, Seed


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """Yield a test client whose requests use the in-memory session."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


TICKER = "ACME"
REJECTED = "Investors should buy the shares; our price target is $140."


def _source() -> ExternalEvidence:
    return ExternalEvidence(
        evidence_id="W.news.abc123def456",
        source_type=ExternalSourceType.NEWS,
        tier=SourceTier.TIER_2_REPUTABLE,
        title="Acme signs a multi-year supply agreement",
        publisher="Reuters",
        url="https://reuters.com/business/acme-vertex",
        excerpt="Acme said it had signed a multi-year supply agreement with Vertex.",
        retrieved_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        published_at=date(2026, 8, 12),
        ticker=TICKER,
    )


def _report(*, generated_at: datetime, with_source: bool = True) -> DeepResearchReport:
    """A validated report carrying both kinds of empty section."""
    return DeepResearchReport(
        contract_version="DEEP_RESEARCH_V1",
        prompt_version="DEEP_RESEARCH_PROMPT_V2/claude-sonnet-5",
        model_id="claude-sonnet-5",
        generated_at=generated_at,
        ticker=TICKER,
        as_of=date(2026, 6, 30),
        score_version="COMPOUNDER_V1_1",
        deterministic_fingerprint="d" * 64,
        evidence_fingerprint="e" * 64,
        status=ResearchStatus.PARTIAL,
        confidence=DeepConfidence(
            level=ConfidenceLevel.MEDIUM,
            ceiling=ConfidenceLevel.MEDIUM,
            claimed=ConfidenceLevel.MEDIUM,
            metric_coverage=0.9,
        ),
        sections=DeepSections(
            recent_developments=(
                DeepClaim(
                    text="Acme signed a supply agreement.",
                    basis=DeepBasis.EXTERNAL,
                    evidence=("W.news.abc123def456",),
                ),
            ),
            catalysts=(
                DeepClaim(
                    text="The available evidence does not address this section.",
                    basis=DeepBasis.UNKNOWN,
                    unknown_reason=UnknownReason.NO_EVIDENCE,
                ),
            ),
            research_conclusion=(
                DeepClaim(
                    text="Evidence is available, but no statement about it passed validation.",
                    basis=DeepBasis.UNKNOWN,
                    unknown_reason=UnknownReason.NO_VALID_CLAIMS,
                ),
            ),
        ),
        external_evidence=(_source(),) if with_source else (),
        unknowns=("catalysts", "research_conclusion"),
        issues=(),
    )


@pytest.fixture
def researched(session: Session, make: type[Make], seed: type[Seed]) -> int:
    """A company with one stored deep research report."""
    company_id = seed.company(session, make, TICKER)
    seed.score(session, company_id, TICKER)
    DeepResearchReportRepository(session).save(
        company_id,
        _report(generated_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC)),
        external_state=EXTERNAL_FRESH,
        external_collected_at=datetime(2026, 8, 18, 11, 0, tzinfo=UTC),
        collected_external=[_source().model_dump(mode="json")],
    )
    session.commit()
    return company_id


@pytest.mark.integration
def test_the_latest_validated_report_is_served(client: TestClient, researched: int) -> None:
    response = client.get(f"/api/deep-research/{TICKER}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PARTIAL"
    assert len(body["sections"]) == 17
    assert body["external_state"] == EXTERNAL_FRESH


@pytest.mark.integration
def test_no_draft_or_rejected_text_is_reachable(client: TestClient, researched: int) -> None:
    """There is no endpoint that returns one, because nothing stores one."""
    body = client.get(f"/api/deep-research/{TICKER}").text

    assert REJECTED not in body
    assert "price target" not in body
    assert "raw_content" not in body


@pytest.mark.integration
def test_issues_carry_codes_but_never_the_rejected_text(
    client: TestClient, session: Session, make: type[Make], seed: type[Seed]
) -> None:
    """A safe code is enough to show something was dropped; the text is not safe."""
    from deep_research import DeepIssueCode, DeepValidationIssue

    company_id = seed.company(session, make, "ISSU")
    seed.score(session, company_id, "ISSU")
    report = _report(generated_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC)).model_copy(
        update={
            "ticker": "ISSU",
            "issues": (
                DeepValidationIssue(
                    code=DeepIssueCode.INVESTMENT_ADVICE,
                    detail=f"recommendation language: {REJECTED!r}",
                ),
            ),
        }
    )
    DeepResearchReportRepository(session).save(company_id, report)
    session.commit()

    body = client.get("/api/deep-research/ISSU").json()

    assert body["issues"] == [{"code": "INVESTMENT_ADVICE", "section": None}]
    assert REJECTED not in client.get("/api/deep-research/ISSU").text


@pytest.mark.integration
def test_both_unknown_reasons_are_serialised(client: TestClient, researched: int) -> None:
    """A UI must read these structurally, never by parsing the fallback sentence."""
    body = client.get(f"/api/deep-research/{TICKER}").json()

    assert body["unknown_reasons"]["catalysts"] == "NO_EVIDENCE"
    assert body["unknown_reasons"]["research_conclusion"] == "NO_VALID_CLAIMS"
    sections = {section["key"]: section for section in body["sections"]}
    assert sections["catalysts"]["unknown_reason"] == "NO_EVIDENCE"
    assert sections["research_conclusion"]["unknown_reason"] == "NO_VALID_CLAIMS"


@pytest.mark.integration
def test_source_metadata_is_returned_for_cited_evidence(
    client: TestClient, researched: int
) -> None:
    body = client.get(f"/api/deep-research/{TICKER}").json()

    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert source["publisher"] == "Reuters"
    assert source["tier"] == "TIER_2_REPUTABLE"
    assert source["published_at"] == "2026-08-12"
    assert source["url"].startswith("https://")


@pytest.mark.integration
def test_a_company_with_no_external_sources_serves_an_empty_list(
    client: TestClient, session: Session, make: type[Make], seed: type[Seed]
) -> None:
    """ACTG's shape: a real report resting on deterministic and SEC evidence."""
    company_id = seed.company(session, make, "NOWB")
    seed.score(session, company_id, "NOWB")
    DeepResearchReportRepository(session).save(
        company_id,
        _report(
            generated_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC), with_source=False
        ).model_copy(update={"ticker": "NOWB"}),
    )
    session.commit()

    body = client.get("/api/deep-research/NOWB").json()

    assert body["sources"] == []
    assert len(body["sections"]) == 17


@pytest.mark.integration
def test_history_is_newest_first_and_retains_older_reports(
    client: TestClient, session: Session, researched: int
) -> None:
    DeepResearchReportRepository(session).save(
        researched, _report(generated_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
    )
    session.commit()

    body = client.get(f"/api/deep-research/{TICKER}/history").json()

    assert len(body) == 2
    assert body[0]["generated_at"] > body[1]["generated_at"]
    assert {"id", "status", "confidence", "external_state"} <= set(body[0])


@pytest.mark.integration
def test_the_latest_endpoint_serves_the_newest_report(
    client: TestClient, session: Session, researched: int
) -> None:
    newest = DeepResearchReportRepository(session).save(
        researched, _report(generated_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
    )
    session.commit()

    assert client.get(f"/api/deep-research/{TICKER}").json()["id"] == newest.id


@pytest.mark.integration
def test_a_historical_report_can_be_opened_by_id(
    client: TestClient, session: Session, researched: int
) -> None:
    first = client.get(f"/api/deep-research/{TICKER}/history").json()[0]

    body = client.get(f"/api/deep-research/reports/{first['id']}").json()

    assert body["id"] == first["id"]
    assert len(body["sections"]) == 17


@pytest.mark.integration
def test_an_unknown_company_is_a_404(client: TestClient) -> None:
    assert client.get("/api/deep-research/GHOST").status_code == 404
    assert client.get("/api/deep-research/GHOST/history").status_code == 404


@pytest.mark.integration
def test_a_company_with_no_report_is_a_404_but_its_history_is_empty(
    client: TestClient, session: Session, make: type[Make], seed: type[Seed]
) -> None:
    """The two say different things: unknown company, versus never researched."""
    seed.company(session, make, "NONE")
    session.commit()

    assert client.get("/api/deep-research/NONE").status_code == 404
    assert client.get("/api/deep-research/NONE/history").json() == []


@pytest.mark.integration
def test_a_missing_report_id_is_a_404(client: TestClient) -> None:
    assert client.get("/api/deep-research/reports/99999").status_code == 404


@pytest.mark.integration
def test_both_deep_kinds_are_allowlisted_with_fixed_arguments(client: TestClient) -> None:
    """The caller picks a key, never an argument."""
    assert JOB_KINDS["deep-research"].args == ("deep-research", "run")
    assert JOB_KINDS["deep-research-refresh"].args == (
        "deep-research",
        "run",
        "--refresh-external",
    )
    for kind in DEEP_KINDS:
        assert JOB_KINDS[kind].needs_target
        assert JOB_KINDS[kind].spends_money


@pytest.mark.integration
def test_the_deep_kinds_are_guarded_per_ticker_not_against_the_pipeline(
    client: TestClient,
) -> None:
    """Deep research writes only its own company's report, so it blocks nothing else."""
    assert not (DEEP_KINDS & MUTATING_KINDS)


@pytest.mark.integration
def test_the_job_kinds_endpoint_advertises_deep_research(client: TestClient) -> None:
    response = client.get("/api/jobs/kinds")

    assert response.status_code == 200, response.text
    kinds = {kind["kind"] for kind in response.json()["kinds"]}
    assert {"deep-research", "deep-research-refresh"} <= kinds
