"""Classifying the failure, and saying what it does and does not license.

Two temptations govern the design of this module. The first is to call a
mechanism working exactly as specified a bug, because its output failed a test;
the second is to read a failed premise as a design brief and quietly choose the
parameter that would have fixed it. Both are refused structurally here: the
classifier requires positive evidence of ledger inconsistency before it will
return a defect, and the next-step record carries a dimension and no number.

The threshold's provenance is also recorded rather than judged. A bound that was
half of a measured quantity is not wrong for being round, but a reader is
entitled to know that nothing but roundness chose the half.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "H1_STATUSES",
    "MECHANISMS",
    "THRESHOLD_CLASSES",
    "classify",
    "h1_status",
    "next_dimension",
    "threshold_provenance",
]

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The mechanism labels the brief allows.
MECHANISMS: tuple[str, ...] = (
    "M1_IMPLEMENTATION_OR_ACCOUNTING_DEFECT",
    "M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT",
    "M3_UNIVERSE_OR_TRADABILITY_CHURN",
    "M4_EQUAL_WEIGHT_RESIZING",
    "M5_TERMINAL_EVENT_CHURN",
    "M6_MIXED",
    "M7_UNRESOLVED",
)

#: The H1 outcomes the brief allows. None of them is confirmation.
H1_STATUSES: tuple[str, ...] = (
    "H1_MECHANISTICALLY_FALSIFIED",
    "H1_INCONCLUSIVE_MECHANISM",
    "H1_FAILURE_DUE_TO_IMPLEMENTATION_DEFECT",
)

#: How a preregistered threshold came to have the value it has.
THRESHOLD_CLASSES: tuple[str, ...] = (
    "ECONOMICALLY_MOTIVATED",
    "EMPIRICALLY_DERIVED_PRE_HOLDOUT",
    "ARBITRARY_PREREGISTRATION_BOUND",
    "OTHER",
)


def threshold_provenance(hypothesis: Mapping[str, Any]) -> dict[str, Any]:
    """Read where the frozen turnover ceiling came from.

    Args:
        hypothesis: The frozen H1 record.

    Returns:
        The criterion text, the two parts of the bound, and a classification.
    """
    criteria = list(hypothesis["falsification_criteria"]["criteria"])
    text = next((line for line in criteria if "turnover" in line), "")
    anchor = float(hypothesis["premises"]["C_turnover_is_material"]["evidence"]["annual_turnover"])
    return {
        "frozen_text": text,
        "anchor_value": anchor,
        "anchor_source": (
            "premise C of the H1 record, which is R1.0's full-window monthly "
            "annual turnover for COMPOUNDER_TOP_DECILE_V1"
        ),
        "anchor_class": "EMPIRICALLY_DERIVED",
        "anchor_measured_after_holdout_was_read": True,
        "reduction_factor": 0.5,
        "reduction_factor_class": "ARBITRARY_PREREGISTRATION_BOUND",
        "reduction_factor_justification_in_record": None,
        "derived_threshold": 1.95,
        "classification": "OTHER",
        "why_other": (
            "the bound is a product of two parts of different kinds. Its anchor, "
            "3.90x, is an empirical measurement — but one taken after R1.0 had "
            "already read its holdout, so it is not pre-holdout. Its reduction "
            "factor, one half, has no derivation anywhere in the record: no cost "
            "model, no break-even calculation and no stability argument connects "
            "it to the 77.9 bps per side premise D measured. Neither named class "
            "fits the whole, so both parts are reported separately."
        ),
        "not_altered": True,
    }


def classify(
    *,
    decomposition: Mapping[str, Any],
    counterfactual: Mapping[str, Any],
    lower_bound: Mapping[str, Any],
    transitions: Mapping[str, Any],
    quarterly_retention: float,
    monthly_three_step_survival: float,
    reconciliation_residual: float,
    accounting_violations: int,
    metric_reproduced_exactly: bool,
    reporting_defects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assign a primary cause to the failed turnover premise.

    A defect is returned only on positive evidence: a turnover figure that does
    not reproduce, a decomposition that does not close, or a ledger that breaks
    its own accounting. Absent that, trading that follows correctly from changed
    target membership is the strategy's mechanism, whatever the verdict on it.

    Args:
        decomposition: Traded notional by source.
        counterfactual: The constant-per-event arithmetic.
        lower_bound: The membership-change floor.
        transitions: Membership changes by cause.
        quarterly_retention: Mean quarter-to-quarter retention.
        monthly_three_step_survival: Mean monthly survival three months out.
        reconciliation_residual: Residual on the traded-notional reconciliation.
        accounting_violations: Violations the harness recorded.
        metric_reproduced_exactly: Whether the independent path matched R1.2.
        reporting_defects: Any metadata defects found, with their impact.

    Returns:
        The primary and secondary mechanisms with the evidence behind each.
    """
    defect = (
        not metric_reproduced_exactly
        or accounting_violations > 0
        or abs(reconciliation_residual) > 1e-6
    )
    shares = decomposition["share_of_all_traded_notional"]
    exit_shares = transitions.get("exit_shares", {})
    ranking_exits = float(exit_shares.get("RANKING", 0.0))

    if defect:
        primary = "M1_IMPLEMENTATION_OR_ACCOUNTING_DEFECT"
    else:
        primary = "M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT"

    secondary: list[dict[str, Any]] = []
    if float(shares["resize"] or 0.0) > 0.02:
        secondary.append(
            {
                "mechanism": "M4_EQUAL_WEIGHT_RESIZING",
                "share_of_traded_notional": shares["resize"],
                "material_to_the_verdict": bool(
                    lower_bound.get("could_same_selections_satisfy_threshold", False)
                ),
                "note": (
                    "removing every resizing trade still leaves annual turnover "
                    f"at {lower_bound['membership_change_floor_annual']:.4f}x, above "
                    f"{lower_bound['frozen_threshold']}x, so this component cannot "
                    "account for the failure"
                ),
            }
        )
    if ranking_exits < 1.0:
        secondary.append(
            {
                "mechanism": "M3_UNIVERSE_OR_TRADABILITY_CHURN",
                "share_of_exits_not_caused_by_ranking": 1.0 - ranking_exits,
                "note": (
                    "a minority of membership changes are availability changes "
                    "rather than score decisions; they are counted, not attributed "
                    "to the ranking"
                ),
            }
        )
    if float(shares["terminal"] or 0.0) > 0.0:
        secondary.append(
            {
                "mechanism": "M5_TERMINAL_EVENT_CHURN",
                "share_of_traded_notional": shares["terminal"],
                "note": "too small to bear on the verdict",
            }
        )

    return {
        "primary": primary,
        "secondary": secondary,
        "implementation_defect": defect,
        "accounting_defect": accounting_violations > 0,
        "reporting_defects": list(reporting_defects),
        "evidence": {
            "metric_reproduced_exactly": metric_reproduced_exactly,
            "decomposition_closes_to": decomposition["shares_sum"],
            "unexplained_notional": decomposition["unexplained_notional"],
            "accounting_violations": accounting_violations,
            "reconciliation_residual": reconciliation_residual,
            "per_rebalance_turnover_ratio_quarterly_over_monthly": counterfactual[
                "per_rebalance_ratio"
            ],
            "frequency_benefit_offset_share": counterfactual["frequency_benefit_offset_share"],
            "quarterly_retention": quarterly_retention,
            "monthly_survival_three_steps": monthly_three_step_survival,
            "retention_gap": quarterly_retention - monthly_three_step_survival,
            "membership_floor_annual_turnover": lower_bound["membership_change_floor_annual"],
            "floor_above_frozen_threshold": lower_bound["floor_above_threshold"],
        },
        "why_not_a_defect": (
            "the turnover figure reproduces bit for bit from an independent path, "
            "the decomposition closes, and no accounting invariant is broken. "
            "Trading that follows correctly from a changed target set is the "
            "strategy's mechanism, not a defect in the harness."
        )
        if not defect
        else None,
        "statement": (
            "A quarter of the rebalances is not a quarter of the trading. Per-"
            "rebalance turnover rose by a factor of "
            f"{counterfactual['per_rebalance_ratio']:.3f} because the ranking drifts "
            "further over three months than over one, and that gave back "
            f"{counterfactual['frequency_benefit_offset_share']:.1%} of the reduction "
            "frequency alone would have delivered. The drift is not new: quarterly "
            f"retention of {quarterly_retention:.1%} is what the monthly ledger's own "
            f"three-month survival of {monthly_three_step_survival:.1%} already showed, "
            "and the H1 record recorded that number as premise B before the "
            "experiment ran."
        ),
    }


