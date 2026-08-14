"""Tests for the fixture-backed providers.

These matter more than they look: the mock providers are what the integration
tests and a keyless clone run against, so a mock that behaves unlike a real
adapter would hide bugs rather than reveal them.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from api_clients import (
    MockFundamentals,
    MockMarketData,
    ProviderDataError,
    load_fixture_providers,
)
from domain import CompanyProfile, FinancialPeriod, PriceBar

if TYPE_CHECKING:
    from pathlib import Path

FIXTURE = {
    "companies": [
        {
            "ticker": "XYZ",
            "name": "Example Corp",
            "exchange": "NASDAQ",
            "sector": "Technology",
            "market_cap": 1_200_000_000,
            "bars": [
                {
                    "date": "2026-01-02",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 1_200_000,
                }
            ],
            "periods": [{"period_end": "2025-12-31", "revenue": 100_000_000}],
        }
    ]
}


@pytest.mark.unit
def test_the_universe_comes_back_as_domain_profiles() -> None:
    profile = CompanyProfile(ticker="AAA", name="Alpha Inc", exchange="NYSE")

    universe = MockMarketData([profile]).get_stock_universe()

    assert universe == [profile]


@pytest.mark.unit
def test_bars_are_clipped_to_the_requested_range() -> None:
    bars = [
        PriceBar(date=date(2026, 1, 1), open=1, high=1, low=1, close=1, volume=1),
        PriceBar(date=date(2026, 6, 1), open=2, high=2, low=2, close=2, volume=1),
        PriceBar(date=date(2026, 12, 1), open=3, high=3, low=3, close=3, volume=1),
    ]
    provider = MockMarketData(bars={"XYZ": bars})

    result = provider.get_daily_prices("XYZ", date(2026, 3, 1), date(2026, 9, 1))

    assert [bar.date for bar in result] == [date(2026, 6, 1)]


@pytest.mark.unit
def test_a_ticker_with_no_configured_bars_returns_an_empty_list() -> None:
    assert MockMarketData().get_daily_prices("NOPE", date(2026, 1, 1), date(2026, 2, 1)) == []


@pytest.mark.unit
def test_a_configured_failure_raises_so_the_error_path_can_be_exercised() -> None:
    provider = MockMarketData(failing_tickers=["BAD"])

    with pytest.raises(ProviderDataError, match="BAD"):
        provider.get_daily_prices("bad", date(2026, 1, 1), date(2026, 2, 1))


@pytest.mark.unit
def test_statements_come_back_oldest_first_and_capped_at_the_limit() -> None:
    periods = [FinancialPeriod(period_end=date(2026, month, 28)) for month in (9, 3, 6)]
    provider = MockFundamentals(statements={"XYZ": periods})

    result = provider.get_financial_statements("XYZ", limit=2)

    assert [p.period_end for p in result] == [date(2026, 6, 28), date(2026, 9, 28)]


@pytest.mark.unit
def test_an_uncovered_ticker_has_no_profile() -> None:
    assert MockFundamentals().get_company_profile("NOPE") is None


@pytest.mark.unit
def test_lookups_are_case_insensitive() -> None:
    profile = CompanyProfile(ticker="XYZ", name="Example Corp")
    provider = MockFundamentals({"XYZ": profile})

    assert provider.get_company_profile("xyz") == profile


@pytest.mark.unit
def test_a_fixture_file_builds_both_providers(tmp_path: Path) -> None:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")

    market_data, fundamentals = load_fixture_providers(path)

    assert [p.ticker for p in market_data.get_stock_universe()] == ["XYZ"]
    assert len(market_data.get_daily_prices("XYZ", date(2026, 1, 1), date(2026, 2, 1))) == 1
    assert len(fundamentals.get_financial_statements("XYZ")) == 1


@pytest.mark.unit
def test_fixture_periods_are_tagged_with_their_source(tmp_path: Path) -> None:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")

    _, fundamentals = load_fixture_providers(path)

    assert fundamentals.get_financial_statements("XYZ")[0].source == "mock"


@pytest.mark.unit
def test_a_missing_fixture_file_raises_a_provider_error(tmp_path: Path) -> None:
    with pytest.raises(ProviderDataError, match="could not read fixture file"):
        load_fixture_providers(tmp_path / "absent.json")


@pytest.mark.unit
def test_a_fixture_of_the_wrong_shape_raises_a_provider_error(tmp_path: Path) -> None:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"wrong": []}), encoding="utf-8")

    with pytest.raises(ProviderDataError, match="'companies' list"):
        load_fixture_providers(path)


@pytest.mark.unit
def test_a_fixture_with_an_invalid_field_fails_loudly(tmp_path: Path) -> None:
    # A bad number must not reach a metric calculation as a silent default.
    path = tmp_path / "universe.json"
    path.write_text(
        json.dumps({"companies": [{"ticker": "X", "name": "X", "market_cap": "lots"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="market_cap"):
        load_fixture_providers(path)
