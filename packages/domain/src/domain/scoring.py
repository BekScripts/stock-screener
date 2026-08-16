"""CompounderScore: the production scoring rules, currently V1.1.

```text
Growth                35  ┐
Financial Quality     25  │  raw score, 0-100
Valuation             25  │
Market Confirmation   15  ┘
                          → risk penalties, 0 to -25
                          → final score, clamped 0-100
```

Every rule here is a deterministic function of already-calculated metrics. There
is no model, no fitting, no hidden normalisation and no peer percentile: a
company that scores 82 can be handed the breakdown and told exactly which
metrics bought which points. `docs/reference/compounder-score.md` states the
same rules in prose and is kept in agreement with this module.

Four policies decide what happens when the data is incomplete, and they matter
more than any individual curve.

**A missing metric is not a zero and not a maximum.** It produces a `SubScore`
with `points=None`, and the component's remaining metrics carry its weight
proportionally — so absence neither rewards nor punishes, it just widens the
error bars. `data_coverage` reports how wide.

**Redistribution never crosses a component.** Unavailable valuation weight is
carried by the other valuation metrics, never by growth. Each component's
maximum is fixed at the number in the table above whatever was available.

**A component that knows too little does not score.** Each declares the metrics
it cannot do without and the share of its weight it needs; below either, it is
`INSUFFICIENT_DATA` and so is the company, which then does not enter the
ranking. This is the difference between "we do not know" and "it is mediocre".

**Risk is never folded into the four components.** Penalties are calculated
separately, displayed separately, and subtracted at the end.

V1.1 adds three guards to V1 and changes no weight, curve or penalty: the
redistribution uplift is capped, a cyclical rebound forfeits its acceleration
bonus, and growth plus acceleration are capped together when growth does not
persist. Each is documented beside the constant that defines it.
"""

from __future__ import annotations

from math import isclose
from typing import TYPE_CHECKING

from domain.curves import band, interpolate
from domain.models import VolumeBasis
from domain.scores import (
    CURRENT_SCORE_VERSION,
    BenchmarkReturns,
    CompanyScore,
    ComponentScore,
    ComponentStatus,
    MetricUnit,
    RiskAssessment,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    SubScore,
    ValuationBasis,
)
from domain.sectors import is_unsupported_sector

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from domain.curves import Bands, Curve
    from domain.models import CompanyMetrics, CompanyProfile

# ---------------------------------------------------------------------------
# Component weights
# ---------------------------------------------------------------------------

GROWTH_MAX = 35.0
QUALITY_MAX = 25.0
VALUATION_MAX = 25.0
MOMENTUM_MAX = 15.0
RAW_SCORE_MAX = GROWTH_MAX + QUALITY_MAX + VALUATION_MAX + MOMENTUM_MAX

MAX_TOTAL_PENALTY = -25.0
"""Floor on the summed risk penalties.

Without it a company could be penalised past the point where the score carries
information: below zero every distressed company looks identical.
"""

_MIN_COMPONENT_COVERAGE = 0.5
"""Share of a component's weight that must be available for it to score.

Half is the point at which a component is measuring the company rather than
whichever metrics happened to arrive. For growth it also means, concretely, that
a level alone is not enough — a trend or history metric has to be present too.
"""

MAX_REDISTRIBUTION_MULTIPLIER = 1.15
"""Ceiling on the uplift a component may receive from redistribution (V1.1).

Full redistribution assumes the metrics that *are* present speak for the ones
that are not. Measured over a real market that assumption paid out too well: a
fifth of scored companies gained more than five points from it, the average gain
inside the top fifty was 5.6, and removing it entirely would have replaced
sixteen of those fifty. Capping the multiplier keeps the principle — absence
neither rewards nor punishes — while bounding how far a partial picture can be
extrapolated. A company missing 13% of a component's weight is still fully
compensated; one missing 28% is compensated for 15% of it and carries the rest
as a lower score, with `data_coverage` saying why.
"""

# ---------------------------------------------------------------------------
# Growth — 35 points
# ---------------------------------------------------------------------------

REVENUE_GROWTH_CURVE: Curve = (
    (0.0, 0.0),
    (0.10, 3.0),
    (0.20, 6.0),
    (0.30, 8.0),
    (0.50, 10.0),
    (0.75, 12.0),
)
ACCELERATION_CURVE: Curve = (
    (-0.20, 0.0),
    (-0.10, 1.0),
    (0.0, 3.0),
    (0.10, 5.0),
    (0.20, 7.0),
    (0.30, 8.0),
)
CAGR_CURVE: Curve = ((0.0, 0.0), (0.05, 1.0), (0.10, 2.0), (0.15, 3.0), (0.25, 5.0), (0.35, 6.0))
GROSS_PROFIT_GROWTH_CURVE: Curve = (
    (0.0, 0.0),
    (0.10, 1.0),
    (0.20, 2.0),
    (0.30, 3.0),
    (0.50, 4.0),
    (0.75, 5.0),
)

