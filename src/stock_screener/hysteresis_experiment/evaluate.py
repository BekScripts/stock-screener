"""Evaluating H2 against the four criteria R1.4 froze.

The signature is the argument. `evaluate` takes turnover, exposure and safety
and has no parameter a return could arrive in, so the mechanism verdict is
mathematically independent of performance rather than merely intended to be.
Performance is computed afterwards, by a different function, and cannot reach
this one.

The aggregate rule is applied exactly as written: `C1 AND C2 AND C3 AND C4`,
with no weighting, no majority and no qualitative override. H2 has no
`PARTIALLY_SUPPORTED` label and none is invented here — that ambiguity was H1's,
and R1.4 fixed it in advance precisely so it could not recur.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import (
    AGGREGATE_RULE,
    H2_ID,
    HISTORICAL_STATUS,
    LEGACY_H1_TURNOVER_REFERENCE,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["CriterionResult", "evaluate", "interpret"]


class CriterionResult(dict[str, Any]):
    """One criterion's verdict, carrying what it was measured against."""


def _criterion(
    number: int,
    identifier: str,
    frozen_text: str,
    requirement: str,
    threshold: float | int | None,
    measured: float | int | None,
    met: bool,
    detail: Mapping[str, Any] | None = None,
) -> CriterionResult:
    """Build one criterion result."""
    return CriterionResult(
        {
            "number": number,
            "id": identifier,
            "frozen_text": frozen_text,
            "requirement": requirement,
            "threshold": threshold,
            "measured": measured,
            "met": met,
            "detail": dict(detail or {}),
        }
    )


def evaluate(
    *,
    comparator: Mapping[str, Any],
    annual_turnover: float,
    churn_turnover: float,
    target_counts_match: bool,
    target_count_mismatches: Sequence[str],
    intentional_cash_quarters: int,
    exposure: Mapping[str, Any],
    accounting_violations: Sequence[str],
    identity_violations: Sequence[str],
    leakage_violations: Sequence[str],
    execution_violations: Sequence[str],
) -> list[CriterionResult]:
    """Apply the four frozen criteria. Takes no performance measurement.

    Args:
        comparator: The frozen R1.2 figures.
        annual_turnover: H2's annual one-way turnover.
        churn_turnover: H2's annualised entry/exit churn turnover.
        target_counts_match: Whether every decision used R1.2's count.
        target_count_mismatches: Decisions where it did not.
        intentional_cash_quarters: Quarters the strategy deliberately held cash.
        exposure: Gross exposure and cash-utilisation evidence.
        accounting_violations: What the harness recorded.
        identity_violations: Identity-contamination findings.
        leakage_violations: Future-information findings.
        execution_violations: Same-close or unpriced execution findings.

    Returns:
        The four criterion results, in order.
    """
    turnover_threshold = float(comparator["annual_turnover"])
    churn_threshold = float(comparator["ranking_churn_annual_turnover"])
    exposure_clean = (
        target_counts_match
        and intentional_cash_quarters == 0
        and not bool(exposure.get("hysteresis_caused_exposure_reduction"))
    )
    safety_findings = (
        list(accounting_violations)
        + list(identity_violations)
        + list(leakage_violations)
        + list(execution_violations)
    )
    return [
        _criterion(
            1,
            "C1_TURNOVER_DIRECTION",
            "H2's annual one-way turnover is not strictly below the comparator's",
            f"annual turnover < {turnover_threshold!r}x",
            turnover_threshold,
            annual_turnover,
            annual_turnover < turnover_threshold,
            {
                "delta": annual_turnover - turnover_threshold,
                "relative_reduction": (
                    (turnover_threshold - annual_turnover) / turnover_threshold
                    if turnover_threshold
                    else None
                ),
                "definition": (
                    "sum of absolute BUY and SELL notional over twice the pre-trade "
                    "NAV, annualised by nominal quarterly frequency — R1.2's, unchanged"
                ),
            },
        ),
        _criterion(
            2,
            "C2_RANKING_CHURN_DIRECTION",
            "the annualised turnover attributable to entry and exit is not below the comparator's",
            f"annualised entry/exit turnover < {churn_threshold!r}x",
            churn_threshold,
            churn_turnover,
            churn_turnover < churn_threshold,
            {
                "delta": churn_turnover - churn_threshold,
                "compared_as": (
                    "an absolute annualised quantity, not a share of turnover. A "
                    "share can fall while the quantity rises."
                ),
            },
        ),
        _criterion(
            3,
            "C3_EXPOSURE_PRESERVATION",
            "the turnover reduction comes from holding fewer names, deliberate "
            "cash, or a hysteresis-caused exposure reduction",
            "same target-count rule at every decision, 0 intentional cash "
            "quarters, no hysteresis-caused exposure reduction",
            0,
            len(target_count_mismatches) + intentional_cash_quarters,
            exposure_clean,
            {
                "target_counts_match": target_counts_match,
                "target_count_mismatches": list(target_count_mismatches),
                "intentional_cash_quarters": intentional_cash_quarters,
                **dict(exposure),
            },
        ),
        _criterion(
            4,
            "C4_ACCOUNTING_AND_EXECUTION_SAFETY",
            "any accounting, identity, leakage or unauthorised same-close "
            "execution violation occurs",
            "0 accounting, 0 identity, 0 leakage, 0 same-close violations",
            0,
            len(safety_findings),
            not safety_findings,
            {
                "accounting_violations": list(accounting_violations),
                "identity_violations": list(identity_violations),
                "leakage_violations": list(leakage_violations),
                "execution_violations": list(execution_violations),
            },
        ),
    ]


def interpret(criteria: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the frozen aggregate rule, and no other.

    Args:
        criteria: The four criterion results.

    Returns:
        The verdict and how it was reached.

    Raises:
        ValueError: If the criterion set is not the four that were frozen.
    """
    if len(criteria) != 4:
        raise ValueError(f"the frozen rule combines four criteria, got {len(criteria)}")
    met = [bool(row["met"]) for row in criteria]
    supported = all(met)
    return {
        "outcome": "SUPPORTED" if supported else "NOT_SUPPORTED",
        "hypothesis_id": H2_ID,
        "aggregate_rule": dict(AGGREGATE_RULE),
        "criteria_met": [row["number"] for row in criteria if row["met"]],
        "criteria_failed": [row["number"] for row in criteria if not row["met"]],
        "how_this_label_was_reached": (
            "the frozen specification states the rule C1 AND C2 AND C3 AND C4, and "
            "it was applied as written. No weighting, no majority, no qualitative "
            "override, and no partial label — H2 has none and none was invented."
        ),
        "partial_label_used": False,
        "performance_entered_this_verdict": False,
        "why_not": (
            "the evaluation function takes turnover, exposure and safety and has "
            "no parameter a return could arrive in. Performance is computed after "
            "this verdict is fixed and cannot reach it."
        ),
        "legacy_h1_turnover_reference": {
            "value": LEGACY_H1_TURNOVER_REFERENCE,
            "is_an_h2_criterion": False,
            "used_in_this_verdict": False,
        },
        "historical_test_classification": HISTORICAL_STATUS,
        "independent_confirmation": "NOT_PROVIDED_BY_R1_5",
        "language": (
            "research-state only. SUPPORTED means the claimed mechanism behaved as "
            "claimed on already-observed history. It is not confirmation, not "
            "validation, and not a statement of investment merit."
        ),
    }
