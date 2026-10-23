"""Adversarial tests for the historical-security layer (blocker 3).

Each test names a way survivorship bias or look-ahead could quietly enter a
backtest through a delisted name, and asserts the layer refuses it: a security
that needs a live company row to be scored, a reused ticker that joins the old
security to the new company, a future split that leaks a share count backward, a
terminal value invented where the source gives none.

All seeded, no network: the properties are properties of the storage and the
frozen calculations, and a deterministic fixture proves them without a provider.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import TYPE_CHECKING

import pytest

from api_clients.errors import ProviderError
from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    FundamentalObservationRepository,
    HistoricalCorporateActionRepository,
    HistoricalObservationRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    PriceHistoryRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
)
from domain import (
    COMPOUNDER_V1_2,
    CURRENT_SCORE_VERSION,
    CompanyProfile,
    CorporateActionKind,
    CorporateActionRecord,
    DelistingReason,
    DelistingRecord,
    FilingObservation,
    PriceBar,
    StatementProfile,
)
from stock_screener.backtest import (
    CohortEntry,
    ingest_cohort,
    ingest_historical_security,
    reconstruct_historical_score,
    reconstruct_score,
    reconstruct_universe,
)
from stock_screener.config import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

BENCHMARK = "SPY"


def _settings() -> Settings:
    return Settings(environment="test", benchmark_symbol=BENCHMARK)


def _weekdays(count: int, start: date = date(2022, 1, 3)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


CALENDAR = _weekdays(120)
FILING = CALENDAR[10]
RESTATEMENT = CALENDAR[40]
AS_OF = CALENDAR[60]


def _fingerprint(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def _hist_security(
    session: Session,
    cik: int,
    ticker: str,
    *,
    name: str | None = None,
    status: str = "ACQUIRED",
    last_trading_date: date | None = None,
    terminal_value: float | None = None,
    statement_profile: str | None = None,
    exchange: str | None = "NASDAQ",
) -> int:
    security = HistoricalSecurityRepository(session).upsert(
        cik,
        name=name or f"{ticker} Historical",
        primary_ticker=ticker,
        exchange=exchange,
        status=status,
        sector=None,
        industry=None,
        statement_profile=statement_profile,
        last_trading_date=last_trading_date,
        terminal_value_per_share=terminal_value,
        quote_currency="USD",
    )
    session.flush()
    return security.id


def _hist_observe(
    session: Session,
    hist_id: int,
    cik: int,
    concept: str,
    value: float,
    filed: date,
    *,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
    period_end: date = date(2021, 12, 31),
    period_start: date | None = date(2021, 10, 1),
) -> None:
    HistoricalObservationRepository(session).add_all(
        hist_id,
        [
            {
                "observation_fingerprint": _fingerprint(cik, concept, value, filed, period_end),
                "taxonomy": taxonomy,
                "concept": concept,
                "value": value,
                "unit": unit,
                "period_start": period_start,
                "period_end": period_end,
                "filed_date": filed,
                "accession": f"acc-{concept}-{filed}",
                "form": "10-K",
                "source": "sec-edgar",
            }
        ],
    )
    session.flush()


def _hist_fundamentals(
    session: Session, hist_id: int, cik: int, *, revenue: float = 100_000_000.0
) -> None:
    """Seed the minimum that yields a point-in-time market cap: revenue + shares."""
    _hist_observe(session, hist_id, cik, "Revenues", revenue, FILING)
    _hist_observe(
        session,
        hist_id,
        cik,
        "EntityCommonStockSharesOutstanding",
        40_000_000.0,
        FILING,
        taxonomy="dei",
        unit="shares",
        period_start=None,
    )


def _hist_prices(session: Session, hist_id: int, days: Sequence[date], close: float = 25.0) -> None:
    HistoricalPriceRepository(session).upsert_bars(
        hist_id,
        [
            PriceBar(date=d, open=close, high=close, low=close, close=close, volume=1_000_000)
            for d in days
        ],
    )


def _hist_split(session: Session, hist_id: int, cik: int, ex_date: date, factor: float) -> None:
    HistoricalCorporateActionRepository(session).add_all(
        hist_id,
        [
            {
                "action_fingerprint": _fingerprint(cik, "split", ex_date, factor),
                "ticker": "X",
                "action_type": "FORWARD_SPLIT",
                "ex_date": ex_date,
                "split_factor": factor,
                "cash_amount": None,
                "currency": None,
                "record_date": None,
                "payable_date": None,
                "source": "test",
                "source_id": f"s-{ex_date}",
            }
        ],
    )
    session.flush()


def _benchmark(session: Session, days: Sequence[date]) -> None:
    BenchmarkPriceRepository(session).upsert_bars(
        BENCHMARK,
        [
            PriceBar(date=d, open=500.0, high=500.0, low=500.0, close=500.0, volume=1_000_000)
            for d in days
        ],
    )


def _company(
    session: Session, ticker: str, *, market_cap: float | None = None, active: bool = True
) -> int:
    companies = CompanyRepository(session)
    companies.upsert_profile(
        CompanyProfile(
            ticker=ticker,
            name=f"{ticker} Inc",
            exchange="NASDAQ",
            sector="Technology",
            industry="Software",
            market_cap=market_cap,
            is_active=active,
        )
    )
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _company_observe(
    session: Session, company_id: int, concept: str, value: float, filed: date, **kw: object
) -> None:
    taxonomy = str(kw.get("taxonomy", "us-gaap"))
    unit = str(kw.get("unit", "USD"))
    period_start = kw.get("period_start", date(2021, 10, 1))
    period_end = date(2021, 12, 31)
    FundamentalObservationRepository(session).add_all(
        company_id,
        [
            {
                "observation_fingerprint": _fingerprint(company_id, concept, value, filed),
                "taxonomy": taxonomy,
                "concept": concept,
                "value": value,
                "unit": unit,
                "period_start": period_start,
                "period_end": period_end,
                "filed_date": filed,
                "accession": f"c-{concept}-{filed}",
                "form": "10-K",
                "source": "sec-edgar",
            }
        ],
    )
    session.flush()


# ---------------------------------------------------------------------------


class TestReconstructionWithoutACompanyRow:
    @pytest.mark.integration
    def test_a_delisted_security_reconstructs_with_no_companies_row(self, session: Session) -> None:
        hist_id = _hist_security(session, 718877, "ATVI", name="Activision Blizzard")
        _hist_fundamentals(session, hist_id, 718877)
        _hist_prices(session, hist_id, CALENDAR)
        _benchmark(session, CALENDAR)

        result = reconstruct_historical_score(session, _settings(), hist_id, AS_OF)

        assert result is not None
        assert result.metrics is not None
        assert result.metrics.market_cap == pytest.approx(1_000_000_000.0)  # 40M x 25
        # No company row was needed or created.
        assert CompanyRepository(session).count() == 0

    @pytest.mark.integration
    def test_the_reconstruction_carries_the_cik_identity(self, session: Session) -> None:
        hist_id = _hist_security(session, 1418091, "TWTR", name="Twitter")
        _hist_fundamentals(session, hist_id, 1418091)
        _hist_prices(session, hist_id, CALENDAR)

        result = reconstruct_historical_score(session, _settings(), hist_id, AS_OF)

        assert result is not None
        assert result.cik == 1418091
        assert result.ticker == "TWTR"


class TestNoDependenceOnTodaysCompanyState:
    @pytest.mark.integration
    def test_a_namesake_company_cannot_change_the_historical_market_cap(
        self, session: Session
    ) -> None:
        """A live company that reused the ticker, with an absurd market cap and
        inactive today, must not touch the delisted security's reconstruction."""
        _company(session, "BBBY", market_cap=9.9e14, active=False)
        hist_id = _hist_security(session, 886158, "BBBY", name="Bed Bath & Beyond")
        _hist_fundamentals(session, hist_id, 886158)
        _hist_prices(session, hist_id, CALENDAR)

        result = reconstruct_historical_score(session, _settings(), hist_id, AS_OF)

        assert result is not None
        assert result.metrics is not None
        # 40M shares x 25.0 — never the namesake's 9.9e14 quote.
        assert result.metrics.market_cap == pytest.approx(1_000_000_000.0)


