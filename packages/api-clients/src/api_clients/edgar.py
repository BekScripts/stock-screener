"""SEC EDGAR adapter: quarterly fundamentals from XBRL company facts.

EDGAR is the source the commercial vendors resell. It is free, needs no key, and
covers every U.S. filer down to the smallest micro cap — which is the coverage a
screener looking for small companies actually needs.

The cost is that XBRL is filings data, not a tidy financial API, and three of its
properties will produce wrong numbers if ignored:

**Tags vary between filers, and over time.** Apple reports revenue under
`RevenueFromContractWithCustomerExcludingAssessedTax`; another company uses
`Revenues` or `SalesRevenueNet`. Taxonomies also retire tags, so one company's
history is often split across two of them. Each concept therefore has a fallback
chain and the whole chain is **merged**, with the more specific tag winning for
any period both cover.

**The same period is reported many times.** A quarter appears in its own 10-Q,
again in the next year's comparatives, and again in each 10-K — Apple has four
facts for one 2018 quarter. Later filings supersede earlier ones, so facts are
de-duplicated by period keeping the most recently *filed* value, which is also
what makes restatements land correctly.

**Cash-flow figures are cumulative, and Q4 is never filed at all.** Within a
fiscal year a filer reports three months, then six, then nine, then the 10-K's
twelve — every fact sharing the year's start date. Consecutive rungs of that
ladder are differenced to recover discrete quarters, and the same operation
produces the fourth quarter that no filer states directly. Skip this and three
quarters in four have no cash flow, while every trailing-twelve-month figure —
which needs four *consecutive* quarters — comes back `None`.

Differencing only applies to concepts that accumulate. A weighted-average share
count is an average, not a sum, and subtracting one from another yields negative
share counts.

Balance-sheet concepts are *instant* facts — a value at a date, with no start —
while income and cash-flow concepts are *durations*. They are collected
separately and joined on the period end.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from api_clients._http import RateLimiter, RetryPolicy, request_json, request_text
from api_clients.errors import ProviderDataError, ProviderError
from api_clients.filing_text import clean_filing_text, extract_sections
from domain import CompanyProfile, Filing, FilingExcerpt, FinancialPeriod, normalise_ticker

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

PROVIDER_NAME = "sec-edgar"

DEFAULT_DATA_URL = "https://data.sec.gov"
DEFAULT_WWW_URL = "https://www.sec.gov"

#: SEC asks for no more than ten requests a second, and blocks clients that
#: ignore it. One tenth of a second between requests keeps us inside that.
DEFAULT_MIN_INTERVAL_SECONDS = 0.11

#: A duration this long is one quarter. Fiscal quarters run 13 weeks and drift a
#: few days either way; the window excludes six-month and nine-month cumulative
#: figures, which filers report alongside the discrete ones.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100

_YEAR_MIN_DAYS = 340
_YEAR_MAX_DAYS = 380

#: Quarters that must already be known inside a fiscal year before its final
#: quarter can be derived from the annual figure.
_QUARTERS_BEFORE_YEAR_END = 3

#: Concept fallback chains, **most specific first**. Every tag in a chain is
#: merged, with earlier entries overriding later ones for periods both cover, so
#: ordering decides precedence rather than which tag is consulted at all.
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
_GROSS_PROFIT_TAGS = ("GrossProfit",)

#: Cost of revenue, most preferred **last** — `_merge_chain` applies the chain in
#: reverse so that a tag earlier in this tuple overwrites a later one for any
#: period both cover.
#:
#: Only concepts that mean *the cost of producing what was sold* belong here.
#: Broad concepts such as `CostsAndExpenses` (total operating cost, including
#: selling, general, administrative and often impairments) are deliberately
#: absent: subtracting one from revenue yields something close to operating
#: income, and calling that a gross profit would produce a margin that is wrong
#: by the entire operating expense base.
#:
#: | Concept | Means | Basis |
#: | --- | --- | --- |
#: | `CostOfGoodsAndServicesSold` | Goods and services sold | includes D&A |
#: | `CostOfRevenue` | Total cost of revenue | includes D&A |
#: | `CostOfGoodsSold` | Goods only | includes D&A |
#: | `CostOfServices` | Services only; the service-company analogue of COGS | includes D&A |
#: | `DirectOperatingCosts` | Costs directly attributable to revenue, used by
#:   shipping, energy and media filers | includes D&A |
#: | `...ExcludingDepreciationDepletionAndAmortization` | The same cost with
#:   D&A stripped out | **excludes D&A** |
#:
#: The excluding-D&A variants are ranked last on purpose. A gross profit derived
#: from one is **higher** than the GAAP figure by the depreciation charged to
#: production, so they are a fallback for filers that publish nothing else, and
#: the period records which concept produced it.
_COST_OF_REVENUE_TAGS = (
    "CostOfGoodsAndServicesSold",
    "CostOfRevenue",
    "CostOfGoodsSold",
    "CostOfServices",
    "DirectOperatingCosts",
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
    "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
)

#: Concepts whose gross profit excludes depreciation, depletion and
#: amortisation. Recorded on the period so a consumer can tell the two bases
#: apart rather than comparing them as though they were the same measure.
COST_BASIS_EXCLUDES_DA = frozenset(
    {
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
        "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
    }
)

GROSS_PROFIT_REPORTED = "GrossProfit"
"""Basis recorded when the filer tagged gross profit directly."""

_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
_OPERATING_CASH_FLOW_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
_SHARES_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)

#: Point-in-time common shares outstanding, **not** the weighted average above.
#:
#: `dei:EntityCommonStockSharesOutstanding` is the cover-page count every 10-Q
#: and 10-K carries, stated as of a date shortly before filing, which makes it
#: the closest thing EDGAR has to a current share count. The us-gaap balance
#: sheet concepts are the fallback for filers whose cover-page tag is missing.
#:
#: The distinction from `_SHARES_TAGS` is load-bearing. A weighted average is an
#: income-statement figure covering a period; multiplying it by today's price
#: would value the company on a share count that was never outstanding on any
#: single day. Dilution needs the average; market capitalisation needs this.
_COMMON_SHARES_DEI_TAGS = ("EntityCommonStockSharesOutstanding",)
_COMMON_SHARES_GAAP_TAGS = (
    "CommonStockSharesOutstanding",
    "CommonStockSharesIssued",
)

_CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
_SHORT_TERM_INVESTMENTS_TAGS = (
    "ShortTermInvestments",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "MarketableSecuritiesCurrent",
)
#: Interest-bearing borrowings and finance leases. Operating-lease liabilities
#: are deliberately absent: they are not borrowings, and the schedules filers tag
#: (`LesseeOperatingLeaseLiabilityPaymentsDue*`) are future minimum payments
#: rather than a balance-sheet amount.
_LONG_TERM_DEBT_TAGS = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "ConvertibleDebtNoncurrent",
    "ConvertibleNotesPayable",
    "NotesPayableRelatedPartiesNoncurrent",
    "FinanceLeaseLiabilityNoncurrent",
)
_SHORT_TERM_DEBT_TAGS = (
    "LongTermDebtCurrent",
    "DebtCurrent",
    "ShortTermBorrowings",
    "ConvertibleDebtCurrent",
    "ConvertibleNotesPayableCurrent",
    "NotesPayableCurrent",
    "FinanceLeaseLiabilityCurrent",
)

_USD = "USD"
_SHARES = "shares"

#: How far after a quarter end a cover-page share count may be dated, and how
#: far before. A 10-Q lands within about six weeks of the quarter it reports and
#: states its share count as of a date near filing; ninety days stops one
#: quarter's count being read as the next one's.
FILING_FORMS = frozenset({"10-K", "10-Q", "8-K"})
"""Forms a research brief may cite.

