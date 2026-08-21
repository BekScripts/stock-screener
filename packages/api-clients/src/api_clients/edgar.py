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
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import structlog

from api_clients._http import RateLimiter, RetryPolicy, request_json, request_text
from api_clients.errors import ProviderDataError, ProviderError
from api_clients.filing_text import clean_filing_text, extract_sections
from domain import (
    CADENCE_BANDS,
    CompanyProfile,
    Filing,
    FilingExcerpt,
    FinancialPeriod,
    PeriodCadence,
    classify_cadence,
    normalise_ticker,
)

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
#: The investing-section line for additions to long-lived operating assets.
#:
#: Ordered most-specific first, so a filer tagging the general concept keeps it
#: and the industry-specific ones only fill in where it is absent. Extractive
#: filers tag their entire capital programme as
#: `PaymentsToAcquireOilAndGasProperty` and never tag the general concept at
#: all; without it their capital spending reads as absent and free cash flow
#: cannot be derived.
_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquireOilAndGasProperty",
    "PaymentsForCapitalImprovements",
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
#:
#: Each classification splits into two tiers, because XBRL debt concepts are of
#: two different kinds and mixing them is what makes a filer's borrowings
#: disappear:
#:
#: * **Totals** roll every borrowing of that classification into one figure.
#:   They are alternatives to one another — a filer tags one — so they resolve
#:   through the usual most-specific-wins chain.
#: * **Components** are individual instruments. A filer with a revolver *and* a
#:   term loan tags both, and they **co-exist**, so they are summed with each
#:   other rather than overriding one another.
#:
#: A total is preferred where the filer states one, since it is that filer's own
#: carrying amount net of issue costs. Components are summed only where no total
#: was tagged. Summing the two tiers together would double-count.
_LONG_TERM_DEBT_TOTAL_TAGS = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
)
_LONG_TERM_DEBT_COMPONENT_TAGS = (
    "LineOfCredit",
    "SecuredDebt",
    "ConvertibleDebtNoncurrent",
    "ConvertibleNotesPayable",
    "NotesPayableRelatedPartiesNoncurrent",
    "FinanceLeaseLiabilityNoncurrent",
)
_SHORT_TERM_DEBT_TOTAL_TAGS = (
    "LongTermDebtCurrent",
    "DebtCurrent",
)
_SHORT_TERM_DEBT_COMPONENT_TAGS = (
    "LinesOfCreditCurrent",
    "SecuredDebtCurrent",
    "ShortTermBorrowings",
    "ConvertibleDebtCurrent",
    "ConvertibleNotesPayableCurrent",
    "NotesPayableCurrent",
    "FinanceLeaseLiabilityCurrent",
)

#: IFRS concept chains, for filers whose facts arrive under `ifrs-full`.
#:
#: Foreign private issuers filing a 20-F under IFRS tag the same statements with
#: entirely different names — `ProfitLossFromOperatingActivities` rather than
#: `OperatingIncomeLoss`, `CostOfSales` rather than `CostOfGoodsAndServicesSold`.
#: Every concept below was taken from facts TSM, SAP and NVO have actually filed,
#: not from what the IFRS taxonomy theoretically contains. None of the three
#: needed a company-extension tag for any field the metric engine reads, which is
#: why this is a static table and not a heuristic tag matcher.
_IFRS_REVENUE_TAGS = (
    "Revenue",
    "RevenueFromContractsWithCustomers",
    "RevenueFromSaleOfGoods",
    "RevenueFromRenderingOfServices",
)
_IFRS_GROSS_PROFIT_TAGS = ("GrossProfit",)
_IFRS_COST_OF_REVENUE_TAGS = ("CostOfSales",)
_IFRS_OPERATING_INCOME_TAGS = ("ProfitLossFromOperatingActivities",)
_IFRS_OPERATING_CASH_FLOW_TAGS = ("CashFlowsFromUsedInOperatingActivities",)

#: Capital spending, where the filer states one concept covering everything.
#: SAP tags only this, and it already includes intangibles, investment property
#: and other non-current assets — so the component concepts below must not be
#: added to it.
_IFRS_CAPEX_TOTAL_TAGS = (
    "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
)
#: Capital spending lines that **co-exist**, summed only where the filer tagged
#: no combined total. TSM and NVO tag property and intangibles separately, and a
#: semiconductor or pharmaceutical capital programme that omitted intangibles
#: would understate reinvestment.
#:
#: These two tiers are not interchangeable measures: SAP's combined concept also
#: sweeps in investment property and other non-current assets, so its capital
#: expenditure is on a slightly broader basis than TSM's or NVO's. That is a real
#: comparability limit between filers rather than a mapping choice, and it is
#: recorded in `docs/reference/metrics.md`.
_IFRS_CAPEX_COMPONENT_TAGS = (
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
)

#: Diluted weighted-average shares. `AdjustedWeightedAverageShares` is the IFRS
#: diluted figure; `WeightedAverageShares` is the basic one and is deliberately
#: not a fallback, for the same reason basic shares are not one under us-gaap.
_IFRS_SHARES_TAGS = ("AdjustedWeightedAverageShares",)

