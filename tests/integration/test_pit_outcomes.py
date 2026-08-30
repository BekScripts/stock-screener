"""The outcome half of the point-in-time record: actions, delistings, returns.

Three passes that together answer "and then what happened". Each is tested for
the same two things — that it stores what a source reports, and that it refuses
to invent what the source does not.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    CorporateActionRepository,
    DelistingRepository,
    ForwardReturnRepository,
    PriceHistoryRepository,
)
from domain import (
    CompanyProfile,
    CorporateActionKind,
    CorporateActionRecord,
    DelistingRecord,
    PriceBar,
)
from stock_screener.pit import (
    discover_delistings,
    ingest_corporate_actions,
    measure_forward_returns,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

BENCHMARK = "SPY"


class FakeActionSource:
    """A provider that reports corporate actions, as Alpaca does."""

    def __init__(self, actions: Sequence[CorporateActionRecord]) -> None:
        self._actions = list(actions)
        self.calls: list[tuple[tuple[str, ...], date, date]] = []

    def get_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[CorporateActionRecord]:
        """Return the actions whose symbol was asked for."""
        self.calls.append((tuple(symbols), start, end))
        wanted = set(symbols)
        return [action for action in self._actions if action.symbol in wanted]


class FakeDelistingSource:
    """A provider that enumerates delistings without a symbol filter."""

    def __init__(self, delistings: Sequence[DelistingRecord]) -> None:
        self._delistings = list(delistings)

    def list_delistings(self, start: date, end: date) -> list[DelistingRecord]:
        """Return the delistings inside the window."""
        return [d for d in self._delistings if start <= d.effective_date <= end]


class PlainProvider:
    """A provider that serves bars and nothing else."""

    def get_daily_prices(self, ticker: str) -> list[PriceBar]:
        """Return nothing; this fake exists only to lack the other methods."""
        return []


def _company(session: Session, ticker: str) -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _split(symbol: str, ex_date: date, factor: float, ident: str) -> CorporateActionRecord:
    """One forward split."""
    return CorporateActionRecord(
        symbol=symbol,
        kind=CorporateActionKind.FORWARD_SPLIT,
        ex_date=ex_date,
        split_factor=factor,
        source="test",
        source_id=ident,
    )


def _sessions(count: int, start: date = date(2024, 1, 2)) -> list[date]:
    """Return `count` consecutive weekdays, standing in for a calendar."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


def _price_history(session: Session, company_id: int, days: Sequence[date], price: float) -> None:
    """Give a company a flat price series over these sessions."""
    PriceHistoryRepository(session).upsert_bars(
        company_id,
        [
            PriceBar(date=day, open=price, high=price, low=price, close=price, volume=1_000_000)
            for day in days
        ],
    )


def _benchmark(session: Session, days: Sequence[date]) -> None:
    """Give the benchmark a bar on every session, defining the calendar."""
    BenchmarkPriceRepository(session).upsert_bars(
        BENCHMARK,
        [
            PriceBar(date=day, open=500.0, high=500.0, low=500.0, close=500.0, volume=1_000_000)
            for day in days
        ],
    )


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------


