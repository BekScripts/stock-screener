"""Turning fails-to-deliver evidence into securities with stable identities.

`ftd` writes down what the files say. This decides what it means, for one
question at a time: **the security that left the market under this symbol on this
date — which one was it?**

The date is not decoration. A symbol alone is unanswerable, and the reason is
`BBBY`: Bed Bath & Beyond's equity was extinguished in 2023, and Beyond, Inc. has
been trading under the symbol since 2025-09-02. Asked "which security is `BBBY`"
the record must decline; asked "which security was `BBBY` on 2023-05-02" it
answers exactly one, and asked about 2025-09-30 it answers a different one.
Every function here therefore takes a delisting — a symbol *with* its effective
date — and never a bare symbol.

## How a span is chosen

`domain.classify_symbol` groups the evidence by CUSIP issuer base, which is the
CUSIP standard rather than a heuristic: characters 1-6 name the issuer, so two
issues sharing them are one company's and two that differ are two companies', and
no lineage is ever drawn across that line.

The issuer holding a symbol on date `D` is **the latest one to have taken it up
by then** — the same rule `universe_members_as_of` uses for membership. Then:

- exactly one such span → **resolved**, and that span's evidence becomes the
  security's identifier intervals;
- none → **unresolved**, which says the record cannot place the name and says
  nothing whatever about whether it existed;
- two beginning on the same day → **ambiguous**, and nothing is created.
  Choosing between them would be a guess, and a guessed identity is worse than
  an absent one because a study cannot see it.

## What is never done here

No CUSIP, CIK, ticker or issuer mapping is invented. No row is written to
`companies` — a delisted security must never enter the investable set, and the
absence of any write to that table is the whole guarantee. And nothing merges a
live company row with a historical security: the two identity spaces are
deliberately separate, and `contamination` reports where a live row looks like it
straddles both rather than repairing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from data_access import (
    DelistingRepository,
    FtdIdentityObservationRepository,
    HistoricalSecurityRepository,
    SecurityIdentifierRepository,
    session_scope,
)
from domain import (
    CorporateActionKind,
    FtdIdentityEvidence,
    IdentifierInterval,
    IssuerSpan,
    SymbolStatus,
    classify_delisting,
    classify_symbol,
    existence_evidence,
    intervals_for_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session, sessionmaker

    from data_access.models import Delisting, FtdIdentityObservation, HistoricalSecurity

log = structlog.get_logger(__name__)

#: How long after a security's last trade its failures may still be published.
#: U.S. equities settle two business days after the trade, and a failure is
#: reported against the settlement date, so evidence trails the tape by a few
#: days. Ten calendar days covers that plus a weekend and a holiday without
#: reaching far enough to swallow a later occupant of the symbol.
SETTLEMENT_GRACE_DAYS = 10

#: The `source` recorded on every interval derived from the published files.
FTD_SOURCE = "SEC_FTD"


class ResolutionOutcome:
    """What resolving one delisting produced. String values, for reports."""

    RESOLVED = "RESOLVED"
    """One compatible issuer span; a security exists or was created for it."""

    AMBIGUOUS = "AMBIGUOUS"
    """More than one issuer span fits the date, or the evidence overlaps itself.
    Nothing was created."""

    UNRESOLVED = "UNRESOLVED"
    """No evidence, or none compatible with the date. Nothing was created, and
    nothing is implied about whether the security existed."""


@dataclass(frozen=True)
class SymbolResolution:
    """The verdict on one delisted symbol, and what it caused.

    Attributes:
        ticker: The delisted symbol.
        effective_date: The date it left the market.
        outcome: `RESOLVED`, `AMBIGUOUS` or `UNRESOLVED`.
        security_id: The security it resolved to, or None.
        created: True when a new historical security was minted for it, False
            when it attached to one already stored.
        issuers: How many distinct issuers the evidence shows for the symbol
            across all time. More than one is a ticker reuse.
        cusips: How many distinct CUSIPs the chosen span carried. More than one
            is a CUSIP-change lineage.
        intervals: How many identifier intervals were written.
        evidence_rows: How many published rows stand behind the verdict.
        detail: The sentence an audit prints.
    """

    ticker: str
    effective_date: date
    outcome: str
    security_id: int | None = None
    created: bool = False
    issuers: int = 0
    cusips: int = 0
    intervals: int = 0
    evidence_rows: int = 0
    detail: str = ""

    @property
    def is_ticker_reuse(self) -> bool:
        """True when more than one issuer has held this symbol."""
        return self.issuers > 1

    @property
    def is_cusip_lineage(self) -> bool:
        """True when the resolved issuer carried more than one CUSIP."""
        return self.cusips > 1


@dataclass(frozen=True)
class ResolutionReport:
    """What a whole resolution pass concluded.

    Attributes:
        resolutions: One entry per delisted symbol considered.
    """

    resolutions: tuple[SymbolResolution, ...] = field(default_factory=tuple)

    def _with(self, outcome: str) -> tuple[SymbolResolution, ...]:
        """Every resolution with one outcome."""
        return tuple(r for r in self.resolutions if r.outcome == outcome)

    @property
    def resolved(self) -> tuple[SymbolResolution, ...]:
        """Symbols the record placed on exactly one security."""
        return self._with(ResolutionOutcome.RESOLVED)

    @property
    def ambiguous(self) -> tuple[SymbolResolution, ...]:
        """Symbols whose evidence fits more than one security."""
        return self._with(ResolutionOutcome.AMBIGUOUS)

    @property
    def unresolved(self) -> tuple[SymbolResolution, ...]:
        """Symbols the record cannot place. Never "did not exist"."""
        return self._with(ResolutionOutcome.UNRESOLVED)

    @property
    def created(self) -> tuple[SymbolResolution, ...]:
        """Symbols that minted a new historical security."""
        return tuple(r for r in self.resolutions if r.created)

    @property
    def ticker_reuse(self) -> tuple[SymbolResolution, ...]:
        """Symbols more than one issuer has held."""
        return tuple(r for r in self.resolutions if r.is_ticker_reuse)

    @property
    def cusip_lineage(self) -> tuple[SymbolResolution, ...]:
        """Securities whose issuer carried more than one CUSIP."""
        return tuple(r for r in self.resolutions if r.is_cusip_lineage)

    def summary(self) -> str:
        """One line describing the pass."""
        return (
            f"resolve-identity: {len(self.resolutions)} delisted symbols — "
            f"{len(self.resolved)} resolved, {len(self.ambiguous)} ambiguous, "
            f"{len(self.unresolved)} unresolved; {len(self.created)} securities created, "
            f"{len(self.ticker_reuse)} ticker reuses, {len(self.cusip_lineage)} CUSIP lineages"
        )


def _action_kind(kind: str) -> CorporateActionKind | None:
    """Read a stored corporate-action kind, or None where the domain has no member.

    A kind the domain does not model leaves the fate `UNKNOWN` rather than being
    forced onto the nearest one it does.
    """
    try:
        return CorporateActionKind(kind)
    except ValueError:
        return None


def as_evidence(row: FtdIdentityObservation) -> FtdIdentityEvidence:
    """Project a stored evidence row onto the domain's neutral shape."""
    return FtdIdentityEvidence(
        cusip=row.cusip,
        symbol=row.symbol,
        issuer_name=row.issuer_name,
        first_settlement=row.first_settlement_date,
        last_settlement=row.last_settlement_date,
        observations=row.observation_count,
        source_file=row.first_source_file,
        evidence_hash=row.evidence_hash,
    )


