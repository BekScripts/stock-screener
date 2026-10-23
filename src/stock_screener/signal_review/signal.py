"""Is the frozen score's cross-sectional signal broad, persistent and stable?

Everything here is descriptive. No rule is created, no threshold is chosen, no
alternative is scored against another and kept. The decile construction is the
frozen one — `backtest_harness.assign_deciles` over the executable
cross-section — so a number recomputed here is the same kind of number R1.0
recorded, cut a different way.

The unit of evidence is the **month**. A month's cross-section shares a market,
so 39,001 security-months are not 39,001 independent draws; every interval and
every t-statistic in this module is computed across the 51 monthly
observations, and the longer horizons are additionally flagged because 60- and
120-session forward returns from monthly decision dates overlap heavily.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from stock_screener.backtest_harness import DECILES, PRIMARY_HORIZON, assign_deciles
from stock_screener.signal_review.statistics import spearman, spread, std

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.backtest_harness import CrossSectionRow

__all__ = [
    "HORIZON_OVERLAP",
    "MonthlySignal",
    "adv_bucket_diagnostics",
    "cross_section_size_effect",
    "decile_table",
    "horizon_diagnostics",
    "leave_one_out",
    "monthly_signal",
    "rank_persistence",
    "score_threshold_stability",
    "subset_diagnostics",
]

#: Sessions in a month, near enough: a 60- or 120-session forward return from a
#: monthly decision date overlaps the next two or five decisions.
_SESSIONS_PER_MONTH = 21

#: Stated on every horizon statistic rather than left to a footnote.
HORIZON_OVERLAP = {
    1: "independent across months",
    5: "independent across months",
    20: "adjacent months overlap by roughly nothing to one session",
    60: "overlaps about two later monthly decisions; months are not independent",
    120: "overlaps about five later monthly decisions; months are strongly dependent",
}


@dataclass(frozen=True)
class MonthlySignal:
    """One month's cross-section, cut into deciles and scored against the labels.

    Attributes:
        month: The decision month.
        decision: The decision session.
        split: The block the month belongs to.
        year: The calendar year.
        observations: Executable observations ranked that month.
        decile_means: Mean realised return per decile, 1 (weakest) to 10.
        decile_counts: Realised labels per decile.
        contrast: `D10 - D1`, the frozen long-short contrast R1.0 recorded.
        cross_section_mean: The month's mean realised return across every
            decile — what simply owning the cross-section would have earned.
        contrast_vs_mean: `D10 - cross-section mean`. A long-only top-decile
            portfolio is exposed to this, not to `D10 - D1`, because it never
            holds the bottom decile. Descriptive, and reported beside the full
            decile profile so neither measure can be read on its own.
        ic: The month's Spearman rank correlation of score against outcome.
        threshold: The lowest score inside the top decile that month.
        horizon: The forward horizon in sessions.
    """

    month: str
    decision: str
    split: str
    year: str
    observations: int
    decile_means: dict[int, float | None]
    decile_counts: dict[int, int]
    contrast: float | None
    cross_section_mean: float | None
    contrast_vs_mean: float | None
    ic: float | None
    threshold: float | None
    horizon: int

    def as_json(self) -> dict[str, Any]:
        """A deterministic mapping."""
        body = asdict(self)
        body["decile_means"] = {str(k): v for k, v in sorted(self.decile_means.items())}
        body["decile_counts"] = {str(k): v for k, v in sorted(self.decile_counts.items())}
        return dict(sorted(body.items()))


def _label(
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    row: CrossSectionRow,
    horizon: int,
) -> float | None:
    status, total = labels.get((row.decision, row.kind, row.identity_id, horizon), ("ABSENT", None))
    return total if status == "REALISED" and total is not None else None


def monthly_signal(
    rows: Sequence[CrossSectionRow],
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    split_of: Mapping[str, str],
    *,
    horizon: int = PRIMARY_HORIZON,
) -> list[MonthlySignal]:
    """Cut every month into the frozen deciles and measure it.

    Args:
        rows: The executable cross-section, every month together.
        labels: The frozen forward-return labels.
        split_of: `{month: block}`.
        horizon: The forward horizon in sessions.

    Returns:
        One record per month, in month order.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.final_score is not None:
            by_month[row.month].append(row)

    out: list[MonthlySignal] = []
    for month in sorted(by_month):
        population = by_month[month]
        buckets: dict[int, list[float]] = defaultdict(list)
        scores: list[float] = []
        outcomes: list[float] = []
        threshold: float | None = None
        for row, bucket in assign_deciles(population):
            realised = _label(labels, row, horizon)
            if bucket == DECILES and row.final_score is not None:
                threshold = (
                    row.final_score if threshold is None else min(threshold, row.final_score)
                )
            if realised is None or row.final_score is None:
                continue
            buckets[bucket].append(realised)
            scores.append(row.final_score)
            outcomes.append(realised)
        means = {
            bucket: (sum(buckets[bucket]) / len(buckets[bucket]) if buckets.get(bucket) else None)
            for bucket in range(1, DECILES + 1)
        }
        low, high = means.get(1), means.get(DECILES)
        pooled = [value for values in buckets.values() for value in values]
        average = sum(pooled) / len(pooled) if pooled else None
        out.append(
            MonthlySignal(
                month=month,
                decision=population[0].decision,
                split=split_of.get(month, "ALL"),
                year=month[:4],
                observations=len(population),
                decile_means=means,
                decile_counts={
                    bucket: len(buckets.get(bucket, ())) for bucket in range(1, DECILES + 1)
                },
                contrast=None if low is None or high is None else high - low,
                cross_section_mean=average,
                contrast_vs_mean=None if high is None or average is None else high - average,
                ic=spearman(scores, outcomes),
                threshold=threshold,
                horizon=horizon,
            )
        )
    return out


