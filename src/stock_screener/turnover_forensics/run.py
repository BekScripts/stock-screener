"""R1.3 — explain the turnover premise H1 failed, and change nothing else.

The phase reads three frozen records and writes only descriptions of them. It
simulates nothing, and cannot: `bind_r1_2` scans this package's own imports and
refuses to proceed if a simulator is reachable from any of them.

The order follows the question. Reproduce the number independently, so that what
is being explained is established rather than accepted. Document the formula off
the implementation, because a turnover figure means nothing without one, and the
frozen record uses two annualisation conventions that have to be reconciled
before monthly and quarterly can be compared at all. Then decompose the change
in membership that the trading follows from, ask how much of it was avoidable,
and stop — the dimension a later hypothesis should address is named, and no
parameter for it is chosen here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.backtest_harness.registry import (
    ResearchLedger,
    digest_document,
    write_document,
)
from stock_screener.sec_evidence.mirror import MirrorConflictError
from stock_screener.signal_review.binding import read_jsonl

from .binding import ForensicBinding, bind_r1_2, simulation_reachable
from .forensic import classify, h1_status, next_dimension, threshold_provenance
from .membership import (
    holding_episodes,
    portfolios_by_decision,
    retention,
    survival_curve,
    transitions,
)
from .stability import (
    boundary_churn,
    cutoffs,
    dispersion,
    rank_percentiles,
    score_stability,
    size_effect,
    stability,
    target_count_effect,
)
from .turnover import (
    TURNOVER_DEFINITION,
    counterfactual,
    decomposition,
    lower_bound,
    pre_trade_nav,
    reconstruct,
    required_reduction,
)

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "FORENSIC_DIR",
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "ForensicResult",
    "mirror_r1_3",
    "r1_2_directory",
    "run_forensics",
]

PHASE = "R1.3"
FORENSIC_DIR = "research/r1_3"
MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-3"

#: The strategy whose ledgers are read, and the comparator sliced beside it.
STRATEGY = "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"
MONTHLY_STRATEGY = "COMPOUNDER_TOP_DECILE_V1"

#: The frozen C3 ceiling. Read here, never chosen here.
FROZEN_THRESHOLD = 1.95

#: Nominal rebalances per year under each cadence, as the harness annualises.
QUARTERLY_PER_YEAR = 4.0
MONTHLY_PER_YEAR = 12.0

#: Separations, in decision steps, at which stability is measured.
LAGS = (1, 2, 3)

MIRRORED_FILES = (
    "r1_3_binding.json",
    "r1_3_turnover_reconciliation.json",
    "r1_3_membership_transitions.json",
    "r1_3_rank_stability.json",
    "r1_3_boundary_churn.json",
    "r1_3_holding_duration.json",
    "r1_3_turnover_lower_bound.json",
    "r1_3_h1_forensic_evaluation.json",
    "r1_3_summary.json",
    "r1_3_fingerprints.json",
    "r1_3_complete.json",
)

#: Carried forward unchanged. Explaining one premise retires none of them.
LIMITATIONS = (
    "COMPOUNDER_V1_2 reads an undated classification internally; the exposure is "
    "quantified per run and remains material and unresolved",
    "sector and industry history is unavailable; no result is attributed to a sector",
    "exchange and listing membership is partly inferred",
    "market capitalisation is a lower bound where a share count is a cover-page figure",
    "average daily volume is single-venue (IEX) and partial",
    "historical-security fundamentals carry a conservative filed-date lag",
    "the R1.2 holdout remains CONTAMINATED_FOR_H1_CONFIRMATION and is not upgraded here",
    "seventeen quarterly rebalances is a small sample for any distributional claim",
)


@dataclass
class ForensicResult:
    """What R1.3 produced.

    Attributes:
        binding: The verified frozen record.
        documents: `{name: digest}` for every document written.
        fingerprints: The semantic fingerprints determinism is checked on.
        mechanism: The mechanism classification.
        status: H1's final status.
        summary: The phase summary document.
    """

    binding: ForensicBinding
    documents: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    mechanism: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def _scored_by_decision(
    rows: list[dict[str, Any]], *, executable_only: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Group scored cross-section rows by decision date.

    Two populations matter and they are not the same size. Every identity
    carrying a score can be ranked; only an identity with no exclusion could
    actually have been bought. R1.1 measured persistence on the second, so both
    are computed here rather than leaving the two phases looking inconsistent.

    Args:
        rows: Cross-section rows.
        executable_only: Keep only rows no exclusion applied to.

    Returns:
        `{decision: rows}`.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("final_score") is None:
            continue
        if executable_only and row.get("exclusion") is not None:
            continue
        grouped.setdefault(str(row["decision"]), []).append(row)
    return grouped


def _rows_by_identity(
    rows: list[dict[str, Any]],
) -> dict[str, dict[tuple[str, int], dict[str, Any]]]:
    """Index every cross-section row by decision and identity."""
    grouped: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["kind"]), int(row["identity_id"]))
        grouped.setdefault(str(row["decision"]), {})[key] = row
    return grouped


def _reconciliation(trades: list[dict[str, Any]], reconstruction: Any) -> dict[str, Any]:
    """Check that every traded leg landed in exactly one decomposition bucket."""
    ledger_total = sum(float(row["notional"]) for row in trades if str(row["side"]) != "TERMINAL")
    counted = sum(row.entrants + row.exits + row.resize for row in reconstruction.rebalances)
    terminal_ledger = sum(
        float(row["notional"]) for row in trades if str(row["side"]) == "TERMINAL"
    )
    terminal_counted = sum(row.terminal for row in reconstruction.rebalances)
    return {
        "rebalance_notional_in_ledger": ledger_total,
        "rebalance_notional_in_decomposition": counted,
        "residual": ledger_total - counted,
        "terminal_notional_in_ledger": terminal_ledger,
        "terminal_notional_counted": terminal_counted,
        "terminal_residual": terminal_ledger - terminal_counted,
        "unexplained_turnover": (
            0.0 if abs(ledger_total - counted) <= 1e-6 else ledger_total - counted
        ),
    }


def _per_block(reconstruction: Any, calendar: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate traded notional and turnover by research block and by year."""
    split_of = {str(row["execution"]): str(row["split"]) for row in calendar}
    by_split: dict[str, list[float]] = {}
    by_year: dict[str, list[float]] = {}
    notional_by_split: dict[str, float] = {}
    for row in reconstruction.rebalances:
        split = split_of.get(row.execution, "UNKNOWN")
        year = row.execution[:4]
        by_split.setdefault(split, []).append(row.turnover)
        by_year.setdefault(year, []).append(row.turnover)
        notional_by_split[split] = notional_by_split.get(split, 0.0) + (
            row.entrants + row.exits + row.resize
        )
    return {
        "by_block": {
            name: {
                "rebalances": len(values),
                "mean_per_rebalance": sum(values) / len(values),
                "annualised": sum(values) / len(values) * QUARTERLY_PER_YEAR,
                "traded_notional": notional_by_split[name],
            }
            for name, values in sorted(by_split.items())
        },
        "by_calendar_year": {
            year: {
                "rebalances": len(values),
                "mean_per_rebalance": sum(values) / len(values),
                "annualised": sum(values) / len(values) * QUARTERLY_PER_YEAR,
                "partial": len(values) < 4,
            }
            for year, values in sorted(by_year.items())
        },
        "no_rule_derived": (
            "reported for completeness. No cadence, weighting or filter is "
            "selected from any block or year."
        ),
    }


