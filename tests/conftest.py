"""Shared pytest fixtures for the root application test suite.

`Make` builds synthetic price and fundamental history; `Seed` writes it into a
database along with a score snapshot, so a test that needs a scored company
starts from one rather than from twenty lines of setup.

Both live here rather than in a second `conftest.py` under `tests/integration/`:
mypy refuses two modules with the same name on one path, and one shared file is
cheaper than the package plumbing that would silence it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

from api_clients import MockFundamentals, MockMarketData
from data_access import (
    PRELIMINARY,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ScoreRecord,
    ScoreSnapshotRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyMetrics,
    CompanyProfile,
    CompanyScore,
    ComponentScore,
    ComponentStatus,
    FinancialPeriod,
    MarketCapSource,
    MetricUnit,
    PriceBar,
    RiskAssessment,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    SubScore,
    ValuationBasis,
    VolumeBasis,
)
from stock_screener.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session

#: Every variable `Settings` reads. A field added to `Settings` without a line
#: here leaks the developer's real environment into the tests, and the failure
#: looks like a bug in the code rather than in the fixture.
_SETTINGS_ENV_VARS = (
    "ENVIRONMENT",
    "LOG_LEVEL",
    "LOG_JSON",
    "DATABASE_URL",
    "MARKET_DATA_PROVIDER",
    "FUNDAMENTALS_PROVIDER",
    "FIXTURE_PATH",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_TRADING_BASE_URL",
    "ALPACA_DATA_BASE_URL",
    "ALPACA_FEED",
    "FUNDAMENTALS_API_KEY",
    "FUNDAMENTALS_BASE_URL",
    "FUNDAMENTALS_API_ROOT",
    "SEC_USER_AGENT",
    "PROVIDER_MAX_ATTEMPTS",
    "PROVIDER_RETRY_BACKOFF_SECONDS",
    "PROVIDER_MIN_REQUEST_INTERVAL_SECONDS",
    "PROVIDER_BATCH_SIZE",
    "PROVIDER_TIMEOUT_SECONDS",
    "PRICE_HISTORY_DAYS",
    "FUNDAMENTALS_QUARTERS",
    "BENCHMARK_SYMBOL",
    "FMP_ENRICHMENT_LIMIT",
    "RESEARCH_PROVIDER",
    "RESEARCH_API_KEY",
    "RESEARCH_MODEL",
    "RESEARCH_MAX_OUTPUT_TOKENS",
    "RESEARCH_EFFORT",
    "RESEARCH_TIMEOUT_SECONDS",
    "MIN_PRICE",
    "MIN_MARKET_CAP",
    "MIN_AVG_DOLLAR_VOLUME",
    "MIN_TRADING_DAYS",
)

LATEST_SESSION = date(2026, 6, 30)
"""The date every synthetic history ends on, so tests never depend on today."""


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every test from the developer's real environment.

    Moves to an empty working directory so no `.env` file is picked up, and
    clears the variables `Settings` reads. Tests that want a value set it
    explicitly with `monkeypatch.setenv`. The settings cache is cleared too, or a
    value read by an earlier test would survive into this one.
    """
    monkeypatch.chdir(tmp_path)
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Return settings pinned to the test environment."""
    return Settings(environment="test")


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    open_session = factory()
    try:
        yield open_session
    finally:
        open_session.close()
        engine.dispose()


class Make:
    """Builders for synthetic price and fundamental history.

    Reached through the `make` fixture rather than imported:
    `--import-mode=importlib` means a test module cannot `from conftest import`,
    and a fixture is the supported way to share helpers across a directory.
    """

    latest_session = LATEST_SESSION

    @staticmethod
    def bars(sessions: int, close: float = 25.0, volume: float = 500_000.0) -> list[PriceBar]:
        """Build consecutive daily bars ending at `LATEST_SESSION`."""
        return [
            PriceBar(
                date=LATEST_SESSION - timedelta(days=offset),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=volume,
            )
            for offset in reversed(range(sessions))
        ]

    @staticmethod
    def quarters(count: int = 8, revenue: float = 100_000_000.0) -> list[FinancialPeriod]:
        """Build consecutive quarterly periods ending at `LATEST_SESSION`."""
        return [
            FinancialPeriod(
                period_end=LATEST_SESSION - timedelta(days=91 * (count - 1 - index)),
                revenue=revenue,
                gross_profit=revenue * 0.4,
                operating_income=revenue * 0.1,
                operating_cash_flow=revenue * 0.12,
                capital_expenditure=revenue * 0.02,
                cash=revenue * 2,
                total_debt=revenue * 0.5,
                shares_outstanding=41_000_000.0,
                source="test",
            )
            for index in range(count)
        ]


@pytest.fixture
def make() -> type[Make]:
    """Return the builders for synthetic price and fundamental history."""
    return Make


@pytest.fixture
def eligible_profile() -> CompanyProfile:
    """A listing that clears every eligibility threshold."""
    return CompanyProfile(
        ticker="XYZ",
        name="Example Corp",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        market_cap=1_200_000_000.0,
    )


@pytest.fixture
def providers(eligible_profile: CompanyProfile) -> tuple[MockMarketData, MockFundamentals]:
    """Mock providers serving one eligible company with full history."""
    bars = Make.bars(sessions=60)
    periods = Make.quarters()
    return (
        MockMarketData([eligible_profile], {eligible_profile.ticker: bars}),
        MockFundamentals(
            {eligible_profile.ticker: eligible_profile}, {eligible_profile.ticker: periods}
        ),
    )


SCORE_DATE = date(2026, 6, 30)
"""The day every seeded score describes. Fixed so nothing depends on today."""


class Seed:
    """Writes companies, history and scores into a test database."""

    score_date = SCORE_DATE

    @staticmethod
    def company(
        session: Session,
        make: type[Make],
        ticker: str,
        *,
        name: str | None = None,
        quarters: int = 8,
        sessions: int = 60,
        revenue: float = 100_000_000.0,
    ) -> int:
        """Store one company with fundamentals and price history.

        Returns:
            The stored company's primary key.
        """
        profile = CompanyProfile(
            ticker=ticker,
            name=name or f"{ticker} Corporation",
            exchange="NASDAQ",
            sector="Technology",
            industry="Software",
            market_cap=1_200_000_000.0,
        )
        companies = CompanyRepository(session)
        companies.upsert_profile(profile)
        stored = companies.get_by_ticker(ticker)
        assert stored is not None

        FinancialSnapshotRepository(session).upsert_periods(
            stored.id, make.quarters(count=quarters, revenue=revenue)
        )
        PriceHistoryRepository(session).upsert_bars(stored.id, make.bars(sessions=sessions))
        session.flush()
        return stored.id

    @staticmethod
    def score(
        session: Session,
        company_id: int,
        ticker: str,
        *,
        final: float = 80.0,
        coverage: float = 1.0,
        growth: float = 30.0,
        quality: float = 20.0,
        valuation: float = 18.0,
        status: ScoringStatus = ScoringStatus.SCORED,
        score_date: date = SCORE_DATE,
        score_version: str = CURRENT_SCORE_VERSION,
        ranking_state: str = PRELIMINARY,
        market_cap: float | None = 1_200_000_000.0,
        revenue_growth: float | None = 0.35,
        market_cap_source: MarketCapSource = MarketCapSource.CALCULATED,
        liquidity_basis: VolumeBasis = VolumeBasis.PARTIAL,
    ) -> None:
        """Store one score snapshot, breakdown included."""
        ScoreSnapshotRepository(session).upsert_scores(
            [
                ScoreRecord(
                    company_id=company_id,
                    score=Seed.company_score(
                        ticker,
                        final=final,
                        coverage=coverage,
                        growth=growth,
                        quality=quality,
                        valuation=valuation,
                        status=status,
                        score_version=score_version,
                    ),
                    metrics=CompanyMetrics(
                        ticker=ticker,
                        market_cap=market_cap,
                        market_cap_source=market_cap_source,
                        liquidity_basis=liquidity_basis,
                        average_dollar_volume_20d=12_500_000.0,
                        revenue_growth_yoy=revenue_growth,
                        revenue_growth_acceleration=0.12,
                        enterprise_value=1_000_000_000.0,
                    ),
                    ranking_state=ranking_state,
                )
            ],
            score_date,
        )
        session.flush()

    @staticmethod
    def company_score(
        ticker: str,
        *,
        final: float = 80.0,
        coverage: float = 1.0,
        growth: float = 30.0,
        quality: float = 20.0,
        valuation: float = 18.0,
        status: ScoringStatus = ScoringStatus.SCORED,
        score_version: str = CURRENT_SCORE_VERSION,
    ) -> CompanyScore:
        """Build a complete score, including one unavailable sub-score.

        The unavailable sub-score is the point: a brief must carry the metrics
        the score could *not* use, and a fixture with perfect data would never
        exercise that.
        """
        if status is not ScoringStatus.SCORED:
            return CompanyScore(ticker=ticker, score_version=score_version, status=status)

        return CompanyScore(
            ticker=ticker,
            score_version=score_version,
            status=status,
            growth=_component("growth", "revenue_growth", growth, 35.0, coverage),
            quality=_component("quality", "gross_margin", quality, 25.0, coverage),
            valuation=_component("valuation", "valuation_multiple", valuation, 25.0, coverage),
            momentum=_component("momentum", "position_52w", 12.0, 15.0, coverage),
            raw_score=growth + quality + valuation + 12.0,
            risk=RiskAssessment(
                dilution_penalty=0.0,
                runway_penalty=None,
                balance_sheet_penalty=0.0,
                total_penalty=0.0,
                level=RiskLevel.LOW,
                coverage=0.67,
                share_count_growth_yoy=0.02,
                warnings=(ScoreWarning.RUNWAY_NOT_ASSESSED,),
            ),
            final_score=final,
            category=ScoreCategory.STRONG_RESEARCH_CANDIDATE,
            data_coverage=coverage,
            valuation_basis=ValuationBasis.EV_TO_REVENUE,
            warnings=(ScoreWarning.WEIGHT_REDISTRIBUTED,),
        )


def _component(
    name: str, primary: str, score: float, maximum: float, coverage: float
) -> ComponentScore:
    """Build a component with one scored and one unavailable sub-score.

    The scored one is named after a real sub-score the engine emits, so the
    brief's preserve map actually fires in these tests. The unavailable one is
    the point of the fixture: a brief must carry what the score could not judge.
    """
    return ComponentScore(
        name=name,
        status=ComponentStatus.SCORED,
        score=score,
        max_points=maximum,
        coverage=coverage,
        subscores=(
            SubScore(
                name=primary,
                points=score,
                max_points=maximum - 5.0,
                observed=0.35,
                unit=MetricUnit.PERCENT,
            ),
            SubScore(name=f"{name}_secondary", points=None, max_points=5.0, observed=None),
        ),
        redistributed=True,
    )


@pytest.fixture
def seed() -> type[Seed]:
    """Return the builders that put companies and scores in the database."""
    return Seed


@pytest.fixture
def scored_company(session: Session, make: type, seed: type[Seed]) -> str:
    """Store one fully scored company and return its ticker."""
    company_id = seed.company(session, make, "XYZ")
    seed.score(session, company_id, "XYZ")
    return "XYZ"
