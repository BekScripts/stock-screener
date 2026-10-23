"""Ingest the append-only record of what each filing actually reported.

The ordinary fundamentals pass writes `financial_snapshots`, which holds one row
per period and overwrites it when a filing restates the figure. This pass writes
beside it, keeping every version of every figure with the date it was filed.

The two run from the same fetch. `SecEdgarFundamentals` holds the company-facts
document it last read, so calling this straight after `update-fundamentals` for
the same ticker costs no additional request. Ingesting observations for a ticker
nobody has fetched will, of course, fetch it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from data_access import CompanyRepository, FundamentalObservationRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from domain import FilingObservation

log = structlog.get_logger(__name__)


@runtime_checkable
class FilingObservationSource(Protocol):
    """A provider that can report the individual facts behind its statements.

    Optional, and checked at runtime rather than required of every provider.
    EDGAR publishes a filing date on every fact; a vendor that serves only
    normalised statements cannot, and must not be made to invent one.
    """

    def get_filing_observations(self, ticker: str) -> list[FilingObservation]:
        """Return every reported fact behind this filer's statements."""
        ...


@dataclass(frozen=True)
class ObservationIngestResult:
    """What one ingestion pass recorded.

    Attributes:
        companies: Companies examined.
        observed: Observations the provider returned.
        stored: Observations newly appended. Lower than `observed` on a re-run,
            because a fact already stored is skipped rather than duplicated.
        undated: Observations the source gave no filing date for. Stored, but
            unusable for point-in-time selection — counted so the gap is
            visible rather than silent.
        skipped: Companies the provider could not answer for.
        failures: Tickers whose fetch raised, with the reason. A retry list — a
            count alone tells an operator that something went wrong and not what
            to run again.
        empty: Tickers the provider answered for with nothing. Distinct from a
            failure: the SEC genuinely has no XBRL facts for many symbols, and
            conflating the two would make a normal gap look like an outage.
    """

    companies: int = 0
    observed: int = 0
    stored: int = 0
    undated: int = 0
    skipped: int = 0
    failures: tuple[tuple[str, str], ...] = ()
    empty: tuple[str, ...] = ()

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        return (
            f"{self.companies} companies: {self.stored} new observations "
            f"of {self.observed} seen, {self.undated} without a filing date, "
            f"{len(self.failures)} failed, {len(self.empty)} had no facts"
        )

    def retry_report(self, limit: int = 20) -> str:
        """Return the failures as a list an operator can act on."""
        if not self.failures:
            return "no failures"
        shown = self.failures[:limit]
        lines = [f"{len(self.failures)} failed:"]
        lines.extend(f"  {ticker}: {reason}" for ticker, reason in shown)
        if len(self.failures) > limit:
            lines.append(f"  ... and {len(self.failures) - limit} more")
        return "\n".join(lines)


def _fingerprint(company_id: int, observation: FilingObservation) -> str:
    """Return a stable digest of one observation's identity.

    Deliberately includes the value. Two filings that report the same figure for
    the same period on the same day are the same observation and should collapse;
    a figure that *changed* is a revision and must append, even in the unusual
    case where the filer did not change the accession.
    """
    parts = (
        company_id,
        observation.taxonomy,
        observation.concept,
        observation.unit,
        observation.period_start,
        observation.period_end,
        observation.filed_date,
        observation.accession,
        observation.form,
        observation.value,
    )
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _row(company_id: int, observation: FilingObservation) -> dict[str, Any]:
    """Return the column mapping for one observation."""
    return {
        "observation_fingerprint": _fingerprint(company_id, observation),
        "taxonomy": observation.taxonomy,
        "concept": observation.concept,
        "value": observation.value,
        "unit": observation.unit,
        "period_start": observation.period_start,
        "period_end": observation.period_end,
        "filed_date": observation.filed_date,
        "accession": observation.accession,
        "form": observation.form,
        "fiscal_year": observation.fiscal_year,
        "fiscal_period": observation.fiscal_period,
        "frame": observation.frame,
        "source": observation.source,
        "observed_at": datetime.now(UTC),
    }


def ingest_filing_observations(
    session: Session,
    provider: object,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
) -> ObservationIngestResult:
    """Append what each filing reported, keeping every earlier version.

    Args:
        session: Open database session. The caller commits.
        provider: A fundamentals provider. One that cannot report individual
            facts is not an error — the pass records nothing and says so, which
            is the honest outcome for a source with no filing dates.
        tickers: Restrict to these symbols. Defaults to every stored company.
        limit: Examine at most this many companies, in ticker order.

    Returns:
        What was recorded.
    """
    if not isinstance(provider, FilingObservationSource):
        log.info(
            "fundamentals provider reports no filing observations; "
            "nothing appended to the point-in-time record",
            provider=type(provider).__name__,
        )
        return ObservationIngestResult()

    companies = CompanyRepository(session).list_all()
    wanted = {ticker.upper() for ticker in tickers} if tickers else None
    if wanted is not None:
        companies = [company for company in companies if company.ticker in wanted]
    if limit is not None:
        companies = sorted(companies, key=lambda company: company.ticker)[:limit]

    repository = FundamentalObservationRepository(session)
    companies_seen = observed = stored = undated = 0
    failures: list[tuple[str, str]] = []
    empty: list[str] = []

    for company in companies:
        companies_seen += 1
        try:
            observations = provider.get_filing_observations(company.ticker)
        except Exception as error:
            log.warning(
                "could not read filing observations",
                ticker=company.ticker,
                error=str(error),
            )
            failures.append((company.ticker, f"{type(error).__name__}: {error}"))
            continue

        if not observations:
            empty.append(company.ticker)
            continue

        observed += len(observations)
        undated += sum(1 for item in observations if item.filed_date is None)
        stored += repository.add_all(company.id, [_row(company.id, item) for item in observations])

    result = ObservationIngestResult(
        companies=companies_seen,
        observed=observed,
        stored=stored,
        undated=undated,
        skipped=len(failures) + len(empty),
        failures=tuple(failures),
        empty=tuple(empty),
    )
    log.info(
        "filing observations ingested",
        companies=result.companies,
        observed=result.observed,
        stored=result.stored,
        undated=result.undated,
        failed=len(result.failures),
        empty=len(result.empty),
    )
    return result
