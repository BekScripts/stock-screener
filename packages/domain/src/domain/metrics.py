"""Deterministic financial calculations.

Every function here is pure: same inputs, same output, no I/O, no clock. That is
what makes the numbers testable against hand-worked examples, which is the only
way to be confident a screener is not quietly ranking on a sign error.

Two rules run through the whole module:

**Missing is not zero.** A ratio whose denominator is absent, zero or negative
returns `None`. A trailing-twelve-month figure missing one quarter returns `None`
rather than summing three. Callers render `None` as a blank cell; they must never
coerce it to `0.0`.

**Periods are matched by date, not by list position.** A company that skipped a
filing would otherwise have its latest quarter compared against something fifteen
months old. `_period_near` looks for a period within `_MATCH_TOLERANCE_DAYS` of
the date it wants and returns `None` when there isn't one, so a gap in the history
produces a missing metric instead of a wrong one.

Input sequences may arrive unsorted or with duplicate dates — providers do both.
`_ordered_periods` and `_ordered_bars` normalise that once, at the top of each
public function.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

from domain.models import (
    CompanyMetrics,
    CompanyProfile,
    FinancialPeriod,
    PriceBar,
    VolumeBasis,
)

# A quarter is ~91 days, so a ±45-day window identifies exactly one of them: wide
# enough for the drift between a 13-week fiscal quarter and a calendar one, narrow
# enough that a missing quarter cannot masquerade as the one being looked for.
_QUARTER_DAYS = 91
_YEAR_DAYS = 365
_HALF_YEAR_DAYS = 182
_MATCH_TOLERANCE_DAYS = 45

_QUARTERS_PER_YEAR = 4
_CAGR_YEARS = 3

#: Four consecutive quarter-ends span three quarters, ~273 days. The tolerance
#: below admits fiscal-calendar drift but rejects a window with a quarter missing
#: from the middle, which would span ~364 days and otherwise look like a year.
_TTM_MIN_SPAN_DAYS = 3 * _QUARTER_DAYS - _MATCH_TOLERANCE_DAYS
_TTM_MAX_SPAN_DAYS = 3 * _QUARTER_DAYS + _MATCH_TOLERANCE_DAYS

DEFAULT_LIQUIDITY_WINDOW = 20
"""Sessions used for the average dollar volume, per the Phase 1 specification."""


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning None rather than raising or inventing a value."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _growth(current: float | None, prior: float | None) -> float | None:
    """Return proportional change from `prior` to `current`.

    A non-positive base makes growth meaningless — "up 300% from minus one
    million" is not a fact about the business — so it yields None alongside the
    missing cases.
    """
    if current is None or prior is None or prior <= 0:
        return None
    return (current - prior) / prior


def _ordered_periods(periods: Sequence[FinancialPeriod]) -> list[FinancialPeriod]:
    """Sort periods oldest-first and collapse duplicate `period_end` dates.

    Providers re-issue a period after a restatement. The later entry in the input
    wins, which matches the order adapters append them in.
    """
    unique: dict[date, FinancialPeriod] = {}
    for period in periods:
        unique[period.period_end] = period
    return sorted(unique.values(), key=lambda period: period.period_end)


def _ordered_bars(bars: Sequence[PriceBar]) -> list[PriceBar]:
    """Sort bars oldest-first and collapse duplicate session dates."""
    unique: dict[date, PriceBar] = {}
    for bar in bars:
        unique[bar.date] = bar
    return sorted(unique.values(), key=lambda bar: bar.date)


def _period_near(
    periods: Sequence[FinancialPeriod],
    target: date,
    tolerance_days: int = _MATCH_TOLERANCE_DAYS,
) -> FinancialPeriod | None:
    """Return the period ending closest to `target`, or None if none is close.

    Args:
        periods: Periods to search. Order does not matter.
        target: The period end being looked for.
        tolerance_days: How far from `target` a period may end and still count.

    Returns:
        The nearest period within tolerance, or None when the history has a gap
        where that period should be.
    """
    best: FinancialPeriod | None = None
    best_distance = tolerance_days + 1
    for period in periods:
        distance = abs((period.period_end - target).days)
        if distance <= tolerance_days and distance < best_distance:
            best, best_distance = period, distance
    return best


def _ttm_window(
    periods: Sequence[FinancialPeriod], quarters_back: int = 0
) -> list[FinancialPeriod] | None:
    """Return the four consecutive periods ending `quarters_back` from the latest.

    Args:
        periods: Periods, already ordered oldest-first and de-duplicated.
        quarters_back: How many quarters before the latest the window should end.
            Zero is the most recent trailing year.

    Returns:
        Exactly four periods spanning roughly a year, or None when the history is
        too short or has a gap that makes the window not a year.
    """
    end = len(periods) - quarters_back
    start = end - _QUARTERS_PER_YEAR
    if start < 0 or end > len(periods):
        return None

    window = list(periods[start:end])
    span = (window[-1].period_end - window[0].period_end).days
    if not _TTM_MIN_SPAN_DAYS <= span <= _TTM_MAX_SPAN_DAYS:
        return None
    return window


def _ttm_revenue(periods: Sequence[FinancialPeriod], quarters_back: int = 0) -> float | None:
    """Sum revenue across a trailing-twelve-month window, or None if incomplete."""
    window = _ttm_window(periods, quarters_back)
    if window is None:
        return None

    total = 0.0
    for period in window:
        if period.revenue is None:
            return None
        total += period.revenue
    return total


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------


def free_cash_flow(period: FinancialPeriod) -> float | None:
    """Return free cash flow for a period.

    Prefers the reported figure. Otherwise derives it as operating cash flow less
    capital expenditure, taking the absolute value of capex so a provider that
    reports spending as a negative number cannot turn an outflow into a credit.

    Args:
        period: The reporting period.

    Returns:
        Free cash flow, or None when neither the reported figure nor both of its
        components are available.
    """
    if period.free_cash_flow is not None:
        return period.free_cash_flow
    if period.operating_cash_flow is None or period.capital_expenditure is None:
        return None
    return period.operating_cash_flow - abs(period.capital_expenditure)


def gross_margin(period: FinancialPeriod) -> float | None:
    """Return gross profit over revenue for a period."""
    return _ratio(period.gross_profit, period.revenue)


def operating_margin(period: FinancialPeriod) -> float | None:
    """Return operating income over revenue for a period."""
    return _ratio(period.operating_income, period.revenue)


def fcf_margin(period: FinancialPeriod) -> float | None:
    """Return free cash flow over revenue for a period."""
    return _ratio(free_cash_flow(period), period.revenue)


def net_cash(period: FinancialPeriod) -> float | None:
    """Return cash less total debt.

    Unlike the margin functions this is a subtraction, so a zero result is a real
    observation: the company's cash exactly offsets its debt. Only a genuinely
    absent input yields None.
    """
    if period.cash is None or period.total_debt is None:
        return None
    return period.cash - period.total_debt


def yoy_revenue_growth(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return revenue growth of the latest quarter against the year-ago quarter.

    Args:
        periods: Quarterly periods in any order.

    Returns:
        Proportional growth as a decimal (0.35 is +35%), or None when either
        quarter is missing or the prior-year revenue is zero or negative.
    """
    ordered = _ordered_periods(periods)
    if not ordered:
        return None

    latest = ordered[-1]
    prior = _period_near(ordered[:-1], latest.period_end - timedelta(days=_YEAR_DAYS))
    if prior is None:
        return None
    return _growth(latest.revenue, prior.revenue)