class TestTickerReuseAndChange:
    @pytest.mark.integration
    def test_ticker_reuse_does_not_join_old_security_to_new_company(self, session: Session) -> None:
        company_id = _company(session, "BBBY", market_cap=5_000_000.0)
        hist_id = _hist_security(session, 886158, "BBBY")

        # The symbol resolves to two different, unrelated things by identity.
        historical = HistoricalSecurityRepository(session).get_by_ticker("BBBY")
        company = CompanyRepository(session).get_by_ticker("BBBY")

        assert historical is not None and historical.id == hist_id and historical.cik == 886158
        assert company is not None and company.id == company_id
        # No shared key: the historical registry has no company_id at all.
        assert not hasattr(historical, "company_id")

    @pytest.mark.integration
    def test_a_ticker_change_keeps_one_economic_security(self, session: Session) -> None:
        """FB→META is one filer, one CIK. Ingesting under each ticker updates the
        single row rather than creating a second security."""
        repo = HistoricalSecurityRepository(session)
        repo.upsert(1326801, name="Meta Platforms", primary_ticker="FB", status="DELISTED")
        repo.upsert(1326801, name="Meta Platforms", primary_ticker="META", status="DELISTED")
        session.flush()

        assert repo.count() == 1
        stored = repo.get_by_cik(1326801)
        assert stored is not None and stored.primary_ticker == "META"


