"""Finding live company rows that hold more than one issuer's history.

`companies` is keyed by ticker, and a ticker is a lease. When a symbol is
reissued to an unrelated company, the row that held it keeps accumulating: the
old issuer's prices and filings sit underneath the new issuer's, under one
`company_id`, and every query that reads that row reads two companies as one.

The proven case is Bed Bath & Beyond. `companies` row 2147 carries ticker `BBBY`,
the name of an unrelated 2025 issuer, Bed Bath's collapse to $0.0743 on
2023-05-02, a twenty-six-month hole, and then a fresh price series. One row, two
issuers, and no identifier on the table that could tell them apart.

## Two issuers under one symbol is not yet a finding

The fails-to-deliver record shows a change of CUSIP issuer base across **1,840**
handovers on the live roster, over 1,486 of 5,766 rows — and most of them are not
defects. Three things have to be true before a live row is actually holding two
companies:

**The handover must be a change of hands, not a reorganisation.** Accenture's
symbol passes from issuer `G1150G` to `G1151C` with a two-day gap: that is
Accenture Ltd re-incorporating as Accenture plc, one company throughout. Barrick's
`ABX` takes a new CUSIP on renaming. Measured across the roster the two shapes
separate cleanly — 432 handovers complete inside a week, 1,182 take more than a
year, and the trough between them is thin. `HANDOVER_GAP_DAYS` sits in that
trough, at about the 120 trading sessions the Phase 8J price-discontinuity audit
already used, and it rules out **596** of the 1,840.

**The row's own data must span it.** A symbol reused in 2008 says nothing about a
`companies` row whose price history begins in 2019 — the earlier occupant never
entered that row. Requiring bars on *both* sides takes the remaining 1,244
handovers down to **107**.

**And the row must show the break.** This one is worth stating plainly: **a gap
in the fails record is a gap in *failures*, not in trading.** A company whose
deliveries simply stop failing vanishes from the file while trading normally, and
**37** of those 107 turn out to have traded straight through their supposed
handover — Amalgamated's holding-company reorganisation among them. So the last
filter is the row's own price series: contamination requires a discontinuity
longer than `HANDOVER_GAP_DAYS` *inside* the handover window, the same measure the
Phase 8J audit used. That leaves **69 rows** the record actually shows stopping
and restarting under two issuers — `companies` row 2147 among them, with an
804-day break between Bed Bath & Beyond (`075896`) and Beyond, Inc. (`690370`).

Every filter makes the scan more conservative, and deliberately so. A row wrongly
called clean costs a study one name it should have excluded; a row wrongly called
contaminated invites someone to take a scalpel to a company that was never two.

## This module reports. It does not repair.

Nothing here writes. Not to `companies`, not to `historical_securities`, not to
`security_identifiers`. Splitting a contaminated row would mean deciding which
prices, filings, snapshots and scores belong to which issuer, across every child
table, and re-deriving every score built on the mixture — a data operation with
real consequences that must be a deliberate, separately-authorised step. Merging
would be worse, since it is the error itself.

What a report earns is the ability to *exclude*. A study that knows row 2147 is
contaminated can drop it; a study that does not know reads a fabricated
twenty-six-month drawdown as a fact about a company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from data_access import CompanyRepository, FtdIdentityObservationRepository
from data_access.models import PriceHistory
from domain import SymbolStatus, classify_symbol
from stock_screener.identity.resolution import as_evidence

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import IssuerSpan

log = structlog.get_logger(__name__)

#: How long a symbol must be quiet between two issuers before the handover reads
#: as a change of hands rather than a corporate reorganisation. A re-incorporation,
#: a redomicile, a holding-company insertion or a rename mints a new CUSIP and
#: keeps trading — Accenture's took two days. A genuine reuse leaves the symbol
#: dormant while the old issuer is wound up and the new one registers; Bed Bath's
#: took twenty-six months. Measured across the live roster the two shapes are
#: cleanly separated, and 180 days sits in the thin trough between them — about
#: the 120 trading sessions the Phase 8J price-discontinuity audit used.
HANDOVER_GAP_DAYS = 180


@dataclass(frozen=True)
class Handover:
    """One symbol passing from one CUSIP issuer to another.

    Attributes:
        from_issuer: The issuer base that held the symbol first.
        to_issuer: The issuer base that held it next.
        gap_start: Last date the first issuer was seen.
        gap_end: First date the second issuer was seen.
        gap_days: Calendar days between them. Negative where the windows overlap.
        bars_before: Stored bars on the live row dated at or before `gap_start`.
            Counted only for a plausible change of hands; zero otherwise.
        bars_after: Stored bars dated at or after `gap_end`, on the same terms.
        price_break_days: The longest run of calendar days with no stored bar,
            measured across the handover window from the last bar before it to
            the first bar after it. Zero where the row does not span the window.
    """

    from_issuer: str
    to_issuer: str
    gap_start: date
    gap_end: date
    gap_days: int
    bars_before: int
    bars_after: int
    price_break_days: int = 0

    @property
    def is_change_of_hands(self) -> bool:
        """Whether the gap is long enough to read as a reuse, not a reorganisation."""
        return self.gap_days > HANDOVER_GAP_DAYS

    @property
    def row_spans_it(self) -> bool:
        """Whether the live row holds price bars on both sides of the handover."""
        return self.bars_before > 0 and self.bars_after > 0

    @property
    def row_stopped_trading(self) -> bool:
        """Whether the row's own bars show it stopping and restarting in the window.

        The filter that separates a genuine change of hands from a company whose
        deliveries merely stopped failing. Without it, a continuously-traded name
        with a quiet stretch in the fails record reads as two companies.
        """
        return self.price_break_days > HANDOVER_GAP_DAYS

    @property
    def contaminates(self) -> bool:
        """Whether this handover puts two companies' data in one row."""
        return self.is_change_of_hands and self.row_spans_it and self.row_stopped_trading


