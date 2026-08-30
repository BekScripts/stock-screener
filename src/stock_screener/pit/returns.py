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
    PriceHistoryRepository,
)
from domain import FORWARD_HORIZONS, OutcomeStatus, forward_returns

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

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
) -> ReturnMeasurementResult:
    """Measure and store forward returns from one observation date.

    Args:
        session: Open database session.
        observation_date: The date the snapshots describe.
        benchmark: The symbol whose history defines the session calendar.
        horizons: Session counts to measure. Defaults to `FORWARD_HORIZONS`.
        tickers: Restrict to these companies. None means every stored company.
        limit: Cap on how many companies to measure, for a rehearsal run.

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
    prices = PriceHistoryRepository(session)

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for company in companies:
        outstanding = [h for h in horizons if (company.id, h) not in already]
        if not outstanding:
            continue

        bars = prices.list_for_company(company.id)
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
