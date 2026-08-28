"""Deep research persistence, against a real SQLite database.

`integration`, not `unit`: what these verify is that the table appends where
`research_reports` upserts, and that is a property of the schema rather than of
any Python object.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from data_access import (
    CompanyRepository,
    DeepResearchReportRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from deep_research import (
    DeepBasis,
    DeepClaim,
    DeepConfidence,
    DeepResearchReport,
    DeepSections,
    ExternalEvidence,
    ExternalSourceType,
    SourceTier,
    external_evidence_id,
)
from domain import CompanyProfile
from research import ConfidenceLevel, ResearchStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

AS_OF = date(2026, 8, 14)
ARTICLE_URL = "https://example-news.test/acme-supply-deal"


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as open_session:
        yield open_session
    engine.dispose()


def _company(session: Session, ticker: str = "ACME") -> int:
    """Insert a company and return its primary key."""
    repo = CompanyRepository(session)
    repo.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc", exchange="NASDAQ"))
    session.flush()
    stored = repo.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _article() -> ExternalEvidence:
    """One external source, as a report would carry it."""
    return ExternalEvidence(
        evidence_id=external_evidence_id(ExternalSourceType.NEWS, ARTICLE_URL),
        source_type=ExternalSourceType.NEWS,
        tier=SourceTier.TIER_2_REPUTABLE,
        title="Acme signs multi-year supply agreement",
        publisher="Example Newswire",
        url=ARTICLE_URL,
        excerpt="Acme said on Tuesday it had signed a multi-year supply agreement.",
        retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        published_at=date(2026, 8, 12),
        ticker="ACME",
    )


def _report(
    *,
    deterministic: str = "d" * 64,
    evidence: str = "e" * 64,
    prompt_version: str = "DEEP_PROMPT_V1",
    generated_at: datetime = datetime(2026, 8, 14, 19, 0, tzinfo=UTC),
    status: ResearchStatus = ResearchStatus.COMPLETE,
    text: str = "Acme said it signed a multi-year supply agreement.",
) -> DeepResearchReport:
    """Build a validated report, varying only what a test cares about."""
    article = _article()
    return DeepResearchReport(
        contract_version="DEEP_RESEARCH_V1",
        prompt_version=prompt_version,
        model_id="test-model",
        generated_at=generated_at,
        ticker="ACME",
        as_of=AS_OF,
        score_version="COMPOUNDER_V1_1",
        deterministic_fingerprint=deterministic,
        evidence_fingerprint=evidence,
        status=status,
        confidence=DeepConfidence(
            level=ConfidenceLevel.MEDIUM,
            ceiling=ConfidenceLevel.MEDIUM,
            claimed=ConfidenceLevel.MEDIUM,
            metric_coverage=0.85,
            external_coverage=1.0,
        ),
        sections=DeepSections(
            recent_developments=(
                DeepClaim(text=text, basis=DeepBasis.EXTERNAL, evidence=(article.evidence_id,)),
            )
        ),
        external_evidence=(article,),
        unknowns=("valuation",),
    )


@pytest.mark.integration
def test_saves_and_reads_back_an_identical_report(session: Session) -> None:
    company_id = _company(session)
    report = _report()

    DeepResearchReportRepository(session).save(company_id, report)
    session.flush()

    stored = DeepResearchReportRepository(session).latest_for_company(company_id)
    assert stored is not None
    assert DeepResearchReport.model_validate(stored.validated_report_json) == report


@pytest.mark.integration
def test_stores_the_queryable_columns_alongside_the_document(session: Session) -> None:
    company_id = _company(session)

    row = DeepResearchReportRepository(session).save(company_id, _report())

    assert row.ticker == "ACME"
    assert row.as_of == AS_OF
    assert row.score_version == "COMPOUNDER_V1_1"
    assert row.deterministic_fingerprint == "d" * 64
    assert row.evidence_fingerprint == "e" * 64
    assert row.status == ResearchStatus.COMPLETE.value
    assert row.confidence == ConfidenceLevel.MEDIUM.value


@pytest.mark.integration
def test_a_stored_report_carries_the_sources_its_claims_cite(session: Session) -> None:
    """A `W.` citation must still resolve after the brief is gone."""
    company_id = _company(session)

    row = DeepResearchReportRepository(session).save(company_id, _report())
    session.flush()

    restored = DeepResearchReport.model_validate(row.validated_report_json)
    carried = {item.evidence_id for item in restored.external_evidence}
    assert restored.cited_external_ids
    assert restored.cited_external_ids <= carried
    assert restored.external_evidence[0].publisher == "Example Newswire"


@pytest.mark.integration
def test_saving_the_same_cache_key_twice_keeps_both_reports(session: Session) -> None:
    """The deliberate divergence from Phase 3: this table appends."""
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)

    repo.save(company_id, _report(generated_at=datetime(2026, 8, 14, 19, 0, tzinfo=UTC)))
    repo.save(company_id, _report(generated_at=datetime(2026, 9, 14, 19, 0, tzinfo=UTC)))
    session.flush()

    assert repo.count() == 2


@pytest.mark.integration
def test_the_older_report_survives_a_later_run(session: Session) -> None:
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)

    repo.save(
        company_id,
        _report(
            generated_at=datetime(2026, 8, 14, 19, 0, tzinfo=UTC), text="The first conclusion."
        ),
    )
    repo.save(
        company_id,
        _report(
            generated_at=datetime(2026, 9, 14, 19, 0, tzinfo=UTC), text="The second conclusion."
        ),
    )
    session.flush()

    history = repo.history_for_company(company_id)
    said = {
        DeepResearchReport.model_validate(row.validated_report_json)
        .sections.recent_developments[0]
        .text
        for row in history
    }
    assert said == {"The first conclusion.", "The second conclusion."}


@pytest.mark.integration
def test_history_is_returned_newest_first(session: Session) -> None:
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)

    repo.save(company_id, _report(generated_at=datetime(2026, 8, 14, 19, 0, tzinfo=UTC)))
    repo.save(company_id, _report(generated_at=datetime(2026, 9, 14, 19, 0, tzinfo=UTC)))
    session.flush()

    history = repo.history_for_company(company_id)

    assert [row.generated_at for row in history] == sorted(
        (row.generated_at for row in history), reverse=True
    )


@pytest.mark.integration
def test_the_cache_finds_the_newest_report_for_a_key(session: Session) -> None:
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)

    repo.save(company_id, _report(generated_at=datetime(2026, 8, 14, 19, 0, tzinfo=UTC)))
    newest = repo.save(company_id, _report(generated_at=datetime(2026, 9, 14, 19, 0, tzinfo=UTC)))
    session.flush()

    found = repo.find_cached(
        company_id,
        deterministic_fingerprint="d" * 64,
        evidence_fingerprint="e" * 64,
        prompt_version="DEEP_PROMPT_V1",
    )

    assert found is not None
    assert found.id == newest.id


@pytest.mark.integration
def test_changed_external_evidence_is_a_cache_miss(session: Session) -> None:
    """The fundamentals are unchanged, the news is not, and the report is stale."""
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)
    repo.save(company_id, _report())
    session.flush()

    found = repo.find_cached(
        company_id,
        deterministic_fingerprint="d" * 64,
        evidence_fingerprint="f" * 64,
        prompt_version="DEEP_PROMPT_V1",
    )

    assert found is None


@pytest.mark.integration
def test_changed_deterministic_evidence_is_a_cache_miss(session: Session) -> None:
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)
    repo.save(company_id, _report())
    session.flush()

    found = repo.find_cached(
        company_id,
        deterministic_fingerprint="c" * 64,
        evidence_fingerprint="e" * 64,
        prompt_version="DEEP_PROMPT_V1",
    )

    assert found is None


@pytest.mark.integration
def test_a_different_prompt_version_is_a_cache_miss(session: Session) -> None:
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)
    repo.save(company_id, _report())
    session.flush()

    found = repo.find_cached(
        company_id,
        deterministic_fingerprint="d" * 64,
        evidence_fingerprint="e" * 64,
        prompt_version="DEEP_PROMPT_V2",
    )

    assert found is None


@pytest.mark.integration
def test_another_companys_report_is_not_a_cache_hit(session: Session) -> None:
    mine = _company(session, "ACME")
    theirs = _company(session, "OTHR")
    repo = DeepResearchReportRepository(session)
    repo.save(mine, _report())
    session.flush()

    found = repo.find_cached(
        theirs,
        deterministic_fingerprint="d" * 64,
        evidence_fingerprint="e" * 64,
        prompt_version="DEEP_PROMPT_V1",
    )

    assert found is None


@pytest.mark.integration
def test_a_failed_report_is_stored_rather_than_swallowed(session: Session) -> None:
    """A run that could not produce a report is a fact worth keeping."""
    company_id = _company(session)

    row = DeepResearchReportRepository(session).save(
        company_id, _report(status=ResearchStatus.FAILED)
    )

    assert row.status == ResearchStatus.FAILED.value


@pytest.mark.integration
def test_latest_for_company_is_none_before_anything_is_researched(session: Session) -> None:
    company_id = _company(session)

    assert DeepResearchReportRepository(session).latest_for_company(company_id) is None


@pytest.mark.integration
def test_validation_issues_are_stored_beside_the_document(session: Session) -> None:
    company_id = _company(session)

    row = DeepResearchReportRepository(session).save(company_id, _report())

    assert row.validation_issues_json == []


@pytest.mark.integration
def test_deleting_a_company_removes_its_deep_reports(session: Session) -> None:
    company_id = _company(session)
    repo = DeepResearchReportRepository(session)
    repo.save(company_id, _report())
    session.flush()

    company = CompanyRepository(session).get_by_ticker("ACME")
    assert company is not None
    session.delete(company)
    session.flush()

    assert repo.count() == 0