_IFRS_CASH_TAGS = ("CashAndCashEquivalents",)

#: Deliberately empty. The us-gaap chain adds short-term investments to cash,
#: and IFRS has no concept that is reliably the same measure: TSM's
#: `ShorttermInvestmentsClassifiedAsCashEquivalents` is already *inside* its cash
#: equivalents and would double-count, `OtherCurrentFinancialAssets` is a wider
#: bucket including derivatives and receivables, and `CurrentInvestments` was
#: last tagged by SAP in 2017 and NVO in 2018. Cash is therefore cash and
#: equivalents alone for an IFRS filer, which understates the liquid position of
#: a company holding marketable securities. That is the safe direction — it
#: lowers net cash and the quality sub-score built on it, so no company looks
#: financially stronger than it is.
_IFRS_SHORT_TERM_INVESTMENTS_TAGS: tuple[str, ...] = ()

#: Point-in-time share counts. NVO tags no `dei` cover-page count at all, so the
#: IFRS balance-sheet concepts are the only route to one for that filer.
_IFRS_COMMON_SHARES_TAGS = (
    "NumberOfSharesOutstanding",
    "NumberOfSharesIssued",
)

#: Borrowings for the whole entity, current and non-current together. Where a
#: filer states this, it is the answer and nothing is added to it: SAP's 6,150.0
#: is exactly its 4,550.0 non-current plus 1,600.0 current, and NVO's 130,958.0
#: is exactly its 118,941.0 plus 12,017.0. Both also tag `BondsIssued`, which is
#: a *breakdown* of those totals — adding it would nearly double the debt.
_IFRS_TOTAL_BORROWINGS_TAGS = ("Borrowings",)

_IFRS_LONG_TERM_DEBT_TOTAL_TAGS = ("NoncurrentPortionOfNoncurrentBorrowings",)
#: Non-current instruments that appear as separate balance-sheet lines and are
#: therefore summed. TSM carries NT$926.6bn of bonds beside NT$31.8bn of bank
#: loans and states no total; reading `LongtermBorrowings` alone would report
#: 3% of its borrowings and make the company look very nearly debt-free.
_IFRS_LONG_TERM_DEBT_COMPONENT_TAGS = (
    "LongtermBorrowings",
    "NoncurrentPortionOfNoncurrentBondsIssued",
)
_IFRS_SHORT_TERM_DEBT_TOTAL_TAGS = ("CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",)
_IFRS_SHORT_TERM_DEBT_COMPONENT_TAGS = (
    "CurrentPortionOfLongtermBorrowings",
    "ShorttermBorrowings",
)
#: Last resort for the current classification only, and never summed with the
#: components above. TSM's current bonds of NT$57.1bn sit *inside* its
#: NT$59.9bn "long-term liabilities — current portion" balance-sheet line, so
#: adding the two would overstate total debt by the whole bond figure.
_IFRS_SHORT_TERM_DEBT_FALLBACK_TAGS = (
    "CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued",
)

_USD = "USD"
_SHARES = "shares"

US_GAAP_NAMESPACE = "us-gaap"
"""Taxonomy a domestic filer, and many foreign ones, tag their statements with."""

IFRS_NAMESPACE = "ifrs-full"
"""Taxonomy a foreign private issuer reporting under IFRS tags instead.

Which of the two a company uses cannot be inferred from where it is listed or
what it files: ASML files a 20-F and tags `us-gaap`, while TSM files a 20-F and
tags `ifrs-full`. It is read per company from the facts document.
"""


class _ConceptSet(NamedTuple):
    """Every concept chain needed to read one taxonomy's statements.

    A record of constants, not a strategy object. The reading machinery —
    `_merge_chain`, `_quarterly_series`, `_instant_series` and the rest — was
    already written to take a facts block and a unit as arguments, so supporting
    a second taxonomy needs a second table rather than a second code path. The
    only genuine behavioural difference is how borrowings are put together, and
    that is a flag rather than a subclass.

    Attributes:
        namespace: The key this taxonomy's facts sit under in a company-facts
            document.
        capex_total: Concepts stating capital spending in one figure.
        capex_components: Concepts summed when no total was tagged.
        total_borrowings: Entity-wide borrowings, current and non-current
            together. Empty for us-gaap, which has no such concept.
        short_term_debt_fallback: Read only when neither the current total nor
            the current components were tagged.
    """

    namespace: str
    revenue: tuple[str, ...]
    gross_profit: tuple[str, ...]
    cost_of_revenue: tuple[str, ...]
    operating_income: tuple[str, ...]
    operating_cash_flow: tuple[str, ...]
    capex_total: tuple[str, ...]
    capex_components: tuple[str, ...]
    shares: tuple[str, ...]
    cash: tuple[str, ...]
    short_term_investments: tuple[str, ...]
    common_shares: tuple[str, ...]
    total_borrowings: tuple[str, ...]
    long_term_debt_total: tuple[str, ...]
    long_term_debt_components: tuple[str, ...]
    short_term_debt_total: tuple[str, ...]
    short_term_debt_components: tuple[str, ...]
    short_term_debt_fallback: tuple[str, ...]


