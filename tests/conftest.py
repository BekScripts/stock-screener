"""Shared pytest fixtures for the root application test suite."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

from api_clients import MockFundamentals, MockMarketData
from data_access import build_session_factory, create_all, create_engine_from_url
from domain import CompanyProfile, FinancialPeriod, PriceBar
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