def subset_diagnostics(
    monthly: Sequence[MonthlySignal], *, key: str = "split"
) -> dict[str, dict[str, Any]]:
    """Aggregate the monthly record by split or by year.

    Args:
        monthly: The monthly records.
        key: `split` or `year`.

    Returns:
        `{subset: summary}`, with an `ALL` entry over everything.
    """
    groups: dict[str, list[MonthlySignal]] = defaultdict(list)
    for record in monthly:
        groups[getattr(record, key)].append(record)
        groups["ALL"].append(record)

    out: dict[str, dict[str, Any]] = {}
    for name, records in sorted(groups.items()):
        contrasts = [r.contrast for r in records if r.contrast is not None]
        against_mean = [r.contrast_vs_mean for r in records if r.contrast_vs_mean is not None]
        coefficients = [r.ic for r in records if r.ic is not None]
        profile: dict[str, float | None] = {
            str(bucket): _pooled(records, bucket) for bucket in range(1, DECILES + 1)
        }
        average = _pooled_mean(records)
        top = profile[str(DECILES)]
        out[name] = {
            "months": len(records),
            "observations": sum(r.observations for r in records),
            "d1_mean": _pooled(records, 1),
            "d10_mean": _pooled(records, DECILES),
            "cross_section_mean": average,
            "decile_profile": profile,
            "d10_minus_d1_pooled": _pooled_contrast(records),
            "d10_minus_cross_section_mean_pooled": (
                None if top is None or average is None else top - average
            ),
            "d10_minus_d1": spread(contrasts),
            "d10_minus_cross_section_mean": spread(against_mean),
            "rank_ic": spread(coefficients),
            "shape": _shape(profile),
        }
    return out


def _pooled(records: Sequence[MonthlySignal], bucket: int) -> float | None:
    """The count-weighted mean of one decile across months."""
    total = sum(
        (record.decile_means[bucket] or 0.0) * record.decile_counts[bucket]
        for record in records
        if record.decile_means.get(bucket) is not None
    )
    count = sum(
        record.decile_counts[bucket]
        for record in records
        if record.decile_means.get(bucket) is not None
    )
    return total / count if count else None


def _pooled_mean(records: Sequence[MonthlySignal]) -> float | None:
    """The count-weighted mean realised return across every decile."""
    total = 0.0
    count = 0
    for record in records:
        for bucket, mean in record.decile_means.items():
            if mean is None:
                continue
            total += mean * record.decile_counts[bucket]
            count += record.decile_counts[bucket]
    return total / count if count else None


