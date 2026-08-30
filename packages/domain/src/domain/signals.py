"""Market signals: what a security is doing, as structured facts.

This layer describes market *state*. It does not rank, it does not score, and it
does not recommend. Phase 8A established why each of those is excluded:

**No composite.** The dimensions this layer reports are anti-correlated —
volatility against distance-from-high measured Spearman -0.58, with a top-100
overlap of two names out of a hundred. Averaging them into one number ranks
highest the securities that are mediocre at everything, which is worse than
useless because it looks meaningful. So there is no `OpportunityScore` here and
there must never be one.

**No blend with CompounderScore.** A 50/50 percentile blend of the two retained
one of the fundamental top twenty. The two answer different questions and a
future strategy layer decides how to weigh them; this layer only supplies facts.

**No claim of profitability.** Every threshold below is provisional. The
point-in-time record holds a single day, which cannot support calibration, so
nothing here has been fitted to returns and nothing here should be read as
evidence that a setup works. The thresholds exist to make a state *nameable*,
not to assert it is tradeable at a profit.

Three kinds of output:

* **Derived facts** — quantities computed from stored features, such as the
  distance from the 52-week low or the compression ratio.
* **Peer percentiles** — where a security sits against its peers on the same
  date, because a 3% average true range means different things in different
  parts of the market.
* **Classifications** — a multi-label setup, and a tri-state tradability
  verdict that distinguishes "we know it is not tradable" from "we cannot tell".

Every function here is pure. The caller supplies the features and the peer
context; nothing reaches for a clock, a database or a later bar.
"""

from __future__ import annotations

import math
from collections.abc import Mapping  # noqa: TC003 - pydantic needs the runtime symbol
from datetime import date  # noqa: TC003 - pydantic needs the runtime symbol
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from domain.models import _Frozen

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from domain.features import MarketFeatures

#: The signal definitions in force. Every persisted signal row carries it.
#:
#: Bump this — never edit a version in place — when a threshold, a
#: classification rule, the peer policy or the percentile method changes. Two
#: rows sharing a version are comparable; two rows under different versions are
#: not, exactly as with `score_version`. See ADR-0011.
MARKET_SIGNALS_V1 = "MARKET_SIGNALS_V1"

CURRENT_SIGNAL_VERSION = MARKET_SIGNALS_V1

#: Sessions in a trading year, matching `domain.features`.
_YEAR_SESSIONS = 252

#: Below this many peers a cross-sectional percentile says more about the sample
#: than the security, so it is reported as None. A percentile over eight names
#: is not a percentile.
MIN_PEER_SAMPLE = 30


class Setup(StrEnum):
    """A named market state, describing behaviour and asserting nothing more.

    Multi-label by design: a security can be compressing *and* in an uptrend,
    and collapsing that into one winner would discard the more informative half.
    None of these is a recommendation, a ranking or a claim of profitability.
    """

    MOMENTUM = "MOMENTUM"
    """Trending up, confirmed against the benchmark, with enough daily range to
    act on and a trend fit that is not noise."""

    MEAN_REVERSION = "MEAN_REVERSION"
    """Stretched downward against its own recent volatility while its longer
    trend is still positive."""

    COMPRESSION = "COMPRESSION"
    """Recent range materially tighter than its own quarter baseline."""

    NONE = "NONE"
    """No named state applies. A valid and common answer — most securities are
    doing nothing describable on most days, and forcing a label would be noise."""


class Tradability(StrEnum):
    """Whether the security can be traded, or whether that is simply unknown.

    Three states rather than a boolean, because the honest answer for most of
    this universe is the third one. Collapsing `UNVERIFIED` into either
    neighbour manufactures a certainty the data does not support: into
    `UNTRADABLE` it would condemn securities for being unmeasured, and into
    `TRADABLE` it would licence trading something never shown to trade.
    """

    TRADABLE = "TRADABLE"
    """Positive evidence: activity is real and a liquidity floor is proven."""

    UNTRADABLE = "UNTRADABLE"
    """Positive evidence the other way — a tape that is substantially dead."""

    UNVERIFIED = "UNVERIFIED"
    """Not enough evidence to say either. Never a synonym for bad."""


