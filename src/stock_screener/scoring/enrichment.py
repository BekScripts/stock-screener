"""Spending a metered provider's requests where they change the answer.

The broad scan runs on Alpaca and EDGAR, which between them cover the whole
market for free. That is enough to rank: prices, filings and a market
capitalisation multiplied out from a cover-page share count. What it cannot give
is a vendor's market cap or **consolidated** volume, and the second of those is
the one that decides whether a company is liquid enough to be worth researching.

So the metered provider is used last and narrowly. The preliminary ranking picks
the few hundred companies whose answers might change; each gets one request; the
rest keep their preliminary state and say so. A quota that runs out mid-pass
stops the pass rather than retrying into a wall — the companies already enriched
stay enriched, and the run reports exactly how far it got.

Enrichment never adjusts a score directly. It replaces *inputs* — market cap,
sector, industry, consolidated volume — and the same formula is then run again
over them. A provider cannot add points by existing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from api_clients import ProviderAuthError, ProviderError, ProviderRateLimitError
from data_access import FINAL, CompanyRepository, ScoreRecord, ScoreSnapshotRepository
from domain import CURRENT_SCORE_VERSION, MARKET_CAP_DISCREPANCY_THRESHOLD
from stock_screener.scoring.engine import build_scores, load_benchmark_returns
from stock_screener.scoring.rankings import top_opportunities

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from api_clients import FundamentalsProvider
    from domain import CompanyProfile
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


class EnrichmentStatus:
    """How much of the intended enrichment actually happened."""

    NOT_RUN = "NOT_RUN"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


@dataclass(slots=True)
class EnrichmentReport:
    """What one enrichment pass did, and what it cost.

    Attributes:
        candidates: Companies selected from the preliminary ranking.
        attempted: Requests actually made.
        succeeded: Requests that returned a profile.
        uncovered: Requests the provider answered with no profile. Coverage gap,
            not failure.
        rate_limited: Requests rejected for quota. The first one ends the pass.
        failed: Requests that failed for any other reason.
        skipped: Candidates never attempted, because the quota ran out first.
        discrepancies: Companies whose provider market cap disagreed materially
            with the calculated one.
        rescored: Snapshots rewritten with enriched inputs.
    """

    candidates: int = 0
    attempted: int = 0
    succeeded: int = 0
    uncovered: int = 0
    rate_limited: int = 0
    failed: int = 0
    skipped: int = 0
    discrepancies: list[str] = field(default_factory=list)
    rescored: int = 0

    @property
    def status(self) -> str:
        """Whether the pass covered every candidate it selected."""
        if self.attempted == 0:
            return EnrichmentStatus.NOT_RUN
        if self.skipped or self.rate_limited or self.failed:
            return EnrichmentStatus.PARTIAL
        return EnrichmentStatus.COMPLETE

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        return (
            f"status={self.status} candidates={self.candidates} attempted={self.attempted} "
            f"succeeded={self.succeeded} uncovered={self.uncovered} "
            f"rate_limited={self.rate_limited} failed={self.failed} skipped={self.skipped} "
            f"rescored={self.rescored}"
        )


def enrich_candidates(
    session: Session,
    provider: FundamentalsProvider,
    settings: Settings,
    *,
    score_date: date | None = None,
    limit: int | None = None,
    score_version: str = CURRENT_SCORE_VERSION,
) -> EnrichmentReport:
    """Enrich the top preliminary candidates and re-score them.

    Args:
        session: Open database session. The caller commits.
        provider: The metered profile source.
        settings: Supplies the enrichment limit and the scoring inputs.
        score_date: The day to enrich. Defaults to the most recent scored day.
        limit: How many candidates to enrich. Defaults to the configured limit.
        score_version: The formula version to read and rewrite.

    Returns:
        What the pass did, including its request accounting.

    Raises:
        ProviderAuthError: If the credentials are rejected. Fatal rather than
            counted: it fails identically for every candidate.
    """
    report = EnrichmentReport()
    repository = ScoreSnapshotRepository(session)
    as_of = score_date or repository.latest_score_date(score_version=score_version)
    if as_of is None:
        log.warning("nothing has been scored yet, so there are no candidates to enrich")
        return report

    wanted = limit if limit is not None else settings.fmp_enrichment_limit
    candidates = top_opportunities(
        session, score_version=score_version, score_date=as_of, limit=wanted
    )
    report.candidates = len(candidates)
    if not candidates:
        return report

    companies = CompanyRepository(session)
    enriched: list[str] = []

    for index, row in enumerate(candidates):
        report.attempted += 1
        try:
            profile = provider.get_company_profile(row.ticker)
        except ProviderAuthError:
            log.error("enrichment halted: the provider rejected the credentials")
            raise
        except ProviderRateLimitError:
            # The quota is a property of the account, not of this symbol, so the
            # next request would fail the same way. Stop and report.
            report.rate_limited += 1
            report.skipped = len(candidates) - index - 1
            log.warning(
                "enrichment stopped: provider quota exhausted",
                enriched=report.succeeded,
                skipped=report.skipped,
            )
            break
        except ProviderError:
            report.failed += 1
            log.exception("enrichment failed", ticker=row.ticker)
            continue

        if profile is None or not _verifies(profile):
            # Either the provider does not cover the symbol, or the profile it
            # returned carries neither of the two figures enrichment exists to
            # obtain. Marking the row FINAL on that would claim a verification
            # that did not happen.
            report.uncovered += 1
            continue

        companies.upsert_profile(profile)
        report.succeeded += 1
        enriched.append(row.ticker)

    session.flush()
    # The profiles were written with a Core upsert, which does not update ORM
    # instances already in the identity map — and the ranking query above loaded
    # exactly those companies. Without this, the re-score could read the market
    # cap the enrichment just replaced.
    session.expire_all()

    if enriched:
        report.rescored = _rescore(session, settings, enriched, as_of, report)

    log.info("enrichment complete", summary=report.summary())
    return report


def _verifies(profile: CompanyProfile) -> bool:
    """Whether a profile carries anything the broad scan could not work out.

    Enrichment buys exactly two things: a market capitalisation quoted rather
    than multiplied out, and volume across every venue rather than one. A
    profile with neither — EDGAR's, for instance — is identity the scan already
    had, and treating it as verification would turn `FINAL` into a label that
    means nothing.
    """
    return profile.market_cap is not None or profile.average_volume is not None


def _rescore(
    session: Session,
    settings: Settings,
    tickers: Sequence[str],
    score_date: date,
    report: EnrichmentReport,
) -> int:
    """Re-run the same formula over the enriched companies and rewrite their rows.

    The scoring rules are untouched; only the inputs changed. A company whose
    consolidated volume turns out to be below the threshold becomes ineligible
    here, which is the whole point of verifying it — the preliminary pass could
    not apply the threshold to single-exchange volume.
    """
    benchmark = load_benchmark_returns(session, settings.benchmark_symbol)
    scored = build_scores(session, settings, benchmark, tickers=tickers)

    for row in scored:
        discrepancy = row.metrics.market_cap_discrepancy
        if discrepancy is not None and discrepancy > MARKET_CAP_DISCREPANCY_THRESHOLD:
            report.discrepancies.append(row.ticker)
            log.warning(
                "market cap disagreement",
                ticker=row.ticker,
                provider=row.metrics.market_cap,
                calculated=row.metrics.calculated_market_cap,
                difference=round(discrepancy, 3),
            )

    written = ScoreSnapshotRepository(session).upsert_scores(
        [
            ScoreRecord(
                row.company_id,
                row.score,
                row.metrics,
                ranking_state=FINAL,
                exclusion_reasons=row.exclusion_reasons,
            )
            for row in scored
        ],
        score_date,
    )
    log.info(
        "enriched candidates re-scored",
        companies=written,
        as_of=str(score_date),
        at=datetime.now(UTC).isoformat(),
    )
    return written
