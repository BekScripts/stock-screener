"""Normalised value objects shared by every layer of the screener.

These models are the vocabulary the rest of the system speaks. Provider adapters
translate vendor payloads into them at the boundary, persistence stores them, and
the metric engine consumes them. Nothing here performs I/O.

Every financial field is `float | None`. `None` means *not reported*; `0.0` means
the company reported zero. Conflating the two is the single most damaging bug this
package can have, so no model ever defaults a financial field to zero.
"""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 — pydantic needs the runtime symbols
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A price or size that cannot meaningfully be negative. Rejecting these at the
#: boundary means a garbled provider response fails as a `ProviderDataError` for
#: one ticker rather than becoming a negative 52-week low in a ranking.
NonNegativeFloat = Annotated[float, Field(ge=0)]

USD = "USD"
"""The currency U.S.-listed securities are quoted in, and the default for both
a reporting currency and a quote currency that nobody stated."""


def normalise_currency(value: str | None) -> str:
    """Return an ISO currency code in canonical form, defaulting to USD.

    An absent code is USD rather than an error. Most filings do not restate the
    obvious, and treating silence as unknown-and-therefore-excluded would empty
    a universe that is overwhelmingly dollar-denominated.

    Args:
        value: A raw currency code from a filing, provider or database row.

    Returns:
        The stripped, upper-cased code, or `USD` when there was none.
    """
    return value.strip().upper() if value and value.strip() else USD


def normalise_ticker(value: str) -> str:
    """Return a ticker in canonical form: stripped and upper-cased.

    Applied by every model that carries one. Ticker is the identity key and its
    uniqueness is enforced on the stored string, so a provider that returns
    `" xyz "` would otherwise create a second company row and split one
    company's price and fundamental history across two identities.

    Args:
        value: A raw symbol from a provider, fixture or command line.

    Returns:
        The canonical symbol.
    """
    return value.strip().upper()


