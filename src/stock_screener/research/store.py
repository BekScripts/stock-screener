"""Storing validated reports, and not paying twice for the same reading.

A report is expensive in a way nothing else in this pipeline is: it costs a model
call. The cache key is therefore the whole question the call answers — the
company, the scoring rules the report explains, a hash of every piece of evidence
supplied, and the prompt that turned that evidence into prose. If those four are
unchanged, the stored report *is* the answer, and generating it again would buy a
differently-worded version of the same thing.

A `FAILED` report is stored but never reused. It records that a run tried and
could not, which is worth keeping; treating it as a hit would turn one bad
afternoon at a provider into a permanently missing report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from data_access import CompanyRepository, ResearchReportRepository
from research import ResearchReport, ResearchStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from data_access import StoredResearchReport
    from research import ResearchBrief

log = structlog.get_logger(__name__)

USABLE_STATUSES = frozenset({ResearchStatus.COMPLETE, ResearchStatus.PARTIAL})
"""Statuses a stored report may be reused from.

`PARTIAL` counts. It means claims were dropped, not that the reading failed, and
re-running the same prompt over the same evidence would drop them again.
"""


class ResearchStorageError(RuntimeError):
    """A report was about a company the database does not have."""


def find_cached_report(
    session: Session, brief: ResearchBrief, *, prompt_version: str
) -> ResearchReport | None:
    """Return a stored report for this exact brief and prompt, if one is usable.

    Args:
        session: Open database session.
        brief: The evidence a report would be generated from.
        prompt_version: The prompt that would produce it.

    Returns:
        The stored report, or None when there is none, when the stored one
        failed, or when it can no longer be read under the current contract
        models — which is a stale cache entry rather than a usable answer.
    """
    company = CompanyRepository(session).get_by_ticker(brief.ticker)
    if company is None:
        return None

    row = ResearchReportRepository(session).find(
        company.id,
        score_version=brief.score.score_version,
        brief_fingerprint=brief.fingerprint(),
        prompt_version=prompt_version,
    )
    if row is None:
        return None

    report = _rehydrate(row)
    if report is None or report.status not in USABLE_STATUSES:
        return None

    log.debug("research cache hit", ticker=brief.ticker, status=report.status.value)
    return report


def save_report(session: Session, report: ResearchReport) -> StoredResearchReport:
    """Store one validated report.

    The parameter type is the enforcement: `ResearchReport` is produced by
    `research.validate_report` and `research.failed_report` and by nothing else,
    so unchecked model output cannot reach this function. A `DraftReport` is a
    different type and does not carry the fields this needs.

    Args:
        session: Open database session. The caller commits.
        report: A validated report.

    Returns:
        The stored row.

    Raises:
        ResearchStorageError: If the report names a company that is not stored.
            Silently dropping it would lose a paid-for model call.
    """
    company = CompanyRepository(session).get_by_ticker(report.ticker)
    if company is None:
        raise ResearchStorageError(f"no stored company for research report: {report.ticker}")

    row = ResearchReportRepository(session).save(company.id, report)
    log.info(
        "research report stored",
        ticker=report.ticker,
        status=report.status.value,
        issues=len(report.issues),
    )
    return row


def _rehydrate(row: StoredResearchReport) -> ResearchReport | None:
    """Return a stored row as a report, or None when it no longer parses."""
    try:
        return ResearchReport.model_validate(row.report)
    except ValueError:
        log.warning(
            "stored research report could not be read under the current contract",
            company_id=row.company_id,
            contract_version=row.contract_version,
        )
        return None
