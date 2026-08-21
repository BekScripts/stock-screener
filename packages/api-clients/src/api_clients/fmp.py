"""Financial Modeling Prep adapter for quarterly fundamentals.

FMP splits a quarter across three endpoints — income statement, balance sheet,
cash flow — plus a separate profile call. This adapter fetches all three and
merges them on the reporting date, because the metric engine needs revenue, cash
and capex from the *same* quarter to produce a margin that means anything.

Two normalisations happen here and nowhere else:

- **Capex sign.** FMP reports capital expenditure as a negative number. The
  domain model wants a positive outflow, so the absolute value is taken. Getting
  this backwards turns cash burn into cash generation, which is the single most
  dangerous error a screener can make.
- **Debt.** FMP's `totalDebt` is used when present, otherwise short- and
  long-term debt are summed — and both must be present, or the total would
  understate what the company owes.
- **Booleans.** FMP sends `1` and `0`, not `true` and `false`, so flags go
  through `_flag` rather than being read as Python truth values.

Written against FMP's **stable** API and verified against a live AAPL response on
2026-08-13 by `scripts/verify_fundamentals.py`. The older `/api/v3/` paths now
return `403 Legacy Endpoint` for keys issued today, and the stable API differs in
two ways that silently broke the first version of this adapter: the symbol is a
query parameter rather than a path segment, and several profile fields are named
differently (`marketCap`, not `mktCap`; `exchange`, not `exchangeShortName`).
Re-run the verification script after any provider change.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import httpx

from api_clients._http import RateLimiter, RetryPolicy, request_json
from api_clients.errors import ProviderDataError
from domain import CompanyProfile, Filing, FilingExcerpt, FinancialPeriod, normalise_ticker

if TYPE_CHECKING:
    from collections.abc import Mapping

PROVIDER_NAME = "fmp"

DEFAULT_BASE_URL = "https://financialmodelingprep.com"

DEFAULT_API_ROOT = "/stable"
"""Path prefix for FMP's current API. `/api/v3` is retired for new keys."""

_QUARTERLY = "quarter"


