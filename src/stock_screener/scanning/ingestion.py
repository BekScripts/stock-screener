"""Pulling provider data into the database.

Three jobs, run in order: refresh the universe, refresh price history, refresh
fundamentals. Each takes its provider and session as arguments — nothing is
constructed here — so all three are testable against fakes and an in-memory
database.

**One bad ticker must never end a market-wide scan.** A provider that 500s on one
obscure symbol out of four thousand is a Tuesday, not an incident. Every per-
ticker unit of work is wrapped, the failure is logged with the ticker, and the
loop continues. The counts come back in an `IngestionReport` so the operator sees
"81 failed" rather than a silent shortfall.

Incremental by default: price history resumes from the last stored session, and
fundamentals are skipped entirely when the provider's newest quarter is one
already held. Re-downloading four years of unchanged statements nightly is how a
rate limit gets hit for no benefit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.exc import SQLAlchemyError

from api_clients import ProviderAuthError, ProviderError, ProviderPlanError
from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
)
from domain import is_supported_listing, normalise_ticker

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from api_clients import FundamentalsProvider, MarketDataProvider
    from domain import PriceBar
    from stock_screener.config import Settings

    #: A company queued for a price fetch: its id, ticker, and the last session
    #: already stored (None when it has no history yet).
    _PriceTarget = tuple[int, str, date | None]

log = structlog.get_logger(__name__)

#: Sessions re-fetched behind the last stored bar. Providers correct recent bars
#: for late-reported trades, and the upsert makes re-fetching them free.
_PRICE_OVERLAP_DAYS = 5

#: Consecutive subscription rejections, with nothing yet succeeding, before a
#: pass gives up.
#:
#: A metered plan rejects with the same status for two very different reasons:
#: the request shape is forbidden outright (too many quarters — every company
#: will fail), or the symbol is outside the plan's coverage (some companies
#: fail). Parsing the provider's prose to tell them apart would be brittle, so
#: the distinction is drawn from behaviour instead — but a streak alone is not
#: enough. A plan covering only a tenth of the market produces long runs of
#: rejections perfectly legitimately, and aborting on those would make the tool
#: useless on exactly the plans that need it most.
#:
#: So the breaker also requires that **nothing has succeeded yet**. Patchy
#: coverage yields an early success and the streak resets; a forbidden request
#: shape never yields one. Fifty wasted requests is a cheap price for not
#: spending the whole daily quota on identical errors.
_PLAN_ERROR_ABORT_STREAK = 50


@dataclass(slots=True)
class IngestionReport:
    """What one ingestion pass did.

    Attributes:
        processed: Units of work attempted.
        succeeded: Units that completed and wrote data.
        skipped: Units deliberately not done — already up to date, or filtered
            out of the universe. Not a failure.
        failed: Units that raised. Each is logged with its ticker.
        blocked: Units the provider's subscription would not serve. Counted
            apart from `failed` because the fix is a billing decision, not a
            retry — and because a large number here is the signal that the plan
            does not cover the part of the market being scanned.
        rows_written: Rows inserted or updated across all units.
    """

    processed: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    rows_written: int = 0
    failures: list[str] = field(default_factory=list)
    blocked_tickers: list[str] = field(default_factory=list)

    def record_failure(self, ticker: str) -> None:
        """Count a failed unit of work and remember its ticker."""
        self.failed += 1
        self.failures.append(ticker)

    def record_blocked(self, ticker: str) -> None:
        """Count a unit the subscription would not serve."""
        self.blocked += 1
        self.blocked_tickers.append(ticker)

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        blocked = f" blocked={self.blocked}" if self.blocked else ""
        return (
            f"processed={self.processed} succeeded={self.succeeded} "
            f"skipped={self.skipped} failed={self.failed}{blocked} "
            f"rows={self.rows_written}"
        )


def update_universe(
    session: Session,
    provider: MarketDataProvider,
    *,
    apply_universe_filter: bool = True,
) -> IngestionReport:
    """Refresh the company table from the market-data provider's asset list.

    Args:
        session: Open database session. The caller commits.
        provider: Source of the asset list.
        apply_universe_filter: Drop listings that are not common stock on a
            supported exchange. Disable only to inspect what the provider offers.

    Returns:
        Counts for the pass. Listings filtered out are `skipped`, not `failed`.

    Raises:
        ProviderError: If the universe itself could not be fetched. Unlike a
            per-ticker failure this is fatal — there is nothing to iterate over.
    """
    report = IngestionReport()
    companies = CompanyRepository(session)

    profiles = provider.get_stock_universe()
    log.info("universe fetched", listings=len(profiles))

    for profile in profiles:
        report.processed += 1
        if apply_universe_filter and (
            profile.is_fund
            or not is_supported_listing(profile.ticker, profile.name, profile.exchange)
        ):
            report.skipped += 1
            continue

        # A rejected row is a database error, not a provider one — the previous
        # `except ProviderError` here could never fire, so a single oversized
        # name or bad value would abort the entire universe refresh. The
        # savepoint keeps one bad listing from poisoning the outer transaction.
        try:
            with session.begin_nested():
                companies.upsert_profile(profile)
        except SQLAlchemyError:
            log.exception("universe upsert failed", ticker=profile.ticker)
            report.record_failure(profile.ticker)
        else:
            report.succeeded += 1
            report.rows_written += 1

    log.info("universe updated", summary=report.summary())
    return report


def update_market_data(
    session: Session,
    provider: MarketDataProvider,
    settings: Settings,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    today: date | None = None,
) -> IngestionReport:
    """Refresh daily price history for stored companies.

    Fetches in batches and resumes from each company's last stored session, less
    a few days of overlap so corrected bars are picked up.

    Args:
        session: Open database session. The caller commits.
        provider: Source of the price bars.
        settings: Supplies the history window and batch size.
        tickers: Restrict to these symbols. Defaults to every stored company.
        limit: Process at most this many companies, in ticker order. Use it to
            bound a first run against a metered provider.
        today: Treat this as the current date. Injected so tests are not
            dependent on when they run.

    Returns:
        Counts for the pass.

    Raises:
        ProviderAuthError: If the credentials are rejected. Fatal rather than
            counted, because it fails identically for every company.
    """
    report = IngestionReport()
    companies = CompanyRepository(session)
    prices = PriceHistoryRepository(session)

    as_of = today or datetime.now(UTC).date()
    default_start = as_of - timedelta(days=settings.price_history_days)

    # Pair each company with the last session already stored, so the fetch
    # resumes there instead of re-downloading the full window every night.
    selected = [
        (company_id, ticker, prices.latest_date(company_id))
        for company_id, ticker in _select_companies(companies, tickers, limit)
    ]

    for batch in _batches(selected, settings.provider_batch_size):
        _ingest_price_batch(batch, provider, prices, default_start, as_of, report)

    log.info("market data updated", summary=report.summary())
    return report


def _ingest_price_batch(
    batch: list[_PriceTarget],
    provider: MarketDataProvider,
    prices: PriceHistoryRepository,
    default_start: date,
    as_of: date,
    report: IngestionReport,
) -> None:
    """Fetch and store one batch, degrading to per-ticker on a batch failure."""
    # One request serves the whole batch, so the window has to be the widest any
    # member needs: a company with no history at all pulls the full period.
    starts = [
        (stored - timedelta(days=_PRICE_OVERLAP_DAYS)) if stored else default_start
        for _, _, stored in batch
    ]
    start = min(starts) if starts else default_start
    symbols = [ticker for _, ticker, _ in batch]

    failed: set[str] = set()
    try:
        bars_by_ticker = provider.get_daily_prices_batch(symbols, start, as_of)
    except ProviderError:
        log.warning("price batch failed, retrying symbols individually", symbols=len(symbols))
        bars_by_ticker = _fetch_individually(provider, symbols, start, as_of, report, failed)

    for company_id, ticker, _ in batch:
        report.processed += 1
        if ticker in failed:
            continue

        bars = bars_by_ticker.get(ticker)
        if not bars:
            # The provider simply has no coverage for this symbol — a gap, not a
            # failure, and not something to retry tomorrow.
            report.skipped += 1
            continue

        report.rows_written += prices.upsert_bars(company_id, bars)
        report.succeeded += 1


def _fetch_individually(
    provider: MarketDataProvider,
    symbols: Sequence[str],
    start: date,
    end: date,
    report: IngestionReport,
    failed: set[str],
) -> dict[str, list[PriceBar]]:
    """Fall back to one request per ticker so one bad symbol loses only itself."""
    results: dict[str, list[PriceBar]] = {}
    for ticker in symbols:
        try:
            bars = provider.get_daily_prices(ticker, start, end)
        except ProviderAuthError:
            # Credentials are rejected for every symbol, not this one. Failing
            # the whole pass immediately beats four thousand identical
            # tracebacks that bury the one line explaining the cause.
            log.error("market data halted: the provider rejected the credentials")
            raise
        except ProviderError:
            log.exception("price fetch failed", ticker=ticker)
            report.record_failure(ticker)
            failed.add(ticker)
        else:
            if bars:
                results[ticker] = list(bars)
    return results


def update_benchmark(
    session: Session,
    provider: MarketDataProvider,
    settings: Settings,
    *,
    today: date | None = None,
) -> IngestionReport:
    """Refresh the broad-market benchmark's daily price history.

    One extra symbol fetched through the same provider as every other price.
    Relative strength is the company's return less this series', so without it
    the market confirmation component cannot be scored at all — which is why
    this runs before scoring rather than being folded into it.

    Args:
        session: Open database session. The caller commits.
        provider: Source of the price bars.
        settings: Supplies the benchmark symbol and the history window.
        today: Treat this as the current date. Injected so tests are not
            dependent on when they run.

    Returns:
        Counts for the pass, covering the single benchmark symbol.

    Raises:
        ProviderAuthError: If the credentials are rejected.
    """
    report = IngestionReport()
    prices = BenchmarkPriceRepository(session)
    symbol = normalise_ticker(settings.benchmark_symbol)

    as_of = today or datetime.now(UTC).date()
    stored = prices.latest_date(symbol)
    start = (
        stored - timedelta(days=_PRICE_OVERLAP_DAYS)
        if stored is not None
        else as_of - timedelta(days=settings.price_history_days)
    )

    report.processed += 1
    try:
        bars = provider.get_daily_prices(symbol, start, as_of)
    except ProviderAuthError:
        log.error("benchmark halted: the provider rejected the credentials")
        raise
    except ProviderError:
        log.exception("benchmark fetch failed", symbol=symbol)
        report.record_failure(symbol)
        return report

    if not bars:
        report.skipped += 1
        log.warning("benchmark returned no bars", symbol=symbol, start=str(start))
        return report

    report.rows_written += prices.upsert_bars(symbol, bars)
    report.succeeded += 1
    log.info("benchmark updated", symbol=symbol, summary=report.summary())
    return report


def update_fundamentals(
    session: Session,
    provider: FundamentalsProvider,
    settings: Settings,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> IngestionReport:
    """Refresh company profiles and quarterly statements.

    A company whose newest stored quarter matches the provider's newest is
    skipped without writing, which is what keeps a nightly run from re-fetching
    years of unchanged filings.

    Args:
        session: Open database session. The caller commits.
        provider: Source of the profiles and statements.
        settings: Supplies how many quarters to request.
        tickers: Restrict to these symbols. Defaults to every stored company.
        limit: Process at most this many companies, in ticker order. A metered
            provider charges per company, so a full universe can exhaust a daily
            quota — bound the first run and inspect it before widening.
        force: Re-store every period even when the provider has nothing newer.
            The incremental skip compares reporting dates, so it cannot see that
            the *adapter* changed — after fixing a normalisation bug the stored
            values are stale and only a forced pass replaces them.

    Returns:
        Counts for the pass.

    Raises:
        ProviderAuthError: If the credentials are rejected.
        ProviderPlanError: If the provider rejects the first
            `_PLAN_ERROR_ABORT_STREAK` companies and accepts none, which means
            the request shape is forbidden rather than the symbols being
            uncovered. Patchy coverage does not abort the pass.
    """
    report = IngestionReport()
    companies = CompanyRepository(session)
    snapshots = FinancialSnapshotRepository(session)
    plan_error_streak = 0

    for company_id, ticker in _select_companies(companies, tickers, limit):
        report.processed += 1
        try:
            written = _ingest_one_company(
                company_id, ticker, provider, companies, snapshots, settings, force=force
            )
        except ProviderAuthError:
            log.error("fundamentals halted: the provider rejected the credentials")
            raise
        except ProviderPlanError as exc:
            report.record_blocked(ticker)
            plan_error_streak += 1
            log.warning("fundamentals not covered by the plan", ticker=ticker)
            if plan_error_streak >= _PLAN_ERROR_ABORT_STREAK and report.succeeded == 0:
                log.error(
                    "fundamentals halted: the provider rejected every recent request",
                    consecutive=plan_error_streak,
                )
                raise ProviderPlanError(
                    f"the provider plan rejected the first {plan_error_streak} companies and "
                    f"accepted none; stopping to preserve the request quota. This usually "
                    f"means the request itself is not permitted rather than the symbols. "
                    f"Last message: {exc}"
                ) from exc
            continue
        except ProviderError:
            log.exception("fundamentals fetch failed", ticker=ticker)
            report.record_failure(ticker)
            continue

        plan_error_streak = 0
        if written is None:
            report.skipped += 1
        else:
            report.succeeded += 1
            report.rows_written += written

    log.info("fundamentals updated", summary=report.summary())
    return report


def _ingest_one_company(
    company_id: int,
    ticker: str,
    provider: FundamentalsProvider,
    companies: CompanyRepository,
    snapshots: FinancialSnapshotRepository,
    settings: Settings,
    *,
    force: bool = False,
) -> int | None:
    """Fetch and store one company's fundamentals.

    Returns:
        Rows written, or None when the company was already up to date.
    """
    profile = provider.get_company_profile(ticker)
    if profile is not None:
        companies.upsert_profile(profile)

    periods = provider.get_financial_statements(ticker, limit=settings.fundamentals_quarters)
    if not periods:
        log.debug("no fundamentals returned", ticker=ticker)
        return None

    stored_latest = snapshots.latest_period_end(company_id)
    provider_latest = max(period.period_end for period in periods)
    if not force and stored_latest is not None and provider_latest <= stored_latest:
        log.debug("fundamentals already current", ticker=ticker, period_end=str(stored_latest))
        return None

    return snapshots.upsert_periods(company_id, periods)


def _select_companies(
    companies: CompanyRepository,
    tickers: Sequence[str] | None,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    """Return `(id, ticker)` pairs for the companies to process, in ticker order.

    Args:
        companies: Repository to read from.
        tickers: Restrict to these symbols. None means every stored company.
        limit: Stop after this many. Applied after the ticker filter, and after
            ordering, so the same companies are chosen on every run — a limit
            that returned a different subset each time would make a partial
            ingest impossible to reason about.

    Returns:
        Companies to process, in ticker order.
    """
    wanted = {normalise_ticker(ticker) for ticker in tickers} if tickers else None
    selected = [
        (company.id, company.ticker)
        for company in companies.list_all()
        if wanted is None or company.ticker in wanted
    ]
    return selected[:limit] if limit is not None else selected


def _batches(items: list[_PriceTarget], size: int) -> list[list[_PriceTarget]]:
    """Split a list into chunks of at most `size`."""
    return [items[index : index + size] for index in range(0, len(items), max(1, size))]