_US_GAAP_CONCEPTS = _ConceptSet(
    namespace=US_GAAP_NAMESPACE,
    revenue=_REVENUE_TAGS,
    gross_profit=_GROSS_PROFIT_TAGS,
    cost_of_revenue=_COST_OF_REVENUE_TAGS,
    operating_income=_OPERATING_INCOME_TAGS,
    operating_cash_flow=_OPERATING_CASH_FLOW_TAGS,
    capex_total=_CAPEX_TAGS,
    # Intangible purchases are deliberately not summed in here. Domestic filers
    # have been scored without them since Phase 1, and adding them now would
    # move existing scores for a reason unrelated to international coverage.
    capex_components=(),
    shares=_SHARES_TAGS,
    cash=_CASH_TAGS,
    short_term_investments=_SHORT_TERM_INVESTMENTS_TAGS,
    common_shares=_COMMON_SHARES_GAAP_TAGS,
    total_borrowings=(),
    long_term_debt_total=_LONG_TERM_DEBT_TOTAL_TAGS,
    long_term_debt_components=_LONG_TERM_DEBT_COMPONENT_TAGS,
    short_term_debt_total=_SHORT_TERM_DEBT_TOTAL_TAGS,
    short_term_debt_components=_SHORT_TERM_DEBT_COMPONENT_TAGS,
    short_term_debt_fallback=(),
)

_IFRS_CONCEPTS = _ConceptSet(
    namespace=IFRS_NAMESPACE,
    revenue=_IFRS_REVENUE_TAGS,
    gross_profit=_IFRS_GROSS_PROFIT_TAGS,
    cost_of_revenue=_IFRS_COST_OF_REVENUE_TAGS,
    operating_income=_IFRS_OPERATING_INCOME_TAGS,
    operating_cash_flow=_IFRS_OPERATING_CASH_FLOW_TAGS,
    capex_total=_IFRS_CAPEX_TOTAL_TAGS,
    capex_components=_IFRS_CAPEX_COMPONENT_TAGS,
    shares=_IFRS_SHARES_TAGS,
    cash=_IFRS_CASH_TAGS,
    short_term_investments=_IFRS_SHORT_TERM_INVESTMENTS_TAGS,
    common_shares=_IFRS_COMMON_SHARES_TAGS,
    total_borrowings=_IFRS_TOTAL_BORROWINGS_TAGS,
    long_term_debt_total=_IFRS_LONG_TERM_DEBT_TOTAL_TAGS,
    long_term_debt_components=_IFRS_LONG_TERM_DEBT_COMPONENT_TAGS,
    short_term_debt_total=_IFRS_SHORT_TERM_DEBT_TOTAL_TAGS,
    short_term_debt_components=_IFRS_SHORT_TERM_DEBT_COMPONENT_TAGS,
    short_term_debt_fallback=_IFRS_SHORT_TERM_DEBT_FALLBACK_TAGS,
)

#: Taxonomies in the order they are looked for. us-gaap first because it covers
#: every domestic filer and a large share of foreign ones.
_CONCEPT_SETS = (_US_GAAP_CONCEPTS, _IFRS_CONCEPTS)

#: Annual reports of a foreign private issuer.
#:
#: The share count on their cover page is stated in **ordinary shares**, while
#: the U.S.-listed security is usually an American Depositary Share representing
#: some number of them. TSM's is five; ASML's, SAP's and NVO's are one. Nothing
#: in the XBRL says which — TSM's cover page calls the security "Common Shares"
#: and the ratio appears only in a prose footnote — so multiplying that count by
#: a U.S. price is right three times in four and 5x wrong the fourth. Cover-page
#: counts from these forms are therefore not read at all.
FPI_ANNUAL_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})

