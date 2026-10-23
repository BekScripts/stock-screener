"""Market-state features derived from stored price bars, free of I/O.

These describe how a security *behaves* — how far it moves, how often, how
smoothly, and how that compares to the market. They are deliberately **not**
score inputs. Nothing here feeds `build_company_metrics` or `CompanyScore`, and
nothing here may be wired into a scoring component without a new
`CURRENT_SCORE_VERSION`; that separation is why this is its own module rather
than an addition to `metrics.py`, whose entire contents are score inputs.

Every function is pure: same bars in, same numbers out, no clock and no I/O. A
caller that wants the market state as it stood on a past date passes only the
bars up to that date — the window anchors on the last bar supplied, never on
today.

**Windows are strict.** A feature named `_60` is computed from exactly 60
observations or it is `None`. The alternative — accepting a half-length window
and reporting it in the same column — makes two rows of a time series
incomparable while looking identical, which is precisely the failure a
point-in-time record exists to prevent. There is no "whole history" feature
here for the same reason: its meaning would change every day the database grows.

**Units are in the names.** `_pct` is a percentage (2.5 means 2.5%), `_ratio` is
a dimensionless ratio, `_share` is a fraction of observations expressed as a
percentage, and anything else is stated in its docstring. Returns are simple
returns expressed as percentages; realised volatility alone uses log returns,
because that is the convention it is defined in, and it says so.
"""

from __future__ import annotations

import math
import statistics as st
from datetime import date  # noqa: TC003 — pydantic needs the runtime symbol
from typing import TYPE_CHECKING

from pydantic import Field

if TYPE_CHECKING:
    from collections.abc import Sequence

from domain.metrics import (
    _growth,
    _ordered_bars,
    average_dollar_volume,
    distance_from_52w_high,
    high_52w,
    latest_close,
    low_52w,
)
from domain.models import PriceBar, _Frozen

#: Sessions in a trading year. The long window, and the longest this module
#: looks back. Chosen over "everything stored" so the same column means the same
#: thing on every snapshot date.
YEAR_SESSIONS = 252

#: The short, medium and quarter windows.
WEEK_SESSIONS = 5
MONTH_SESSIONS = 20
QUARTER_SESSIONS = 60

#: Annualisation factor for a daily volatility figure.
_ANNUALISE = math.sqrt(YEAR_SESSIONS)

#: A single-session move at or beyond this magnitude. Recorded as an integrity
#: flag only. It is **not** a split detector: this module never infers a
#: corporate action from a price movement, because an unadjusted split and a
#: genuine collapse are indistinguishable from the bar alone.
_EXTREME_MOVE = 0.40

#: Movement thresholds, as fractions. One space for every threshold feature —
#: the prototype mixed 0.01 and 1 under identically-suffixed names.
_MOVE_THRESHOLDS = (0.01, 0.02, 0.03)