The recent-filings index is dominated by ownership reports and prospectus
supplements. These three are the ones that carry the business: the annual and
quarterly reports, and the current report a company files when something happens
between them.
"""

DEFAULT_FILING_LIMIT = 8
"""Filings returned per company unless asked for more."""

_COVER_PAGE_WINDOW_DAYS = 90
_COVER_PAGE_BACKSTOP_DAYS = 3


class SecEdgarFundamentals:
    """Quarterly fundamentals from the SEC's XBRL company-facts API.

    One request per company returns every fact the filer has ever tagged, which
    is why this needs no per-statement calls the way a commercial API does.

    Profiles carry identity only. EDGAR has no market capitalisation and no
    sector, so those stay `None` — pair this with a provider that supplies them,
    or the eligibility screen will exclude everything for missing market cap.

    Args:
        user_agent: Contact string sent on every request. **The SEC requires
            this** and blocks requests without a real one; it should name the
            application and an email address.
        data_base_url: Host serving the XBRL API.
        www_base_url: Host serving the ticker-to-CIK map.
        timeout_seconds: Per-request timeout. Company-facts documents run to
            several megabytes, so this is generous.
        limiter: Request spacing. Defaults to the SEC's ten-per-second ceiling.
        retry: Retry policy. Defaults apply if omitted.
        client: HTTP client, injected by tests.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        data_base_url: str = DEFAULT_DATA_URL,
        www_base_url: str = DEFAULT_WWW_URL,
        timeout_seconds: float = 60.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("the SEC requires a User-Agent naming the app and a contact email")

        self._data_base_url = data_base_url.rstrip("/")
        self._www_base_url = www_base_url.rstrip("/")
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter(DEFAULT_MIN_INTERVAL_SECONDS)
        self._retry = retry or RetryPolicy()
        self._cik_by_ticker: dict[str, int] | None = None
        self._facts_cache: tuple[str, dict[str, Any]] | None = None
        self._submissions_cache: tuple[str, dict[str, Any]] | None = None

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> SecEdgarFundamentals:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on exit."""
        self.close()

    # -- FundamentalsProvider ---------------------------------------------

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return the registrant's name for one ticker.

        EDGAR publishes filings, not market data, so there is no market
        capitalisation, sector or industry here. All three stay `None` for a
        provider that has them to fill in.

        Args:
            ticker: The symbol to look up.

        Returns:
            A profile carrying identity only, or None when the SEC does not map
            this ticker to a filer.

        Raises:
            ProviderError: If a request failed.
        """
        symbol = normalise_ticker(ticker)
        cik = self._cik_for(symbol)
        if cik is None:
            return None

        facts = self._company_facts(cik)
        return CompanyProfile(
            ticker=symbol,
            name=str(facts.get("entityName") or symbol),
            # The registrant's own SIC description, e.g. "State Commercial
            # Banks". Not a market-data sector taxonomy, but it is what makes the
            # unsupported-sector rule work without a commercial provider — and it
            # comes free with a request the adapter already needs.
            industry=self._industry_for(cik),
            # Filings are in USD unless a filer says otherwise; the units key on
            # each fact is checked when the figures themselves are read.
            currency=_USD,
        )

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Return normalised quarterly financials, oldest period first.

        Args:
            ticker: The symbol to look up.
            limit: Maximum quarters to return, most recent kept.

        Returns:
            One period per fiscal quarter the filer has reported, including
            fourth quarters derived from the annual figure. Empty when the SEC
            does not cover the symbol.

        Raises:
            ProviderError: If a request failed or the payload was unusable.
        """
        symbol = normalise_ticker(ticker)
        cik = self._cik_for(symbol)
        if cik is None:
            return []

        payload = self._company_facts(cik)
        all_facts = payload.get("facts", {})
        gaap = all_facts.get("us-gaap", {})
        if not gaap:
            return []
        dei = all_facts.get("dei", {})

        cost_of_revenue, cost_sources = _sourced_quarterly_series(gaap, _COST_OF_REVENUE_TAGS)
        flows = {
            "revenue": _quarterly_series(gaap, _REVENUE_TAGS),
            "gross_profit": _quarterly_series(gaap, _GROSS_PROFIT_TAGS),
            "cost_of_revenue": cost_of_revenue,
            "operating_income": _quarterly_series(gaap, _OPERATING_INCOME_TAGS),
            "operating_cash_flow": _quarterly_series(gaap, _OPERATING_CASH_FLOW_TAGS),
            "capital_expenditure": _quarterly_series(gaap, _CAPEX_TAGS),
            # Weighted averages are not additive, so no ladder differencing:
            # a fiscal Q4 share count simply stays missing rather than being
            # invented by subtraction.
            "shares_outstanding": _quarterly_series(
                gaap, _SHARES_TAGS, unit=_SHARES, additive=False
            ),
        }
        instants = {
            "cash": _instant_series(gaap, _CASH_TAGS),
            "short_term_investments": _instant_series(gaap, _SHORT_TERM_INVESTMENTS_TAGS),
            "long_term_debt": _instant_series(gaap, _LONG_TERM_DEBT_TAGS),
            "short_term_debt": _instant_series(gaap, _SHORT_TERM_DEBT_TAGS),
            # Cover-page count first, balance-sheet count as the fallback.
            "common_shares_outstanding": {
                **_instant_series(gaap, _COMMON_SHARES_GAAP_TAGS, unit=_SHARES),
                **_instant_series(dei, _COMMON_SHARES_DEI_TAGS, unit=_SHARES),
            },
        }
        period_ends = sorted({end for series in flows.values() for end in series})
        periods = [
            _build_period(period_end, flows, instants, cost_sources) for period_end in period_ends
        ]
        return periods[-limit:] if limit > 0 else periods

    def get_filings(self, ticker: str, limit: int = DEFAULT_FILING_LIMIT) -> list[Filing]:
        """Return the company's most recent filings, newest first.

        Read from the submissions document the adapter already fetches for the
        SIC description, so this costs no request the profile lookup does not
        make anyway. Only the forms in `FILING_FORMS` are kept: the recent index
        is mostly ownership reports and prospectus supplements, and a research
        brief that cited those would be citing noise.

        Args:
            ticker: The symbol to look up.
            limit: Maximum filings to return, most recent kept.

        Returns:
            Index entries, newest first. Empty when the SEC does not cover the
            symbol or the filer has no recent filing of a relevant form. No
            document is fetched — `Filing` is metadata.

        Raises:
            ProviderError: If a request failed or the payload was unusable.
        """
        symbol = normalise_ticker(ticker)
        cik = self._cik_for(symbol)
        if cik is None:
            return []

        recent = self._submissions(cik).get("filings", {})
        if not isinstance(recent, dict):
            return []
        index = recent.get("recent")
        if not isinstance(index, dict):
            return []

        filings = _filings_from_index(index, cik)
        filings.sort(key=lambda filing: (filing.filed, filing.accession), reverse=True)
        return filings[:limit] if limit > 0 else filings

    # -- internals ---------------------------------------------------------

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> list[FilingExcerpt]:
        """Fetch one filing's document and return the sections worth quoting.

        Separate from `get_filings` on purpose. Indexing a company's filings is
        one cheap request; reading them is one request per document, and a
        research brief needs the text of a handful of filings rather than all of
        them. Keeping the two apart is what lets the index stay complete while
        extraction stays selective.

        Never called while a brief is assembled — a brief reads what extraction
        already stored, so assembling one touches no network.

        Args:
            ticker: The symbol the filing belongs to, for the error message.
            filing: The stored index entry. Its `primary_document` names the
                document to fetch; without one there is nothing to read.

        Returns:
            One excerpt per section located, in reading order. Empty when the
            filing names no primary document, the form is one this extractor
            does not read, or no heading could be found confidently.

        Raises:
            ProviderRequestError: If the document could not be fetched.
            ProviderRateLimitError: If the SEC is rate limiting.
        """
        if not filing.primary_document:
            log.debug("filing has no primary document", ticker=ticker, accession=filing.accession)
            return []

        cik = self._cik_for(ticker)
        if cik is None:
            return []

        url = _document_url(cik, filing.accession, filing.primary_document)
        document = request_text(
            self._client,
            "GET",
            url,
            provider=PROVIDER_NAME,
            limiter=self._limiter,
            retry=self._retry,
        )
        sections = extract_sections(filing.form, clean_filing_text(document))
        return [
            FilingExcerpt(
                accession=filing.accession,
                form=filing.form,
                section=section.section,
                text=section.text,
                filed=filing.filed,
                url=url,
                source=PROVIDER_NAME,
            )
            for section in sections
        ]

    def _cik_for(self, symbol: str) -> int | None:
        """Return the SEC filer number for a ticker, loading the map once."""
        if self._cik_by_ticker is None:
            self._cik_by_ticker = self._load_cik_map()
        return self._cik_by_ticker.get(symbol)

    def _load_cik_map(self) -> dict[str, int]:
        """Fetch and index the SEC's ticker-to-CIK file.

        Raises:
            ProviderDataError: If the file is not in the expected shape.
        """
        payload = request_json(
            self._client,
            "GET",
            f"{self._www_base_url}/files/company_tickers.json",
            provider=PROVIDER_NAME,
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            raise ProviderDataError("sec returned an unexpected ticker map")

        mapping: dict[str, int] = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if isinstance(ticker, str) and isinstance(cik, int):
                mapping[normalise_ticker(ticker)] = cik
        if not mapping:
            raise ProviderDataError("sec ticker map contained no usable entries")
        return mapping

    def _industry_for(self, cik: int) -> str | None:
        """Return the filer's SIC description, or None when the SEC has none.

        The submissions document is small compared with company facts, and it is
        the only free source of a business classification in the pipeline. A
        failure here is not fatal: identity and statements are what this adapter
        exists for, and a company with no industry is scored rather than
        excluded.
        """
        try:
            submissions = self._submissions(cik)
        except ProviderError:
            log.warning("sec submissions lookup failed", cik=cik)
            return None

        description = submissions.get("sicDescription")
        if not isinstance(description, str) or not description.strip():
            return None
        return description.strip()

    def _submissions(self, cik: int) -> dict[str, Any]:
        """Fetch one filer's submission metadata, holding the most recent."""
        key = f"{cik:010d}"
        if self._submissions_cache is not None and self._submissions_cache[0] == key:
            return self._submissions_cache[1]

        payload = request_json(
            self._client,
            "GET",
            f"{self._data_base_url}/submissions/CIK{key}.json",
            provider=PROVIDER_NAME,
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            raise ProviderDataError(f"sec returned an unexpected submissions payload for {key}")

        self._submissions_cache = (key, payload)
        return payload

    def _company_facts(self, cik: int) -> dict[str, Any]:
        """Fetch every XBRL fact for one filer.

        The document is several megabytes and both public methods need it, so the
        most recent one is held. A single-entry cache is enough: ingestion works
        through one company at a time.
        """
        key = f"{cik:010d}"
        if self._facts_cache is not None and self._facts_cache[0] == key:
            return self._facts_cache[1]

        payload = request_json(
            self._client,
            "GET",
            f"{self._data_base_url}/api/xbrl/companyfacts/CIK{key}.json",
            provider=PROVIDER_NAME,
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            raise ProviderDataError(f"sec returned an unexpected facts payload for CIK {key}")

        self._facts_cache = (key, payload)
        return payload


def _filings_from_index(index: Mapping[str, Any], cik: int) -> list[Filing]:
    """Turn the submissions index's parallel arrays into filings.

    The SEC publishes the recent-filings index column-wise: one array per field,
    aligned by position. A filer whose arrays disagree in length would otherwise
    pair a form with the wrong accession number, so the shortest array bounds the
    read and the surplus is ignored rather than zipped against nothing.
    """
    accessions = _string_column(index, "accessionNumber")
    forms = _string_column(index, "form")
    filed = _string_column(index, "filingDate")
    periods = _string_column(index, "reportDate")
    documents = _string_column(index, "primaryDocument")

    rows = min(len(accessions), len(forms), len(filed))
    filings: list[Filing] = []
    for position in range(rows):
        form = forms[position].strip().upper()
        filed_on = _as_date(filed[position])
        accession = accessions[position].strip()
        if form not in FILING_FORMS or filed_on is None or not accession:
            continue

        document = documents[position] if position < len(documents) else ""
        filings.append(
            Filing(
                accession=accession,
                form=form,
                filed=filed_on,
                period_end=_as_date(periods[position]) if position < len(periods) else None,
                primary_document=document or None,
                url=_filing_url(cik, accession, document),
                source=PROVIDER_NAME,
            )
        )
    return filings


def _string_column(index: Mapping[str, Any], key: str) -> list[str]:
    """Return one column of the submissions index as strings."""
    column = index.get(key)
    if not isinstance(column, list):
        return []
    return [str(value) if value is not None else "" for value in column]


def _document_url(cik: int, accession: str, document: str) -> str:
    """Return the canonical location of one document inside a filing."""
    plain = accession.replace("-", "")
    return f"{DEFAULT_WWW_URL}/Archives/edgar/data/{cik}/{plain}/{document}"


def _filing_url(cik: int, accession: str, document: str) -> str:
    """Return the canonical SEC location of one filing.

    The archive path strips the dashes from the accession number; the printed
    form keeps them. Both appear in the same URL, which is why this is built
    rather than taken from a field.
    """
    plain = accession.replace("-", "")
    base = f"{DEFAULT_WWW_URL}/Archives/edgar/data/{cik}/{plain}"
    return f"{base}/{document}" if document else f"{base}/{accession}-index.htm"


def _latest_filed(facts: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the most recently filed fact from a group covering one period.

    The same quarter is filed repeatedly — in its own 10-Q, in next year's
    comparatives, and in each 10-K. The newest filing is the one that reflects
    any restatement, so it wins.
    """
    dated = [fact for fact in facts if isinstance(fact.get("filed"), str)]
    if not dated:
        return next(iter(facts), None)
    return max(dated, key=lambda fact: str(fact["filed"]))


def _facts_for_tag(gaap: Mapping[str, Any], tag: str, unit: str) -> list[Mapping[str, Any]]:
    """Return the raw facts filed under one concept tag."""
    entries = gaap.get(tag, {}).get("units", {}).get(unit)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _merge_chain(
    build: Callable[[list[Mapping[str, Any]]], dict[date, float]],
    gaap: Mapping[str, Any],
    tags: Sequence[str],
    unit: str,
) -> dict[date, float]:
    """Combine every tag in a fallback chain, most specific winning per period.

    Taking only the first tag that has *any* facts is not enough: accounting
    taxonomies retire tags, so a filer's history is often split across two of
    them. Apple still carries five `AvailableForSaleSecuritiesDebtSecuritiesCurrent`
    facts from 2010 while reporting sixty-two current ones under
    `MarketableSecuritiesCurrent` — stopping at the first non-empty tag returned
    a decade-old number and nothing recent.

    Chains are therefore merged. Tags are applied least-specific first so that a
    more specific tag overwrites for any period both cover, which also gives a
    filer that switched tags mid-history one continuous series.

    Args:
        build: Turns one tag's raw facts into a period-to-value mapping.
        gaap: The `us-gaap` block of a company-facts document.
        tags: Concept fallback chain, most specific first.
        unit: Unit key to read.

    Returns:
        The merged series.
    """
    merged: dict[date, float] = {}
    for tag in reversed(tags):
        merged.update(build(_facts_for_tag(gaap, tag, unit)))
    return merged


def _quarterly_series(
    gaap: Mapping[str, Any],
    tags: Sequence[str],
    unit: str = _USD,
    *,
    additive: bool = True,
) -> dict[date, float]:
    """Return one value per fiscal quarter end for a concept.

    Two shapes have to be handled, and most filers use both for different
    statements. Income-statement concepts are usually filed as discrete
    three-month durations. Cash-flow concepts are almost always filed
    **cumulatively**: every fact in a fiscal year shares that year's start date,
    so a 10-Q reports three months, then six, then nine, and the 10-K reports
    twelve. Read at face value, only the first quarter of each year would have a
    cash-flow figure — which is exactly what happens if the ladder is ignored.

    Consecutive rungs of such a ladder are therefore differenced: nine months
    less six months is the third quarter. The same operation yields the fourth
    quarter, which no filer reports directly, as the year less the nine months
    before it.

    Args:
        gaap: The `us-gaap` block of a company-facts document.
        tags: Concept fallback chain.
        unit: Unit key to read, `USD` for money and `shares` for counts.
        additive: Whether the concept accumulates over the year. True for flows
            like revenue and cash generated. **False for weighted averages such
            as diluted share count**, where the annual figure is the average of
            its quarters rather than their sum — differencing those produces
            negative share counts, which is how this was caught.

    Returns:
        A mapping of period end to value, discrete quarters only.
    """

    def build(facts: list[Mapping[str, Any]]) -> dict[date, float]:
        return _quarters_from_facts(facts, additive=additive)

    return _merge_chain(build, gaap, tags, unit)


def _sourced_quarterly_series(
    gaap: Mapping[str, Any], tags: Sequence[str]
) -> tuple[dict[date, float], dict[date, str]]:
    """Return a quarterly series plus the concept each period came from.

    Same merge order as `_quarterly_series` — least specific first, so a more
    specific tag overwrites — but it also records the winning concept per
    period. Which concept produced a cost figure decides whether the gross
    profit derived from it includes depreciation, and that is not something a
    reader should have to infer from the number.

    Args:
        gaap: The `us-gaap` block of a company-facts document.
        tags: Concept fallback chain, most specific first.

    Returns:
        The merged series and a matching map of period end to concept name.
    """
    values: dict[date, float] = {}
    sources: dict[date, str] = {}
    for tag in reversed(tags):
        series = _quarters_from_facts(_facts_for_tag(gaap, tag, _USD), additive=True)
        values.update(series)
        for period_end in series:
            sources[period_end] = tag
    return values, sources


def _quarters_from_facts(facts: list[Mapping[str, Any]], *, additive: bool) -> dict[date, float]:
    """Turn one tag's duration facts into discrete quarters."""
    if not facts:
        return {}

    direct: dict[date, list[Mapping[str, Any]]] = {}
    ladders: dict[date, dict[date, list[Mapping[str, Any]]]] = {}

    for fact in facts:
        start = _as_date(fact.get("start"))
        end = _as_date(fact.get("end"))
        if start is None or end is None:
            continue
        span = (end - start).days
        if _QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS:
            direct.setdefault(end, []).append(fact)
        # Deliberately not `elif`. A three-month fact beginning at the fiscal
        # year start is both a discrete quarter *and* the first rung of that
        # year's cumulative ladder. Excluding it from the ladder leaves the
        # second quarter with nothing to subtract from, which is precisely how
        # every Q2 cash flow went missing.
        if span <= _YEAR_MAX_DAYS:
            ladders.setdefault(start, {}).setdefault(end, []).append(fact)

    series = _resolve(direct)
    if additive:
        resolved_ladders = {start: _resolve(rungs) for start, rungs in ladders.items()}
        for start, rungs in resolved_ladders.items():
            _difference_ladder(series, start, rungs)
        # A filer reporting discrete quarters files each with its own start, so
        # its annual figure shares a start with nothing and the ladder above
        # cannot reach the fourth quarter. Subtracting the three known quarters
        # from the year is the only route to it for those filers.
        for start, rungs in resolved_ladders.items():
            _derive_year_end_quarter(series, start, rungs)
    return series


def _resolve(grouped: Mapping[date, list[Mapping[str, Any]]]) -> dict[date, float]:
    """Collapse each period's competing facts to the most recently filed value."""
    resolved: dict[date, float] = {}
    for period_end, group in grouped.items():
        chosen = _latest_filed(group)
        value = _as_float(chosen.get("val")) if chosen else None
        if value is not None:
            resolved[period_end] = value
    return resolved


def _derive_year_end_quarter(
    series: dict[date, float], start: date, rungs: Mapping[date, float]
) -> None:
    """Fill a fiscal year's final quarter from the annual figure.

    Used where the cumulative ladder cannot help: a filer that reports each
    quarter discretely gives every fact its own start date, so the annual figure
    has no six- or nine-month rung to subtract. The three quarters inside the
    year serve instead.

    Refuses unless exactly three quarters sit inside the year and they leave one
    quarter's gap to its end — otherwise the subtraction would manufacture a
    "quarter" out of a partially covered year, which is worse than a missing one.

    Args:
        series: Discrete quarters so far, updated in place.
        start: The fiscal-year start shared by these facts.
        rungs: Cumulative value by period end, including the annual figure.
    """
    for end, total in rungs.items():
        span = (end - start).days
        if not (_YEAR_MIN_DAYS <= span <= _YEAR_MAX_DAYS) or end in series:
            continue

        inside = sorted(known for known in series if start <= known < end)
        if len(inside) != _QUARTERS_BEFORE_YEAR_END:
            continue

        gap = (end - inside[-1]).days
        if not (_QUARTER_MIN_DAYS <= gap <= _QUARTER_MAX_DAYS):
            continue

        series[end] = total - sum(series[known] for known in inside)


def _difference_ladder(series: dict[date, float], start: date, rungs: Mapping[date, float]) -> None:
    """Turn one fiscal year's cumulative figures into discrete quarters.

    Gaps only: a discrete quarter the filer reported directly always wins over
    one reconstructed by subtraction.

    Args:
        series: Discrete quarters so far, updated in place.
        start: The fiscal-year start every rung shares.
        rungs: Cumulative value by period end.
    """
    previous_end, previous_value = start, 0.0
    for end in sorted(rungs):
        gap = (end - previous_end).days
        if _QUARTER_MIN_DAYS <= gap <= _QUARTER_MAX_DAYS and end not in series:
            series[end] = rungs[end] - previous_value
        previous_end, previous_value = end, rungs[end]


def _instant_series(
    facts: Mapping[str, Any], tags: Sequence[str], unit: str = _USD
) -> dict[date, float]:
    """Return one point-in-time value per reporting date for a concept.

    Instant facts carry an `end` and no `start`, which is how they are told apart
    from the duration facts on the income and cash-flow statements.

    Args:
        facts: A taxonomy's facts — `us-gaap` or `dei`.
        tags: Concept fallback chain, most specific first.
        unit: The unit to read. Share counts are `shares`, not `USD`.
    """
    return _merge_chain(_instants_from_facts, facts, tags, unit)


def _instants_from_facts(facts: list[Mapping[str, Any]]) -> dict[date, float]:
    """Turn one tag's instant facts into a value per reporting date."""
    grouped: dict[date, list[Mapping[str, Any]]] = {}

    for fact in facts:
        if fact.get("start") is not None:
            continue
        value_end = _as_date(fact.get("end"))
        if value_end is not None:
            grouped.setdefault(value_end, []).append(fact)

    series: dict[date, float] = {}
    for period_end, group in grouped.items():
        chosen = _latest_filed(group)
        value = _as_float(chosen.get("val")) if chosen else None
        if value is not None:
            series[period_end] = value
    return series


def _cover_page_instant(series: Mapping[date, float], period_end: date) -> float | None:
    """Return the share count filed alongside a quarter, or None.

    The cover-page count is stated *as of a date shortly before the filing*, so
    for a quarter ending 30 June it is typically dated in late July or early
    August — weeks after the balance-sheet date the other instants share. Looking
    only at the period end would find nothing, so the window runs forward to the
    next quarter's filing and the most recent count inside it wins.

    Multi-class filers tag one count per share class. Company-facts flattens the
    class dimension away, so the newest-filed fact is taken rather than a sum:
    summing risks double-counting a value re-filed in comparatives, and
    understating a two-class company is the safer error — it can be caught by the
    market-cap cross-check, whereas a doubled share count cannot.

    Args:
        series: Share counts by their stated date.
        period_end: The quarter to find a count for.

    Returns:
        The latest count in the window, or None when the filer tagged none.
    """
    window_start = period_end - timedelta(days=_COVER_PAGE_BACKSTOP_DAYS)
    window_end = period_end + timedelta(days=_COVER_PAGE_WINDOW_DAYS)
    dated = [stated for stated in series if window_start <= stated <= window_end]
    return series[max(dated)] if dated else None


def _nearest_instant(series: Mapping[date, float], period_end: date) -> float | None:
    """Return a balance-sheet value at or very near a period end.

    A balance sheet is dated to the same day as the quarter that closes it, but
    a filer occasionally dates it a day or two out. Anything further away is a
    different quarter and is not substituted.
    """
    if period_end in series:
        return series[period_end]
    for offset in (1, -1, 2, -2, 3, -3):
        candidate = period_end + timedelta(days=offset)
        if candidate in series:
            return series[candidate]
    return None


def _build_period(
    period_end: date,
    flows: Mapping[str, Mapping[date, float]],
    instants: Mapping[str, Mapping[date, float]],
    cost_sources: Mapping[date, str] | None = None,
) -> FinancialPeriod:
    """Assemble one quarter from the collected series.

    Args:
        period_end: The quarter being built.
        flows: Duration-based series by field name.
        instants: Balance-sheet series by field name.
        cost_sources: Which cost concept produced each period's figure, used to
            record how a derived gross profit was arrived at.
    """
    revenue = flows["revenue"].get(period_end)
    gross_profit = flows["gross_profit"].get(period_end)
    gross_profit_basis = GROSS_PROFIT_REPORTED if gross_profit is not None else None
    if gross_profit is None:
        # Not every filer tags gross profit; those that don't usually tag the
        # cost of revenue, and the subtraction is exact rather than an estimate.
        # A directly reported figure is never overwritten by this — the branch
        # only runs when the filer published none.
        cost = flows["cost_of_revenue"].get(period_end)
        if revenue is not None and cost is not None:
            gross_profit = revenue - cost
            gross_profit_basis = (cost_sources or {}).get(period_end)

    capex = flows["capital_expenditure"].get(period_end)
    cash = _nearest_instant(instants["cash"], period_end)
    short_term_investments = _nearest_instant(instants["short_term_investments"], period_end)
    if cash is not None and short_term_investments is not None:
        cash += short_term_investments

    return FinancialPeriod(
        period_end=period_end,
        revenue=revenue,
        gross_profit=gross_profit,
        gross_profit_basis=gross_profit_basis,
        operating_income=flows["operating_income"].get(period_end),
        operating_cash_flow=flows["operating_cash_flow"].get(period_end),
        # XBRL reports capital expenditure as a positive payment, but the domain
        # model's contract is an outflow either way.
        capital_expenditure=None if capex is None else abs(capex),
        cash=cash,
        total_debt=_total_debt(instants, period_end),
        shares_outstanding=flows["shares_outstanding"].get(period_end),
        common_shares_outstanding=_cover_page_instant(
            instants["common_shares_outstanding"], period_end
        ),
        reported_currency=_USD,
        source=PROVIDER_NAME,
    )


def _total_debt(instants: Mapping[str, Mapping[date, float]], period_end: date) -> float | None:
    """Return short- plus long-term debt for a period, or None if none is tagged.

    XBRL has no single `totalDebt`; there are only the individual instruments,
    and a filer with only one kind tags only that one. Summing what is present
    is therefore correct rather than partial — unlike a vendor's `totalDebt`
    fallback, where a missing component means the vendor omitted it.

    **Absence is not zero.** A filer tags a line item only while it exists, so a
    company that repaid its borrowings stops tagging them and looks identical to
    one whose borrowing tag this chain does not recognise. Reading absence as
    zero would let the second case pass as debt-free, and a risky company
    appearing financially strong is a far worse error than a debt-free one
    losing its net-cash figure. An explicit zero in the filing is still a
    recognised value and is returned as `0.0`.

    Args:
        instants: Balance-sheet series by field name.
        period_end: The quarter being built.

    Returns:
        Total debt, `0.0` where the filing states zero, or None where no
        borrowing was tagged at all.
    """
    long_term = _nearest_instant(instants["long_term_debt"], period_end)
    short_term = _nearest_instant(instants["short_term_debt"], period_end)
    if long_term is None and short_term is None:
        return None
    return (long_term or 0.0) + (short_term or 0.0)


def _as_date(value: object) -> date | None:
    """Parse an ISO date, returning None when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    """Return a float, or None when the value is absent or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def load_cik_map_from_file(path: str) -> dict[str, int]:
    """Load a ticker-to-CIK map from a saved copy of the SEC's file.

    Useful for tests and for running against a pinned snapshot rather than
    re-fetching the map.

    Args:
        path: Location of a `company_tickers.json` copy.

    Returns:
        A mapping of normalised ticker to filer number.

    Raises:
        ProviderDataError: If the file cannot be read or parsed.
    """
    try:
        with open(path, encoding="utf-8") as handle:  # noqa: PTH123 — plain read
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ProviderDataError(f"could not read the CIK map: {exc}") from exc

    return {
        normalise_ticker(entry["ticker"]): entry["cik_str"]
        for entry in payload.values()
        if isinstance(entry, dict) and isinstance(entry.get("ticker"), str)
    }