#: How far after a quarter end a cover-page share count may be dated, and how
#: far before. A 10-Q lands within about six weeks of the quarter it reports and
#: states its share count as of a date near filing; ninety days stops one
#: quarter's count being read as the next one's.
FILING_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "20-F/A", "40-F", "40-F/A"})
"""Forms a research brief may cite.

The recent-filings index is dominated by ownership reports and prospectus
supplements. These are the ones that carry the business: the annual and
quarterly reports, the current report a domestic company files when something
happens between them, and the annual reports their foreign counterparts file
instead.

**`6-K` is deliberately excluded**, despite being the only current report a
foreign private issuer has. It is not a foreign `8-K`. An 8-K reports a material
event under a numbered item; a 6-K is an untyped envelope for anything a company
publishes at home — TSM has filed 713 of them, carrying monthly revenue notices,
month-end reports, board changes, AGM notices and dividend adjustments, none of
it XBRL-tagged and none of it structured enough to quote from.

Including them made a foreign issuer's citable record entirely noise *and* hid
the one filing worth reading: at fifty to ninety 6-Ks a year, the eight most
recent filings for TSM and NVO were all 6-Ks and neither company's 20-F was
reachable at all. Leaving them out is what makes the annual report citable.
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

        payload = self._company_facts(cik)
        all_facts = payload.get("facts", {})
        concepts = _detect_concept_set(all_facts)
        return CompanyProfile(
            ticker=symbol,
            name=str(payload.get("entityName") or symbol),
            # The registrant's own SIC description, e.g. "State Commercial
            # Banks". Not a market-data sector taxonomy, but it is what makes the
            # unsupported-sector rule work without a commercial provider — and it
            # comes free with a request the adapter already needs.
            industry=self._industry_for(cik),
            # Read from the unit the filer actually tagged its revenue in, never
            # assumed. This field decides `UNSUPPORTED_CURRENCY`, and a company
            # reporting in TWD while its market capitalisation is quoted in
            # dollars would otherwise pass a screen it should fail — silently,
            # and by a factor that looks like a plausible valuation.
            reporting_currency=_detect_currency(all_facts, concepts),
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
        concepts = _detect_concept_set(all_facts)
        if concepts is None:
            return []
        book = all_facts.get(concepts.namespace, {})
        dei = all_facts.get("dei", {})
        # The currency the filer tagged, not an assumption. Everything money-
        # denominated below is read from this unit key; reading `USD` from a
        # company that files in EUR finds nothing at all, which is exactly how
        # ASML — whose concepts all match the us-gaap chains — produced no
        # periods whatsoever.
        unit = _detect_currency(all_facts, concepts) or _USD

        # Which cadence this filer actually reports on, read from its revenue
        # facts. Everything below is then read at that one cadence, so a year
        # and the quarters inside it never both become periods and nothing is
        # counted twice.
        cadence, period_starts = _detect_cadence(book, concepts, unit)
        if cadence is PeriodCadence.UNKNOWN:
            # Nothing said. The quarterly path is what this adapter has always
            # done and what the domestic universe needs, so an undetectable
            # filer keeps the old behaviour rather than losing its history.
            cadence = PeriodCadence.QUARTERLY
        if cadence is PeriodCadence.QUARTERLY:
            flows, cost_sources = _quarterly_flows(book, concepts, unit)
        else:
            flows, cost_sources = _reported_flows(book, concepts, cadence, unit)
        instants = {
            "cash": _instant_series(book, concepts.cash, unit),
            "short_term_investments": _instant_series(book, concepts.short_term_investments, unit),
            "total_borrowings": _instant_series(book, concepts.total_borrowings, unit),
            "long_term_debt_total": _instant_series(book, concepts.long_term_debt_total, unit),
            "long_term_debt_components": _summed_instant_series(
                book, concepts.long_term_debt_components, unit
            ),
            "short_term_debt_total": _instant_series(book, concepts.short_term_debt_total, unit),
            "short_term_debt_components": _summed_instant_series(
                book, concepts.short_term_debt_components, unit
            ),
            "short_term_debt_fallback": _instant_series(
                book, concepts.short_term_debt_fallback, unit
            ),
            # Cover-page count first, balance-sheet count as the fallback. Counts
            # stated on a foreign private issuer's annual report are dropped —
            # see `FPI_ANNUAL_FORMS` for why they cannot be multiplied by a
            # U.S. price.
            "common_shares_outstanding": {
                **_instant_series(
                    book, concepts.common_shares, unit=_SHARES, exclude_forms=FPI_ANNUAL_FORMS
                ),
                **_instant_series(
                    dei, _COMMON_SHARES_DEI_TAGS, unit=_SHARES, exclude_forms=FPI_ANNUAL_FORMS
                ),
            },
        }
        period_ends = sorted({end for series in flows.values() for end in series})
        periods = [
            _build_period(
                period_end,
                flows,
                instants,
                cost_sources,
                currency=unit,
                cadence=cadence,
                period_start=period_starts.get(period_end),
            )
            for period_end in period_ends
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


def _detect_concept_set(all_facts: Mapping[str, Any]) -> _ConceptSet | None:
    """Return the concept table matching the taxonomy this filer tagged.

    Read from the facts document rather than inferred from the company, because
    nothing about a company predicts it. ASML and TSM both file a 20-F as
    foreign private issuers; ASML's statements arrive under `us-gaap` and TSM's
    under `ifrs-full`. Guessing from the exchange, the form or the country would
    be wrong for one of them.

    **Presence alone is not the test.** A filer that changed taxonomy keeps the
    old block forever: Telus carries one us-gaap concept with four facts, the
    newest from 2018, beside 269 ifrs-full concepts with eight thousand. Taking
    the first namespace that exists would read the vestigial one and return no
    periods at all, so the taxonomies are ranked by how much of the income
    statement each actually carries.

    Args:
        all_facts: The `facts` block of a company-facts document.

    Returns:
        The concept set whose revenue concepts the filer tagged most, or the
        larger block when neither has revenue — a genuinely pre-revenue company
        still has a balance sheet. None when no recognised taxonomy is present,
        which is not an error, only a company there is nothing to read.
    """
    ranked: list[tuple[int, int, int, _ConceptSet]] = []
    for index, concepts in enumerate(_CONCEPT_SETS):
        book = all_facts.get(concepts.namespace)
        if not isinstance(book, dict) or not book:
            continue
        revenue_facts = sum(
            len(entries)
            for tag in concepts.revenue
            for entries in book.get(tag, {}).get("units", {}).values()
            if isinstance(entries, list)
        )
        # Negative index keeps the declared order as the final tie-break, so a
        # filer tagging both taxonomies identically still resolves to us-gaap.
        ranked.append((revenue_facts, len(book), -index, concepts))
    if not ranked:
        return None
    return max(ranked, key=lambda entry: entry[:3])[3]


def _detect_currency(all_facts: Mapping[str, Any], concepts: _ConceptSet | None) -> str | None:
    """Return the currency a filer states its statements in.

    Taken from the unit key carrying the most revenue facts. The unit is the
    filing's own declaration, which makes it the authoritative answer — unlike a
    market-data vendor's `currency` field, which for an ADR is the currency the
    *share* trades in and is USD for every foreign issuer on a U.S. exchange.

    Picking the most-tagged unit rather than the first also handles a
    convenience translation correctly. TSM tags 26 revenue facts in TWD and 9 in
    USD, the USD column being a courtesy restatement at a single year-end spot
    rate; TWD is what the company reports in, and it wins on count.

    Args:
        all_facts: The `facts` block of a company-facts document.
        concepts: The taxonomy table to read revenue concepts from.

    Returns:
        An ISO currency code, or None when nothing could be determined.
    """
    if concepts is None:
        return None
    book = all_facts.get(concepts.namespace, {})
    counts: dict[str, int] = {}
    for tag in concepts.revenue:
        units = book.get(tag, {}).get("units", {})
        if not isinstance(units, dict):
            continue
        for unit, entries in units.items():
            if unit == _SHARES or not isinstance(entries, list):
                continue
            counts[unit] = counts.get(unit, 0) + len(entries)
    if not counts:
        return None
    # Ties broken alphabetically so the answer never depends on dict ordering.
    return max(sorted(counts), key=lambda unit: counts[unit])


def _facts_for_tag(
    gaap: Mapping[str, Any],
    tag: str,
    unit: str,
    *,
    exclude_forms: frozenset[str] | None = None,
) -> list[Mapping[str, Any]]:
    """Return the raw facts filed under one concept tag.

    Args:
        gaap: A taxonomy's facts.
        tag: The concept to read.
        unit: The unit key to read.
        exclude_forms: Forms whose facts are dropped. Used to keep a foreign
            private issuer's ordinary-share cover-page count out of the series
            that market capitalisation is calculated from.
    """
    entries = gaap.get(tag, {}).get("units", {}).get(unit)
    if not isinstance(entries, list):
        return []
    facts: list[Mapping[str, Any]] = [entry for entry in entries if isinstance(entry, dict)]
    if exclude_forms is None:
        return facts
    return [fact for fact in facts if fact.get("form") not in exclude_forms]


def _merge_chain(
    build: Callable[[list[Mapping[str, Any]]], dict[date, float]],
    gaap: Mapping[str, Any],
    tags: Sequence[str],
    unit: str,
    *,
    exclude_forms: frozenset[str] | None = None,
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
        exclude_forms: Forms whose facts are dropped before merging.

    Returns:
        The merged series.
    """
    merged: dict[date, float] = {}
    for tag in reversed(tags):
        merged.update(build(_facts_for_tag(gaap, tag, unit, exclude_forms=exclude_forms)))
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


