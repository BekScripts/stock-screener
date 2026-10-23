"""Applying the frozen rule to one prospective cross-section.

This is deliberately thin. The rule is `membership_hypothesis.rule.select`,
unchanged and re-used rather than reimplemented, because a prospective
reimplementation that drifted from the historical one would break the
comparison it exists to make.

What this module adds is the paired construction: one ranked cross-section
produces both target sets, so the comparator and the candidate cannot diverge on
their inputs. `paired_targets` takes a single sequence of candidates and returns
both, which makes fetching them separately impossible rather than merely
discouraged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import Candidate, rank, retention_limit, select

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["PairedTargets", "ordinary_target_count", "paired_targets"]


def ordinary_target_count(cross_section_size: int) -> int:
    """The frozen top-decile target count for a cross-section.

    Reproduces `assign_deciles`' bucket-10 size arithmetically, which is what
    R1.2 selected and what every later phase held fixed: the names at positions
    at or beyond nine tenths of the way through the ordered cross-section.

    Args:
        cross_section_size: How many executable identities there are.

    Returns:
        The number of positions to hold.
    """
    if cross_section_size <= 0:
        return 0
    return sum(
        1
        for position in range(cross_section_size)
        if min(10, int(position * 10 / cross_section_size) + 1) == 10
    )


class PairedTargets(dict[str, Any]):
    """Both strategies' target sets, from one cross-section."""


def paired_targets(
    candidates: Sequence[Candidate], incumbents: Iterable[tuple[str, int]]
) -> PairedTargets:
    """Build the comparator's and the candidate's target sets together.

    Args:
        candidates: The executable ranked cross-section. One sequence, used by
            both strategies, so their inputs cannot differ.
        incumbents: What H2 held going into this decision. Empty at the first
            decision, which is what makes the two sets equal there.

    Returns:
        Both target sets, the shared target count, and H2's attribution.
    """
    ordered = rank(candidates)
    target_count = ordinary_target_count(len(ordered))
    comparator: list[tuple[str, int]] = []
    for row in ordered:
        if len(comparator) == target_count:
            break
        if row.eligible:
            comparator.append(row.key)
    outcome = select(candidates, incumbents, target_count)
    positions = {row.key: index for index, row in enumerate(ordered)}
    scores = {row.key: row.final_score for row in ordered}
    return PairedTargets(
        {
            "cross_section_size": len(ordered),
            "target_count": target_count,
            "target_count_function": "ordinary_target_count (frozen top-decile rule)",
            "retention_limit": retention_limit(target_count),
            "comparator_target_set": list(comparator),
            "h2_target_set": list(outcome.selected),
            "h2_retained_in_band": list(outcome.retained_in_band),
            "h2_retained_in_decile": list(outcome.retained_in_decile),
            "h2_admitted": list(outcome.admitted),
            "h2_forced_out": list(outcome.forced_out),
            "h2_dropped_out_of_band": list(outcome.dropped_out_of_band),
            "h2_dropped_over_capacity": list(outcome.dropped_over_capacity),
            "h2_displaced": [list(pair) for pair in outcome.displaced],
            "ranks": {f"{kind}:{identity}": index for (kind, identity), index in positions.items()},
            "scores": {f"{kind}:{identity}": value for (kind, identity), value in scores.items()},
            "sets_are_equal": sorted(comparator) == sorted(outcome.selected),
        }
    )
