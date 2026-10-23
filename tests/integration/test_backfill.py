"""The backfill runner: does running the past look like running the day?

A backfill is only worth having if a session it describes is indistinguishable
from the one a live run would have written. These tests assert that first — the
cached price source and the repository must agree bar for bar, and neither may
ever hand back a bar from after the date — and then assert the four operational
properties the runner promises: idempotent, resumable, observable, safe to
rerun.

They run against a real file-backed database because the runner opens a session
per date, and a per-connection in-memory database would give each of those
sessions a different empty world.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from data_access import (
    EVENT_ACTIVE,
    OBSERVATION_BASELINE,
    BenchmarkPriceRepository,
    CompanyRepository,
    ForwardReturnRecord,
    PitCaptureRunRepository,
    PitFeatureSnapshotRepository,
    PitSignalSnapshotRepository,
    PriceHistoryRepository,
    UniverseEventRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CURRENT_SIGNAL_VERSION,
    CompanyProfile,
    OutcomeStatus,
    PriceBar,
)
from stock_screener.backtest import Cadence, backfill_returns, backfill_signals, market_sessions
from stock_screener.backtest.backfill import DateStatus, sample
from stock_screener.config import Settings
from stock_screener.pit import FEATURE_VERSION
from stock_screener.pit.prices import CachedPriceSource, RepositoryPriceSource

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

BENCHMARK = "SPY"


def _weekdays(count: int, start: date = date(2024, 1, 2)) -> list[date]:
    """Return `count` consecutive weekdays, standing in for a calendar."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


CALENDAR = _weekdays(80)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'backfill.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def _settings() -> Settings:
    return Settings(environment="test", benchmark_symbol=BENCHMARK)


def _seed(session: Session, tickers: Sequence[str], days: Sequence[date]) -> dict[str, int]:
    """Give each ticker a rising price series over these sessions."""
    BenchmarkPriceRepository(session).upsert_bars(
        BENCHMARK,
        [
            PriceBar(date=day, open=500.0, high=501.0, low=499.0, close=500.0, volume=1_000_000)
            for day in days
        ],
    )
    companies = CompanyRepository(session)
    ids: dict[str, int] = {}
    for offset, ticker in enumerate(tickers):
        companies.upsert_profile(
            CompanyProfile(
                ticker=ticker,
                name=f"{ticker} Inc",
                exchange="NASDAQ",
                sector="Technology",
                industry="Software",
                market_cap=None,
            )
        )
        stored = companies.get_by_ticker(ticker)
        assert stored is not None
        ids[ticker] = stored.id
        PriceHistoryRepository(session).upsert_bars(
            stored.id,
            [
                PriceBar(
                    date=day,
                    open=20.0 + offset + index * 0.1,
                    high=21.0 + offset + index * 0.1,
                    low=19.0 + offset + index * 0.1,
                    close=20.5 + offset + index * 0.1,
                    volume=1_000_000,
                )
                for index, day in enumerate(days)
            ],
        )
    return ids


def _baseline_event(session: Session, company_id: int, ticker: str, when: date) -> None:
    """Record the company as first seen active on a date, as capture would."""
    UniverseEventRepository(session).add_all(
        [
            {
                "company_id": company_id,
                "event_fingerprint": f"{ticker}-{when}",
                "ticker": ticker,
                "event_type": EVENT_ACTIVE,
                "observation_kind": OBSERVATION_BASELINE,
                "effective_date": when,
                "is_active": True,
                "exchange": "NASDAQ",
                "source": "test",
            }
        ]
    )


@pytest.mark.unit
def test_monthly_sampling_takes_the_last_session_of_each_month() -> None:
    sessions = [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 29)]

    assert sample(sessions, Cadence.MONTHLY) == (date(2024, 1, 31), date(2024, 2, 29))


@pytest.mark.unit
def test_weekly_sampling_takes_the_last_session_of_each_week() -> None:
    # Monday 2024-01-01 through Tuesday 2024-01-09, with the weekend absent.
    sessions = [date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9)]

    assert sample(sessions, Cadence.WEEKLY) == (date(2024, 1, 5), date(2024, 1, 9))