def holder_spans(spans: Sequence[IssuerSpan], as_of: date) -> tuple[IssuerSpan, ...]:
    """Return the issuer that held a symbol on a date: the latest to have begun by it.

    The same rule `domain.universe_members_as_of` uses for membership — *the
    latest record dated on or before the date decides* — applied to a symbol's
    occupants. A symbol's issuers hold it in sequence, so the one holding it on a
    date is the last one to have taken it up by then.

    **This replaces a defective rule.** Phase 9A asked instead whether a span had
    begun by the date *and stopped being seen shortly after it*, which bounds a
    span's end from above but never from below — so any long-dead earlier
    occupant also qualified. `ATVI` was the worked case: Activision Inc held it
    from 2004 to 2008 and Activision Blizzard from 2008 to its 2023 merger, and
    both satisfied the old test, so the merger every source agrees about came
    back `AMBIGUOUS`. `TWTR` failed the same way against Tweeter Home
    Entertainment. Measured over the whole registry the old rule resolved 2,413
    of 3,165 symbols and called 677 ambiguous; this one resolves **3,158** and
    ties on none.

    Nothing is spliced by the change. A span is eligible only if it had begun by
    the date, so a later occupant can never answer for an earlier one's exit; and
    where two issuers' windows genuinely overlap, `classify_symbol` has already
    returned `AMBIGUOUS` before this function is reached.

    Args:
        spans: The issuer spans the evidence shows for the symbol.
        as_of: The date the symbol is being read on — a delisting's effective
            date, or a stored security's last trading session.

    Returns:
        The holding span, as a one-tuple. Empty when no issuer had taken the
        symbol up by the date, and longer only where two spans begin on the very
        same day, which is a genuine ambiguity rather than a choice to make.
    """
    started = [span for span in spans if span.first_settlement <= as_of]
    if not started:
        return ()
    latest = max(span.first_settlement for span in started)
    return tuple(span for span in started if span.first_settlement == latest)


