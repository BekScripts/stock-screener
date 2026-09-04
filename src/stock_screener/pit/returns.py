"""Measure what happened after each point-in-time observation date.

The capture record says what was knowable. This says what followed, and without
it no question about whether the score predicts anything can be asked at all.

The measurement itself is in `domain.outcomes`, which is pure. This module is
the retrieval around it: the market's session calendar from the benchmark
series, each security's own bars, and the append.

**The calendar comes from the benchmark, not from the securities.** A session is
a day the market was open, which the benchmark answers because it trades every
one of them. Deriving it from a security's own bars would let a security that
stopped trading appear to reach its horizon on schedule, which is precisely
backwards: the market went on without it.

Re-running is cheap and safe. A horizon already measured is skipped before its
prices are read, and a truncated horizon is simply measured again later, when
enough sessions have passed for it to realise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    ForwardReturnRepository,
    HistoricalSecurityRepository,
)
from domain import FORWARD_HORIZONS, OutcomeStatus, forward_returns
from stock_screener.pit.prices import HistoricalPriceSource, RepositoryPriceSource

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from stock_screener.pit.prices import PriceSource

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReturnMeasurementResult:
    """What one measurement pass did.

    Attributes:
        observation_date: The date measured from.
        companies: How many companies were examined.
        stored: How many horizon rows were newly appended.
        by_status: How many of each status, so a run that produced mostly
            `TRUNCATED` is visibly a run that ran too early rather than a run
            that failed.
    """

    observation_date: date
    companies: int = 0
    stored: int = 0
    by_status: tuple[tuple[str, int], ...] = ()

    def summary(self) -> str:
        """Return a one-line summary for the CLI."""
        breakdown = ", ".join(f"{status} {count}" for status, count in self.by_status)
        return (
            f"{self.observation_date}: {self.companies} companies, "
            f"{self.stored} measurements stored ({breakdown or 'none'})"
        )


def measure_forward_returns(
    session: Session,
    observation_date: date,
    *,
    benchmark: str,
    horizons: Sequence[int] = FORWARD_HORIZONS,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    prices: PriceSource | None = None,
) -> ReturnMeasurementResult:
    """Measure and store forward returns from one observation date.

    Args:
        session: Open database session.
        observation_date: The date the snapshots describe.
        benchmark: The symbol whose history defines the session calendar.
        horizons: Session counts to measure. Defaults to `FORWARD_HORIZONS`.
        tickers: Restrict to these companies. None means every stored company.
        limit: Cap on how many companies to measure, for a rehearsal run.
        prices: Where to read each company's bars from. Defaults to a query per
            company. A backfill measuring many observation dates passes a cache
            holding the price record, which returns the same bars without
            re-reading the table on every date.

    Returns:
        What was measured and what was stored.
    """
    calendar = [bar.date for bar in BenchmarkPriceRepository(session).list_bars(benchmark)]
    if not calendar:
        log.warning("no benchmark history, so there is no session calendar")
        return ReturnMeasurementResult(observation_date=observation_date)

    companies = CompanyRepository(session).list_all()
    wanted = {ticker.upper() for ticker in tickers} if tickers else None
    if wanted is not None:
        companies = [company for company in companies if company.ticker in wanted]
    companies = sorted(companies, key=lambda company: company.ticker)
    if limit is not None:
        companies = companies[:limit]

    repository = ForwardReturnRepository(session)
    already = repository.measured_horizons(observation_date)
    price_source = prices or RepositoryPriceSource(session)

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for company in companies:
        outstanding = [h for h in horizons if (company.id, h) not in already]
        if not outstanding:
            continue

        bars = price_source.bars_until(company.id)
        if not bars:
            continue
        opens = {bar.date: bar.open for bar in bars if bar.open is not None}
        closes = {bar.date: bar.close for bar in bars if bar.close is not None}

        for outcome in forward_returns(
            calendar, opens, closes, observation_date, horizons=outstanding
        ):
            counts[outcome.status.value] = counts.get(outcome.status.value, 0) + 1
            rows.append(
                {
                    "company_id": company.id,
                    "observation_date": observation_date,
                    "horizon_sessions": outcome.horizon_sessions,
                    "status": outcome.status.value,
                    "entry_date": outcome.entry_date,
                    "entry_price": outcome.entry_price,
                    "exit_date": outcome.exit_date,
                    "exit_price": outcome.exit_price,
                    "total_return": outcome.total_return,
                    "last_seen_date": outcome.last_seen_date,
                    "last_seen_price": outcome.last_seen_price,
                }
            )

    stored = repository.add_all(rows)
    order = [status.value for status in OutcomeStatus]
    return ReturnMeasurementResult(
        observation_date=observation_date,
        companies=len(companies),
        stored=stored,
        by_status=tuple((status, counts[status]) for status in order if status in counts),
    )


@dataclass(frozen=True)
class HistoricalReturnMeasurementResult:
    """What one measurement pass over delisted securities did.

    Attributes:
        observation_date: The date measured from.
        securities: How many historical securities were examined.
        priced: How many of those had any stored bars at all. The gap between
            this and `securities` is the free record's reach, not a failure.
        stored: How many horizon rows were newly appended.
        by_status: How many of each status.
    """

    observation_date: date
    securities: int = 0
    priced: int = 0
    stored: int = 0
    by_status: tuple[tuple[str, int], ...] = ()

    def summary(self) -> str:
        """Return a one-line summary for the CLI."""
        breakdown = ", ".join(f"{status} {count}" for status, count in self.by_status)
        return (
            f"{self.observation_date}: {self.securities} historical securities "
            f"({self.priced} priced), {self.stored} measurements stored "
            f"({breakdown or 'none'})"
        )


def measure_historical_forward_returns(
    session: Session,
    observation_date: date,
    *,
    benchmark: str,
    horizons: Sequence[int] = FORWARD_HORIZONS,
    security_ids: Sequence[int] | None = None,
    limit: int | None = None,
) -> HistoricalReturnMeasurementResult:
    """Measure and store forward returns for securities that left the market.

    The company path with one substitution: the bars come from
    `historical_prices` under a `historical_security_id`, and the row written
    carries that identity instead of a `company_id`. Everything else is the same
    — the same `domain.forward_returns`, the same benchmark calendar, the same
    four statuses, the same table.

    That sameness is the point of Phase 9B. A delisted security's outcome is not
    a parallel dataset to be unioned in by a caller who remembers to; it is a row
    in `forward_returns` that any ordinary query over a decision date returns
    without asking for it.

    **The calendar still comes from the benchmark.** For a delisted security that
    matters more than it does for a survivor: deriving sessions from its own bars
    would let a security that stopped trading in March appear to reach its
    60-session horizon on time, when what actually happened is that the market
    went on without it. Reading the benchmark is what turns that into
    `INTERRUPTED`.

    **No `companies` row is consulted, and none is needed.** The security's
    identity, its bars and its calendar are all reachable without one, which is
    the property the survivorship test exercises directly.

    Args:
        session: Open database session.
        observation_date: The date to measure from.
        benchmark: The symbol whose history defines the session calendar.
        horizons: Session counts to measure. Defaults to `FORWARD_HORIZONS`.
        security_ids: Restrict to these historical securities. None means every
            stored one.
        limit: Cap on how many securities to measure, for a rehearsal run.

    Returns:
        What was measured and what was stored.
    """
    calendar = [bar.date for bar in BenchmarkPriceRepository(session).list_bars(benchmark)]
    if not calendar:
        log.warning("no benchmark history, so there is no session calendar")
        return HistoricalReturnMeasurementResult(observation_date=observation_date)

    securities = HistoricalSecurityRepository(session).list_all()
    if security_ids is not None:
        wanted = set(security_ids)
        securities = [security for security in securities if security.id in wanted]
    securities = sorted(securities, key=lambda security: security.id)
    if limit is not None:
        securities = securities[:limit]

    repository = ForwardReturnRepository(session)
    already = repository.measured_historical_horizons(observation_date)
    prices = HistoricalPriceSource(session)

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    priced = 0
    for security in securities:
        outstanding = [h for h in horizons if (security.id, h) not in already]
        if not outstanding:
            continue

        bars = prices.bars_until(security.id)
        if not bars:
            # No stored bars. The security is not dropped from the record — it
            # simply has no measurable outcome, and the audit reports the gap
            # rather than a zero.
            continue
        priced += 1
        opens = {bar.date: bar.open for bar in bars if bar.open is not None}
        closes = {bar.date: bar.close for bar in bars if bar.close is not None}

        for outcome in forward_returns(
            calendar, opens, closes, observation_date, horizons=outstanding
        ):
            counts[outcome.status.value] = counts.get(outcome.status.value, 0) + 1
            rows.append(
                {
                    "historical_security_id": security.id,
                    "observation_date": observation_date,
                    "horizon_sessions": outcome.horizon_sessions,
                    "status": outcome.status.value,
                    "entry_date": outcome.entry_date,
                    "entry_price": outcome.entry_price,
                    "exit_date": outcome.exit_date,
                    "exit_price": outcome.exit_price,
                    "total_return": outcome.total_return,
                    "last_seen_date": outcome.last_seen_date,
                    "last_seen_price": outcome.last_seen_price,
                }
            )

    stored = repository.add_all(rows)
    order = [status.value for status in OutcomeStatus]
    return HistoricalReturnMeasurementResult(
        observation_date=observation_date,
        securities=len(securities),
        priced=priced,
        stored=stored,
        by_status=tuple((status, counts[status]) for status in order if status in counts),
    )
