"""Cross-sectional signal diagnostics: does a higher frozen score go anywhere?

This is the other research mode, and it is kept apart from the portfolio on
purpose. A portfolio answers "what would this path have looked like under
execution rules"; a diagnostic answers "is the score associated with different
subsequent returns at all". Mixing them lets a construction choice be read as a
signal finding.

The labels are the frozen ones — `forward_returns`, measured once by
`domain.forward_returns` on the benchmark calendar and bound here by their
fingerprint. They are not recomputed under a friendlier endpoint rule, and
their statuses are never flattened: only `REALISED` observations enter a return
statistic, and `INTERRUPTED`, `UNPRICED`, `TRUNCATED` and `NO_ENTRY` are
counted and reported rather than dropped or read as zero.

Deciles are cut low to high, so `D1` is the weakest tenth of the frozen score
and `D10` the strongest, and `D10 - D1` is defined before any of them is
computed. Uncertainty is expressed across **months**, not across security-
months: two securities in the same month share a market, and treating them as
independent draws would shrink every interval by a factor nobody earned.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence

    from stock_screener.backtest_harness.crosssection import CrossSectionRow

__all__ = [
    "BOOTSTRAP_SAMPLES",
    "BOOTSTRAP_SEED",
    "DECILES",
    "HORIZONS",
    "MIN_DECILE_CROSS_SECTION",
    "PRIMARY_HORIZON",
    "DecileStat",
    "InformationCoefficient",
    "ScoreDistribution",
    "assign_deciles",
    "contrast_series",
    "decile_diagnostics",
    "load_labels",
    "rank_information_coefficient",
    "score_distribution",
]

#: Buckets, fixed before any return is inspected.
DECILES = 10

#: Below this many observations a month is not cut into deciles at all. Ten per
#: bucket is the smallest partition that is still a partition; the rule is
#: deterministic and is set here rather than after the counts were seen.
MIN_DECILE_CROSS_SECTION = 100

#: The preregistered primary diagnostic horizon. The others are secondary.
PRIMARY_HORIZON = 20

#: Every horizon the frozen labels carry.
HORIZONS = (1, 5, 20, 60, 120)

#: A seeded, deterministic bootstrap. Months are the resampling unit.
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260101


@dataclass(frozen=True)
class DecileStat:
    """One decile's outcome over one horizon and one block.

    Attributes:
        decile: 1 for the weakest tenth of the score, 10 for the strongest.
        horizon: Sessions ahead.
        split: The block, or `ALL`.
        observations: Security-months assigned to the decile.
        realised: Of those, labels with both ends priced.
        mean: Mean realised total return.
        median: Median realised total return.
        win_rate: Share of realised returns above zero.
        volatility: Standard deviation of realised returns.
        p10: Tenth percentile.
        p90: Ninetieth percentile.
        statuses: Counts by label status, including the unmeasurable ones.
        mean_score: Mean frozen score in the bucket, for a sanity read.
    """

    decile: int
    horizon: int
    split: str
    observations: int
    realised: int
    mean: float | None
    median: float | None
    win_rate: float | None
    volatility: float | None
    p10: float | None
    p90: float | None
    statuses: dict[str, int]
    mean_score: float | None

    def as_json(self) -> dict[str, object]:
        """A deterministic mapping."""
        return dict(sorted(asdict(self).items()))


@dataclass(frozen=True)
class InformationCoefficient:
    """The monthly rank correlation between the frozen score and the outcome.

    Attributes:
        horizon: Sessions ahead.
        split: The block, or `ALL`.
        months: Months with enough realised labels to correlate.
        mean: Mean monthly Spearman correlation.
        median: Median monthly correlation.
        std: Standard deviation across months.
        positive_share: Share of months with a positive coefficient.
        t_stat: Mean over the standard error of the mean, across months.
        ci_low: Lower bound of a bootstrap interval over months.
        ci_high: Upper bound of the same interval.
    """

    horizon: int
    split: str
    months: int
    mean: float | None
    median: float | None
    std: float | None
    positive_share: float | None
    t_stat: float | None
    ci_low: float | None
    ci_high: float | None

    def as_json(self) -> dict[str, object]:
        """A deterministic mapping."""
        return dict(sorted(asdict(self).items()))


@dataclass(frozen=True)
class ScoreDistribution:
    """The frozen score's shape, so a discontinuity is seen before a return is.

    Attributes:
        scope: `ALL` or a month.
        count: Observations carrying a score.
        missing: Observations in the population without one.
        mean: Mean score.
        std: Standard deviation.
        minimum: Smallest score.
        p10: Tenth percentile.
        p25: Lower quartile.
        median: Median.
        p75: Upper quartile.
        p90: Ninetieth percentile.
        maximum: Largest score.
    """

    scope: str
    count: int
    missing: int
    mean: float | None
    std: float | None
    minimum: float | None
    p10: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    maximum: float | None

    def as_json(self) -> dict[str, object]:
        """A deterministic mapping."""
        return dict(sorted(asdict(self).items()))


def _quantile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not invent an ordering."""
    order = sorted(range(len(values)), key=lambda position: values[position])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    ranked_left, ranked_right = _ranks(left), _ranks(right)
    mean_left = sum(ranked_left) / len(ranked_left)
    mean_right = sum(ranked_right) / len(ranked_right)
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(ranked_left, ranked_right, strict=True)
    )
    spread_left = math.sqrt(sum((a - mean_left) ** 2 for a in ranked_left))
    spread_right = math.sqrt(sum((b - mean_right) ** 2 for b in ranked_right))
    if spread_left == 0 or spread_right == 0:
        return None
    return covariance / (spread_left * spread_right)