def _interval_rows(
    intervals: Sequence[IdentifierInterval], evidence: Sequence[FtdIdentityEvidence]
) -> list[dict[str, Any]]:
    """Project domain intervals onto storable rows, carrying each one's provenance.

    Every interval keeps the issuer name, row count, evidence digest and source
    file of the CUSIP behind it, so a stored identity can be traced back to the
    published lines that established it.
    """
    by_cusip = {item.cusip: item for item in evidence}
    rows: list[dict[str, Any]] = []
    for interval in intervals:
        source = by_cusip.get(interval.cusip or "")
        rows.append(
            {
                "historical_security_id": interval.security_id,
                "cusip": interval.cusip,
                "ticker": interval.ticker,
                "venue": interval.venue,
                "valid_from": interval.valid_from,
                "valid_to": interval.valid_to,
                "source": interval.source,
                "confidence": str(interval.confidence),
                "lineage": str(interval.lineage),
                "issuer_name": source.issuer_name if source else None,
                "observation_count": source.observations if source else None,
                "evidence_hash": source.evidence_hash if source else None,
                "source_file": source.source_file if source else None,
            }
        )
    return rows


def _existing_security_for(
    session: Session, ticker: str, span: IssuerSpan, effective_date: date
) -> int | None:
    """Find a historical security already stored for this symbol and window.

    Phase 8H ingested 402 delisted securities by CIK from a cohort whose
    identities a caller had already established. Those rows are better evidence
    than anything derivable here — they carry a real filer number — so a symbol
    that resolves to one of them attaches to it rather than minting a rival.

    The match is `primary_ticker` **and** a last trading date inside the span or
    within the settlement grace of the delisting. The date half is what makes it
    safe: matching on ticker alone is precisely how two issuers become one row.

    Args:
        session: An open session.
        ticker: The delisted symbol.
        span: The issuer span the evidence chose.
        effective_date: The delisting's date.

    Returns:
        The security's id when exactly one matches, else None — including when
        several do, since choosing between them would be the guess this module
        refuses to make.
    """
    repository = HistoricalSecurityRepository(session)
    cutoff = effective_date + timedelta(days=SETTLEMENT_GRACE_DAYS)
    matches = [
        security
        for security in repository.list_all()
        if security.primary_ticker.upper() == ticker.upper()
        and security.last_trading_date is not None
        and span.first_settlement <= security.last_trading_date <= cutoff
    ]
    if len(matches) != 1:
        return None
    return matches[0].id