class TrendDirection(StrEnum):
    """Sign of the fitted trend, reported separately from its quality."""

    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class SignalThresholds(_Frozen):
    """Every cut-off the classifier applies, in one versioned, overridable place.

    **All of these are provisional.** They come from the Phase 8A exploratory
    buckets, which were chosen to be conventional and defensible rather than
    optimised, and which that report labelled "ANALYTICAL BUCKETS, NOT
    PRODUCTION RULES". None has been fitted to returns, because the
    point-in-time record holds one day and fitting against it would produce a
    number that looks validated and is not.

    They live in a model rather than as module constants so that a future phase
    with real history can calibrate by constructing a different instance under a
    new `MARKET_SIGNALS_V2`, without any of the classification code changing.

    Attributes:
        momentum_min_trend_r2: Minimum trend fit. 8A used 0.50.
        momentum_min_atr_pct: Minimum daily range to be worth acting on, in
            percent. 8A used 1.5.
        momentum_min_above_sma20_share: Minimum share of the last 60 sessions
            closing above their own 20-session mean, in percent. Stands in for
            8A's `close > SMA50 and SMA20 > SMA50`, which needs moving averages
            the stored feature set does not carry.
        momentum_max_drawdown_from_high: How far below the 52-week high a
            momentum name may sit, as a fraction. 8A used `close >= 0.90 x 60d
            high`; this applies the same 10% to the 52-week high, which is the
            window the stored feature covers and is therefore stricter.
        mean_reversion_max_stretch: How many standard deviations of its own
            20-session volatility a security must have fallen. 8A used a
            20-day z-score of -1.0.
        mean_reversion_min_long_return_pct: The longer trend must still be
            positive, standing in for 8A's `close > SMA100`.
        compression_max_ratio: 20-session over 60-session average true range.
            8A used 0.70.
        untradable_min_flat_bar_share: Flat-bar share, in percent, at or above
            which the tape is judged dead. HBB — the worked example — printed
            `high == low` on 59.9% of sessions while carrying an unremarkable
            average true range.
        untradable_min_zero_move_share: Zero-move share, in percent, treated the
            same way.
        untradable_max_closing_band_pct: A 60-session closing band this narrow,
            in percent, is a security going nowhere.
        untradable_max_median_range_pct: A median intraday range at or below
            this, in percent, means most sessions had no range at all.
        tradable_min_venue_dollar_volume: Dollar volume on the stored venue at
            or above which liquidity is *proven*. Because stored volume is a
            strict subset of the consolidated tape, clearing this floor
            establishes a minimum; failing it establishes nothing.
        min_bars_for_verdict: Below this many sessions, nothing is verified.
    """

    momentum_min_trend_r2: float = Field(default=0.50, ge=0, le=1)
    momentum_min_atr_pct: float = Field(default=1.5, gt=0)
    momentum_min_above_sma20_share: float = Field(default=60.0, ge=0, le=100)
    momentum_max_drawdown_from_high: float = Field(default=-0.10, le=0)

    mean_reversion_max_stretch: float = Field(default=-1.0, lt=0)
    mean_reversion_min_long_return_pct: float = Field(default=0.0)

    compression_max_ratio: float = Field(default=0.70, gt=0)

    untradable_min_flat_bar_share: float = Field(default=20.0, ge=0, le=100)
    untradable_min_zero_move_share: float = Field(default=20.0, ge=0, le=100)
    untradable_max_closing_band_pct: float = Field(default=5.0, ge=0)
    untradable_max_median_range_pct: float = Field(default=0.10, ge=0)

    tradable_min_venue_dollar_volume: float = Field(default=1_000_000.0, gt=0)
    min_bars_for_verdict: int = Field(default=60, gt=0)


#: The thresholds `MARKET_SIGNALS_V1` is defined by. Frozen with the version.
DEFAULT_THRESHOLDS = SignalThresholds()