class TestCorporateActionIngestion:
    """Reading the factors that make a past price readable."""

    @pytest.mark.integration
    def test_actions_are_stored_for_known_companies(self, session: Session) -> None:
        _company(session, "AAPL")
        provider = FakeActionSource([_split("AAPL", date(2020, 8, 31), 4.0, "a1")])

        result = ingest_corporate_actions(
            session, provider, start=date(2020, 1, 1), end=date(2021, 1, 1)
        )

        assert result.stored == 1
        assert CorporateActionRepository(session).count() == 1

    @pytest.mark.integration
    def test_re_running_stores_nothing_further(self, session: Session) -> None:
        """The source's own id is the identity, so a refetch is a no-op."""
        _company(session, "AAPL")
        provider = FakeActionSource([_split("AAPL", date(2020, 8, 31), 4.0, "a1")])
        start, end = date(2020, 1, 1), date(2021, 1, 1)

        ingest_corporate_actions(session, provider, start=start, end=end)
        second = ingest_corporate_actions(session, provider, start=start, end=end)

        assert second.fetched == 1
        assert second.stored == 0

    @pytest.mark.integration
    def test_two_actions_alike_but_for_their_id_are_both_kept(self, session: Session) -> None:
        """ACNB filed one $0.34 dividend twice, with different payable dates.

        A composite of kind, date and amount cannot tell them apart, and
        collapsing them would decide unilaterally which of a source's two
        statements is the true one.
        """
        _company(session, "ACNB")
        twice = [
            CorporateActionRecord(
                symbol="ACNB",
                kind=CorporateActionKind.CASH_DIVIDEND,
                ex_date=date(2025, 5, 30),
                cash_amount=0.34,
                currency="USD",
                payable_date=date(2025, 6, day),
                source="test",
                source_id=f"d{day}",
            )
            for day in (12, 13)
        ]

        result = ingest_corporate_actions(
            session, FakeActionSource(twice), start=date(2025, 1, 1), end=date(2026, 1, 1)
        )

        assert result.stored == 2

    @pytest.mark.integration
    def test_an_action_for_an_unknown_symbol_is_recorded_not_dropped(
        self, session: Session
    ) -> None:
        _company(session, "AAPL")
        provider = FakeActionSource(
            [
                _split("AAPL", date(2020, 8, 31), 4.0, "a1"),
                _split("GONE", date(2020, 8, 31), 2.0, "a2"),
            ]
        )

        result = ingest_corporate_actions(
            session, provider, start=date(2020, 1, 1), end=date(2021, 1, 1)
        )

        # GONE was never asked for, so it never arrives — but a provider that
        # volunteered it would have the symbol named rather than discarded.
        assert result.unmatched == ()
        assert result.stored == 1

    @pytest.mark.integration
    def test_a_provider_without_the_endpoint_is_skipped_not_failed(self, session: Session) -> None:
        _company(session, "AAPL")

        result = ingest_corporate_actions(
            session, PlainProvider(), start=date(2020, 1, 1), end=date(2021, 1, 1)
        )

        assert result.fetched == 0
        assert CorporateActionRepository(session).count() == 0


# ---------------------------------------------------------------------------
# Delistings
# ---------------------------------------------------------------------------


class TestDelistingDiscovery:
    """Finding the securities the companies table cannot contain."""

    @pytest.mark.integration
    def test_a_delisting_is_stored_without_a_company_row(self, session: Session) -> None:
        """The whole point: these securities are not in `companies`."""
        provider = FakeDelistingSource(
            [
                DelistingRecord(
                    symbol="ATVI",
                    kind=CorporateActionKind.CASH_MERGER,
                    effective_date=date(2023, 10, 13),
                    cash_per_share=95.0,
                    source="test",
                    source_id="m1",
                )
            ]
        )

        result = discover_delistings(
            session, provider, start=date(2023, 1, 1), end=date(2023, 12, 31)
        )

        assert result.stored == 1
        assert CompanyRepository(session).get_by_ticker("ATVI") is None

    @pytest.mark.integration
    def test_a_worthless_removal_stores_no_cash_rather_than_zero(self, session: Session) -> None:
        """Zero and unknown are different, and `kind` already says which."""
        provider = FakeDelistingSource(
            [
                DelistingRecord(
                    symbol="DEAD",
                    kind=CorporateActionKind.WORTHLESS_REMOVAL,
                    effective_date=date(2024, 6, 3),
                    source="test",
                    source_id="w1",
                )
            ]
        )

        discover_delistings(session, provider, start=date(2024, 1, 1), end=date(2024, 12, 31))

        stored = DelistingRepository(session).by_ticker(["DEAD"])["DEAD"]
        assert stored.cash_per_share is None

    @pytest.mark.integration
    def test_a_stock_merger_records_who_paid_and_at_what_rate(self, session: Session) -> None:
        provider = FakeDelistingSource(
            [
                DelistingRecord(
                    symbol="PXD",
                    kind=CorporateActionKind.STOCK_MERGER,
                    effective_date=date(2024, 5, 3),
                    acquirer_symbol="XOM",
                    acquirer_rate=2.3234,
                    acquiree_rate=1.0,
                    source="test",
                    source_id="s1",
                )
            ]
        )

        discover_delistings(session, provider, start=date(2024, 1, 1), end=date(2024, 12, 31))

        stored = DelistingRepository(session).by_ticker(["PXD"])["PXD"]
        assert stored.acquirer_ticker == "XOM"
        assert stored.acquirer_rate == pytest.approx(2.3234)

    @pytest.mark.integration
    def test_re_running_a_window_stores_nothing_further(self, session: Session) -> None:
        provider = FakeDelistingSource(
            [
                DelistingRecord(
                    symbol="ATVI",
                    kind=CorporateActionKind.CASH_MERGER,
                    effective_date=date(2023, 10, 13),
                    cash_per_share=95.0,
                    source="test",
                    source_id="m1",
                )
            ]
        )
        start, end = date(2023, 1, 1), date(2023, 12, 31)

        discover_delistings(session, provider, start=start, end=end)
        second = discover_delistings(session, provider, start=start, end=end)

        assert second.fetched == 1
        assert second.stored == 0

    @pytest.mark.integration
    def test_a_reused_ticker_resolves_to_the_earlier_security(self, session: Session) -> None:
        """138 delisted tickers are live symbols today, owned by other companies."""
        provider = FakeDelistingSource(
            [
                DelistingRecord(
                    symbol="TWIN",
                    kind=CorporateActionKind.CASH_MERGER,
                    effective_date=date(2021, 4, 1),
                    cash_per_share=10.0,
                    source="test",
                    source_id="e1",
                ),
                DelistingRecord(
                    symbol="TWIN",
                    kind=CorporateActionKind.WORTHLESS_REMOVAL,
                    effective_date=date(2025, 4, 1),
                    source="test",
                    source_id="e2",
                ),
            ]
        )

        discover_delistings(session, provider, start=date(2020, 1, 1), end=date(2026, 1, 1))

        stored = DelistingRepository(session).by_ticker(["TWIN"])["TWIN"]
        assert stored.effective_date == date(2021, 4, 1)

    @pytest.mark.integration
    def test_a_provider_that_cannot_enumerate_is_skipped_not_failed(self, session: Session) -> None:
        result = discover_delistings(
            session, PlainProvider(), start=date(2023, 1, 1), end=date(2023, 12, 31)
        )

        assert result.fetched == 0
        assert DelistingRepository(session).count() == 0


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------