class MarketFeatures(_Frozen):
    """One company's observed market state as of the last bar supplied.

    Every field is `None` when the bars cannot support it, never zero. A `None`
    here means "not enough history for this window", which is a different
    statement from "this security did not move", and the two must not be
    conflated in a dataset a strategy will later be calibrated against.

    Attributes:
        as_of: Date of the last bar the features were computed from. This is the
            feature's own effective date, and it may be earlier than the date
            the caller asked for when a security has stopped trading.
        bars_available: How many bars were supplied, after ordering and
            de-duplication. Records why a window came back `None`.
        last_close: Closing price on `as_of`.
        atr_pct_14: Average true range over 14 sessions, as a percentage of
            `last_close`.
        atr_pct_20: The same over 20 sessions.
        atr_pct_60: The same over 60 sessions.
        rvol_5_pct: Annualised realised volatility from 5 log returns, in
            percent.
        rvol_20_pct: The same over 20.
        rvol_60_pct: The same over 60.
        rvol_252_pct: The same over 252.
        vol_expansion_ratio: `rvol_20_pct / rvol_60_pct`. Above 1 means recent
            volatility exceeds the quarter's baseline.
        avg_abs_ret_20_pct: Mean absolute daily return over 20 sessions.
        avg_abs_ret_60_pct: The same over 60.
        avg_abs_ret_252_pct: The same over 252.
        median_abs_ret_252_pct: Median absolute daily return over 252 sessions.
        max_abs_ret_60_pct: Largest absolute daily return in 60 sessions.
        max_abs_ret_252_pct: The same over 252.
        days_over_1pct_share: Percentage of the last 252 sessions moving more
            than 1% in absolute terms.
        days_over_2pct_share: The same at 2%.
        days_over_3pct_share: The same at 3%.
        median_range_pct: Median intraday range over 252 sessions, as a
            percentage of the previous close.
        range_over_1pct_share: Percentage of the last 252 sessions whose
            intraday range exceeded 1% of the previous close.
        range_over_2pct_share: The same at 2%.
        flat_bar_share: Percentage of the last 252 sessions printing `high ==
            low`. The marker that identifies a security which barely trades — a
            name can carry a high average true range from gaps alone while
            printing an unchanged bar most days.
        zero_move_share: Percentage of the last 252 sessions closing exactly
            unchanged.
        closing_band_pct: The 60-session closing range as a percentage of
            `last_close`. A narrow band is a security going nowhere, which
            volatility measures do not reliably show.
        ret_5d_pct: Simple return over the last 5 sessions, in percent.
        ret_20d_pct: The same over 20.
        ret_60d_pct: The same over 60.
        ret_252d_pct: The same over 252.
        trend_r2: R-squared of log close against session index over 60 sessions,
            0 to 1. Near zero is directionless noise; near one is a clean trend.
        trend_slope: Slope of that fit, in log price per session.
        above_sma20_share: Percentage of the last 60 sessions closing above
            their own trailing 20-session mean.
        high_52w: Highest intraday high over the trailing calendar year.
        low_52w: Lowest intraday low over the trailing calendar year.
        distance_from_52w_high: `close / high_52w - 1`, as a fraction. Negative
            below the high. Computed by `domain.metrics`, on a trailing
            calendar year against intraday highs.
        gap_over_1pct_share: Percentage of the last 252 sessions opening more
            than 1% away from the previous close.
        gap_over_2pct_share: The same at 2%.
        mean_abs_gap_pct: Mean absolute overnight gap over 252 sessions.
        max_abs_gap_pct: Largest absolute overnight gap in 252 sessions.
        venue_dollar_volume_median: Median of `close * volume` over 60 sessions.
            **Single-venue.** Stored bars come from one exchange and are a small
            and highly variable fraction of the consolidated tape, so this is a
            floor on real liquidity and never an estimate of it. Named for the
            venue so it can never be read as consolidated volume.
        venue_dollar_volume_mean: The 20-session mean of the same quantity, from
            `domain.average_dollar_volume`.
        venue_volume_stability: Standard deviation of share volume over 60
            sessions divided by its mean. Higher is lumpier.
        venue_volume_ratio: Mean 20-session volume over mean 60-session volume.
        beta_60: Slope of the company's daily returns on the benchmark's, over
            the last 60 sessions **both** series share. `None` without a
            benchmark.
        correlation_60: Correlation across those same shared sessions.
        rel_strength_20_pct: Company return minus benchmark return over the last
            20 shared sessions, in percentage points. Both legs span the same
            calendar window, so a security whose last bar is stale is not
            measured against a benchmark window it never traded through.
        rel_strength_60_pct: The same over 60 shared sessions.
        has_extreme_move: Whether any single session in the last 252 moved 40%
            or more. An integrity flag for a reader, **not** a corporate action:
            this module never infers a split or a dividend from price movement.
    """

    as_of: date
    bars_available: int = Field(ge=0)
    last_close: float | None = None

    atr_pct_14: float | None = None
    atr_pct_20: float | None = None
    atr_pct_60: float | None = None

    rvol_5_pct: float | None = None
    rvol_20_pct: float | None = None
    rvol_60_pct: float | None = None
    rvol_252_pct: float | None = None
    vol_expansion_ratio: float | None = None

    avg_abs_ret_20_pct: float | None = None
    avg_abs_ret_60_pct: float | None = None
    avg_abs_ret_252_pct: float | None = None
    median_abs_ret_252_pct: float | None = None
    max_abs_ret_60_pct: float | None = None
    max_abs_ret_252_pct: float | None = None

    days_over_1pct_share: float | None = None
    days_over_2pct_share: float | None = None
    days_over_3pct_share: float | None = None

    median_range_pct: float | None = None
    range_over_1pct_share: float | None = None
    range_over_2pct_share: float | None = None

    flat_bar_share: float | None = None
    zero_move_share: float | None = None
    closing_band_pct: float | None = None

    ret_5d_pct: float | None = None
    ret_20d_pct: float | None = None
    ret_60d_pct: float | None = None
    ret_252d_pct: float | None = None

    trend_r2: float | None = None
    trend_slope: float | None = None
    above_sma20_share: float | None = None

    high_52w: float | None = None
    low_52w: float | None = None
    distance_from_52w_high: float | None = None

    gap_over_1pct_share: float | None = None
    gap_over_2pct_share: float | None = None
    mean_abs_gap_pct: float | None = None
    max_abs_gap_pct: float | None = None

    venue_dollar_volume_median: float | None = None
    venue_dollar_volume_mean: float | None = None
    venue_volume_stability: float | None = None
    venue_volume_ratio: float | None = None

    beta_60: float | None = None
    correlation_60: float | None = None
    rel_strength_20_pct: float | None = None
    rel_strength_60_pct: float | None = None

    has_extreme_move: bool = False


