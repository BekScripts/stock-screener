"""Market feature arithmetic, against hand-worked numbers.

The point of these is that a window means what its name says. A feature called
`_60` computed from thirty bars would be indistinguishable, in the column, from
one computed from sixty — and two rows of a point-in-time table would then be
incomparable while looking identical. Several tests below exist only to pin that
down.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from domain import compute_market_features
from domain.models import PriceBar


def _bars(
    closes: list[float],
    *,
    start: date = date(2025, 1, 1),
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    volumes: list[float] | None = None,
) -> list[PriceBar]:
    """Build a bar series from closes, defaulting the other fields to the close."""
    return [
        PriceBar(
            date=start + timedelta(days=index),
            open=(opens or closes)[index],
            high=(highs or closes)[index],
            low=(lows or closes)[index],
            close=closes[index],
            volume=(volumes or [1_000.0] * len(closes))[index],
        )
        for index in range(len(closes))
    ]


def _flat(count: int, price: float = 100.0) -> list[float]:
    """A series that does not move."""
    return [price] * count


@pytest.mark.unit
def test_no_bars_produce_no_features() -> None:
    assert compute_market_features([]) is None


@pytest.mark.unit
def test_as_of_is_the_last_bar_not_today() -> None:
    """The caller decides the effective date by choosing the bars."""
    features = compute_market_features(_bars(_flat(10), start=date(2020, 3, 2)))

    assert features is not None
    assert features.as_of == date(2020, 3, 11)
    assert features.bars_available == 10


@pytest.mark.unit
def test_bars_are_ordered_and_deduplicated_before_use() -> None:
    """Providers send bars unsorted and repeated; both are normalised."""
    ordered = _bars([10.0, 11.0, 12.0])
    shuffled = [ordered[2], ordered[0], ordered[1], ordered[2]]

    features = compute_market_features(shuffled)

    assert features is not None
    assert features.as_of == date(2025, 1, 3)
    assert features.bars_available == 3
    assert features.last_close == 12.0


@pytest.mark.unit
def test_a_window_shorter_than_its_name_is_none_not_an_estimate() -> None:
    """59 bars must not produce a value in a column named for 60."""
    features = compute_market_features(_bars(_flat(59)))

    assert features is not None
    assert features.atr_pct_14 is not None
    assert features.atr_pct_60 is None
    assert features.rvol_60_pct is None
    assert features.closing_band_pct is None
    assert features.ret_60d_pct is None


@pytest.mark.unit
def test_the_window_fills_at_exactly_its_named_length() -> None:
    """The boundary is inclusive: 61 closes give exactly 60 sessions of return."""
    features = compute_market_features(_bars(_flat(61)))

    assert features is not None
    assert features.ret_60d_pct == pytest.approx(0.0)
    assert features.atr_pct_60 is not None


@pytest.mark.unit
def test_average_true_range_is_a_percentage_of_the_last_close() -> None:
    """Range of 2 on a close of 100 across 14 sessions is 2%."""
    closes = _flat(20, 100.0)
    features = compute_market_features(_bars(closes, highs=[101.0] * 20, lows=[99.0] * 20))

    assert features is not None
    assert features.atr_pct_14 == pytest.approx(2.0)


@pytest.mark.unit
def test_returns_are_percentages_not_fractions() -> None:
    """A 10% move reads as 10.0, never 0.1."""
    closes = [*_flat(20, 100.0), 110.0]
    features = compute_market_features(_bars(closes))

    assert features is not None
    assert features.ret_5d_pct == pytest.approx(10.0)


@pytest.mark.unit
def test_a_zero_close_is_skipped_rather_than_dividing_by_it() -> None:
    """PriceBar permits a zero close; a return against it is undefined, not infinite."""
    features = compute_market_features(_bars([10.0, 0.0, 10.0, 11.0, *_flat(20)]))

    assert features is not None
    assert features.last_close == 100.0


@pytest.mark.unit
def test_flat_bars_are_counted_where_high_equals_low() -> None:
    """The marker that identifies a security which barely trades."""
    closes = _flat(252, 50.0)
    highs = [50.0] * 252
    lows = [50.0] * 252
    # A quarter of sessions actually traded a range.
    for index in range(0, 252, 4):
        highs[index], lows[index] = 51.0, 49.0

    features = compute_market_features(_bars(closes, highs=highs, lows=lows))

    assert features is not None
    assert features.flat_bar_share == pytest.approx(75.0)


@pytest.mark.unit
def test_a_gappy_security_can_carry_a_high_atr_while_barely_trading() -> None:
    """Why flat-bar share exists: average true range does not catch this."""
    closes = [100.0 + (4.0 if index % 2 else 0.0) for index in range(252)]
    features = compute_market_features(_bars(closes, highs=closes, lows=closes, opens=closes))

    assert features is not None
    assert features.atr_pct_14 is not None
    assert features.atr_pct_14 > 3.0
    assert features.flat_bar_share == pytest.approx(100.0)


@pytest.mark.unit
def test_closing_band_measures_a_security_going_nowhere() -> None:
    """Band is the 60-session closing range over the last close."""
    closes = [*_flat(59, 100.0), 110.0]
    features = compute_market_features(_bars(closes))

    assert features is not None
    # Range 100..110 against a last close of 110.
    assert features.closing_band_pct == pytest.approx(10 / 110 * 100)


@pytest.mark.unit
def test_movement_thresholds_share_one_numeric_space() -> None:
    """Daily-move and intraday-range shares are both fractions of the prior close."""
    closes = [100.0]
    for index in range(1, 253):
        closes.append(closes[-1] * (1.02 if index % 2 else 1 / 1.02))
    features = compute_market_features(_bars(closes))

    assert features is not None
    assert features.days_over_1pct_share == pytest.approx(100.0)
    assert features.days_over_3pct_share == pytest.approx(0.0)


@pytest.mark.unit
def test_trend_r2_separates_a_clean_trend_from_noise() -> None:
    """A monotone series fits; an alternating one does not."""
    trending = compute_market_features(_bars([100.0 * (1.01**index) for index in range(60)]))
    noisy = compute_market_features(
        _bars([100.0 + (5.0 if index % 2 else -5.0) for index in range(60)])
    )

    assert trending is not None
    assert noisy is not None
    assert trending.trend_r2 == pytest.approx(1.0)
    assert noisy.trend_r2 is not None
    assert noisy.trend_r2 < 0.05


@pytest.mark.unit
def test_a_benchmark_is_optional_and_costs_only_the_four_market_fields() -> None:
    bars = _bars([100.0 * (1.001**index) for index in range(120)])

    features = compute_market_features(bars)

    assert features is not None
    assert features.beta_60 is None
    assert features.correlation_60 is None
    assert features.rel_strength_20_pct is None
    assert features.rel_strength_60_pct is None
    assert features.atr_pct_14 is not None
    assert features.rvol_60_pct is not None


@pytest.mark.unit
def test_beta_of_a_security_that_moves_with_the_market_is_one() -> None:
    market = _bars([100.0 * (1.005 if index % 2 else 0.995) ** 1 for index in range(80)])
    identical = _bars([bar.close for bar in market])

    features = compute_market_features(identical, benchmark_bars=market)

    assert features is not None
    assert features.beta_60 == pytest.approx(1.0)
    assert features.correlation_60 == pytest.approx(1.0)
    assert features.rel_strength_60_pct == pytest.approx(0.0)


@pytest.mark.unit
def test_market_relative_figures_align_on_shared_sessions() -> None:
    """A stale security is not measured against a window it never traded through.

    The company stops trading 40 sessions before the benchmark does. Its
    relative strength must be computed over sessions both were open, not over
    the benchmark's most recent 60.
    """
    market_closes = [100.0 * (1.002**index) for index in range(160)]
    market = _bars(market_closes)
    company = _bars(market_closes[:120])

    features = compute_market_features(company, benchmark_bars=market)

    assert features is not None
    assert features.as_of == date(2025, 4, 30)
    # Identical prices on every shared session, so no relative strength at all.
    assert features.rel_strength_60_pct == pytest.approx(0.0)
    assert features.beta_60 == pytest.approx(1.0)


@pytest.mark.unit
def test_an_extreme_move_is_flagged_and_never_called_a_split() -> None:
    """The flag is for a reader. Nothing in this project infers a corporate action."""
    closes = [*_flat(100, 100.0), 50.0, *_flat(160, 50.0)]
    features = compute_market_features(_bars(closes))

    assert features is not None
    assert features.has_extreme_move is True


@pytest.mark.unit
def test_volume_fields_are_named_for_the_venue_not_for_liquidity() -> None:
    """Stored bars are single-venue; the names must not invite a liquidity read."""
    features = compute_market_features(_bars(_flat(60, 10.0), volumes=[2_000.0] * 60))

    assert features is not None
    assert features.venue_dollar_volume_median == pytest.approx(20_000.0)
    assert not hasattr(features, "dollar_volume")
    assert not hasattr(features, "liquidity")