PERSISTENCE_QUARTERS = 4
"""Year-over-year observations growth persistence is counted over."""

REBOUND_YOY_FLOOR = 0.30
REBOUND_CAGR_CEILING = 0.05
REBOUND_ACCELERATION_CAP = 3.0
"""The cyclical-rebound guard (V1.1).

Fast year-over-year growth against a flat or negative three-year trend is a
recovery from a trough, not expansion — a refiner whose revenue fell and came
back has the same arithmetic as a company that doubled its business. The growth
*level* is real and keeps its points; what is withdrawn is the **acceleration
bonus**, because acceleration measures the change in growth rate and for a
rebound that change is an artifact of the depressed base it is measured against.

The cap is the curve's own zero-acceleration reference, so a rebound is scored as
though its growth rate were merely steady. Companies with no three-year CAGR do
not trigger it: an unknown trend is not a flat one.
"""

LUMPY_PERSISTENCE_CEILING = 2.0
LUMPY_GROWTH_BUDGET = 12.0
"""The lumpy-growth guard (V1.1).

Revenue growth and acceleration are both computed from a single quarter against
a single quarter a year earlier. A company recognising a milestone payment can
therefore max out twenty of the thirty-five growth points on one lumpy quarter,
while persistence — the only sub-score that can see the lumpiness — carries four.
Measured over a real market, companies growing 75%+ with two or fewer positive
quarters out of four scored **eleven points higher** on average than companies
growing just as fast every quarter.

The guard caps what those two sub-scores may earn *together* when persistence is
weak, scaling both proportionally so neither is singled out. It only binds when
headline growth is extreme, which is exactly when a single quarter can carry the
component. Unknown persistence does not trigger it.
"""

# ---------------------------------------------------------------------------
# Financial quality — 25 points
# ---------------------------------------------------------------------------

GROSS_MARGIN_BANDS: Bands = (
    (0.65, 6.0),
    (0.50, 5.0),
    (0.40, 4.0),
    (0.30, 3.0),
    (0.20, 2.0),
    (0.10, 1.0),
)
"""Level of gross margin, as bands rather than a curve.

The specification states this one as ranges — "40-50% → 4" — because the level
is read categorically: a 45% gross margin business and a 48% one are the same
kind of business. The trend adjustment below is what separates them.
"""

GROSS_MARGIN_TREND_CURVE: Curve = ((-0.03, -1.0), (0.0, 0.0), (0.03, 1.0))
FCF_MARGIN_CURVE: Curve = (
    (-0.30, 0.0),
    (-0.20, 1.0),
    (-0.10, 2.0),
    (0.0, 3.0),
    (0.05, 4.0),
    (0.10, 5.0),
    (0.15, 6.0),
    (0.25, 7.0),
)
NET_CASH_CURVE: Curve = (
    (-0.75, 0.0),
    (-0.50, 1.0),
    (-0.25, 2.0),
    (-0.10, 3.0),
    (0.0, 4.0),
    (0.10, 5.0),
    (0.20, 6.0),
)
OPERATING_MARGIN_CHANGE_CURVE: Curve = (
    (-0.10, 0.0),
    (-0.05, 1.0),
    (0.0, 2.0),
    (0.03, 3.0),
    (0.05, 4.0),
    (0.10, 5.0),
)

_FCF_IMPROVEMENT_THRESHOLD = 0.05
"""Percentage-point improvement in FCF margin that earns a burning company +1.

A company whose burn is closing fast is a different prospect from one whose burn
is stable at the same depth, and the level curve alone cannot see the
difference.
"""

# ---------------------------------------------------------------------------
# Valuation — 25 points
# ---------------------------------------------------------------------------

SALES_MULTIPLE_CURVE: Curve = (
    (1.0, 15.0),
    (2.0, 13.0),
    (3.0, 11.0),
    (5.0, 8.0),
    (8.0, 5.0),
    (12.0, 2.0),
    (20.0, 0.0),
)
"""Points for EV/Revenue, or Price/Sales when enterprise value is unavailable.

Deliberately broad, and deliberately blind to growth — the growth-adjusted
sub-score below is where a high multiple is allowed to justify itself.
"""

FCF_YIELD_CURVE: Curve = ((0.0, 0.0), (0.02, 1.0), (0.05, 2.0), (0.08, 3.0))

_GROWTH_TIER_BOUNDS: tuple[float, ...] = (0.40, 0.25, 0.10)
"""Lower bounds of the growth tiers, fastest first."""