def _window(values: Sequence[float], size: int) -> list[float] | None:
    """Return exactly the last `size` values, or None when there are too few.

    Strict by design. A window that quietly shortens itself produces a column
    whose meaning varies between rows, which is unrecoverable once written into
    an immutable snapshot.
    """
    if size <= 0 or len(values) < size:
        return None
    return list(values[-size:])


def _mean(values: Sequence[float]) -> float | None:
    """Arithmetic mean, or None when there is nothing to average."""
    return sum(values) / len(values) if values else None


def _share_above(values: Sequence[float], threshold: float) -> float | None:
    """Percentage of `values` strictly above `threshold`."""
    if not values:
        return None
    return sum(1 for value in values if value > threshold) / len(values) * 100


def _simple_returns(closes: Sequence[float]) -> list[float]:
    """Close-to-close simple returns, skipping any session with a zero base.

    A zero close is permitted by `PriceBar`, and dividing by it would raise. The
    session is dropped rather than recorded as an infinite move.
    """
    returns: list[float] = []
    for index in range(1, len(closes)):
        change = _growth(closes[index], closes[index - 1])
        if change is not None:
            returns.append(change)
    return returns


def _log_returns(closes: Sequence[float]) -> list[float]:
    """Close-to-close log returns, skipping any session with a zero price."""
    return [
        math.log(closes[index] / closes[index - 1])
        for index in range(1, len(closes))
        if closes[index - 1] > 0 and closes[index] > 0
    ]


def _realised_volatility(log_returns: Sequence[float], size: int) -> float | None:
    """Annualised realised volatility over exactly `size` returns, in percent."""
    window = _window(log_returns, size)
    if window is None or len(window) < 2:
        return None
    return st.stdev(window) * _ANNUALISE * 100


def _true_ranges(bars: Sequence[PriceBar]) -> list[float]:
    """True range for every session after the first."""
    return [
        max(
            bars[index].high - bars[index].low,
            abs(bars[index].high - bars[index - 1].close),
            abs(bars[index].low - bars[index - 1].close),
        )
        for index in range(1, len(bars))
    ]


