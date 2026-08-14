"""The contracts every provider adapter implements.

These protocols are the seam that keeps one vendor's decisions out of the rest of
the system. Everything above this line — ingestion, the metric engine, the
scanner — depends on the protocol; nothing depends on Alpaca or FMP. Swapping a
data vendor should be a change to one adapter and one setting.

The return types are `domain` models, never vendor payloads or dictionaries. An
adapter that returns raw JSON has moved the normalisation problem rather than
solved it, and the vendor's field names leak into the metric engine, which is
exactly the failure mode Phase 1 calls out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from domain import CompanyProfile, FinancialPeriod, PriceBar


@runtime_checkable
class MarketDataProvider(Protocol):
    """A source of tradable securities and their daily price history."""

    def get_stock_universe(self) -> list[CompanyProfile]:
        """Return every security the provider considers active and tradable.

        Market-data vendors generally know a security's venue and status but not
        its fundamentals, so `sector`, `industry` and `market_cap` are usually
        None here and filled in later from the fundamentals provider.

        Returns:
            One profile per listing, unfiltered — applying the universe rules is
            the caller's job.

        Raises:
            ProviderError: If the universe could not be retrieved.
        """
        ...

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        """Return daily bars for one ticker over an inclusive date range.

        Args:
            ticker: The symbol to fetch.
            start: First session to include.
            end: Last session to include.

        Returns:
            Bars ordered oldest first. An empty list means the provider has no
            data for the range, which is not an error.

        Raises:
            ProviderError: If the request failed.
        """
        ...

    def get_daily_prices_batch(
        self, tickers: Sequence[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        """Return daily bars for several tickers in as few requests as possible.

        Exists because fetching four thousand symbols one at a time is the
        fastest way to exhaust a rate limit. Adapters whose vendor has no batch
        endpoint may loop internally.

        Args:
            tickers: Symbols to fetch.
            start: First session to include.
            end: Last session to include.

        Returns:
            A mapping of ticker to bars, ordered oldest first. Tickers the
            provider returned nothing for are absent from the mapping.

        Raises:
            ProviderError: If the request failed.
        """
        ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    """A source of company profiles and reported financial statements."""

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return identity and classification data for one company.

        Args:
            ticker: The symbol to look up.

        Returns:
            The profile, or None when the provider does not cover this symbol.
            A symbol the vendor has never heard of is a gap in coverage, not a
            failure, so it does not raise.

        Raises:
            ProviderError: If the request failed.
        """
        ...

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Return normalised quarterly financials, most recent periods included.

        Income statement, balance sheet and cash flow are merged into one period
        per reporting date, because the metric engine needs revenue, cash and
        capex for the same quarter to compute a margin.

        Args:
            ticker: The symbol to look up.
            limit: Maximum number of quarters to request. Sixteen are needed for
                a three-year CAGR.

        Returns:
            Periods ordered oldest first. Empty when the provider has no
            statements for the symbol.

        Raises:
            ProviderError: If the request failed.
        """
        ...