_MULTIPLE_TIER_BOUNDS: tuple[float, ...] = (20.0, 10.0, 5.0, 2.0)
"""Lower bounds of the sales-multiple tiers, most expensive first."""

GROWTH_ADJUSTED_MATRIX: tuple[tuple[float, ...], ...] = (
    # >=20x  10-20x  5-10x  2-5x   <2x
    (1.0, 3.0, 5.0, 7.0, 7.0),  # growth >= 40%
    (1.0, 2.0, 4.0, 6.0, 7.0),  # growth 25-40%
    (0.0, 1.0, 3.0, 5.0, 6.0),  # growth 10-25%
    (0.0, 0.0, 1.0, 3.0, 5.0),  # growth < 10%
)
"""Growth against valuation, as a table rather than a ratio.

A ratio of multiple to growth is easy to write and impossible to read: it
divides by a number that approaches zero, it treats 100% growth at 25x as
equivalent to 20% growth at 5x, and nobody can say what a value of 0.4 means. A
table states the judgement directly — cheap and fast growing earns the maximum,
expensive and slow earns nothing, and extreme growth at an extreme multiple
earns one point, not seven.
"""

# ---------------------------------------------------------------------------
# Market confirmation — 15 points
# ---------------------------------------------------------------------------

RELATIVE_STRENGTH_CURVE: Curve = ((-0.30, 0.0), (-0.15, 1.0), (0.0, 3.0), (0.15, 5.0), (0.30, 6.0))
FIFTY_TWO_WEEK_CURVE: Curve = ((-0.50, 0.0), (-0.35, 1.0), (-0.20, 2.0), (-0.10, 2.5), (-0.05, 3.0))

# ---------------------------------------------------------------------------
# Risk penalties
# ---------------------------------------------------------------------------

DILUTION_CURVE: Curve = ((0.02, 0.0), (0.05, -1.0), (0.10, -3.0), (0.20, -6.0), (0.35, -10.0))
RUNWAY_CURVE: Curve = ((6.0, -10.0), (12.0, -6.0), (18.0, -3.0), (24.0, -1.0))
LEVERAGE_CURVE: Curve = ((0.25, 0.0), (0.50, -2.0), (0.75, -4.0), (1.0, -5.0))

RUNWAY_SAFE_MONTHS = 24.0
"""Months of cash above which no runway penalty applies."""

_MONTHS_PER_YEAR = 12.0

_RISK_LEVEL_BOUNDS: tuple[tuple[float, RiskLevel], ...] = (
    (-3.0, RiskLevel.LOW),
    (-8.0, RiskLevel.MEDIUM),
    (-15.0, RiskLevel.HIGH),
)
"""Inclusive floors for each risk label, mildest first."""

_CATEGORY_BOUNDS: tuple[tuple[float, ScoreCategory], ...] = (
    (85.0, ScoreCategory.EXCEPTIONAL_RESEARCH_CANDIDATE),
    (75.0, ScoreCategory.STRONG_RESEARCH_CANDIDATE),
    (65.0, ScoreCategory.WORTH_WATCHING),
    (50.0, ScoreCategory.MIXED),
)
"""Inclusive floors for each research-priority band, highest first."""

_ASSESSABLE_RISKS = 3
"""Dilution, cash runway and leverage. Liquidity is a warning, not a penalty."""


class ScoringError(RuntimeError):
    """A component was defined with weights that do not add up.

    A programming error rather than a data one, raised loudly because the
    consequence — every company in the market scored against the wrong
    denominator — is invisible in the output.
    """


# ---------------------------------------------------------------------------
# Component assembly
# ---------------------------------------------------------------------------


def _points(curve: Curve, value: float | None) -> float | None:
    """Return interpolated points, or None when the metric is unavailable."""
    return None if value is None else interpolate(curve, value)


def _round(value: float) -> float:
    """Round to two decimals, the precision scores are stored and shown at."""
    return round(value, 2)