#: Features the peer percentiles are computed over. Deliberately short: each is
#: a quantity where "compared to what?" is the natural next question, and adding
#: one changes the signal version because a stored row would gain a column.
PERCENTILE_FEATURES: tuple[str, ...] = (
    "atr_pct_14",
    "rvol_60_pct",
    "avg_abs_ret_252_pct",
    "ret_20d_pct",
    "ret_60d_pct",
    "rel_strength_60_pct",
    "trend_r2",
    "distance_from_52w_high",
    "venue_dollar_volume_median",
)


class PeerContext(_Frozen):
    """The cross-section one snapshot date's percentiles are measured against.

    **The peer population is every company with a feature snapshot on this date
    under this feature version.** Not a sector — Phase 8A measured sector as
    missing for 89% of companies, and a sector-relative percentile over the
    remaining 11% would be a statement about coverage rather than about the
    market. Not the rank-eligible set either, because eligibility is a
    fundamental judgement and this layer is deliberately independent of it.

    A security is compared against everything the system was watching that day,
    which is a population that can be stated in one sentence and reproduced
    exactly.

    Attributes:
        sorted_values: Each feature's non-null values across the population, in
            ascending order.
        population: How many companies the context was built from, before
            per-feature null filtering.
    """

    sorted_values: Mapping[str, tuple[float, ...]]
    population: int = Field(ge=0)

    def percentile(self, feature: str, value: float | None) -> float | None:
        """Return where a value sits among its peers, 0 to 100.

        Uses the mid-rank convention: the fraction strictly below, plus half the
        fraction equal. Ties therefore share one percentile and the result is
        deterministic regardless of input order — two securities with identical
        average true range cannot be separated by an accident of sorting.

        Args:
            feature: Which feature to rank against.
            value: The security's value. None returns None.

        Returns:
            The percentile, or None when the value is missing, the feature is
            not ranked, or the peer sample is below `MIN_PEER_SAMPLE`.
        """
        if value is None:
            return None
        values = self.sorted_values.get(feature)
        if values is None or len(values) < MIN_PEER_SAMPLE:
            return None

        below = _count_below(values, value)
        equal = _count_below(values, value, inclusive=True) - below
        return (below + equal / 2) / len(values) * 100


def _count_below(values: Sequence[float], target: float, *, inclusive: bool = False) -> int:
    """Return how many sorted values fall below `target`, by binary search."""
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] < target or (inclusive and values[middle] == target):
            low = middle + 1
        else:
            high = middle
    return low


def build_peer_context(features: Iterable[MarketFeatures]) -> PeerContext:
    """Return the cross-section for one snapshot date.

    Args:
        features: Every company's features for that date. The caller is
            responsible for supplying one date's worth and no more — mixing
            dates would compare a security against a market that had moved.

    Returns:
        The context. A feature whose values are all missing simply has an empty
        series, and every percentile against it comes back None.
    """
    collected: dict[str, list[float]] = {name: [] for name in PERCENTILE_FEATURES}
    population = 0
    for item in features:
        population += 1
        for name in PERCENTILE_FEATURES:
            value = getattr(item, name, None)
            if value is not None:
                collected[name].append(float(value))
    return PeerContext(
        sorted_values={name: tuple(sorted(values)) for name, values in collected.items()},
        population=population,
    )


