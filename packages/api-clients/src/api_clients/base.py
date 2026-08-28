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

    from domain import (
        CompanyProfile,
        ExternalSearchResult,
        Filing,
        FilingExcerpt,
        FinancialPeriod,
        FxConversion,
        PriceBar,
    )


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

    def get_filings(self, ticker: str, limit: int = 8) -> list[Filing]:
        """Return recent filing index entries, newest first.

        Metadata only — form, dates, accession number and location. No document
        is fetched, so this is cheap enough to run beside a fundamentals refresh.

        Every provider implements this, and a provider that has no filings index
        returns an empty list rather than raising. Absence of filings is a fact
        about a source's coverage, not a failure, and a research brief that
        carries none is still a valid brief.

        Args:
            ticker: The symbol to look up.
            limit: Maximum filings to return, most recent kept.

        Returns:
            Index entries newest first, or an empty list.

        Raises:
            ProviderError: If the request failed.
        """
        ...

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> list[FilingExcerpt]:
        """Return quotable sections from one filing's document.

        The expensive counterpart to `get_filings`: one request per document,
        so callers read a handful of filings rather than a market's worth.
        Extraction is deterministic — headings located by pattern, text taken
        verbatim — and a provider that cannot read documents returns an empty
        list rather than raising.

        A form this provider does not parse, or a document whose sections cannot
        be located confidently, yields nothing. That is the intended outcome: a
        research section resting on filing text then answers `UNKNOWN`, which is
        preferable to quoting a table of contents as a business description.

        Args:
            ticker: The symbol the filing belongs to.
            filing: The index entry naming the document to read.

        Returns:
            One excerpt per section located, or an empty list.

        Raises:
            ProviderError: If the document could not be fetched.
        """
        ...


@runtime_checkable
class ExternalResearchProvider(Protocol):
    """A source of current, externally published material about a company.

    The narrowest interface that supports Phase 6C: one search, results back.
    There is deliberately no crawl, no queue, no browser and no content store —
    deep research needs evidence collection, not a search platform, and every
    capability added here is one the collector would then have to have an opinion
    about.

    Implementations translate a vendor's response into `ExternalSearchResult` and
    stop there. They assign no tier and reject nothing on quality grounds: which
    publishers are trustworthy and which results are duplicates are collection
    policy, and an adapter that decided either would make the policy untestable
    without a transport.
    """

    def search(
        self, query: str, *, since: date | None = None, limit: int = 20
    ) -> list[ExternalSearchResult]:
        """Return current published material matching a query.

        Args:
            query: What to search for, normally a ticker and company name plus a
                topic. Composed by the caller; adapters do not rewrite it.
            since: Earliest publication date to include. A vendor without a date
                filter may return older material anyway, which the collector
                filters — so this is a request, not a guarantee.
            limit: Most results to return. A vendor may return fewer.

        Returns:
            The results, in whatever order the vendor ranked them. Order carries
            no meaning downstream: the collector sorts and the fingerprint is
            order-insensitive, precisely so a vendor reshuffling its ranking is
            not mistaken for new evidence.

        Raises:
            ProviderError: If the search could not be performed.
        """
        ...


@runtime_checkable
class FxProvider(Protocol):
    """A source of dated exchange rates.

    Deliberately one method. The screener converts one number — a market
    capitalisation — into the currency a company files in, and everything else
    an FX vendor sells is a different product.
    """

    def get_rate(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        """Return the rate converting `base` into `quote` around a date.

        Args:
            base: Currency to convert from, e.g. the currency a security trades
                in.
            quote: Currency to convert to, e.g. the currency a company files in.
            as_of: The date wanted. An implementation may answer with an earlier
                date when no rate was fixed on this one, and must say which date
                it used rather than presenting it as the date requested.

        Returns:
            The conversion, or None when this source does not publish the pair.
            None is an ordinary answer, not a failure: no source covers every
            currency, and a caller that cannot convert must produce a missing
            ratio rather than a mixed-currency one.

        Raises:
            ProviderError: If the request itself failed.
        """
        ...
