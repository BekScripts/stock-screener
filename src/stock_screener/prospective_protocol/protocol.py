"""`H2_PROSPECTIVE_SHADOW_PROTOCOL_V1` — the experiment that has not started.

The protocol's whole job is to be uncomfortable later. It fixes the window
before anyone knows what is in it, fixes the criteria before the first
observation, and fixes what may and may not be said at the end.

Three choices carry most of the weight.

The criteria are **paired and directional**. P1 and P2 compare H2 against the
comparator run on the same prospective cross-sections, not against the
historical 1.6177x. Requiring the historical magnitude would fit the prospective
test to a number that came out of the data that generated the hypothesis, which
is the error the whole prospective exercise exists to avoid.

The window is **fixed at four transitions** and cannot move in either direction.
Not stopped early on a good run, not extended on a bad one, not chosen as the
best four afterwards.

And the first eligible decision is **a rule, not a date**. Nothing here computes
a calendar. A quarter counts only if the runner was armed before the close of
the session preceding it; a quarter that was missed is skipped rather than
reconstructed, because a reconstruction is a backtest wearing a prospective
label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import RULE, RULE_VERSION

from .candidate import CANDIDATE_ID
from .contract import PROSPECTIVE_CONTRACT_ID

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "AGGREGATE_RULE",
    "COMPARATOR",
    "CRITERIA",
    "PROTOCOL_ID",
    "REQUIRED_DECISIONS",
    "REQUIRED_TRANSITIONS",
    "STREAM_ID",
    "evaluate_prospective",
    "interpret_prospective",
    "prospective_protocol",
]

#: The protocol's identity.
PROTOCOL_ID = "H2_PROSPECTIVE_SHADOW_PROTOCOL_V1"

#: Where prospective observations will live. Never the historical dataset.
STREAM_ID = "PROSPECTIVE_SHADOW_STREAM_V1"

#: The paired comparator, unchanged.
COMPARATOR = "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"

#: One formation plus four transitions. Fixed in both directions.
REQUIRED_DECISIONS = 5
REQUIRED_TRANSITIONS = 4

#: The four criteria, by identifier.
CRITERIA: tuple[str, ...] = (
    "P1_TURNOVER_DIRECTION",
    "P2_CHURN_DIRECTION",
    "P3_EXPOSURE_PRESERVATION",
    "P4_SAFETY_AND_PROTOCOL_INTEGRITY",
)

#: How they combine, fixed before the first observation.
AGGREGATE_RULE: dict[str, Any] = {
    "expression": "P1 AND P2 AND P3 AND P4",
    "confirmed_label": "PROSPECTIVE_MECHANISM_CONFIRMED",
    "not_confirmed_label": "PROSPECTIVE_MECHANISM_NOT_CONFIRMED",
    "pending_label": "PENDING",
    "partial_labels_permitted": False,
    "majority_vote": False,
    "weighting": False,
    "qualitative_override": False,
    "stated_before_any_observation": True,
}


def _criteria() -> list[dict[str, Any]]:
    """The four prospective criteria, stated in full."""
    return [
        {
            "number": 1,
            "id": "P1_TURNOVER_DIRECTION",
            "requirement": (
                "H2's cumulative annualised one-way turnover is strictly below the "
                "paired comparator's, measured over the same four transitions"
            ),
            "comparison": "PAIRED_AND_DIRECTIONAL",
            "threshold": "the comparator's own prospective figure, not a fixed number",
            "definition": (
                "sum of absolute BUY and SELL notional over twice the pre-trade NAV "
                "at the rebalance open, annualised by nominal quarterly frequency — "
                "the established definition, unchanged"
            ),
            "historical_magnitude_required": False,
        },
        {
            "number": 2,
            "id": "P2_CHURN_DIRECTION",
            "requirement": (
                "H2's cumulative annualised entry/exit churn turnover is strictly "
                "below the paired comparator's over the same four transitions"
            ),
            "comparison": "PAIRED_AND_DIRECTIONAL",
            "threshold": "the comparator's own prospective figure",
            "measured_as": (
                "an absolute annualised quantity, never a share of turnover — a "
                "share can fall while the quantity rises"
            ),
            "historical_magnitude_required": False,
        },
        {
            "number": 3,
            "id": "P3_EXPOSURE_PRESERVATION",
            "requirement": (
                "across all five decisions: the same target-count rule as the "
                "comparator, zero intentional cash caused by hysteresis, and no "
                "exposure reduction caused by hysteresis"
            ),
            "comparison": "ABSOLUTE",
            "threshold": 0,
            "why": ("so a turnover reduction cannot be bought by holding fewer names or more cash"),
            "residual_cash_allowed": (
                "yes — residual cash from the frozen fractional-share and execution "
                "policy is expected and is not a deliberate cash position"
            ),
        },
        {
            "number": 4,
            "id": "P4_SAFETY_AND_PROTOCOL_INTEGRITY",
            "requirement": (
                "zero accounting violations, zero identity violations, zero "
                "decision-time leakage, zero same-close executions, and zero "
                "retrospective decision replacements"
            ),
            "comparison": "ABSOLUTE",
            "threshold": 0,
            "note": (
                "the last clause makes protocol integrity a pass condition: a "
                "rewritten decision packet fails the test rather than being quietly "
                "absorbed by it"
            ),
        },
    ]


def prospective_protocol(*, code_commit: str) -> dict[str, Any]:
    """Build the frozen prospective protocol.

    Args:
        code_commit: The commit the protocol was frozen at.

    Returns:
        The protocol, as a JSON-safe mapping.
    """
    return {
        "protocol_id": PROTOCOL_ID,
        "status": "FROZEN — defined, not active; no observation has been taken",
        "frozen_at_code_commit": code_commit,
        "candidate_id": CANDIDATE_ID,
        "rule_version": RULE_VERSION,
        "rule": dict(RULE),
        "comparator": COMPARATOR,
        "data_stream": STREAM_ID,
        "data_contract": PROSPECTIVE_CONTRACT_ID,
        "purpose": (
            "test whether the frozen H2 membership-stability mechanism continues to "
            "reduce turnover and entry/exit churn relative to the frozen quarterly "
            "top-decile comparator, on observations occurring strictly after this "
            "protocol was frozen and activated."
        ),
        "what_kind_of_study": {
            "is": "a MECHANISM confirmation study",
            "is_not": "an alpha confirmation study",
            "why": (
                "four transitions cannot establish a return effect. They can show "
                "whether the mechanism that was claimed continues to behave as "
                "claimed on data that did not help produce the claim."
            ),
        },
        "activation": {
            "boundary_field": "PROSPECTIVE_PROTOCOL_ACTIVE_AT",
            "assigned_by": "R1.7, when the runner is built and armed",
            "assigned_here": None,
            "rule": (
                "no observation whose decision instant precedes the activation "
                "boundary may take part in the criteria"
            ),
        },
        "first_eligible_decision": {
            "assigned": False,
            "value": "NOT_YET_ASSIGNED",
            "rule": (
                "the first quarter-end benchmark session whose decision instant "
                "falls strictly after PROSPECTIVE_PROTOCOL_ACTIVE_AT, and for which "
                "the runner was armed no later than the close of the immediately "
                "preceding benchmark session"
            ),
            "derived_from": "the frozen benchmark-session calendar logic, at activation time",
            "hardcoded_date": None,
            "if_the_arming_requirement_is_missed": (
                "skip that quarter. It never enters the prospective sequence, and "
                "the next eligible quarter becomes the first observation."
            ),
            "why_not_a_date": (
                "a date written now would be a guess about deployment timing, and a "
                "protocol whose validity depends on when someone happened to deploy "
                "is not a protocol."
            ),
        },
        "retrospective_reconstruction": {
            "counts_toward_confirmation": False,
            "permitted_as": "a separate diagnostic",
            "mandatory_labels": ["RETROSPECTIVE", "NOT_PROSPECTIVE_CONFIRMATION"],
            "rule": (
                "a quarter the runner did not capture live may be reconstructed for "
                "diagnostics and must carry both labels. It never enters the "
                "criteria. 'The runner was not active, but we can reconstruct what "
                "it would have selected' is a backtest, whatever it is called."
            ),
        },
        "window": {
            "required_decisions": REQUIRED_DECISIONS,
            "required_transitions": REQUIRED_TRANSITIONS,
            "composition": "one initial portfolio formation plus four completed "
            "quarter-to-quarter transitions",
            "evaluation_uses": "the first four valid transitions after formation",
            "stop_early_on_success": False,
            "extend_on_unfavourable_result": False,
            "choose_best_four_afterwards": False,
            "drop_an_unfavourable_transition": False,
            "replace_an_outage_transition_retrospectively": False,
            "note": (
                "a decision taken while the runner was not properly armed never "
                "entered the sequence, so there is nothing to drop or replace."
            ),
        },
        "initial_formation": {
            "rule": (
                "at the first eligible decision there are no incumbents, so the "
                "hysteresis rule reduces to the ordinary one"
            ),
            "requirement": "H2's initial target set equals the comparator's exactly",
            "a_difference_is": "A PROTOCOL DEFECT, not a finding",
        },
        "paired_design": {
            "mandatory": True,
            "shared": [
                "universe",
                "features",
                "scores",
                "rank order",
                "tradability state",
                "execution-price source",
                "corporate-action state",
            ],
            "differs": "membership persistence, and nothing else",
        },
        "execution": {
            "rule": "CLOSE_SIGNAL_NEXT_SESSION_OPEN",
            "mode": "SHADOW",
            "means": "internally simulated reference execution",
            "broker_connectivity": "NOT AUTHORISED",
            "paper_orders": "NOT AUTHORISED",
            "live_orders": "NOT AUTHORISED",
            "terminology": {
                "shadow": "internally simulated reference execution — this protocol",
                "paper": "orders into a broker paper account — a separate phase",
                "live": "real capital — a separate phase",
            },
        },
        "criteria": _criteria(),
        "aggregate_rule": dict(AGGREGATE_RULE),
        "historical_values_are_references_only": {
            "h2_annual_turnover": 1.6177403954997098,
            "comparator_annual_turnover": 2.253982668222645,
            "h2_churn_annual_turnover": 1.411598539883737,
            "comparator_churn_annual_turnover": 2.11411510465003,
            "h2_retention": 0.643969,
            "comparator_retention": 0.457707,
            "turnover_reduction_fraction": 0.2823,
            "legacy_h1_turnover_reference": 1.95,
            "any_of_these_is_a_prospective_threshold": False,
            "why": (
                "requiring the prospective test to reproduce a historical magnitude "
                "would fit it to an effect size that came out of the data which "
                "generated the hypothesis. The claim is directional: less total "
                "turnover, less membership churn, same exposure, safe execution."
            ),
        },
        "diagnostics": {
            "status": "DESCRIPTIVE — none of these decides the prospective verdict",
            "membership": [
                "quarter-to-quarter retention, both strategies",
                "entrants",
                "exits",
                "re-entry identities and events",
                "holding duration",
            ],
            "retention_has_a_pass_threshold": False,
            "why_not": "P1 and P2 measure the claimed mechanism directly; a second "
            "threshold on retention would double-count it",
            "performance": [
                "gross return",
                "net at 10 bps per side",
                "CAGR where mathematically meaningful",
                "volatility",
                "Sharpe where meaningful",
                "drawdown",
                "SPY return",
                "benchmark-relative return",
            ],
            "performance_is_a_criterion": False,
            "performance_status_label": "PROSPECTIVE_PERFORMANCE_OBSERVATION",
        },
        "interim_policy": {
            "permitted_reports": [
                "decision captured",
                "decision packet SHA",
                "target counts",
                "data completeness",
                "execution reference captured",
                "protocol healthy",
            ],
            "forbidden_before_the_window_closes": [
                "CONFIRMED",
                "FAILED",
                "SUPPORTED PROSPECTIVELY",
                "any cumulative mechanism interpretation",
            ],
            "verdict_before_completion": "PENDING",
            "sealing": (
                "P1, P2 and the performance comparison are best kept sealed from "
                "research decision-making until the window closes"
            ),
            "promotion_before_completion": "NOT PERMITTED — the candidate stays "
            "PROSPECTIVE_CONFIRMATION_PENDING however strong a single quarter looks",
        },
        "changes_during_the_window": {
            "strategy_semantics": "FROZEN — entry threshold, retention threshold, "
            "frequency, weighting, execution timing, target-count rule, score, "
            "tradability semantics and identity semantics may not change",
            "a_semantic_change_ends_the_test": True,
            "and_requires": [
                "a new candidate version",
                "a new protocol",
                "a new prospective clock",
            ],
            "code_change_classification": {
                "NON_SEMANTIC_INFRASTRUCTURE": (
                    "logging, performance, operational retry, presentation — "
                    "permitted only while outputs remain identical"
                ),
                "SEMANTIC_RESEARCH_CHANGE": (
                    "anything affecting selection, features, ranking, identity, "
                    "execution or accounting — invalidates continuation under V1"
                ),
                "on_doubt": "FAIL CLOSED — treat it as semantic",
            },
        },
        "early_termination": {
            "legitimate_reasons": [
                "semantic pipeline defect",
                "identity defect",
                "decision-time leakage",
                "data corruption",
                "execution-reference failure preventing an honest comparison",
                "protocol-binding failure",
            ],
            "not_reasons": [
                "poor returns",
                "high turnover",
                "an unfavourable interim reading",
            ],
        },
        "defect_discovered_later": {
            "rule": "PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT",
            "affected_decisions": "documented, never rewritten",
            "a_corrected_run_requires": "a new prospective protocol",
        },
        "what_confirmation_would_mean": {
            "permitted_statement": (
                "the membership-stability mechanism independently reproduced its "
                "intended turnover and churn effect on prospectively observed data"
            ),
            "not_permitted": [
                "the strategy has proven alpha",
                "the strategy is ready for real money",
                "the historical returns are validated",
                "the strategy is production ready",
            ],
        },
        "what_failure_would_mean": {
            "label": "H2_PROSPECTIVE_MECHANISM_NOT_CONFIRMED",
            "applies_when": "P1 or P2 fails without a pipeline defect",
            "next_step": "return to hypothesis review",
            "not_permitted": (
                "changing the band and continuing the same test. That is a new "
                "hypothesis and a new protocol."
            ),
        },
        "status_at_freeze": {
            "protocol_definition": "FROZEN",
            "prospective_runner": "NOT_YET_ACTIVE",
            "first_eligible_decision": "NOT_YET_ASSIGNED",
            "observations": 0,
            "completed_transitions": 0,
            "prospective_verdict": "PENDING",
            "next_phase": "R1.7 — BUILD AND ACTIVATE THE PROSPECTIVE H2 SHADOW RUNNER",
        },
    }


def evaluate_prospective(
    *,
    completed_transitions: int,
    h2_turnover: float | None = None,
    comparator_turnover: float | None = None,
    h2_churn: float | None = None,
    comparator_churn: float | None = None,
    target_count_mismatches: Sequence[str] = (),
    hysteresis_cash_quarters: int = 0,
    hysteresis_exposure_reduction: bool = False,
    accounting_violations: Sequence[str] = (),
    identity_violations: Sequence[str] = (),
    leakage_violations: Sequence[str] = (),
    same_close_violations: Sequence[str] = (),
    retrospective_replacements: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Apply the four prospective criteria once the window has closed.

    Takes no performance measurement: the mechanism verdict cannot see one.

    Args:
        completed_transitions: How many transitions the window has recorded.
        h2_turnover: H2's cumulative annualised one-way turnover.
        comparator_turnover: The paired comparator's.
        h2_churn: H2's cumulative annualised entry/exit churn turnover.
        comparator_churn: The paired comparator's.
        target_count_mismatches: Decisions where the target-count rule differed.
        hysteresis_cash_quarters: Quarters holding cash because of hysteresis.
        hysteresis_exposure_reduction: Whether hysteresis reduced exposure.
        accounting_violations: What the harness recorded.
        identity_violations: Identity-contamination findings.
        leakage_violations: Decision-time leakage findings.
        same_close_violations: Same-close execution findings.
        retrospective_replacements: Decision packets replaced after the fact.

    Returns:
        The four criterion results, each `PENDING` until the window closes.
    """
    closed = completed_transitions >= REQUIRED_TRANSITIONS
    criteria = _criteria()
    safety = (
        list(accounting_violations)
        + list(identity_violations)
        + list(leakage_violations)
        + list(same_close_violations)
        + list(retrospective_replacements)
    )
    exposure_clean = (
        not list(target_count_mismatches)
        and hysteresis_cash_quarters == 0
        and not hysteresis_exposure_reduction
    )
    measured: list[tuple[float | None, float | None, bool | None]] = [
        (
            h2_turnover,
            comparator_turnover,
            None
            if h2_turnover is None or comparator_turnover is None
            else h2_turnover < comparator_turnover,
        ),
        (
            h2_churn,
            comparator_churn,
            None if h2_churn is None or comparator_churn is None else h2_churn < comparator_churn,
        ),
        (len(list(target_count_mismatches)) + hysteresis_cash_quarters, 0, exposure_clean),
        (len(safety), 0, not safety),
    ]
    results: list[dict[str, Any]] = []
    for criterion, (value, reference, met) in zip(criteria, measured, strict=True):
        results.append(
            {
                **criterion,
                "measured": value,
                "comparator_value": reference,
                "met": None if not closed or met is None else bool(met),
                "status": "PENDING" if not closed else ("MET" if met else "NOT_MET"),
                "window_closed": closed,
                "completed_transitions": completed_transitions,
            }
        )
    results[3]["detail"] = {
        "accounting_violations": list(accounting_violations),
        "identity_violations": list(identity_violations),
        "leakage_violations": list(leakage_violations),
        "same_close_violations": list(same_close_violations),
        "retrospective_replacements": list(retrospective_replacements),
    }
    return results