def _build_component(
    name: str,
    subscores: Sequence[SubScore],
    max_points: float,
    *,
    required: Collection[str] = (),
    min_coverage: float = _MIN_COMPONENT_COVERAGE,
) -> ComponentScore:
    """Combine sub-scores into a component, redistributing missing weight.

    The available metrics carry the component's full weight in proportion to
    their own: a growth component missing its 5-point gross-profit metric scores
    the other 30 points' worth and scales the result by 35/30. A company is
    therefore neither credited nor debited for a metric nobody could calculate.

    Args:
        name: Component identifier.
        subscores: Every metric considered, available or not.
        max_points: The component's fixed contribution to the raw score.
        required: Metrics without which the component cannot be scored at all.
        min_coverage: Share of the component's weight that must be available.

    Returns:
        The component, `INSUFFICIENT_DATA` when a required metric is missing or
        coverage falls short.

    Raises:
        ScoringError: If the sub-score weights do not sum to `max_points`.
    """
    declared = sum(sub.max_points for sub in subscores)
    if not isclose(declared, max_points):
        raise ScoringError(
            f"{name} sub-scores declare {declared} points but the component is worth {max_points}"
        )

    available = [sub for sub in subscores if sub.available]
    available_weight = sum(sub.max_points for sub in available)
    coverage = available_weight / max_points

    missing_required = {sub.name for sub in subscores if not sub.available} & set(required)
    if missing_required or available_weight == 0 or coverage < min_coverage:
        return ComponentScore(
            name=name,
            status=ComponentStatus.INSUFFICIENT_DATA,
            score=None,
            max_points=max_points,
            coverage=round(coverage, 4),
            subscores=tuple(subscores),
        )

    earned = sum(sub.points or 0.0 for sub in available)
    multiplier = min(max_points / available_weight, MAX_REDISTRIBUTION_MULTIPLIER)
    scaled = earned * multiplier
    return ComponentScore(
        name=name,
        status=ComponentStatus.SCORED,
        score=_round(min(scaled, max_points)),
        max_points=max_points,
        coverage=round(coverage, 4),
        subscores=tuple(subscores),
        redistributed=available_weight < max_points,
    )


# ---------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------


def is_cyclical_rebound(metrics: CompanyMetrics) -> bool:
    """Whether fast growth sits on a flat or negative three-year trend.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        True when year-over-year growth clears the floor while the three-year
        CAGR does not clear its ceiling. False when either is unavailable — an
        unknown trend is not a flat one, and guessing would penalise a young
        company for having no history.
    """
    if metrics.revenue_growth_yoy is None or metrics.revenue_cagr_3y is None:
        return False
    return metrics.revenue_growth_yoy >= REBOUND_YOY_FLOOR and (
        metrics.revenue_cagr_3y <= REBOUND_CAGR_CEILING
    )


def _apply_growth_guards(
    metrics: CompanyMetrics,
    level: float | None,
    acceleration: float | None,
    persistence: float | None,
) -> tuple[float | None, float | None, dict[str, str]]:
    """Apply the two V1.1 growth guards, in order, and explain what they did.

    The rebound guard runs first and only touches acceleration; the lumpy guard
    then applies to whatever the pair is worth together. Both leave an
    explanation on the sub-score, because a company that scores lower than its
    raw metrics suggest should be able to say which rule did it.

    Args:
        metrics: The company's calculated metrics.
        level: Points earned on the revenue-growth curve.
        acceleration: Points earned on the acceleration curve.
        persistence: Positive quarters out of four, or None when unknown.

    Returns:
        The adjusted points and a note per sub-score the guards changed.
    """
    notes: dict[str, str] = {}

    if (
        acceleration is not None
        and acceleration > REBOUND_ACCELERATION_CAP
        and is_cyclical_rebound(metrics)
    ):
        acceleration = REBOUND_ACCELERATION_CAP
        notes["growth_acceleration"] = "capped: growth is a rebound against a flat three-year trend"

    if (
        persistence is not None
        and persistence <= LUMPY_PERSISTENCE_CEILING
        and level is not None
        and acceleration is not None
        and level + acceleration > LUMPY_GROWTH_BUDGET
    ):
        scale = LUMPY_GROWTH_BUDGET / (level + acceleration)
        level, acceleration = level * scale, acceleration * scale
        explanation = (
            f"scaled: growth and acceleration capped at {LUMPY_GROWTH_BUDGET:.0f} "
            f"points combined on {persistence:.0f} of {PERSISTENCE_QUARTERS} positive quarters"
        )
        notes["revenue_growth"] = explanation
        notes["growth_acceleration"] = explanation

    return level, acceleration, notes