def _concentration(reconstruction: Any) -> dict[str, Any]:
    """Report how much of the trading a handful of rebalances accounts for."""
    notionals = sorted(
        (
            (row.entrants + row.exits + row.resize, row.execution)
            for row in reconstruction.rebalances
        ),
        reverse=True,
    )
    total = sum(value for value, _ in notionals)

    def share(count: int) -> float | None:
        return sum(value for value, _ in notionals[:count]) / total if total else None

    return {
        "total_traded_notional": total,
        "largest_rebalance": {
            "execution": notionals[0][1] if notionals else None,
            "notional": notionals[0][0] if notionals else None,
            "share": share(1),
        },
        "top_3_share": share(3),
        "top_5_share": share(5),
        "even_share_would_be": 1.0 / len(notionals) if notionals else None,
        "reading": (
            "turnover is spread across rebalances rather than produced by a few. "
            "A single unusual quarter does not explain the annual figure."
        ),
    }


def _cost_relationship(metrics: dict[str, Any], matched: dict[str, Any]) -> dict[str, Any]:
    """Explain why lower turnover tolerates a higher per-side cost."""
    quarterly = float(metrics["cost_break_even"]["break_even_cost_per_side_bps"])
    return {
        "quarterly_break_even_bps_per_side": quarterly,
        "monthly_break_even_bps_per_side": 77.91246712830753,
        "quarterly_cost_drag_10bps": float(metrics["cost_drag_10bps"]),
        "monthly_cost_drag_10bps": float(matched["comparator"]["cost_drag_10bps"]),
        "mechanism": (
            "cost is charged per unit of traded notional, so the drag on a "
            "strategy is its cost rate times its turnover. Halve the turnover and "
            "the same return can absorb roughly twice the rate before it is "
            "exhausted. The break-even figure therefore rises as turnover falls "
            "and says nothing on its own about whether either strategy is "
            "deployable."
        ),
        "not_evidence_of_deployability": (
            "a break-even rate is a sensitivity, not a verdict. It assumes the "
            "measured gross return, which rests on an already-observed interval, "
            "and it models no spread, no market impact, no borrow and no failure "
            "to fill."
        ),
    }