def interpret_prospective(criteria: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the frozen aggregate rule, and refuse to do so early.

    Args:
        criteria: The four prospective criterion results.

    Returns:
        The verdict, `PENDING` until every transition is in.

    Raises:
        ValueError: If the criterion set is not the four that were frozen.
    """
    if len(criteria) != len(CRITERIA):
        raise ValueError(f"the frozen rule combines {len(CRITERIA)} criteria, got {len(criteria)}")
    closed = all(bool(row.get("window_closed")) for row in criteria)
    if not closed:
        return {
            "outcome": "PENDING",
            "reason": (
                "the frozen window requires "
                f"{REQUIRED_TRANSITIONS} completed transitions. No mechanism verdict "
                "exists before it closes, whatever the interim readings look like."
            ),
            "aggregate_rule": dict(AGGREGATE_RULE),
            "completed_transitions": max(
                (int(row.get("completed_transitions", 0)) for row in criteria),
                default=0,
            ),
            "required_transitions": REQUIRED_TRANSITIONS,
            "performance_entered_this_verdict": False,
            "candidate_status": "PROSPECTIVE_CONFIRMATION_PENDING",
        }
    met = [bool(row["met"]) for row in criteria]
    confirmed = all(met)
    return {
        "outcome": (
            AGGREGATE_RULE["confirmed_label"]
            if confirmed
            else AGGREGATE_RULE["not_confirmed_label"]
        ),
        "aggregate_rule": dict(AGGREGATE_RULE),
        "criteria_met": [row["number"] for row in criteria if row["met"]],
        "criteria_failed": [row["number"] for row in criteria if not row["met"]],
        "how_this_label_was_reached": (
            "the frozen rule P1 AND P2 AND P3 AND P4, applied as written. No "
            "weighting, no majority, no qualitative override, and no partial label."
        ),
        "performance_entered_this_verdict": False,
        "what_it_means": (
            "the membership-stability mechanism independently reproduced its "
            "intended turnover and churn effect on prospectively observed data"
            if confirmed
            else "the mechanism did not reproduce its intended effect prospectively"
        ),
        "what_it_does_not_mean": [
            "proven alpha",
            "validated historical returns",
            "readiness for real money",
            "production readiness",
        ],
        "performance_status": "PROSPECTIVE_PERFORMANCE_OBSERVATION",
    }