def score_growth(metrics: CompanyMetrics) -> ComponentScore:
    """Score the growth component, worth 35 points.

    Current revenue growth is the anchor and is required: a company whose latest
    growth rate is unknown has no growth story to judge, whatever else is
    available.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        The growth component and its five sub-scores.
    """
    observations = metrics.recent_revenue_growth_yoy
    persistence: float | None = None
    if len(observations) >= PERSISTENCE_QUARTERS:
        persistence = float(sum(1 for value in observations[:PERSISTENCE_QUARTERS] if value > 0))

    level = _points(REVENUE_GROWTH_CURVE, metrics.revenue_growth_yoy)
    acceleration = _points(ACCELERATION_CURVE, metrics.revenue_growth_acceleration)
    level, acceleration, guards = _apply_growth_guards(metrics, level, acceleration, persistence)

    subscores = (
        SubScore(
            name="revenue_growth",
            points=level,
            max_points=12.0,
            observed=metrics.revenue_growth_yoy,
            unit=MetricUnit.PERCENT,
            note=guards.get("revenue_growth"),
        ),
        SubScore(
            name="growth_acceleration",
            points=acceleration,
            max_points=8.0,
            observed=metrics.revenue_growth_acceleration,
            unit=MetricUnit.POINTS,
            note=guards.get("growth_acceleration"),
        ),
        SubScore(
            name="revenue_cagr_3y",
            points=_points(CAGR_CURVE, metrics.revenue_cagr_3y),
            max_points=6.0,
            observed=metrics.revenue_cagr_3y,
            unit=MetricUnit.PERCENT,
        ),
        SubScore(
            name="gross_profit_growth",
            points=_points(GROSS_PROFIT_GROWTH_CURVE, metrics.gross_profit_growth_yoy),
            max_points=5.0,
            observed=metrics.gross_profit_growth_yoy,
            unit=MetricUnit.PERCENT,
        ),
        SubScore(
            name="growth_persistence",
            points=persistence,
            max_points=4.0,
            observed=persistence,
            unit=MetricUnit.COUNT,
            note=f"{len(observations)} of {PERSISTENCE_QUARTERS} comparable quarters observed",
        ),
    )
    return _build_component("growth", subscores, GROWTH_MAX, required=("revenue_growth",))


# ---------------------------------------------------------------------------
# Financial quality
# ---------------------------------------------------------------------------


def _gross_margin_points(metrics: CompanyMetrics) -> tuple[float | None, str | None]:
    """Return gross margin points and a note describing the trend adjustment."""
    if metrics.gross_margin is None:
        return None, None

    level = band(GROSS_MARGIN_BANDS, metrics.gross_margin)
    if metrics.gross_margin_change is None:
        return level, "level only; no comparable year-ago margin"

    adjustment = interpolate(GROSS_MARGIN_TREND_CURVE, metrics.gross_margin_change)
    # Both halves are points, and the note says so. Written as "level 2 trend
    # +1.00" the second number reads like a margin or a direction code, and a
    # reader repeating it produced "a positive trend of +1.00 level".
    return (
        max(0.0, min(level + adjustment, 7.0)),
        f"{level:.0f} points on margin level, {adjustment:+.2f} points on margin trend",
    )


def _fcf_margin_points(metrics: CompanyMetrics) -> tuple[float | None, str | None]:
    """Return FCF margin points, crediting a burning company that is improving."""
    if metrics.fcf_margin is None:
        return None, None

    level = interpolate(FCF_MARGIN_CURVE, metrics.fcf_margin)
    improving = (
        metrics.fcf_margin < 0
        and metrics.fcf_margin_change is not None
        and metrics.fcf_margin_change >= _FCF_IMPROVEMENT_THRESHOLD
    )
    if not improving:
        return level, None
    return min(level + 1.0, 7.0), "burn narrowing by more than 5pp year on year"


def score_quality(metrics: CompanyMetrics) -> ComponentScore:
    """Score the financial quality component, worth 25 points.

    Cash against debt is measured relative to market capitalisation rather than
    in dollars, so a manufacturer's financing arm is not confused with distress
    and a small software company is not flattered by owing little in absolute
    terms.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        The quality component and its four sub-scores.
    """
    gross_points, gross_note = _gross_margin_points(metrics)
    fcf_points, fcf_note = _fcf_margin_points(metrics)

    net_cash_ratio: float | None = None
    if metrics.net_cash is not None and metrics.market_cap:
        net_cash_ratio = metrics.net_cash / metrics.market_cap

    subscores = (
        SubScore(
            name="gross_margin",
            points=gross_points,
            max_points=7.0,
            observed=metrics.gross_margin,
            unit=MetricUnit.PERCENT,
            note=gross_note,
        ),
        SubScore(
            name="fcf_margin",
            points=fcf_points,
            max_points=7.0,
            observed=metrics.fcf_margin,
            unit=MetricUnit.PERCENT,
            note=fcf_note,
        ),
        SubScore(
            name="cash_vs_debt",
            points=_points(NET_CASH_CURVE, net_cash_ratio),
            max_points=6.0,
            observed=net_cash_ratio,
            unit=MetricUnit.PERCENT,
            note="net cash over market cap",
        ),
        SubScore(
            name="operating_margin_trend",
            points=_points(OPERATING_MARGIN_CHANGE_CURVE, metrics.operating_margin_change),
            max_points=5.0,
            observed=metrics.operating_margin_change,
            unit=MetricUnit.POINTS,
        ),
    )
    return _build_component("quality", subscores, QUALITY_MAX)


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


