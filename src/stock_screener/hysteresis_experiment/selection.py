"""Building H2's portfolios from the comparator's own frozen cross-sections.

Nothing here constructs a universe, a score or a ranking. The executable rows,
the scores and the target counts are R1.2's, read from its frozen ledgers, so
the only thing that can differ between the two strategies is which of those rows
each one selects. That is what makes the comparison mean anything.

Two guards live in this module rather than in a test. The target-count sequence
is asserted against R1.2's selection ledger at every decision, because a
silently different decile count would make C1 a comparison between two different
portfolios rather than two selection rules. And the first decision is asserted
to reproduce R1.2's selection exactly, which it must: with no incumbents the
hysteresis rule reduces to the ordinary one, so a difference there would mean
the ranking itself had drifted.

No forward return, price or label is read here. Selection sees incumbency,
eligibility, score and the frozen target count, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import Candidate, rank, retention_limit, select
from stock_screener.turnover_forensics.membership import cause_of

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.backtest_harness.crosssection import CrossSectionRow
    from stock_screener.membership_hypothesis.rule import Selection

__all__ = [
    "Decision",
    "SelectionError",
    "build_decisions",
    "ordinary_top_decile",
]


class SelectionError(RuntimeError):
    """Raised when the experiment would not be comparing like with like."""


@dataclass
class Decision:
    """One quarter-end decision, with everything the rule did to it.

    Attributes:
        decision: The signal date.
        execution: The session the orders execute at the open.
        month: The month label the harness uses.
        quarter: The calendar quarter.
        block: The legacy research block, descriptive only.
        target_count: The frozen R1.2 top-decile count.
        selected: What the hysteresis rule chose, best-ranked first.
        ordinary: What the ordinary rule would have chosen.
        outcome: The rule's own attribution of each name.
        forced_reasons: `{identity: cause}` for incumbents the contract removed.
        displacement: One row per retained incumbent and the name it kept out.
    """

    decision: str
    execution: str
    month: str
    quarter: str
    block: str
    target_count: int
    selected: tuple[tuple[str, int], ...]
    ordinary: tuple[tuple[str, int], ...]
    outcome: Selection
    forced_reasons: dict[tuple[str, int], str] = field(default_factory=dict)
    displacement: list[dict[str, Any]] = field(default_factory=list)

    @property
    def jaccard(self) -> float:
        """Overlap between the two target sets."""
        chosen, ordinary = set(self.selected), set(self.ordinary)
        union = chosen | ordinary
        return len(chosen & ordinary) / len(union) if union else 1.0

    def as_json(self) -> dict[str, Any]:
        """Return the decision as a JSON-safe mapping."""
        chosen, ordinary = set(self.selected), set(self.ordinary)
        return {
            "decision": self.decision,
            "execution": self.execution,
            "month": self.month,
            "quarter": self.quarter,
            "legacy_block": self.block,
            "target_count": self.target_count,
            "selected": len(self.selected),
            "ordinary_top_decile": len(self.ordinary),
            "common_identities": len(chosen & ordinary),
            "band_retained_not_in_ordinary": len(self.outcome.retained_in_band),
            "ordinary_displaced": len(ordinary - chosen),
            "jaccard": self.jaccard,
            "retained_in_decile": len(self.outcome.retained_in_decile),
            "admitted": len(self.outcome.admitted),
            "forced_out": len(self.outcome.forced_out),
            "dropped_out_of_band": len(self.outcome.dropped_out_of_band),
            "dropped_over_capacity": len(self.outcome.dropped_over_capacity),
            "forced_out_by_cause": _tally(self.forced_reasons.values()),
        }


def _tally(values: Any) -> dict[str, int]:
    """Count occurrences, sorted by name."""
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


def ordinary_top_decile(
    pool: Sequence[CrossSectionRow], target_count: int
) -> tuple[tuple[str, int], ...]:
    """Return the selection the ordinary rule would make.

    Args:
        pool: The executable cross-section at one decision.
        target_count: The frozen R1.2 count.

    Returns:
        The best-ranked `target_count` identities under the frozen ordering.
    """
    ordered = rank(
        Candidate(row.kind, row.identity_id, float(row.final_score or 0.0)) for row in pool
    )
    return tuple(row.key for row in ordered[:target_count])


def build_decisions(
    calendar: Sequence[Mapping[str, Any]],
    executable: Mapping[str, Sequence[CrossSectionRow]],
    universe: Mapping[str, Mapping[tuple[str, int], Mapping[str, Any]]],
    target_counts: Mapping[str, int],
    comparator_first: Sequence[tuple[str, int]],
) -> list[Decision]:
    """Apply the frozen rule across the frozen calendar.

    Args:
        calendar: R1.2's quarterly decision calendar, in order.
        executable: `{decision: executable rows}` from R1.2's cross-sections.
        universe: `{decision: {identity: row}}` over the whole cross-section, for
            attributing a forced exit to its cause.
        target_counts: `{decision: count}` from R1.2's selection ledger.
        comparator_first: R1.2's selection at the first decision.

    Returns:
        One decision per quarter end, in order.

    Raises:
        SelectionError: If a target count is missing, or the first selection does
            not reproduce the comparator's.
    """
    decisions: list[Decision] = []
    held: tuple[tuple[str, int], ...] = ()
    for entry in calendar:
        date = str(entry["decision"])
        pool = list(executable.get(date, []))
        if date not in target_counts:
            raise SelectionError(f"no frozen target count for {date}")
        target = int(target_counts[date])
        candidates = [
            Candidate(row.kind, row.identity_id, float(row.final_score or 0.0)) for row in pool
        ]
        outcome = select(candidates, held, target)
        ordinary = ordinary_top_decile(pool, target)

        rows = universe.get(date, {})
        forced = {key: cause_of(rows.get(key)) for key in outcome.forced_out}
        scores = {row.key: float(row.final_score or 0.0) for row in pool}
        symbols = {row.key: row.symbol for row in pool}
        positions = {
            row.key: index
            for index, row in enumerate(
                rank(
                    Candidate(row.kind, row.identity_id, float(row.final_score or 0.0))
                    for row in pool
                )
            )
        }
        limit = retention_limit(target)
        displacement = [
            {
                "decision": date,
                "execution": str(entry["execution"]),
                "retained_kind": retained[0],
                "retained_identity_id": retained[1],
                "retained_symbol": symbols.get(retained, ""),
                "retained_rank": positions.get(retained),
                "retained_rank_percentile": (
                    positions[retained] / len(pool) if pool and retained in positions else None
                ),
                "retained_score": scores.get(retained),
                "retained_in_ordinary_top_decile": retained in set(ordinary),
                "displaced_kind": displaced[0],
                "displaced_identity_id": displaced[1],
                "displaced_symbol": symbols.get(displaced, ""),
                "displaced_rank": positions.get(displaced),
                "displaced_rank_percentile": (
                    positions[displaced] / len(pool) if pool and displaced in positions else None
                ),
                "displaced_score": scores.get(displaced),
                "rank_difference": (
                    positions[retained] - positions[displaced]
                    if retained in positions and displaced in positions
                    else None
                ),
                "score_difference": (
                    scores[displaced] - scores[retained]
                    if retained in scores and displaced in scores
                    else None
                ),
                "retention_limit": limit,
                "target_count": target,
                "paired_on": "contemporaneous rank only; no forward return was read",
            }
            for retained, displaced in outcome.displaced
        ]

        decisions.append(
            Decision(
                decision=date,
                execution=str(entry["execution"]),
                month=str(entry["month"]),
                quarter=str(entry["quarter"]),
                block=str(entry["split"]),
                target_count=target,
                selected=outcome.selected,
                ordinary=ordinary,
                outcome=outcome,
                forced_reasons=forced,
                displacement=displacement,
            )
        )
        held = outcome.selected

    if decisions and set(decisions[0].selected) != set(comparator_first):
        raise SelectionError(
            "the first selection does not reproduce the comparator's: with no "
            "incumbents the rule must reduce to the ordinary one"
        )
    return decisions
