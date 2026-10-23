"""Fundamental metrics as they stood on a past date.

These are the tests that make look-ahead hard to reintroduce. Each one states a
temporal fact — what was knowable on a date — and asserts the engine agrees.
The worked example from the Phase 8D brief is
`test_a_restatement_is_invisible_until_it_is_filed`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from data_access import (
    CompanyRepository,
    FilingAcceptanceRepository,
    FinancialSnapshotRepository,
    FundamentalObservationRepository,
)
from domain import CompanyProfile, FinancialPeriod, PeriodCadence
from stock_screener.pit import build_metrics_as_of, periods_as_of

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make

Q4 = date(2025, 12, 31)
Q4_START = date(2025, 10, 1)
FIRST_FILING = date(2026, 2, 15)
RESTATEMENT = date(2026, 4, 30)
ACCESSION = "0000000000-26-000001"


def _company(session: Session, ticker: str = "ACME") -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _accept(session: Session, accession: str, accepted_at: datetime) -> None:
    """Store when the SEC accepted one filing."""
    FilingAcceptanceRepository(session).add_all(
        [
            {
                "accession": accession,
                "accepted_at": accepted_at,
                "filed_date": accepted_at.date(),
                "form": "10-K",
                "source": "sec-edgar",
            }
        ]
    )
    session.flush()


def _observe(
    session: Session,
    company_id: int,
    concept: str,
    value: float,
    filed: date | None,
    *,
    period_end: date = Q4,
    period_start: date | None = Q4_START,
    unit: str = "USD",
    accession: str = ACCESSION,
) -> None:
    """Append one filing observation, as the ingestion pass would."""
    fingerprint = hashlib.sha256(
        f"{concept}|{value}|{filed}|{period_end}|{unit}|{accession}".encode()
    ).hexdigest()
    FundamentalObservationRepository(session).add_all(
        company_id,
        [
            {
                "observation_fingerprint": fingerprint,
                "taxonomy": "us-gaap",
                "concept": concept,
                "value": value,
                "unit": unit,
                "period_start": period_start,
                "period_end": period_end,
                "filed_date": filed,
                "accession": accession,
                "form": "10-K",
                "source": "sec-edgar",
            }
        ],
    )
    session.flush()


def _revenue_as_of(session: Session, company_id: int, as_of: date) -> float | None:
    """Return the Q4 revenue a decision on `as_of` would have seen."""
    periods = periods_as_of(session, company_id, as_of)
    match = [period for period in periods if period.period_end == Q4]
    return match[0].revenue if match else None


# ---------------------------------------------------------------------------
# The worked example
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_restatement_is_invisible_until_it_is_filed(session: Session) -> None:
    """Revenue 100M filed 2026-02-15, restated to 104M filed 2026-04-30.

    January sees nothing, March sees the original, May sees the restatement.
    """
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    _observe(session, company_id, "Revenues", 104_000_000.0, RESTATEMENT)

    assert _revenue_as_of(session, company_id, date(2026, 1, 15)) is None
    assert _revenue_as_of(session, company_id, date(2026, 3, 1)) == 100_000_000.0
    assert _revenue_as_of(session, company_id, date(2026, 5, 1)) == 104_000_000.0


@pytest.mark.integration
def test_without_an_acceptance_time_a_filing_waits_until_the_next_day(
    session: Session,
) -> None:
    """The conservative fallback, and the reason it is conservative.

    A filing date says nothing about the hour. With no acceptance instant stored
    the system cannot know whether the document was public before that day's
    close, so it withholds it — wrong by at most a day, and never early.
    """
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)

    assert _revenue_as_of(session, company_id, FIRST_FILING) is None
    assert _revenue_as_of(session, company_id, date(2026, 2, 16)) == 100_000_000.0


@pytest.mark.integration
def test_a_filing_accepted_before_the_close_is_available_that_day(
    session: Session,
) -> None:
    """26.4% of filings arrive before the open and 27.9% during the session.

    With the acceptance instant known, those are correctly visible to a
    close-of-day decision on the very day they were filed.
    """
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    _accept(session, ACCESSION, datetime(2026, 2, 15, 11, 1, tzinfo=UTC))  # 06:01 ET

    assert _revenue_as_of(session, company_id, FIRST_FILING) == 100_000_000.0


@pytest.mark.integration
def test_a_filing_accepted_after_the_close_is_not_available_that_day(
    session: Session,
) -> None:
    """45.7% of filings arrive after the close. A date comparison would show
    them to a decision that could not have seen them; an instant does not."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    _accept(session, ACCESSION, datetime(2026, 2, 15, 21, 30, tzinfo=UTC))  # 16:30 ET

    assert _revenue_as_of(session, company_id, FIRST_FILING) is None
    assert _revenue_as_of(session, company_id, date(2026, 2, 16)) == 100_000_000.0