class FmpFundamentals:
    """Quarterly fundamentals and company profiles from FMP.

    Args:
        api_key: FMP API key. Sent as a query parameter, which is what FMP
            expects; it is never written to a log or an error message.
        base_url: API host.
        api_root: Path prefix before each endpoint. Defaults to the current
            `/stable` API; override only if FMP moves it again.
        timeout_seconds: Per-request timeout.
        limiter: Minimum spacing between requests. FMP's free tier is metered
            per minute, so a non-zero interval is advisable.
        retry: Retry policy. Defaults apply if omitted.
        client: HTTP client, injected by tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_root: str = DEFAULT_API_ROOT,
        timeout_seconds: float = 30.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_root = "/" + api_root.strip("/")
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"accept": "application/json"},
            timeout=timeout_seconds,
        )
        self._limiter = limiter or RateLimiter(0.0)
        self._retry = retry or RetryPolicy()

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> FmpFundamentals:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on exit."""
        self.close()

    # -- FundamentalsProvider ---------------------------------------------

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return sector, industry and market capitalisation for one company.

        Args:
            ticker: The symbol to look up.

        Returns:
            The profile, or None when FMP does not cover the symbol.

        Raises:
            ProviderError: If the request failed.
        """
        payload = self._get("/profile", {"symbol": normalise_ticker(ticker)})
        if not isinstance(payload, list) or not payload:
            return None

        raw = payload[0]
        if not isinstance(raw, dict):
            raise ProviderDataError("fmp returned an unexpected profile payload")

        return CompanyProfile(
            ticker=str(raw.get("symbol") or ticker),
            name=str(raw.get("companyName") or ticker),
            exchange=_text(raw.get("exchange")),
            sector=_text(raw.get("sector")),
            industry=_text(raw.get("industry")),
            market_cap=_number(raw.get("marketCap")),
            # Consolidated across every venue, which is what the liquidity
            # threshold is calibrated against — unlike a single-exchange feed.
            average_volume=_number(raw.get("averageVolume")),
            # FMP's profile currency is the currency the *share* trades in,
            # which for an ADR is USD whatever the company reports in. It is
            # therefore the quote currency and never the reporting one; the
            # reporting currency comes off the statements themselves.
            quote_currency=_text(raw.get("currency")),
            # FMP sends 1/0, not true/false. An identity check against `False`
            # never matched either, so a delisted company came back active.
            is_active=_flag(raw.get("isActivelyTrading"), default=True),
            # The provider's own classification beats guessing from the name.
            is_fund=_flag(raw.get("isEtf")) or _flag(raw.get("isFund")),
        )

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Return merged quarterly statements, oldest period first.

        Args:
            ticker: The symbol to look up.
            limit: Quarters to request from each endpoint.

        Returns:
            One period per reporting date present in any of the three
            statements. A date missing from one statement leaves those fields
            None rather than dropping the period.

        Raises:
            ProviderError: If a request failed.
        """
        symbol = normalise_ticker(ticker)
        income = self._statement("/income-statement", symbol, limit)
        balance = self._statement("/balance-sheet-statement", symbol, limit)
        cash_flow = self._statement("/cash-flow-statement", symbol, limit)

        period_ends = sorted(set(income) | set(balance) | set(cash_flow))
        return [
            self._merge(
                period_end,
                income.get(period_end),
                balance.get(period_end),
                cash_flow.get(period_end),
            )
            for period_end in period_ends
        ]

    # -- internals ---------------------------------------------------------

    def get_filings(self, ticker: str, limit: int = 8) -> list[Filing]:
        """Return nothing: filings come from EDGAR, which is free and complete.

        FMP does publish a filings endpoint, but spending a metered request on an
        index EDGAR serves for nothing would put a quota between the screener and
        data it can already reach. Returning empty keeps this adapter conformant
        without making it the source of something it should not be.

        Args:
            ticker: Ignored.
            limit: Ignored.

        Returns:
            An empty list, always.
        """
        return []

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> list[FilingExcerpt]:
        """Return nothing: filing documents are read from EDGAR.

        Same reasoning as `get_filings`. The documents are free at the source,
        and a metered request to read one would put a quota between the screener
        and text it can already fetch.

        Args:
            ticker: Ignored.
            filing: Ignored.

        Returns:
            An empty list, always.
        """
        return []

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """Issue a GET against the API root, with the key appended to the query."""
        query = dict(params or {})
        query["apikey"] = self._api_key
        return request_json(
            self._client,
            "GET",
            f"{self._api_root}{endpoint}",
            provider=PROVIDER_NAME,
            params=query,
            limiter=self._limiter,
            retry=self._retry,
        )

    def _statement(self, endpoint: str, symbol: str, limit: int) -> dict[date, Mapping[str, Any]]:
        """Fetch one statement type and index its rows by reporting date."""
        payload = self._get(endpoint, {"symbol": symbol, "period": _QUARTERLY, "limit": limit})
        if not isinstance(payload, list):
            raise ProviderDataError(f"fmp returned a non-list payload for {endpoint}")

        rows: dict[date, Mapping[str, Any]] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            period_end = _parse_date(raw.get("date"))
            if period_end is not None:
                rows[period_end] = raw
        return rows

    @staticmethod
    def _merge(
        period_end: date,
        income: Mapping[str, Any] | None,
        balance: Mapping[str, Any] | None,
        cash_flow: Mapping[str, Any] | None,
    ) -> FinancialPeriod:
        """Combine the three statements for one date into a domain period."""
        income = income or {}
        balance = balance or {}
        cash_flow = cash_flow or {}

        capex = _number(cash_flow.get("capitalExpenditure"))
        gross_profit = _number(income.get("grossProfit"))
        return FinancialPeriod(
            period_end=period_end,
            revenue=_number(income.get("revenue")),
            gross_profit=gross_profit,
            # FMP publishes a single computed gross profit rather than the
            # underlying concept, so the basis is the field name itself.
            gross_profit_basis="grossProfit" if gross_profit is not None else None,
            operating_income=_number(income.get("operatingIncome")),
            operating_cash_flow=_number(cash_flow.get("operatingCashFlow")),
            # Stored as a positive outflow regardless of FMP's sign convention.
            capital_expenditure=None if capex is None else abs(capex),
            free_cash_flow=_number(cash_flow.get("freeCashFlow")),
            cash=_number(balance.get("cashAndShortTermInvestments")),
            total_debt=_total_debt(balance),
            # Weighted-average diluted shares. Dilution compares this field
            # against itself a year earlier, so the concept must not drift to a
            # basic or period-end count in a future adapter.
            shares_outstanding=_number(income.get("weightedAverageShsOutDil")),
            reported_currency=_text(
                income.get("reportedCurrency") or balance.get("reportedCurrency")
            ),
            source=PROVIDER_NAME,
        )


def _total_debt(balance: Mapping[str, Any]) -> float | None:
    """Return total debt, summing the two maturities when no total is given.

    Both maturities are required for the fallback. Summing whichever happened to
    be present would silently understate debt — a company with $2bn of long-term
    borrowing that the provider omitted would report only its short-term balance,
    and `net_cash` would then describe a fortress balance sheet that does not
    exist. An understated debt figure is worse than a missing one, because
    Phase 2 will read it as a reason to *reduce* the risk penalty.
    """
    total = _number(balance.get("totalDebt"))
    if total is not None:
        return total

    short_term = _number(balance.get("shortTermDebt"))
    long_term = _number(balance.get("longTermDebt"))
    if short_term is None or long_term is None:
        return None
    return short_term + long_term


def _flag(value: object, *, default: bool = False) -> bool:
    """Interpret an FMP boolean, which arrives as 1/0 rather than true/false.

    Args:
        value: The raw field. None means the provider omitted it.
        default: What an omitted field means. `isActivelyTrading` defaults to
            True (assume tradable unless told otherwise); classification flags
            default to False.

    Returns:
        The flag as a Python bool.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return default


def _text(value: object) -> str | None:
    """Return a stripped string, or None when absent or blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object) -> float | None:
    """Return a float, or None when the value is absent or not numeric.

    Returns None rather than 0.0 for an unparseable value: a field FMP omitted is
    unknown, and pretending it is zero would put a fabricated figure into a
    margin.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_date(value: object) -> date | None:
    """Parse an ISO date string, returning None when it is absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