@pytest.mark.unit
def test_daily_sampling_keeps_every_session() -> None:
    sessions = CALENDAR[:10]

    assert sample(sessions, Cadence.DAILY) == tuple(sessions)


@pytest.mark.integration
def test_the_cached_source_never_returns_a_bar_after_the_date(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        ids = _seed(session, ["AAA"], CALENDAR)

    with session_scope(factory) as session:
        cache = CachedPriceSource.load(session)

        bars = cache.bars_until(ids["AAA"], CALENDAR[20])

        assert bars[-1].date == CALENDAR[20]
        assert all(bar.date <= CALENDAR[20] for bar in bars)


@pytest.mark.integration
def test_the_cached_source_and_the_repository_agree_bar_for_bar(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        ids = _seed(session, ["AAA", "BBB"], CALENDAR)

    with session_scope(factory) as session:
        cache = CachedPriceSource.load(session)
        repository = RepositoryPriceSource(session)

        for company_id in ids.values():
            for cut in (CALENDAR[0], CALENDAR[37], CALENDAR[-1]):
                assert list(cache.bars_until(company_id, cut)) == list(
                    repository.bars_until(company_id, cut)
                )


@pytest.mark.integration
def test_a_company_with_no_bars_gets_an_empty_series_from_the_cache(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA"], CALENDAR)

    with session_scope(factory) as session:
        cache = CachedPriceSource.load(session)

        assert cache.bars_until(9_999, CALENDAR[10]) == ()


@pytest.mark.integration
def test_backfilling_signals_writes_one_row_per_security_per_session(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA", "BBB"], CALENDAR)
    wanted = CALENDAR[60:63]

    report = backfill_signals(factory, _settings(), wanted)

    assert [r.status for r in report.results] == [DateStatus.WRITTEN] * 3
    assert report.covered == tuple(wanted)
    assert report.missing == ()
    with session_scope(factory) as session:
        stored = PitSignalSnapshotRepository(session).for_date(
            wanted[0], signal_version=CURRENT_SIGNAL_VERSION
        )
    assert {row.ticker for row in stored} == {"AAA", "BBB"}


@pytest.mark.integration
def test_a_backfilled_capture_invents_no_membership_history(
    factory: sessionmaker[Session],
) -> None:
    """The one thing a backfill must never do.

    `companies` holds today's roster, so writing a membership event for a past
    date would assert that today's survivors were present then — which is the
    survivorship bias the whole point-in-time record exists to remove. A capture
    for a past date writes features and nothing else, because the state it would
    record was already first seen on a later day.
    """
    day = CALENDAR[60]
    with session_scope(factory) as session:
        ids = _seed(session, ["AAA"], CALENDAR)
        _baseline_event(session, ids["AAA"], "AAA", CALENDAR[70])
    with session_scope(factory) as session:
        before = len(UniverseEventRepository(session).all_events())

    backfill_signals(factory, _settings(), [day])

    with session_scope(factory) as session:
        events = UniverseEventRepository(session).all_events()
    assert len(events) == before
    assert all(event.effective_date == CALENDAR[70] for event in events)


@pytest.mark.integration
def test_backfilling_signals_twice_writes_nothing_the_second_time(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA"], CALENDAR)
    wanted = CALENDAR[60:63]
    backfill_signals(factory, _settings(), wanted)

    again = backfill_signals(factory, _settings(), wanted)

    assert again.rows == 0
    assert [r.status for r in again.results] == [DateStatus.SKIPPED] * 3
    assert again.covered == tuple(wanted)


def _half_written_day(session: Session, company_id: int, ticker: str, day: date) -> None:
    """Leave a feature row for a day whose capture run never completed.

    The state a crash mid-capture leaves behind, and the one `capture_day`
    refuses to top up: a point-in-time row is never rewritten, so the next
    attempt on that day raises rather than producing a day that looks whole.
    """
    run = PitCaptureRunRepository(session).start(
        day, score_version=CURRENT_SCORE_VERSION, feature_version=FEATURE_VERSION
    )
    PitFeatureSnapshotRepository(session).add_all(
        [
            {
                "run_id": run.id,
                "company_id": company_id,
                "ticker": ticker,
                "snapshot_date": day,
                "captured_at": datetime(2024, 1, 1, tzinfo=UTC),
                "feature_version": FEATURE_VERSION,
                "feature_as_of": day,
                "bars_available": 1,
                "has_extreme_move": False,
            }
        ]
    )


@pytest.mark.integration
def test_a_session_that_fails_does_not_end_the_run(factory: sessionmaker[Session]) -> None:
    wanted = CALENDAR[60:63]
    doomed = wanted[1]
    with session_scope(factory) as session:
        ids = _seed(session, ["AAA"], CALENDAR)
        _half_written_day(session, ids["AAA"], "AAA", doomed)

    report = backfill_signals(factory, _settings(), wanted)

    assert [r.session_date for r in report.failed] == [doomed]
    assert {r.session_date for r in report.written} == {wanted[0], wanted[2]}


@pytest.mark.integration
def test_a_failed_session_is_reported_missing_rather_than_covered(
    factory: sessionmaker[Session],
) -> None:
    wanted = CALENDAR[60:63]
    doomed = wanted[1]
    with session_scope(factory) as session:
        ids = _seed(session, ["AAA"], CALENDAR)
        _half_written_day(session, ids["AAA"], "AAA", doomed)

    report = backfill_signals(factory, _settings(), wanted)

    assert report.missing == (doomed,)
    assert report.covered == (wanted[0], wanted[2])


@pytest.mark.integration
def test_a_failed_session_leaves_no_signals_behind(factory: sessionmaker[Session]) -> None:
    doomed = CALENDAR[61]
    with session_scope(factory) as session:
        ids = _seed(session, ["AAA"], CALENDAR)
        _half_written_day(session, ids["AAA"], "AAA", doomed)

    backfill_signals(factory, _settings(), [doomed])

    with session_scope(factory) as session:
        stored = PitSignalSnapshotRepository(session).for_date(
            doomed, signal_version=CURRENT_SIGNAL_VERSION
        )
    assert stored == []


def _measurements(session: Session, decision: date) -> list[ForwardReturnRecord]:
    """Return every stored measurement from one decision date."""
    return list(
        session.scalars(
            select(ForwardReturnRecord).where(ForwardReturnRecord.observation_date == decision)
        )
    )


@pytest.mark.integration
def test_backfilled_returns_enter_on_the_session_after_the_decision_date(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA"], CALENDAR)
    decision = CALENDAR[40]

    backfill_returns(factory, _settings(), [decision])

    with session_scope(factory) as session:
        rows = _measurements(session, decision)
    assert rows
    assert {row.entry_date for row in rows} == {CALENDAR[41]}


@pytest.mark.integration
def test_a_horizon_past_the_record_is_truncated_rather_than_zero(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA"], CALENDAR)
    decision = CALENDAR[-2]

    backfill_returns(factory, _settings(), [decision])

    with session_scope(factory) as session:
        rows = _measurements(session, decision)
    long_horizons = [row for row in rows if row.horizon_sessions >= 5]
    assert long_horizons
    assert all(row.status == OutcomeStatus.TRUNCATED.value for row in long_horizons)
    assert all(row.total_return is None for row in long_horizons)


@pytest.mark.integration
def test_measuring_the_same_decision_date_twice_stores_nothing_new(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA"], CALENDAR)
    decision = CALENDAR[40]
    first = backfill_returns(factory, _settings(), [decision])

    again = backfill_returns(factory, _settings(), [decision])

    assert first.rows > 0
    assert again.rows == 0
    assert again.covered == (decision,)


@pytest.mark.integration
def test_market_sessions_come_from_the_benchmark_within_the_range(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        _seed(session, ["AAA"], CALENDAR)

    with session_scope(factory) as session:
        found = market_sessions(session, BENCHMARK, start=CALENDAR[10], end=CALENDAR[14])

    assert found == tuple(CALENDAR[10:15])
