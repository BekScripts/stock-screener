"""Repository tests, run against a real SQLite database.

These are `integration`, not `unit`: they execute actual SQL, and that is the
point. The upsert behaviour they verify lives in the database's `ON CONFLICT`
handling, so a test with a mocked session would prove nothing about whether a
second ingest duplicates a quarter.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import (
    Company,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
    to_company_profile,
    to_financial_period,
    to_price_bar,
)
from domain import CompanyProfile, FinancialPeriod, PriceBar

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as open_session:
        yield open_session
    engine.dispose()


def _company(session: Session, ticker: str = "XYZ") -> int:
    """Insert a company and return its primary key."""
    repo = CompanyRepository(session)
    repo.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc", exchange="NASDAQ"))
    session.flush()
    stored = repo.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _period(day: date, revenue: float) -> FinancialPeriod:
    """Build a period with a revenue figure."""
    return FinancialPeriod(period_end=day, revenue=revenue, source="test")


def _bar(day: date, close: float) -> PriceBar:
    """Build a daily bar."""
    return PriceBar(date=day, open=close, high=close, low=close, close=close, volume=1_000.0)


@pytest.mark.integration
def test_a_company_is_inserted_once_and_updated_thereafter(session: Session) -> None:
    repo = CompanyRepository(session)

    repo.upsert_profile(CompanyProfile(ticker="XYZ", name="Old Name", exchange="NASDAQ"))
    repo.upsert_profile(CompanyProfile(ticker="XYZ", name="New Name", exchange="NASDAQ"))
    session.flush()

    stored = repo.get_by_ticker("XYZ")
    assert repo.count() == 1
    assert stored is not None
    assert stored.name == "New Name"


@pytest.mark.integration
def test_an_upsert_does_not_erase_fields_the_provider_left_unset(session: Session) -> None:
    # Alpaca knows the exchange, FMP knows the sector. Whichever runs second
    # must not blank out what the other contributed.
    repo = CompanyRepository(session)
    repo.upsert_profile(
        CompanyProfile(ticker="XYZ", name="Example", exchange="NASDAQ", sector="Technology")
    )
    repo.upsert_profile(CompanyProfile(ticker="XYZ", name="Example", exchange="NASDAQ"))
    session.flush()

    stored = repo.get_by_ticker("XYZ")
    assert stored is not None
    assert stored.sector == "Technology"


@pytest.mark.integration
def test_a_company_can_still_be_marked_inactive(session: Session) -> None:
    # False is a real observation, unlike None, so it must overwrite.
    repo = CompanyRepository(session)
    repo.upsert_profile(CompanyProfile(ticker="XYZ", name="Example", is_active=True))
    repo.upsert_profile(CompanyProfile(ticker="XYZ", name="Example", is_active=False))
    session.flush()

    stored = repo.get_by_ticker("XYZ")
    assert stored is not None
    assert stored.is_active is False


@pytest.mark.integration
def test_tickers_are_stored_upper_cased(session: Session) -> None:
    repo = CompanyRepository(session)
    repo.upsert_profile(CompanyProfile(ticker="xyz", name="Example"))
    session.flush()

    assert repo.get_by_ticker("XYZ") is not None


@pytest.mark.integration
def test_ingesting_the_same_quarter_twice_leaves_one_row(session: Session) -> None:
    company_id = _company(session)
    repo = FinancialSnapshotRepository(session)
    periods = [_period(date(2026, 3, 31), 100.0), _period(date(2026, 6, 30), 110.0)]

    repo.upsert_periods(company_id, periods)
    repo.upsert_periods(company_id, periods)
    session.flush()

    assert repo.count() == 2


@pytest.mark.integration
def test_a_restated_quarter_replaces_the_original_figure(session: Session) -> None:
    company_id = _company(session)
    repo = FinancialSnapshotRepository(session)

    repo.upsert_periods(company_id, [_period(date(2026, 6, 30), 100.0)])
    repo.upsert_periods(company_id, [_period(date(2026, 6, 30), 125.0)])
    session.flush()

    stored = repo.list_for_company(company_id)
    assert len(stored) == 1
    assert stored[0].revenue == pytest.approx(125.0)


@pytest.mark.integration
def test_the_latest_period_end_drives_the_incremental_skip(session: Session) -> None:
    company_id = _company(session)
    repo = FinancialSnapshotRepository(session)

    assert repo.latest_period_end(company_id) is None

    repo.upsert_periods(
        company_id, [_period(date(2026, 3, 31), 1.0), _period(date(2026, 6, 30), 2.0)]
    )
    session.flush()

    assert repo.latest_period_end(company_id) == date(2026, 6, 30)


@pytest.mark.integration
def test_a_period_with_no_revenue_stores_null_not_zero(session: Session) -> None:
    company_id = _company(session)
    repo = FinancialSnapshotRepository(session)

    repo.upsert_periods(company_id, [FinancialPeriod(period_end=date(2026, 6, 30))])
    session.flush()

    stored = repo.list_for_company(company_id)[0]
    assert stored.revenue is None
    assert to_financial_period(stored).revenue is None


@pytest.mark.integration
def test_ingesting_the_same_bars_twice_leaves_one_row_per_session(session: Session) -> None:
    company_id = _company(session)
    repo = PriceHistoryRepository(session)
    bars = [_bar(date(2026, 6, 29), 10.0), _bar(date(2026, 6, 30), 11.0)]

    repo.upsert_bars(company_id, bars)
    repo.upsert_bars(company_id, bars)
    session.flush()

    assert repo.count() == 2


@pytest.mark.integration
def test_a_corrected_bar_replaces_the_original(session: Session) -> None:
    company_id = _company(session)
    repo = PriceHistoryRepository(session)

    repo.upsert_bars(company_id, [_bar(date(2026, 6, 30), 10.0)])
    repo.upsert_bars(company_id, [_bar(date(2026, 6, 30), 10.42)])
    session.flush()

    stored = repo.list_for_company(company_id)
    assert len(stored) == 1
    assert stored[0].close == pytest.approx(10.42)


@pytest.mark.integration
def test_duplicate_dates_within_one_call_are_collapsed(session: Session) -> None:
    # Two overlapping pages of a paginated response. PostgreSQL rejects an
    # ON CONFLICT statement that touches the same row twice, so this has to be
    # de-duplicated before the write rather than left to the database.
    company_id = _company(session)
    repo = PriceHistoryRepository(session)

    written = repo.upsert_bars(
        company_id, [_bar(date(2026, 6, 30), 10.0), _bar(date(2026, 6, 30), 11.0)]
    )
    session.flush()

    assert written == 1
    assert repo.count() == 1


@pytest.mark.integration
def test_two_companies_can_share_a_session_date(session: Session) -> None:
    first = _company(session, "AAA")
    second = _company(session, "BBB")
    repo = PriceHistoryRepository(session)

    repo.upsert_bars(first, [_bar(date(2026, 6, 30), 10.0)])
    repo.upsert_bars(second, [_bar(date(2026, 6, 30), 20.0)])
    session.flush()

    assert repo.count() == 2


@pytest.mark.integration
def test_the_latest_stored_date_drives_the_incremental_price_fetch(session: Session) -> None:
    company_id = _company(session)
    repo = PriceHistoryRepository(session)

    assert repo.latest_date(company_id) is None

    repo.upsert_bars(company_id, [_bar(date(2026, 6, 29), 1.0), _bar(date(2026, 6, 30), 2.0)])
    session.flush()

    assert repo.latest_date(company_id) == date(2026, 6, 30)


@pytest.mark.integration
def test_writing_nothing_writes_nothing(session: Session) -> None:
    company_id = _company(session)

    assert PriceHistoryRepository(session).upsert_bars(company_id, []) == 0
    assert FinancialSnapshotRepository(session).upsert_periods(company_id, []) == 0


@pytest.mark.integration
def test_stored_rows_convert_back_into_domain_models(session: Session) -> None:
    company_id = _company(session)
    PriceHistoryRepository(session).upsert_bars(company_id, [_bar(date(2026, 6, 30), 10.5)])
    session.flush()

    company = session.get(Company, company_id)
    assert company is not None

    profile = to_company_profile(company)
    stored_bar = to_price_bar(PriceHistoryRepository(session).list_for_company(company_id)[0])

    assert profile.ticker == "XYZ"
    assert profile.exchange == "NASDAQ"
    assert stored_bar.close == pytest.approx(10.5)


@pytest.mark.integration
def test_deleting_a_company_cascades_to_its_children(session: Session) -> None:
    # Foreign keys are off by default in SQLite; the engine enables them, and
    # this is what proves it stayed enabled.
    company_id = _company(session)
    PriceHistoryRepository(session).upsert_bars(company_id, [_bar(date(2026, 6, 30), 1.0)])
    session.flush()

    company = session.get(Company, company_id)
    assert company is not None
    session.delete(company)
    session.flush()

    assert PriceHistoryRepository(session).count() == 0