def _shape(profile: Mapping[str, float | None]) -> dict[str, Any]:
    """Whether the decile profile rises, or turns up at both ends.

    A long-short contrast is only a fair summary of a monotone profile. When
    both tails outperform the middle, `D10 - D1` says almost nothing while the
    top decile may still be well above the cross-section — so the shape is
    reported rather than assumed.
    """
    values = [profile[str(bucket)] for bucket in range(1, DECILES + 1)]
    present = [value for value in values if value is not None]
    if len(present) < DECILES:
        return {"classification": "INCOMPLETE"}
    bottom = present[0]
    top = present[-1]
    middle = sorted(present[2:-2])
    trough = middle[0] if middle else None
    rising = sum(1 for a, b in pairwise(present) if b > a)
    u_shaped = bool(trough is not None and bottom > trough and top > trough and bottom > present[4])
    return {
        "classification": "U_SHAPED" if u_shaped else ("RISING" if rising >= 6 else "FLAT"),
        "steps_up": rising,
        "steps": DECILES - 1,
        "bottom_decile": bottom,
        "top_decile": top,
        "middle_trough": trough,
        "note": (
            "a U-shaped profile makes D10-D1 a poor summary: both tails beat the middle, so the "
            "long-short contrast collapses while the long-only top decile can still be well "
            "above the cross-section"
        ),
    }


def _pooled_contrast(records: Sequence[MonthlySignal]) -> float | None:
    low, high = _pooled(records, 1), _pooled(records, DECILES)
    return None if low is None or high is None else high - low


def leave_one_out(monthly: Sequence[MonthlySignal], *, key: str | None = None) -> dict[str, Any]:
    """Drop one month — or one year — at a time and recompute the two headlines.

    Diagnostic, not 51 experiments: nothing is selected on the result, and the
    question is only whether the sign survives every single removal.

    Args:
        monthly: The monthly records.
        key: None to drop one month at a time, `year` to drop one year.

    Returns:
        The range of the leave-one-out means and how often the sign held.
    """
    labels = (
        [record.month for record in monthly]
        if key is None
        else sorted({record.year for record in monthly})
    )
    contrasts: dict[str, float | None] = {}
    coefficients: dict[str, float | None] = {}
    for label in labels:
        kept = [
            record
            for record in monthly
            if (record.month != label if key is None else record.year != label)
        ]
        values = [r.contrast for r in kept if r.contrast is not None]
        ics = [r.ic for r in kept if r.ic is not None]
        contrasts[label] = sum(values) / len(values) if values else None
        coefficients[label] = sum(ics) / len(ics) if ics else None
    contrast_values = [v for v in contrasts.values() if v is not None]
    ic_values = [v for v in coefficients.values() if v is not None]
    return {
        "unit": "month" if key is None else "year",
        "removals": len(labels),
        "d10_minus_d1": {
            "min": min(contrast_values) if contrast_values else None,
            "max": max(contrast_values) if contrast_values else None,
            "positive_after_removal": sum(1 for v in contrast_values if v > 0),
            "by_removal": dict(sorted(contrasts.items())) if key is not None else None,
        },
        "rank_ic": {
            "min": min(ic_values) if ic_values else None,
            "max": max(ic_values) if ic_values else None,
            "positive_after_removal": sum(1 for v in ic_values if v > 0),
            "by_removal": dict(sorted(coefficients.items())) if key is not None else None,
        },
    }


