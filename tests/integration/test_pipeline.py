"""End-to-end tests: providers in, screened companies out.

These use real SQLite and the real ingestion, metric and screening code. Only
the provider transport is faked, which is the boundary this project owns. Nothing
here touches the network.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError

from api_clients import (
    MockFundamentals,
    MockMarketData,
    ProviderAuthError,
    ProviderPlanError,
)
from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
)
from domain import (
    CompanyProfile,
    EligibilityThresholds,
    ExclusionReason,
    FinancialPeriod,
    PriceBar,
)
from stock_screener.config import Settings
from stock_screener.scanning import (
    scan_market,
    update_benchmark,
    update_fundamentals,
    update_market_data,
    update_universe,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from conftest import Make

SETTINGS = Settings(environment="test")
THRESHOLDS = EligibilityThresholds()
LATEST_SESSION = date(2026, 6, 30)


def _run_pipeline(
    session: Session,
    market_data: MockMarketData,
    fundamentals: MockFundamentals,
) -> None:
    """Run all three ingestion passes against one session."""
    update_universe(session, market_data)
    session.flush()
    update_market_data(session, market_data, SETTINGS, today=LATEST_SESSION)
    update_fundamentals(session, fundamentals, SETTINGS)
    session.flush()


@pytest.mark.integration
def test_the_full_pipeline_produces_an_eligible_company(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
) -> None:
    market_data, fundamentals = providers

    _run_pipeline(session, market_data, fundamentals)
    result = scan_market(session, THRESHOLDS)

    assert result.processed == 1
    assert [row.ticker for row in result.eligible] == ["XYZ"]

    metrics = result.eligible[0].metrics
    assert metrics.price == pytest.approx(25.0)
    assert metrics.gross_margin == pytest.approx(0.4)
    assert metrics.trading_days_used == 20


@pytest.mark.integration
def test_running_the_pipeline_twice_does_not_duplicate_a_single_row(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
) -> None:
    market_data, fundamentals = providers

    _run_pipeline(session, market_data, fundamentals)
    counts = (
        CompanyRepository(session).count(),
        FinancialSnapshotRepository(session).count(),
        PriceHistoryRepository(session).count(),
    )

    _run_pipeline(session, market_data, fundamentals)

    assert counts == (
        CompanyRepository(session).count(),
        FinancialSnapshotRepository(session).count(),
        PriceHistoryRepository(session).count(),
    )


@pytest.mark.integration
def test_the_universe_filter_drops_funds_and_unsupported_venues(session: Session) -> None:
    market_data = MockMarketData(
        [
            CompanyProfile(ticker="GOOD", name="Good Corp", exchange="NASDAQ"),
            CompanyProfile(ticker="BRDX", name="Broad Market Index ETF", exchange="ARCA"),
            CompanyProfile(ticker="OTCX", name="Overcounter Inc", exchange="OTC"),
        ]
    )

    report = update_universe(session, market_data)
    session.flush()

    assert report.processed == 3
    assert report.succeeded == 1
    assert report.skipped == 2
    assert [c.ticker for c in CompanyRepository(session).list_all()] == ["GOOD"]


@pytest.mark.integration
def test_one_failing_ticker_does_not_stop_the_others(session: Session, make: type[Make]) -> None:
    # The behaviour the specification is most explicit about: 81 failures out of
    # 3,842 must still leave 3,761 successes, not zero.
    profiles = [
        CompanyProfile(ticker="AAA", name="Alpha Inc", exchange="NASDAQ"),
        CompanyProfile(ticker="BAD", name="Broken Inc", exchange="NASDAQ"),
        CompanyProfile(ticker="CCC", name="Gamma Inc", exchange="NASDAQ"),
    ]
    bars = {profile.ticker: make.bars(30) for profile in profiles}
    market_data = MockMarketData(profiles, bars, failing_tickers=["BAD"])

    update_universe(session, market_data)
    session.flush()
    report = update_market_data(session, market_data, SETTINGS, today=LATEST_SESSION)
    session.flush()

    assert report.succeeded == 2
    assert report.failed == 1
    assert report.failures == ["BAD"]
    assert PriceHistoryRepository(session).count() == 60


@pytest.mark.integration
def test_a_failing_fundamentals_ticker_is_recorded_and_skipped(
    session: Session, make: type[Make]
) -> None:
    profiles = [
        CompanyProfile(ticker="AAA", name="Alpha Inc", exchange="NASDAQ"),
        CompanyProfile(ticker="BAD", name="Broken Inc", exchange="NASDAQ"),
    ]
    market_data = MockMarketData(profiles)
    fundamentals = MockFundamentals(statements={"AAA": make.quarters()}, failing_tickers=["BAD"])

    update_universe(session, market_data)
    session.flush()
    report = update_fundamentals(session, fundamentals, SETTINGS)

    assert report.succeeded == 1
    assert report.failed == 1
    assert report.failures == ["BAD"]


@pytest.mark.integration
def test_fundamentals_are_skipped_when_the_provider_has_nothing_newer(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
) -> None:
    # This is the incremental guard that stops a nightly job re-downloading four
    # years of unchanged filings for every company.
    market_data, fundamentals = providers
    update_universe(session, market_data)
    session.flush()

    first = update_fundamentals(session, fundamentals, SETTINGS)
    session.flush()
    second = update_fundamentals(session, fundamentals, SETTINGS)

    assert first.succeeded == 1
    assert second.succeeded == 0
    assert second.skipped == 1


@pytest.mark.integration
def test_prices_resume_from_the_last_stored_session(session: Session, make: type[Make]) -> None:
    requested: list[tuple[date, date]] = []

    class RecordingProvider(MockMarketData):
        def get_daily_prices_batch(
            self, tickers: Sequence[str], start: date, end: date
        ) -> dict[str, list[PriceBar]]:
            requested.append((start, end))
            return super().get_daily_prices_batch(tickers, start, end)

    profile = CompanyProfile(ticker="XYZ", name="Example Corp", exchange="NASDAQ")
    provider = RecordingProvider([profile], {"XYZ": make.bars(60)})

    update_universe(session, provider)
    session.flush()
    update_market_data(session, provider, SETTINGS, today=LATEST_SESSION)
    session.flush()
    update_market_data(session, provider, SETTINGS, today=LATEST_SESSION)

    first_start, second_start = requested[0][0], requested[1][0]
    assert first_start == LATEST_SESSION - timedelta(days=SETTINGS.price_history_days)
    # The second pass starts near the last stored bar, not at the full window.
    assert second_start > first_start


@pytest.mark.integration
def test_a_company_with_no_data_is_reported_as_missing_rather_than_failing(
    session: Session,
) -> None:
    profile = CompanyProfile(ticker="DARK", name="Darkwater Inc", exchange="NYSE")

    update_universe(session, MockMarketData([profile]))
    session.flush()
    result = scan_market(session, THRESHOLDS)

    assert result.eligible == ()
    assert ExclusionReason.MISSING_REQUIRED_DATA in result.rows[0].eligibility.reasons


@pytest.mark.integration
def test_the_scan_reports_excluded_companies_with_their_reasons(
    session: Session, make: type[Make]
) -> None:
    cheap = CompanyProfile(
        ticker="PENY", name="Pennywise Inc", exchange="NASDAQ", market_cap=140_000_000.0
    )
    market_data = MockMarketData([cheap], {"PENY": make.bars(30, close=1.55, volume=900_000)})

    update_universe(session, market_data)
    session.flush()
    update_market_data(session, market_data, SETTINGS, today=LATEST_SESSION)
    session.flush()
    result = scan_market(session, THRESHOLDS)

    assert result.processed == 1
    assert result.eligible == ()
    assert result.rows[0].eligibility.reasons == (ExclusionReason.PRICE_BELOW_MINIMUM,)


@pytest.mark.integration
def test_the_scan_can_be_restricted_to_named_tickers(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
) -> None:
    market_data, fundamentals = providers
    _run_pipeline(session, market_data, fundamentals)

    assert scan_market(session, THRESHOLDS, tickers=["NOPE"]).processed == 0
    assert scan_market(session, THRESHOLDS, tickers=["xyz"]).processed == 1


@pytest.mark.integration
def test_thresholds_change_which_companies_pass(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
) -> None:
    market_data, fundamentals = providers
    _run_pipeline(session, market_data, fundamentals)

    strict = EligibilityThresholds(min_market_cap=5_000_000_000.0)

    assert len(scan_market(session, THRESHOLDS).eligible) == 1
    assert scan_market(session, strict).eligible == ()


# -- audit regressions -------------------------------------------------------


@pytest.mark.integration
def test_a_database_error_on_one_listing_does_not_abort_the_universe(
    session: Session,
) -> None:
    # The handler here caught only ProviderError, which a repository never
    # raises — so a single row the database rejected took the whole universe
    # refresh with it, and the operator saw a traceback instead of "1 failed".
    profiles = [
        CompanyProfile(ticker="AAA", name="Alpha Inc", exchange="NASDAQ"),
        CompanyProfile(ticker="BAD", name="Broken Inc", exchange="NASDAQ"),
        CompanyProfile(ticker="CCC", name="Gamma Inc", exchange="NASDAQ"),
    ]

    real_upsert = CompanyRepository.upsert_profile

    def failing_upsert(self: CompanyRepository, profile: CompanyProfile) -> None:
        if profile.ticker == "BAD":
            raise OperationalError("INSERT", {}, Exception("simulated database failure"))
        real_upsert(self, profile)

    CompanyRepository.upsert_profile = failing_upsert  # type: ignore[method-assign]
    try:
        report = update_universe(session, MockMarketData(profiles))
    finally:
        CompanyRepository.upsert_profile = real_upsert  # type: ignore[method-assign]
    session.flush()

    assert report.succeeded == 2
    assert report.failed == 1
    assert report.failures == ["BAD"]
    assert [c.ticker for c in CompanyRepository(session).list_all()] == ["AAA", "CCC"]


@pytest.mark.integration
def test_a_padded_ticker_does_not_create_a_second_company(session: Session) -> None:
    # Normalisation happens in the domain model, so every path into the database
    # — provider, fixture, CLI — lands on the same identity.
    market_data = MockMarketData(
        [
            CompanyProfile(ticker="xyz", name="Example Corp", exchange="NASDAQ"),
            CompanyProfile(ticker=" XYZ ", name="Example Corp", exchange="NASDAQ"),
        ]
    )

    update_universe(session, market_data)
    session.flush()

    assert CompanyRepository(session).count() == 1
    assert CompanyRepository(session).list_all()[0].ticker == "XYZ"


@pytest.mark.integration
def test_a_foreign_currency_reporter_is_excluded_end_to_end(
    session: Session, make: type[Make]
) -> None:
    profile = CompanyProfile(
        ticker="EURO",
        name="Continental Holdings",
        exchange="NYSE",
        market_cap=2_000_000_000.0,
        currency="EUR",
    )
    market_data = MockMarketData([profile], {"EURO": make.bars(30)})

    update_universe(session, market_data)
    session.flush()
    update_market_data(session, market_data, SETTINGS, today=LATEST_SESSION)
    session.flush()
    result = scan_market(session, THRESHOLDS)

    assert result.eligible == ()
    assert ExclusionReason.UNSUPPORTED_CURRENCY in result.rows[0].eligibility.reasons


@pytest.mark.integration
def test_a_limit_bounds_how_many_companies_fundamentals_touches(
    session: Session, make: type[Make]
) -> None:
    # A metered provider charges per company, so a first run has to be bounded.
    profiles = [
        CompanyProfile(ticker=t, name=f"{t} Inc", exchange="NASDAQ")
        for t in ("AAA", "BBB", "CCC", "DDD")
    ]
    statements = {p.ticker: make.quarters() for p in profiles}

    update_universe(session, MockMarketData(profiles))
    session.flush()
    report = update_fundamentals(
        session, MockFundamentals(statements=statements), SETTINGS, limit=2
    )

    assert report.processed == 2


@pytest.mark.integration
def test_the_limit_selects_the_same_companies_every_run(session: Session, make: type[Make]) -> None:
    # Ticker order, not insertion or hash order — a limit that returned a
    # different subset each night would make a partial ingest impossible to
    # reason about, and some companies would never be reached at all.
    profiles = [
        CompanyProfile(ticker=t, name=f"{t} Inc", exchange="NASDAQ")
        for t in ("ZZZ", "AAA", "MMM", "BBB")
    ]
    update_universe(session, MockMarketData(profiles))
    session.flush()

    seen: list[str] = []

    class RecordingFundamentals(MockFundamentals):
        def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
            seen.append(ticker)
            return []

    update_fundamentals(session, RecordingFundamentals(), SETTINGS, limit=2)
    first_pass = list(seen)
    seen.clear()
    update_fundamentals(session, RecordingFundamentals(), SETTINGS, limit=2)

    assert first_pass == ["AAA", "BBB"]
    assert seen == first_pass


@pytest.mark.integration
def test_a_few_uncovered_symbols_do_not_stop_the_pass(session: Session, make: type[Make]) -> None:
    # A metered plan may cover large caps and not small ones. Those are coverage
    # gaps, counted apart from failures, and the pass must continue through them.
    profiles = [
        CompanyProfile(ticker=t, name=f"{t} Inc", exchange="NASDAQ") for t in ("AAA", "BBB", "CCC")
    ]

    class PartiallyCoveredPlan(MockFundamentals):
        def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
            if ticker == "BBB":
                raise ProviderPlanError("symbol not available on this subscription")
            return make.quarters()

    update_universe(session, MockMarketData(profiles))
    session.flush()
    report = update_fundamentals(session, PartiallyCoveredPlan(), SETTINGS)

    assert report.succeeded == 2
    assert report.blocked == 1
    assert report.blocked_tickers == ["BBB"]
    assert report.failed == 0


@pytest.mark.integration
def test_a_wholesale_plan_rejection_stops_the_pass(session: Session) -> None:
    # The other reading of the same status: the request shape itself is
    # forbidden, so every company will fail. Stopping preserves the daily quota
    # instead of spending it collecting thousands of identical errors.
    profiles = [
        CompanyProfile(ticker=f"T{i:03d}", name=f"T{i} Inc", exchange="NASDAQ") for i in range(60)
    ]

    class ForbiddenRequestShape(MockFundamentals):
        def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
            raise ProviderPlanError("the values for 'limit' must be between 0 and 5")

    update_universe(session, MockMarketData(profiles))
    session.flush()

    with pytest.raises(ProviderPlanError, match="accepted none"):
        update_fundamentals(session, ForbiddenRequestShape(), SETTINGS)


@pytest.mark.integration
def test_rejected_credentials_stop_the_pass_immediately(session: Session, make: type[Make]) -> None:
    # A rejected key fails for every symbol. Surfacing it once beats burying the
    # cause under one traceback per company.
    profiles = [
        CompanyProfile(ticker=t, name=f"{t} Inc", exchange="NASDAQ") for t in ("AAA", "BBB")
    ]

    class RejectedKey(MockMarketData):
        def get_daily_prices_batch(
            self, tickers: Sequence[str], start: date, end: date
        ) -> dict[str, list[PriceBar]]:
            raise ProviderAuthError("provider rejected the credentials (HTTP 401)")

        def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
            raise ProviderAuthError("provider rejected the credentials (HTTP 401)")

    update_universe(session, MockMarketData(profiles))
    session.flush()

    with pytest.raises(ProviderAuthError, match="rejected the credentials"):
        update_market_data(session, RejectedKey(profiles), SETTINGS, today=LATEST_SESSION)


@pytest.mark.integration
def test_heavy_but_partial_blocking_does_not_abort_the_pass(
    session: Session, make: type[Make]
) -> None:
    # A plan covering a tenth of the market produces long runs of rejections
    # legitimately. Aborting on those would make the tool useless on exactly the
    # plans that most need bounded runs — so the breaker also requires that
    # nothing has succeeded.
    profiles = [
        CompanyProfile(ticker=f"T{i:03d}", name=f"T{i} Inc", exchange="NASDAQ") for i in range(80)
    ]

    class MostlyBlockedPlan(MockFundamentals):
        def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
            if ticker == "T000":  # one early success, then a long blocked run
                return make.quarters()
            raise ProviderPlanError("symbol not available on this subscription")

    update_universe(session, MockMarketData(profiles))
    session.flush()
    report = update_fundamentals(session, MostlyBlockedPlan(), SETTINGS)

    assert report.succeeded == 1
    assert report.blocked == 79
    assert report.processed == 80


@pytest.mark.integration
def test_force_restores_periods_the_incremental_skip_would_leave_stale(
    session: Session,
    providers: tuple[MockMarketData, MockFundamentals],
    make: type[Make],
) -> None:
    # The skip compares reporting dates, so it cannot see that the adapter
    # changed. After fixing a normalisation bug the stored values are wrong and
    # only a forced pass replaces them.
    market_data, fundamentals = providers
    update_universe(session, market_data)
    session.flush()
    update_fundamentals(session, fundamentals, SETTINGS)
    session.flush()

    assert update_fundamentals(session, fundamentals, SETTINGS).skipped == 1

    forced = update_fundamentals(session, fundamentals, SETTINGS, force=True)

    assert forced.succeeded == 1
    assert forced.skipped == 0


@pytest.mark.integration
def test_the_benchmark_series_is_stored_under_its_symbol(
    session: Session,
    make: type[Make],
) -> None:
    provider = MockMarketData([], {"SPY": make.bars(sessions=30)})

    report = update_benchmark(session, provider, SETTINGS, today=LATEST_SESSION)
    session.flush()

    assert report.succeeded == 1
    assert BenchmarkPriceRepository(session).latest_date("SPY") == LATEST_SESSION
    # The benchmark is not a company, so nothing entered the scannable universe.
    assert CompanyRepository(session).count() == 0


@pytest.mark.integration
def test_a_benchmark_the_provider_does_not_cover_is_skipped_not_failed(
    session: Session,
) -> None:
    report = update_benchmark(session, MockMarketData([], {}), SETTINGS, today=LATEST_SESSION)

    assert report.skipped == 1
    assert report.failed == 0


@pytest.mark.integration
def test_a_failing_benchmark_fetch_is_counted_rather_than_raised(session: Session) -> None:
    provider = MockMarketData([], {}, failing_tickers=["SPY"])

    report = update_benchmark(session, provider, SETTINGS, today=LATEST_SESSION)

    assert report.failed == 1
    assert report.failures == ["SPY"]


@pytest.mark.integration
def test_re_running_the_benchmark_update_does_not_duplicate_sessions(
    session: Session,
    make: type[Make],
) -> None:
    provider = MockMarketData([], {"SPY": make.bars(sessions=30)})

    update_benchmark(session, provider, SETTINGS, today=LATEST_SESSION)
    session.flush()
    update_benchmark(session, provider, SETTINGS, today=LATEST_SESSION)
    session.flush()

    assert BenchmarkPriceRepository(session).count() == 30