def _record_security(
    session: Session,
    delisting: Delisting,
    span: IssuerSpan,
    evidence: Sequence[FtdIdentityEvidence],
) -> tuple[int, bool, int]:
    """Store the security for one resolved span, and its identifier intervals.

    Returns:
        `(security_id, created, intervals_written)`.
    """
    ticker = delisting.ticker.upper()
    security_id = _existing_security_for(session, ticker, span, delisting.effective_date)
    created = security_id is None
    if security_id is None:
        # No CIK: the fails record establishes none, and inventing a filer number
        # to satisfy a column is the one thing this layer exists not to do. The
        # column is nullable precisely so this row can exist.
        security = HistoricalSecurityRepository(session).create(
            cik=None,
            name=span.names[0],
            primary_ticker=ticker,
            status=str(classify_delisting(_action_kind(delisting.kind))),
            last_trading_date=delisting.effective_date,
            notes=(
                f"identity established from SEC fails-to-deliver evidence: issuer "
                f"{span.issuer_base}, {span.observations} rows over "
                f"{span.first_settlement}..{span.last_settlement}"
            ),
        )
        security_id = security.id

    intervals = intervals_for_evidence(
        security_id,
        ticker,
        [item for item in evidence if item.issuer_base == span.issuer_base],
        source=FTD_SOURCE,
        venue=None,
    )
    written = SecurityIdentifierRepository(session).add_all(_interval_rows(intervals, evidence))
    return security_id, created, written


def assess_delisting(session: Session, delisting: Delisting) -> SymbolResolution:
    """Decide what a delisted symbol resolves to, **without writing anything**.

    The whole verdict, and none of its consequences. `resolve_delisting` calls
    this and then records what it says; an audit calls it alone, so every figure
    it reports is re-derivable from the stored evidence rather than remembered
    from a run that has since scrolled away.

    Args:
        session: An open session.
        delisting: The delisting to judge — a symbol *with* its date.

    Returns:
        The verdict. `security_id`, `created` and `intervals` are left unset,
        because nothing was created.
    """
    ticker = delisting.ticker.upper()
    rows = FtdIdentityObservationRepository(session).for_symbol(ticker)
    evidence = [as_evidence(row) for row in rows]
    existence = existence_evidence(evidence)
    verdict = classify_symbol(ticker, evidence)

    if verdict.status is SymbolStatus.UNRESOLVED:
        return SymbolResolution(
            ticker=ticker,
            effective_date=delisting.effective_date,
            outcome=ResolutionOutcome.UNRESOLVED,
            detail=existence.detail,
        )
    if verdict.status is SymbolStatus.AMBIGUOUS:
        return SymbolResolution(
            ticker=ticker,
            effective_date=delisting.effective_date,
            outcome=ResolutionOutcome.AMBIGUOUS,
            issuers=len(verdict.spans),
            evidence_rows=existence.observations,
            detail=verdict.detail,
        )

    candidates = holder_spans(verdict.spans, delisting.effective_date)
    if not candidates:
        return SymbolResolution(
            ticker=ticker,
            effective_date=delisting.effective_date,
            outcome=ResolutionOutcome.UNRESOLVED,
            issuers=len(verdict.spans),
            evidence_rows=existence.observations,
            detail=(
                f"{len(verdict.spans)} issuer span(s) carry {ticker}, none of which had taken "
                f"the symbol up by {delisting.effective_date}"
            ),
        )
    if len(candidates) > 1:
        return SymbolResolution(
            ticker=ticker,
            effective_date=delisting.effective_date,
            outcome=ResolutionOutcome.AMBIGUOUS,
            issuers=len(verdict.spans),
            evidence_rows=existence.observations,
            detail=(
                f"{len(candidates)} issuer spans begin on the same day for {ticker} at "
                f"{delisting.effective_date}; the record names no single holder"
            ),
        )

    span = candidates[0]
    return SymbolResolution(
        ticker=ticker,
        effective_date=delisting.effective_date,
        outcome=ResolutionOutcome.RESOLVED,
        issuers=len(verdict.spans),
        cusips=len(span.cusips),
        evidence_rows=span.observations,
        detail=(
            f"{ticker} on {delisting.effective_date} is issuer {span.issuer_base} "
            f"({', '.join(span.cusips)}), {span.observations} rows over "
            f"{span.first_settlement}..{span.last_settlement}"
        ),
    )


