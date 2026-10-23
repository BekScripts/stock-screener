"""Market-signal classification, percentiles and the tri-state verdict.

Two properties get the most attention here, because both were falsified
assumptions in Phase 8A. That a high average true range does not imply a
tradable security — HBB carried 3.91% while printing an unchanged bar on 59.9%
of sessions — and that missing evidence is its own answer rather than a soft no.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain import (
    MIN_PEER_SAMPLE,
    PERCENTILE_FEATURES,
    MarketFeatures,
    Setup,
    SignalThresholds,
    Tradability,
    TrendDirection,
    assess_tradability,
    build_peer_context,
    classify_setups,
    compression_ratio,
    derive_signals,
    distance_from_52w_low,
    stretch_20d,
    trend_direction,
)

AS_OF = date(2026, 8, 26)


def _features(**overrides: object) -> MarketFeatures:
    """A liquid, unremarkable security. Overrides make it interesting."""
    base: dict[str, object] = {
        "as_of": AS_OF,
        "bars_available": 282,
        "last_close": 100.0,
        "atr_pct_14": 2.5,
        "atr_pct_20": 2.5,
        "atr_pct_60": 2.5,
        "rvol_20_pct": 30.0,
        "rvol_60_pct": 30.0,
        "vol_expansion_ratio": 1.0,
        "ret_20d_pct": 1.0,
        "ret_60d_pct": 5.0,
        "ret_252d_pct": 10.0,
        "rel_strength_60_pct": 2.0,
        "trend_r2": 0.30,
        "trend_slope": 0.001,
        "above_sma20_share": 55.0,
        "high_52w": 120.0,
        "low_52w": 80.0,
        "distance_from_52w_high": -0.05,
        "flat_bar_share": 0.0,
        "zero_move_share": 0.0,
        "median_range_pct": 2.0,
        "closing_band_pct": 25.0,
        "venue_dollar_volume_median": 5_000_000.0,
    }
    base.update(overrides)
    return MarketFeatures(**base)  # type: ignore[arg-type]


def _peers(count: int = 100, **series: list[float]) -> object:
    """A peer context built from `count` synthetic companies."""
    population = [
        _features(atr_pct_14=1.0 + index / 100, ret_20d_pct=float(index)) for index in range(count)
    ]
    return build_peer_context(population)


# ---------------------------------------------------------------------------
# Derived facts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compression_ratio_is_short_over_long_range() -> None:
    assert compression_ratio(_features(atr_pct_20=1.4, atr_pct_60=2.0)) == pytest.approx(0.7)


@pytest.mark.unit
def test_compression_is_none_when_either_range_is_missing() -> None:
    assert compression_ratio(_features(atr_pct_20=None)) is None
    assert compression_ratio(_features(atr_pct_60=None)) is None
    assert compression_ratio(_features(atr_pct_60=0.0)) is None


@pytest.mark.unit
def test_distance_from_the_low_is_derived_from_stored_values() -> None:
    """Not a new bar computation — a ratio of two numbers already stored."""
    assert distance_from_52w_low(_features(last_close=120.0, low_52w=80.0)) == pytest.approx(0.5)
    assert distance_from_52w_low(_features(low_52w=None)) is None
    assert distance_from_52w_low(_features(low_52w=0.0)) is None


@pytest.mark.unit
def test_stretch_is_the_move_in_units_of_the_securitys_own_volatility() -> None:
    """A 10% fall means something different for a utility and for a biotech."""
    quiet = stretch_20d(_features(ret_20d_pct=-10.0, rvol_20_pct=20.0))
    wild = stretch_20d(_features(ret_20d_pct=-10.0, rvol_20_pct=80.0))

    assert quiet is not None
    assert wild is not None
    assert quiet < wild < 0
    assert quiet == pytest.approx(-10.0 / (20.0 * (20 / 252) ** 0.5))


@pytest.mark.unit
def test_stretch_is_none_without_volatility_to_normalise_by() -> None:
    assert stretch_20d(_features(rvol_20_pct=None)) is None
    assert stretch_20d(_features(rvol_20_pct=0.0)) is None
    assert stretch_20d(_features(ret_20d_pct=None)) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("slope", "expected"),
    [
        (0.01, TrendDirection.UP),
        (-0.01, TrendDirection.DOWN),
        (0.0, TrendDirection.FLAT),
        (None, TrendDirection.UNKNOWN),
    ],
)
def test_trend_direction_reports_the_sign_only(
    slope: float | None, expected: TrendDirection
) -> None:
    assert trend_direction(_features(trend_slope=slope)) is expected


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_percentiles_are_uniform_over_a_uniform_population() -> None:
    peers = build_peer_context([_features(atr_pct_14=float(index)) for index in range(100)])

    assert peers.percentile("atr_pct_14", 0.0) == pytest.approx(0.5)
    assert peers.percentile("atr_pct_14", 50.0) == pytest.approx(50.5)
    assert peers.percentile("atr_pct_14", 99.0) == pytest.approx(99.5)


@pytest.mark.unit
def test_ties_share_one_percentile_and_do_not_depend_on_order() -> None:
    """Two securities with the same value must not be separated by sorting."""
    values = [1.0] * 40 + [2.0] * 40
    forward = build_peer_context([_features(atr_pct_14=v) for v in values])
    backward = build_peer_context([_features(atr_pct_14=v) for v in reversed(values)])

    assert forward.percentile("atr_pct_14", 1.0) == backward.percentile("atr_pct_14", 1.0)
    assert forward.percentile("atr_pct_14", 1.0) == pytest.approx(25.0)
    assert forward.percentile("atr_pct_14", 2.0) == pytest.approx(75.0)


@pytest.mark.unit
def test_a_missing_value_has_no_percentile() -> None:
    peers = build_peer_context([_features(atr_pct_14=float(i)) for i in range(100)])

    assert peers.percentile("atr_pct_14", None) is None


@pytest.mark.unit
def test_a_sample_below_the_minimum_yields_no_percentile() -> None:
    """A percentile over eight names says more about the sample than the security."""
    small = build_peer_context(
        [_features(atr_pct_14=float(index)) for index in range(MIN_PEER_SAMPLE - 1)]
    )
    enough = build_peer_context(
        [_features(atr_pct_14=float(index)) for index in range(MIN_PEER_SAMPLE)]
    )

    assert small.percentile("atr_pct_14", 5.0) is None
    assert enough.percentile("atr_pct_14", 5.0) is not None


@pytest.mark.unit
def test_null_values_are_excluded_from_the_peer_series_not_treated_as_zero() -> None:
    peers = build_peer_context(
        [_features(atr_pct_14=None) for _ in range(50)]
        + [_features(atr_pct_14=float(index)) for index in range(50)]
    )

    assert peers.population == 100
    assert len(peers.sorted_values["atr_pct_14"]) == 50


@pytest.mark.unit
def test_an_unranked_feature_has_no_percentile() -> None:
    peers = build_peer_context([_features() for _ in range(50)])

    assert peers.percentile("flat_bar_share", 10.0) is None
    assert "flat_bar_share" not in PERCENTILE_FEATURES


# ---------------------------------------------------------------------------
# Setup classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_clean_uptrend_is_momentum() -> None:
    setups, evidence = classify_setups(
        _features(
            ret_60d_pct=25.0,
            rel_strength_60_pct=18.0,
            trend_r2=0.80,
            atr_pct_14=3.0,
            above_sma20_share=85.0,
            distance_from_52w_high=-0.02,
        )
    )

    assert Setup.MOMENTUM in setups
    assert evidence and "60d return" in evidence[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "override",
    [
        {"ret_60d_pct": -1.0},
        {"rel_strength_60_pct": -1.0},
        {"trend_r2": 0.49},
        {"atr_pct_14": 1.4},
        {"above_sma20_share": 59.0},
        {"distance_from_52w_high": -0.11},
    ],
)
def test_momentum_needs_every_condition(override: dict[str, float]) -> None:
    """Each threshold is load-bearing; failing any one drops the label."""
    base = {
        "ret_60d_pct": 25.0,
        "rel_strength_60_pct": 18.0,
        "trend_r2": 0.80,
        "atr_pct_14": 3.0,
        "above_sma20_share": 85.0,
        "distance_from_52w_high": -0.02,
    }
    setups, _ = classify_setups(_features(**{**base, **override}))

    assert Setup.MOMENTUM not in setups


@pytest.mark.unit
def test_a_missing_input_never_counts_as_a_pass() -> None:
    """An unknown trend fit is not a good one."""
    setups, _ = classify_setups(
        _features(
            ret_60d_pct=25.0,
            rel_strength_60_pct=18.0,
            trend_r2=None,
            atr_pct_14=3.0,
            above_sma20_share=85.0,
            distance_from_52w_high=-0.02,
        )
    )

    assert Setup.MOMENTUM not in setups


@pytest.mark.unit
def test_a_sharp_fall_against_a_positive_long_trend_is_mean_reversion() -> None:
    setups, _ = classify_setups(_features(ret_20d_pct=-15.0, rvol_20_pct=40.0, ret_252d_pct=30.0))

    assert Setup.MEAN_REVERSION in setups


@pytest.mark.unit
def test_a_fall_in_a_downtrend_is_not_mean_reversion() -> None:
    """The long trend must still be up; otherwise it is just falling."""
    setups, _ = classify_setups(_features(ret_20d_pct=-15.0, rvol_20_pct=40.0, ret_252d_pct=-30.0))

    assert Setup.MEAN_REVERSION not in setups


@pytest.mark.unit
def test_a_tightening_range_is_compression() -> None:
    setups, _ = classify_setups(_features(atr_pct_20=1.2, atr_pct_60=2.0))

    assert Setup.COMPRESSION in setups


@pytest.mark.unit
def test_a_security_can_carry_more_than_one_setup() -> None:
    """Multi-label by design: compressing inside an uptrend is both, not a winner."""
    setups, evidence = classify_setups(
        _features(
            ret_60d_pct=25.0,
            rel_strength_60_pct=18.0,
            trend_r2=0.80,
            atr_pct_14=3.0,
            above_sma20_share=85.0,
            distance_from_52w_high=-0.02,
            atr_pct_20=1.2,
            atr_pct_60=2.0,
        )
    )

    assert Setup.MOMENTUM in setups
    assert Setup.COMPRESSION in setups
    assert len(evidence) == len(setups)


@pytest.mark.unit
def test_nothing_describable_is_none_not_a_forced_label() -> None:
    setups, evidence = classify_setups(_features())

    assert setups == (Setup.NONE,)
    assert evidence == ()


@pytest.mark.unit
def test_none_never_appears_beside_a_real_setup() -> None:
    setups, _ = classify_setups(_features(atr_pct_20=1.2, atr_pct_60=2.0))

    assert Setup.NONE not in setups


@pytest.mark.unit
def test_thresholds_are_parameters_not_constants() -> None:
    """A future version calibrates by passing different thresholds, not by editing rules."""
    quiet = _features(atr_pct_20=1.8, atr_pct_60=2.0)

    assert Setup.COMPRESSION not in classify_setups(quiet)[0]
    assert (
        Setup.COMPRESSION in classify_setups(quiet, SignalThresholds(compression_max_ratio=0.95))[0]
    )


# ---------------------------------------------------------------------------
# Tradability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_liquid_active_security_is_tradable() -> None:
    verdict, reasons, proven = assess_tradability(_features())

    assert verdict is Tradability.TRADABLE
    assert proven is True
    assert reasons


@pytest.mark.unit
def test_a_dead_tape_is_untradable_however_high_its_atr() -> None:
    """HBB: 3.91% average true range assembled from gaps, unchanged 59.9% of days."""
    verdict, reasons, proven = assess_tradability(
        _features(
            atr_pct_14=3.91,
            flat_bar_share=59.9,
            zero_move_share=31.7,
            median_range_pct=0.0,
            venue_dollar_volume_median=50_000_000.0,
        )
    )

    assert verdict is Tradability.UNTRADABLE
    assert proven is False
    assert any("unchanged high and low" in reason for reason in reasons)


@pytest.mark.unit
def test_dead_tape_evidence_outranks_a_proven_liquidity_floor() -> None:
    """Volume cannot rescue a security that does not move within its sessions."""
    verdict, _, _ = assess_tradability(
        _features(flat_bar_share=95.0, venue_dollar_volume_median=1e9)
    )

    assert verdict is Tradability.UNTRADABLE


@pytest.mark.unit
def test_thin_venue_volume_is_unverified_never_untradable() -> None:
    """Stored volume is one venue and a strict subset of the tape.

    Clearing the floor proves a minimum. Failing it proves nothing at all, so it
    can never be evidence against the security.
    """
    verdict, reasons, proven = assess_tradability(_features(venue_dollar_volume_median=1_000.0))

    assert verdict is Tradability.UNVERIFIED
    assert proven is False
    assert any("proves nothing either way" in reason for reason in reasons)


@pytest.mark.unit
def test_absent_volume_is_unverified() -> None:
    verdict, _, proven = assess_tradability(_features(venue_dollar_volume_median=None))

    assert verdict is Tradability.UNVERIFIED
    assert proven is False


@pytest.mark.unit
def test_too_little_history_is_unverified() -> None:
    verdict, reasons, _ = assess_tradability(_features(bars_available=40))

    assert verdict is Tradability.UNVERIFIED
    assert any("below the" in reason for reason in reasons)


@pytest.mark.unit
def test_a_narrow_closing_band_is_positive_evidence_against() -> None:
    verdict, _, _ = assess_tradability(_features(closing_band_pct=3.0))

    assert verdict is Tradability.UNTRADABLE


@pytest.mark.unit
def test_every_verdict_carries_its_reasons() -> None:
    for override in ({}, {"flat_bar_share": 95.0}, {"venue_dollar_volume_median": 1.0}):
        _, reasons, _ = assess_tradability(_features(**override))
        assert reasons


@pytest.mark.unit
def test_missing_data_never_becomes_a_verdict_in_either_direction() -> None:
    """The rule the three-state model exists to enforce."""
    verdict, _, _ = assess_tradability(
        _features(
            flat_bar_share=None,
            zero_move_share=None,
            median_range_pct=None,
            closing_band_pct=None,
            venue_dollar_volume_median=None,
        )
    )

    assert verdict is Tradability.UNVERIFIED


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_derive_signals_is_pure_and_repeatable() -> None:
    features = _features(atr_pct_20=1.2, atr_pct_60=2.0)
    peers = build_peer_context([_features(atr_pct_14=float(i)) for i in range(100)])

    first = derive_signals(features, peers)
    second = derive_signals(features, peers)

    assert first == second


@pytest.mark.unit
def test_a_momentum_name_on_a_dead_tape_reports_both_facts() -> None:
    """Deliberate: the setup describes price, the verdict describes execution.

    HBB rose while its tape was dead. Suppressing either fact would hide
    something true; a strategy layer is expected to read both.
    """
    peers = build_peer_context([_features(atr_pct_14=float(i)) for i in range(100)])
    signals = derive_signals(
        _features(
            ret_60d_pct=40.0,
            rel_strength_60_pct=35.0,
            trend_r2=0.77,
            atr_pct_14=3.91,
            above_sma20_share=90.0,
            distance_from_52w_high=-0.04,
            flat_bar_share=59.9,
        ),
        peers,
    )

    assert Setup.MOMENTUM in signals.setups
    assert signals.tradability is Tradability.UNTRADABLE


@pytest.mark.unit
def test_signals_carry_no_composite_and_no_ranking() -> None:
    """Phase 8A falsified the scalar. The absence is asserted, not assumed."""
    peers = build_peer_context([_features(atr_pct_14=float(i)) for i in range(100)])
    signals = derive_signals(_features(), peers)

    forbidden = {"score", "rank", "opportunity_score", "total", "composite", "weight"}
    assert not {name.lower() for name in type(signals).model_fields} & forbidden


@pytest.mark.unit
def test_the_watermark_travels_with_the_signal() -> None:
    peers = build_peer_context([_features(atr_pct_14=float(i)) for i in range(100)])
    signals = derive_signals(_features(as_of=date(2026, 8, 20)), peers)

    assert signals.feature_as_of == date(2026, 8, 20)
