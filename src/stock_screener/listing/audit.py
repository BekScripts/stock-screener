"""What the listing evidence covers, and — more importantly — what it does not.

Every figure here is a count of *evidence*, never of membership. The distinction
the whole phase turns on is that a security absent from a month's file is
`UNKNOWN`, not absent from the market, so this module never reports a "not
listed" total and there is no state available for it to report.

`censored_venue` is the number that most needs saying out loud: NYSE American
appears in no file this layer imports, so its members can be neither confirmed
nor excluded. Counting them is the only honest thing to do with a blind spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import Integer, func, select

from data_access.models import Company, Delisting, ListingObservation, ListingSourceFile
from domain import ListingEvidenceState, Venue, continuous_runs
from domain.listing import MonthlyObservation

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)

#: Exchange spellings this project stores for NYSE American. The venue the free
#: source cannot see, listed here so the censored population is countable.
NYSE_AMERICAN = frozenset({"AMEX", "NYSE AMERICAN", "NYSEAMERICAN"})


@dataclass(frozen=True)
class MonthCoverage:
    """One month's evidence.

    Attributes:
        month: The observation month.
        nasdaq: Symbols observed on Nasdaq.
        nyse: Symbols observed on NYSE.
        duplicates: Symbols observed on both venues in the same month, which
            should be zero — the tapes are disjoint.
    """

    month: date
    nasdaq: int
    nyse: int
    duplicates: int


@dataclass(frozen=True)
class ListingAudit:
    """The coverage report.

    Attributes:
        months: Per-month counts.
        by_identity: Observation counts by identity state.
        live_observed: Live companies observed at least once.
        live_total: Live companies.
        live_censored: Live companies stored on NYSE American.
        delisted_observed: Delisted symbols in scope observed at least once.
        delisted_total: Delisted symbols in scope.
        sources_imported: Files imported.
        sources_quarantined: Files read and refused.
    """

    months: tuple[MonthCoverage, ...] = field(default_factory=tuple)
    by_identity: dict[str, int] = field(default_factory=dict)
    live_observed: int = 0
    live_total: int = 0
    live_censored: int = 0
    delisted_observed: int = 0
    delisted_total: int = 0
    sources_imported: int = 0
    sources_quarantined: int = 0

    @property
    def live_unknown(self) -> int:
        """Live companies with no observation — **UNKNOWN**, never not-listed."""
        return self.live_total - self.live_observed

    @property
    def delisted_unknown(self) -> int:
        """In-scope delisted symbols with no observation. Also UNKNOWN."""
        return self.delisted_total - self.delisted_observed

    def lines(self) -> tuple[str, ...]:
        """The report, one line each."""
        first = self.months[0].month if self.months else None
        last = self.months[-1].month if self.months else None
        return (
            f"months covered: {len(self.months)} ({first}..{last})",
            f"source files: {self.sources_imported} imported, "
            f"{self.sources_quarantined} quarantined",
            f"live universe: {self.live_observed}/{self.live_total} observed, "
            f"{self.live_unknown} UNKNOWN, {self.live_censored} on a censored venue",
            f"delisted in scope: {self.delisted_observed}/{self.delisted_total} observed, "
            f"{self.delisted_unknown} UNKNOWN",
            "identity: " + ", ".join(f"{k}={v}" for k, v in sorted(self.by_identity.items())),
        )


def build_listing_audit(session: Session, *, scope_from: date) -> ListingAudit:
    """Measure what the imported evidence covers.

    Args:
        session: An open session.
        scope_from: The first month in scope, for the delisted cohort.

    Returns:
        The counts.
    """
    months: list[MonthCoverage] = []
    rows = session.execute(
        select(
            ListingObservation.observation_month,
            func.sum(func.cast(ListingObservation.venue == str(Venue.NASDAQ), Integer)),
            func.sum(func.cast(ListingObservation.venue == str(Venue.NYSE), Integer)),
        ).group_by(ListingObservation.observation_month)
    )
    for month, nasdaq, nyse in rows:
        duplicates = session.execute(
            select(func.count()).select_from(
                select(ListingObservation.symbol)
                .where(ListingObservation.observation_month == month)
                .group_by(ListingObservation.symbol)
                .having(func.count() > 1)
                .subquery()
            )
        ).scalar_one()
        months.append(
            MonthCoverage(
                month=month, nasdaq=int(nasdaq or 0), nyse=int(nyse or 0), duplicates=duplicates
            )
        )
    months.sort(key=lambda m: m.month)

    by_identity: dict[str, int] = {
        str(state): int(count)
        for state, count in session.execute(
            select(ListingObservation.identity_state, func.count()).group_by(
                ListingObservation.identity_state
            )
        ).all()
    }

    live = {
        (c.ticker or "").upper(): (c.exchange or "").upper()
        for c in session.execute(select(Company)).scalars()
    }
    observed = {
        symbol for (symbol,) in session.execute(select(ListingObservation.symbol).distinct())
    }
    live_observed = sum(1 for t in live if t in observed)
    live_censored = sum(1 for t, ex in live.items() if ex in NYSE_AMERICAN)

    delisted = {
        (t or "").upper()
        for (t,) in session.execute(
            select(Delisting.ticker).where(Delisting.effective_date >= scope_from).distinct()
        )
    }
    delisted_observed = sum(1 for t in delisted if t in observed)

    # `.all()` is load-bearing: a SQLAlchemy Result carries `keys()`, so `dict()`
    # over one treats it as a mapping and subscripts it rather than iterating.
    statuses: dict[str, int] = {
        str(status): int(count)
        for status, count in session.execute(
            select(ListingSourceFile.status, func.count()).group_by(ListingSourceFile.status)
        ).all()
    }

    audit = ListingAudit(
        months=tuple(months),
        by_identity=by_identity,
        live_observed=live_observed,
        live_total=len(live),
        live_censored=live_censored,
        delisted_observed=delisted_observed,
        delisted_total=len(delisted),
        sources_imported=statuses.get("IMPORTED", 0),
        sources_quarantined=statuses.get("QUARANTINED", 0),
    )
    log.info("listing audit", **{"lines": audit.lines()})
    return audit


def runs_for_symbol(session: Session, symbol: str, venue: Venue) -> tuple[tuple[date, date], ...]:
    """Return a symbol's unbroken observed runs on one venue.

    A run is an **inferred** continuity claim; its endpoints are observed and the
    span between them is not. Two runs separated by a hole stay separate, which
    is what keeps one issuer's history from being joined to the next occupant of
    its ticker.
    """
    rows = session.execute(
        select(ListingObservation.observation_month)
        .where(
            (ListingObservation.symbol == symbol.upper()) & (ListingObservation.venue == str(venue))
        )
        .order_by(ListingObservation.observation_month)
    )
    observations = [
        MonthlyObservation(symbol=symbol.upper(), venue=venue, month=month) for (month,) in rows
    ]
    return continuous_runs(observations, venue)


def evidence_state_on(
    session: Session, symbol: str, venue: Venue, as_of: date
) -> ListingEvidenceState:
    """Return the month-resolution state for one symbol, venue and date."""
    hit = session.execute(
        select(ListingObservation.id).where(
            (ListingObservation.symbol == symbol.upper())
            & (ListingObservation.venue == str(venue))
            & (ListingObservation.observation_month == as_of.replace(day=1))
        )
    ).first()
    return (
        ListingEvidenceState.MARKET_SHARE_OBSERVED
        if hit is not None
        else ListingEvidenceState.UNKNOWN
    )