class TestForwardReturnMeasurement:
    """Measuring what followed, against a calendar the benchmark defines."""

    @pytest.mark.integration
    def test_returns_are_measured_and_stored(self, session: Session) -> None:
        days = _sessions(40)
        _benchmark(session, days)
        company_id = _company(session, "ACME")
        _price_history(session, company_id, days, 100.0)

        result = measure_forward_returns(session, days[0], benchmark=BENCHMARK, horizons=(1, 5))

        assert result.stored == 2
        assert ForwardReturnRepository(session).count() == 2

    @pytest.mark.integration
    def test_the_measured_horizon_is_the_one_asked_for(self, session: Session) -> None:
        days = _sessions(40)
        _benchmark(session, days)
        company_id = _company(session, "ACME")
        _price_history(session, company_id, days, 100.0)

        measure_forward_returns(session, days[0], benchmark=BENCHMARK, horizons=(5,))

        assert ForwardReturnRepository(session).measured_horizons(days[0]) == {(company_id, 5)}

    @pytest.mark.integration
    def test_re_measuring_a_horizon_stores_nothing_further(self, session: Session) -> None:
        days = _sessions(40)
        _benchmark(session, days)
        company_id = _company(session, "ACME")
        _price_history(session, company_id, days, 100.0)

        measure_forward_returns(session, days[0], benchmark=BENCHMARK, horizons=(5,))
        second = measure_forward_returns(session, days[0], benchmark=BENCHMARK, horizons=(5,))

        assert second.stored == 0

    @pytest.mark.integration
    def test_a_horizon_not_yet_measured_is_added_beside_the_others(self, session: Session) -> None:
        """A truncated horizon realises on a later run without disturbing the rest."""
        days = _sessions(40)
        _benchmark(session, days)
        company_id = _company(session, "ACME")
        _price_history(session, company_id, days, 100.0)

        measure_forward_returns(session, days[0], benchmark=BENCHMARK, horizons=(5,))
        second = measure_forward_returns(session, days[0], benchmark=BENCHMARK, horizons=(5, 20))

        assert second.stored == 1
        assert (company_id, 20) in ForwardReturnRepository(session).measured_horizons(days[0])

    @pytest.mark.integration
    def test_without_a_benchmark_there_is_no_calendar_and_nothing_is_measured(
        self, session: Session
    ) -> None:
        """A security's own bars must never stand in for the market's sessions."""
        days = _sessions(40)
        company_id = _company(session, "ACME")
        _price_history(session, company_id, days, 100.0)

        result = measure_forward_returns(session, days[0], benchmark=BENCHMARK)

        assert result.stored == 0
        assert ForwardReturnRepository(session).count() == 0

    @pytest.mark.integration
    def test_a_company_with_no_prices_is_skipped_silently(self, session: Session) -> None:
        days = _sessions(40)
        _benchmark(session, days)
        _company(session, "ACME")

        result = measure_forward_returns(session, days[0], benchmark=BENCHMARK)

        assert result.stored == 0
