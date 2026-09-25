"""The H2 record and the R1.5 specification, written but never executed.

A preregistration is only worth anything if it is uncomfortable later, so the
two documents here are written to be hard to wriggle out of. The criteria are
fixed, the aggregate rule that combines them is fixed — which is the one place
H2 deliberately differs from H1, where no such rule existed and its absence had
to be respected after the fact — and the rule carries exactly one
parameterisation with the alternatives named as prohibited rather than left
unmentioned.

The honest part is the contamination record. H2 was derived from R1.3's analysis
of the whole frozen period, development, validation and the already-read holdout
alike. Nothing in that period can confirm it. A later historical run is worth
doing and is worth calling what it is: an exploratory test of a preregistered
rule, not an out-of-sample result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .rule import (
    ENTRY_WIDTHS,
    RETENTION_WIDTHS,
    RULE,
    RULE_VERSION,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AGGREGATE_RULE",
    "COMPARATOR",
    "CRITERIA",
    "H2_ID",
    "HISTORICAL_STATUS",
    "LEGACY_H1_TURNOVER_REFERENCE",
    "PROHIBITED_VARIATIONS",
    "SPEC_STATUS",
    "experiment_spec",
    "hypothesis_record",
]

#: The one hypothesis this phase defines.
H2_ID = "H2_MEMBERSHIP_STABILITY"

#: The only comparator H2 is judged against.
COMPARATOR = "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"

#: How a later historical run must describe itself, whatever it finds.
HISTORICAL_STATUS = "EXPLORATORY_HISTORICAL_TEST"

#: The R1.5 specification's state until it is run. Exactly once, then never.
SPEC_STATUS = "PREREGISTERED_NOT_EXECUTED"

#: H1's turnover ceiling, kept as a continuity reading and not a criterion.
#: R1.3 classified it `OTHER`: an empirical anchor times an undeclared one half.
LEGACY_H1_TURNOVER_REFERENCE = 1.95

#: What a later phase may not vary while calling itself H2.
PROHIBITED_VARIATIONS: tuple[str, ...] = (
    "any entry threshold other than the ordinary top-decile target count",
    "any retention band other than two selection widths",
    "any rebalance frequency other than calendar quarter ends",
    "any weighting other than equal",
    "any execution rule other than the next session's open",
    "any cost convention other than SIMPLE_10BPS_PER_SIDE_V1",
    "a minimum holding period, cooldown or trade-size threshold",
    "a turnover penalty or score smoothing",
    "a fixed position count or position-weight drift band",
    "rank averaging across decisions",
    "sector, industry or any other exposure constraint",
    "a second parameterisation of any kind, evaluated in the same run or another",
)


def _criteria(comparator: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the four frozen criteria against the comparator's measurements."""
    turnover = float(comparator["annual_turnover"])
    churn = float(comparator["ranking_churn_annual_turnover"])
    return [
        {
            "number": 1,
            "id": "C1_TURNOVER_DIRECTION",
            "frozen_text": (
                "H2's annual one-way turnover is not strictly below "
                f"{turnover!r}x, the comparator's"
            ),
            "requirement": f"annual turnover < {turnover!r}x",
            "comparator_value": turnover,
            "threshold": turnover,
            "direction": "STRICTLY_LESS_THAN",
            "measured_on": "the frozen R1.2 turnover definition, unchanged",
        },
        {
            "number": 2,
            "id": "C2_RANKING_CHURN_DIRECTION",
            "frozen_text": (
                "the annualised turnover attributable to membership entry and exit "
                f"is not below {churn!r}x, the comparator's"
            ),
            "requirement": f"annualised entry/exit turnover < {churn!r}x",
            "comparator_value": churn,
            "threshold": churn,
            "direction": "STRICTLY_LESS_THAN",
            "measured_on": (
                "absolute annualised turnover contribution, not a share. A share "
                "can fall while the quantity rises, so the share is reported and "
                "the quantity decides."
            ),
        },
        {
            "number": 3,
            "id": "C3_EXPOSURE_PRESERVATION",
            "frozen_text": (
                "the turnover reduction comes from holding fewer names, from "
                "deliberate cash, or from any reduction in exposure the hysteresis "
                "itself caused"
            ),
            "requirement": (
                "the same target-count rule as R1.2 at every decision, 0 quarters "
                "holding intentional cash, and no exposure reduction attributable "
                "to hysteresis"
            ),
            "threshold": 0,
            "direction": "EQUALS",
            "measured_on": (
                "target count per decision against R1.2's, the count of quarters "
                "flagged as holding cash, and mean invested weight. Residual cash "
                "from the frozen share and execution policy is expected and allowed."
            ),
        },
        {
            "number": 4,
            "id": "C4_ACCOUNTING_AND_EXECUTION_SAFETY",
            "frozen_text": (
                "any accounting violation, identity violation, future leakage or "
                "unauthorised same-close execution occurs"
            ),
            "requirement": (
                "0 accounting violations, 0 identity violations, 0 future leakage, "
                "0 same-close executions"
            ),
            "threshold": 0,
            "direction": "EQUALS",
            "measured_on": "the harness's own invariants and the bias audit",
        },
    ]


