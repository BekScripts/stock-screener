"""What population the result describes, what it is limited by, and what follows.

Three jobs. First, the funnel: 164,193 universe observations became 39,001
executable ones, and a result about the second set is not a result about the
first — so the attrition is measured by stage, by reason and by identity class,
and the population the top-decile portfolio actually describes is named.

Second, the limitations that are easy to let slide once a return looks good:
how many observations the frozen score reached through an undated
classification, what sits at the floor of the score's scale, and how far the
delisted half of the universe survives to the tradable set.

Third, the decision. The classification rubric is written here, in code, as a
set of conditions rather than a judgement, and several of its inputs are not
known when it is written — that is the point of writing it down first.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.backtest_harness import CrossSectionRow

__all__ = [
    "CLASSIFICATIONS",
    "HOLDOUT_STATUS",
    "RUBRIC",
    "classification_limitation",
    "classify_signal",
    "evaluate_h1",
    "experiment_spec",
    "holdout_status",
    "hypothesis_record",
    "sample_attrition",
    "score_floor_audit",
]

CLASSIFICATIONS = (
    "STRONG_ENOUGH_FOR_HYPOTHESIS_TEST",
    "PROMISING_BUT_FRAGILE",
    "INCONCLUSIVE",
    "NO_MEANINGFUL_SIGNAL",
)

#: R1.0 evaluated its preregistered strategies against 2025-01…2026-03 once.
#: That was legitimate. It also means the interval is no longer untouched for
#: anything invented afterwards, and this phase says so in the record rather
#: than leaving a later reader to assume otherwise.
HOLDOUT_STATUS = {
    "R1_0_HOLDOUT_STATUS": "VALID_FOR_R1_0_PREREGISTERED_STRATEGIES",
    "NEW_DERIVED_HYPOTHESES": "HOLDOUT_ALREADY_OBSERVED",
}

#: The conditions the classification is made of, fixed before the diagnostics
#: that feed most of them were computed. Statistical significance is
#: deliberately **not** a condition: the question this phase answers is whether
#: one further hypothesis-driven experiment is warranted, not whether the
#: effect is established. The uncertainty is reported either way.
RUBRIC: dict[str, str] = {
    "direction": "mean D10-D1 and mean rank IC are positive in development, validation and the "
    "R1.0 holdout",
    "breadth": "at least 55% of months have a positive rank IC",
    "month_robustness": "removing any single month leaves both headline means positive",
    "year_robustness": "removing any single year leaves both headline means positive",
    "name_concentration": "the five largest identity contributors are under half of the profit",
    "month_concentration": "the single best month is under a quarter of the profit",
    "persistence": "top-decile membership survives one month at least half the time",
    "cost_headroom": "the analytic per-side break-even is at least twice the 10 bps sensitivity",
}


def holdout_status() -> dict[str, Any]:
    """The research status of the interval R1.0 already evaluated."""
    return {
        **HOLDOUT_STATUS,
        "interval": "2025-01 … 2026-03",
        "why": (
            "R1.0 preregistered its four strategies, hashed the preregistration, and read this "
            "interval once. That result stands for those strategies. Any rule derived after "
            "seeing it has, by construction, been informed by it."
        ),
        "valid_untouched_holdout_for_new_hypotheses": "NONE EXISTS IN THE FROZEN DATASET",
        "policy_for_r1_2": {
            "development": "ALLOWED",
            "validation": "ALLOWED",
            "r1_0_holdout": "REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION",
            "reporting_rule": (
                "report the interval's result in full and never describe it as untouched "
                "confirmation"
            ),
        },
        "true_confirmation_hierarchy": [
            "observations that did not exist when the hypothesis was frozen",
            "a paper-trading or shadow period under the frozen rules",
            "an independently frozen future extension of the research dataset",
        ],
        "not_acceptable": "inventing a new historical 'untouched' period by re-cutting the split",
    }


def sample_attrition(rows: Sequence[CrossSectionRow]) -> dict[str, Any]:
    """The funnel from the frozen universe to the tradable set, by stage and class.

    Args:
        rows: The whole R1.0 cross-section ledger.

    Returns:
        Counts and shares at each stage, the exclusion reasons ranked, the same
        funnel for each identity class, and a statement of what population the
        executable set is.
    """
    stages = ("UNIVERSE", "FEATURE_READY", "SCORE_READY", "RANKABLE", "TRADABLE", "EXECUTABLE")
    order = {name: index for index, name in enumerate(stages)}

    def funnel(sample: Sequence[CrossSectionRow]) -> dict[str, Any]:
        reached = dict.fromkeys(stages, 0)
        for row in sample:
            depth = order.get(row.stage, 0)
            for name in stages[: depth + 1]:
                reached[name] += 1
        total = reached["UNIVERSE"] or 1
        return {
            "counts": reached,
            "share_of_universe": {name: value / total for name, value in reached.items()},
        }

    reasons: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.exclusion is not None:
            reasons[row.exclusion] += 1
    total = len(rows)
    by_kind = {
        kind: funnel([row for row in rows if row.kind == kind])
        for kind in sorted({row.kind for row in rows})
    }
    executable = [row for row in rows if row.stage == "EXECUTABLE"]
    return {
        "universe_observations": total,
        "overall": funnel(rows),
        "by_kind": by_kind,
        "exclusions": {
            reason: {"observations": count, "share_of_universe": count / total}
            for reason, count in sorted(reasons.items(), key=lambda item: -item[1])
        },
        "executable_population": (
            "identity-months that the SEC evidence classified as common stock on a supported "
            "venue, that the frozen COMPOUNDER_V1_2 could score on fundamentals current for the "
            "filer's cadence, whose frozen MARKET_SIGNALS_V1 snapshot proved a single-venue "
            "liquidity floor, and which had an opening price on the next session"
        ),
        "executable_share": len(executable) / total if total else None,
        "interpretation": (
            "the top-decile result describes the tenth of that population with the highest "
            "frozen score — not the tenth of the listed market, and not the tenth of the "
            "frozen research universe"
        ),
    }


def classification_limitation(
    rows: Sequence[CrossSectionRow], holdings: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """How much of the result rests on a score that read an undated classification.

    `COMPOUNDER_V1_2` reads a company's sector, activity flag and statement
    profile from its current row, because the project keeps no history of them.
    R1.0 recorded that per observation rather than hiding it. This counts it.

    Args:
        rows: The cross-section ledger.
        holdings: The top-decile holdings ledger.

    Returns:
        The affected share of the executable set, of the selections, and of
        the portfolio's position-months.
    """
    executable = [row for row in rows if row.stage == "EXECUTABLE"]
    affected = [row for row in executable if row.classification_is_current]
    selected = {(str(h["kind"]), int(h["identity_id"]), str(h["month"])) for h in holdings}
    selected_affected = sum(
        1 for row in affected if (row.kind, row.identity_id, row.month) in selected
    )
    weight = sum(float(h["actual_weight"]) for h in holdings if str(h["kind"]) == "COMPANY")
    total_weight = sum(float(h["actual_weight"]) for h in holdings)
    return {
        "rule": "COMPOUNDER_V1_2 reads sector, activity and statement profile from the "
        "company's current row; the project keeps no history of them",
        "executable_observations": len(executable),
        "executable_affected": len(affected),
        "executable_affected_share": len(affected) / len(executable) if executable else None,
        "top_decile_selections": len(selected),
        "top_decile_selections_affected": selected_affected,
        "top_decile_selections_affected_share": (
            selected_affected / len(selected) if selected else None
        ),
        "portfolio_weight_months_affected_share": (weight / total_weight if total_weight else None),
        "severity": (
            "material by coverage: essentially every COMPANY observation is affected, because "
            "every company reconstruction reads the current row. The exposure is a "
            "reclassification risk — a company whose sector, activity flag or statement profile "
            "changed since the decision date would have been screened under today's label — "
            "not a price or fundamentals leak."
        ),
        "mitigation_available": "NONE without a dated classification history, which the "
        "frozen dataset does not contain",
    }


def score_floor_audit(rows: Sequence[CrossSectionRow]) -> dict[str, Any]:
    """Everything sitting exactly at the bottom of the score's scale.

    Args:
        rows: The cross-section ledger.

    Returns:
        The floor observations, how they are spread across identities and
        months, and whether the pattern looks like a defect or a floor.
    """
    executable = [row for row in rows if row.stage == "EXECUTABLE" and row.final_score is not None]
    floor = [row for row in executable if row.final_score == 0.0]
    identities: dict[str, int] = defaultdict(int)
    months: dict[str, int] = defaultdict(int)
    for row in floor:
        identities[f"{row.kind}:{row.identity_id}:{row.symbol}"] += 1
        months[row.month] += 1
    return {
        "executable_scored": len(executable),
        "at_exact_zero": len(floor),
        "share": len(floor) / len(executable) if executable else None,
        "distinct_identities": len(identities),
        "distinct_months": len(months),
        "by_identity": dict(sorted(identities.items())),
        "by_month": dict(sorted(months.items())),
        "observations": [
            {
                "kind": row.kind,
                "identity_id": row.identity_id,
                "symbol": row.symbol,
                "month": row.month,
                "decision": row.decision,
                "status": row.score_status,
                "freshness": row.freshness,
            }
            for row in sorted(floor, key=lambda r: (r.decision, r.kind, r.identity_id))
        ],
        "verdict_rule": (
            "a floor is expected behaviour when every component scores at its minimum; a defect "
            "would show as one identity pinned at zero across many months, or as a status other "
            "than SCORED"
        ),
    }


def evaluate_h1(
    horizons: Mapping[str, Any],
    persistence: Mapping[str, Any],
    turnover_annual: float,
    cost: Mapping[str, Any],
    name_concentration: Mapping[str, Any],
    month_concentration: Mapping[str, Any],
    leave_month: Mapping[str, Any],
    leave_year: Mapping[str, Any],
) -> dict[str, Any]:
    """Score the five premises H1 rests on against the frozen evidence.

    Args:
        horizons: The horizon diagnostics.
        persistence: The rank-persistence diagnostics.
        turnover_annual: The realised annual turnover.
        cost: The cost break-even diagnostics.
        name_concentration: Identity contribution concentration.
        month_concentration: Month contribution concentration.
        leave_month: Leave-one-month-out results.
        leave_year: Leave-one-year-out results.

    Returns:
        A verdict and its evidence for each premise.
    """

    def verdict(strong: bool, weak: bool) -> str:
        if strong:
            return "SUPPORTED"
        return "WEAKLY_SUPPORTED" if weak else "NOT_SUPPORTED"

    short = [
        float(horizons[str(h)]["d10_minus_d1"]["mean"] or 0.0) for h in (1, 5) if str(h) in horizons
    ]
    long = [
        float(horizons[str(h)]["d10_minus_d1"]["mean"] or 0.0)
        for h in (60, 120)
        if str(h) in horizons
    ]
    retention = persistence["top_decile_retention_1m"]["mean"]
    survival = persistence["top_decile_retention_3m"]["mean"]
    break_even = cost.get("break_even_cost_per_side_bps")
    net_cost = abs(
        float(cost["validation"]["observed_total_return"])
        - float(cost["validation"]["predicted_total_return"] + 0.0)
    )
    top5 = name_concentration["share_of_profit"]["top_5"]
    best_month = month_concentration["share_of_profit"]["top_1"]
    months_positive = leave_month["d10_minus_d1"]["positive_after_removal"]
    months_total = leave_month["removals"]
    years_positive = leave_year["d10_minus_d1"]["positive_after_removal"]
    years_total = leave_year["removals"]
    return {
        "A_medium_horizon_stronger": {
            "claim": "D10-D1 is materially stronger at 60/120 sessions than at 1/5",
            "evidence": {
                "short_horizon_means": short,
                "long_horizon_means": long,
                "ratio_120_over_5": (
                    long[-1] / short[-1] if short and long and short[-1] not in (0.0,) else None
                ),
            },
            "verdict": verdict(
                bool(long and short and min(long) > max(short) * 3),
                bool(long and short and min(long) > max(short)),
            ),
            "caveat": "60- and 120-session windows overlap across monthly decisions, so this "
            "is not independent evidence at those horizons",
        },
        "B_membership_persists": {
            "claim": "top-decile membership persists enough for a lower-frequency portfolio",
            "evidence": {
                "one_month_retention": retention,
                "three_month_retention": survival,
                "month_to_month_rank_correlation": persistence["rank_correlation"]["mean"],
            },
            "verdict": verdict(
                bool(retention is not None and retention >= 0.60),
                bool(retention is not None and retention >= 0.45),
            ),
        },
        "C_turnover_is_material": {
            "claim": "monthly rebalancing produces turnover worth trying to reduce",
            "evidence": {"annual_turnover": turnover_annual},
            "verdict": verdict(turnover_annual >= 3.0, turnover_annual >= 1.5),
        },
        "D_costs_bite_but_do_not_destroy": {
            "claim": "10 bps per side costs measurable performance without erasing the result",
            "evidence": {
                "break_even_cost_per_side_bps": break_even,
                "model_residual": net_cost,
            },
            "verdict": verdict(
                bool(break_even is not None and break_even >= 40.0),
                bool(break_even is not None and break_even >= 20.0),
            ),
        },
        "E_not_a_single_event": {
            "claim": "the effect is not concentrated in one name, month or year",
            "evidence": {
                "top_5_identity_share_of_profit": top5,
                "best_month_share_of_profit": best_month,
                "months_whose_removal_keeps_the_sign": f"{months_positive}/{months_total}",
                "years_whose_removal_keeps_the_sign": f"{years_positive}/{years_total}",
            },
            "verdict": verdict(
                bool(
                    top5 is not None
                    and top5 < 0.35
                    and best_month is not None
                    and best_month < 0.20
                    and months_positive == months_total
                    and years_positive == years_total
                ),
                bool(
                    top5 is not None
                    and top5 < 0.50
                    and best_month is not None
                    and best_month < 0.25
                    and months_positive == months_total
                ),
            ),
        },
    }


def classify_signal(
    splits: Mapping[str, Any],
    persistence: Mapping[str, Any],
    leave_month: Mapping[str, Any],
    leave_year: Mapping[str, Any],
    name_concentration: Mapping[str, Any],
    month_concentration: Mapping[str, Any],
    cost: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the rubric and return the classification with every condition's result.

    Args:
        splits: Split-level diagnostics.
        persistence: Rank-persistence diagnostics.
        leave_month: Leave-one-month-out results.
        leave_year: Leave-one-year-out results.
        name_concentration: Identity concentration.
        month_concentration: Month concentration.
        cost: Cost break-even diagnostics.

    Returns:
        The classification, the conditions and the reasoning.
    """
    blocks = ("DEVELOPMENT", "VALIDATION", "HOLDOUT")
    contrasts = {
        block: splits.get(block, {}).get("d10_minus_d1", {}).get("mean") for block in blocks
    }
    coefficients = {block: splits.get(block, {}).get("rank_ic", {}).get("mean") for block in blocks}
    overall_contrast = splits.get("ALL", {}).get("d10_minus_d1", {}).get("mean")
    overall_ic = splits.get("ALL", {}).get("rank_ic", {}).get("mean")
    positive_ic_share = splits.get("ALL", {}).get("rank_ic", {}).get("positive_share")
    retention = persistence["top_decile_retention_1m"]["mean"]
    break_even = cost.get("break_even_cost_per_side_bps")
    top5 = name_concentration["share_of_profit"]["top_5"]
    best_month = month_concentration["share_of_profit"]["top_1"]

    conditions = {
        "direction": all(
            value is not None and value > 0
            for value in [*contrasts.values(), *coefficients.values()]
        ),
        "breadth": bool(positive_ic_share is not None and positive_ic_share >= 0.55),
        "month_robustness": (
            leave_month["d10_minus_d1"]["positive_after_removal"] == leave_month["removals"]
            and leave_month["rank_ic"]["positive_after_removal"] == leave_month["removals"]
        ),
        "year_robustness": (
            leave_year["d10_minus_d1"]["positive_after_removal"] == leave_year["removals"]
            and leave_year["rank_ic"]["positive_after_removal"] == leave_year["removals"]
        ),
        "name_concentration": bool(top5 is not None and top5 < 0.50),
        "month_concentration": bool(best_month is not None and best_month < 0.25),
        "persistence": bool(retention is not None and retention >= 0.50),
        "cost_headroom": bool(break_even is not None and break_even >= 20.0),
    }
    failed = sorted(name for name, passed in conditions.items() if not passed)
    if overall_contrast is None or overall_ic is None or overall_contrast <= 0 or overall_ic <= 0:
        classification = "NO_MEANINGFUL_SIGNAL"
    elif not conditions["direction"]:
        classification = "INCONCLUSIVE"
    elif not failed:
        classification = "STRONG_ENOUGH_FOR_HYPOTHESIS_TEST"
    else:
        classification = "PROMISING_BUT_FRAGILE"
    return {
        "classification": classification,
        "rubric": RUBRIC,
        "conditions": conditions,
        "failed_conditions": failed,
        "measurements": {
            "d10_minus_d1_by_split": contrasts,
            "rank_ic_by_split": coefficients,
            "overall_d10_minus_d1": overall_contrast,
            "overall_rank_ic": overall_ic,
            "positive_ic_month_share": positive_ic_share,
            "top_decile_one_month_retention": retention,
            "top_5_identity_share_of_profit": top5,
            "best_month_share_of_profit": best_month,
            "break_even_cost_per_side_bps": break_even,
        },
        "statistical_note": (
            "significance is not a condition of this rubric and is not claimed: the monthly "
            "D10-D1 and rank IC both have t-statistics below 2 and bootstrap intervals that "
            "include zero. The classification answers whether one further preregistered "
            "experiment is warranted, not whether the effect is established."
        ),
        "not_a_recommendation": "this is a research-state classification, not a decision to trade",
    }