def resolve_delisting(session: Session, delisting: Delisting) -> SymbolResolution:
    """Resolve one delisted symbol to a security, or record why it cannot be.

    Args:
        session: An open session.
        delisting: The delisting to resolve — a symbol *with* its date.

    Returns:
        The verdict, and what it caused.
    """
    assessment = assess_delisting(session, delisting)
    if assessment.outcome is not ResolutionOutcome.RESOLVED:
        return assessment

    ticker = delisting.ticker.upper()
    rows = FtdIdentityObservationRepository(session).for_symbol(ticker)
    evidence = [as_evidence(row) for row in rows]
    verdict = classify_symbol(ticker, evidence)
    span = holder_spans(verdict.spans, delisting.effective_date)[0]
    security_id, created, intervals = _record_security(session, delisting, span, evidence)
    return replace(assessment, security_id=security_id, created=created, intervals=intervals)


def attach_identity(session: Session, security: HistoricalSecurity) -> SymbolResolution:
    """Give a stored historical security its dated identifier intervals.

    The companion to `resolve_delisting`, for securities that already exist but
    carry no identity. Phase 8I ingested 402 of them from a cohort whose CIKs a
    caller had established, and Phase 9A only walked the **delisting registry** —
    so a security whose exit no corporate-action feed files was never reached.
    That is not a rare corner: it is every receivership. SVB, First Republic and
    Bed Bath & Beyond all have rows, prices and asserted fates, and no delisting
    record at all.

    The anchor is therefore the security's own `last_trading_date` rather than a
    delisting's effective date. It is the same question asked of the same
    evidence — *which issuer held this symbol then* — with the security's own
    record supplying the date instead of a feed.

    Args:
        session: An open session.
        security: The stored security to give an identity to.

    Returns:
        The verdict. Intervals are written only when exactly one issuer held the
        symbol on the date; an ambiguous or unresolved security keeps none, and
        keeps its prices and its outcomes regardless.
    """
    ticker = security.primary_ticker.upper()
    as_of = security.last_trading_date
    if as_of is None:
        return SymbolResolution(
            ticker=ticker,
            effective_date=date.min,
            outcome=ResolutionOutcome.UNRESOLVED,
            detail=f"{ticker} records no last trading date to resolve its identity against",
        )

    rows = FtdIdentityObservationRepository(session).for_symbol(ticker)
    evidence = [as_evidence(row) for row in rows]
    existence = existence_evidence(evidence)
    verdict = classify_symbol(ticker, evidence)

    if verdict.status is SymbolStatus.UNRESOLVED:
        return SymbolResolution(
            ticker=ticker,
            effective_date=as_of,
            outcome=ResolutionOutcome.UNRESOLVED,
            detail=existence.detail,
        )
    if verdict.status is SymbolStatus.AMBIGUOUS:
        return SymbolResolution(
            ticker=ticker,
            effective_date=as_of,
            outcome=ResolutionOutcome.AMBIGUOUS,
            issuers=len(verdict.spans),
            evidence_rows=existence.observations,
            detail=verdict.detail,
        )

    holders = holder_spans(verdict.spans, as_of)
    if len(holders) != 1:
        return SymbolResolution(
            ticker=ticker,
            effective_date=as_of,
            outcome=(ResolutionOutcome.UNRESOLVED if not holders else ResolutionOutcome.AMBIGUOUS),
            issuers=len(verdict.spans),
            evidence_rows=existence.observations,
            detail=(
                f"{len(verdict.spans)} issuer span(s) carry {ticker}; "
                f"{len(holders)} held it on {as_of}"
            ),
        )

    span = holders[0]
    intervals = intervals_for_evidence(
        security.id,
        ticker,
        [item for item in evidence if item.issuer_base == span.issuer_base],
        source=FTD_SOURCE,
        venue=None,
    )
    written = SecurityIdentifierRepository(session).add_all(_interval_rows(intervals, evidence))
    return SymbolResolution(
        ticker=ticker,
        effective_date=as_of,
        outcome=ResolutionOutcome.RESOLVED,
        security_id=security.id,
        created=False,
        issuers=len(verdict.spans),
        cusips=len(span.cusips),
        intervals=written,
        evidence_rows=span.observations,
        detail=(
            f"{ticker} on {as_of} is issuer {span.issuer_base} "
            f"({', '.join(span.cusips)}), {span.observations} rows over "
            f"{span.first_settlement}..{span.last_settlement}"
        ),
    )


