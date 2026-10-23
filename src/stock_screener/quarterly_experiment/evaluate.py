"""The five frozen falsification criteria, evaluated mechanically.

R1.1 stated each criterion as the thing that would count *against* H1. This
module inverts each one into a predicate over measured quantities and reports
the measurement beside the verdict, so a reader can check the arithmetic rather
than trust it.

**The specification defines no rule for combining them.** That is deliberate
and it is respected here: R1.2 does not invent weights, does not declare one
criterion decisive, and does not construct an aggregate threshold after seeing
the numbers. The only aggregation performed is the unavoidable one — all met,
none met, or a mixture — and it is labelled as a reporting convention rather
than as a preregistered rule.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "OUTCOMES",
    "CriterionResult",
    "evaluate_criteria",
    "interpret",
]

OUTCOMES = (
    "SUPPORTED_BY_R1_2",
    "PARTIALLY_SUPPORTED_BY_R1_2",
    "NOT_SUPPORTED_BY_R1_2",
)


@dataclass(frozen=True)
class CriterionResult:
    """One frozen criterion, its measurement and its verdict.

    Attributes:
        number: Its position in the frozen list, from one.
        frozen_text: The criterion exactly as R1.1 stated it — as a
            falsification condition.
        requirement: The same condition inverted into what must hold for H1 to
            survive it.
        measured: What R1.2 measured.
        threshold: The value the measurement is compared against.
        met: Whether H1 survived this criterion.
        detail: Anything a reader needs to check the arithmetic.
    """

    number: int
    frozen_text: str
    requirement: str
    measured: float | None
    threshold: float | None
    met: bool
    detail: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        """A deterministic mapping."""
        return dict(sorted(asdict(self).items()))


def evaluate_criteria(
    criteria: Sequence[str],
    *,
    contrast_60: float | None,
    decile_association: float | None,
    annual_turnover: float | None,
    monthly_turnover_reference: float,
    block_relative: Mapping[str, float | None],
    top_five_share: float | None,
) -> list[CriterionResult]:
    """Evaluate all five criteria against the measured quantities.

    Args:
        criteria: The frozen criterion texts, in the order R1.1 stated them.
        contrast_60: Mean quarterly `D10 - D1` at the 60-session horizon.
        decile_association: The decile rank association at that horizon.
        annual_turnover: The quarterly strategy's annualised turnover.
        monthly_turnover_reference: The monthly figure the third criterion
            names — 3.90 times a year.
        block_relative: Benchmark-relative total return per chronological block.
        top_five_share: The five largest contributors' share of profit.

    Returns:
        One result per criterion, in the frozen order.

    Raises:
        ValueError: If five criteria were not supplied. The criteria are frozen
            and R1.2 does not proceed on a different number of them.
    """
    if len(criteria) != 5:
        raise ValueError(f"expected five frozen criteria, got {len(criteria)}")

    halved = monthly_turnover_reference / 2.0
    positive_blocks = [
        name for name, value in sorted(block_relative.items()) if value is not None and value > 0
    ]
    return [
        CriterionResult(
            number=1,
            frozen_text=criteria[0],
            requirement="mean quarterly D10 - D1 at 60 sessions is positive over the full window",
            measured=contrast_60,
            threshold=0.0,
            met=bool(contrast_60 is not None and contrast_60 > 0),
            detail={"horizon_sessions": 60, "unit": "fraction of price per observation"},
        ),
        CriterionResult(
            number=2,
            frozen_text=criteria[1],
            requirement="the decile rank association at 60 sessions is positive",
            measured=decile_association,
            threshold=0.0,
            met=bool(decile_association is not None and decile_association > 0),
            detail={
                "definition": "Spearman correlation of decile index against that decile's mean "
                "realised return, the same construction R1.1 used"
            },
        ),
        CriterionResult(
            number=3,
            frozen_text=criteria[2],
            requirement=(
                f"annual turnover falls by at least half against {monthly_turnover_reference:.2f}x,"
                f" that is to {halved:.3f}x or lower"
            ),
            measured=annual_turnover,
            threshold=halved,
            met=bool(annual_turnover is not None and annual_turnover <= halved),
            detail={
                "monthly_reference": monthly_turnover_reference,
                "reduction": (
                    None
                    if annual_turnover is None
                    else monthly_turnover_reference - annual_turnover
                ),
                "reduction_share": (
                    None
                    if annual_turnover is None or monthly_turnover_reference == 0
                    else 1.0 - annual_turnover / monthly_turnover_reference
                ),
            },
        ),
        CriterionResult(
            number=4,
            frozen_text=criteria[3],
            requirement="benchmark-relative total return is positive in at least two of the "
            "three chronological blocks",
            measured=float(len(positive_blocks)),
            threshold=2.0,
            met=len(positive_blocks) >= 2,
            detail={
                "by_block": dict(sorted(block_relative.items())),
                "positive_blocks": positive_blocks,
                "note": "the third block is the interval R1.0 already read; it participates in "
                "this predeclared diagnostic but confirms nothing for H1",
            },
        ),
        CriterionResult(
            number=5,
            frozen_text=criteria[4],
            requirement="the five largest identity contributors are under half of the profit",
            measured=top_five_share,
            threshold=0.50,
            met=bool(top_five_share is not None and top_five_share < 0.50),
            detail={"definition": "share of total profit, identities ranked by contribution"},
        ),
    ]


def interpret(results: Sequence[CriterionResult], aggregate_rule: str | None) -> dict[str, Any]:
    """Turn the five verdicts into one research-state label, without weights.

    Args:
        results: The evaluated criteria.
        aggregate_rule: What the frozen specification says about combining
            them. None means it says nothing, which is the case here.

    Returns:
        The outcome, the per-criterion verdicts and a statement of exactly how
        the label was reached.
    """
    met = [result.number for result in results if result.met]
    failed = [result.number for result in results if not result.met]
    if not failed:
        outcome = "SUPPORTED_BY_R1_2"
    elif not met:
        outcome = "NOT_SUPPORTED_BY_R1_2"
    else:
        outcome = "PARTIALLY_SUPPORTED_BY_R1_2"
    return {
        "outcome": outcome,
        "criteria_met": met,
        "criteria_failed": failed,
        "aggregate_rule_in_frozen_spec": aggregate_rule,
        "how_this_label_was_reached": (
            "the frozen specification states five criteria and no rule for combining them. "
            "Each is reported individually above. The label here is the unavoidable "
            "all/none/mixed summary of those five verdicts — it applies no weights, treats no "
            "criterion as decisive, and was not constructed after seeing the measurements."
        )
        if aggregate_rule is None
        else f"applied the frozen aggregate rule: {aggregate_rule}",
        "language": (
            "research-state only. Not 'proven', not 'validated profitable', not 'production ready'."
        ),
    }