class _Frozen(BaseModel):
    """Base for immutable value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PriceBar(_Frozen):
    """One daily OHLCV observation for a single security.

    Attributes:
        date: The trading session the bar covers.
        open: Opening price, in the listing currency.
        high: Session high. Must be at least `low`.
        low: Session low.
        close: Closing price, split- and dividend-adjusted where the provider
            supports it.
        volume: Shares traded during the session.
    """

    date: date
    open: NonNegativeFloat
    high: NonNegativeFloat
    low: NonNegativeFloat
    close: NonNegativeFloat
    volume: NonNegativeFloat

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        """Reject a bar whose high is below its low.

        Impossible rather than merely unusual. Accepted, it would put a 52-week
        high below the 52-week low and make the distance-from-high metric
        meaningless for that company.
        """
        if self.high < self.low:
            raise ValueError(f"bar high {self.high} is below its low {self.low}")
        return self


class PeriodCadence(StrEnum):
    """How long a reporting period covers, classified from its actual dates.

    Read from the XBRL context, never from the form that carried it or a label
    the filer wrote. A `6-K` may hold a six-month statement, a three-month
    result, an earnings release or nothing financial at all; a `20-F` normally
    holds a year. The duration is the fact and the form is a hint.

    Companies are compared *within* a cadence and never across one. A year of
    revenue and a quarter of revenue are both real figures and neither is the
    other divided or multiplied by anything.
    """

    QUARTERLY = "QUARTERLY"
    """About three months."""

    SEMIANNUAL = "SEMIANNUAL"
    """About six months. The interim cadence most foreign private issuers file,
    and never half a year pretending to be two quarters."""

    ANNUAL = "ANNUAL"
    """About twelve months, including a 52- or 53-week fiscal year."""

    UNKNOWN = "UNKNOWN"
    """A duration matching none of the above, or a period with no start date.
    Carried rather than guessed: an unclassifiable period is excluded from
    comparisons instead of being forced into the nearest bucket."""


#: Duration bands, in days, that classify a period. Deliberately wide.
#:
#: Fiscal calendars are not calendar quarters: a 13-week quarter is 91 days but
#: drifts a few days either side, and a 52/53-week fiscal year is 364 or 371
#: days rather than 365. Requiring exact counts would classify a large share of
#: real filers as UNKNOWN.
#:
#: The bands do not touch. A 120-day duration is not a long quarter or a short
#: half-year, it is something else, and calling it either would put a figure
#: covering four months into a series of three-month ones.
CADENCE_BANDS: tuple[tuple[PeriodCadence, int, int], ...] = (
    (PeriodCadence.QUARTERLY, 80, 100),
    (PeriodCadence.SEMIANNUAL, 165, 200),
    (PeriodCadence.ANNUAL, 340, 380),
)


def classify_cadence(start: date | None, end: date) -> PeriodCadence:
    """Return the cadence a period's own dates put it in.

    Args:
        start: First day of the period. None yields `UNKNOWN` — a duration
            cannot be measured from one end.
        end: Last day of the period.

    Returns:
        The matching cadence, or `UNKNOWN` when the span matches no band.
    """
    if start is None:
        return PeriodCadence.UNKNOWN
    span = (end - start).days
    for cadence, low, high in CADENCE_BANDS:
        if low <= span <= high:
            return cadence
    return PeriodCadence.UNKNOWN


class FinancialPeriod(_Frozen):
    """One reporting period of normalised fundamentals.

    A quarter for a domestic filer, and a half-year or a full year for the many
    foreign issuers that report on those cadences. The period is whatever the
    company actually reported: nothing here is a year divided by four.

    Attributes:
        period_start: First day of the reporting period, when the source says.
            None for a period whose duration is unknown — chiefly a balance
            sheet joined to nothing else.
        period_end: Last day of the reporting period. Periods are compared and
            ordered by this field.
        cadence: How long the period covers, classified from its dates. The
            field that stops a year of revenue being read as a quarter of it.
        revenue: Total revenue for the period.
        gross_profit: Revenue less cost of revenue.
        gross_profit_basis: How `gross_profit` was arrived at — the provider's
            concept name when it was reported directly, or the cost concept it
            was derived from. Two derived figures are not always the same
            measure: a cost concept that excludes depreciation yields a higher
            gross profit than one that includes it, and only this field
            distinguishes them.
        operating_income: Income from operations.
        operating_cash_flow: Net cash provided by operating activities.
        capital_expenditure: Capital spending as a positive outflow. Adapters
            normalise the sign, so a company that spent 5M reports `5_000_000.0`
            here regardless of the vendor's convention.
        free_cash_flow: Reported free cash flow, when the provider gives one.
            When absent, `domain.free_cash_flow` derives it from the two fields
            above.
        cash: Cash, equivalents and short-term investments.
        total_debt: Short- plus long-term debt.
        shares_outstanding: Share count for the period. **Weighted-average
            diluted shares.** Deliberately not mixed with basic shares or a
            period-end count: dilution is measured by comparing this field
            against itself a year earlier, and comparing a weighted average
            against a point-in-time count would manufacture a change that did
            not happen. Every adapter must map the same concept — which one each
            reads from is recorded in `docs/reference/metrics.md`.
        common_shares_outstanding: Common shares outstanding at a point in time,
            as stated on the filing's cover page. This is the count to multiply
            by a price: a weighted average covers a period and was never the
            number outstanding on any single day. Never used for dilution, and
            never substituted for the field above.
        reported_currency: ISO code the statements were filed in, when the
            provider says. None means unknown.
        source: Identifier of the provider the period came from.
    """

    period_end: date
    period_start: date | None = None
    cadence: PeriodCadence = PeriodCadence.UNKNOWN
    revenue: float | None = None
    gross_profit: float | None = None
    gross_profit_basis: str | None = None
    operating_income: float | None = None
    operating_cash_flow: float | None = None
    capital_expenditure: float | None = None
    free_cash_flow: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    shares_outstanding: float | None = None
    common_shares_outstanding: float | None = None
    reported_currency: str | None = None
    source: str = "unknown"

    @model_validator(mode="after")
    def _derive_cadence(self) -> Self:
        """Classify the period from its own dates unless a cadence was given.

        Deriving rather than requiring it means every existing construction
        site keeps working, and a period built with real dates is classified
        correctly without anyone remembering to pass the field.
        """
        if self.cadence is PeriodCadence.UNKNOWN and self.period_start is not None:
            object.__setattr__(
                self, "cadence", classify_cadence(self.period_start, self.period_end)
            )
        return self


class Filing(_Frozen):
    """One regulatory filing, as an index entry rather than a document.

    Metadata only, on purpose. The accession number, the form and the dates are
    enough to say *that* a company filed, *when*, and *for which period* — which
    is what a research brief needs to cite a filing rather than allude to one.
    The document behind the URL is deliberately not fetched: extracting text is a
    separate concern with its own failure modes, and a citation to a filing
    nobody read is still a verifiable citation.

    Attributes:
        accession: The SEC accession number, which identifies the filing
            uniquely. Kept in its dashed form, as the SEC prints it.
        form: Filing type — `10-K`, `10-Q`, `8-K`.
        filed: The date the filing was submitted.
        period_end: The reporting period it covers, when it states one. An `8-K`
            usually does not, which is not a gap.
        primary_document: File name of the filing's main document, when the
            index names one.
        url: Canonical location of the filing on the SEC's site.
        source: Identifier of the provider the entry came from.
    """

    accession: str
    form: str
    filed: date
    period_end: date | None = None
    primary_document: str | None = None
    url: str = ""
    source: str = "unknown"


class VolumeBasis(StrEnum):
    """How much of the market a volume figure represents.

    A dollar-volume threshold is calibrated against the consolidated tape. The
    same threshold applied to one exchange's share of volume is roughly
    twenty-five times stricter, which would quietly exclude most of the smaller
    companies this project exists to find — so the basis travels with the number
    and the screen refuses to compare across it.
    """

    CONSOLIDATED = "CONSOLIDATED"
    """Every U.S. venue. Comparable with the configured threshold."""

    PARTIAL = "PARTIAL"
    """A single exchange, such as a free IEX-only feed. Not comparable."""

    UNKNOWN = "UNKNOWN"
    """No volume, or no statement about where it came from."""


class MarketCapSource(StrEnum):
    """Where a market capitalisation came from.

    A figure a vendor supplied and one this system multiplied out are not the
    same claim, and the difference decides how much weight a discrepancy
    deserves. Carrying the source stops an estimate being read as a quote.
    """

    PROVIDER = "PROVIDER"
    """Supplied directly by the fundamentals provider."""

    CALCULATED = "CALCULATED"
    """Latest close times the most recent cover-page share count from filings.
    Correct to within a share issuance since the last filing, and blind to share
    classes the ticker does not represent."""

    UNKNOWN = "UNKNOWN"
    """Neither was available."""


class EligibilityWarning(StrEnum):
    """A caveat about a verdict that is not grounds for exclusion."""

    LIQUIDITY_UNVERIFIED = "LIQUIDITY_UNVERIFIED"
    """Only partial-market volume was available, so the liquidity threshold was
    not applied. The company may or may not clear it."""

    STALE_FUNDAMENTALS = "STALE_FUNDAMENTALS"
    """The newest reported period is older than this company's own reporting
    cadence explains.

    Judged against the cadence, not a fixed calendar. An annual filer whose last
    statement covers a year ending eight months ago is reporting entirely
    normally; a quarterly filer in the same position has missed two quarters.
    A warning rather than an exclusion — the figures are real and were true of
    the period they cover — but a score built on statements two reporting cycles
    old is describing a company that may no longer exist in that shape."""

    MARKET_CAP_CALCULATED = "MARKET_CAP_CALCULATED"
    """Market capitalisation was multiplied out from filings and a price rather
    than supplied by a provider. Good enough to screen on, worth verifying
    before acting on."""

    MARKET_CAP_DISCREPANCY = "MARKET_CAP_DISCREPANCY"
    """The provider's market capitalisation and the calculated one disagree
    materially. Neither is discarded and neither is averaged — the difference is
    surfaced, because its usual causes (a stale share count, multiple share
    classes, a recent issuance) each mean something different."""

    FX_UNAVAILABLE = "FX_UNAVAILABLE"
    """The company reports in one currency and trades in another, and no
    exchange rate was available to bring them together.

    A caveat rather than an exclusion, because it costs the company only the
    part of the picture that needs both sides. Revenue growth, margins and every
    price-based figure are computed from one currency each and remain perfectly
    valid; what cannot be computed is any ratio of a financial figure to a
    market capitalisation, and those return None rather than a mixed-currency
    number. The company is screened, and usually lands on `INSUFFICIENT_DATA`
    because valuation could not reach its coverage floor — which is the honest
    account of what is known about it."""


class FilingExcerpt(_Frozen):
    """Verbatim text lifted from one section of one filing.

    The counterpart to `Filing`, and deliberately a separate type. A `Filing`
    says a document exists; an excerpt says what part of it reads. Only the
    second can support a claim about a business, which is why they are never
    merged into one record — a citation that cannot distinguish "this 8-K was
    filed" from "this 8-K says" is a citation that proves nothing.

    Extraction is deterministic: headings are located by pattern, the body
    between them is taken verbatim, and nothing is summarised, paraphrased or
    inferred. A section that cannot be located confidently is absent rather than
    approximated.

    Attributes:
        accession: The filing this came from, in the SEC's dashed form.
        form: Filing type — `10-K`, `10-Q`, `8-K`.
        section: Which part of the filing, as a stable slug: `business`,
            `risk_factors`, `mda`, or `item_2.02` for an 8-K item.
        text: The extracted text, verbatim and bounded in length.
        filed: The date the filing was submitted.
        url: Where the document can be read.
        source: Identifier of the provider the text came from.
    """

    accession: str
    form: str
    section: str
    text: str
    filed: date
    url: str = ""
    source: str = "unknown"


class ExternalSearchResult(_Frozen):
    """One hit from an external search provider, before any judgement is applied.

    The raw shape at the provider boundary, normalised only enough to be the same
    across vendors: Tavily, Exa and Brave all return a title, a URL, a snippet and
    sometimes a date, under different field names. Translating them here is what
    keeps the collector free of vendor shapes — ADR 0003's rule, applied to a
    fourth kind of provider.

    Deliberately **not** `deep_research.ExternalEvidence`. A search hit is a
    candidate; evidence is what survived tiering, deduplication and validation.
    Keeping the two types apart is what stops an unvetted result reaching a brief,
    in the same way `DraftReport` and `ResearchReport` are kept apart.

    No tier and no source type here. Both are collection policy — a judgement
    about who published this and how close they sit to the facts — and a provider
    has no basis for either.

    Attributes:
        title: The headline, as the provider reports it.
        url: Where it can be read. The identity of the result.
        snippet: The provider's extract. May be empty, which usually disqualifies
            the result: evidence with nothing quotable supports no claim.
        published_at: Publication date when the provider supplies one, and None
            when it does not. Never inferred — a guessed date on a piece of
            current evidence is worse than an absent one.
        publisher: Site or outlet name when the provider names one. The domain is
            derived from `url` rather than trusted from here.
        author: Byline when supplied.
    """

    title: str
    url: str
    snippet: str = ""
    published_at: date | None = None
    publisher: str = ""
    author: str = ""


class CompanyProfile(_Frozen):
    """Identity and classification for one listed company.

    Attributes:
        ticker: Exchange symbol. Normalised to stripped upper case.
        name: Registered company name.
        exchange: Exchange code as reported by the provider, e.g. `NASDAQ`.
        sector: Broad sector classification, when known.
        industry: Narrower industry classification, when known.
        market_cap: Market capitalisation **in USD, in whole dollars** — not
            thousands or millions. Market-data providers rarely supply it, so it
            usually arrives from the fundamentals provider.
        average_volume: Average daily share volume across **all** venues, when a
            provider supplies it. This is the trustworthy liquidity input; a
            figure derived from single-exchange bars is not comparable with it.
        reporting_currency: ISO code the company states its **financial
            statements** in, read from the filing. None means unknown, which is
            treated as USD.
        quote_currency: ISO code the **listed security** trades in, and
            therefore the currency `market_cap` and every price are quoted in.
            None means unknown, which is treated as USD — the only listings this
            screen admits are NASDAQ, NYSE and NYSE American, all of which quote
            in dollars.

            Kept apart from `reporting_currency` because the two were one field
            and the two sources disagreed about what it meant: EDGAR wrote the
            filing's currency into it and a market-data vendor wrote the trading
            currency, which for an ADR is USD whatever the company reports in.
            One field could only ever hold one of those answers, and whichever
            arrived last won.
        is_fund: Whether the provider classifies this as an ETF or fund. A
            provider's own flag is far more reliable than inferring it from the
            name, so when it is set the name heuristics are not consulted.
        is_active: Whether the security is currently trading.
    """

    ticker: str
    name: str
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = Field(default=None, ge=0)
    average_volume: float | None = Field(default=None, ge=0)
    reporting_currency: str | None = None
    quote_currency: str | None = None
    is_fund: bool = False
    is_active: bool = True

    @model_validator(mode="after")
    def _normalise_ticker(self) -> Self:
        """Canonicalise the ticker so one company cannot become two rows."""
        canonical = normalise_ticker(self.ticker)
        if canonical != self.ticker:
            object.__setattr__(self, "ticker", canonical)
        return self

    @property
    def reports_in_usd(self) -> bool:
        """Whether the company states its financial statements in dollars.

        An unknown currency counts as USD: the overwhelming majority of
        U.S.-listed common stock reports in dollars, and excluding every company
        whose provider omitted the field would empty the universe.
        """
        return normalise_currency(self.reporting_currency) == USD

    @property
    def needs_conversion(self) -> bool:
        """Whether statements and market capitalisation are in different money.

        True for TSM, ASML, SAP and NVO; false for every domestic filer, which
        is what keeps the FX layer entirely off the domestic path.
        """
        return normalise_currency(self.reporting_currency) != normalise_currency(
            self.quote_currency
        )


class FxConversion(_Frozen):
    """One exchange rate, with enough provenance to reproduce it.

    A rate without its date is not a fact about anything: applying today's rate
    to a score computed three months ago silently restates that score in money
    that did not exist yet. So the date actually used travels with the number,
    and it is the date the *rate* is for — not the date it was asked for, which
    on a weekend is a day no market fixed a price.

    Attributes:
        base: The currency being converted **from** — the quote currency of the
            listed security, so a market capitalisation is in this money.
        quote: The currency being converted **to** — the company's reporting
            currency, so a balance sheet is in this money.
        rate: How many units of `quote` one unit of `base` buys. Multiply.
        rate_date: The date the rate is for, which may be earlier than the date
            requested when that fell on a weekend or a holiday.
        provider: Which source published it.
        retrieved_at: When it was fetched, which is not when it applied.
    """

    base: str
    quote: str
    rate: float = Field(gt=0)
    rate_date: date
    provider: str
    retrieved_at: datetime | None = None

    def convert(self, amount: float | None) -> float | None:
        """Return `amount`, expressed in `quote`. None stays None."""
        return None if amount is None else amount * self.rate


class CompanyMetrics(_Frozen):
    """Every derived figure the scanner calculates for one company.

    All fields other than `ticker` are optional: a company with three quarters of
    history genuinely has no three-year CAGR, and saying so is more useful than
    inventing one.

    Attributes:
        ticker: The company the metrics describe.
        price: Latest close.
        market_cap: Market capitalisation — the provider's figure when there is
            one, otherwise the calculated one. Every ratio built on size uses
            this field, so its source travels beside it.
        market_cap_source: Which of the two `market_cap` is.
        calculated_market_cap: Latest close times the most recent cover-page
            share count, whenever both exist. Kept even when a provider figure
            is preferred, so the two can be compared.
        market_cap_discrepancy: How far apart the two are, as a proportion of
            the provider's figure. None when only one exists.
        average_dollar_volume_20d: Average daily dollar volume. Taken from a
            provider's consolidated average where one exists, otherwise the mean
            of `close * volume` over the most recent sessions, up to twenty.
        liquidity_basis: Which of those two the figure is, and therefore whether
            it may be compared with the configured threshold.
        trading_days_used: How many sessions the average above is based on. Below
            twenty, treat the liquidity figure as low-confidence.
        revenue_growth_yoy: Latest quarter against the same quarter a year prior.
        previous_revenue_growth_yoy: The prior quarter's equivalent comparison.
        revenue_growth_acceleration: The two above subtracted, in decimal
            percentage points.
        recent_revenue_growth_yoy: Year-over-year growth for each of the latest
            four quarters, newest first. Shorter than four when the history has
            a gap — which means *unknown*, never a quarter that shrank.
        ttm_revenue: Sum of the latest four quarters.
        ttm_revenue_growth: Latest trailing year against the preceding one.
        revenue_cagr_3y: Compound annual growth over three years.
        gross_margin: Gross profit over revenue, latest quarter.
        gross_margin_change: Gross margin against the year-ago quarter, in
            decimal percentage points.
        gross_profit_growth_yoy: Gross profit against the year-ago quarter.
        operating_margin: Operating income over revenue, latest quarter.
        operating_margin_change: Operating margin against the year-ago quarter,
            in decimal percentage points.
        fcf_margin: Free cash flow over revenue, latest quarter.
        fcf_margin_change: FCF margin against the year-ago quarter, in decimal
            percentage points.
        ttm_free_cash_flow: Free cash flow summed over the latest four quarters.
        cash: Cash and equivalents, latest quarter.
        debt: Total debt, latest quarter.
        net_cash: Cash less total debt.
        enterprise_value: Market cap plus debt less cash. None when debt or cash
            is unknown — never computed by treating absent debt as zero.
        share_count_growth_yoy: Dilution over the past year.
        return_6m: Total return over roughly six months.
        return_12m: Total return over roughly twelve months.
        high_52w: Highest close in the past year.
        low_52w: Lowest close in the past year.
        distance_from_52w_high: Latest close over the 52-week high, less one.
            Negative when the stock trades below its high.
        reported_currency: The currency the fundamental figures here are
            denominated in, taken from the latest period. None when no period
            said. Fundamentals are kept in the money the company reported them
            in and are never restated — converting a history would put exchange
            rate movement into revenue growth, where it is not.
        quote_currency: The currency `price` and `market_cap` are in. USD for
            every listing this screen admits.
        market_cap_reporting_currency: `market_cap` expressed in
            `reported_currency`. Equal to `market_cap` when the two currencies
            match, and None when they differ and no rate was available.
        fx: The conversion used, or None when none was needed or none was
            found. Carried so a stored score can say which rate, from which
            date and which source, produced its valuation.
        fundamental_cadence: How often this company reports. The field that
            stops a screen calling an annual filer's revenue growth a
            "latest-quarter" figure.
        fundamentals_through: The end of the most recent reported period. Read
            with the cadence beside it: eight months after a fiscal year end is
            ordinary for an annual filer and very stale for a quarterly one.
        ttm_basis: How the trailing-year figures were formed — one stated
            fiscal year, four quarters, or two half-years.
    """

    ticker: str

    price: float | None = None
    market_cap: float | None = None
    market_cap_source: MarketCapSource = MarketCapSource.UNKNOWN
    calculated_market_cap: float | None = None
    market_cap_discrepancy: float | None = None
    average_dollar_volume_20d: float | None = None
    liquidity_basis: VolumeBasis = VolumeBasis.UNKNOWN
    trading_days_used: int = 0

    revenue_growth_yoy: float | None = None
    previous_revenue_growth_yoy: float | None = None
    revenue_growth_acceleration: float | None = None
    recent_revenue_growth_yoy: tuple[float, ...] = ()
    ttm_revenue: float | None = None
    ttm_revenue_growth: float | None = None
    revenue_cagr_3y: float | None = None

    gross_margin: float | None = None
    gross_margin_change: float | None = None
    gross_profit_growth_yoy: float | None = None
    operating_margin: float | None = None
    operating_margin_change: float | None = None
    fcf_margin: float | None = None
    fcf_margin_change: float | None = None
    ttm_free_cash_flow: float | None = None

    cash: float | None = None
    debt: float | None = None
    net_cash: float | None = None
    enterprise_value: float | None = None

    share_count_growth_yoy: float | None = None

    return_6m: float | None = None
    return_12m: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    distance_from_52w_high: float | None = None

    reported_currency: str | None = None
    quote_currency: str | None = None
    market_cap_reporting_currency: float | None = None
    fx: FxConversion | None = None

    fundamental_cadence: PeriodCadence = PeriodCadence.UNKNOWN
    fundamentals_through: date | None = None
    ttm_basis: str = "UNAVAILABLE"

    @property
    def market_cap_for_ratios(self) -> float | None:
        """Market capitalisation in the same money as the fundamentals here.

        **The only market capitalisation any ratio against a financial figure
        may use.** `market_cap` is quoted in dollars while a foreign issuer's
        cash, debt and revenue are not, so dividing one by the other is wrong by
        an exchange rate — for TSM that is a factor of about thirty-two, which
        turns the most expensive large cap on the board into the cheapest.

        Returns None rather than falling back to the unconverted figure when a
        conversion was needed and unavailable. A missing sub-score is a gap; a
        mixed-currency sub-score is a wrong answer that looks like a right one.
        """
        if not self.needs_conversion:
            return self.market_cap
        return self.market_cap_reporting_currency

    @property
    def needs_conversion(self) -> bool:
        """Whether the fundamentals and the market capitalisation differ in money."""
        return normalise_currency(self.reported_currency) != normalise_currency(self.quote_currency)

    @property
    def reports_in_usd(self) -> bool:
        """Whether these fundamentals may be compared with a USD market cap.

        An unknown currency counts as USD, matching `CompanyProfile`: the
        overwhelming majority of U.S.-listed common stock reports in dollars,
        and excluding every company whose filings did not say would empty the
        universe.
        """
        return normalise_currency(self.reported_currency) == USD

    @model_validator(mode="after")
    def _check_currency_coherence(self) -> Self:
        """Reject an enterprise value that no market capitalisation supports.

        An enterprise value is a market capitalisation plus debt less cash. If
        the market capitalisation could not be expressed in the currency of the
        debt and the cash, there is no arithmetic that produces a meaningful
        answer — so holding one here would mean some path had quietly built the
        mixed-currency figure this whole layer exists to prevent.

        Raising rather than clearing it, because a value that got this far is
        evidence of a bug upstream and silently blanking it would hide the bug
        while fixing the symptom.
        """
        if (
            self.enterprise_value is not None
            and self.needs_conversion
            and self.market_cap_reporting_currency is None
        ):
            raise ValueError(
                f"{self.ticker}: enterprise value in {self.reported_currency} cannot come from a "
                f"market capitalisation in {self.quote_currency} with no conversion"
            )
        return self

    @model_validator(mode="after")
    def _normalise_ticker(self) -> Self:
        """Canonicalise the ticker so it always matches the profile's."""
        canonical = normalise_ticker(self.ticker)
        if canonical != self.ticker:
            object.__setattr__(self, "ticker", canonical)
        return self