def horizon_diagnostics(
    rows: Sequence[CrossSectionRow],
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    split_of: Mapping[str, str],
    horizons: Sequence[int],
) -> dict[str, Any]:
    """`D10 - D1` and rank IC at each horizon, each with its overlap warning.

    Args:
        rows: The executable cross-section.
        labels: The frozen labels.
        split_of: `{month: block}`.
        horizons: The horizons to measure.

    Returns:
        One entry per horizon.
    """
    out: dict[str, Any] = {}
    for horizon in horizons:
        monthly = monthly_signal(rows, labels, split_of, horizon=horizon)
        contrasts = [r.contrast for r in monthly if r.contrast is not None]
        against_mean = [r.contrast_vs_mean for r in monthly if r.contrast_vs_mean is not None]
        coefficients = [r.ic for r in monthly if r.ic is not None]
        out[str(horizon)] = {
            "horizon_sessions": horizon,
            "months": len(monthly),
            "realised_labels": sum(sum(r.decile_counts.values()) for r in monthly),
            "d10_minus_d1_pooled": _pooled_contrast(monthly),
            "d10_minus_d1": spread(contrasts),
            "d10_minus_cross_section_mean": spread(against_mean),
            "rank_ic": spread(coefficients),
            "months_overlapped": max(1, horizon // _SESSIONS_PER_MONTH),
            "independence": HORIZON_OVERLAP.get(horizon, "unknown"),
        }
    return out


def rank_persistence(rows: Sequence[CrossSectionRow]) -> dict[str, Any]:
    """How much of the ranking survives to the next month, and to the third.

    Args:
        rows: The executable cross-section.

    Returns:
        Month-to-month rank correlation, top-decile retention at one and three
        months, and the same for the top two deciles.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.final_score is not None:
            by_month[row.month].append(row)
    months = sorted(by_month)
    deciles: dict[str, dict[tuple[str, int], int]] = {}
    scores: dict[str, dict[tuple[str, int], float]] = {}
    for month in months:
        assigned = assign_deciles(by_month[month])
        deciles[month] = {row.key: bucket for row, bucket in assigned}
        scores[month] = {row.key: row.final_score or 0.0 for row, _ in assigned}

    correlations: list[float] = []
    retention: list[float] = []
    top_two_retention: list[float] = []
    survival: list[float] = []
    overlap_counts: list[int] = []
    for index, month in enumerate(months[:-1]):
        later = months[index + 1]
        shared = sorted(set(scores[month]) & set(scores[later]))
        overlap_counts.append(len(shared))
        if len(shared) >= 3:
            value = spearman(
                [scores[month][key] for key in shared], [scores[later][key] for key in shared]
            )
            if value is not None:
                correlations.append(value)
        top = [key for key, bucket in deciles[month].items() if bucket == DECILES]
        if top:
            retention.append(sum(1 for key in top if deciles[later].get(key) == DECILES) / len(top))
            top_two_retention.append(
                sum(1 for key in top if (deciles[later].get(key) or 0) >= DECILES - 1) / len(top)
            )
        if index + 3 < len(months):
            third = months[index + 3]
            if top:
                survival.append(
                    sum(1 for key in top if deciles[third].get(key) == DECILES) / len(top)
                )
    return {
        "month_pairs": len(months) - 1,
        "shared_identities_per_pair": {
            "mean": sum(overlap_counts) / len(overlap_counts) if overlap_counts else None,
            "min": min(overlap_counts) if overlap_counts else None,
            "max": max(overlap_counts) if overlap_counts else None,
        },
        "rank_correlation": spread(correlations),
        "top_decile_retention_1m": spread(retention),
        "top_two_decile_retention_1m": spread(top_two_retention),
        "top_decile_retention_3m": spread(survival),
    }


def score_threshold_stability(monthly: Sequence[MonthlySignal]) -> dict[str, Any]:
    """Where the top-decile cutoff sat each month, and how far it moved.

    Args:
        monthly: The monthly records.

    Returns:
        The distribution of the monthly cutoff. A stable band means the top
        decile is a roughly constant quality region; a wandering one means it
        is a purely relative claim. Either way no fixed cutoff is created.
    """
    thresholds = [record.threshold for record in monthly if record.threshold is not None]
    return {
        "months": len(thresholds),
        "cutoff": spread(thresholds),
        "by_month": {record.month: record.threshold for record in monthly},
    }


def cross_section_size_effect(monthly: Sequence[MonthlySignal]) -> dict[str, Any]:
    """Whether a month's signal strength moves with how many names it ranked.

    Args:
        monthly: The monthly records.

    Returns:
        Rank correlations of cross-section size against the month's contrast
        and against its IC. Diagnostic: no minimum-size rule is created.
    """
    sizes = [float(record.observations) for record in monthly]
    contrasts = [record.contrast for record in monthly]
    coefficients = [record.ic for record in monthly]
    paired_contrast = [(s, c) for s, c in zip(sizes, contrasts, strict=True) if c is not None]
    paired_ic = [(s, c) for s, c in zip(sizes, coefficients, strict=True) if c is not None]
    return {
        "cross_section_size": spread(sizes),
        "size_vs_contrast_rank_correlation": spearman(
            [s for s, _ in paired_contrast], [c for _, c in paired_contrast]
        ),
        "size_vs_ic_rank_correlation": spearman(
            [s for s, _ in paired_ic], [c for _, c in paired_ic]
        ),
        "note": "diagnostic only; no minimum universe-size rule is created",
    }


def adv_bucket_diagnostics(
    rows: Sequence[CrossSectionRow],
    labels: Mapping[tuple[str, str, int, int], tuple[str, float | None]],
    *,
    horizon: int = PRIMARY_HORIZON,
    buckets: int = 5,
) -> dict[str, Any]:
    """Signal strength across the frozen single-venue liquidity field.

    The field is `pit_feature_snapshots.venue_dollar_volume_median`, which the
    contract classifies as single-venue (IEX) and partial. Buckets are formed
    within each month so the comparison is cross-sectional, and missing ADV
    stays missing rather than becoming a bucket of its own.

    Args:
        rows: The executable cross-section.
        labels: The frozen labels.
        horizon: The forward horizon.
        buckets: How many equal-count buckets per month.

    Returns:
        Per bucket: observations, mean realised return, and the mean of the
        monthly score-return rank correlations inside that bucket.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    missing = 0
    for row in rows:
        if row.adv_dollar is None:
            missing += 1
            continue
        if row.final_score is not None:
            by_month[row.month].append(row)

    returns: dict[int, list[float]] = defaultdict(list)
    monthly_ic: dict[int, list[float]] = defaultdict(list)
    for month in sorted(by_month):
        population = sorted(
            by_month[month], key=lambda row: (row.adv_dollar or 0.0, row.kind, row.identity_id)
        )
        size = len(population)
        grouped: dict[int, list[CrossSectionRow]] = defaultdict(list)
        for position, row in enumerate(population):
            grouped[min(buckets, int(position * buckets / size) + 1)].append(row)
        for bucket, members in grouped.items():
            scores: list[float] = []
            outcomes: list[float] = []
            for row in members:
                realised = _label(labels, row, horizon)
                if realised is None or row.final_score is None:
                    continue
                returns[bucket].append(realised)
                scores.append(row.final_score)
                outcomes.append(realised)
            value = spearman(scores, outcomes)
            if value is not None:
                monthly_ic[bucket].append(value)
    return {
        "field": "venue_dollar_volume_median (single-venue IEX, partial)",
        "observations_without_adv": missing,
        "buckets": {
            str(bucket): {
                "observations": len(returns.get(bucket, ())),
                "mean_return": (
                    sum(returns[bucket]) / len(returns[bucket]) if returns.get(bucket) else None
                ),
                "mean_monthly_ic": (
                    sum(monthly_ic[bucket]) / len(monthly_ic[bucket])
                    if monthly_ic.get(bucket)
                    else None
                ),
                "ic_std_across_months": std(monthly_ic.get(bucket, [])),
            }
            for bucket in range(1, buckets + 1)
        },
        "note": (
            "bucket 1 is the least liquid fifth of each month's cross-section. "
            "Diagnostic only; no liquidity rule is created, and market "
            "capitalisation is deliberately not used as a size control because "
            "the contract records it as a lower bound."
        ),
    }


def decile_table(
    monthly: Sequence[MonthlySignal], rows: Sequence[CrossSectionRow]
) -> dict[str, Any]:
    """Mean score against mean and median outcome, decile by decile.

    Args:
        monthly: The monthly records at the primary horizon.
        rows: The executable cross-section, for the score in each bucket.

    Returns:
        One row per decile plus the linear rank association between the decile
        index and its mean outcome — preregistered, not fitted.
    """
    by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.final_score is not None:
            by_month[row.month].append(row)
    measured = {record.month for record in monthly}
    scores: dict[int, list[float]] = defaultdict(list)
    outcomes: dict[int, list[float]] = defaultdict(list)
    for month, population in by_month.items():
        if month not in measured:
            continue
        for row, bucket in assign_deciles(population):
            if row.final_score is not None:
                scores[bucket].append(row.final_score)
    for record in monthly:
        for bucket, mean in record.decile_means.items():
            if mean is not None:
                outcomes[bucket].append(mean)
    table = {
        str(bucket): {
            "mean_score": (
                sum(scores[bucket]) / len(scores[bucket]) if scores.get(bucket) else None
            ),
            "observations": len(scores.get(bucket, ())),
            "mean_return": (
                sum(outcomes[bucket]) / len(outcomes[bucket]) if outcomes.get(bucket) else None
            ),
            "median_month_return": _median(outcomes.get(bucket, [])),
        }
        for bucket in range(1, DECILES + 1)
    }
    indices = [float(bucket) for bucket in range(1, DECILES + 1)]
    raw: list[Any] = [table[str(bucket)]["mean_return"] for bucket in range(1, DECILES + 1)]
    means: list[float | None] = [None if m is None else float(m) for m in raw]
    paired = [(i, m) for i, m in zip(indices, means, strict=True) if m is not None]
    return {
        "deciles": table,
        "decile_rank_association": spearman([i for i, _ in paired], [m for _, m in paired]),
        "monotone_steps_up": sum(
            1 for a, b in pairwise(means) if a is not None and b is not None and b > a
        ),
        "steps": DECILES - 1,
    }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