def valuation_multiple(metrics: CompanyMetrics) -> tuple[float | None, ValuationBasis]:
    """Return the sales multiple to value the company on, and which one it is.

    Enterprise value is preferred because it accounts for the balance sheet.
    When debt or cash is unknown enterprise value is unknown too — absent debt is
    never read as zero — so the calculation falls back to price-to-sales and
    says so, rather than presenting one measure as the other.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        The multiple and its basis, or `(None, NOT_AVAILABLE)` when trailing
        revenue is missing or not positive.
    """
    revenue = metrics.ttm_revenue
    if revenue is None or revenue <= 0:
        return None, ValuationBasis.NOT_AVAILABLE

    if metrics.enterprise_value is not None:
        return metrics.enterprise_value / revenue, ValuationBasis.EV_TO_REVENUE

    if metrics.market_cap is not None and metrics.market_cap > 0:
        return metrics.market_cap / revenue, ValuationBasis.PRICE_TO_SALES

    return None, ValuationBasis.NOT_AVAILABLE


def _tier(bounds: Sequence[float], value: float) -> int:
    """Return the index of the first bound `value` reaches, or the last index."""
    for index, bound in enumerate(bounds):
        if value >= bound:
            return index
    return len(bounds)


def growth_adjusted_points(multiple: float | None, growth: float | None) -> float | None:
    """Return points for a valuation multiple read against revenue growth.

    Args:
        multiple: EV/Revenue or Price/Sales. A negative multiple — a company
            valued below its net cash — falls in the cheapest tier.
        growth: Latest year-over-year revenue growth, as a decimal.

    Returns:
        Points from `GROWTH_ADJUSTED_MATRIX`, or None when either input is
        unavailable.
    """
    if multiple is None or growth is None:
        return None
    return GROWTH_ADJUSTED_MATRIX[_tier(_GROWTH_TIER_BOUNDS, growth)][
        _tier(_MULTIPLE_TIER_BOUNDS, multiple)
    ]


def score_valuation(metrics: CompanyMetrics) -> tuple[ComponentScore, ValuationBasis]:
    """Score the valuation component, worth 25 points.

    Nothing here requires profitability. A loss-making company is valued on
    revenue and judged on whether its growth justifies the multiple; earnings
    multiples are simply absent rather than punitive, which is the whole point
    of a screen looking for companies before they are obvious.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        The valuation component and the basis its primary multiple used.
    """
    multiple, basis = valuation_multiple(metrics)

    fcf_yield: float | None = None
    if metrics.ttm_free_cash_flow is not None and metrics.market_cap:
        fcf_yield = metrics.ttm_free_cash_flow / metrics.market_cap

    subscores = (
        SubScore(
            name="valuation_multiple",
            points=_points(SALES_MULTIPLE_CURVE, multiple),
            max_points=15.0,
            observed=multiple,
            unit=MetricUnit.MULTIPLE,
            note=basis.value,
        ),
        SubScore(
            name="growth_adjusted_valuation",
            points=growth_adjusted_points(multiple, metrics.revenue_growth_yoy),
            max_points=7.0,
            observed=multiple,
            unit=MetricUnit.MULTIPLE,
            note="multiple read against revenue growth",
        ),
        SubScore(
            name="fcf_yield",
            points=_points(FCF_YIELD_CURVE, fcf_yield),
            max_points=3.0,
            observed=fcf_yield,
            unit=MetricUnit.PERCENT,
        ),
    )
    component = _build_component(
        "valuation", subscores, VALUATION_MAX, required=("valuation_multiple",)
    )
    return component, basis


# ---------------------------------------------------------------------------
# Market confirmation
# ---------------------------------------------------------------------------


def relative_strength(company: float | None, benchmark: float | None) -> float | None:
    """Return a company's return less the benchmark's, in decimal points.

    Args:
        company: The company's return over the period, as a decimal.
        benchmark: The benchmark's return over the same period.

    Returns:
        `company - benchmark`, where 0.20 means twenty percentage points of
        outperformance, or None when either is unavailable.
    """
    if company is None or benchmark is None:
        return None
    return company - benchmark