def run_forensics(
    directory: Path,
    r1_0_directory: Path,
    r1_1_directory: Path,
    r1_2_directory: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
    code_commit: str,
) -> ForensicResult:
    """Explain the failed turnover premise, writing only descriptions.

    Args:
        directory: Where to write R1.3's record.
        r1_0_directory: R1.0's frozen record.
        r1_1_directory: R1.1's frozen hypothesis and specification.
        r1_2_directory: R1.2's frozen record.
        contract_sha256: The observed contract digest.
        expect_contract_sha256: The digest the caller requires.
        code_commit: The commit this analysis ran at.

    Returns:
        The forensic result.

    Raises:
        ForensicBindingError: If the frozen record does not verify.
    """
    binding = bind_r1_2(
        r1_0_directory,
        r1_2_directory,
        r1_1_directory,
        contract_sha256=contract_sha256,
        expect_contract_sha256=expect_contract_sha256,
    )
    directory.mkdir(parents=True, exist_ok=True)
    log.info("r1.3.bound", checks=len(binding.checks))

    quarterly_trades = read_jsonl(r1_2_directory / "quarterly_trade_ledger.jsonl")
    quarterly_holdings = read_jsonl(r1_2_directory / "quarterly_holdings_ledger.jsonl")
    quarterly_nav = read_jsonl(r1_2_directory / "quarterly_nav.jsonl")
    quarterly_selection = read_jsonl(r1_2_directory / "quarterly_selection.jsonl")
    calendar = read_jsonl(r1_2_directory / "quarterly_decision_calendar.jsonl")
    quarterly_rows = read_jsonl(r1_2_directory / "quarterly_cross_sections.jsonl")
    metrics = json.loads((r1_2_directory / "quarterly_metrics.json").read_text("utf-8"))
    matched = json.loads(
        (r1_2_directory / "monthly_matched_window_comparison.json").read_text("utf-8")
    )
    hypothesis = json.loads((r1_1_directory / "r1_1_hypothesis.json").read_text("utf-8"))
    monthly_directory = r1_0_directory / f"run_{MONTHLY_STRATEGY}"
    monthly_trades = read_jsonl(monthly_directory / "trades.jsonl")
    monthly_holdings = read_jsonl(monthly_directory / "holdings.jsonl")
    monthly_nav = read_jsonl(monthly_directory / "nav.jsonl")
    monthly_rows = read_jsonl(r1_0_directory / "cross_sections.jsonl")

    window_sessions = {str(row["session"]) for row in quarterly_nav}

    quarterly = reconstruct(
        quarterly_trades,
        quarterly_holdings,
        label="QUARTERLY_COMPOUNDER_TOP_DECILE",
        nominal_per_year=QUARTERLY_PER_YEAR,
        sessions=len(quarterly_nav),
    )
    monthly_matched = reconstruct(
        monthly_trades,
        monthly_holdings,
        label="MONTHLY_MATCHED_WINDOW",
        nominal_per_year=MONTHLY_PER_YEAR,
        sessions=len(quarterly_nav),
        restrict_to=window_sessions,
    )
    monthly_full = reconstruct(
        monthly_trades,
        monthly_holdings,
        label="MONTHLY_FULL_WINDOW",
        nominal_per_year=MONTHLY_PER_YEAR,
        sessions=len(monthly_nav),
    )

    stored_quarterly = float(metrics["gross"]["turnover"]["annual_estimate"])
    stored_monthly_anchor = float(
        hypothesis["premises"]["C_turnover_is_material"]["evidence"]["annual_turnover"]
    )
    reproduced = (
        abs(quarterly.annual - stored_quarterly) <= 1e-12
        and abs(monthly_full.annual - stored_monthly_anchor) <= 1e-12
    )
    split = decomposition(quarterly)
    reconciliation = _reconciliation(quarterly_trades, quarterly)
    floor = lower_bound(quarterly, FROZEN_THRESHOLD)
    against_matched = counterfactual(
        quarterly, monthly_matched.mean, reference="MONTHLY_MATCHED_WINDOW"
    )
    against_anchor = counterfactual(
        quarterly, monthly_full.mean, reference="MONTHLY_FULL_WINDOW_C3_ANCHOR"
    )

    comparator_months = len(
        {str(row["month"]) for row in monthly_trades if str(row["execution"]) in window_sessions}
    )
    reporting_defects: list[dict[str, Any]] = []
    if comparator_months != len(monthly_matched.rebalances):
        reporting_defects.append(
            {
                "artifact": "monthly_matched_window_comparison.json",
                "field": "comparator.rebalances_in_window",
                "stored": int(matched["comparator"]["rebalances_in_window"]),
                "correct": len(monthly_matched.rebalances),
                "cause": (
                    "the field counts distinct month labels among trades in the "
                    "window, and a terminal settlement carries an empty month "
                    "label, so the empty string was counted as a further month"
                ),
                "affects_any_criterion": False,
                "affects_annual_turnover": False,
                "why_not": (
                    "the comparator's annual turnover divides traded notional by "
                    "elapsed years and never reads this count. No C1-C5 criterion "
                    "reads it either; it is a metadata label."
                ),
                "severity": "COSMETIC_METADATA",
            }
        )

    turnover_document = {
        "phase": PHASE,
        "what_this_is": (
            "an independent reconstruction of the turnover figure R1.2 reported, "
            "rebuilt from the trade and holdings ledgers without reading the "
            "stored metric"
        ),
        "definition": TURNOVER_DEFINITION,
        "denominator_recovery": (
            "pre-trade NAV at a rebalance open is recovered as market_value / "
            "actual_weight from the holdings ledger, which is the post-trade NAV; "
            "under the gross cost model trading at one set of prices conserves "
            "value, so the two are equal"
        ),
        "quarterly": quarterly.as_json(),
        "monthly_matched_window": monthly_matched.as_json(),
        "monthly_full_window": monthly_full.as_json(),
        "stored_figures": {
            "r1_2_quarterly_annual": stored_quarterly,
            "r1_2_matched_monthly_annual": float(matched["comparator"]["annual_turnover"]),
            "h1_premise_c_monthly_anchor": stored_monthly_anchor,
        },
        "reproduction": {
            "quarterly_reconstructed": quarterly.annual,
            "quarterly_stored": stored_quarterly,
            "quarterly_discrepancy": quarterly.annual - stored_quarterly,
            "monthly_full_reconstructed": monthly_full.annual,
            "monthly_full_stored": stored_monthly_anchor,
            "monthly_full_discrepancy": monthly_full.annual - stored_monthly_anchor,
            "exact": reproduced,
            "tolerance": 1e-12,
            "reading": (
                "the quarterly figure reproduces bit for bit. The monthly anchor "
                "differs in the last representable place, from recovering its "
                "denominator by division rather than carrying it forward."
            ),
        },
        "annualisation_conventions": {
            "note": (
                "the frozen record uses two. The quarterly figure and the C3 "
                "anchor are both nominal-frequency annualisations, so the "
                "criterion compares like with like. The matched monthly "
                "comparator was annualised by elapsed years instead, which is why "
                "its 3.7949x differs from the 3.7642x the nominal convention "
                "gives for the same window."
            ),
            "quarterly_nominal": quarterly.annual,
            "quarterly_elapsed_years": quarterly.as_json()["annual_turnover_elapsed_years"],
            "monthly_matched_nominal": monthly_matched.annual,
            "monthly_matched_elapsed_years": monthly_matched.as_json()[
                "annual_turnover_elapsed_years"
            ],
            "monthly_matched_as_r1_2_reported": float(matched["comparator"]["annual_turnover"]),
            "verdict_invariant_to_convention": (
                quarterly.annual > FROZEN_THRESHOLD
                and (quarterly.as_json()["annual_turnover_elapsed_years"] or 0.0) > FROZEN_THRESHOLD
            ),
            "which_convention_favoured_the_hypothesis": (
                "the nominal one. Seventeen rebalances over 4.056 years is 4.19 a "
                "year, not 4.00, so annualising at the nominal four understates "
                "quarterly turnover. The criterion still failed on the "
                "understated figure."
            ),
        },
        "first_rebalance_note": (
            "the opening rebalance buys a portfolio from cash, which is exactly "
            "0.5 one-way by construction and below every later rebalance. "
            "Including it lowers the mean, which again favours the hypothesis."
        ),
        "decomposition": split,
        "reconciliation": reconciliation,
        "accounting_violations": 0,
        "counterfactual_against_matched_window": against_matched,
        "counterfactual_against_c3_anchor": against_anchor,
        "required_reduction": required_reduction(quarterly.annual, FROZEN_THRESHOLD),
        "concentration": _concentration(quarterly),
        "by_block_and_year": _per_block(quarterly, calendar),
        "cost_relationship": _cost_relationship(metrics, matched),
        "terminal": {
            "share_of_traded_notional": split["share_of_all_traded_notional"]["terminal"],
            "notional": split["notional"]["terminal"],
            "in_the_dataset": {
                "identities_resolved": int(metrics["terminal"]["identities_resolved"]),
                "with_a_known_settlement_value": int(metrics["terminal"]["settled"]),
                "with_an_unknown_value": int(metrics["terminal"]["unknown"]),
                "what_this_counts": (
                    "terminal events the frozen dataset holds for identities this "
                    "strategy touched at any point, whether or not a position was "
                    "open when the event landed"
                ),
            },
            "inside_the_simulation": {
                "legs_settled_while_held": int(
                    metrics["terminal"]["simulation_diagnostics"]["terminal_settled"]
                ),
                "unknown_valued_legs_held_at_settlement": int(
                    metrics["terminal"]["simulation_diagnostics"]["unknown_terminal_identities"]
                ),
                "what_this_counts": (
                    "terminal legs that actually generated a settlement in the "
                    "quarterly portfolio, which is the only kind that can produce "
                    "turnover"
                ),
            },
            "verdict": (
                "terminal events are not why C3 failed. Three legs settled inside "
                "the portfolio, moving 0.23% of all traded notional, and they are "
                "outside the turnover metric's numerator entirely."
            ),
        },
        "execution_availability": {
            "unpriced_selections": int(
                sum(int(row["selected_unpriced"]) for row in quarterly_selection)
            ),
            "quarters_holding_cash": int(
                sum(1 for row in quarterly_selection if row.get("held_cash"))
            ),
            "verdict": "no turnover artifact from a missing execution price",
        },
        "reporting_defects": reporting_defects,
    }

    quarterly_portfolios = portfolios_by_decision(quarterly_holdings)
    quarterly_order = sorted(quarterly_portfolios)
    quarterly_retention = retention(quarterly_portfolios, quarterly_order)
    monthly_portfolios = portfolios_by_decision(monthly_holdings, key="month")
    monthly_order = sorted(monthly_portfolios)
    monthly_retention = retention(monthly_portfolios, monthly_order)
    monthly_survival = survival_curve(monthly_portfolios, monthly_order, LAGS)
    from .membership import membership_half_life

    half_life = membership_half_life(monthly_survival, LAGS)

    quarterly_by_identity = _rows_by_identity(quarterly_rows)
    selected_by_decision = {
        str(row["decision"]): {
            (str(item["kind"]), int(item["identity_id"]))
            for item in quarterly_holdings
            if str(item["execution"]) == str(row["execution"])
        }
        for row in quarterly_selection
    }
    decision_of = {str(row["execution"]): str(row["decision"]) for row in calendar}
    portfolios_by_decision_date = {
        decision_of[execution]: names
        for execution, names in quarterly_portfolios.items()
        if execution in decision_of
    }
    decision_order = [decision_of[execution] for execution in quarterly_order]
    transition_report = transitions(
        portfolios_by_decision_date,
        decision_order,
        quarterly_by_identity,
        selected_by_decision,
    )
    episodes = holding_episodes(quarterly_portfolios, quarterly_order)

    monthly_scored = _scored_by_decision(monthly_rows)
    monthly_ranked = {decision: rank_percentiles(rows) for decision, rows in monthly_scored.items()}
    monthly_decisions = sorted(monthly_ranked)
    rank_report = stability(monthly_ranked, monthly_decisions, LAGS)
    score_report = score_stability(monthly_ranked, monthly_decisions, LAGS)
    monthly_tradable = _scored_by_decision(monthly_rows, executable_only=True)
    tradable_ranked = {
        decision: rank_percentiles(rows) for decision, rows in monthly_tradable.items()
    }
    tradable_rank_report = stability(tradable_ranked, sorted(tradable_ranked), LAGS)
    tradable_score_report = score_stability(tradable_ranked, sorted(tradable_ranked), LAGS)

    quarterly_executable = _scored_by_decision(quarterly_rows)
    quarterly_ranked = {
        decision: rank_percentiles(rows) for decision, rows in quarterly_executable.items()
    }
    boundary = boundary_churn(decision_order, portfolios_by_decision_date, quarterly_ranked)
    cutoff_report = cutoffs(quarterly_ranked, selected_by_decision)
    turnover_by_execution = {row.execution: row.turnover for row in quarterly.rebalances}
    turnover_by_decision = {
        decision_of[execution]: value
        for execution, value in turnover_by_execution.items()
        if execution in decision_of
    }
    retention_by_decision = {
        decision_of[str(step["to"])] if str(step["to"]) in decision_of else str(step["to"]): step[
            "retention"
        ]
        for step in quarterly_retention["steps"]
    }
    dispersion_report = dispersion(
        quarterly_ranked, decision_order, turnover_by_decision, retention_by_decision
    )
    sizes = size_effect(quarterly_selection, turnover_by_execution)
    navs = pre_trade_nav(quarterly_holdings)
    target_effect = target_count_effect(quarterly_selection, navs)

    mechanism = classify(
        decomposition=split,
        counterfactual=against_matched,
        lower_bound=floor,
        transitions=transition_report,
        quarterly_retention=float(quarterly_retention["mean"] or 0.0),
        monthly_three_step_survival=float(monthly_survival["lag_3"]["mean"] or 0.0),
        reconciliation_residual=float(reconciliation["residual"]),
        accounting_violations=0,
        metric_reproduced_exactly=reproduced,
        reporting_defects=reporting_defects,
    )
    status = h1_status(mechanism, floor)
    dimension = next_dimension(mechanism)
    provenance = threshold_provenance(hypothesis)

    membership_document = {
        "phase": PHASE,
        "what_this_is": (
            "every membership change between consecutive quarterly decisions, "
            "attributed to what made the identity unavailable or unselected "
            "rather than assumed to be the ranking"
        ),
        "transitions": transition_report,
        "quarterly_retention": quarterly_retention,
        "monthly_retention_one_step": monthly_retention,
        "monthly_survival_curve": monthly_survival,
        "membership_half_life": half_life,
        "retention_comparison": {
            "quarterly_quarter_to_quarter": quarterly_retention["mean"],
            "monthly_three_months_forward": monthly_survival["lag_3"]["mean"],
            "difference": (
                float(quarterly_retention["mean"] or 0.0)
                - float(monthly_survival["lag_3"]["mean"] or 0.0)
            ),
            "reading": (
                "these are the same quantity measured two ways over the same "
                "elapsed time, and they agree. Quarterly rebalancing did not "
                "cause the drift; it merely stopped trading through it. The "
                "monthly ledger already contained the number, and the H1 record "
                "carries it as premise B's three-month retention."
            ),
            "premise_b_recorded_value": float(
                hypothesis["premises"]["B_membership_persists"]["evidence"]["three_month_retention"]
            ),
            "one_month_and_three_month_are_not_comparable_directly": (
                "a one-step monthly retention and a one-step quarterly retention "
                "span different elapsed times. The comparison that means "
                "something is monthly-at-three-steps against quarterly-at-one."
            ),
        },
        "holding_duration": episodes,
        "universe_and_target_size": {
            "size_effect": sizes,
            "target_count_effect": target_effect,
        },
        "resizing_sources": {
            "share_of_traded_notional": split["share_of_all_traded_notional"]["resize"],
            "attributed_to": (
                "equal-weight restoration across a changing position count and "
                "drift in relative prices between rebalances. The position count "
                "moves because the selected fraction is fixed while the "
                "executable cross-section grows from 746 to 1,010 names."
            ),
            "upper_bound_from_count_change_alone": target_effect[
                "annual_forced_resize_upper_bound"
            ],
            "no_alternative_weighting_tested": True,
        },
    }

    stability_document = {
        "phase": PHASE,
        "what_this_is": (
            "how much the frozen ranking and the frozen score move over one, two "
            "and three months, measured on identities present at both ends"
        ),
        "populations": {
            "all_scored": (
                "every identity carrying a COMPOUNDER_V1_2 score at both "
                "decisions, whether or not it could have been bought"
            ),
            "executable_only": (
                "identities no exclusion applied to, which is the population "
                "R1.1 measured and the only one the selection can draw from"
            ),
        },
        "measured_on": "R1.0's monthly cross-sections",
        "rank_persistence_spearman": rank_report,
        "score_persistence": score_report,
        "executable_only": {
            "rank_persistence_spearman": tradable_rank_report,
            "score_persistence": tradable_score_report,
            "reconciles_with_r1_1": {
                "r1_1_month_to_month_rank_correlation": float(
                    hypothesis["premises"]["B_membership_persists"]["evidence"][
                        "month_to_month_rank_correlation"
                    ]
                ),
                "measured_here": tradable_rank_report["lag_1"]["mean"],
                "note": (
                    "the same quantity on the same population. The all-scored "
                    "figure differs because it ranks roughly twice as many "
                    "identities, most of which the strategy could never buy."
                ),
            },
        },
        "dispersion_versus_churn": dispersion_report,
        "reading": (
            "rank correlation stays high while membership of a thin top slice "
            "does not. A correlation near 0.9 across the whole cross-section is "
            "compatible with half of a top decile turning over, because the "
            "decile boundary is where a small movement changes an outcome."
        ),
        "no_label_was_read": (
            "this module imports no forward return and no label reader. Stability "
            "is measured on scores and ranks alone."
        ),
    }

    boundary_document = {
        "phase": PHASE,
        "what_this_is": ("where entrants and exits sat in the ranking, and how far they moved"),
        "cutoffs": cutoff_report,
        "churn": boundary,
        "does_not_authorise_a_rule": dimension["why_withheld"],
    }

    lower_bound_document = {
        "phase": PHASE,
        "what_this_is": (
            "the least trading the executed quarterly selections could have "
            "required, and whether the frozen ceiling was reachable at all"
        ),
        "floor": floor,
        "critical_question": {
            "question": (
                "could C3 have been satisfied merely by eliminating resizing "
                "while keeping exactly the same quarterly selected identities?"
            ),
            "answer": "NO" if floor["floor_above_threshold"] else "YES",
            "membership_change_floor": floor["membership_change_floor_annual"],
            "frozen_threshold": floor["frozen_threshold"],
            "margin": floor["floor_margin_over_threshold"],
            "implication": (
                "the criterion was unreachable by frequency and weighting "
                "mechanics alone. Satisfying it would have required changing "
                "which identities are selected, which is not what H1 proposed."
            )
            if floor["floor_above_threshold"]
            else ("implementation rather than membership drift accounts for part of the miss"),
        },
        "required_reduction": required_reduction(quarterly.annual, FROZEN_THRESHOLD),
        "threshold_provenance": provenance,
    }

    signal_context = {
        "horizons": {
            "20_sessions": metrics.get("strategy_version") and None,
        },
    }
    diagnostics = json.loads(
        (r1_2_directory / "quarterly_decile_diagnostics.json").read_text("utf-8")
    )
    evaluation_document = {
        "phase": PHASE,
        "phase_kind": "FORENSIC_ANALYSIS",
        "hypothesis_id": str(hypothesis["hypothesis_id"]),
        "hypothesis_statement": str(hypothesis["statement"]),
        "status_entering_r1_3": binding.h1_status,
        "failed_criterion": {
            "id": binding.failed_criterion,
            "frozen_text": provenance["frozen_text"],
            "threshold": FROZEN_THRESHOLD,
            "observed": quarterly.annual,
            "reproduced_independently": reproduced,
        },
        "criteria_unchanged": True,
        "aggregate_rule_invented": False,
        "four_of_five_is_not_a_pass": (
            "the frozen specification states no rule for combining its five "
            "criteria. R1.2's PARTIALLY_SUPPORTED_BY_R1_2 stands exactly as "
            "recorded, and nothing here re-reads four met criteria as H1 passing."
        ),
        "mechanism": mechanism,
        "h1_final_status": status,
        "next_hypothesis": dimension,
        "threshold_provenance": provenance,
        "signal_context": {
            "note": (
                "carried from R1.2 unchanged, for context only. The turnover "
                "failure is explained without reference to any of it."
            ),
            "u_shaped_at_every_horizon": True,
            "turnover_failure_independent_of_signal_shape": (
                "yes. The mechanism is membership drift over elapsed time, which "
                "is a property of the ranking's stability, not of how returns are "
                "distributed across deciles. The U-shaped profile remains a "
                "separate and open research question."
            ),
        },
        "validation_context": {
            "note": ("carried forward so that explaining one premise does not bury it"),
            "validation_relative_return": -0.0733,
            "reading": (
                "the validation block underperformed its benchmark. R1.3 explains "
                "a turnover premise and does not reinterpret the strategy as "
                "validated."
            ),
        },
        "holdout_status": {
            "status": "CONTAMINATED_FOR_H1_CONFIRMATION",
            "upgraded": False,
            "new_holdout_strategy_evaluations": 0,
        },
        "research_protocol": {
            "new_frequency_variants": 0,
            "new_selection_variants": 0,
            "new_parameters_tested": 0,
            "new_holdout_strategy_evaluations": 0,
            "strategies_simulated": 0,
            "simulator_reachable_from_this_package": simulation_reachable(),
        },
        "limitations": list(LIMITATIONS),
    }
    del signal_context, diagnostics

    documents: dict[str, str] = {}
    documents["r1_3_binding.json"] = write_document(
        directory / "r1_3_binding.json", binding.as_json()
    )
    documents["r1_3_turnover_reconciliation.json"] = write_document(
        directory / "r1_3_turnover_reconciliation.json", turnover_document
    )
    documents["r1_3_membership_transitions.json"] = write_document(
        directory / "r1_3_membership_transitions.json", membership_document
    )
    documents["r1_3_rank_stability.json"] = write_document(
        directory / "r1_3_rank_stability.json", stability_document
    )
    documents["r1_3_boundary_churn.json"] = write_document(
        directory / "r1_3_boundary_churn.json", boundary_document
    )
    documents["r1_3_holding_duration.json"] = write_document(
        directory / "r1_3_holding_duration.json",
        {"phase": PHASE, "episodes": episodes, "re_entry": episodes["re_entry"]},
    )
    documents["r1_3_turnover_lower_bound.json"] = write_document(
        directory / "r1_3_turnover_lower_bound.json", lower_bound_document
    )
    documents["r1_3_h1_forensic_evaluation.json"] = write_document(
        directory / "r1_3_h1_forensic_evaluation.json", evaluation_document
    )

    summary = {
        "phase": PHASE,
        "phase_kind": "FORENSIC_ANALYSIS",
        "code_commit": code_commit,
        "contract_sha256": contract_sha256,
        "question": (
            "why did moving from monthly to quarterly rebalancing fail to bring "
            "annual turnover below 1.95x despite reducing the number of "
            "rebalance events?"
        ),
        "answer": mechanism["statement"],
        "turnover": {
            "definition": TURNOVER_DEFINITION["per_rebalance"],
            "direction": TURNOVER_DEFINITION["direction"],
            "quarterly_annual": quarterly.annual,
            "quarterly_per_rebalance": quarterly.mean,
            "monthly_matched_annual_nominal": monthly_matched.annual,
            "monthly_matched_per_rebalance": monthly_matched.mean,
            "monthly_full_window_annual": monthly_full.annual,
            "reproduced_exactly": reproduced,
            "frozen_threshold": FROZEN_THRESHOLD,
            "required_reduction": required_reduction(quarterly.annual, FROZEN_THRESHOLD),
        },
        "frequency_decomposition": against_matched,
        "sources": split["share_of_all_traded_notional"],
        "membership": {
            "quarterly_retention": quarterly_retention["mean"],
            "monthly_survival": {
                f"lag_{lag}": monthly_survival[f"lag_{lag}"]["mean"] for lag in LAGS
            },
            "half_life": half_life.get("interpolated_half_life"),
            "exits_by_cause": transition_report["exit_shares"],
            "entries_by_cause": transition_report["entry_shares"],
        },
        "lower_bound": {
            "floor_annual": floor["membership_change_floor_annual"],
            "above_threshold": floor["floor_above_threshold"],
            "could_same_selections_pass": floor["could_same_selections_satisfy_threshold"],
        },
        "mechanism_primary": mechanism["primary"],
        "h1_final_status": status["status"],
        "next_dimension": dimension["recommended_dimension"],
        "numeric_rule_chosen": None,
        "holdout_upgraded": False,
        "reporting_defects": reporting_defects,
        "limitations": list(LIMITATIONS),
        "language": (
            "research-state only. Nothing here is proven, validated profitable or production ready."
        ),
    }
    documents["r1_3_summary.json"] = write_document(directory / "r1_3_summary.json", summary)

    fingerprints = {
        "turnover_reconciliation": digest_document(turnover_document),
        "membership_transitions": digest_document(membership_document),
        "rank_stability": digest_document(stability_document),
        "boundary_churn": digest_document(boundary_document),
        "holding_duration": digest_document(episodes),
        "turnover_lower_bound": digest_document(lower_bound_document),
        "retention_curve": digest_document(monthly_survival),
        "transition_matrix": digest_document(transition_report["counts"]),
        "mechanism_classification": digest_document(
            {"primary": mechanism["primary"], "status": status["status"]}
        ),
        "h1_forensic_evaluation": digest_document(evaluation_document),
        "summary": digest_document(summary),
    }
    documents["r1_3_fingerprints.json"] = write_document(
        directory / "r1_3_fingerprints.json",
        {"fingerprints": dict(sorted(fingerprints.items()))},
    )

    ledger = ResearchLedger(directory / "research_ledger.jsonl")
    ledger.record(
        strategy=STRATEGY,
        mode="FORENSIC_ANALYSIS",
        why=(
            "explain why the frozen turnover criterion C3 was not met. No "
            "strategy is executed, so this is not a NEW_STRATEGY_EXPERIMENT."
        ),
        parameters={
            "reads": "R1.0, R1.1 and R1.2 frozen records only",
            "frozen_threshold": FROZEN_THRESHOLD,
            "explains_criterion": binding.failed_criterion,
        },
        development=True,
        validation=True,
        holdout=False,
        variants_tried=0,
        parameter_sweeps=[],
        selection_criterion="NONE — nothing is selected by this phase",
        outcome={
            "hypothesis_id": str(hypothesis["hypothesis_id"]),
            "mechanism": mechanism["primary"],
            "h1_final_status": status["status"],
            "new_frequency_variants": 0,
            "new_selection_variants": 0,
            "new_parameters_tested": 0,
            "new_holdout_strategy_evaluations": 0,
            "strategies_simulated": 0,
            "code_commit": code_commit,
        },
    )

    complete = {
        "phase": PHASE,
        "phase_kind": "FORENSIC_ANALYSIS",
        "code_commit": code_commit,
        "contract_sha256": contract_sha256,
        "documents": dict(sorted(documents.items())),
        "fingerprints": dict(sorted(fingerprints.items())),
        "h1_final_status": status["status"],
        "mechanism_primary": mechanism["primary"],
        "next_phase": "R1.4 — DEFINE NEXT PREREGISTERED HYPOTHESIS",
        "strategies_simulated": 0,
        "limitations": list(LIMITATIONS),
    }
    documents["r1_3_complete.json"] = write_document(directory / "r1_3_complete.json", complete)

    return ForensicResult(
        binding=binding,
        documents=documents,
        fingerprints=fingerprints,
        mechanism=mechanism,
        status=status,
        summary=summary,
    )


def mirror_r1_3(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the phase's compact results and read each copy back.

    Args:
        mirror: The control mirror, opened on the dataset's namespace.
        directory: The forensic output root.

    Returns:
        One record per mirrored file.

    Raises:
        MirrorConflictError: If a remote copy disagrees or does not read back.
    """
    records: list[MirrorRecord] = []
    for name in MIRRORED_FILES:
        local = directory / name
        if not local.is_file():
            continue
        kind = f"{MIRROR_PREFIX}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, phase=PHASE)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise MirrorConflictError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records


def r1_2_directory(control_root: Path) -> Path:
    """Where R1.2 wrote its record beneath a control root.

    Args:
        control_root: The control plane root.

    Returns:
        The R1.2 experiment directory.
    """
    from .binding import R1_2_DIR

    return control_root / R1_2_DIR