class MarketSignals(_Frozen):
    """One security's market state on one date: facts, percentiles, verdicts.

    Attributes:
        signal_version: The definitions this row was produced under.
        feature_as_of: Date of the newest bar the underlying features used.
            Carried forward so a reader never has to join back to learn the
            watermark, and so a stale series is visible in the signal itself.
        bars_available: Sessions the features were computed from.
        compression_ratio: 20-session over 60-session average true range. Below
            one means the recent range is tighter than the quarter's baseline.
        volatility_expansion_ratio: 20-session over 60-session realised
            volatility, taken straight from the features.
        distance_from_52w_high: Fraction below the trailing-year intraday high.
            Negative below it.
        distance_from_52w_low: Fraction above the trailing-year intraday low,
            derived here from the stored close and low.
        stretch_20d: The 20-session return expressed in standard deviations of
            the security's own 20-session volatility. Negative is a fall. This
            is the standardisation that stands in for Phase 8A's 20-day
            z-score, which the stored feature set does not carry directly.
        trend_direction: Sign of the fitted log-price slope.
        trend_r2: Quality of that fit, 0 to 1, carried from the features.
        percentiles: Peer percentile per ranked feature, 0 to 100. A missing
            entry means the value or the sample was insufficient.
        setups: Every named state that applies, or `(NONE,)`.
        setup_evidence: Why each setup fired, in the order they are listed.
        tradability: The tri-state verdict.
        tradability_reasons: The evidence behind the verdict, always populated.
        liquidity_floor_proven: Whether stored venue volume establishes a
            minimum consolidated dollar volume. False never means illiquid — it
            means unproven.
    """

    signal_version: str = CURRENT_SIGNAL_VERSION
    feature_as_of: date | None = None
    bars_available: int = Field(default=0, ge=0)

    compression_ratio: float | None = None
    volatility_expansion_ratio: float | None = None
    distance_from_52w_high: float | None = None
    distance_from_52w_low: float | None = None
    stretch_20d: float | None = None
    trend_direction: TrendDirection = TrendDirection.UNKNOWN
    trend_r2: float | None = None

    percentiles: Mapping[str, float] = Field(default_factory=dict)

    setups: tuple[Setup, ...] = (Setup.NONE,)
    setup_evidence: tuple[str, ...] = ()

    tradability: Tradability = Tradability.UNVERIFIED
    tradability_reasons: tuple[str, ...] = ()
    liquidity_floor_proven: bool = False


def compression_ratio(features: MarketFeatures) -> float | None:
    """Return 20-session over 60-session average true range."""
    if features.atr_pct_20 is None or not features.atr_pct_60:
        return None
    return features.atr_pct_20 / features.atr_pct_60


def distance_from_52w_low(features: MarketFeatures) -> float | None:
    """Return how far above the trailing-year low the close sits, as a fraction.

    Derived rather than stored: `domain.metrics` publishes the low and the
    close, and the ratio between two stored numbers is not worth a feature
    column or a re-capture.
    """
    if features.last_close is None or not features.low_52w or features.low_52w <= 0:
        return None
    return features.last_close / features.low_52w - 1


def stretch_20d(features: MarketFeatures) -> float | None:
    """Return the 20-session return in units of the security's own volatility.

    Realised volatility is annualised, so it is rescaled to a 20-session horizon
    before the comparison. The result is the standardised move Phase 8A's
    mean-reversion bucket called a 20-day z-score: -1 is a one-sigma fall for
    *this* security, which is a different absolute move for a utility and for a
    biotech.

    Returns None when either input is missing or volatility is zero — a
    security that has not moved at all cannot be stretched.
    """
    if features.ret_20d_pct is None or not features.rvol_20_pct:
        return None
    horizon = features.rvol_20_pct * math.sqrt(20 / _YEAR_SESSIONS)
    if horizon <= 0:
        return None
    return features.ret_20d_pct / horizon


def trend_direction(features: MarketFeatures) -> TrendDirection:
    """Return the sign of the fitted trend, or UNKNOWN when there is no fit."""
    if features.trend_slope is None:
        return TrendDirection.UNKNOWN
    if features.trend_slope > 0:
        return TrendDirection.UP
    if features.trend_slope < 0:
        return TrendDirection.DOWN
    return TrendDirection.FLAT