def _capex_series(book: Mapping[str, Any], concepts: _ConceptSet, unit: str) -> dict[date, float]:
    """Return capital spending per period, preferring a filer's combined figure.

    Two shapes, and summing them together would double-count. SAP states one
    concept covering property, intangibles, investment property and other
    non-current assets. TSM and NVO state property and intangibles as separate
    lines, and reading only the property line would omit a real part of the
    capital programme.

    A stated total therefore wins outright, exactly as it does for borrowings,
    and the components are summed only for the periods no total covers.

    Args:
        book: The taxonomy's facts.
        concepts: The taxonomy table.
        unit: The currency unit to read.

    Returns:
        Capital spending by period end. Empty where the filer tagged neither.
    """
    totals = _quarterly_series(book, concepts.capex_total, unit)
    if not concepts.capex_components:
        return totals

    summed: dict[date, float] = {}
    for tag in concepts.capex_components:
        for period_end, value in _quarters_from_facts(
            _facts_for_tag(book, tag, unit), additive=True
        ).items():
            summed[period_end] = summed.get(period_end, 0.0) + value
    summed.update(totals)
    return summed


def _sourced_quarterly_series(
    gaap: Mapping[str, Any], tags: Sequence[str], unit: str = _USD
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
        unit: The currency unit to read.

    Returns:
        The merged series and a matching map of period end to concept name.
    """
    values: dict[date, float] = {}
    sources: dict[date, str] = {}
    for tag in reversed(tags):
        series = _quarters_from_facts(_facts_for_tag(gaap, tag, unit), additive=True)
        values.update(series)
        for period_end in series:
            sources[period_end] = tag
    return values, sources


def _quarterly_flows(
    book: Mapping[str, Any], concepts: _ConceptSet, unit: str
) -> tuple[dict[str, dict[date, float]], dict[date, str]]:
    """Build the duration series for a filer that reports quarterly.

    The domestic path, unchanged: cumulative cash-flow ladders are differenced
    into discrete quarters and a fourth quarter is recovered from the annual
    figure, because no filer states one.
    """
    cost_of_revenue, cost_sources = _sourced_quarterly_series(book, concepts.cost_of_revenue, unit)
    flows = {
        "revenue": _quarterly_series(book, concepts.revenue, unit),
        "gross_profit": _quarterly_series(book, concepts.gross_profit, unit),
        "cost_of_revenue": cost_of_revenue,
        "operating_income": _quarterly_series(book, concepts.operating_income, unit),
        "operating_cash_flow": _quarterly_series(book, concepts.operating_cash_flow, unit),
        "capital_expenditure": _capex_series(book, concepts, unit),
        # Weighted averages are not additive, so no ladder differencing:
        # a fiscal Q4 share count simply stays missing rather than being
        # invented by subtraction.
        "shares_outstanding": _quarterly_series(
            book, concepts.shares, unit=_SHARES, additive=False
        ),
    }
    return flows, cost_sources


def _reported_flows(
    book: Mapping[str, Any], concepts: _ConceptSet, cadence: PeriodCadence, unit: str
) -> tuple[dict[str, dict[date, float]], dict[date, str]]:
    """Build the duration series for a filer that reports annually or half-yearly.

    **Nothing is differenced and nothing is derived.** Each figure is a period
    the company actually stated, taken at face value. The ladder arithmetic the
    quarterly path uses exists to recover quarters a filer never published; a
    filer reporting a full year has published the full year, and subtracting
    anything from it would manufacture a period nobody reported.

    Args:
        book: The taxonomy's facts.
        concepts: The concept table.
        cadence: The cadence to read.
        unit: The reporting currency unit.

    Returns:
        The duration series by field name, and which cost concept produced each
        period's gross profit.
    """
    cost_of_revenue: dict[date, float] = {}
    cost_sources: dict[date, str] = {}
    for tag in reversed(concepts.cost_of_revenue):
        series = {
            end: value
            for end, (_, value) in _periods_from_facts(
                _facts_for_tag(book, tag, unit), cadence
            ).items()
        }
        cost_of_revenue.update(series)
        for period_end in series:
            cost_sources[period_end] = tag

    capex_total = _cadence_series(book, concepts.capex_total, cadence, unit)
    capex: dict[date, float] = {}
    for tag in concepts.capex_components:
        for period_end, (_, value) in _periods_from_facts(
            _facts_for_tag(book, tag, unit), cadence
        ).items():
            capex[period_end] = capex.get(period_end, 0.0) + value
    capex.update(capex_total)

    flows = {
        "revenue": _cadence_series(book, concepts.revenue, cadence, unit),
        "gross_profit": _cadence_series(book, concepts.gross_profit, cadence, unit),
        "cost_of_revenue": cost_of_revenue,
        "operating_income": _cadence_series(book, concepts.operating_income, cadence, unit),
        "operating_cash_flow": _cadence_series(book, concepts.operating_cash_flow, cadence, unit),
        "capital_expenditure": capex,
        "shares_outstanding": _cadence_series(book, concepts.shares, cadence, _SHARES),
    }
    return flows, cost_sources


def _periods_from_facts(
    facts: list[Mapping[str, Any]], cadence: PeriodCadence
) -> dict[date, tuple[date, float]]:
    """Return one value per period end for facts of a single cadence.

    Duration facts only, filtered to the band the cadence covers, and resolved
    to the most recently filed value where a period was reported more than once.
    That last part is what stops a 20-F's comparative columns — the same fiscal
    year restated in each of the next two annual reports — being counted as
    extra history.

    Args:
        facts: One concept's raw facts.
        cadence: The cadence to keep.

    Returns:
        Period end mapped to its start date and value.
    """
    grouped: dict[date, list[Mapping[str, Any]]] = {}
    starts: dict[date, date] = {}
    for fact in facts:
        start, end = _as_date(fact.get("start")), _as_date(fact.get("end"))
        if start is None or end is None or classify_cadence(start, end) is not cadence:
            continue
        grouped.setdefault(end, []).append(fact)
        starts[end] = start

    resolved: dict[date, tuple[date, float]] = {}
    for period_end, group in grouped.items():
        chosen = _latest_filed(group)
        value = _as_float(chosen.get("val")) if chosen else None
        if value is not None:
            resolved[period_end] = (starts[period_end], value)
    return resolved


def _cadence_series(
    book: Mapping[str, Any], tags: Sequence[str], cadence: PeriodCadence, unit: str
) -> dict[date, float]:
    """Return one value per period end for a concept at one cadence.

    Chains merge exactly as they do for quarters — least specific first, so a
    more specific tag wins for any period both cover.
    """
    merged: dict[date, float] = {}
    for tag in reversed(tags):
        for period_end, (_, value) in _periods_from_facts(
            _facts_for_tag(book, tag, unit), cadence
        ).items():
            merged[period_end] = value
    return merged


def _cadence_starts(
    book: Mapping[str, Any], tags: Sequence[str], cadence: PeriodCadence, unit: str
) -> dict[date, date]:
    """Return the start date of each period a concept covers at one cadence."""
    starts: dict[date, date] = {}
    for tag in reversed(tags):
        for period_end, (start, _) in _periods_from_facts(
            _facts_for_tag(book, tag, unit), cadence
        ).items():
            starts[period_end] = start
    return starts


def _detect_cadence(
    book: Mapping[str, Any], concepts: _ConceptSet, unit: str
) -> tuple[PeriodCadence, dict[date, date]]:
    """Return the cadence a filer actually reports on, and its period starts.

    Chosen from revenue, because it is the one concept every filer states and
    the one every growth metric is built from. The winner is the cadence with
    the longest **contiguous recent run** — not simply the most facts, and not
    the shortest duration available.

    That distinction is the whole point. Brookfield has filed nine three-month
    facts, which looks like a rich quarterly history until you notice they are
    all second quarters, one per year, three hundred and sixty-five days apart.
    A run counted by contiguity sees a quarterly run of one and an annual run of
    nine, and picks annual — where the periods really are consecutive and a
    trailing year really can be formed. Counting facts alone would have picked
    quarterly and then failed to build anything from it.

    Args:
        book: The taxonomy's facts.
        concepts: The concept table for that taxonomy.
        unit: The reporting currency unit.

    Returns:
        The chosen cadence and the start date of every period at it. `UNKNOWN`
        with an empty mapping when no cadence has any usable history.
    """
    # Revenue decides, because it is the concept every filer states and the one
    # every growth metric is built from. Where a filer tags none — a
    # pre-revenue company, or a partial document — the cash-flow and operating
    # lines answer instead, rather than the whole history being lost to a
    # concept that happened to be absent.
    for chain in (concepts.revenue, concepts.operating_cash_flow, concepts.operating_income):
        cadence, starts = _cadence_from(book, chain, unit)
        if cadence is not PeriodCadence.UNKNOWN:
            return cadence, starts
    return PeriodCadence.UNKNOWN, {}


def _cadence_from(
    book: Mapping[str, Any], chain: Sequence[str], unit: str
) -> tuple[PeriodCadence, dict[date, date]]:
    """Return the cadence one concept chain reports on, and its period starts."""
    best = (0, 0, PeriodCadence.UNKNOWN)
    starts: dict[date, date] = {}
    for cadence, low, high in CADENCE_BANDS:
        # The quarterly candidate is measured with the ladder differencing the
        # domestic path already does. A filer reporting revenue cumulatively —
        # three months, then six, then nine — states only one discrete quarter
        # per year directly, so a raw band filter would see a quarterly run of
        # one and read a perfectly ordinary domestic filer as annual.
        series = (
            _quarterly_series(book, chain, unit)
            if cadence is PeriodCadence.QUARTERLY
            else _cadence_series(book, chain, cadence, unit)
        )
        if not series:
            continue
        run = _contiguous_run(sorted(series), low, high)
        # Finer cadences win ties, so a filer reporting both quarterly and
        # annually is read at the resolution it actually publishes.
        rank = (run, len(series), cadence)
        if rank[:2] > best[:2]:
            best = rank
            starts = _cadence_starts(book, chain, cadence, unit)
    return best[2], starts


def _contiguous_run(period_ends: Sequence[date], low: int, high: int) -> int:
    """Return how many recent periods sit one cadence-length apart.

    Counted backwards from the newest period, stopping at the first gap. A
    history with a hole in the middle is not evidence of the cadence it would
    have had without the hole.

    Args:
        period_ends: Period ends, oldest first.
        low: Shortest acceptable gap between consecutive periods.
        high: Longest acceptable gap.

    Returns:
        The length of the run, at least 1 when any period exists.
    """
    if not period_ends:
        return 0
    run = 1
    for earlier, later in zip(reversed(period_ends[:-1]), reversed(period_ends), strict=False):
        if not low <= (later - earlier).days <= high:
            break
        run += 1
    return run


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
    facts: Mapping[str, Any],
    tags: Sequence[str],
    unit: str = _USD,
    *,
    exclude_forms: frozenset[str] | None = None,
) -> dict[date, float]:
    """Return one point-in-time value per reporting date for a concept.

    Instant facts carry an `end` and no `start`, which is how they are told apart
    from the duration facts on the income and cash-flow statements.

    Args:
        facts: A taxonomy's facts — `us-gaap` or `dei`.
        tags: Concept fallback chain, most specific first.
        unit: The unit to read. Share counts are `shares`, not `USD`.
        exclude_forms: Forms whose facts are dropped.
    """
    return _merge_chain(_instants_from_facts, facts, tags, unit, exclude_forms=exclude_forms)


def _summed_instant_series(
    facts: Mapping[str, Any], tags: Sequence[str], unit: str = _USD
) -> dict[date, float]:
    """Return the sum across concepts that co-exist rather than substitute.

    `_instant_series` resolves a chain of *alternative* names for one measure.
    This is its counterpart for concepts that are separate things: a revolving
    credit facility and a term loan are both outstanding at once, so a filer
    tagging `LineOfCredit` and `SecuredDebt` owes the sum of the two. Taking
    only the most specific would report one loan and silently drop the other.

    Args:
        facts: A taxonomy's facts — `us-gaap` or `dei`.
        tags: Concepts to add together.
        unit: The unit to read.

    Returns:
        A value per reporting date, summed over whichever concepts that date
        carries. Dates no concept covers are absent rather than zero.
    """
    summed: dict[date, float] = {}
    for tag in tags:
        for period_end, value in _instants_from_facts(_facts_for_tag(facts, tag, unit)).items():
            summed[period_end] = summed.get(period_end, 0.0) + value
    return summed


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
    *,
    currency: str = _USD,
    cadence: PeriodCadence = PeriodCadence.UNKNOWN,
    period_start: date | None = None,
) -> FinancialPeriod:
    """Assemble one quarter from the collected series.

    Args:
        period_end: The quarter being built.
        flows: Duration-based series by field name.
        instants: Balance-sheet series by field name.
        cost_sources: Which cost concept produced each period's figure, used to
            record how a derived gross profit was arrived at.
        currency: The unit every money figure here was read in. Recorded on the
            period so a consumer can refuse to mix it with a market
            capitalisation quoted in another currency.
        cadence: How long the period covers, detected for the filer as a whole.
        period_start: First day of the period, when the facts stated one.
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
        period_start=period_start,
        cadence=cadence,
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
        reported_currency=currency,
        source=PROVIDER_NAME,
    )