def load_labels(
    db: sqlite3.Connection, decisions: Iterable[str], horizons: Sequence[int] = HORIZONS
) -> dict[tuple[str, str, int, int], tuple[str, float | None]]:
    """Read the frozen forward-return labels for the study's decision dates.

    Args:
        db: The research database, opened read-only.
        decisions: The decision sessions, as ISO dates.
        horizons: The horizons to read.

    Returns:
        `{(decision, kind, identity_id, horizon): (status, total_return)}`.
    """
    out: dict[tuple[str, str, int, int], tuple[str, float | None]] = {}
    for when in sorted(set(decisions)):
        for horizon in horizons:
            for company_id, historical_id, status, total in db.execute(
                "SELECT company_id, historical_security_id, status, total_return"
                " FROM forward_returns WHERE observation_date = ? AND horizon_sessions = ?",
                (when, int(horizon)),
            ):
                kind = "COMPANY" if company_id is not None else "HISTORICAL"
                identity_id = int(company_id if company_id is not None else historical_id)
                out[(when, kind, identity_id, int(horizon))] = (
                    str(status),
                    None if total is None else float(total),
                )
    return out


def assign_deciles(rows: Sequence[CrossSectionRow]) -> list[tuple[CrossSectionRow, int]]:
    """Cut one month's cross-section into deciles, low score to high."""
    ordered = sorted(rows, key=lambda row: (row.final_score or 0.0, row.kind, row.identity_id))
    total = len(ordered)
    assigned: list[tuple[CrossSectionRow, int]] = []
    for position, row in enumerate(ordered):
        bucket = min(DECILES, int(position * DECILES / total) + 1)
        assigned.append((row, bucket))
    return assigned