@pytest.mark.integration
def test_the_cutoff_is_the_close_to_the_minute(session: Session) -> None:
    """One minute either side of 16:00 New York decides availability."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    # 2026-02-15 is winter, so the close is 21:00 UTC.
    _accept(session, ACCESSION, datetime(2026, 2, 15, 20, 59, tzinfo=UTC))

    assert _revenue_as_of(session, company_id, FIRST_FILING) == 100_000_000.0


@pytest.mark.integration
def test_one_minute_after_the_close_is_not_available(session: Session) -> None:
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    _accept(session, ACCESSION, datetime(2026, 2, 15, 21, 1, tzinfo=UTC))

    assert _revenue_as_of(session, company_id, FIRST_FILING) is None


@pytest.mark.integration
def test_the_close_follows_daylight_saving(session: Session) -> None:
    """A fixed UTC offset would be an hour wrong for two-thirds of the year.

    The same 20:30 UTC acceptance is before the close in winter (21:00 UTC) and
    after it in summer (20:00 UTC).
    """
    winter = _company(session, "WNTR")
    _observe(
        session,
        winter,
        "Revenues",
        1.0,
        date(2026, 1, 15),
        period_end=date(2025, 12, 31),
        accession="acc-winter",
    )
    _accept(session, "acc-winter", datetime(2026, 1, 15, 20, 30, tzinfo=UTC))

    summer = _company(session, "SMMR")
    _observe(
        session,
        summer,
        "Revenues",
        1.0,
        date(2026, 7, 15),
        period_end=date(2026, 6, 30),
        accession="acc-summer",
    )
    _accept(session, "acc-summer", datetime(2026, 7, 15, 20, 30, tzinfo=UTC))

    assert periods_as_of(session, winter, date(2026, 1, 15))  # 15:30 ET, before
    assert not periods_as_of(session, summer, date(2026, 7, 15))  # 16:30 ET, after


@pytest.mark.integration
def test_period_end_is_never_a_proxy_for_availability(session: Session) -> None:
    """The quarter ended in December. It was not knowable in January."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)

    assert _revenue_as_of(session, company_id, date(2026, 1, 31)) is None


@pytest.mark.integration
def test_an_observation_with_no_filing_date_never_becomes_available(
    session: Session,
) -> None:
    """Unknown availability is not a licence to assume it. Even years later."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 90_000_000.0, None)

    assert FundamentalObservationRepository(session).count() == 1
    assert _revenue_as_of(session, company_id, date(2030, 1, 1)) is None


# ---------------------------------------------------------------------------
# Isolation from later information
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_later_filing_cannot_change_an_earlier_answer(session: Session) -> None:
    """Compute first, then insert the future. The earlier answer must not move."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    before = periods_as_of(session, company_id, date(2026, 3, 1))

    _observe(session, company_id, "Revenues", 104_000_000.0, RESTATEMENT)
    after = periods_as_of(session, company_id, date(2026, 3, 1))

    assert before == after
    assert after[0].revenue == 100_000_000.0


@pytest.mark.integration
def test_the_operational_table_cannot_influence_a_historical_metric(
    session: Session,
) -> None:
    """`financial_snapshots` is overwritten routinely. It is not read here."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    before = periods_as_of(session, company_id, date(2026, 3, 1))

    # The operational table says something entirely different for the same
    # period, as it would after a restatement was ingested.
    FinancialSnapshotRepository(session).upsert_periods(
        company_id,
        [
            FinancialPeriod(
                period_end=Q4,
                period_start=Q4_START,
                cadence=PeriodCadence.QUARTERLY,
                revenue=999_000_000.0,
                source="operational",
            )
        ],
    )
    session.flush()

    after = periods_as_of(session, company_id, date(2026, 3, 1))
    assert after == before
    assert after[0].revenue == 100_000_000.0


@pytest.mark.integration
def test_price_bars_are_bounded_by_the_same_date(session: Session, make: type[Make]) -> None:
    """Both halves are point-in-time, not just the fundamentals."""
    from data_access import PriceHistoryRepository

    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    PriceHistoryRepository(session).upsert_bars(company_id, make.bars(sessions=60))
    session.flush()

    early = build_metrics_as_of(session, company_id, date(2026, 3, 1))
    assert early.metrics is not None
    # The seeded bars end at LATEST_SESSION (2026-06-30), all after the cutoff.
    assert early.metrics.price is None


# ---------------------------------------------------------------------------
# Multi-concept resolution
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_metric_waits_for_its_slowest_input(session: Session) -> None:
    """Revenue filed in February, gross profit in April.

    A margin needs both. In March only revenue exists, so the margin does not.
    """
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    _observe(session, company_id, "GrossProfit", 40_000_000.0, RESTATEMENT)

    march = periods_as_of(session, company_id, date(2026, 3, 1))
    may = periods_as_of(session, company_id, date(2026, 5, 1))

    assert march[0].revenue == 100_000_000.0
    assert march[0].gross_profit is None
    assert may[0].gross_profit == 40_000_000.0


@pytest.mark.integration
def test_restating_one_component_leaves_the_others_alone(session: Session) -> None:
    """A revenue revision must not disturb the gross profit filed beside it."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)
    _observe(session, company_id, "GrossProfit", 40_000_000.0, FIRST_FILING)
    _observe(session, company_id, "Revenues", 104_000_000.0, RESTATEMENT)

    march = periods_as_of(session, company_id, date(2026, 3, 1))
    may = periods_as_of(session, company_id, date(2026, 5, 1))

    assert (march[0].revenue, march[0].gross_profit) == (100_000_000.0, 40_000_000.0)
    assert (may[0].revenue, may[0].gross_profit) == (104_000_000.0, 40_000_000.0)