@dataclass(frozen=True)
class ContaminatedCompany:
    """A live company row demonstrably holding two issuers' history.

    Attributes:
        company_id: The live row.
        ticker: Its symbol.
        name: Its stored name.
        issuers: The CUSIP issuer bases seen under the symbol, earliest first.
        handovers: The handovers that contaminate this row — those the row's own
            price history spans.
        status: The symbol's verdict — `REUSED` or `AMBIGUOUS`.
        detail: The sentence a report prints.
    """

    company_id: int
    ticker: str
    name: str
    issuers: tuple[str, ...]
    handovers: tuple[Handover, ...]
    status: str
    detail: str


@dataclass(frozen=True)
class ContaminationReport:
    """Every live row the evidence implicates, and everything ruled out on the way.

    Attributes:
        checked: Live company rows examined.
        with_evidence: How many the fails record carries any row for.
        multi_issuer: How many carry more than one CUSIP issuer under their symbol.
        reorganisations: Handovers completed inside `HANDOVER_GAP_DAYS` — one
            company reminting its CUSIP, not two companies.
        changes_of_hands: Handovers with a longer gap in the fails record.
        spanned: Of those, how many the live row holds bars on both sides of.
        traded_through: Of those, how many the row traded straight through —
            the fails record went quiet, the security did not.
        contaminated: Rows whose own price history stops and restarts across a
            change of hands.
    """

    checked: int = 0
    with_evidence: int = 0
    multi_issuer: int = 0
    reorganisations: int = 0
    changes_of_hands: int = 0
    spanned: int = 0
    traded_through: int = 0
    contaminated: tuple[ContaminatedCompany, ...] = field(default_factory=tuple)

    @property
    def without_evidence(self) -> int:
        """Live rows the fails record carries nothing for.

        Not a finding. A company whose deliveries never failed is absent from the
        file, and absence there is evidence of nothing at all.
        """
        return self.checked - self.with_evidence

    def summary(self) -> str:
        """One line describing the scan."""
        return (
            f"contamination: {len(self.contaminated)} of {self.checked} live company rows "
            f"hold two issuers' history. {self.multi_issuer} rows carry more than one CUSIP "
            f"issuer; of their handovers {self.reorganisations} completed inside "
            f"{HANDOVER_GAP_DAYS} days (a reorganisation, not a reuse), {self.changes_of_hands} "
            f"did not, {self.spanned} of those are spanned by the row's own bars, and "
            f"{self.traded_through} of *those* traded straight through — the fails record "
            f"went quiet, the security did not. "
            f"{self.with_evidence} rows had any fails-to-deliver evidence at all"
        )


def _bar_counts(session: Session, company_id: int, before: date, after: date) -> tuple[int, int]:
    """Count a company's stored bars on each side of a handover."""
    counts = session.execute(
        select(
            func.count().filter(PriceHistory.date <= before),
            func.count().filter(PriceHistory.date >= after),
        ).where(PriceHistory.company_id == company_id)
    ).one()
    return (int(counts[0] or 0), int(counts[1] or 0))


def _price_break_days(session: Session, company_id: int, start: date, end: date) -> int:
    """Return the longest run of days with no stored bar across a handover window.

    Measured from the last bar at or before `start` to the first bar at or after
    `end`, so a row that traded continuously reports a few days and one that
    stopped reports the length of its hole.

    Args:
        session: An open session.
        company_id: The live row.
        start: Last date the outgoing issuer was seen.
        end: First date the incoming issuer was seen.

    Returns:
        The longest gap in calendar days, or 0 when the row does not bracket the
        window on both sides.
    """
    bracket = session.execute(
        select(
            func.max(PriceHistory.date).filter(PriceHistory.date <= start),
            func.min(PriceHistory.date).filter(PriceHistory.date >= end),
        ).where(PriceHistory.company_id == company_id)
    ).one()
    last_before, first_after = bracket
    if last_before is None or first_after is None:
        return 0
    inside = list(
        session.scalars(
            select(PriceHistory.date)
            .where(
                PriceHistory.company_id == company_id,
                PriceHistory.date > start,
                PriceHistory.date < end,
            )
            .order_by(PriceHistory.date)
        )
    )
    sequence: list[date] = [last_before, *inside, first_after]
    return max(int((right - left).days) for left, right in pairwise(sequence))