def h1_status(mechanism: Mapping[str, Any], lower_bound: Mapping[str, Any]) -> dict[str, Any]:
    """Return H1's final status, which is never confirmation.

    Args:
        mechanism: The output of `classify`.
        lower_bound: The membership-change floor.

    Returns:
        The status and the reasoning behind it.
    """
    if mechanism["implementation_defect"]:
        status = "H1_FAILURE_DUE_TO_IMPLEMENTATION_DEFECT"
        reason = "a defect in the harness or its accounting produced the figure"
    elif mechanism["primary"] == "M7_UNRESOLVED":
        status = "H1_INCONCLUSIVE_MECHANISM"
        reason = "the turnover figure is valid but its sources do not decompose"
    elif lower_bound["floor_above_threshold"]:
        status = "H1_MECHANISTICALLY_FALSIFIED"
        reason = (
            "the least trading the executed quarterly selections could possibly "
            f"require is {lower_bound['membership_change_floor_annual']:.4f}x a year, "
            f"above the frozen ceiling of {lower_bound['frozen_threshold']}x. The "
            "criterion was therefore unreachable by changing frequency alone, "
            "whatever the implementation"
        )
    else:
        status = "H1_INCONCLUSIVE_MECHANISM"
        reason = (
            "the floor sits below the ceiling, so implementation choices rather "
            "than membership drift may account for the miss"
        )
    return {
        "status": status,
        "reason": reason,
        "confirmed": False,
        "what_is_closed": (
            "the question H1 asked — whether reducing rebalance frequency alone "
            "would reduce annual turnover by half. It would not."
        ),
        "what_is_not_closed": (
            "whether COMPOUNDER_V1_2 selects well. C1, C2, C4 and C5 were met and "
            "are untouched by this finding, and the validation block's 7.3 "
            "percentage point shortfall against SPY is untouched by it too."
        ),
    }


