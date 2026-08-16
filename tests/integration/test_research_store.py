"""Storing validated reports, and reusing them instead of paying twice.

`integration`: the cache key is a unique constraint, and whether a second run
writes a second row or replaces the first is a property of the database rather
than of the code that calls it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from data_access import ResearchReportRepository
from domain import COMPOUNDER_V1
from research import (
    Basis,
    ConfidenceLevel,
    DraftClaim,
    DraftReport,
    IssueCode,
    ResearchStatus,
    Section,
    failed_report,
    validate_report,
)
from stock_screener.research import (
    ResearchStorageError,
    assemble_brief,
    find_cached_report,
    save_report,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from research import ResearchBrief, ResearchReport
    from stock_screener.config import Settings

GENERATED_AT = datetime(2026, 6, 30, 18, 0, tzinfo=UTC)
PROMPT = "RESEARCH_PROMPT_V1"


@pytest.fixture
def brief(session: Session, settings: Settings, scored_company: str) -> ResearchBrief:
    """A real brief assembled from the seeded company."""
    assembled = assemble_brief(session, settings, scored_company)
    assert assembled is not None
    return assembled


def _draft(text: str = "Growth carried the score.") -> DraftReport:
    """A draft whose one claim cites the growth component."""
    return DraftReport(
        ticker="XYZ",
        claims=(
            DraftClaim(
                section=Section.WHY_IT_RANKED_HIGH,
                text=text,
                basis=Basis.DETERMINISTIC,
                evidence=("S.growth",),
            ),
        ),
        confidence=ConfidenceLevel.MEDIUM,
    )


def _report(brief: ResearchBrief, draft: DraftReport | None = None) -> ResearchReport:
    """Validate a draft against the brief, which is the only way to get a report."""
    return validate_report(
        draft or _draft(),
        brief,
        prompt_version=PROMPT,
        model_id="test-model",
        generated_at=GENERATED_AT,
    )


@pytest.mark.integration
def test_stores_a_validated_report(session: Session, brief: ResearchBrief) -> None:
    row = save_report(session, _report(brief))

    assert row.id is not None
    assert row.brief_fingerprint == brief.fingerprint()
    assert row.prompt_version == PROMPT
    assert row.score_version == brief.score.score_version
    assert ResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_a_stored_report_round_trips(session: Session, brief: ResearchBrief) -> None:
    original = _report(brief)
    save_report(session, original)

    found = find_cached_report(session, brief, prompt_version=PROMPT)

    assert found == original


@pytest.mark.integration
def test_reuses_a_stored_report_for_the_same_brief_and_prompt(
    session: Session, brief: ResearchBrief
) -> None:
    save_report(session, _report(brief))

    assert find_cached_report(session, brief, prompt_version=PROMPT) is not None


@pytest.mark.integration
def test_nothing_stored_is_a_cache_miss(session: Session, brief: ResearchBrief) -> None:
    assert find_cached_report(session, brief, prompt_version=PROMPT) is None


@pytest.mark.integration
def test_a_different_prompt_version_is_a_cache_miss(session: Session, brief: ResearchBrief) -> None:
    save_report(session, _report(brief))

    assert find_cached_report(session, brief, prompt_version="RESEARCH_PROMPT_V2") is None


@pytest.mark.integration
def test_changed_evidence_is_a_cache_miss(
    session: Session,
    settings: Settings,
    make: type[Make],
    seed: type[Seed],
    brief: ResearchBrief,
) -> None:
    save_report(session, _report(brief))

    seed.company(session, make, "XYZ", quarters=8, revenue=333_000_000.0)
    restated = assemble_brief(session, settings, "XYZ")

    assert restated is not None
    assert restated.fingerprint() != brief.fingerprint()
    assert find_cached_report(session, restated, prompt_version=PROMPT) is None


@pytest.mark.integration
def test_a_different_score_version_is_a_cache_miss(
    session: Session,
    settings: Settings,
    make: type[Make],
    seed: type[Seed],
    brief: ResearchBrief,
) -> None:
    save_report(session, _report(brief))

    company_id = seed.company(session, make, "XYZ")
    seed.score(session, company_id, "XYZ", score_version=COMPOUNDER_V1)
    other = assemble_brief(session, settings, "XYZ", score_version=COMPOUNDER_V1)

    assert other is not None
    assert find_cached_report(session, other, prompt_version=PROMPT) is None


@pytest.mark.integration
def test_saving_the_same_report_twice_replaces_the_row(
    session: Session, brief: ResearchBrief
) -> None:
    save_report(session, _report(brief))
    save_report(session, _report(brief))

    assert ResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_a_failed_report_is_stored_but_never_reused(session: Session, brief: ResearchBrief) -> None:
    save_report(
        session,
        failed_report(
            brief,
            prompt_version=PROMPT,
            model_id="test-model",
            generated_at=GENERATED_AT,
            detail="provider returned 429",
        ),
    )

    assert ResearchReportRepository(session).count() == 1
    assert find_cached_report(session, brief, prompt_version=PROMPT) is None


@pytest.mark.integration
def test_a_partial_report_is_good_enough_to_reuse(session: Session, brief: ResearchBrief) -> None:
    report = _report(brief)

    assert report.status is ResearchStatus.PARTIAL
    save_report(session, report)

    assert find_cached_report(session, brief, prompt_version=PROMPT) is not None


@pytest.mark.integration
def test_a_report_about_an_unknown_company_is_refused(
    session: Session, brief: ResearchBrief
) -> None:
    orphan = _report(brief).model_copy(update={"ticker": "GONE"})

    with pytest.raises(ResearchStorageError, match="no stored company"):
        save_report(session, orphan)


@pytest.mark.integration
def test_a_brief_for_a_company_no_longer_stored_is_a_cache_miss(
    session: Session, brief: ResearchBrief
) -> None:
    orphan = brief.model_copy(update={"ticker": "GONE"})

    assert find_cached_report(session, orphan, prompt_version=PROMPT) is None


@pytest.mark.integration
def test_an_unreadable_stored_report_is_a_cache_miss(
    session: Session, brief: ResearchBrief
) -> None:
    row = save_report(session, _report(brief))
    row.report = {"not": "a report"}
    session.flush()

    assert find_cached_report(session, brief, prompt_version=PROMPT) is None


# --- what reaches storage --------------------------------------------------


@pytest.mark.integration
def test_only_validated_content_is_persisted(session: Session, brief: ResearchBrief) -> None:
    # The draft states a figure the brief does not contain. Validation drops the
    # claim, so what lands in the table is the checked version — not what the
    # model said.
    invented = _draft("Growth carried the score, at 41.7% revenue growth.")
    report = _report(brief, invented)

    save_report(session, report)
    stored = find_cached_report(session, brief, prompt_version=PROMPT)

    assert stored is not None
    assert IssueCode.FABRICATED_NUMBER in {issue.code for issue in stored.issues}
    assert all("41.7" not in claim.text for claim in stored.sections.why_it_ranked_high)


@pytest.mark.integration
def test_the_issue_list_is_stored_beside_the_report(session: Session, brief: ResearchBrief) -> None:
    report = _report(brief, _draft("Growth carried the score, at 41.7% revenue growth."))

    row = save_report(session, report)

    assert row.issues
    assert len(row.issues) == len(report.issues)


@pytest.mark.integration
def test_a_draft_is_not_a_report_and_cannot_be_stored(
    session: Session, brief: ResearchBrief
) -> None:
    # The real guarantee is the type signature: `save_report` takes a
    # `ResearchReport`, which only `validate_report` and `failed_report` build,
    # so mypy rejects this at the call site. Asserting it also fails at runtime
    # keeps the guarantee from resting on the type checker alone.
    with pytest.raises(AttributeError):
        save_report(session, _draft())  # type: ignore[arg-type]

    assert ResearchReportRepository(session).count() == 0
