"""In-memory providers backed by fixture data.

These exist for two reasons. They let the whole pipeline — ingest, calculate,
screen, report — run end to end on a clone with no API keys, and they give the
tests a provider that behaves like the real thing without a network.

Fixture data is *passed in*, never read at import time. A module that opens a
file when imported breaks in every context that imports it for a different
reason, and makes the tests depend on a path.

These are fakes of the transport, not of the domain: they return real
`CompanyProfile`, `PriceBar` and `FinancialPeriod` objects built by the same
validation everything else uses. A fixture with a malformed field fails here,
loudly, rather than propagating a nonsense number into a metric.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from api_clients.errors import ProviderDataError
from domain import CompanyProfile, FinancialPeriod, PriceBar

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date

PROVIDER_NAME = "mock"


class MockMarketData:
    """A `MarketDataProvider` serving pre-loaded profiles and bars.

    Args:
        profiles: The universe to return.
        bars: Daily history per ticker. Tickers absent from the mapping return
            no bars, mirroring a provider with partial coverage.
        failing_tickers: Symbols that raise `ProviderDataError` when their bars
            are requested. Used to exercise the "one bad ticker must not kill the
            scan" path.
    """

    def __init__(
        self,
        profiles: Iterable[CompanyProfile] = (),
        bars: Mapping[str, Sequence[PriceBar]] | None = None,
        *,
        failing_tickers: Iterable[str] = (),
    ) -> None:
        self._profiles = list(profiles)
        self._bars = {ticker: list(series) for ticker, series in (bars or {}).items()}
        self._failing = {ticker.upper() for ticker in failing_tickers}

    def get_stock_universe(self) -> list[CompanyProfile]:
        """Return the configured universe."""
        return list(self._profiles)

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        """Return the configured bars for one ticker, clipped to the date range.

        Args:
            ticker: The symbol to fetch.
            start: First session to include.
            end: Last session to include.

        Returns:
            Matching bars, oldest first.

        Raises:
            ProviderDataError: If the ticker was configured to fail.
        """
        symbol = ticker.upper()
        if symbol in self._failing:
            raise ProviderDataError(f"mock provider is configured to fail for {symbol}")

        series = self._bars.get(symbol, [])
        return sorted(
            (bar for bar in series if start <= bar.date <= end),
            key=lambda bar: bar.date,
        )

    def get_daily_prices_batch(
        self, tickers: Sequence[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        """Return bars for several tickers.

        A failing ticker raises rather than being silently omitted, matching a
        real batch endpoint that rejects the whole request.

        Args:
            tickers: Symbols to fetch.
            start: First session to include.
            end: Last session to include.

        Returns:
            A mapping of ticker to bars, excluding tickers with no data.

        Raises:
            ProviderDataError: If any requested ticker was configured to fail.
        """
        results: dict[str, list[PriceBar]] = {}
        for ticker in tickers:
            series = self.get_daily_prices(ticker, start, end)
            if series:
                results[ticker.upper()] = series
        return results


class MockFundamentals:
    """A `FundamentalsProvider` serving pre-loaded profiles and statements.

    Args:
        profiles: Profiles by ticker. A ticker absent from the mapping returns
            None, mirroring a vendor that does not cover it.
        statements: Quarterly history by ticker.
        failing_tickers: Symbols that raise `ProviderDataError` on any lookup.
    """

    def __init__(
        self,
        profiles: Mapping[str, CompanyProfile] | None = None,
        statements: Mapping[str, Sequence[FinancialPeriod]] | None = None,
        *,
        failing_tickers: Iterable[str] = (),
    ) -> None:
        self._profiles = {ticker.upper(): profile for ticker, profile in (profiles or {}).items()}
        self._statements = {
            ticker.upper(): list(periods) for ticker, periods in (statements or {}).items()
        }
        self._failing = {ticker.upper() for ticker in failing_tickers}

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return the configured profile, or None when the ticker is uncovered.

        Raises:
            ProviderDataError: If the ticker was configured to fail.
        """
        symbol = self._check(ticker)
        return self._profiles.get(symbol)

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Return the configured statements, oldest first, capped at `limit`.

        Raises:
            ProviderDataError: If the ticker was configured to fail.
        """
        symbol = self._check(ticker)
        periods = sorted(self._statements.get(symbol, []), key=lambda p: p.period_end)
        return periods[-limit:] if limit > 0 else periods

    def _check(self, ticker: str) -> str:
        """Upper-case the ticker, raising if it is configured to fail."""
        symbol = ticker.upper()
        if symbol in self._failing:
            raise ProviderDataError(f"mock provider is configured to fail for {symbol}")
        return symbol


def load_fixture_providers(path: Path | str) -> tuple[MockMarketData, MockFundamentals]:
    """Build both mock providers from a JSON fixture file.

    The file holds one object per ticker:

    ```json
    {
      "companies": [
        {
          "ticker": "XYZ",
          "name": "Example Corp",
          "exchange": "NASDAQ",
          "sector": "Technology",
          "market_cap": 1200000000,
          "bars": [{"date": "2026-01-02", "open": 10, "high": 11,
                    "low": 9, "close": 10.5, "volume": 1200000}],
          "periods": [{"period_end": "2025-12-31", "revenue": 100000000}]
        }
      ]
    }
    ```

    Args:
        path: Location of the fixture file.

    Returns:
        A market-data provider and a fundamentals provider sharing the data.

    Raises:
        ProviderDataError: If the file is not valid JSON in the expected shape.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderDataError(f"could not read fixture file: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), list):
        raise ProviderDataError("fixture file must be an object with a 'companies' list")

    profiles: list[CompanyProfile] = []
    bars: dict[str, list[PriceBar]] = {}
    statements: dict[str, list[FinancialPeriod]] = {}

    for entry in payload["companies"]:
        if not isinstance(entry, dict):
            raise ProviderDataError("each fixture company must be an object")
        profile = _fixture_profile(entry)
        profiles.append(profile)
        bars[profile.ticker] = [PriceBar(**bar) for bar in entry.get("bars", [])]
        statements[profile.ticker] = [
            FinancialPeriod(source=PROVIDER_NAME, **period) for period in entry.get("periods", [])
        ]

    return (
        MockMarketData(profiles, bars),
        MockFundamentals({p.ticker: p for p in profiles}, statements),
    )


def _fixture_profile(entry: dict[str, Any]) -> CompanyProfile:
    """Build a profile from a fixture entry, ignoring its bars and periods."""
    fields = {key: value for key, value in entry.items() if key not in {"bars", "periods"}}
    return CompanyProfile(**fields)
