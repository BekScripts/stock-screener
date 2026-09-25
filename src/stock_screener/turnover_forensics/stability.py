"""How stable the ranking itself is, and where the churning names sit in it.

If turnover is caused by membership change and membership change is mostly the
ranking moving, the next question is whether the ranking moves a lot or moves a
little in a place where a little is enough. A selection rule with a hard cutoff
converts an arbitrarily small score change into a complete position, so the
distinction is not pedantic: the same turnover means something quite different
depending on whether names cross the line by a hair or by a mile.

Every band here is declared on ranks, never on returns, and nothing in this
module chooses a number for a future rule. Measuring where names sit relative to
a cutoff is not the same as picking a buffer, and picking one from these data
would use the outcome to select the parameter.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from stock_screener.signal_review.statistics import spearman

from .membership import DISTANCE_BANDS, RANK_MOVE_BANDS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "boundary_churn",
    "cutoffs",
    "dispersion",
    "rank_percentiles",
    "score_stability",
    "size_effect",
    "stability",
    "target_count_effect",
]


def rank_percentiles(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]:
    """Rank one executable cross-section, best score first.

    Args:
        rows: Executable rows for a single decision, each carrying `final_score`.

    Returns:
        `({identity: rank percentile}, {identity: score})`, where percentile 0.0
        is the highest-scoring name.
    """
    scored = [
        ((str(row["kind"]), int(row["identity_id"])), float(row["final_score"]))
        for row in rows
        if row.get("final_score") is not None
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    size = len(scored)
    percentiles = {
        identity: (index / size if size else 0.0) for index, (identity, _) in enumerate(scored)
    }
    return percentiles, dict(scored)


def cutoffs(
    ranked: Mapping[str, tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]],
    selected: Mapping[str, set[tuple[str, int]]],
) -> dict[str, Any]:
    """Find the score that separated the selected fraction at each decision.

    Args:
        ranked: `{decision: (percentiles, scores)}`.
        selected: Identities selected at each decision.

    Returns:
        Per decision: the cutoff score, the next score below it, and the gap.
    """
    result: dict[str, Any] = {}
    for decision, (_, scores) in sorted(ranked.items()):
        chosen = [
            scores[identity] for identity in selected.get(decision, set()) if identity in scores
        ]
        if not chosen:
            result[decision] = None
            continue
        cutoff = min(chosen)
        below = [value for value in scores.values() if value < cutoff]
        nearest = max(below) if below else None
        result[decision] = {
            "cutoff_score": cutoff,
            "highest_score_below_cutoff": nearest,
            "boundary_gap": None if nearest is None else cutoff - nearest,
            "selected": len(chosen),
            "executable": len(scores),
        }
    return result


def _band(value: float, bands: Sequence[tuple[str, float, float]]) -> str:
    """Return the band a value falls into."""
    for name, low, high in bands:
        if low <= value < high:
            return name
    return bands[-1][0]


def boundary_churn(
    order: Sequence[str],
    portfolios: Mapping[str, set[tuple[str, int]]],
    ranked: Mapping[str, tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]],
) -> dict[str, Any]:
    """Describe where entrants and exits sat, and how far they moved.

    Args:
        order: Decisions in chronological order.
        portfolios: Held identities per decision.
        ranked: `{decision: (percentiles, scores)}`.

    Returns:
        Band counts for entrants and exits, the rank-move distribution, and the
        share of changes that are small moves near the cutoff.
    """
    entrant_prior: dict[str, int] = defaultdict(int)
    exit_current: dict[str, int] = defaultdict(int)
    moves: dict[str, int] = defaultdict(int)
    move_values: list[float] = []
    unranked = {"entrants": 0, "exits": 0}
    for earlier, later in pairwise(order):
        before, after = portfolios[earlier], portfolios[later]
        was_pct = ranked.get(earlier, ({}, {}))[0]
        now_pct = ranked.get(later, ({}, {}))[0]
        for identity in after - before:
            prior = was_pct.get(identity)
            if prior is None:
                unranked["entrants"] += 1
                continue
            entrant_prior[_band(prior, DISTANCE_BANDS)] += 1
            current = now_pct.get(identity)
            if current is not None:
                move = abs(prior - current)
                moves[_band(move, RANK_MOVE_BANDS)] += 1
                move_values.append(move)
        for identity in before - after:
            current = now_pct.get(identity)
            if current is None:
                unranked["exits"] += 1
                continue
            exit_current[_band(current, DISTANCE_BANDS)] += 1
            prior = was_pct.get(identity)
            if prior is not None:
                move = abs(prior - current)
                moves[_band(move, RANK_MOVE_BANDS)] += 1
                move_values.append(move)
    total_moves = sum(moves.values())
    ordered = sorted(move_values)
    near = moves["UNDER_5_POINTS"] + moves["POINTS_5_TO_10"]
    return {
        "bands_declared_on": "rank percentile only; no return was consulted",
        "entrant_prior_band": dict(sorted(entrant_prior.items())),
        "entrant_prior_shares": {
            name: value / sum(entrant_prior.values())
            for name, value in sorted(entrant_prior.items())
        }
        if entrant_prior
        else {},
        "exit_current_band": dict(sorted(exit_current.items())),
        "exit_current_shares": {
            name: value / sum(exit_current.values()) for name, value in sorted(exit_current.items())
        }
        if exit_current
        else {},
        "rank_move_band": dict(sorted(moves.items())),
        "rank_move_shares": {name: value / total_moves for name, value in sorted(moves.items())}
        if total_moves
        else {},
        "median_rank_move": ordered[len(ordered) // 2] if ordered else None,
        "mean_rank_move": sum(ordered) / len(ordered) if ordered else None,
        "share_moving_under_10_points": near / total_moves if total_moves else None,
        "not_ranked_at_the_other_decision": unranked,
        "this_does_not_authorise_a_buffer": (
            "these bands describe the observed distribution. Choosing a "
            "hysteresis width from them would select a parameter using the "
            "outcome it is meant to be tested against."
        ),
    }


def stability(
    ranked: Mapping[str, tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]],
    order: Sequence[str],
    lags: Sequence[int],
) -> dict[str, Any]:
    """Measure rank persistence across the whole cross-section at several lags.

    Args:
        ranked: `{decision: (percentiles, scores)}`.
        order: Decisions in chronological order.
        lags: Separations, in decision steps.

    Returns:
        `{"lag_<n>": distribution}` of Spearman rank correlation over identities
        present at both decisions.
    """
    result: dict[str, Any] = {}
    for lag in lags:
        values: list[float] = []
        pairs: list[int] = []
        for index in range(len(order) - lag):
            early = ranked.get(order[index], ({}, {}))[1]
            late = ranked.get(order[index + lag], ({}, {}))[1]
            common = sorted(set(early) & set(late))
            if len(common) < 2:
                continue
            correlation = spearman(
                [early[identity] for identity in common],
                [late[identity] for identity in common],
            )
            if correlation is not None:
                values.append(correlation)
                pairs.append(len(common))
        ordered = sorted(values)
        result[f"lag_{lag}"] = {
            "observations": len(ordered),
            "identities_per_observation": (sum(pairs) / len(pairs) if pairs else None),
            "mean": sum(ordered) / len(ordered) if ordered else None,
            "median": ordered[len(ordered) // 2] if ordered else None,
            "minimum": ordered[0] if ordered else None,
            "maximum": ordered[-1] if ordered else None,
        }
    return result


def score_stability(
    ranked: Mapping[str, tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]],
    order: Sequence[str],
    lags: Sequence[int],
) -> dict[str, Any]:
    """Measure how much the raw score itself moves, not just the ordering.

    Args:
        ranked: `{decision: (percentiles, scores)}`.
        order: Decisions in chronological order.
        lags: Separations, in decision steps.

    Returns:
        Absolute score change statistics at each lag.
    """
    result: dict[str, Any] = {}
    for lag in lags:
        deltas: list[float] = []
        for index in range(len(order) - lag):
            early = ranked.get(order[index], ({}, {}))[1]
            late = ranked.get(order[index + lag], ({}, {}))[1]
            deltas.extend(
                abs(late[identity] - early[identity]) for identity in set(early) & set(late)
            )
        ordered = sorted(deltas)
        last = max(len(ordered) - 1, 0)
        result[f"lag_{lag}"] = {
            "observations": len(ordered),
            "mean_absolute_change": sum(ordered) / len(ordered) if ordered else None,
            "median_absolute_change": ordered[len(ordered) // 2] if ordered else None,
            "p90_absolute_change": ordered[int(0.90 * last)] if ordered else None,
            "maximum_absolute_change": ordered[-1] if ordered else None,
        }
    return result


def dispersion(
    ranked: Mapping[str, tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]],
    order: Sequence[str],
    turnover: Mapping[str, float],
    retention_by_decision: Mapping[str, float],
) -> dict[str, Any]:
    """Relate each cross-section's score spread to how much it churned.

    Args:
        ranked: `{decision: (percentiles, scores)}`.
        order: Decisions in chronological order.
        turnover: `{decision: one-way turnover}`.
        retention_by_decision: `{decision: retention into that decision}`.

    Returns:
        Per-decision dispersion with turnover and retention beside it, and the
        rank correlation between dispersion and churn.
    """
    rows: list[dict[str, Any]] = []
    for decision in order:
        scores = sorted(ranked.get(decision, ({}, {}))[1].values())
        if not scores:
            continue
        count = len(scores)
        mean = sum(scores) / count
        variance = sum((value - mean) ** 2 for value in scores) / count
        last = count - 1
        rows.append(
            {
                "decision": decision,
                "executable": count,
                "score_mean": mean,
                "score_standard_deviation": variance**0.5,
                "score_p25": scores[int(0.25 * last)],
                "score_p75": scores[int(0.75 * last)],
                "score_iqr": scores[int(0.75 * last)] - scores[int(0.25 * last)],
                "turnover": turnover.get(decision),
                "retention_into_this_decision": retention_by_decision.get(decision),
            }
        )
    paired = [
        (row["score_standard_deviation"], row["turnover"])
        for row in rows
        if row["turnover"] is not None
    ]
    correlation = (
        spearman([left for left, _ in paired], [right for _, right in paired])
        if len(paired) > 1
        else None
    )
    return {
        "per_decision": rows,
        "spearman_dispersion_versus_turnover": correlation,
        "reading": (
            "descriptive association across seventeen observations. No causal "
            "claim, and no rule derived from it."
        ),
    }


def size_effect(
    selection: Sequence[Mapping[str, Any]], turnover: Mapping[str, float]
) -> dict[str, Any]:
    """Relate universe and portfolio size to turnover at each decision.

    Args:
        selection: Rows of the selection ledger.
        turnover: `{execution: one-way turnover}`.

    Returns:
        Per-decision sizes with turnover, and the rank correlations.
    """
    rows: list[dict[str, Any]] = []
    eligible: list[int] = []
    chosen: list[int] = []
    churn: list[float] = []
    for row in selection:
        execution = str(row["execution"])
        measured = turnover.get(execution)
        count_eligible = int(row["eligible"])
        count_selected = int(row["selected"])
        rows.append(
            {
                "decision": str(row["decision"]),
                "execution": execution,
                "universe": int(row["universe"]),
                "eligible": count_eligible,
                "selected": count_selected,
                "turnover": measured,
            }
        )
        if measured is not None:
            eligible.append(count_eligible)
            chosen.append(count_selected)
            churn.append(measured)
    return {
        "per_decision": rows,
        "spearman_eligible_versus_turnover": (
            spearman(eligible, churn) if len(churn) > 1 else None
        ),
        "spearman_selected_versus_turnover": (spearman(chosen, churn) if len(churn) > 1 else None),
        "eligible_minimum": min(eligible) if eligible else None,
        "eligible_maximum": max(eligible) if eligible else None,
        "selected_minimum": min(chosen) if chosen else None,
        "selected_maximum": max(chosen) if chosen else None,
    }


def target_count_effect(
    selection: Sequence[Mapping[str, Any]], navs: Mapping[str, float]
) -> dict[str, Any]:
    """Isolate the turnover a changing target count forces on its own.

    Holding a fixed fraction of a growing cross-section means the position count
    moves, and every retained name's equal weight moves with it. That component
    is arithmetic: going from `n` to `m` names changes each retained target
    weight from `1/n` to `1/m`, which must be traded whatever the ranking did.

    Args:
        selection: Rows of the selection ledger, in order.
        navs: `{execution: pre-trade NAV}`, for context only.

    Returns:
        Per-step forced resizing turnover and its total.
    """
    rows = sorted(selection, key=lambda row: str(row["execution"]))
    steps: list[dict[str, Any]] = []
    for earlier, later in pairwise(rows):
        before, after = int(earlier["selected"]), int(later["selected"])
        if before <= 0 or after <= 0:
            continue
        # A retained name must move from 1/before to 1/after of NAV. The forced
        # one-way turnover is that gap times however many names are retained,
        # bounded above by every name being retained.
        gap = abs(1.0 / after - 1.0 / before)
        steps.append(
            {
                "execution": str(later["execution"]),
                "selected_before": before,
                "selected_after": after,
                "per_name_weight_change": gap,
                "forced_resize_if_all_retained": gap * min(before, after) / 2.0,
                "pre_trade_nav": navs.get(str(later["execution"])),
            }
        )
    forced = [step["forced_resize_if_all_retained"] for step in steps]
    return {
        "steps": steps,
        "mean_forced_resize_per_rebalance": (sum(forced) / len(forced) if forced else None),
        "annual_forced_resize_upper_bound": (sum(forced) / len(forced) * 4.0 if forced else None),
        "what_this_bounds": (
            "the most that a changing position count can force on retained "
            "names. It is an upper bound on that component, not a measurement "
            "of realised resizing."
        ),
    }