def decile_diagnostics(
    rows: Sequence[CrossSectionRow],
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    split_of: Mapping[str, str],
    *,
    horizons: Sequence[int] = HORIZONS,
) -> tuple[list[DecileStat], dict[str, int], list[dict[str, object]]]:
    """Assign deciles month by month and summarise each over every horizon.

    Args:
        rows: The eligible cross-section, every month together. Only rows
            carrying a score take part.
        labels: The frozen labels, as `load_labels` returns them.
        split_of: `{month: block}`.
        horizons: The horizons to summarise.

    Returns:
        The statistics, the months skipped by reason, and one record per month
        naming its cross-section size and whether it was cut.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.final_score is not None:
            by_month[row.month].append(row)

    skipped: dict[str, int] = defaultdict(int)
    months: list[dict[str, object]] = []
    buckets: dict[tuple[int, int, str], list[tuple[float, str, float | None]]] = defaultdict(list)
    for month in sorted(by_month):
        population = by_month[month]
        if len(population) < MIN_DECILE_CROSS_SECTION:
            skipped["BELOW_MINIMUM_CROSS_SECTION"] += 1
            months.append({"month": month, "observations": len(population), "cut": False})
            continue
        months.append({"month": month, "observations": len(population), "cut": True})
        block = split_of.get(month, "ALL")
        for row, bucket in assign_deciles(population):
            for horizon in horizons:
                status, total = labels.get(
                    (row.decision, row.kind, row.identity_id, horizon), ("ABSENT", None)
                )
                scopes = (block,) if block == "ALL" else (block, "ALL")
                for scope in scopes:
                    buckets[(bucket, horizon, scope)].append(
                        (row.final_score or 0.0, status, total)
                    )

    stats: list[DecileStat] = []
    for (bucket, horizon, scope), entries in sorted(buckets.items()):
        statuses: dict[str, int] = defaultdict(int)
        realised: list[float] = []
        for _score, status, total in entries:
            statuses[status] += 1
            if status == "REALISED" and total is not None:
                realised.append(total)
        scores = [score for score, _status, _total in entries]
        stats.append(
            DecileStat(
                decile=bucket,
                horizon=horizon,
                split=scope,
                observations=len(entries),
                realised=len(realised),
                mean=sum(realised) / len(realised) if realised else None,
                median=_quantile(realised, 0.5),
                win_rate=(
                    sum(1 for value in realised if value > 0) / len(realised) if realised else None
                ),
                volatility=_std(realised),
                p10=_quantile(realised, 0.10),
                p90=_quantile(realised, 0.90),
                statuses=dict(sorted(statuses.items())),
                mean_score=sum(scores) / len(scores) if scores else None,
            )
        )
    return stats, dict(skipped), months


def contrast_series(
    rows: Sequence[CrossSectionRow],
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    split_of: Mapping[str, str],
    *,
    horizon: int = PRIMARY_HORIZON,
) -> tuple[list[dict[str, object]], dict[str, dict[str, float | int | None]]]:
    """`D10 - D1` month by month, and its spread measured across months.

    The contrast is defined before any decile is computed and is not redefined
    here. What this adds is the only honest way to put an interval around it:
    a month is one observation, because two securities in the same month share
    a market and treating them as independent draws would shrink every interval
    by a factor nobody earned.

    Args:
        rows: The eligible cross-section.
        labels: The frozen labels.
        split_of: `{month: block}`.
        horizon: The horizon to contrast at.

    Returns:
        The monthly series, and a summary per block and overall.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.final_score is not None:
            by_month[row.month].append(row)

    monthly: list[dict[str, object]] = []
    series: dict[str, list[float]] = defaultdict(list)
    for month in sorted(by_month):
        population = by_month[month]
        if len(population) < MIN_DECILE_CROSS_SECTION:
            continue
        buckets: dict[int, list[float]] = defaultdict(list)
        for row, bucket in assign_deciles(population):
            status, total = labels.get(
                (row.decision, row.kind, row.identity_id, horizon), ("ABSENT", None)
            )
            if status == "REALISED" and total is not None:
                buckets[bucket].append(total)
        low, high = buckets.get(1, []), buckets.get(DECILES, [])
        if not low or not high:
            continue
        contrast = sum(high) / len(high) - sum(low) / len(low)
        block = split_of.get(month, "ALL")
        monthly.append(
            {
                "month": month,
                "horizon": horizon,
                "split": block,
                "d1_observations": len(low),
                "d10_observations": len(high),
                "d10_minus_d1": contrast,
            }
        )
        series[block].append(contrast)
        if block != "ALL":
            series["ALL"].append(contrast)

    summary: dict[str, dict[str, float | int | None]] = {}
    for scope, values in sorted(series.items()):
        mean = sum(values) / len(values) if values else None
        deviation = _std(values)
        error = deviation / math.sqrt(len(values)) if deviation and values else None
        low_bound, high_bound = _bootstrap(values)
        summary[scope] = {
            "months": len(values),
            "mean": mean,
            "median": _quantile(values, 0.5),
            "std": deviation,
            "standard_error": error,
            "t_stat": mean / error if mean is not None and error else None,
            "positive_months": (
                sum(1 for value in values if value > 0) / len(values) if values else None
            ),
            "ci_low": low_bound,
            "ci_high": high_bound,
        }
    return monthly, summary