def _atr_pct(true_ranges: Sequence[float], size: int, close: float | None) -> float | None:
    """Mean true range over exactly `size` sessions, as a percentage of `close`."""
    window = _window(true_ranges, size)
    if window is None or not close or close <= 0:
        return None
    average = _mean(window)
    return None if average is None else average / close * 100


def _session_return_pct(closes: Sequence[float], sessions: int) -> float | None:
    """Simple return over exactly `sessions` sessions, in percent.

    Needs `sessions + 1` closes: the baseline is the close that many sessions
    before the last one.
    """
    if len(closes) < sessions + 1:
        return None
    change = _growth(closes[-1], closes[-(sessions + 1)])
    return None if change is None else change * 100


def _trend(closes: Sequence[float], size: int) -> tuple[float | None, float | None]:
    """R-squared and slope of log close against session index over `size` bars."""
    window = _window(closes, size)
    if window is None:
        return None, None
    prices = [math.log(close) for close in window if close > 0]
    if len(prices) < size:
        return None, None

    indices = range(len(prices))
    mean_x = sum(indices) / len(prices)
    mean_y = sum(prices) / len(prices)
    var_x = sum((x - mean_x) ** 2 for x in indices)
    var_y = sum((y - mean_y) ** 2 for y in prices)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(indices, prices, strict=True))
    if var_x == 0 or var_y == 0:
        return None, None
    return covariance**2 / (var_x * var_y), covariance / var_x


def _above_sma_share(closes: Sequence[float], window: int, lookback: int) -> float | None:
    """Percentage of the last `lookback` sessions closing above their own SMA."""
    if len(closes) < window + lookback - 1:
        return None
    flags = [
        1 if closes[index] > sum(closes[index - window + 1 : index + 1]) / window else 0
        for index in range(len(closes) - lookback, len(closes))
    ]
    return sum(flags) / len(flags) * 100


def _aligned_returns(
    bars: Sequence[PriceBar], benchmark: Sequence[PriceBar]
) -> tuple[list[float], list[float], list[date]]:
    """Return the two series' simple returns on the sessions they share.

    Aligning on dates rather than on position is what stops a security whose
    last bar is months old being compared against the benchmark's most recent
    window. Both legs always describe the same calendar span.
    """
    benchmark_closes = {bar.date: bar.close for bar in benchmark}
    company_closes = {bar.date: bar.close for bar in bars}
    shared = sorted(set(benchmark_closes) & set(company_closes))

    company: list[float] = []
    market: list[float] = []
    dates: list[date] = []
    for index in range(1, len(shared)):
        previous, current = shared[index - 1], shared[index]
        company_change = _growth(company_closes[current], company_closes[previous])
        market_change = _growth(benchmark_closes[current], benchmark_closes[previous])
        if company_change is None or market_change is None:
            continue
        company.append(company_change)
        market.append(market_change)
        dates.append(current)
    return company, market, dates


def _beta_and_correlation(
    company: Sequence[float], market: Sequence[float], size: int
) -> tuple[float | None, float | None]:
    """Beta of company returns on market returns, and their correlation."""
    company_window = _window(company, size)
    market_window = _window(market, size)
    if company_window is None or market_window is None:
        return None, None

    mean_c = sum(company_window) / len(company_window)
    mean_m = sum(market_window) / len(market_window)
    cov = sum(
        (c - mean_c) * (m - mean_m) for c, m in zip(company_window, market_window, strict=True)
    )
    var_c = sum((c - mean_c) ** 2 for c in company_window)
    var_m = sum((m - mean_m) ** 2 for m in market_window)
    if var_m == 0 or var_c == 0:
        return None, None
    return cov / var_m, cov / math.sqrt(var_c * var_m)


