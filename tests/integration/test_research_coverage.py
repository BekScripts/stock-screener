"""The coverage audit: does the funnel say where the record actually runs out?

The audit exists to stop one specific dishonesty — reading a date that holds
nothing as a date on which nothing happened. So the tests here are mostly about
absence: that a universe the record cannot reconstruct counts as zero members
and not as the current roster, that a proxy for membership is never quietly
promoted into membership, and that a security lost to missing filings is
attributed to that stage rather than to the one after it.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import (
    EVENT_ACTIVE,
    OBSERVATION_BASELINE,
    BenchmarkPriceRepository,
    CompanyRepository,
    DelistingRepository,
    FundamentalObservationRepository,
    PriceHistoryRepository,
    UniverseEventRepository,
)
from domain import CompanyProfile, CorporateActionKind, PriceBar, ScoringStatus
from stock_screener.backtest import date_coverage, research_window
from stock_screener.backtest.coverage import (
    SURVIVORSHIP_BASELINE_ONLY,
    SURVIVORSHIP_OBSERVED,
    SURVIVORSHIP_UNRECONSTRUCTED,
    DateCoverage,
)
from stock_screener.config import Settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

BENCHMARK = "SPY"


def _weekdays(count: int, start: date = date(2024, 1, 2)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


CALENDAR = _weekdays(60)
AS_OF = CALENDAR[40]


def _settings() -> Settings:
    return Settings(environment="test", benchmark_symbol=BENCHMARK)


def _company(session: Session, ticker: str) -> int:
    CompanyRepository(session).upsert_profile(
        CompanyProfile(
            ticker=ticker,
            name=f"{ticker} Inc",
            exchange="NASDAQ",
            sector="Technology",
            industry="Software",
            market_cap=None,
        )
    )
    stored = CompanyRepository(session).get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _prices(session: Session, company_id: int, days: Sequence[date]) -> None:
    PriceHistoryRepository(session).upsert_bars(
        company_id,
        [
            PriceBar(date=day, open=25.0, high=26.0, low=24.0, close=25.0, volume=1_000_000)
            for day in days
        ],
    )


def _benchmark(session: Session, days: Sequence[date]) -> None:
    BenchmarkPriceRepository(session).upsert_bars(
        BENCHMARK,
        [
            PriceBar(date=day, open=500.0, high=501.0, low=499.0, close=500.0, volume=1_000_000)
            for day in days
        ],
    )


def _baseline_event(session: Session, company_id: int, ticker: str, when: date) -> None:
    """Record the company as first seen active on a date."""
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


def _coverage(as_of: date, **overrides: object) -> DateCoverage:
    """Build a coverage row for the pure window reduction."""
    base: dict[str, object] = {
        "as_of": as_of,
        "in_record": 10,
        "universe_members": 0,
        "universe_observed": 0,
        "universe_baseline": 0,
        "universe_undetermined": 10,
        "listed_on_date": 10,
        "sufficient_price_history": 10,
        "fundamentals_available": 10,
        "market_cap_available": 10,
        "score_reconstructable": 10,
        "signals_available": 10,
        "forward_returns_available": 10,
        "recorded_exits": 0,
        "terminal_outcomes_available": 0,
        "interrupted": 0,
        "fully_usable": 10,
    }
    base.update(overrides)
    return DateCoverage(**base)  # type: ignore[arg-type]


@pytest.mark.integration
def test_membership_recorded_only_after_the_date_leaves_the_universe_empty(
    session: Session,
) -> None:
    company_id = _company(session, "AAA")
    _benchmark(session, CALENDAR)
    _prices(session, company_id, CALENDAR)
    _baseline_event(session, company_id, "AAA", CALENDAR[50])

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.universe_members == 0
    assert coverage.universe_undetermined == 1


@pytest.mark.integration
def test_a_security_that_traded_is_listed_but_not_thereby_a_universe_member(
    session: Session,
) -> None:
    company_id = _company(session, "AAA")
    _benchmark(session, CALENDAR)
    _prices(session, company_id, CALENDAR)
    _baseline_event(session, company_id, "AAA", CALENDAR[50])

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.listed_on_date == 1
    assert coverage.universe_members == 0


@pytest.mark.integration
def test_a_security_that_stopped_trading_before_the_date_is_not_listed_on_it(
    session: Session,
) -> None:
    company_id = _company(session, "AAA")
    _benchmark(session, CALENDAR)
    _prices(session, company_id, CALENDAR[:20])

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.listed_on_date == 0


@pytest.mark.integration
def test_a_security_with_nothing_filed_by_the_date_is_insufficient_data(
    session: Session,
) -> None:
    company_id = _company(session, "AAA")
    _benchmark(session, CALENDAR)
    _prices(session, company_id, CALENDAR)

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.by_status[ScoringStatus.INSUFFICIENT_DATA.value] == 1
    assert coverage.fundamentals_available == 0
    assert coverage.score_reconstructable == 0
    assert coverage.fully_usable == 0


@pytest.mark.integration
def test_a_filing_made_after_the_date_does_not_count_towards_it(
    session: Session,
) -> None:
    company_id = _company(session, "AAA")
    _benchmark(session, CALENDAR)
    _prices(session, company_id, CALENDAR)
    FundamentalObservationRepository(session).add_all(
        company_id,
        [
            {
                "observation_fingerprint": "later",
                "taxonomy": "us-gaap",
                "concept": "Revenues",
                "value": 1_000.0,
                "unit": "USD",
                "period_start": CALENDAR[0],
                "period_end": CALENDAR[45],
                "filed_date": CALENDAR[50],
                "accession": "0000000000-24-000001",
                "form": "10-Q",
                "source": "test",
            }
        ],
    )

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.fundamentals_available == 0
    assert coverage.by_status[ScoringStatus.INSUFFICIENT_DATA.value] == 1


@pytest.mark.integration
def test_the_attrition_report_starts_at_the_whole_roster(session: Session) -> None:
    _benchmark(session, CALENDAR)
    for ticker in ("AAA", "BBB", "CCC"):
        _prices(session, _company(session, ticker), CALENDAR)

    stages = date_coverage(session, _settings(), AS_OF).attrition()

    assert stages[0] == ("in_record", 3, 0.0)
    assert [name for name, _, _ in stages][-1] == "fully_usable"


@pytest.mark.integration
def test_an_exit_outside_todays_roster_is_still_a_recorded_exit(session: Session) -> None:
    """The survivorship number must not be measured against today's survivors.
    A delisted ticker with no company row is exactly the case a roster-joined
    count would drop, and exactly the case a study needs counted."""
    _benchmark(session, CALENDAR)
    DelistingRepository(session).add_all(
        [
            {
                "ticker": "GONE",
                "kind": CorporateActionKind.CASH_MERGER.value,
                "effective_date": CALENDAR[10],
                "cash_per_share": 42.0,
                "acquirer_ticker": None,
                "acquirer_rate": None,
                "acquiree_rate": None,
                "source": "test",
                "source_id": "d-gone",
            }
        ]
    )

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.recorded_exits == 1
    assert coverage.terminal_outcomes_available == 1


@pytest.mark.integration
def test_an_exit_with_no_stated_terminal_value_is_recorded_but_not_usable(
    session: Session,
) -> None:
    _benchmark(session, CALENDAR)
    DelistingRepository(session).add_all(
        [
            {
                "ticker": "GONE",
                "kind": CorporateActionKind.STOCK_MERGER.value,
                "effective_date": CALENDAR[10],
                "cash_per_share": None,
                "acquirer_ticker": None,
                "acquirer_rate": None,
                "acquiree_rate": None,
                "source": "test",
                "source_id": "d-gone",
            }
        ]
    )

    coverage = date_coverage(session, _settings(), AS_OF)

    assert coverage.recorded_exits == 1
    assert coverage.terminal_outcomes_available == 0


@pytest.mark.unit
def test_the_research_window_is_the_longest_unbroken_run_of_usable_dates() -> None:
    coverages = [
        _coverage(date(2024, 1, 31), fully_usable=5),
        _coverage(date(2024, 2, 29), fully_usable=0),
        _coverage(date(2024, 3, 28), fully_usable=7),
        _coverage(date(2024, 4, 30), fully_usable=7),
        _coverage(date(2024, 5, 31), fully_usable=7),
    ]

    window = research_window(coverages)

    assert window.longest_run == (date(2024, 3, 28), date(2024, 5, 31))
    assert window.first_usable == date(2024, 1, 31)
    assert window.last_usable == date(2024, 5, 31)
    assert window.minimum_usable == 0


@pytest.mark.unit
def test_a_universe_the_record_cannot_reach_is_labelled_unreconstructed() -> None:
    coverages = [_coverage(date(2024, 1, 31), universe_members=0, universe_observed=0)]

    assert research_window(coverages).survivorship == SURVIVORSHIP_UNRECONSTRUCTED


@pytest.mark.unit
def test_a_universe_of_baselines_alone_is_not_called_observed() -> None:
    coverages = [
        _coverage(date(2024, 1, 31), universe_members=9, universe_baseline=9, universe_observed=0)
    ]

    assert research_window(coverages).survivorship == SURVIVORSHIP_BASELINE_ONLY


@pytest.mark.unit
def test_one_witnessed_transition_makes_the_universe_observed() -> None:
    coverages = [
        _coverage(date(2024, 1, 31), universe_members=9, universe_baseline=8, universe_observed=1)
    ]

    assert research_window(coverages).survivorship == SURVIVORSHIP_OBSERVED