def _handovers(session: Session, company_id: int, spans: tuple[IssuerSpan, ...]) -> list[Handover]:
    """Build the handovers between one symbol's consecutive issuers."""
    handovers: list[Handover] = []
    for previous, following in pairwise(spans):
        gap_start = previous.last_settlement
        gap_end = following.first_settlement
        gap_days = (gap_end - gap_start).days
        # Only a plausible change of hands is worth a price query; a two-day
        # reorganisation gap is settled without touching six million bars.
        before = after = 0
        break_days = 0
        if gap_days > HANDOVER_GAP_DAYS:
            before, after = _bar_counts(session, company_id, gap_start, gap_end)
            if before > 0 and after > 0:
                break_days = _price_break_days(session, company_id, gap_start, gap_end)
        handovers.append(
            Handover(
                from_issuer=previous.issuer_base,
                to_issuer=following.issuer_base,
                gap_start=gap_start,
                gap_end=gap_end,
                gap_days=gap_days,
                bars_before=before,
                bars_after=after,
                price_break_days=break_days,
            )
        )
    return handovers


def detect_contaminated_companies(session: Session) -> ContaminationReport:
    """Find live company rows whose stored history spans a symbol handover.

    Reads only. The report is the deliverable; no row is merged, split, renamed
    or flagged in the database, because deciding which of two issuers a price bar
    belongs to is a data operation and not a detection one.

    Args:
        session: An open session.

    Returns:
        What was checked, what was ruled out on the way, and what is implicated.
    """
    companies = CompanyRepository(session).list_all(active_only=True)
    grouped = FtdIdentityObservationRepository(session).for_symbols(
        [company.ticker for company in companies]
    )

    contaminated: list[ContaminatedCompany] = []
    with_evidence = 0
    multi_issuer = 0
    reorganisations = 0
    changes_of_hands = 0
    spanned = 0
    traded_through = 0

    for company in companies:
        rows = grouped.get(company.ticker.upper(), [])
        if not rows:
            # Absence proves nothing: the file lists a security only when a
            # delivery in it failed. Not a finding, and never counted as one.
            continue
        with_evidence += 1
        verdict = classify_symbol(company.ticker, [as_evidence(row) for row in rows])
        if verdict.status not in (SymbolStatus.REUSED, SymbolStatus.AMBIGUOUS):
            continue
        multi_issuer += 1

        handovers = _handovers(session, company.id, verdict.spans)
        reorganisations += sum(1 for handover in handovers if not handover.is_change_of_hands)
        changes_of_hands += sum(1 for handover in handovers if handover.is_change_of_hands)
        spanned += sum(
            1 for handover in handovers if handover.is_change_of_hands and handover.row_spans_it
        )
        traded_through += sum(
            1
            for handover in handovers
            if handover.is_change_of_hands
            and handover.row_spans_it
            and not handover.row_stopped_trading
        )
        mixing = tuple(handover for handover in handovers if handover.contaminates)
        if not mixing:
            continue

        contaminated.append(
            ContaminatedCompany(
                company_id=company.id,
                ticker=company.ticker,
                name=company.name,
                issuers=tuple(span.issuer_base for span in verdict.spans),
                handovers=mixing,
                status=str(verdict.status),
                detail="; ".join(
                    f"{handover.from_issuer} to {handover.to_issuer} over "
                    f"{handover.gap_start}..{handover.gap_end} ({handover.gap_days} days), "
                    f"with {handover.bars_before} stored bars before, "
                    f"{handover.bars_after} after, and a {handover.price_break_days}-day "
                    f"break in the row's own price series"
                    for handover in mixing
                ),
            )
        )

    log.info(
        "identity.contamination_scan",
        checked=len(companies),
        with_evidence=with_evidence,
        multi_issuer=multi_issuer,
        reorganisations=reorganisations,
        changes_of_hands=changes_of_hands,
        spanned=spanned,
        traded_through=traded_through,
        contaminated=len(contaminated),
    )
    return ContaminationReport(
        checked=len(companies),
        with_evidence=with_evidence,
        multi_issuer=multi_issuer,
        reorganisations=reorganisations,
        changes_of_hands=changes_of_hands,
        spanned=spanned,
        traded_through=traded_through,
        contaminated=tuple(sorted(contaminated, key=lambda item: item.ticker)),
    )
