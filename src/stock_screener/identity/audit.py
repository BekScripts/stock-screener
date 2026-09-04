"""The Phase 9A audit: what the identity layer holds, counted from the database.

Every figure here is a query, not a running total kept by whichever pass happened
to write last. That matters because the passes are resumable and re-runnable: a
count accumulated across three interrupted ingests would describe none of them,
while a count read from the tables describes the record as it actually stands.

The audit answers, in order: what evidence was read, what identities came out of
it, what could not be resolved and why, which live company rows the evidence
implicates, and — the number that has to be zero — how many existing forward
returns were moved onto a historical security.

That last one is the point of auditing at all. Phase 9A is an *architecture*
change: the schema learns to hold a delisted security's outcomes, and not one
measurement is migrated into that shape, because migrating one needs its identity
proven first and proving it is a separate, deliberate operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import distinct, func, select

from data_access import (
    CompanyRepository,
    FtdIdentityObservationRepository,
    FtdSourceFileRepository,
    HistoricalSecurityRepository,
    SecurityIdentifierRepository,
)
from data_access.models import (
    Delisting,
    ForwardReturnRecord,
    FtdIdentityObservation,
    HistoricalSecurity,
    SecurityIdentifier,
)
from stock_screener.identity.contamination import detect_contaminated_companies
from stock_screener.identity.resolution import FTD_SOURCE, assess_delisted_symbols

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy import Select
    from sqlalchemy.orm import Session

    from stock_screener.identity.contamination import ContaminationReport
    from stock_screener.identity.resolution import ResolutionReport


@dataclass(frozen=True)
class IdentityAudit:
    """The state of the identity layer, every figure read from the tables.

    Attributes:
        ftd_files: Archives recorded as read.
        ftd_rows: Published lines behind them.
        ftd_first_settlement: Earliest settlement date across the record.
        ftd_last_settlement: Latest settlement date across the record.
        ftd_triples: Distinct `(cusip, symbol, issuer_name)` rows stored.
        unique_cusips: Distinct CUSIPs in the evidence.
        unique_symbols: Distinct symbols in the evidence.
        unique_issuer_names: Distinct issuer names in the evidence.
        invalid_cusips: Evidence rows whose CUSIP fails its own check digit.
            Recorded, never repaired.
        delisted_symbols: Symbols in the delisting registry.
        resolved_symbols: Delisted symbols placed on exactly one security.
        unresolved_symbols: Delisted symbols the record cannot place. Says
            nothing about whether they existed.
        ambiguous_symbols: Delisted symbols whose evidence fits more than one.
        ticker_reuse_cases: Symbols more than one issuer has held.
        cusip_change_cases: Resolved securities carrying more than one CUSIP.
        reverse_split_cases: Identifier intervals recorded as opened by a
            reverse split. Only ever asserted from evidence outside the fails
            file, which records no corporate action.
        historical_securities: Rows in the registry.
        securities_with_identity: Rows carrying at least one identifier interval
            derived from the fails-to-deliver record — what the identity layer
            established, as opposed to what a resolution run happened to mint.
        cik_resolved: Registry rows carrying a filer number.
        cik_unresolved: Registry rows carrying none.
        identifier_intervals: Dated identifier intervals stored.
        live_companies: Active company rows.
        contaminated_companies: Live rows the evidence shows two issuers under.
        forward_returns_total: Every measurement stored.
        forward_returns_company: Measurements against a live company.
        forward_returns_historical: Measurements against a delisted security.
            **Must be zero** unless a migration was explicitly authorised.
    """

    ftd_files: int = 0
    ftd_rows: int = 0
    ftd_first_settlement: date | None = None
    ftd_last_settlement: date | None = None
    ftd_triples: int = 0
    unique_cusips: int = 0
    unique_symbols: int = 0
    unique_issuer_names: int = 0
    invalid_cusips: int = 0
    delisted_symbols: int = 0
    resolved_symbols: int = 0
    unresolved_symbols: int = 0
    ambiguous_symbols: int = 0
    ticker_reuse_cases: int = 0
    cusip_change_cases: int = 0
    reverse_split_cases: int = 0
    historical_securities: int = 0
    securities_with_identity: int = 0
    cik_resolved: int = 0
    cik_unresolved: int = 0
    identifier_intervals: int = 0
    live_companies: int = 0
    contaminated_companies: int = 0
    forward_returns_total: int = 0
    forward_returns_company: int = 0
    forward_returns_historical: int = 0
    contaminated_examples: tuple[str, ...] = field(default_factory=tuple)

    def lines(self) -> tuple[str, ...]:
        """Render the audit as the report Phase 9A asks for, one figure a line."""
        window = (
            f"{self.ftd_first_settlement} .. {self.ftd_last_settlement}"
            if self.ftd_first_settlement is not None
            else "none"
        )
        migrated = self.forward_returns_historical
        migrated_note = "" if migrated == 0 else "  <-- NOT ZERO: investigate before proceeding"
        return (
            "Fails-to-deliver evidence",
            f"  files processed                {self.ftd_files:,}",
            f"  settlement date range          {window}",
            f"  raw rows                       {self.ftd_rows:,}",
            f"  distinct identity triples      {self.ftd_triples:,}",
            f"  unique CUSIPs                  {self.unique_cusips:,}",
            f"  unique tickers                 {self.unique_symbols:,}",
            f"  unique issuer names            {self.unique_issuer_names:,}",
            f"  CUSIPs failing their check     {self.invalid_cusips:,}",
            "Delisted-symbol resolution",
            f"  delisted symbols considered    {self.delisted_symbols:,}",
            f"  resolved to a security_id      {self.resolved_symbols:,}",
            f"  unresolved                     {self.unresolved_symbols:,}",
            f"  ambiguous                      {self.ambiguous_symbols:,}",
            f"  ticker-reuse cases             {self.ticker_reuse_cases:,}",
            f"  CUSIP-change lineage cases     {self.cusip_change_cases:,}",
            f"  reverse-split lineage cases    {self.reverse_split_cases:,}",
            "Security identity",
            f"  historical securities          {self.historical_securities:,}",
            f"  with an FTD-derived identity   {self.securities_with_identity:,}",
            f"  CIK-resolved                   {self.cik_resolved:,}",
            f"  CIK-unresolved                 {self.cik_unresolved:,}",
            f"  identifier intervals           {self.identifier_intervals:,}",
            "Live company contamination",
            f"  active company rows            {self.live_companies:,}",
            f"  contaminated (two issuers)     {self.contaminated_companies:,}",
            "Forward returns",
            f"  total measurements             {self.forward_returns_total:,}",
            f"  company-backed                 {self.forward_returns_company:,}",
            f"  historical-security-backed     {self.forward_returns_historical:,}{migrated_note}",
        )


def _count(session: Session, statement: Select[tuple[int]]) -> int:
    """Run a scalar count, treating a NULL result as zero."""
    return int(session.scalar(statement) or 0)


def build_identity_audit(
    session: Session,
    *,
    resolution: ResolutionReport | None = None,
    contamination: ContaminationReport | None = None,
) -> IdentityAudit:
    """Read the state of the identity layer out of the database.

    Args:
        session: An open session.
        resolution: A resolution pass's report. Omitted, the verdicts are
            **re-derived** read-only from the stored evidence, so the figures
            describe the record rather than a run whose log has scrolled away.
            Passing one adds the two figures only a run knows — how many
            securities it minted, and how many intervals it wrote.
        contamination: A contamination scan's report. Omitted, one is run.

    Returns:
        Every figure the Phase 9A audit asks for.
    """
    verdicts = resolution if resolution is not None else assess_delisted_symbols(session)
    scan = contamination if contamination is not None else detect_contaminated_companies(session)
    files, rows, first, last = FtdSourceFileRepository(session).summary()
    triples, cusips, symbols, issuer_names = FtdIdentityObservationRepository(
        session
    ).distinct_counts()

    securities = HistoricalSecurityRepository(session)
    stored_securities = securities.count()
    cik_resolved = _count(
        session,
        select(func.count())
        .select_from(HistoricalSecurity)
        .where(HistoricalSecurity.cik.is_not(None)),
    )

    return IdentityAudit(
        ftd_files=files,
        ftd_rows=rows,
        ftd_first_settlement=first,
        ftd_last_settlement=last,
        ftd_triples=triples,
        unique_cusips=cusips,
        unique_symbols=symbols,
        unique_issuer_names=issuer_names,
        invalid_cusips=_count(
            session,
            select(func.count())
            .select_from(FtdIdentityObservation)
            .where(FtdIdentityObservation.cusip_valid.is_(False)),
        ),
        delisted_symbols=_count(
            session, select(func.count(distinct(Delisting.ticker))).select_from(Delisting)
        ),
        resolved_symbols=len(verdicts.resolved),
        unresolved_symbols=len(verdicts.unresolved),
        ambiguous_symbols=len(verdicts.ambiguous),
        ticker_reuse_cases=len(verdicts.ticker_reuse),
        cusip_change_cases=len(verdicts.cusip_lineage),
        reverse_split_cases=_count(
            session,
            select(func.count())
            .select_from(SecurityIdentifier)
            .where(SecurityIdentifier.lineage == "REVERSE_SPLIT"),
        ),
        historical_securities=stored_securities,
        securities_with_identity=_count(
            session,
            select(func.count(distinct(SecurityIdentifier.historical_security_id)))
            .select_from(SecurityIdentifier)
            .where(SecurityIdentifier.source == FTD_SOURCE),
        ),
        cik_resolved=cik_resolved,
        cik_unresolved=stored_securities - cik_resolved,
        identifier_intervals=SecurityIdentifierRepository(session).count(),
        live_companies=CompanyRepository(session).count(),
        contaminated_companies=len(scan.contaminated),
        contaminated_examples=(
            ()
            if contamination is None
            else tuple(c.ticker for c in contamination.contaminated[:20])
        ),
        forward_returns_total=_count(
            session, select(func.count()).select_from(ForwardReturnRecord)
        ),
        forward_returns_company=_count(
            session,
            select(func.count())
            .select_from(ForwardReturnRecord)
            .where(ForwardReturnRecord.company_id.is_not(None)),
        ),
        forward_returns_historical=_count(
            session,
            select(func.count())
            .select_from(ForwardReturnRecord)
            .where(ForwardReturnRecord.historical_security_id.is_not(None)),
        ),
    )


__all__ = ["IdentityAudit", "build_identity_audit"]
