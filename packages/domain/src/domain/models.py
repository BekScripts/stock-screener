"""Normalised value objects shared by every layer of the screener.

These models are the vocabulary the rest of the system speaks. Provider adapters
translate vendor payloads into them at the boundary, persistence stores them, and
the metric engine consumes them. Nothing here performs I/O.

Every financial field is `float | None`. `None` means *not reported*; `0.0` means
the company reported zero. Conflating the two is the single most damaging bug this
package can have, so no model ever defaults a financial field to zero.
"""

from __future__ import annotations

from datetime import date  # noqa: TC003 — pydantic needs the runtime symbol
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A price or size that cannot meaningfully be negative. Rejecting these at the
#: boundary means a garbled provider response fails as a `ProviderDataError` for
#: one ticker rather than becoming a negative 52-week low in a ranking.
NonNegativeFloat = Annotated[float, Field(ge=0)]

USD = "USD"
"""The only reporting currency the metric engine can safely mix with market cap."""


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


class FinancialPeriod(_Frozen):
    """One reporting period of normalised fundamentals, usually a quarter.

    Attributes:
        period_end: Last day of the reporting period. Periods are compared and
            ordered by this field.
        revenue: Total revenue for the period.
        gross_profit: Revenue less cost of revenue.
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
        reported_currency: ISO code the statements were filed in, when the
            provider says. None means unknown.
        source: Identifier of the provider the period came from.
    """

    period_end: date
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    operating_cash_flow: float | None = None
    capital_expenditure: float | None = None
    free_cash_flow: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    shares_outstanding: float | None = None
    reported_currency: str | None = None
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


class EligibilityWarning(StrEnum):
    """A caveat about a verdict that is not grounds for exclusion."""

    LIQUIDITY_UNVERIFIED = "LIQUIDITY_UNVERIFIED"
    """Only partial-market volume was available, so the liquidity threshold was
    not applied. The company may or may not clear it."""


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
        currency: ISO code the company reports its financials in, when the
            provider says. None means unknown, which is treated as USD.
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
    currency: str | None = None
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
        """Whether the company's statements are comparable with its market cap.

        An unknown currency counts as USD: the overwhelming majority of
        U.S.-listed common stock reports in dollars, and excluding every company
        whose provider omitted the field would empty the universe.
        """
        return self.currency is None or self.currency.strip().upper() == USD


class CompanyMetrics(_Frozen):
    """Every derived figure the scanner calculates for one company.

    All fields other than `ticker` are optional: a company with three quarters of
    history genuinely has no three-year CAGR, and saying so is more useful than
    inventing one.

    Attributes:
        ticker: The company the metrics describe.
        price: Latest close.
        market_cap: Market capitalisation, carried through from the profile.
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
        ttm_revenue: Sum of the latest four quarters.
        ttm_revenue_growth: Latest trailing year against the preceding one.
        revenue_cagr_3y: Compound annual growth over three years.
        gross_margin: Gross profit over revenue, latest quarter.
        gross_profit_growth_yoy: Gross profit against the year-ago quarter.
        operating_margin: Operating income over revenue, latest quarter.
        fcf_margin: Free cash flow over revenue, latest quarter.
        cash: Cash and equivalents, latest quarter.
        debt: Total debt, latest quarter.
        net_cash: Cash less total debt.
        share_count_growth_yoy: Dilution over the past year.
        return_6m: Total return over roughly six months.
        return_12m: Total return over roughly twelve months.
        high_52w: Highest close in the past year.
        low_52w: Lowest close in the past year.
        distance_from_52w_high: Latest close over the 52-week high, less one.
            Negative when the stock trades below its high.
    """

    ticker: str

    price: float | None = None
    market_cap: float | None = None
    average_dollar_volume_20d: float | None = None
    liquidity_basis: VolumeBasis = VolumeBasis.UNKNOWN
    trading_days_used: int = 0

    revenue_growth_yoy: float | None = None
    previous_revenue_growth_yoy: float | None = None
    revenue_growth_acceleration: float | None = None
    ttm_revenue: float | None = None
    ttm_revenue_growth: float | None = None
    revenue_cagr_3y: float | None = None

    gross_margin: float | None = None
    gross_profit_growth_yoy: float | None = None
    operating_margin: float | None = None
    fcf_margin: float | None = None

    cash: float | None = None
    debt: float | None = None
    net_cash: float | None = None

    share_count_growth_yoy: float | None = None

    return_6m: float | None = None
    return_12m: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    distance_from_52w_high: float | None = None

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
