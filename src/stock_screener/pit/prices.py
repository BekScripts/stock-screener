"""Reading a security's bars as they stood on a past date.

Every point-in-time pass asks the same question of the price record — *what had
this security traded at by this date* — and the obvious implementation asks the
database once per company per date. That is correct and it is what
`RepositoryPriceSource` does. It is also quadratic: describing 1,150 sessions
over 5,700 companies means six and a half million queries over the same six
million rows, and the record is read from end to end on every one of them.

`CachedPriceSource` reads it once instead and slices. The bars it hands back are
the same bars, built by the same converter, bounded by the same `as_of` — the
difference is where they were read from, not what they say. Nothing here
computes anything: no formula, no window, no adjustment. It is retrieval, and it
exists so that a backfill over years of sessions is bounded by arithmetic rather
than by I/O.

The bound is the load-bearing part. `bars_until(company_id, as_of)` returns the
bars **on or before** `as_of` and never one after it, in both implementations,
because a pass that could see tomorrow's bar would not be a point-in-time pass
at all.
"""

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import select

from data_access import PriceHistory, PriceHistoryRepository, to_price_bar
from domain import PriceBar

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from sqlalchemy.orm import Session


log = structlog.get_logger(__name__)

#: How many rows the bulk read buffers at a time. Bounds peak memory during the
#: read itself; the assembled cache is what actually holds the record.
_READ_CHUNK_ROWS = 50_000


class PriceSource(Protocol):
    """Where a point-in-time pass reads a security's bars from."""

    def bars_until(self, company_id: int, as_of: date | None = None) -> Sequence[PriceBar]:
        """Return one company's bars on or before a date, oldest first.

        Args:
            company_id: The owning company's primary key.
            as_of: The last session that may be seen. None means every stored
                bar, which is what a forward-return measurement needs and what
                a feature computation must never be given.

        Returns:
            The bars, in date order. Empty when the company has none.
        """
        ...


class RepositoryPriceSource:
    """The default source: one query per company, straight from the table.

    This is the behaviour every caller had before a source could be injected,
    and it stays the default so that no existing pass changes.

    Args:
        session: The session to read in. Not owned.
    """

    def __init__(self, session: Session) -> None:
        self._repository = PriceHistoryRepository(session)

    def bars_until(self, company_id: int, as_of: date | None = None) -> Sequence[PriceBar]:
        """Return one company's bars on or before a date, oldest first."""
        rows = self._repository.list_for_company(company_id, until=as_of)
        return [to_price_bar(row) for row in rows]


class CachedPriceSource:
    """Every stored bar, read once and sliced per date.

    Holds the whole price record in memory — roughly 3.7 GB for six million
    bars — which is the trade a backfill wants: the read happens once instead of
    once per session described.

    A company absent from the cache has no bars, exactly as it would from the
    table. Nothing is fetched lazily, so a cache built before a write does not
    see that write; a backfill loads it once at the start and the price record
    does not move underneath it.
    """

    def __init__(self, bars: dict[int, list[PriceBar]]) -> None:
        """Bind a prepared cache.

        Args:
            bars: Bars per company, each list already in date order. Callers
                normally use `load` rather than this constructor.
        """
        self._bars = bars
        self._dates = {company_id: [bar.date for bar in rows] for company_id, rows in bars.items()}

    @classmethod
    def load(
        cls, session: Session, *, company_ids: Iterable[int] | None = None
    ) -> CachedPriceSource:
        """Read the price record into memory in one pass.

        Args:
            session: The session to read in.
            company_ids: Restrict to these companies. None loads every one,
                which is what a full backfill needs.

        Returns:
            The loaded cache.
        """
        # Columns, not entities. Loading six million rows through the ORM would
        # put six million instances in the identity map and exhaust memory long
        # before the read finished; the bars are built from the columns instead,
        # which is the same six-field copy `to_price_bar` performs on a row it
        # is handed one at a time.
        statement = select(
            PriceHistory.company_id,
            PriceHistory.date,
            PriceHistory.open,
            PriceHistory.high,
            PriceHistory.low,
            PriceHistory.close,
            PriceHistory.volume,
        ).order_by(PriceHistory.company_id, PriceHistory.date)
        wanted = None if company_ids is None else list(company_ids)
        if wanted is not None:
            statement = statement.where(PriceHistory.company_id.in_(wanted))

        bars: dict[int, list[PriceBar]] = {}
        for company_id, day, open_, high, low, close, volume in session.execute(
            statement
        ).yield_per(_READ_CHUNK_ROWS):
            bars.setdefault(company_id, []).append(
                PriceBar(date=day, open=open_, high=high, low=low, close=close, volume=volume)
            )

        source = cls(bars)
        log.info(
            "price cache loaded",
            companies=len(bars),
            bars=source.bar_count,
        )
        return source

    @property
    def bar_count(self) -> int:
        """How many bars the cache holds."""
        return sum(len(rows) for rows in self._bars.values())

    @property
    def companies(self) -> int:
        """How many companies have at least one bar."""
        return len(self._bars)

    def bars_until(self, company_id: int, as_of: date | None = None) -> Sequence[PriceBar]:
        """Return one company's bars on or before a date, oldest first.

        The slice is by binary search over the company's own session dates, so
        the cost is logarithmic in the company's history rather than linear in
        the record.
        """
        rows = self._bars.get(company_id)
        if not rows:
            return ()
        if as_of is None:
            return rows
        cut = bisect.bisect_right(self._dates[company_id], as_of)
        return rows[:cut] if cut else ()