class TestNoLookAhead:
    @pytest.mark.integration
    def test_a_future_split_does_not_leak_a_share_count_backward(self, session: Session) -> None:
        """The share count is the pre-split filed figure; the future split is
        applied to the *price* to recover what it traded at, so the market cap
        re-inflates rather than the count being restated backward."""
        cik = 111111
        without = _hist_security(session, cik, "NOS")
        _hist_fundamentals(session, without, cik)
        _hist_prices(session, without, CALENDAR)

        withsplit = _hist_security(session, cik + 1, "SPL")
        _hist_fundamentals(session, withsplit, cik + 1)
        _hist_prices(session, withsplit, CALENDAR)
        # A 2-for-1 split after the decision date. The stored close is on today's
        # split-adjusted basis; recovering the as-traded price doubles it.
        _hist_split(session, withsplit, cik + 1, CALENDAR[90], 2.0)

        base = reconstruct_historical_score(session, _settings(), without, AS_OF)
        split = reconstruct_historical_score(session, _settings(), withsplit, AS_OF)

        assert base is not None and base.metrics is not None
        assert split is not None and split.metrics is not None
        base_cap = base.metrics.market_cap
        split_cap = split.metrics.market_cap
        assert base_cap is not None and split_cap is not None
        assert split_cap == pytest.approx(2 * base_cap)

    @pytest.mark.integration
    def test_a_restatement_is_invisible_before_it_was_filed(self, session: Session) -> None:
        cik = 222222
        hist_id = _hist_security(session, cik, "RES")
        _hist_observe(session, hist_id, cik, "Revenues", 100_000_000.0, FILING)
        _hist_observe(session, hist_id, cik, "Revenues", 104_000_000.0, RESTATEMENT)
        _hist_prices(session, hist_id, CALENDAR)

        repo = HistoricalObservationRepository(session)
        before_any = repo.available_as_of(hist_id, CALENDAR[5])
        after_first = repo.available_as_of(hist_id, CALENDAR[20])
        after_restate = repo.available_as_of(hist_id, CALENDAR[50])

        assert [o.value for o in before_any] == []
        assert [o.value for o in after_first] == [100_000_000.0]
        assert sorted(o.value for o in after_restate) == [100_000_000.0, 104_000_000.0]


class TestUniverseSeparation:
    @pytest.mark.integration
    def test_a_delisted_security_is_absent_from_the_investable_universe(
        self, session: Session
    ) -> None:
        """The universe is reconstructed from `companies`; a historical security
        has no row there and so can never appear in it."""
        hist_id = _hist_security(session, 719739, "SIVB", name="SVB Financial")
        _hist_fundamentals(session, hist_id, 719739)

        universe = reconstruct_universe(session, AS_OF)

        assert universe.members == ()
        # Yet it remains addressable through its own registry.
        assert HistoricalSecurityRepository(session).get_by_cik(719739) is not None

    @pytest.mark.integration
    def test_companies_and_historical_securities_do_not_contaminate(self, session: Session) -> None:
        """The same ticker, different data, different tables — each path reads
        only its own, and writing one never moves the other's counts."""
        company_id = _company(session, "AAA")
        _company_observe(session, company_id, "Revenues", 500_000_000.0, FILING)
        _company_observe(
            session,
            company_id,
            "EntityCommonStockSharesOutstanding",
            10_000_000.0,
            FILING,
            taxonomy="dei",
            unit="shares",
            period_start=None,
        )
        PriceHistoryRepository(session).upsert_bars(
            company_id,
            [
                PriceBar(date=d, open=25.0, high=25.0, low=25.0, close=25.0, volume=1_000_000)
                for d in CALENDAR
            ],
        )
        hist_id = _hist_security(session, 886158, "AAA")
        _hist_fundamentals(session, hist_id, 886158, revenue=100_000_000.0)
        _hist_prices(session, hist_id, CALENDAR)
        _benchmark(session, CALENDAR)

        company = reconstruct_score(session, _settings(), company_id, AS_OF)
        historical = reconstruct_historical_score(session, _settings(), hist_id, AS_OF)

        assert company is not None and company.metrics is not None
        assert historical is not None and historical.metrics is not None
        # Company: 10M shares x 25 = 250M. Historical: 40M x 25 = 1B. Disjoint.
        assert company.metrics.market_cap == pytest.approx(250_000_000.0)
        assert historical.metrics.market_cap == pytest.approx(1_000_000_000.0)
        assert FundamentalObservationRepository(session).count() == 2
        assert HistoricalObservationRepository(session).count() == 2