def previous_yoy_revenue_growth(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return the prior quarter's year-over-year revenue growth.

    This is the comparison one quarter behind `yoy_revenue_growth` — Q1 against
    Q1 a year earlier, where the latest figure compares Q2 to Q2. Growth
    acceleration is the difference between the two.

    Args:
        periods: Quarterly periods in any order.

    Returns:
        Proportional growth as a decimal, or None when either quarter of that
        comparison is missing.
    """
    ordered = _ordered_periods(periods)
    if not ordered:
        return None

    latest = ordered[-1]
    previous = _period_near(ordered[:-1], latest.period_end - timedelta(days=_QUARTER_DAYS))
    if previous is None:
        return None

    prior_year = _period_near(
        [p for p in ordered if p.period_end < previous.period_end],
        previous.period_end - timedelta(days=_YEAR_DAYS),
    )
    if prior_year is None:
        return None
    return _growth(previous.revenue, prior_year.revenue)


def growth_acceleration(current: float | None, previous: float | None) -> float | None:
    """Return the change in growth rate, in decimal percentage points.

    Growth rates are subtracted, never divided: 35% following 18% is +17
    percentage points of acceleration, returned as `0.17`. Dividing would give
    1.94, which is a different and far less useful quantity.

    Args:
        current: The latest growth rate, as a decimal.
        previous: The preceding growth rate, as a decimal.

    Returns:
        `current - previous`, or None if either is unavailable.
    """
    if current is None or previous is None:
        return None
    return current - previous


def ttm_revenue(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return revenue summed over the latest four quarters.

    Args:
        periods: Quarterly periods in any order.

    Returns:
        The trailing-twelve-month total, or None when fewer than four consecutive
        quarters are available or any of them is missing a revenue figure. Three
        quarters are never silently summed.
    """
    return _ttm_revenue(_ordered_periods(periods))


def ttm_revenue_growth(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return growth of the latest trailing year against the preceding one.

    Args:
        periods: Quarterly periods in any order. Eight consecutive quarters are
            required.

    Returns:
        Proportional growth as a decimal, or None when the history is too short.
    """
    ordered = _ordered_periods(periods)
    return _growth(_ttm_revenue(ordered), _ttm_revenue(ordered, _QUARTERS_PER_YEAR))


def revenue_cagr_3y(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return the three-year compound annual growth rate of revenue.

    Methodology: the latest trailing-twelve-month revenue against the
    trailing-twelve-month revenue ending twelve quarters earlier, which needs
    sixteen quarters of history. TTM is used rather than annual reports because
    the ingestion layer stores quarterly statements, and TTM avoids comparing a
    partial fiscal year against a complete one.

    Args:
        periods: Quarterly periods in any order.

    Returns:
        The annualised rate as a decimal, or None when sixteen quarters are not
        available or the starting revenue is not positive.
    """
    ordered = _ordered_periods(periods)
    ending = _ttm_revenue(ordered)
    beginning = _ttm_revenue(ordered, _CAGR_YEARS * _QUARTERS_PER_YEAR)
    if ending is None or beginning is None or beginning <= 0 or ending <= 0:
        return None
    return float((ending / beginning) ** (1 / _CAGR_YEARS)) - 1


def gross_profit_growth_yoy(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return gross profit growth of the latest quarter against the year-ago one.

    Args:
        periods: Quarterly periods in any order.

    Returns:
        Proportional growth as a decimal, or None when either quarter is missing
        or the prior-year gross profit is not positive.
    """
    ordered = _ordered_periods(periods)
    if not ordered:
        return None

    latest = ordered[-1]
    prior = _period_near(ordered[:-1], latest.period_end - timedelta(days=_YEAR_DAYS))
    if prior is None:
        return None
    return _growth(latest.gross_profit, prior.gross_profit)


def share_count_growth_yoy(periods: Sequence[FinancialPeriod]) -> float | None:
    """Return the change in shares outstanding over roughly one year.

    Positive means dilution. The result is deliberately not clamped: a company
    that halved its share count through a buyback reports a negative figure.

    Args:
        periods: Quarterly periods in any order.

    Returns:
        Proportional change as a decimal, or None when either observation is
        missing.
    """
    ordered = _ordered_periods(periods)
    if not ordered:
        return None

    latest = ordered[-1]
    prior = _period_near(ordered[:-1], latest.period_end - timedelta(days=_YEAR_DAYS))
    if prior is None:
        return None
    return _growth(latest.shares_outstanding, prior.shares_outstanding)


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------


def latest_close(bars: Sequence[PriceBar]) -> float | None:
    """Return the most recent closing price, or None when there is no history."""
    ordered = _ordered_bars(bars)
    return ordered[-1].close if ordered else None


def trading_days_used(bars: Sequence[PriceBar], window: int = DEFAULT_LIQUIDITY_WINDOW) -> int:
    """Return how many sessions the liquidity window actually covers.

    Args:
        bars: Daily bars in any order.
        window: The desired window length.

    Returns:
        `min(window, sessions available)`. Below `window`, treat any liquidity
        figure computed from the same bars as low-confidence.
    """
    return min(window, len(_ordered_bars(bars)))


def average_dollar_volume(
    bars: Sequence[PriceBar], window: int = DEFAULT_LIQUIDITY_WINDOW
) -> float | None:
    """Return mean `close * volume` over the most recent sessions.

    Computed over however many sessions exist, up to `window`, so a recently
    listed stock still gets a figure. The confidence in that figure is reported
    separately by `trading_days_used`, and the eligibility screen requires a full
    window before the number counts towards the liquidity filter.

    Args:
        bars: Daily bars in any order.
        window: Maximum number of sessions to average.

    Returns:
        The average daily dollar volume, or None when there is no history.
    """
    ordered = _ordered_bars(bars)
    if not ordered:
        return None

    recent = ordered[-window:]
    return sum(bar.close * bar.volume for bar in recent) / len(recent)


def total_return(bars: Sequence[PriceBar], lookback_days: int) -> float | None:
    """Return the price return over approximately `lookback_days`.

    The baseline is the last session on or before the calendar target date, so
    the calculation lands on a real trading day rather than assuming markets were
    open exactly 182 or 365 days ago.

    Args:
        bars: Daily bars in any order.
        lookback_days: Calendar days to look back from the latest session.

    Returns:
        Proportional return as a decimal, or None when the history does not reach
        back that far or the baseline close is not positive.
    """
    ordered = _ordered_bars(bars)
    if not ordered:
        return None

    latest = ordered[-1]
    target = latest.date - timedelta(days=lookback_days)
    baseline = next((bar for bar in reversed(ordered) if bar.date <= target), None)
    if baseline is None:
        return None
    return _growth(latest.close, baseline.close)


def return_6m(bars: Sequence[PriceBar]) -> float | None:
    """Return the price return over roughly six months."""
    return total_return(bars, _HALF_YEAR_DAYS)


def return_12m(bars: Sequence[PriceBar]) -> float | None:
    """Return the price return over roughly twelve months."""
    return total_return(bars, _YEAR_DAYS)


def _trailing_year(bars: Sequence[PriceBar]) -> list[PriceBar]:
    """Return the bars falling within a year of the most recent session."""
    ordered = _ordered_bars(bars)
    if not ordered:
        return []
    cutoff = ordered[-1].date - timedelta(days=_YEAR_DAYS)
    return [bar for bar in ordered if bar.date >= cutoff]


def high_52w(bars: Sequence[PriceBar]) -> float | None:
    """Return the highest intraday price of the past year, or None if no history.

    Intraday highs are used rather than closes, which is the conventional reading
    of a 52-week high and the reason a stock can sit below its high on a day it
    closed at a record.
    """
    window = _trailing_year(bars)
    return max(bar.high for bar in window) if window else None


def low_52w(bars: Sequence[PriceBar]) -> float | None:
    """Return the lowest intraday price of the past year, or None if no history."""
    window = _trailing_year(bars)
    return min(bar.low for bar in window) if window else None


def distance_from_52w_high(bars: Sequence[PriceBar]) -> float | None:
    """Return the latest close against the 52-week high, less one.

    Negative values mean the stock trades below its high; zero means it closed at
    it. Returns None when there is no history or the high is not positive.
    """
    high = high_52w(bars)
    close = latest_close(bars)
    if high is None or close is None or high <= 0:
        return None
    return close / high - 1


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def resolve_liquidity(
    profile: CompanyProfile,
    price: float | None,
    bars: Sequence[PriceBar],
    *,
    bar_volume_basis: VolumeBasis = VolumeBasis.UNKNOWN,
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW,
) -> tuple[float | None, VolumeBasis]:
    """Return the best available dollar volume, and what it represents.

    A provider's consolidated average is preferred over anything derived from
    price bars, because a free market-data feed often carries only one exchange
    — a few percent of the real volume — and a threshold calibrated for the
    whole market cannot be applied to that.

    Args:
        profile: May carry a consolidated average share volume.
        price: Latest close, needed to turn share volume into dollar volume.
        bars: Daily history, used only when the profile has no average.
        bar_volume_basis: What the bars' volume represents. The caller knows
            which feed produced them; the metric engine cannot.
        liquidity_window: Sessions to average over when falling back to bars.

    Returns:
        The dollar volume and its basis. `UNKNOWN` when neither source yields
        one.
    """
    if profile.average_volume is not None and price is not None:
        return price * profile.average_volume, VolumeBasis.CONSOLIDATED

    from_bars = average_dollar_volume(bars, liquidity_window)
    if from_bars is None:
        return None, VolumeBasis.UNKNOWN
    return from_bars, bar_volume_basis


def build_company_metrics(
    profile: CompanyProfile,
    periods: Sequence[FinancialPeriod],
    bars: Sequence[PriceBar],
    *,
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW,
    bar_volume_basis: VolumeBasis = VolumeBasis.UNKNOWN,
) -> CompanyMetrics:
    """Calculate every derived metric for one company.

    Margins are taken from the latest reported quarter rather than a trailing
    year, so they move as soon as the business does; growth figures use
    year-over-year comparisons, which is where seasonality has to be handled.

    Args:
        profile: Identity and market capitalisation for the company.
        periods: Its quarterly financial history, in any order.
        bars: Its daily price history, in any order.
        liquidity_window: Sessions to average dollar volume over.
        bar_volume_basis: What the bars' volume represents — see
            `resolve_liquidity`.

    Returns:
        A fully populated `CompanyMetrics`, with None in every position the
        available data cannot support.
    """
    ordered = _ordered_periods(periods)
    latest = ordered[-1] if ordered else None
    price = latest_close(bars)
    liquidity, basis = resolve_liquidity(
        profile,
        price,
        bars,
        bar_volume_basis=bar_volume_basis,
        liquidity_window=liquidity_window,
    )

    current_growth = yoy_revenue_growth(ordered)
    prior_growth = previous_yoy_revenue_growth(ordered)

    return CompanyMetrics(
        ticker=profile.ticker,
        price=price,
        market_cap=profile.market_cap,
        average_dollar_volume_20d=liquidity,
        liquidity_basis=basis,
        trading_days_used=trading_days_used(bars, liquidity_window),
        revenue_growth_yoy=current_growth,
        previous_revenue_growth_yoy=prior_growth,
        revenue_growth_acceleration=growth_acceleration(current_growth, prior_growth),
        ttm_revenue=ttm_revenue(ordered),
        ttm_revenue_growth=ttm_revenue_growth(ordered),
        revenue_cagr_3y=revenue_cagr_3y(ordered),
        gross_margin=gross_margin(latest) if latest else None,
        gross_profit_growth_yoy=gross_profit_growth_yoy(ordered),
        operating_margin=operating_margin(latest) if latest else None,
        fcf_margin=fcf_margin(latest) if latest else None,
        cash=latest.cash if latest else None,
        debt=latest.total_debt if latest else None,
        net_cash=net_cash(latest) if latest else None,
        share_count_growth_yoy=share_count_growth_yoy(ordered),
        return_6m=return_6m(bars),
        return_12m=return_12m(bars),
        high_52w=high_52w(bars),
        low_52w=low_52w(bars),
        distance_from_52w_high=distance_from_52w_high(bars),
    )