class ExclusionReason(StrEnum):
    """Why a security failed the eligibility screen.

    A security can fail for several reasons at once; the screen reports all of
    them rather than short-circuiting on the first.
    """

    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    MARKET_CAP_BELOW_MINIMUM = "MARKET_CAP_BELOW_MINIMUM"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    INACTIVE = "INACTIVE"
    UNSUPPORTED_SECURITY_TYPE = "UNSUPPORTED_SECURITY_TYPE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"


class EligibilityThresholds(_Frozen):
    """The configurable bar a security must clear to enter the scan.

    The application builds this from `Settings`; the defaults here mirror the
    values in `.env.example` so the package is usable standalone.

    Attributes:
        min_price: Minimum latest close.
        min_market_cap: Minimum market capitalisation.
        min_avg_dollar_volume: Minimum average daily dollar volume.
        min_trading_days: Sessions of price history required before the
            liquidity figure counts. Below this the security is excluded as
            illiquid rather than passed on thin data.
    """

    min_price: float = Field(default=2.0, gt=0)
    min_market_cap: float = Field(default=100_000_000.0, gt=0)
    min_avg_dollar_volume: float = Field(default=1_000_000.0, gt=0)
    min_trading_days: int = Field(default=20, gt=0)


class EligibilityResult(_Frozen):
    """The outcome of screening one security.

    Attributes:
        ticker: The security screened.
        eligible: True only when `reasons` is empty.
        reasons: Every check the security failed, in a stable order.
        warnings: Caveats that do not exclude the security but qualify the
            verdict — a check that could not be applied rather than one it
            failed.
    """

    ticker: str
    eligible: bool
    reasons: tuple[ExclusionReason, ...] = ()
    warnings: tuple[EligibilityWarning, ...] = ()