class TestTerminalValueAndFrozenScore:
    @pytest.mark.integration
    def test_a_missing_terminal_value_stays_none_never_zero(self, session: Session) -> None:
        """A receivership whose equity no source priced keeps UNKNOWN."""
        hist_id = _hist_security(
            session, 1132979, "FRC", status=DelistingReason.RECEIVERSHIP.value, terminal_value=None
        )
        _hist_fundamentals(session, hist_id, 1132979)
        _hist_prices(session, hist_id, CALENDAR)

        result = reconstruct_historical_score(session, _settings(), hist_id, AS_OF)

        assert result is not None
        assert result.status == "RECEIVERSHIP"
        assert result.terminal_value_per_share is None

    @pytest.mark.integration
    def test_the_current_score_version_is_v1_2(self) -> None:
        assert CURRENT_SCORE_VERSION == COMPOUNDER_V1_2 == "COMPOUNDER_V1_2"

    @pytest.mark.integration
    def test_the_historical_path_scores_identically_to_the_company_path(
        self, session: Session
    ) -> None:
        """Same data through both paths yields the same score — the historical
        layer reuses the frozen scorer rather than a second implementation."""
        company_id = _company(session, "SAME")
        _company_observe(session, company_id, "Revenues", 100_000_000.0, FILING)
        _company_observe(
            session,
            company_id,
            "EntityCommonStockSharesOutstanding",
            40_000_000.0,
            FILING,
            taxonomy="dei",
            unit="shares",
            period_start=None,
        )
        PriceHistoryRepository(session).upsert_bars(
            company_id,
            [
                PriceBar(date=d, open=25.0, high=25.0, low=25.0, close=25.0, volume=1_000_000)
                for d in CALENDAR
            ],
        )
        hist_id = _hist_security(session, 999999, "SAME")
        _hist_fundamentals(session, hist_id, 999999)
        _hist_prices(session, hist_id, CALENDAR)
        _benchmark(session, CALENDAR)

        company = reconstruct_score(session, _settings(), company_id, AS_OF)
        historical = reconstruct_historical_score(session, _settings(), hist_id, AS_OF)

        assert company is not None and historical is not None
        assert company.score.status is historical.score.status
        assert company.score.final_score == historical.score.final_score
        assert company.score.score_version == historical.score.score_version
        assert company.metrics is not None and historical.metrics is not None
        assert company.metrics.market_cap == historical.metrics.market_cap


class _FakeObservations:
    """A fake EDGAR-by-CIK source. Raises to model a 404 when told to."""

    def __init__(
        self,
        observations: list[FilingObservation],
        *,
        profile: StatementProfile = StatementProfile.GENERAL,
        raises: bool = False,
    ) -> None:
        self._observations = observations
        self._profile = profile
        self._raises = raises

    def get_filing_observations_by_cik(self, cik: int) -> list[FilingObservation]:
        if self._raises:
            raise ProviderError("sec-edgar request failed with HTTP 404")
        return list(self._observations)

    def get_statement_profile_by_cik(self, cik: int) -> StatementProfile:
        if self._raises:
            raise ProviderError("sec-edgar request failed with HTTP 404")
        return self._profile


class _FakeMarket:
    """A fake Alpaca. Returns canned bars, actions and delistings."""

    def __init__(
        self,
        bars: list[PriceBar],
        *,
        actions: list[CorporateActionRecord] | None = None,
        delistings: list[DelistingRecord] | None = None,
    ) -> None:
        self._bars = bars
        self._actions = actions or []
        self._delistings = delistings or []

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        return [bar for bar in self._bars if start <= bar.date <= end]

    def get_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[CorporateActionRecord]:
        return list(self._actions)

    def list_delistings(self, start: date, end: date) -> list[DelistingRecord]:
        return [d for d in self._delistings if start <= d.effective_date <= end]