#: The aggregate rule, stated before the experiment rather than withheld.
AGGREGATE_RULE: dict[str, Any] = {
    "expression": "C1 AND C2 AND C3 AND C4",
    "supported_label": "SUPPORTED",
    "not_supported_label": "NOT_SUPPORTED",
    "partial_labels_permitted": False,
    "stated_before_the_experiment": True,
    "why_h2_has_one_and_h1_did_not": (
        "H1 stated five criteria and no way of combining them, so R1.2 could only "
        "report each and summarise. That was respected rather than repaired after "
        "the fact. H2 fixes the combination up front so the same ambiguity cannot "
        "arise twice."
    ),
    "what_it_does_not_decide": (
        "SUPPORTED means the claimed mechanism behaved as claimed: the rule cut "
        "turnover, cut it by cutting entry and exit churn, and did so without "
        "shrinking the portfolio or breaking an invariant. It is not a statement "
        "that H2 is a better investment than the comparator, and performance plays "
        "no part in it."
    ),
}

#: Criteria are listed here for import; the values are bound at build time.
CRITERIA = (
    "C1_TURNOVER_DIRECTION",
    "C2_RANKING_CHURN_DIRECTION",
    "C3_EXPOSURE_PRESERVATION",
    "C4_ACCOUNTING_AND_EXECUTION_SAFETY",
)


