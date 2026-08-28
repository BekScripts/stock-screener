"""A fundamentals provider assembled from two sources.

No single free source covers what the screener needs. EDGAR has complete,
authoritative statements for every U.S. filer but publishes no market
capitalisation and no sector, because it is a filings archive rather than a
market-data service. A commercial profile endpoint has both, and is often
available for every symbol even on plans that gate the statements.

This composes them: identity and valuation from one, financials from the other.
It is deliberately a thin delegation rather than a merge strategy — it decides
*which provider answers which question*, and nothing about what the answers
mean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api_clients.errors import ProviderError

if TYPE_CHECKING:
    from api_clients.base import FundamentalsProvider
    from domain import CompanyProfile, Filing, FilingExcerpt, FinancialPeriod

log = structlog.get_logger(__name__)


class CompositeFundamentals:
    """Serves profiles from one provider and statements from another.

    Args:
        profile_source: Supplies market capitalisation, sector and exchange.
        statement_source: Supplies quarterly financials.
        fall_back_to_statement_profile: When the profile source has nothing for
            a symbol, use the statement source's profile instead. That yields a
            company with a name but no market cap, which the eligibility screen
            then excludes as missing required data — visible, rather than the
            company vanishing from the universe without explanation.
    """

    def __init__(
        self,
        profile_source: FundamentalsProvider,
        statement_source: FundamentalsProvider,
        *,
        fall_back_to_statement_profile: bool = True,
    ) -> None:
        self._profiles = profile_source
        self._statements = statement_source
        self._fall_back = fall_back_to_statement_profile

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return one profile combining what each source actually knows.

        **Not "whichever provider answered".** The two sources are authoritative
        about different things, and taking either wholesale throws away what the
        other one knew:

        * The market-data provider knows the size and liquidity of the *listing* —
          market capitalisation, consolidated volume, the currency the share
          trades in, and whether it is a fund. EDGAR is a filings archive and has
          none of that.
        * EDGAR knows what the *filings* say — above all the currency the company
          reports in, read from the XBRL unit key. A market-data vendor's
          currency field for an ADR is the currency the share trades in, which is
          USD for every foreign issuer on a U.S. exchange, so it cannot answer
          this question and must not be allowed to appear to.

        Merging field by field is what keeps TSM's `reporting_currency` as TWD
        while its market capitalisation comes from the vendor that has one. The
        previous behaviour returned the vendor's profile entire, which left the
        reporting currency empty and made a company filing in Taiwan dollars look
        like one filing in dollars.

        Args:
            ticker: The symbol to look up.

        Returns:
            The merged profile, or None when neither source knows the symbol. A
            profile provider that fails is not fatal — the statements are the
            part that cannot be reconstructed, and a company missing only its
            market cap is excluded with a reason rather than lost.
        """
        try:
            market = self._profiles.get_company_profile(ticker)
        except ProviderError:
            log.warning("profile source failed, falling back", ticker=ticker)
            market = None

        filings = self._statements.get_company_profile(ticker) if self._fall_back else None
        if market is None:
            return filings
        if filings is None:
            return market

        return market.model_copy(
            update={
                # The filing's own unit, which only EDGAR has. Everything the FX
                # layer and the eligibility screen do with currency rests on it.
                "reporting_currency": filings.reporting_currency or market.reporting_currency,
                # EDGAR's SIC description is what makes the unsupported-sector
                # rule work when a vendor classifies a bank as "Financials" and
                # nothing more specific.
                "industry": market.industry or filings.industry,
                # Read from the filing's own concepts, which is the only place it
                # can be read from. A market-data vendor sells labels, and labels
                # are exactly what this classification exists to overrule.
                "statement_profile": filings.statement_profile or market.statement_profile,
                "name": market.name or filings.name,
            }
        )

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Return quarterly financials from the statement source.

        Args:
            ticker: The symbol to look up.
            limit: Maximum quarters to return.

        Returns:
            Periods oldest first.

        Raises:
            ProviderError: If the statement source failed. Unlike the profile,
                this is not recoverable from elsewhere.
        """
        return self._statements.get_financial_statements(ticker, limit=limit)

    def get_filings(self, ticker: str, limit: int = 8) -> list[Filing]:
        """Return filing metadata from the statement source.

        Filings belong with the statements for the same reason the statements
        belong where they do: both come from the filer's own submissions, and a
        profile provider that happened to expose an index would be answering a
        question about a different corpus.

        Args:
            ticker: The symbol to look up.
            limit: Maximum filings to return.

        Returns:
            Index entries newest first, or empty when the source has none.

        Raises:
            ProviderError: If the statement source failed.
        """
        return self._statements.get_filings(ticker, limit=limit)

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> list[FilingExcerpt]:
        """Return quotable filing text from the statement source.

        The same source that indexed the filing reads it. Asking one provider
        which documents exist and another what they say would let the two
        disagree about which document an accession names.

        Args:
            ticker: The symbol the filing belongs to.
            filing: The index entry naming the document to read.

        Returns:
            One excerpt per section located, or empty.

        Raises:
            ProviderError: If the statement source failed.
        """
        return self._statements.get_filing_excerpts(ticker, filing)