@pytest.mark.integration
def test_a_period_with_no_available_observation_is_absent_not_zero(
    session: Session,
) -> None:
    """Missing stays missing. The rule that runs through the whole codebase."""
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)

    periods = periods_as_of(session, company_id, date(2026, 3, 1))
    assert periods[0].revenue == 100_000_000.0
    assert periods[0].operating_income is None
    assert periods[0].cash is None


@pytest.mark.integration
def test_nothing_filed_yet_yields_no_metrics_rather_than_empty_ones(
    session: Session,
) -> None:
    company_id = _company(session)
    _observe(session, company_id, "Revenues", 100_000_000.0, FIRST_FILING)

    result = build_metrics_as_of(session, company_id, date(2026, 1, 1))

    assert result.metrics is None
    assert result.periods == ()
    assert result.observations_available == 0


@pytest.mark.integration
def test_an_unknown_company_is_reported_not_raised(session: Session) -> None:
    result = build_metrics_as_of(session, 9999, date(2026, 3, 1))

    assert result.metrics is None


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_two_decision_dates_see_two_different_companies(session: Session) -> None:
    """The scenario the phase exists for.

    One company, four quarters, filings spread across a year, and one
    restatement. Metrics built on two dates must reflect only what each date
    knew — including a trailing-twelve-month revenue that differs precisely
    because the restatement had not happened yet.
    """
    company_id = _company(session)
    quarters = [
        (date(2025, 3, 31), date(2025, 1, 1), 100_000_000.0, date(2025, 5, 1)),
        (date(2025, 6, 30), date(2025, 4, 1), 110_000_000.0, date(2025, 8, 1)),
        (date(2025, 9, 30), date(2025, 7, 1), 120_000_000.0, date(2025, 11, 1)),
        (Q4, Q4_START, 130_000_000.0, FIRST_FILING),
    ]
    for period_end, period_start, revenue, filed in quarters:
        _observe(
            session,
            company_id,
            "Revenues",
            revenue,
            filed,
            period_end=period_end,
            period_start=period_start,
        )
    # Q4 is later restated upward.
    _observe(session, company_id, "Revenues", 137_000_000.0, RESTATEMENT)

    march = periods_as_of(session, company_id, date(2026, 3, 1))
    may = periods_as_of(session, company_id, date(2026, 5, 1))

    # March: four quarters, Q4 at its originally filed figure.
    assert [period.period_end for period in march] == [q[0] for q in quarters]
    assert march[-1].revenue == 130_000_000.0
    assert sum(p.revenue or 0 for p in march) == 460_000_000.0

    # May: same four quarters, Q4 restated. The trailing year moves by exactly
    # the restatement and by nothing else.
    assert [period.period_end for period in may] == [q[0] for q in quarters]
    assert may[-1].revenue == 137_000_000.0
    assert sum(p.revenue or 0 for p in may) == 467_000_000.0

    # And an August decision, before Q3 was filed, saw only two quarters.
    august = periods_as_of(session, company_id, date(2025, 8, 15))
    assert [period.period_end for period in august] == [
        date(2025, 3, 31),
        date(2025, 6, 30),
    ]


@pytest.mark.integration
def test_the_engine_reuses_the_frozen_normalisation(session: Session) -> None:
    """Not a second implementation.

    The same function normalises the live provider path and this one, so a
    change to concept chains, cadence or currency rules reaches both or
    neither.
    """
    import inspect

    from api_clients import periods_from_observations
    from api_clients.edgar import SecEdgarFundamentals

    live = inspect.getsource(SecEdgarFundamentals.get_financial_statements)
    assert "periods_from_facts_document" in live
    assert "periods_from_facts_document" in inspect.getsource(periods_from_observations)