#: The one hypothesis this phase is allowed to carry forward, stated before any
#: of its premises were scored.
H1_ID = "H1_MEDIUM_HORIZON_SELECTION"
H1_STATEMENT = (
    "COMPOUNDER_V1_2 appears to be a persistent medium-horizon selection signal rather than a "
    "short-horizon timing signal; a lower-frequency top-decile portfolio may preserve much of "
    "the selection effect while reducing turnover and transaction-cost sensitivity."
)

#: The single experiment H1 licenses, if it survives. One frequency, one
#: selection fraction, one weighting, one execution rule, one cost assumption.
R1_2_STRATEGY = "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"


def hypothesis_record(
    *,
    premises: Mapping[str, Any],
    classification: Mapping[str, Any],
    binding: Mapping[str, Any],
    code_commit: str,
    limitations: Sequence[str],
    evidence_against: Sequence[str],
) -> dict[str, Any]:
    """Freeze H1, what motivated it, and what would count against it.

    The record states plainly that R1.0's already-observed results motivated
    the hypothesis. That is what makes it a hypothesis rather than a finding,
    and it is why the interval R1.0 read cannot confirm it.

    Args:
        premises: The scored premises from `evaluate_h1`.
        classification: The signal classification.
        binding: The R1.0 record this hypothesis is derived from.
        code_commit: The commit the hypothesis was written at.
        limitations: The frozen limitations carried forward.
        evidence_against: What the diagnostics say against the hypothesis,
            measured rather than asserted.

    Returns:
        The hypothesis record.
    """
    verdicts = {name: body["verdict"] for name, body in premises.items()}
    unsupported = sorted(name for name, verdict in verdicts.items() if verdict == "NOT_SUPPORTED")
    status = "REJECTED" if unsupported else "ACCEPTED_FOR_ONE_EXPERIMENT"
    return {
        "hypothesis_id": H1_ID,
        "status": status,
        "statement": H1_STATEMENT,
        "derived_from": {
            "phase": "R1.0",
            "completion_marker_sha256": binding.get("marker_sha256"),
            "dataset_id": binding.get("dataset_id"),
            "contract_version": binding.get("contract_version"),
            "acknowledgement": (
                "R1.0's results — which included one evaluation of the 2025-01…2026-03 "
                "interval — motivated this hypothesis. It is therefore derived from observed "
                "data and cannot be confirmed by that same interval."
            ),
        },
        "code_commit": code_commit,
        "premises": premises,
        "premise_verdicts": verdicts,
        "unsupported_premises": unsupported,
        "signal_classification": classification.get("classification"),
        "evidence_against": list(evidence_against),
        "limitations": list(limitations),
        "data_already_observed": {
            "development": "2022-01 … 2023-12",
            "validation": "2024-01 … 2024-12",
            "r1_0_holdout": "2025-01 … 2026-03 (read once, under R1.0's preregistration)",
        },
        "holdout_contamination": HOLDOUT_STATUS,
        "falsification_criteria": {
            "stated_before_the_experiment": True,
            "criteria": [
                "the quarterly cross-section's mean D10-D1 at the 60-session horizon is not "
                "positive over the full window",
                "top-decile selection at quarter ends loses the monotone top-end ordering that "
                "monthly selection shows, measured as a non-positive decile rank association",
                "annual turnover does not fall by at least half against the monthly baseline's "
                "3.90x",
                "the benchmark-relative total return is not positive in at least two of the "
                "three blocks",
                "the five largest identity contributors account for half or more of the profit",
            ],
            "note": "thresholds are fixed here, before the experiment runs, and are not to be "
            "revised after seeing its result",
        },
        "next_experiment": R1_2_STRATEGY,
    }