def _obs(
    concept: str, value: float, *, taxonomy: str = "us-gaap", unit: str = "USD"
) -> FilingObservation:
    return FilingObservation(
        taxonomy=taxonomy,
        concept=concept,
        value=value,
        unit=unit,
        period_end=date(2022, 12, 31),
        period_start=None if unit == "shares" else date(2022, 10, 1),
        filed_date=FILING,
        accession=f"acc-{concept}",
        form="10-K",
        source="sec-edgar",
    )


class TestIngestion:
    @pytest.mark.integration
    def test_a_cash_merger_ingests_the_full_chain(self, session: Session) -> None:
        source = _FakeObservations(
            [
                _obs("Revenues", 100_000_000.0),
                _obs(
                    "EntityCommonStockSharesOutstanding",
                    40_000_000.0,
                    taxonomy="dei",
                    unit="shares",
                ),
            ]
        )
        market = _FakeMarket(
            [
                PriceBar(date=d, open=25.0, high=25.0, low=25.0, close=25.0, volume=1_000_000)
                for d in CALENDAR
            ],
            delistings=[
                DelistingRecord(
                    symbol="ATVI",
                    kind=CorporateActionKind.CASH_MERGER,
                    effective_date=CALENDAR[-1],
                    cash_per_share=95.0,
                    source="alpaca",
                    source_id="d-atvi",
                )
            ],
        )

        result = ingest_historical_security(
            session,
            observations_source=source,
            market=market,
            cik=718877,
            ticker="ATVI",
            name="Activision",
            start=CALENDAR[0],
            end=CALENDAR[-1],
            exchange="NASDAQ",
        )
        session.commit()

        assert result.status == "ACQUIRED"
        assert result.terminal_value_per_share == 95.0
        assert "NO_TERMINAL_VALUE" not in result.gaps
        assert result.observations_stored == 2
        assert result.prices_stored == len(CALENDAR)

        stored = HistoricalSecurityRepository(session).get_by_cik(718877)
        assert stored is not None
        rec = reconstruct_historical_score(session, _settings(), stored.id, AS_OF)
        assert rec is not None and rec.metrics is not None
        assert rec.metrics.market_cap == pytest.approx(1_000_000_000.0)

    @pytest.mark.integration
    def test_an_fdic_bank_records_the_gap_without_crashing(self, session: Session) -> None:
        """First Republic: no EDGAR facts (404), prices present, status asserted."""
        source = _FakeObservations([], raises=True)
        market = _FakeMarket(
            [
                PriceBar(date=d, open=3.5, high=3.5, low=3.5, close=3.5, volume=1_000_000)
                for d in CALENDAR
            ]
        )

        result = ingest_historical_security(
            session,
            observations_source=source,
            market=market,
            cik=1132979,
            ticker="FRC",
            name="First Republic",
            start=CALENDAR[0],
            end=CALENDAR[-1],
            exchange="NYSE",
            asserted_status=DelistingReason.RECEIVERSHIP,
        )
        session.commit()

        assert result.status == "RECEIVERSHIP"
        assert "NO_EDGAR_FUNDAMENTALS" in result.gaps
        assert "NO_TERMINAL_VALUE" in result.gaps
        assert result.observations_stored == 0
        assert result.prices_stored == len(CALENDAR)
        assert result.terminal_value_per_share is None

    @pytest.mark.integration
    def test_a_bank_is_classified_from_the_full_facts(self, session: Session) -> None:
        source = _FakeObservations(
            [_obs("Revenues", 5.0)], profile=StatementProfile.FINANCIAL_INSTITUTION
        )
        market = _FakeMarket(
            [PriceBar(date=CALENDAR[0], open=100.0, high=100.0, low=100.0, close=100.0, volume=1)]
        )

        ingest_historical_security(
            session,
            observations_source=source,
            market=market,
            cik=719739,
            ticker="SIVB",
            name="SVB Financial",
            start=CALENDAR[0],
            end=CALENDAR[-1],
            exchange="NASDAQ",
            asserted_status=DelistingReason.RECEIVERSHIP,
        )
        session.commit()

        stored = HistoricalSecurityRepository(session).get_by_cik(719739)
        assert stored is not None
        assert stored.statement_profile == "FINANCIAL_INSTITUTION"

    @pytest.mark.integration
    def test_re_ingesting_the_same_cik_updates_one_row(self, session: Session) -> None:
        source = _FakeObservations([_obs("Revenues", 100.0)])
        market = _FakeMarket(
            [PriceBar(date=CALENDAR[0], open=10.0, high=10.0, low=10.0, close=10.0, volume=1)]
        )
        for _ in range(2):
            ingest_historical_security(
                session,
                observations_source=source,
                market=market,
                cik=886158,
                ticker="BBBY",
                name="Bed Bath & Beyond",
                start=CALENDAR[0],
                end=CALENDAR[-1],
                exchange="NASDAQ",
            )
            session.commit()

        assert HistoricalSecurityRepository(session).count() == 1


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database.

    File-backed because the cohort runner opens a session per security, and a
    per-connection in-memory database would give each of them a blank world.
    """
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'cohort.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


class TestCohortIngestion:
    """A cohort is many securities, most of which the free record cannot serve.

    The runner therefore has to treat an unserviceable name as an outcome rather
    than an error, keep every other name's work, and report the gaps by name so
    the shape of what is missing is readable without opening the database.
    """

    @staticmethod
    def _entry(ticker: str, cik: int) -> CohortEntry:
        return CohortEntry(
            cik=cik,
            ticker=ticker,
            name=f"{ticker} Corporation",
            start=CALENDAR[0],
            end=CALENDAR[-1],
            exchange="NASDAQ",
        )

    @pytest.mark.integration
    def test_a_filer_edgar_serves_nothing_for_records_the_gap_and_continues(
        self, factory: sessionmaker[Session]
    ) -> None:
        source = _FakeObservations([], raises=True)
        market = _FakeMarket(
            [
                PriceBar(date=d, open=25.0, high=25.0, low=25.0, close=25.0, volume=1_000_000)
                for d in CALENDAR
            ]
        )

        report = ingest_cohort(
            factory,
            observations_source=source,
            market=market,
            cohort=[self._entry("AAA", 111), self._entry("BBB", 222)],
        )

        assert len(report.results) == 2
        assert report.failures == ()
        assert report.gap_counts()["NO_EDGAR_FUNDAMENTALS"] == 2
        assert report.with_fundamentals == 0
        assert report.with_prices == 2

    @pytest.mark.integration
    def test_a_security_outside_the_free_price_window_records_that_gap(
        self, factory: sessionmaker[Session]
    ) -> None:
        source = _FakeObservations([_obs("Revenues", 1_000.0)])
        market = _FakeMarket([])

        report = ingest_cohort(
            factory,
            observations_source=source,
            market=market,
            cohort=[self._entry("AAA", 111)],
        )

        assert report.gap_counts()["NO_FREE_PRICE_HISTORY"] == 1
        assert report.with_prices == 0

    @pytest.mark.integration
    def test_a_terminal_value_no_source_states_stays_unknown(
        self, factory: sessionmaker[Session]
    ) -> None:
        source = _FakeObservations([_obs("Revenues", 1_000.0)])
        market = _FakeMarket(
            [PriceBar(date=CALENDAR[0], open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
        )

        report = ingest_cohort(
            factory,
            observations_source=source,
            market=market,
            cohort=[self._entry("AAA", 111)],
        )

        assert report.with_terminal_value == 0
        assert report.results[0].terminal_value_per_share is None
        assert report.gap_counts()["NO_TERMINAL_VALUE"] == 1

    @pytest.mark.integration
    def test_a_provider_that_cannot_serve_the_reads_ingests_nothing(
        self, factory: sessionmaker[Session]
    ) -> None:
        report = ingest_cohort(
            factory,
            observations_source=object(),
            market=object(),
            cohort=[self._entry("AAA", 111)],
        )

        assert report.results == ()
        assert report.failures == ()

    @pytest.mark.integration
    def test_re_running_a_cohort_stores_no_observation_twice(
        self, factory: sessionmaker[Session]
    ) -> None:
        source = _FakeObservations([_obs("Revenues", 1_000.0)])
        market = _FakeMarket(
            [PriceBar(date=CALENDAR[0], open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
        )
        cohort = [self._entry("AAA", 111)]
        first = ingest_cohort(factory, observations_source=source, market=market, cohort=cohort)

        again = ingest_cohort(factory, observations_source=source, market=market, cohort=cohort)

        assert first.results[0].observations_stored == 1
        assert again.results[0].observations_stored == 0