def rank_information_coefficient(
    rows: Sequence[CrossSectionRow],
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    split_of: Mapping[str, str],
    *,
    horizons: Sequence[int] = HORIZONS,
) -> tuple[list[InformationCoefficient], list[dict[str, object]]]:
    """Correlate the frozen score with the outcome, one month at a time.

    Args:
        rows: The eligible cross-section.
        labels: The frozen labels.
        split_of: `{month: block}`.
        horizons: The horizons to correlate.

    Returns:
        The summaries per horizon and block, and the monthly series itself.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.final_score is not None:
            by_month[row.month].append(row)

    monthly: list[dict[str, object]] = []
    series: dict[tuple[int, str], list[float]] = defaultdict(list)
    for month in sorted(by_month):
        block = split_of.get(month, "ALL")
        for horizon in horizons:
            scores: list[float] = []
            outcomes: list[float] = []
            for row in by_month[month]:
                status, total = labels.get(
                    (row.decision, row.kind, row.identity_id, horizon), ("ABSENT", None)
                )
                if status != "REALISED" or total is None or row.final_score is None:
                    continue
                scores.append(row.final_score)
                outcomes.append(total)
            coefficient = _spearman(scores, outcomes)
            monthly.append(
                {
                    "month": month,
                    "horizon": horizon,
                    "split": block,
                    "observations": len(scores),
                    "ic": coefficient,
                }
            )
            if coefficient is None:
                continue
            series[(horizon, block)].append(coefficient)
            if block != "ALL":
                series[(horizon, "ALL")].append(coefficient)

    summaries: list[InformationCoefficient] = []
    for (horizon, scope), values in sorted(series.items()):
        mean = sum(values) / len(values) if values else None
        deviation = _std(values)
        error = deviation / math.sqrt(len(values)) if deviation and values else None
        low, high = _bootstrap(values)
        summaries.append(
            InformationCoefficient(
                horizon=horizon,
                split=scope,
                months=len(values),
                mean=mean,
                median=_quantile(values, 0.5),
                std=deviation,
                positive_share=(
                    sum(1 for value in values if value > 0) / len(values) if values else None
                ),
                t_stat=mean / error if mean is not None and error else None,
                ci_low=low,
                ci_high=high,
            )
        )
    return summaries, monthly


def _bootstrap(values: Sequence[float]) -> tuple[float | None, float | None]:
    """A seeded percentile bootstrap of the mean, resampling months."""
    if len(values) < 3:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - a seeded research bootstrap
    means: list[float] = []
    size = len(values)
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [values[rng.randrange(size)] for _ in range(size)]
        means.append(sum(sample) / size)
    means.sort()
    return _quantile(means, 0.025), _quantile(means, 0.975)


def score_distribution(
    rows: Sequence[CrossSectionRow], population: Sequence[CrossSectionRow]
) -> list[ScoreDistribution]:
    """The frozen score's shape overall and month by month.

    Args:
        rows: Observations carrying a score.
        population: The wider population the missing count is measured against.

    Returns:
        One row for `ALL` and one per month, in order.
    """
    scored: dict[str, list[float]] = defaultdict(list)
    total_population: dict[str, int] = defaultdict(int)
    for row in population:
        total_population[row.month] += 1
        total_population["ALL"] += 1
    for row in rows:
        if row.final_score is None:
            continue
        scored[row.month].append(row.final_score)
        scored["ALL"].append(row.final_score)

    out: list[ScoreDistribution] = []
    for scope in ["ALL", *sorted(key for key in scored if key != "ALL")]:
        values = scored.get(scope, [])
        out.append(
            ScoreDistribution(
                scope=scope,
                count=len(values),
                missing=total_population.get(scope, 0) - len(values),
                mean=sum(values) / len(values) if values else None,
                std=_std(values),
                minimum=min(values) if values else None,
                p10=_quantile(values, 0.10),
                p25=_quantile(values, 0.25),
                median=_quantile(values, 0.50),
                p75=_quantile(values, 0.75),
                p90=_quantile(values, 0.90),
                maximum=max(values) if values else None,
            )
        )
    return out