def score_momentum(
    metrics: CompanyMetrics, benchmark: BenchmarkReturns | None = None
) -> ComponentScore:
    """Score the market confirmation component, worth 15 points.

    Only 15 of 100, and deliberately so. Strong fundamentals with a weak price
    may be the opportunity rather than the warning, and a component large enough
    to remove those companies would turn the screen into a momentum strategy.

    Args:
        metrics: The company's calculated metrics.
        benchmark: Broad-market returns to measure against. Without it relative
            strength cannot be calculated and the component falls back to the
            52-week position alone, which is not enough coverage to score.

    Returns:
        The momentum component and its three sub-scores.
    """
    market = benchmark or BenchmarkReturns(symbol="")
    strength_6m = relative_strength(metrics.return_6m, market.return_6m)
    strength_12m = relative_strength(metrics.return_12m, market.return_12m)

    subscores = (
        SubScore(
            name="relative_strength_6m",
            points=_points(RELATIVE_STRENGTH_CURVE, strength_6m),
            max_points=6.0,
            observed=strength_6m,
            unit=MetricUnit.POINTS,
            note=f"against {market.symbol}" if market.symbol else None,
        ),
        SubScore(
            name="relative_strength_12m",
            points=_points(RELATIVE_STRENGTH_CURVE, strength_12m),
            max_points=6.0,
            observed=strength_12m,
            unit=MetricUnit.POINTS,
            note=f"against {market.symbol}" if market.symbol else None,
        ),
        SubScore(
            name="position_52w",
            points=_points(FIFTY_TWO_WEEK_CURVE, metrics.distance_from_52w_high),
            max_points=3.0,
            observed=metrics.distance_from_52w_high,
            unit=MetricUnit.PERCENT,
        ),
    )
    return _build_component("momentum", subscores, MOMENTUM_MAX)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


def cash_runway_months(metrics: CompanyMetrics) -> float | None:
    """Return months of cash at the trailing burn rate.

    Burn is trailing-twelve-month free cash flow divided by twelve, which is the
    measure the rest of the system already has and is stable across a lumpy
    quarter.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        Months of runway, or None when cash or trailing free cash flow is
        unavailable **or when the company generates cash** — a business funding
        itself has no runway to run out, and reporting a number there would
        invite a penalty for the healthiest balance sheets.
    """
    if metrics.cash is None or metrics.ttm_free_cash_flow is None:
        return None
    if metrics.ttm_free_cash_flow >= 0:
        return None

    monthly_burn = -metrics.ttm_free_cash_flow / _MONTHS_PER_YEAR
    return metrics.cash / monthly_burn


def _runway_penalty(metrics: CompanyMetrics) -> tuple[float | None, float | None]:
    """Return the runway penalty and the runway it was based on."""
    if metrics.cash is None or metrics.ttm_free_cash_flow is None:
        return None, None
    if metrics.ttm_free_cash_flow >= 0:
        # Generating cash: no finite runway, and therefore no penalty. This is an
        # assessed risk, not an unknown one.
        return 0.0, None

    months = cash_runway_months(metrics)
    if months is None:  # pragma: no cover — guarded by the checks above
        return None, None
    if months > RUNWAY_SAFE_MONTHS:
        return 0.0, months
    return interpolate(RUNWAY_CURVE, months), months


def risk_level(total_penalty: float) -> RiskLevel:
    """Return the risk label for a total penalty.

    Args:
        total_penalty: The summed penalties, zero or negative.

    Returns:
        The band the penalty falls in.
    """
    for floor, level in _RISK_LEVEL_BOUNDS:
        if total_penalty >= floor:
            return level
    return RiskLevel.VERY_HIGH


