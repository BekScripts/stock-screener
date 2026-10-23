"""Ingest when the SEC accepted each filing, so availability can be judged.

The observation record says which day a filing carries. This pass says at which
*instant* it became public, which is the only thing that can decide whether a
decision taken during that day could have seen it.

Reads the submissions index, which publishes `acceptanceDateTime` per accession,
and stores one row per accession. An accession is unique across all filers, so a
filing carrying two hundred facts is stored once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from data_access import (
    CompanyRepository,
    FilingAcceptanceRepository,
    FundamentalObservationRepository,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from domain import AcceptedFiling

log = structlog.get_logger(__name__)


@runtime_checkable
class FilingAcceptanceSource(Protocol):
    """A provider that can report when its filings were accepted.

    Optional and checked at runtime, like the observation source. A vendor
    serving normalised statements has no acceptance clock and must not be made
    to invent one.
    """

    def get_filing_acceptance(self, ticker: str) -> list[AcceptedFiling]:
        """Return when the SEC accepted each of this filer's submissions."""
        ...


@dataclass(frozen=True)
class AcceptanceIngestResult:
    """What one acceptance pass recorded.

    Attributes:
        companies: Companies examined.
        observed: Acceptance records the provider returned.
        stored: Records newly appended, after skipping accessions already known.
        failures: Tickers whose fetch raised, with the reason — a retry list.
        empty: Tickers the provider answered for with nothing.
    """

    companies: int = 0
    observed: int = 0
    stored: int = 0
    failures: tuple[tuple[str, str], ...] = ()
    empty: tuple[str, ...] = ()

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        return (
            f"{self.companies} companies: {self.stored} new acceptance times "
            f"of {self.observed} seen, {len(self.failures)} failed, "
            f"{len(self.empty)} had none"
        )

    def retry_report(self, limit: int = 20) -> str:
        """Return the failures as a list an operator can act on."""
        if not self.failures:
            return "no failures"
        lines = [f"{len(self.failures)} failed:"]
        lines.extend(f"  {ticker}: {reason}" for ticker, reason in self.failures[:limit])
        if len(self.failures) > limit:
            lines.append(f"  ... and {len(self.failures) - limit} more")
        return "\n".join(lines)


def _row(record: AcceptedFiling) -> dict[str, Any]:
    """Return the column mapping for one acceptance record."""
    return {
        "accession": record.accession,
        "accepted_at": record.accepted_at,
        "filed_date": record.filed_date,
        "form": record.form,
        "source": record.source,
    }


def ingest_filing_acceptance(
    session: Session,
    provider: object,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    only_missing: bool = True,
) -> AcceptanceIngestResult:
    """Record when each filing became public.

    Args:
        session: Open database session. The caller commits.
        provider: A fundamentals provider. One that cannot report acceptance
            times records nothing and says so.
        tickers: Restrict to these symbols. Defaults to every stored company.
        limit: Examine at most this many companies, in ticker order.
        only_missing: Skip companies whose observation accessions are already
            fully resolved. Makes a re-run cheap rather than a second full pass
            over the SEC.

    Returns:
        What was recorded.
    """
    if not isinstance(provider, FilingAcceptanceSource):
        log.info(
            "fundamentals provider reports no acceptance times",
            provider=type(provider).__name__,
        )
        return AcceptanceIngestResult()

    companies = CompanyRepository(session).list_all()
    wanted = {ticker.upper() for ticker in tickers} if tickers else None
    if wanted is not None:
        companies = [company for company in companies if company.ticker in wanted]
    companies = sorted(companies, key=lambda company: company.ticker)
    if limit is not None:
        companies = companies[:limit]

    observations = FundamentalObservationRepository(session)
    acceptance = FilingAcceptanceRepository(session)
    seen = observed = stored = 0
    failures: list[tuple[str, str]] = []
    empty: list[str] = []

    for company in companies:
        seen += 1
        if only_missing and not observations.unresolved_accessions(company.id):
            continue
        try:
            records = provider.get_filing_acceptance(company.ticker)
        except Exception as error:
            log.warning("could not read filing acceptance", ticker=company.ticker, error=str(error))
            failures.append((company.ticker, f"{type(error).__name__}: {error}"))
            continue

        if not records:
            empty.append(company.ticker)
            continue

        observed += len(records)
        stored += acceptance.add_all([_row(record) for record in records])

    result = AcceptanceIngestResult(
        companies=seen,
        observed=observed,
        stored=stored,
        failures=tuple(failures),
        empty=tuple(empty),
    )
    log.info(
        "filing acceptance ingested",
        companies=result.companies,
        observed=result.observed,
        stored=result.stored,
        failed=len(result.failures),
        empty=len(result.empty),
    )
    return result