def experiment_spec(
    *,
    hypothesis: Mapping[str, Any],
    binding: Mapping[str, Any],
    code_commit: str,
) -> dict[str, Any]:
    """Freeze the one R1.2 experiment. It is specified here and executed elsewhere.

    Args:
        hypothesis: The hypothesis record this experiment tests.
        binding: The R1.0 record and dataset bindings.
        code_commit: The commit the specification was written at.

    Returns:
        The experiment specification.

    Raises:
        ValueError: If the hypothesis was not accepted. A specification without
            a surviving hypothesis is a strategy someone simply wanted to try.
    """
    if hypothesis.get("status") != "ACCEPTED_FOR_ONE_EXPERIMENT":
        raise ValueError(
            "no accepted hypothesis: an R1.2 specification may not be created without one"
        )
    return {
        "experiment_id": "R1.2",
        "strategy_version": R1_2_STRATEGY,
        "tests_hypothesis": hypothesis.get("hypothesis_id"),
        "code_commit": code_commit,
        "dataset_id": binding.get("dataset_id"),
        "contract_version": binding.get("contract_version"),
        "rules": {
            "signal": "quarter-end close — the last benchmark session of each calendar quarter",
            "ranking": "frozen COMPOUNDER_V1_2, descending; ties by (kind, identity_id)",
            "selection": "top 10% of the executable cross-section",
            "weighting": "equal weight",
            "execution": "open of the first benchmark session strictly after the signal",
            "rebalance": "calendar quarter ends only",
            "sector_logic": "NONE",
            "leverage": "NONE",
            "shorting": "NONE",
            "cost_sensitivity": "SIMPLE_10BPS_PER_SIDE_V1, unchanged",
            "terminal_policy": "TERMINAL_LEG_POLICY_V1, unchanged",
            "share_policy": "FRACTIONAL_SHARES_RESEARCH_V1, unchanged",
            "benchmark": "SPY, unchanged",
        },
        "why_quarterly": (
            "the 60-session horizon is approximately one quarter, and the frozen diagnostics "
            "show markedly stronger decile separation at 60 and 120 sessions than at 1 and 5. "
            "Quarterly is one hypothesis-derived test point chosen before any quarterly result "
            "exists. It is not claimed to be optimal, and no other frequency is tested."
        ),
        "optimization_performed": "NONE",
        "variants_tried": 1,
        "parameter_sweeps": [],
        "frequencies_tested": 1,
        "selection_fractions_tested": 1,
        "weightings_tested": 1,
        "execution_rules_tested": 1,
        "cost_levels_tested": 1,
        "historical_holdout_status": HOLDOUT_STATUS,
        "reporting_rule": (
            "the 2025-01…2026-03 result is reported in full and is never described as untouched "
            "confirmation of this hypothesis"
        ),
        "true_confirmation_requirement": (
            "observations that did not exist when this specification was frozen, or a paper "
            "trading period run under exactly these rules"
        ),
        "falsification_criteria": hypothesis.get("falsification_criteria"),
        "executed_in_r1_1": False,
    }