def assess_risk(metrics: CompanyMetrics) -> RiskAssessment:
    """Calculate the risk penalties applied after the raw score.

    Each penalty answers a question the four positive components deliberately do
    not: dilution is invisible to a growth score, and a company can grow revenue
    50% while having six months of cash left. Only severe leverage is penalised
    here, because ordinary balance-sheet quality is already scored in the
    quality component and charging for it twice would rank capital-intensive
    industries by their industry.

    An unknown risk is not scored as absent. It produces `None`, lowers
    `coverage`, and raises a warning — so a company whose share count history is
    missing is not credited with having issued no shares.

    Args:
        metrics: The company's calculated metrics.

    Returns:
        Every penalty, the total floored at `MAX_TOTAL_PENALTY`, and the label.
    """
    warnings: list[ScoreWarning] = []

    dilution = _points(DILUTION_CURVE, metrics.share_count_growth_yoy)
    if dilution is None:
        warnings.append(ScoreWarning.DILUTION_NOT_ASSESSED)

    runway, months = _runway_penalty(metrics)
    if runway is None:
        warnings.append(ScoreWarning.RUNWAY_NOT_ASSESSED)

    leverage: float | None = None
    net_debt_ratio: float | None = None
    if metrics.net_cash is not None and metrics.market_cap:
        net_debt_ratio = -metrics.net_cash / metrics.market_cap
        leverage = interpolate(LEVERAGE_CURVE, net_debt_ratio)
    else:
        warnings.append(ScoreWarning.LEVERAGE_NOT_ASSESSED)

    if metrics.liquidity_basis is not VolumeBasis.CONSOLIDATED:
        # A statement about the feed, not the company. Phase 2 does not price it.
        warnings.append(ScoreWarning.LIQUIDITY_UNVERIFIED)

    assessed = [penalty for penalty in (dilution, runway, leverage) if penalty is not None]
    total = max(sum(assessed), MAX_TOTAL_PENALTY)

    return RiskAssessment(
        dilution_penalty=None if dilution is None else _round(dilution),
        runway_penalty=None if runway is None else _round(runway),
        balance_sheet_penalty=None if leverage is None else _round(leverage),
        liquidity_penalty=0.0,
        total_penalty=_round(total),
        level=risk_level(total),
        coverage=round(len(assessed) / _ASSESSABLE_RISKS, 4),
        share_count_growth_yoy=metrics.share_count_growth_yoy,
        cash_runway_months=None if months is None else _round(months),
        net_debt_to_market_cap=net_debt_ratio,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def categorise(final_score: float) -> ScoreCategory:
    """Return the research-priority band a final score falls in.

    Args:
        final_score: The risk-adjusted score, 0-100.

    Returns:
        The category. These rank research priority, not investment merit.
    """
    for floor, category in _CATEGORY_BOUNDS:
        if final_score >= floor:
            return category
    return ScoreCategory.LOW_PRIORITY


def _data_coverage(components: Sequence[ComponentScore]) -> float:
    """Return the share of all scoring metrics that were available, 0-1."""
    available = sum(
        sub.max_points for component in components for sub in component.subscores if sub.available
    )
    return round(available / RAW_SCORE_MAX, 4)


def score_company(
    profile: CompanyProfile,
    metrics: CompanyMetrics,
    benchmark: BenchmarkReturns | None = None,
    *,
    eligible: bool = True,
) -> CompanyScore:
    """Calculate the complete CompounderScore for one company.

    Args:
        profile: Identity and classification. Sector and industry decide whether
            the model applies to this business at all.
        metrics: Every derived figure, as produced by `build_company_metrics`.
        benchmark: Broad-market returns for relative strength. Without them the
            market confirmation component cannot reach its minimum coverage, and
            the company is `INSUFFICIENT_DATA` rather than scored on three
            components out of four.
        eligible: Whether the security passed the Phase 1 screen. An ineligible
            security is not scored at all.

    Returns:
        The score, or a `CompanyScore` carrying the status that explains why
        there is no number. `final_score` is None unless the status is `SCORED`.
    """
    if not eligible:
        return CompanyScore(ticker=profile.ticker, status=ScoringStatus.NOT_ELIGIBLE)

    if is_unsupported_sector(profile.sector, profile.industry):
        return CompanyScore(ticker=profile.ticker, status=ScoringStatus.UNSUPPORTED_SECTOR)

    growth = score_growth(metrics)
    quality = score_quality(metrics)
    valuation, basis = score_valuation(metrics)
    momentum = score_momentum(metrics, benchmark)
    components = (growth, quality, valuation, momentum)

    warnings: list[ScoreWarning] = []
    if any(component.redistributed for component in components):
        warnings.append(ScoreWarning.WEIGHT_REDISTRIBUTED)
    if basis is ValuationBasis.PRICE_TO_SALES:
        warnings.append(ScoreWarning.VALUATION_FALLBACK_PRICE_TO_SALES)
    if benchmark is None or (benchmark.return_6m is None and benchmark.return_12m is None):
        warnings.append(ScoreWarning.NO_BENCHMARK)

    coverage = _data_coverage(components)
    if any(component.status is ComponentStatus.INSUFFICIENT_DATA for component in components):
        return CompanyScore(
            ticker=profile.ticker,
            status=ScoringStatus.INSUFFICIENT_DATA,
            growth=growth,
            quality=quality,
            valuation=valuation,
            momentum=momentum,
            data_coverage=coverage,
            valuation_basis=basis,
            warnings=tuple(warnings),
        )

    risk = assess_risk(metrics)
    raw = sum(component.score or 0.0 for component in components)
    final = min(max(raw + risk.total_penalty, 0.0), RAW_SCORE_MAX)

    for warning in risk.warnings:
        if warning not in warnings:
            warnings.append(warning)

    return CompanyScore(
        ticker=profile.ticker,
        score_version=CURRENT_SCORE_VERSION,
        status=ScoringStatus.SCORED,
        growth=growth,
        quality=quality,
        valuation=valuation,
        momentum=momentum,
        raw_score=_round(raw),
        risk=risk,
        final_score=_round(final),
        category=categorise(final),
        data_coverage=coverage,
        valuation_basis=basis,
        warnings=tuple(warnings),
    )