def hypothesis_record(
    *,
    evidence: Mapping[str, Any],
    comparator: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the H2 hypothesis record.

    Args:
        evidence: The R1.3 mechanism measurements H2 rests on.
        comparator: The frozen R1.2 figures H2 is judged against.
        provenance: Digests and commits of the records this was derived from.

    Returns:
        The record, as a JSON-safe mapping.
    """
    return {
        "hypothesis_id": H2_ID,
        "status": "PREREGISTERED_NOT_EXECUTED",
        "statement": (
            "Holding the quarterly rebalance frequency, CompounderScore, top-decile "
            "target size, equal weighting, execution timing and every data semantic "
            "fixed, a deterministic one-decile membership-hysteresis rule will "
            "reduce turnover by reducing rank-driven entries and exits while "
            "preserving portfolio exposure."
        ),
        "claims": {
            "mechanism": (
                "turnover falls, and falls because fewer positions are entered and "
                "exited, not because fewer positions are held"
            ),
            "performance": (
                "NONE. H2 makes no claim that returns improve, and no performance "
                "measurement takes part in its verdict."
            ),
        },
        "supersedes": {
            "hypothesis_id": "H1_MEDIUM_HORIZON_SELECTION",
            "final_status": "H1_MECHANISTICALLY_FALSIFIED",
            "reopened": False,
            "note": (
                "H1 is closed and stays closed. H2 is a different claim about a "
                "different lever, not a retry of H1 under another name."
            ),
        },
        "why_this_hypothesis_exists": {
            "mechanism_identified_by": "R1.3",
            "mechanism": "M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT",
            "reasoning": [
                "reducing rebalance frequency alone was not enough: annual turnover "
                f"fell only to {evidence['quarterly_annual_turnover']!r}x",
                "per-event turnover rose by a factor of "
                f"{evidence['per_rebalance_ratio']!r} as the gap between rebalances grew",
                f"{evidence['ranking_churn_share']!r} of quarterly turnover came from "
                "entry and exit churn rather than from resizing",
                "holding the same selected memberships and removing every resizing "
                f"trade still leaves a floor of {evidence['membership_floor']!r}x",
                "therefore any further material reduction requires changing "
                "membership persistence itself, which is what this rule does",
            ],
            "what_is_not_claimed": (
                "that a hysteresis band would have worked historically. It has never "
                "been run. R1.3 measured where names sit relative to the cutoff and "
                "deliberately chose no band from those distributions; this rule's "
                "width is structural, not fitted."
            ),
            "evidence_against_an_easy_result": [
                "only "
                f"{evidence['share_moving_under_10_points']!r} of membership changes "
                "move a name less than ten rank-percentile points, and "
                f"{evidence['share_moving_over_20_points']!r} move it more than twenty, "
                "so most churn is substantial ranking movement rather than names "
                "hovering at the boundary — a band may therefore catch less of it "
                "than its motivation suggests",
                f"{evidence['exit_share_not_ranking']!r} of exits and "
                f"{evidence['entry_share_not_ranking']!r} of entries are availability "
                "changes rather than score decisions, and no retention band can "
                "prevent those",
                "retaining a name the ordinary rule would sell means not holding a "
                "higher-ranked name, and the cost of that is unmeasured",
            ],
        },
        "rule": dict(RULE),
        "rule_version": RULE_VERSION,
        "rule_parameterisations": 1,
        "entry_widths": ENTRY_WIDTHS,
        "retention_widths": RETENTION_WIDTHS,
        "band_is_structural_not_optimised": (
            "the band is two selection widths: one width admits, a second width "
            "retains. It is expressed in names rather than percent so it inherits "
            "the entry rule's rounding instead of introducing a second one, and it "
            "equals the top 20% by construction. No alternative width was evaluated, "
            "and none may be."
        ),
        "selection_algorithm": [
            "1. build the same executable ranked cross-section R1.2 builds",
            "2. take the ordinary top-decile target count from the frozen R1.2 rule",
            "3. remove incumbents the frozen contract no longer permits",
            "4. keep remaining incumbents whose rank position is inside twice the count",
            "5. if kept incumbents exceed the count, drop the worst-ranked until it fits",
            "6. fill any vacancy with the highest-ranked eligible identities not held",
            "7. the final count equals the ordinary count whenever enough names exist",
            "8. equal-weight the selected set",
            "9. execute at the next session's open, exactly as R1.2",
        ],
        "tie_breaking": "the existing stable ordering: descending score, then (kind, identity_id)",
        "first_rebalance": (
            "no incumbents exist, so the first portfolio is exactly R1.2's top-decile "
            "selection for that decision. Hysteresis begins at the second rebalance."
        ),
        "forced_exits_override_retention": [
            "universe ineligibility",
            "missing score",
            "failed tradability",
            "missing required execution eligibility",
            "terminal state",
            "any contract violation",
        ],
        "identical_to_r1_2": [
            "quarterly frequency and the same 17 decision dates",
            "COMPOUNDER_V1_2",
            "cross-section construction",
            "the top-decile target count",
            "equal weighting",
            "the $100,000 nominal capital convention",
            "signal at the quarter-end close",
            "execution at the next session's open",
            "SIMPLE_10BPS_PER_SIDE_V1",
            "TERMINAL_LEG_POLICY_V1",
            "SPY as benchmark",
            "price data, identity rules, missingness rules, corporate-action behaviour",
        ],
        "comparator": {
            "strategy_version": COMPARATOR,
            "why_this_one": (
                "H2 changes one thing about R1.2 and must be measured against R1.2. "
                "The monthly portfolio is context, never the primary comparison."
            ),
            **dict(comparator),
        },
        "primary_variable": {
            "name": "annual one-way turnover",
            "definition": (
                "sum of absolute BUY and SELL notional divided by twice the pre-trade "
                "NAV at the rebalance open, annualised by nominal quarterly frequency"
            ),
            "definition_changed": False,
        },
        "falsification_criteria": {
            "criteria": _criteria(comparator),
            "aggregate_rule": dict(AGGREGATE_RULE),
            "stated_before_the_experiment": True,
            "note": (
                "thresholds are fixed here, before the experiment runs, and are not "
                "to be revised after seeing its result"
            ),
        },
        "legacy_h1_reference": {
            "value": LEGACY_H1_TURNOVER_REFERENCE,
            "role": "CONTINUITY_DIAGNOSTIC_ONLY",
            "is_an_h2_criterion": False,
            "why_not": (
                "R1.3 classified 1.95x as a product of an empirical anchor and an "
                "undeclared one half, with nothing in the record deriving the half. "
                "A later run may report whether H2 happens to cross it; crossing it "
                "proves nothing about H2 and failing to cross it refutes nothing."
            ),
            "threshold_moved": False,
        },
        "secondary_diagnostics": {
            "status": "DESCRIPTIVE — none of these decides the H2 verdict",
            "performance": [
                "total return",
                "CAGR",
                "volatility",
                "Sharpe",
                "Sortino",
                "maximum drawdown",
                "Calmar",
                "benchmark-relative return",
                "10 bps per side sensitivity",
            ],
            "performance_compared_against": [COMPARATOR, "SPY"],
            "performance_winner_defined": False,
            "signal_preservation": [
                "selected-set 20-session return",
                "selected-set 60-session return",
                "selected-set 120-session return",
                "selected set versus eligible cross-section mean",
                "average selected COMPOUNDER_V1_2 score",
                "selected score percentile distribution",
            ],
            "membership": [
                "quarter-to-quarter retention",
                "entrants per rebalance",
                "exits per rebalance",
                "re-entry identities and events",
                "holding-duration distribution",
                "rank of retained incumbents",
                "rank of newly admitted identities",
            ],
            "mechanistic_expectation": (
                "retention rises and entry/exit churn falls. No numeric retention "
                "target is set, because setting one from R1.3's measurements would "
                "be the same error as choosing the band from them."
            ),
            "displacement": {
                "what_to_record": (
                    "for every incumbent the band retains that the ordinary rule "
                    "would have dropped: identity, rank percentile, score, and its "
                    "ordinary top-decile status; and for the higher-ranked candidate "
                    "it kept out, the same fields"
                ),
                "purpose": "the rule's opportunity cost, measured rather than assumed",
                "forward_returns_may_be_consulted_when_pairing": False,
            },
        },
        "selection_inputs_permitted": [
            "current holdings",
            "current frozen eligibility under the contract",
            "current frozen COMPOUNDER_V1_2 rank",
        ],
        "selection_inputs_forbidden": [
            "any future return",
            "any future price",
            "any next-quarter score",
            "holding profitability",
            "any benchmark result",
        ],
        "historical_evidence_status": dict(
            _contamination(),
        ),
        "prohibited_variations": list(PROHIBITED_VARIATIONS),
        "if_the_result_is_poor": (
            "the rule is not adjusted and re-run. The band, the frequency, the entry "
            "threshold, the weighting and the execution rule are frozen at this "
            "version. Any variation requires a new hypothesis id and a new "
            "preregistration, and remains historically contaminated for the same "
            "reason this one is."
        ),
        "runs_permitted": 1,
        "derived_from": dict(provenance),
        "evidence": dict(evidence),
        "limitations": list(LIMITATIONS),
        "language": (
            "research-state only. Nothing here is proven, validated profitable or "
            "production ready, and H2 has not been run."
        ),
    }


def _contamination() -> dict[str, Any]:
    """State plainly what the frozen history can and cannot establish for H2."""
    return {
        "r1_3_used_the_full_historical_period": True,
        "blocks_exposed": ["DEVELOPMENT", "VALIDATION", "HOLDOUT"],
        "the_frozen_dataset_is": "HYPOTHESIS_GENERATION_DATA_FOR_H2",
        "a_later_historical_run_may_provide": [
            "EXPLORATORY_HISTORICAL_EVIDENCE",
            "MECHANISM_EVIDENCE",
        ],
        "a_later_historical_run_may_not_provide": [
            "INDEPENDENT_CONFIRMATION",
            "CLEAN_HOLDOUT_CONFIRMATION",
            "OUT_OF_SAMPLE_CONFIRMATION",
            "PROSPECTIVE_CONFIRMATION",
        ],
        "mandatory_classification_of_a_historical_run": HISTORICAL_STATUS,
        "clean_h2_holdout_remains": False,
        "holdout_reset": False,
        "why_no_reset_is_possible": (
            "every month of the frozen dataset has been exposed to the research "
            "process — R1.0 read all of it, R1.1 interpreted it, R1.2 measured a "
            "strategy over it and R1.3 analysed its mechanism across all three "
            "blocks. Renaming a subperiod would not make it unseen; it would only "
            "make the record less honest."
        ),
        "CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST": True,
        "what_independent_confirmation_would_require": (
            "data unavailable to the process that generated H2: either prospective "
            "observations accumulated after this preregistration, or a genuinely "
            "untouched external historical sample carrying equivalent point-in-time "
            "data semantics."
        ),
        "prospective_duration_chosen_here": None,
        "why_not_chosen": (
            "no existing research policy defines one, and inventing a duration in a "
            "preregistration phase would fix a number nothing yet justifies."
        ),
    }


#: Carried forward unchanged. Preregistering a hypothesis retires none of them.
LIMITATIONS = (
    "COMPOUNDER_V1_2 reads an undated classification internally; the exposure is "
    "quantified per run and remains material and unresolved",
    "sector and industry history is unavailable; no result is attributed to a sector",
    "exchange and listing membership is partly inferred",
    "market capitalisation is a lower bound where a share count is a cover-page figure",
    "average daily volume is single-venue (IEX) and partial",
    "historical-security fundamentals carry a conservative filed-date lag",
    "seventeen quarterly rebalances is a small sample for any distributional claim",
    "the validation block underperformed its benchmark by 7.3 percentage points under "
    "the comparator, and nothing in H2 addresses that",
)


def experiment_spec(
    *,
    hypothesis_sha256: str,
    comparator: Mapping[str, Any],
    bindings: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the R1.5 specification, frozen and unexecuted.

    Args:
        hypothesis_sha256: The digest of the H2 record this binds to.
        comparator: The frozen comparator figures.
        bindings: The dataset, contract and upstream-phase digests.

    Returns:
        The specification, as a JSON-safe mapping.
    """
    return {
        "experiment_id": "R1.5",
        "strategy_version": "COMPOUNDER_QUARTERLY_HYSTERESIS_V1",
        "rule_version": RULE_VERSION,
        "tests_hypothesis": H2_ID,
        "hypothesis_sha256": hypothesis_sha256,
        "status": SPEC_STATUS,
        "executed": False,
        "runs_permitted": 1,
        "rules": dict(RULE),
        "optimization_performed": "NONE",
        "variants_tried": 1,
        "parameter_sweeps": [],
        "frequencies_tested": 1,
        "entry_thresholds_tested": 1,
        "retention_bands_tested": 1,
        "selection_fractions_tested": 1,
        "weightings_tested": 1,
        "execution_rules_tested": 1,
        "cost_levels_tested": 1,
        "comparator": {"strategy_version": COMPARATOR, **dict(comparator)},
        "primary_variable": "annual one-way turnover, R1.2 definition unchanged",
        "criteria_count": len(CRITERIA),
        "criteria": list(CRITERIA),
        "aggregate_rule": dict(AGGREGATE_RULE),
        "legacy_h1_turnover_reference": {
            "value": LEGACY_H1_TURNOVER_REFERENCE,
            "is_a_criterion": False,
        },
        "historical_evidence_status": _contamination(),
        "result_classification": HISTORICAL_STATUS,
        "prohibited_variations": list(PROHIBITED_VARIATIONS),
        "bindings": dict(bindings),
        "execution_notes": (
            "R1.5 executes this once. The binder refuses a spec already marked "
            "executed, a spec whose rules differ from the executor's in any entry, "
            "and any spec offering more than one of anything."
        ),
    }
