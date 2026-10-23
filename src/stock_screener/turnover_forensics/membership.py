"""Why the portfolio's membership changes, and how fast.

Turnover is a consequence; membership change is the cause. So this module does
not ask how much was traded but which identities left, which arrived, and what
made each of them eligible or not at the moment the decision was taken.

The distinction that matters is between a name that fell in the ranking and a
name that stopped being available at all. Both produce a trade, and calling
both "ranking churn" would attribute to the score a decision the universe made.
The cross-section rows carry the exclusion that applied at each decision, so the
attribution is read rather than inferred.

A second question runs alongside: is the drift over a quarter anything more than
the drift already visible month to month? The monthly ledger answers that on its
own, because the top decile it selects every month can be followed forward one,
two and three months without simulating anything.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING, Any

__all__ = [
    "CAUSE_BY_EXCLUSION",
    "DISTANCE_BANDS",
    "RANK_MOVE_BANDS",
    "cause_of",
    "holding_episodes",
    "membership_half_life",
    "portfolios_by_decision",
    "re_entry",
    "retention",
    "survival_curve",
    "transitions",
]

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Which kind of change each cross-section exclusion represents. The score's own
#: ranking is deliberately *not* in this table: a name present and executable
#: that simply ranks below the cutoff is ranking churn, and everything here is
#: something else.
CAUSE_BY_EXCLUSION: dict[str, str] = {
    "SCORE_NOT_ELIGIBLE": "SCORE_READINESS",
    "SCORE_INSUFFICIENT_DATA": "SCORE_READINESS",
    "SCORE_UNSUPPORTED_SECTOR": "SCORE_READINESS",
    "NO_SIGNAL_SNAPSHOT": "SCORE_READINESS",
    "STALE_FUNDAMENTALS": "FEATURE_FRESHNESS",
    "NOT_TRADABLE_UNVERIFIED": "TRADABILITY",
    "NOT_TRADABLE_UNTRADABLE": "TRADABILITY",
    "NO_EXECUTION_PRICE": "EXECUTION_AVAILABILITY",
    "SAME_DAY_HISTORICAL_FILING": "EVIDENCE_TIMING",
}

#: Rank-percentile bands for describing where a name sat, declared on ranks
#: alone and never on returns.
DISTANCE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("TOP_5_PCT", 0.0, 0.05),
    ("PCT_5_TO_10", 0.05, 0.10),
    ("PCT_10_TO_15", 0.10, 0.15),
    ("PCT_15_TO_20", 0.15, 0.20),
    ("BEYOND_20_PCT", 0.20, 1.01),
)

#: How far a name moved in rank percentile across the interval.
RANK_MOVE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("UNDER_5_POINTS", 0.0, 0.05),
    ("POINTS_5_TO_10", 0.05, 0.10),
    ("POINTS_10_TO_20", 0.10, 0.20),
    ("OVER_20_POINTS", 0.20, 10.0),
)


def cause_of(row: Mapping[str, Any] | None) -> str:
    """Classify why an identity was not selectable at one decision.

    Args:
        row: The identity's cross-section row, or None when it was absent.

    Returns:
        A cause label. `RANKING` means the name was executable and simply did
        not rank high enough, which is the only case the score is responsible
        for.
    """
    if row is None:
        return "NOT_IN_UNIVERSE"
    exclusion = row.get("exclusion")
    if exclusion is None:
        return "RANKING"
    return CAUSE_BY_EXCLUSION.get(str(exclusion), "OTHER")


def portfolios_by_decision(
    holdings: Sequence[Mapping[str, Any]], key: str = "execution"
) -> dict[str, set[tuple[str, int]]]:
    """Group held identities by the decision or execution that produced them.

    Args:
        holdings: Rows of a holdings ledger.
        key: The field to group on.

    Returns:
        `{key value: {(kind, identity_id)}}`.
    """
    grouped: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for row in holdings:
        grouped[str(row[key])].add((str(row["kind"]), int(row["identity_id"])))
    return dict(grouped)


def retention(
    portfolios: Mapping[str, set[tuple[str, int]]], order: Sequence[str]
) -> dict[str, Any]:
    """Measure how much of each portfolio survives into the next one.

    The denominator is the earlier portfolio: the share of what was held that is
    still held one step later. A name that leaves and returns later counts as
    having left.

    Args:
        portfolios: Held identities per decision.
        order: Decisions in chronological order.

    Returns:
        The retention series and its distribution.
    """
    rows: list[dict[str, Any]] = []
    for earlier, later in pairwise(order):
        before, after = portfolios[earlier], portfolios[later]
        rows.append(
            {
                "from": earlier,
                "to": later,
                "held_before": len(before),
                "held_after": len(after),
                "retained": len(before & after),
                "retention": len(before & after) / len(before) if before else None,
            }
        )
    values = [row["retention"] for row in rows if row["retention"] is not None]
    ordered = sorted(values)
    last = max(len(ordered) - 1, 0)
    return {
        "denominator": "identities held at the earlier decision",
        "steps": rows,
        "mean": sum(ordered) / len(ordered) if ordered else None,
        "median": ordered[len(ordered) // 2] if ordered else None,
        "p25": ordered[int(0.25 * last)] if ordered else None,
        "p75": ordered[int(0.75 * last)] if ordered else None,
        "minimum": ordered[0] if ordered else None,
        "maximum": ordered[-1] if ordered else None,
    }


def survival_curve(
    portfolios: Mapping[str, set[tuple[str, int]]],
    order: Sequence[str],
    lags: Sequence[int],
) -> dict[str, Any]:
    """Follow each portfolio forward by several steps without re-selecting.

    Args:
        portfolios: Held identities per decision.
        order: Decisions in chronological order.
        lags: How many steps forward to look.

    Returns:
        `{"lag_<n>": distribution}` plus the observation count at each lag.
    """
    curve: dict[str, Any] = {}
    for lag in lags:
        values = [
            len(portfolios[order[index]] & portfolios[order[index + lag]])
            / len(portfolios[order[index]])
            for index in range(len(order) - lag)
            if portfolios[order[index]]
        ]
        ordered = sorted(values)
        last = max(len(ordered) - 1, 0)
        curve[f"lag_{lag}"] = {
            "observations": len(ordered),
            "mean": sum(ordered) / len(ordered) if ordered else None,
            "median": ordered[len(ordered) // 2] if ordered else None,
            "p25": ordered[int(0.25 * last)] if ordered else None,
            "p75": ordered[int(0.75 * last)] if ordered else None,
            "minimum": ordered[0] if ordered else None,
            "maximum": ordered[-1] if ordered else None,
        }
    return curve


def membership_half_life(curve: Mapping[str, Any], lags: Sequence[int]) -> dict[str, Any]:
    """Describe, not model, how long half a portfolio lasts.

    Two readings are given. The interpolated one reads the observed curve where
    it crosses one half. The geometric one asks what the first step alone would
    imply if each step were independent — and the gap between them is the
    finding, because a curve that decays more slowly than its first step
    predicts has a persistent core inside a churning fringe.

    Args:
        curve: The output of `survival_curve`.
        lags: The lags present in that curve, ascending.

    Returns:
        Both readings, or a statement that the curve does not reach one half.
    """
    means = [(lag, curve[f"lag_{lag}"]["mean"]) for lag in lags]
    usable = [(lag, value) for lag, value in means if value is not None]
    if not usable:
        return {"supported": False, "reason": "no survival observations"}
    first_lag, first_mean = usable[0]
    geometric: float | None = None
    if 0.0 < first_mean < 1.0:
        from math import log

        geometric = first_lag * log(0.5) / log(first_mean)
    crossing: float | None = None
    for (early_lag, early), (late_lag, late) in pairwise(usable):
        if early >= 0.5 > late:
            span = early - late
            crossing = early_lag + (early - 0.5) / span * (late_lag - early_lag)
            break
    if crossing is None and usable[-1][1] >= 0.5:
        return {
            "supported": False,
            "reason": "survival stays at or above one half over the observed lags",
            "geometric_from_first_step": geometric,
            "observed_means": dict(usable),
        }
    return {
        "supported": crossing is not None,
        "unit": "steps of the decision interval",
        "interpolated_half_life": crossing,
        "geometric_from_first_step": geometric,
        "decay_is_slower_than_geometric": (
            None if crossing is None or geometric is None else bool(crossing > geometric)
        ),
        "observed_means": dict(usable),
        "caveat": (
            "descriptive interpolation between observed lags, not a fitted model, "
            "and not used to choose any parameter"
        ),
    }


def transitions(
    portfolios: Mapping[str, set[tuple[str, int]]],
    order: Sequence[str],
    cross_sections: Mapping[str, Mapping[tuple[str, int], Mapping[str, Any]]],
    selected: Mapping[str, set[tuple[str, int]]],
) -> dict[str, Any]:
    """Classify every membership change by what actually caused it.

    Args:
        portfolios: Held identities per decision.
        order: Decisions in chronological order.
        cross_sections: `{decision: {identity: row}}` for the frozen universe.
        selected: Identities the ranking chose at each decision, held or not.

    Returns:
        Counts per transition class, per-step rows, and the exit and entry
        breakdowns the brief asks for separately.
    """
    counts: dict[str, int] = defaultdict(int)
    exit_causes: dict[str, int] = defaultdict(int)
    entry_causes: dict[str, int] = defaultdict(int)
    steps: list[dict[str, Any]] = []
    for earlier, later in pairwise(order):
        before, after = portfolios[earlier], portfolios[later]
        now = cross_sections.get(later, {})
        was = cross_sections.get(earlier, {})
        leaving: dict[str, int] = defaultdict(int)
        arriving: dict[str, int] = defaultdict(int)
        overlap = len(before & after)
        counts["IN_TO_IN"] += overlap
        for identity in before - after:
            cause = cause_of(now.get(identity))
            counts[f"IN_TO_OUT_{cause}"] += 1
            exit_causes[cause] += 1
            leaving[cause] += 1
        for identity in after - before:
            cause = cause_of(was.get(identity))
            counts[f"OUT_TO_IN_{cause}"] += 1
            entry_causes[cause] += 1
            arriving[cause] += 1
        steps.append(
            {
                "from": earlier,
                "to": later,
                "in_to_in": overlap,
                "out_by_cause": dict(sorted(leaving.items())),
                "in_by_cause": dict(sorted(arriving.items())),
            }
        )
    exits = sum(exit_causes.values())
    entries = sum(entry_causes.values())
    unheld = {
        decision: len(selected.get(decision, set()) - portfolios.get(decision, set()))
        for decision in order
    }
    return {
        "what_ranking_means": (
            "the identity was present, priced, score-ready and tradable at the "
            "other decision and simply did not rank inside the selected fraction. "
            "Every other label is an availability change, not a score decision."
        ),
        "counts": dict(sorted(counts.items())),
        "exits_by_cause": dict(sorted(exit_causes.items())),
        "exit_shares": {cause: value / exits for cause, value in sorted(exit_causes.items())}
        if exits
        else {},
        "entries_by_cause": dict(sorted(entry_causes.items())),
        "entry_shares": {cause: value / entries for cause, value in sorted(entry_causes.items())}
        if entries
        else {},
        "total_exits": exits,
        "total_entries": entries,
        "selected_but_not_held": dict(sorted(unheld.items())),
        "steps": steps,
    }


def holding_episodes(
    portfolios: Mapping[str, set[tuple[str, int]]], order: Sequence[str]
) -> dict[str, Any]:
    """Measure how long positions actually last, and how often they come back.

    An episode is a run of consecutive decisions at which one identity was held.
    A second episode for the same identity is a re-entry, which is the signature
    of a name sitting close enough to the cutoff to cross it repeatedly.

    Args:
        portfolios: Held identities per decision.
        order: Decisions in chronological order.

    Returns:
        The episode-length distribution, bucketed counts, and re-entry statistics.
    """
    runs: dict[tuple[str, int], list[list[int]]] = defaultdict(list)
    for index, decision in enumerate(order):
        for identity in portfolios[decision]:
            spans = runs[identity]
            if spans and spans[-1][1] == index - 1:
                spans[-1][1] = index
            else:
                spans.append([index, index])
    lengths: list[int] = []
    gaps: list[int] = []
    re_entry_events = 0
    re_entry_identities = 0
    for spans in runs.values():
        lengths.extend(span[1] - span[0] + 1 for span in spans)
        if len(spans) > 1:
            re_entry_identities += 1
            re_entry_events += len(spans) - 1
            gaps.extend(later[0] - earlier[1] - 1 for earlier, later in pairwise(spans))
    ordered = sorted(lengths)
    last = max(len(ordered) - 1, 0)
    buckets = {
        "one": sum(1 for value in lengths if value == 1),
        "two": sum(1 for value in lengths if value == 2),
        "three_to_four": sum(1 for value in lengths if 3 <= value <= 4),
        "five_to_eight": sum(1 for value in lengths if 5 <= value <= 8),
        "nine_or_more": sum(1 for value in lengths if value >= 9),
    }
    ordered_gaps = sorted(gaps)
    entrants = sum(len(spans) for spans in runs.values())
    return {
        "unit": "consecutive decisions held",
        "episodes": len(lengths),
        "identities": len(runs),
        "mean": sum(ordered) / len(ordered) if ordered else None,
        "median": ordered[len(ordered) // 2] if ordered else None,
        "p25": ordered[int(0.25 * last)] if ordered else None,
        "p75": ordered[int(0.75 * last)] if ordered else None,
        "p90": ordered[int(0.90 * last)] if ordered else None,
        "maximum": ordered[-1] if ordered else None,
        "buckets": buckets,
        "re_entry": {
            "identities_with_at_least_one_re_entry": re_entry_identities,
            "re_entry_events": re_entry_events,
            "episodes_total": entrants,
            "share_of_episodes_that_are_re_entries": (
                re_entry_events / entrants if entrants else None
            ),
            "median_decisions_outside_before_return": (
                ordered_gaps[len(ordered_gaps) // 2] if ordered_gaps else None
            ),
            "minimum_decisions_outside": ordered_gaps[0] if ordered_gaps else None,
        },
    }


def re_entry(episodes: Mapping[str, Any]) -> dict[str, Any]:
    """Return just the re-entry view of a holding-episode summary.

    Args:
        episodes: The output of `holding_episodes`.

    Returns:
        The re-entry block.
    """
    return dict(episodes["re_entry"])