def classify_setups(
    features: MarketFeatures, thresholds: SignalThresholds = DEFAULT_THRESHOLDS
) -> tuple[tuple[Setup, ...], tuple[str, ...]]:
    """Return every named state that applies, with the evidence for each.

    Multi-label and unordered — the tuple is not a ranking, and a security
    carrying two labels is not more interesting than one carrying one.
    Insufficient data yields `NONE`, never a guess.

    Args:
        features: The security's stored market state.
        thresholds: The cut-offs to apply. All provisional; see
            `SignalThresholds`.

    Returns:
        The setups, and one evidence string per setup in the same order.
    """
    setups: list[Setup] = []
    evidence: list[str] = []

    momentum = _momentum_evidence(features, thresholds)
    if momentum is not None:
        setups.append(Setup.MOMENTUM)
        evidence.append(momentum)

    reversion = stretch_20d(features)
    long_return = features.ret_252d_pct
    short_return = features.ret_20d_pct
    if (
        reversion is not None
        and long_return is not None
        and short_return is not None
        and reversion <= thresholds.mean_reversion_max_stretch
        and long_return > thresholds.mean_reversion_min_long_return_pct
    ):
        setups.append(Setup.MEAN_REVERSION)
        evidence.append(
            f"20d return {short_return:+.1f}% is {reversion:.2f} sigma of its own "
            f"20d volatility, while the 252d return is {long_return:+.1f}%"
        )

    ratio = compression_ratio(features)
    short_atr, long_atr = features.atr_pct_20, features.atr_pct_60
    if (
        ratio is not None
        and short_atr is not None
        and long_atr is not None
        and ratio <= thresholds.compression_max_ratio
    ):
        setups.append(Setup.COMPRESSION)
        evidence.append(f"20d ATR {short_atr:.2f}% is {ratio:.2f}x the 60d ATR {long_atr:.2f}%")

    if not setups:
        return (Setup.NONE,), ()
    return tuple(setups), tuple(evidence)


def _momentum_evidence(features: MarketFeatures, thresholds: SignalThresholds) -> str | None:
    """Return the evidence string when every momentum condition holds, else None.

    Each input is bound before it is compared. A missing input is never treated
    as a pass — an unknown trend fit is not a good one.
    """
    ret_60 = features.ret_60d_pct
    relative = features.rel_strength_60_pct
    fit = features.trend_r2
    atr = features.atr_pct_14
    above = features.above_sma20_share
    drawdown = features.distance_from_52w_high
    if (
        ret_60 is None
        or relative is None
        or fit is None
        or atr is None
        or above is None
        or drawdown is None
    ):
        return None
    if not (
        ret_60 > 0
        and relative > 0
        and fit >= thresholds.momentum_min_trend_r2
        and atr >= thresholds.momentum_min_atr_pct
        and above >= thresholds.momentum_min_above_sma20_share
        and drawdown >= thresholds.momentum_max_drawdown_from_high
    ):
        return None
    return (
        f"60d return {ret_60:+.1f}% and {relative:+.1f}pp vs benchmark, "
        f"trend R2 {fit:.2f}, {above:.0f}% of sessions above the 20-day mean, "
        f"{drawdown * 100:+.1f}% from the 52w high"
    )


def assess_tradability(
    features: MarketFeatures, thresholds: SignalThresholds = DEFAULT_THRESHOLDS
) -> tuple[Tradability, tuple[str, ...], bool]:
    """Return the tri-state verdict, its reasons, and whether liquidity is proven.

    The order matters. Positive evidence of a dead tape outranks everything: a
    security printing an unchanged bar on most sessions is untradable however
    much dollar volume it appears to carry, which is the lesson of HBB — an
    average true range of 3.91% assembled almost entirely from overnight gaps,
    on a tape that did not move within 59.9% of its sessions.

    Liquidity is only ever read in one direction. Stored volume comes from a
    single venue and is a strict subset of the consolidated tape, so clearing
    the floor **proves** a minimum and failing it **proves nothing**. A thin
    venue print is therefore never grounds for `UNTRADABLE`; it leaves the
    verdict `UNVERIFIED`.

    Args:
        features: The security's stored market state.
        thresholds: The cut-offs to apply.

    Returns:
        The verdict, the evidence behind it, and whether a liquidity floor was
        established.
    """
    reasons: list[str] = []

    dead = _dead_tape_reasons(features, thresholds)
    if dead:
        return Tradability.UNTRADABLE, tuple(dead), False

    if features.bars_available < thresholds.min_bars_for_verdict:
        return (
            Tradability.UNVERIFIED,
            (
                f"only {features.bars_available} sessions stored, "
                f"below the {thresholds.min_bars_for_verdict} needed to judge",
            ),
            False,
        )

    volume = features.venue_dollar_volume_median
    if volume is None:
        return (
            Tradability.UNVERIFIED,
            ("no venue dollar volume stored, so no liquidity floor can be proven",),
            False,
        )

    if volume < thresholds.tradable_min_venue_dollar_volume:
        return (
            Tradability.UNVERIFIED,
            (
                f"venue dollar volume ${volume:,.0f} is below the "
                f"${thresholds.tradable_min_venue_dollar_volume:,.0f} floor; because "
                "stored volume is a subset of the consolidated tape this proves "
                "nothing either way",
            ),
            False,
        )

    reasons.append(
        f"venue dollar volume ${volume:,.0f} proves at least that much trades, "
        f"clearing the ${thresholds.tradable_min_venue_dollar_volume:,.0f} floor"
    )
    if features.flat_bar_share is not None:
        reasons.append(f"flat-bar share {features.flat_bar_share:.1f}%")
    if features.median_range_pct is not None:
        reasons.append(f"median intraday range {features.median_range_pct:.2f}%")
    return Tradability.TRADABLE, tuple(reasons), True


