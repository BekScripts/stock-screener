"""Discover the securities that left the market, so a study can see them.

Everything else in this package bounds what a past decision could know. This
answers the prior question of *which securities existed to be decided about*,
and without it the point-in-time work is precise about a universe that is
already wrong.

The measurement that motivates it: across 4,599 companies and the 120 sessions
after 2024-01-02, not one security in the database stopped trading. A universe
of that size loses roughly a hundred names to acquisitions and failures over six
months. The zero is a property of the `companies` table, which holds what is
investable today and therefore cannot contain anything that stopped being so.

**Discovery is by date range, not by symbol.** Asking the provider about a
symbol can only confirm the fate of a security already known, which is no help
when the missing ones are exactly those the database never had. The
corporate-action endpoint also answers without a symbol filter, and that is what
makes enumeration possible: 18 of 20 well-known large-cap delistings appear in
neither the active nor the inactive asset list, and the corporate-action record
has them.

What it does not have is a receivership. SVB and First Republic ended in FDIC
receivership rather than a merger or a removal, and neither appears under any
type. The gap is stated rather than filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from api_clients.errors import ProviderError
from data_access import CompanyRepository, DelistingRepository

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import DelistingRecord

log = structlog.get_logger(__name__)


@runtime_checkable
class DelistingSource(Protocol):
    """A provider that can enumerate delistings over a date range."""

    def list_delistings(self, start: date, end: date) -> list[DelistingRecord]:
        """Return every security that left the market in a window."""
        ...


@dataclass(frozen=True)
class DelistingDiscoveryResult:
    """What one discovery pass found.

    Attributes:
        fetched: How many delistings the provider reported.
        stored: How many were new.
        known: How many name a ticker the database currently carries. A high
            count is not reassurance — it mostly means the ticker was reissued
            to a different company, which is the join hazard this record exists
            to make visible.
        by_kind: How many of each kind.
        failures: `(window, reason)` for each window that could not be read.
    """

    fetched: int = 0
    stored: int = 0
    known: int = 0
    by_kind: tuple[tuple[str, int], ...] = ()
    failures: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        """Return a one-line summary for the CLI."""
        breakdown = ", ".join(f"{kind} {count}" for kind, count in self.by_kind)
        return (
            f"{self.fetched} delistings found, {self.stored} new, "
            f"{self.known} naming a current ticker ({breakdown or 'none'}), "
            f"{len(self.failures)} failed windows"
        )


def discover_delistings(
    session: Session,
    provider: object,
    *,
    start: date,
    end: date,
) -> DelistingDiscoveryResult:
    """Enumerate and store the securities that left the market in a window.

    Args:
        session: Open database session.
        provider: Market-data provider. Must satisfy `DelistingSource` or the
            pass returns an empty result without touching the database.
        start: Earliest effective date to discover.
        end: Latest effective date to discover.

    Returns:
        What was found and what was stored.
    """
    if not isinstance(provider, DelistingSource):
        log.warning(
            "provider cannot enumerate delistings, skipping",
            provider=type(provider).__name__,
        )
        return DelistingDiscoveryResult()

    window = f"{start} .. {end}"
    try:
        found = provider.list_delistings(start, end)
    except ProviderError as exc:
        # Never fatal. A universe without its delistings is the state this
        # repository has been in all along; failing a run does not improve it.
        log.warning("delisting discovery unavailable", window=window, error=str(exc))
        return DelistingDiscoveryResult(failures=((window, str(exc)),))

    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for record in found:
        counts[record.kind.value] = counts.get(record.kind.value, 0) + 1
        rows.append(
            {
                "ticker": record.symbol,
                "kind": record.kind.value,
                "effective_date": record.effective_date,
                "cash_per_share": record.cash_per_share,
                "acquirer_ticker": record.acquirer_symbol,
                "acquirer_rate": record.acquirer_rate,
                "acquiree_rate": record.acquiree_rate,
                "source": record.source,
                "source_id": record.source_id,
            }
        )

    stored = DelistingRepository(session).add_all(rows)
    known = _overlapping_tickers(session, {record.symbol for record in found})
    return DelistingDiscoveryResult(
        fetched=len(found),
        stored=stored,
        known=known,
        by_kind=tuple(sorted(counts.items(), key=lambda item: -item[1])),
    )


def _overlapping_tickers(session: Session, tickers: set[str]) -> int:
    """Return how many delisted tickers a live company currently claims."""
    live = {company.ticker for company in CompanyRepository(session).list_all()}
    return len(live & tickers)