def _relative_strength_pct(
    company: Sequence[float], market: Sequence[float], size: int
) -> float | None:
    """Compounded company return less benchmark return over `size` shared sessions."""
    company_window = _window(company, size)
    market_window = _window(market, size)
    if company_window is None or market_window is None:
        return None

    def compound(returns: Sequence[float]) -> float:
        total = 1.0
        for value in returns:
            total *= 1 + value
        return total - 1

    return (compound(company_window) - compound(market_window)) * 100


def compute_market_features(
    bars: Sequence[PriceBar],
    *,
    benchmark_bars: Sequence[PriceBar] = (),
) -> MarketFeatures | None:
    """Return the observed market state as of the last bar supplied.

    The caller decides the effective date by deciding which bars to pass. To
    describe a past session, pass only the bars up to it; nothing here reaches
    for a later price.

    Args:
        bars: The company's daily bars. May arrive unsorted or with duplicate
            dates; both are normalised.
        benchmark_bars: The market's daily bars, for beta, correlation and
            relative strength. Omitting them costs those four fields and
            nothing else.

    Returns:
        The features, or None when no bar was supplied at all. Every individual
        field is None when its window cannot be filled.
    """
    ordered = _ordered_bars(bars)
    if not ordered:
        return None

    closes = [bar.close for bar in ordered]
    close = latest_close(ordered)
    true_ranges = _true_ranges(ordered)
    simple = _simple_returns(closes)
    logs = _log_returns(closes)
    absolute = [abs(value) for value in simple]

    year_abs = _window(absolute, YEAR_SESSIONS)
    quarter_abs = _window(absolute, QUARTER_SESSIONS)

    ranges = [
        (ordered[index].high - ordered[index].low) / closes[index - 1]
        for index in range(1, len(ordered))
        if closes[index - 1] > 0
    ]
    gaps = [
        ordered[index].open / closes[index - 1] - 1
        for index in range(1, len(ordered))
        if closes[index - 1] > 0
    ]
    year_ranges = _window(ranges, YEAR_SESSIONS)
    year_gaps = _window(gaps, YEAR_SESSIONS)

    year_bars = ordered[-YEAR_SESSIONS:] if len(ordered) >= YEAR_SESSIONS else []
    flat_share = (
        sum(1 for bar in year_bars if bar.high == bar.low) / len(year_bars) * 100
        if year_bars
        else None
    )
    zero_share = (
        sum(1 for value in year_abs if value == 0) / len(year_abs) * 100 if year_abs else None
    )

    band_window = _window(closes, QUARTER_SESSIONS)
    band = (
        (max(band_window) - min(band_window)) / close * 100
        if band_window is not None and close and close > 0
        else None
    )

    volumes = [bar.volume for bar in ordered]
    dollar_volumes = [bar.close * bar.volume for bar in ordered]
    quarter_dollars = _window(dollar_volumes, QUARTER_SESSIONS)
    quarter_volumes = _window(volumes, QUARTER_SESSIONS)
    month_volumes = _window(volumes, MONTH_SESSIONS)
    quarter_volume_mean = _mean(quarter_volumes) if quarter_volumes is not None else None

    rvol_20 = _realised_volatility(logs, MONTH_SESSIONS)
    rvol_60 = _realised_volatility(logs, QUARTER_SESSIONS)

    company_returns, market_returns, _ = _aligned_returns(ordered, _ordered_bars(benchmark_bars))
    beta, correlation = _beta_and_correlation(company_returns, market_returns, QUARTER_SESSIONS)
    r_squared, slope = _trend(closes, QUARTER_SESSIONS)

    return MarketFeatures(
        as_of=ordered[-1].date,
        bars_available=len(ordered),
        last_close=close,
        atr_pct_14=_atr_pct(true_ranges, 14, close),
        atr_pct_20=_atr_pct(true_ranges, MONTH_SESSIONS, close),
        atr_pct_60=_atr_pct(true_ranges, QUARTER_SESSIONS, close),
        rvol_5_pct=_realised_volatility(logs, WEEK_SESSIONS),
        rvol_20_pct=rvol_20,
        rvol_60_pct=rvol_60,
        rvol_252_pct=_realised_volatility(logs, YEAR_SESSIONS),
        vol_expansion_ratio=(rvol_20 / rvol_60 if rvol_20 is not None and rvol_60 else None),
        avg_abs_ret_20_pct=_percent(_mean(_window(absolute, MONTH_SESSIONS) or [])),
        avg_abs_ret_60_pct=_percent(_mean(quarter_abs or [])),
        avg_abs_ret_252_pct=_percent(_mean(year_abs or [])),
        median_abs_ret_252_pct=(st.median(year_abs) * 100 if year_abs else None),
        max_abs_ret_60_pct=(max(quarter_abs) * 100 if quarter_abs else None),
        max_abs_ret_252_pct=(max(year_abs) * 100 if year_abs else None),
        days_over_1pct_share=_share_above(year_abs or [], _MOVE_THRESHOLDS[0]),
        days_over_2pct_share=_share_above(year_abs or [], _MOVE_THRESHOLDS[1]),
        days_over_3pct_share=_share_above(year_abs or [], _MOVE_THRESHOLDS[2]),
        median_range_pct=(st.median(year_ranges) * 100 if year_ranges else None),
        range_over_1pct_share=_share_above(year_ranges or [], _MOVE_THRESHOLDS[0]),
        range_over_2pct_share=_share_above(year_ranges or [], _MOVE_THRESHOLDS[1]),
        flat_bar_share=flat_share,
        zero_move_share=zero_share,
        closing_band_pct=band,
        ret_5d_pct=_session_return_pct(closes, WEEK_SESSIONS),
        ret_20d_pct=_session_return_pct(closes, MONTH_SESSIONS),
        ret_60d_pct=_session_return_pct(closes, QUARTER_SESSIONS),
        ret_252d_pct=_session_return_pct(closes, YEAR_SESSIONS),
        trend_r2=r_squared,
        trend_slope=slope,
        above_sma20_share=_above_sma_share(closes, MONTH_SESSIONS, QUARTER_SESSIONS),
        high_52w=high_52w(ordered),
        low_52w=low_52w(ordered),
        distance_from_52w_high=distance_from_52w_high(ordered),
        gap_over_1pct_share=_share_above(
            [abs(value) for value in year_gaps or []], _MOVE_THRESHOLDS[0]
        ),
        gap_over_2pct_share=_share_above(
            [abs(value) for value in year_gaps or []], _MOVE_THRESHOLDS[1]
        ),
        mean_abs_gap_pct=_percent(_mean([abs(value) for value in year_gaps or []])),
        max_abs_gap_pct=(max(abs(value) for value in year_gaps) * 100 if year_gaps else None),
        venue_dollar_volume_median=(st.median(quarter_dollars) if quarter_dollars else None),
        venue_dollar_volume_mean=average_dollar_volume(ordered),
        venue_volume_stability=(
            st.stdev(quarter_volumes) / quarter_volume_mean
            if quarter_volumes is not None and len(quarter_volumes) > 1 and quarter_volume_mean
            else None
        ),
        venue_volume_ratio=_volume_ratio(month_volumes, quarter_volume_mean),
        beta_60=beta,
        correlation_60=correlation,
        rel_strength_20_pct=_relative_strength_pct(company_returns, market_returns, MONTH_SESSIONS),
        rel_strength_60_pct=_relative_strength_pct(
            company_returns, market_returns, QUARTER_SESSIONS
        ),
        has_extreme_move=any(value >= _EXTREME_MOVE for value in year_abs or []),
    )


def _volume_ratio(recent: Sequence[float] | None, baseline: float | None) -> float | None:
    """Recent mean volume over a longer-run mean, or None when either is absent."""
    if recent is None or not baseline:
        return None
    average = _mean(recent)
    return None if average is None else average / baseline


def _percent(value: float | None) -> float | None:
    """Scale a fraction to a percentage, preserving None."""
    return None if value is None else value * 100