def _total_debt(instants: Mapping[str, Mapping[date, float]], period_end: date) -> float | None:
    """Return short- plus long-term debt for a period, or None if none is tagged.

    XBRL has no single `totalDebt`; there are only the individual instruments,
    and a filer with only one kind tags only that one. Summing what is present
    is therefore correct rather than partial — unlike a vendor's `totalDebt`
    fallback, where a missing component means the vendor omitted it.

    Each classification is read as a stated total where the filer gives one, and
    otherwise as the sum of the instruments it tagged. Both tiers matter: a
    filer that states only current maturities while carrying its revolver and
    term loan under instrument concepts would otherwise report those maturities
    as its entire debt — and where the maturities are zero, report no debt at
    all while owing the sum of the two facilities.

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
    # An entity-wide total, where the taxonomy has one and the filer stated it,
    # is the whole answer. SAP's `Borrowings` of 6,150.0 is exactly its 4,550.0
    # non-current plus 1,600.0 current, and NVO's 130,958.0 is exactly its
    # 118,941.0 plus 12,017.0. Both filers *also* tag `BondsIssued`, which is a
    # breakdown of that same total rather than a further borrowing — adding it
    # would report NVO as owing 252,032.0 against an actual 130,958.0.
    entity_total = _nearest_instant(instants.get("total_borrowings", {}), period_end)
    if entity_total is not None:
        return entity_total

    long_term = _classified_debt(instants, "long_term_debt", period_end)
    short_term = _classified_debt(instants, "short_term_debt", period_end)
    if short_term is None:
        # Read only when the classification produced nothing at all. TSM's
        # current bonds of NT$57.1bn sit *inside* the NT$59.9bn "long-term
        # liabilities — current portion" line its balance sheet actually shows,
        # so this is a last resort rather than an addition.
        short_term = _nearest_instant(instants.get("short_term_debt_fallback", {}), period_end)
    if long_term is None and short_term is None:
        return None
    return (long_term or 0.0) + (short_term or 0.0)


def _classified_debt(
    instants: Mapping[str, Mapping[date, float]], field: str, period_end: date
) -> float | None:
    """Return one classification's borrowings, preferring the filer's own total.

    Args:
        instants: Balance-sheet series by field name.
        field: The classification prefix, `long_term_debt` or `short_term_debt`.
        period_end: The quarter being built.

    Returns:
        The stated total where there is one, otherwise the sum of the tagged
        instruments, or None where the filing tagged neither.
    """
    total = _nearest_instant(instants[f"{field}_total"], period_end)
    if total is not None:
        return total
    return _nearest_instant(instants[f"{field}_components"], period_end)


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