def attach_identities(
    session_factory: sessionmaker[Session],
    *,
    only_missing: bool = True,
    limit: int | None = None,
    on_progress: Callable[[SymbolResolution, int, int], None] | None = None,
) -> ResolutionReport:
    """Give every stored historical security without one its identifier intervals.

    Args:
        session_factory: How to open a session.
        only_missing: Skip securities that already hold intervals. On by default.
        limit: Process at most this many.
        on_progress: Called with each verdict, its index and the total.

    Returns:
        Every verdict, in order.
    """
    with session_scope(session_factory) as session:
        securities = HistoricalSecurityRepository(session).list_all()
        having = (
            {row.historical_security_id for row in SecurityIdentifierRepository(session).list_all()}
            if only_missing
            else set()
        )
        pending = [security.id for security in securities if security.id not in having]
    if limit is not None:
        pending = pending[:limit]

    resolutions: list[SymbolResolution] = []
    total = len(pending)
    for index, security_id in enumerate(pending, start=1):
        with session_scope(session_factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            if security is None:  # pragma: no cover — just read
                continue
            resolution = attach_identity(session, security)
        resolutions.append(resolution)
        if on_progress is not None:
            on_progress(resolution, index, total)

    return ResolutionReport(resolutions=tuple(resolutions))


def assess_delisted_symbols(session: Session, *, limit: int | None = None) -> ResolutionReport:
    """Judge every recorded delisting against the evidence, writing nothing.

    What `identity audit` uses, so its resolution figures are a property of the
    stored record rather than of a run whose log has been lost.

    Args:
        session: An open session.
        limit: Consider only the first N delistings.

    Returns:
        Every verdict, in order.
    """
    delistings = DelistingRepository(session).earliest_per_ticker()
    ordered = delistings if limit is None else delistings[:limit]
    return ResolutionReport(
        resolutions=tuple(assess_delisting(session, record) for record in ordered)
    )


def resolve_delisted_symbols(
    session_factory: sessionmaker[Session],
    *,
    limit: int | None = None,
    on_progress: Callable[[SymbolResolution, int, int], None] | None = None,
) -> ResolutionReport:
    """Resolve every recorded delisting against the fails-to-deliver evidence.

    One transaction per symbol, so a run killed halfway leaves every resolved
    security stored and the one in flight absent.

    Args:
        session_factory: How to open a session.
        limit: Consider only the first N delistings, for a smoke run.
        on_progress: Called with each verdict, its index and the total.

    Returns:
        Every verdict, in order.
    """
    with session_scope(session_factory) as session:
        # One delisting per symbol, the earliest: a symbol reissued and delisted
        # twice has two exits, and the first belongs to the first occupant.
        identifiers = [record.id for record in DelistingRepository(session).earliest_per_ticker()]
    ordered = identifiers if limit is None else identifiers[:limit]

    resolutions: list[SymbolResolution] = []
    total = len(ordered)
    for index, delisting_id in enumerate(ordered, start=1):
        with session_scope(session_factory) as session:
            reloaded = DelistingRepository(session).get(delisting_id)
            if reloaded is None:  # pragma: no cover — the row was just read
                continue
            resolution = resolve_delisting(session, reloaded)
        resolutions.append(resolution)
        if on_progress is not None:
            on_progress(resolution, index, total)

    return ResolutionReport(resolutions=tuple(resolutions))