def next_dimension(mechanism: Mapping[str, Any]) -> dict[str, Any]:
    """Name the dimension a later hypothesis should address, and no parameter.

    Args:
        mechanism: The output of `classify`.

    Returns:
        The recommended dimension, with the numeric choices explicitly withheld.
    """
    return {
        "recommended_dimension": "MEMBERSHIP_STABILITY_OF_THE_SELECTED_SET",
        "why": (
            "frequency was the wrong lever because it does not touch the quantity "
            "that drives trading. Per-rebalance turnover tracks one minus "
            "retention, and retention is a property of how the selection rule "
            "responds to a drifting ranking, not of how often it is applied."
        ),
        "a_later_hypothesis_could_ask": (
            "whether a preregistered membership-stability rule reduces annual "
            "turnover below the previously failed threshold without materially "
            "degrading the frozen Compounder signal."
        ),
        "numeric_rule_chosen_here": None,
        "parameters_selected_from_these_data": 0,
        "withheld_deliberately": [
            "any hysteresis or buffer width",
            "any minimum holding period",
            "any alternative selected fraction or fixed position count",
            "any rebalance frequency",
            "any turnover penalty or score smoothing",
        ],
        "why_withheld": (
            "these data are the outcome the rule would be judged against. "
            "Choosing the width here would fit the parameter to the result and "
            "leave nothing left to test. A later phase must state its rule before "
            "it is evaluated."
        ),
        "not_permitted_next": [
            "testing another cadence as a response to this finding",
            "treating the four met criteria as H1 confirmation",
            "re-reading the R1.2 holdout as untouched evidence for a new rule",
        ],
    }