def _dead_tape_reasons(features: MarketFeatures, thresholds: SignalThresholds) -> list[str]:
    """Return positive evidence that the tape is dead, if there is any."""
    reasons: list[str] = []
    if (
        features.flat_bar_share is not None
        and features.flat_bar_share >= thresholds.untradable_min_flat_bar_share
    ):
        reasons.append(f"unchanged high and low on {features.flat_bar_share:.1f}% of sessions")
    if (
        features.zero_move_share is not None
        and features.zero_move_share >= thresholds.untradable_min_zero_move_share
    ):
        reasons.append(f"closed unchanged on {features.zero_move_share:.1f}% of sessions")
    if (
        features.median_range_pct is not None
        and features.median_range_pct <= thresholds.untradable_max_median_range_pct
    ):
        reasons.append(f"median intraday range {features.median_range_pct:.3f}%")
    if (
        features.closing_band_pct is not None
        and features.closing_band_pct <= thresholds.untradable_max_closing_band_pct
    ):
        reasons.append(f"60-session closing band spans {features.closing_band_pct:.1f}% of price")
    return reasons


def derive_signals(
    features: MarketFeatures,
    peers: PeerContext,
    *,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
    signal_version: str = CURRENT_SIGNAL_VERSION,
) -> MarketSignals:
    """Return one security's market state, from its features and its cross-section.

    Pure: the same features and the same peer context always produce the same
    signals. Nothing here reads a clock or a later bar, which is what makes a
    stored signal reproducible from a stored feature row.

    Args:
        features: The security's stored market state.
        peers: The cross-section for the same snapshot date.
        thresholds: The cut-offs to apply. All provisional.
        signal_version: Stamped onto the result.

    Returns:
        The derived facts, percentiles, setups and verdict.
    """
    setups, evidence = classify_setups(features, thresholds)
    verdict, reasons, proven = assess_tradability(features, thresholds)

    percentiles = {
        name: value
        for name in PERCENTILE_FEATURES
        if (value := peers.percentile(name, getattr(features, name, None))) is not None
    }

    return MarketSignals(
        signal_version=signal_version,
        feature_as_of=features.as_of,
        bars_available=features.bars_available,
        compression_ratio=compression_ratio(features),
        volatility_expansion_ratio=features.vol_expansion_ratio,
        distance_from_52w_high=features.distance_from_52w_high,
        distance_from_52w_low=distance_from_52w_low(features),
        stretch_20d=stretch_20d(features),
        trend_direction=trend_direction(features),
        trend_r2=features.trend_r2,
        percentiles=percentiles,
        setups=setups,
        setup_evidence=evidence,
        tradability=verdict,
        tradability_reasons=reasons,
        liquidity_floor_proven=proven,
    )
