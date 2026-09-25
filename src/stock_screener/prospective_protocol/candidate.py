"""The frozen H2 candidate, and the reason it is frozen.

The reason matters enough to be structural. H2 is frozen because the
preregistered *mechanism* passed — turnover fell, it fell by cutting entry and
exit churn, and it did so without shrinking the portfolio or breaking an
invariant. It is not frozen because it returned more than the comparator. It
returned 1.35 percentage points more over seventeen rebalances on one realised
path, which is noise, and the candidate record says so in the same breath as it
records the number.

Everything historical is closed here. The record carries the measurements as
references, names the modifications that are prohibited under this candidate id,
and states that no further historical variant can supply what the candidate
lacks. What it lacks is evidence from data that did not help produce it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import (
    H2_ID,
    RULE,
    RULE_VERSION,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CANDIDATE_ID",
    "CANDIDATE_STATE",
    "HISTORICAL_CLASSIFICATION",
    "PROHIBITED_MODIFICATIONS",
    "PROMOTION_LADDER",
    "candidate_freeze",
]

#: The durable identity of what is being carried forward.
CANDIDATE_ID = "H2_MEMBERSHIP_STABILITY_CANDIDATE_V1"

#: Every state word that applies, so no one of them can be read alone.
CANDIDATE_STATE: tuple[str, ...] = (
    "FROZEN_CANDIDATE",
    "PROSPECTIVE_CONFIRMATION_PENDING",
    "NOT_PRODUCTION",
    "NO_LIVE_CAPITAL",
)

#: What the historical evidence is, stated once and not softened elsewhere.
HISTORICAL_CLASSIFICATION = "EXPLORATORY_SUPPORT_ONLY"

#: Changes that cannot be made under this candidate id. Each needs a new one.
PROHIBITED_MODIFICATIONS: tuple[str, ...] = (
    "any retention band other than two selection widths",
    "any entry threshold other than the ordinary top-decile target count",
    "any rebalance frequency other than calendar quarter ends",
    "any weighting other than equal",
    "any execution rule other than the next session's open",
    "any target-count rule other than the frozen one",
    "a minimum holding period, cooldown or trade-size threshold",
    "a turnover penalty or score smoothing",
    "a fixed position count or position-weight drift band",
    "a sector, industry or other exposure constraint",
    "a market-regime filter or any MARKET_SIGNALS input",
    "any score version other than COMPOUNDER_V1_2",
)

#: The only ladder, and R1.6 authorises only its first step.
PROMOTION_LADDER: tuple[dict[str, Any], ...] = (
    {"stage": "FROZEN_H2_CANDIDATE", "status": "CURRENT", "authorised_by": "R1.6"},
    {
        "stage": "PROSPECTIVE_SHADOW",
        "status": "NEXT — preparation authorised by R1.6, activation by R1.7",
        "means": "internally simulated reference execution; no broker, no orders",
    },
    {
        "stage": "BROKER_PAPER",
        "status": "NOT AUTHORISED — requires its own approved phase",
        "means": "orders placed into a broker paper account",
    },
    {
        "stage": "SUPERVISED_SMALL_LIVE",
        "status": "NOT AUTHORISED",
        "means": "real capital, supervised, size-limited",
    },
    {
        "stage": "CONSTRAINED_AUTONOMOUS",
        "status": "NOT AUTHORISED",
        "means": "real capital, autonomous within constraints",
    },
)


def candidate_freeze(
    *, binding: Mapping[str, Any], protocol_id: str, code_commit: str
) -> dict[str, Any]:
    """Build the candidate freeze record.

    Args:
        binding: The verified historical binding, as JSON.
        protocol_id: The prospective protocol this candidate awaits.
        code_commit: The commit the freeze was written at.

    Returns:
        The record, as a JSON-safe mapping.
    """
    measurements = dict(binding["historical_measurements"])
    return {
        "candidate_id": CANDIDATE_ID,
        "hypothesis_id": H2_ID,
        "rule_version": RULE_VERSION,
        "rule": dict(RULE),
        "state": list(CANDIDATE_STATE),
        "frozen_at_code_commit": code_commit,
        "why_this_is_frozen": {
            "reason": (
                "the preregistered mechanism passed. R1.5 found C1, C2, C3 and C4 "
                "all met under the aggregate rule fixed before execution: turnover "
                "fell, it fell by cutting entry and exit churn, and it did so while "
                "holding the comparator's target count with no intentional cash and "
                "no invariant broken."
            ),
            "not_the_reason": (
                "historical performance. H2 returned 1.35 percentage points more "
                "than the comparator over seventeen rebalances on one realised "
                "path, with a marginally lower Sharpe. That difference is noise, it "
                "took no part in the verdict, and it is not why anything is frozen."
            ),
            "performance_entered_the_verdict": False,
        },
        "bound_to": {
            "h2_record_sha256": binding["h2_record_sha256"],
            "r1_5_experiment_spec_sha256": binding["r1_5_experiment_spec_sha256"],
            "r1_5_evaluation_sha256": binding["r1_5_evaluation_sha256"],
            "r1_5_completion_marker_sha256": binding["r1_5_completion_marker_sha256"],
            "contract_version": "BACKTEST_DATA_CONTRACT_V1",
            "contract_sha256": binding["contract_sha256"],
            "score_version": "COMPOUNDER_V1_2",
            "execution_rule": "CLOSE_SIGNAL_NEXT_SESSION_OPEN",
            "terminal_policy": "TERMINAL_LEG_POLICY_V1",
            "cost_convention": "SIMPLE_10BPS_PER_SIDE_V1",
            "comparator": "COMPOUNDER_QUARTERLY_TOP_DECILE_V1",
        },
        "historical_result": {
            "phase": "R1.5",
            "classification": HISTORICAL_CLASSIFICATION,
            "outcome": binding["r1_5_outcome"],
            "criteria_met": measurements["criteria_met"],
            "criteria_failed": measurements["criteria_failed"],
            "aggregate_rule": "C1 AND C2 AND C3 AND C4",
            "closed": True,
        },
        "historical_references": {
            "note": (
                "carried as context. None of these is a prospective threshold, and "
                "the prospective criteria are directional rather than tied to any "
                "of these magnitudes."
            ),
            "h2_annual_turnover": measurements["h2_annual_turnover"],
            "comparator_annual_turnover": measurements["comparator_annual_turnover"],
            "h2_churn_annual_turnover": measurements["h2_churn_annual_turnover"],
            "comparator_churn_annual_turnover": measurements["comparator_churn_annual_turnover"],
            "turnover_reduction_fraction": measurements["turnover_reduction_fraction"],
            "h2_retention": measurements["h2_retention"],
            "comparator_retention": measurements["comparator_retention"],
            "legacy_h1_turnover_reference": 1.95,
            "legacy_reference_is_a_criterion": False,
        },
        "historical_performance_references": {
            "status": "PROSPECTIVE_PERFORMANCE_OBSERVATION is a separate thing; this "
            "is closed historical context and determined nothing",
            "h2_total_return": measurements["h2_total_return"],
            "comparator_total_return": measurements["comparator_total_return"],
            "h2_sharpe": measurements["h2_sharpe"],
            "comparator_sharpe": measurements["comparator_sharpe"],
            "is_a_criterion": False,
            "validated": False,
        },
        "historical_development": {
            "parameter_search": "CLOSED",
            "additional_retention_widths": "PROHIBITED",
            "additional_frequencies": "PROHIBITED",
            "additional_weighting_rules": "PROHIBITED",
            "additional_holding_period_rules": "PROHIBITED",
            "historical_clean_holdout": "NONE",
            "why": (
                "every month of the frozen dataset has been exposed to the research "
                "process, and R1.3 derived this hypothesis from all of it. A further "
                "historical variant could not supply what the candidate lacks, and "
                "would need a new hypothesis id which would be contaminated for the "
                "same reason."
            ),
            "a_new_variant_would_require": [
                "a new hypothesis identifier",
                "its own preregistration",
                "and it would still carry EXPLORATORY_SUPPORT_ONLY on this dataset",
            ],
        },
        "prospective_status": {
            "independent_confirmation": "PENDING",
            "awaiting_protocol": protocol_id,
            "observations_collected": 0,
            "completed_transitions": 0,
            "verdict": "PENDING",
        },
        "prohibited_modifications": list(PROHIBITED_MODIFICATIONS),
        "promotion_ladder": [dict(step) for step in PROMOTION_LADDER],
        "authorisations": {
            "shadow_research_observation": "PREPARATION AUTHORISED BY R1.6",
            "broker_connectivity": "NOT AUTHORISED",
            "paper_orders": "NOT AUTHORISED",
            "live_orders": "NOT AUTHORISED",
            "real_capital": "NOT AUTHORISED",
        },
        "claims_confirmation": False,
        "language": (
            "research-state only. Nothing here is confirmed, validated, production "
            "ready or authorised for capital."
        ),
    }
