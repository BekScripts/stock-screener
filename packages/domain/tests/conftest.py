"""Builders for the synthetic companies the metric tests are written against.

Every number in these tests is hand-workable. `Make.quarters` lays periods on a
strict 91-day grid ending at `LATEST_QUARTER`, so "four quarters back" is an
exact date and an expected value can be worked out on paper rather than by
running the code and pasting whatever it printed.

The builders are reached through the `make` fixture rather than imported.
`--import-mode=importlib` means a test module cannot `from conftest import ...`,
and a fixture is the supported way to share helpers across files in the same
directory.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from domain import (
    BenchmarkReturns,
    CompanyMetrics,
    CompanyProfile,
    FinancialPeriod,
    PriceBar,
    VolumeBasis,
)

LATEST_QUARTER = date(2026, 6, 30)
LATEST_SESSION = date(2026, 6, 30)
QUARTER_DAYS = 91


class Make:
    """Factories for periods and price bars on a predictable date grid."""

    latest_quarter = LATEST_QUARTER
    latest_session = LATEST_SESSION

    @staticmethod
    def quarter_end(quarters_back: int) -> date:
        """Return the period end `quarters_back` quarters before the latest."""
        return LATEST_QUARTER - timedelta(days=QUARTER_DAYS * quarters_back)

    @staticmethod
    def period(quarters_back: int, **fields: float | None) -> FinancialPeriod:
        """Build one period at an offset, setting only the fields under test."""
        return FinancialPeriod(period_end=Make.quarter_end(quarters_back), **fields)

    @staticmethod
    def quarters(revenues: list[float | None], **constant: float | None) -> list[FinancialPeriod]:
        """Build consecutive quarters from a list of revenues, oldest first.

        Args:
            revenues: Revenue per quarter. The last entry is the latest quarter.
            constant: Fields applied identically to every period.

        Returns:
            Periods on a 91-day grid ending at `LATEST_QUARTER`.
        """
        total = len(revenues)
        return [
            FinancialPeriod(
                period_end=Make.quarter_end(total - 1 - index), revenue=revenue, **constant
            )
            for index, revenue in enumerate(revenues)
        ]

    @staticmethod
    def series(field: str, values: list[float | None]) -> list[FinancialPeriod]:
        """Build consecutive quarters carrying one named field, oldest first."""
        total = len(values)
        return [
            FinancialPeriod(period_end=Make.quarter_end(total - 1 - index), **{field: value})
            for index, value in enumerate(values)
        ]

    @staticmethod
    def bar(days_back: int, close: float, volume: float = 1_000.0, **overrides: float) -> PriceBar:
        """Build one daily bar `days_back` calendar days before the latest.

        High and low default to a symmetric 1% band around the close, so
        52-week high and low tests have predictable values.
        """
        return PriceBar(
            date=LATEST_SESSION - timedelta(days=days_back),
            open=overrides.get("open", close),
            high=overrides.get("high", close * 1.01),
            low=overrides.get("low", close * 0.99),
            close=close,
            volume=volume,
        )

    @staticmethod
    def flat_series(sessions: int, close: float = 10.0, volume: float = 1_000.0) -> list[PriceBar]:
        """Build consecutive daily bars all at the same price and volume."""
        return [Make.bar(days_back, close, volume) for days_back in reversed(range(sessions))]

    @staticmethod
    def profile(**overrides: Any) -> CompanyProfile:
        """Build a scoreable company profile, overriding only what matters."""
        defaults: dict[str, Any] = {
            "ticker": "XYZ",
            "name": "Example Corp",
            "exchange": "NASDAQ",
            "sector": "Technology",
            "industry": "Software - Application",
            "market_cap": 1_000_000_000.0,
        }
        return CompanyProfile(**{**defaults, **overrides})

    @staticmethod
    def metrics(**overrides: Any) -> CompanyMetrics:
        """Build metrics with every field available, then apply overrides.

        The defaults are a healthy, unremarkable company. A scoring test sets
        the one or two metrics it is about and leaves the rest alone, so a
        failure points at the rule under test rather than at the fixture.
        """
        defaults: dict[str, Any] = {
            "ticker": "XYZ",
            "price": 25.0,
            "market_cap": 1_000_000_000.0,
            "average_dollar_volume_20d": 5_000_000.0,
            "liquidity_basis": VolumeBasis.CONSOLIDATED,
            "trading_days_used": 20,
            "revenue_growth_yoy": 0.30,
            "previous_revenue_growth_yoy": 0.20,
            "revenue_growth_acceleration": 0.10,
            "recent_revenue_growth_yoy": (0.30, 0.20, 0.15, 0.10),
            "ttm_revenue": 400_000_000.0,
            "ttm_revenue_growth": 0.25,
            "revenue_cagr_3y": 0.20,
            "gross_margin": 0.55,
            "gross_margin_change": 0.0,
            "gross_profit_growth_yoy": 0.30,
            "operating_margin": 0.10,
            "operating_margin_change": 0.0,
            "fcf_margin": 0.12,
            "fcf_margin_change": 0.0,
            "ttm_free_cash_flow": 48_000_000.0,
            "cash": 200_000_000.0,
            "debt": 50_000_000.0,
            "net_cash": 150_000_000.0,
            "enterprise_value": 850_000_000.0,
            "share_count_growth_yoy": 0.01,
            "return_6m": 0.20,
            "return_12m": 0.35,
            "high_52w": 30.0,
            "low_52w": 15.0,
            "distance_from_52w_high": -0.05,
        }
        return CompanyMetrics(**{**defaults, **overrides})

    @staticmethod
    def benchmark(
        return_6m: float | None = 0.05, return_12m: float | None = 0.10
    ) -> BenchmarkReturns:
        """Build benchmark returns to measure relative strength against."""
        return BenchmarkReturns(symbol="SPY", return_6m=return_6m, return_12m=return_12m)


@pytest.fixture
def make() -> type[Make]:
    """Return the builders for synthetic periods and price bars."""
    return Make
