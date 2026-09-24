"""The R1.1 orchestration: bind the R1.0 record, take it apart, decide once.

The order matters here too, though for a different reason than in R1.0. There
the order protected the result from the researcher; here it protects the
decision from the result. The classification rubric and the hypothesis's
falsification criteria are written in `review.py` as code, before the
diagnostics that feed them are computed, and this module only applies them.

No strategy runs. Nothing is executed, nothing is rebalanced, no alternative is
scored and kept. The single output that points forward is a specification for
one experiment, and it refuses to exist unless the hypothesis it tests
survived.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.backtest_harness import (
    DECILES,
    HORIZONS,
    PRIMARY_HORIZON,
    CrossSectionRow,
    load_labels,
)
from stock_screener.backtest_harness.registry import digest_document, write_document
from stock_screener.sec_evidence.mirror import MirrorConflictError
from stock_screener.signal_review.binding import R1_0_DIR, bind_r1_0, read_jsonl
from stock_screener.signal_review.portfolio_review import (
    concentration,
    cost_break_even,
    drawdown_episodes,
    holding_concentration,
    identity_contributions,
    month_contributions,
    relative_path,
    return_distribution,
    rolling_performance,
    turnover_sources,
)
from stock_screener.signal_review.review import (
    classification_limitation,
    classify_signal,
    evaluate_h1,
    experiment_spec,
    holdout_status,
    hypothesis_record,
    sample_attrition,
    score_floor_audit,
)
from stock_screener.signal_review.signal import (
    adv_bucket_diagnostics,
    cross_section_size_effect,
    decile_table,
    horizon_diagnostics,
    leave_one_out,
    monthly_signal,
    rank_persistence,
    score_threshold_stability,
    subset_diagnostics,
)

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "R1_1_FILES",
    "REVIEW_DIR",
    "ReviewResult",
    "mirror_r1_1",
    "review_r1_0",
]

PHASE = "R1.1"
REVIEW_DIR = "research/r1_1"

MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-1"

TOP_DECILE = "COMPOUNDER_TOP_DECILE_V1"
EQUAL_WEIGHT = "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1"
PASSIVE = "BASELINE_PASSIVE_V1"

#: Every file this phase writes, in the order a reader should meet them.
R1_1_FILES = (
    "holdout_status.json",
    "compounder_signal_review.json",
    "compounder_split_diagnostics.json",
    "compounder_year_diagnostics.json",
    "compounder_persistence.json",
    "compounder_concentration.json",
    "compounder_turnover_analysis.json",
    "compounder_cost_break_even.json",
    "sample_attrition.json",
    "r1_1_hypothesis.json",
    "r1_1_summary.json",
    "r1_1_fingerprints.json",
    "r1_1_complete.json",
)

#: Compact metadata only. The monthly ledger is reproducible from the R1.0
#: record and is not mirrored.
MIRRORED_FILES = (
    "holdout_status.json",
    "compounder_signal_review.json",
    "compounder_split_diagnostics.json",
    "compounder_year_diagnostics.json",
    "compounder_persistence.json",
    "compounder_concentration.json",
    "compounder_turnover_analysis.json",
    "compounder_cost_break_even.json",
    "sample_attrition.json",
    "r1_1_hypothesis.json",
    "r1_1_summary.json",
    "r1_1_fingerprints.json",
    "r1_1_complete.json",
)

#: Carried from R1.0 unchanged. A result that looks good does not retire them.
LIMITATIONS = (
    "sector and industry history is unavailable; no result here is attributed to a sector",
    "exchange and listing membership is partly inferred; a MEMBER verdict is INFERRED",
    "market capitalisation is a lower bound where a share count is a cover-page figure",
    "average daily volume is single-venue (IEX) and partial; it is reported, never imposed",
    "historical-security fundamentals carry a conservative filed-date lag",
    "no historical FX series exists, so currency-sensitive ratios are unavailable for foreign "
    "filers at every historical decision date",
    "dividends are inside the vendor's adjusted basis rather than modelled as cash",
    "spinoffs are unsupported",
    "a delisted security whose consideration nobody published carries an UNKNOWN terminal value",
    "there is no intraday model: the only decision points are the close and the next open",
    "the risk-free rate is zero because the frozen dataset holds no risk-free series",
    "COMPOUNDER_V1_2 reads an undated classification internally; see the limitation record",
)


@dataclass
class ReviewResult:
    """Everything one R1.1 execution produced.

    Attributes:
        directory: Where the outputs were written.
        fingerprints: Semantic digests of every deterministic output.
        summary: The summary document.
        marker: The completion marker.
        classification: The signal classification.
        hypothesis: The hypothesis record.
        spec: The R1.2 specification, when one was created.
        notes: Anything a reader must be told.
    """

    directory: Path
    fingerprints: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    marker: dict[str, Any] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)
    hypothesis: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(dict(sorted(row.items())), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return digest_document({"rows": [dict(sorted(row.items())) for row in rows]})


def _final_prices(
    db: sqlite3.Connection, session: str, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], float]:
    """The last session's close for every identity still held at the end."""
    wanted = set(keys)
    out: dict[tuple[str, int], float] = {}
    for kind, query in (
        ("COMPANY", "SELECT company_id, close FROM price_history WHERE date = ?"),
        (
            "HISTORICAL",
            "SELECT historical_security_id, close FROM historical_prices WHERE date = ?",
        ),
    ):
        for identity_id, close in db.execute(query, (session,)):
            key = (kind, int(identity_id))
            if key in wanted and close is not None:
                out[key] = float(close)
    return out


def review_r1_0(
    directory: Path,
    r1_0_directory: Path,
    database: Path,
    *,
    expect_marker_sha: str,
    expect_split_sha: str,
    expect_preregistration_sha: str,
    code_commit: str,
    progress: Any = None,
) -> ReviewResult:
    """Interpret the R1.0 record and decide whether one more experiment follows.

    Args:
        directory: Where to write the R1.1 outputs.
        r1_0_directory: The R1.0 record.
        database: The frozen research database, opened read-only.
        expect_marker_sha: The R1.0 completion marker's digest.
        expect_split_sha: The frozen split's digest.
        expect_preregistration_sha: The R1.0 preregistration's digest.
        code_commit: The commit this interpretation was made at.
        progress: Receives one line per stage.

    Returns:
        The result, its fingerprints and its decision.
    """

    def say(line: str) -> None:
        if progress is not None:
            progress(line)

    directory.mkdir(parents=True, exist_ok=True)
    raw = read_jsonl(r1_0_directory / "cross_sections.jsonl")
    binding = bind_r1_0(
        r1_0_directory,
        expect_marker_sha=expect_marker_sha,
        expect_split_sha=expect_split_sha,
        expect_preregistration_sha=expect_preregistration_sha,
        cross_sections=raw,
    )
    say(f"R1.0 record bound: {len(binding.verified)} fingerprints re-derived and matched")

    rows = [CrossSectionRow(**row) for row in raw]
    executable = [row for row in rows if row.stage == "EXECUTABLE"]
    split_document = json.loads(
        (r1_0_directory / "research_split_v1.json").read_text(encoding="utf-8")
    )
    split_of = {
        str(entry["month"]): str(entry["split"])
        for entry in split_document.get("decision_dates", [])
    }
    decisions = sorted({row.decision for row in executable})
    say(
        f"cross-section loaded: {len(rows)} rows, {len(executable)} executable, "
        f"{len(decisions)} decision dates"
    )

    fingerprints: dict[str, str] = {}
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        labels = load_labels(db, decisions)
        say(f"frozen labels loaded: {len(labels)} label rows")

        monthly = monthly_signal(executable, labels, split_of)
        splits = subset_diagnostics(monthly, key="split")
        years = subset_diagnostics(monthly, key="year")
        leave_month = leave_one_out(monthly)
        leave_year = leave_one_out(monthly, key="year")
        persistence = rank_persistence(executable)
        horizons = horizon_diagnostics(executable, labels, split_of, HORIZONS)
        thresholds = score_threshold_stability(monthly)
        size_effect = cross_section_size_effect(monthly)
        adv = adv_bucket_diagnostics(executable, labels)
        table = decile_table(monthly, executable)
        say(
            f"signal diagnostics: {len(monthly)} months, "
            f"overall IC {splits['ALL']['rank_ic']['mean']}"
        )

        trades = read_jsonl(r1_0_directory / f"run_{TOP_DECILE}" / "trades.jsonl")
        holdings = read_jsonl(r1_0_directory / f"run_{TOP_DECILE}" / "holdings.jsonl")
        nav = read_jsonl(r1_0_directory / f"run_{TOP_DECILE}" / "nav.jsonl")
        equal_nav = read_jsonl(r1_0_directory / f"run_{EQUAL_WEIGHT}" / "nav.jsonl")
        passive_nav = read_jsonl(r1_0_directory / f"run_{PASSIVE}" / "nav.jsonl")
        last_session = str(nav[-1]["session"])
        held = {(str(t["kind"]), int(t["identity_id"])) for t in trades}
        prices = _final_prices(db, last_session, sorted(held))

    contributions = identity_contributions(trades, prices)
    metrics = json.loads(
        (r1_0_directory / f"run_{TOP_DECILE}" / "metrics.json").read_text(encoding="utf-8")
    )
    equal_metrics = json.loads(
        (r1_0_directory / f"run_{EQUAL_WEIGHT}" / "metrics.json").read_text(encoding="utf-8")
    )
    net_metrics = json.loads(
        (r1_0_directory / f"run_{TOP_DECILE}" / "metrics.SIMPLE_10BPS_PER_SIDE_V1.json").read_text(
            encoding="utf-8"
        )
    )
    capital = 100_000.0
    profit = float(nav[-1]["nav"]) - capital
    reconciliation = sum(record.pnl for record in contributions) - profit
    names = concentration(contributions, profit)
    months = month_contributions(nav, capital)
    holding = holding_concentration(holdings)
    turnover = turnover_sources(trades, holdings)
    window = metrics["window"]
    years_elapsed = float(window["sessions"] - 1) / 252.0
    cost = cost_break_even(
        trades,
        nav,
        strategy_total_return=float(window["total_return"]),
        benchmark_total_return=float(metrics["benchmark"]["total_return"]),
        observed_cost_bps=10.0,
        observed_total_return=float(net_metrics["window"]["total_return"]),
        years=years_elapsed,
    )
    relative = relative_path(nav, passive_nav)
    rolling = rolling_performance(nav, passive_nav)
    say(
        f"portfolio forensics: top-5 identities {names['share_of_profit']['top_5']}, "
        f"break-even {cost['break_even_cost_per_side_bps']} bps"
    )

    attrition = sample_attrition(rows)
    limitation = classification_limitation(rows, holdings)
    floor = score_floor_audit(rows)
    by_kind = _kind_contributions(contributions, holdings, profit)
    terminal = {
        TOP_DECILE: _terminal_sensitivity(metrics),
        EQUAL_WEIGHT: _terminal_sensitivity(equal_metrics),
    }

    classification = classify_signal(
        splits, persistence, leave_month, leave_year, names, months, cost
    )
    premises = evaluate_h1(
        horizons,
        persistence,
        float(metrics["turnover"]["annual_estimate"]),
        cost,
        names,
        months,
        leave_month,
        leave_year,
    )
    reconciliation_view = _label_versus_path(splits, metrics, equal_metrics)
    hypothesis = hypothesis_record(
        premises=premises,
        classification=classification,
        binding={
            "marker_sha256": binding.marker_sha256,
            "dataset_id": binding.dataset_id,
            "contract_version": binding.contract_version,
        },
        code_commit=code_commit,
        limitations=LIMITATIONS,
        evidence_against=_evidence_against(splits, years, table, horizons, reconciliation_view),
    )
    spec: dict[str, Any] | None = None
    try:
        spec = experiment_spec(
            hypothesis=hypothesis,
            binding={
                "dataset_id": binding.dataset_id,
                "contract_version": binding.contract_version,
            },
            code_commit=code_commit,
        )
    except ValueError:
        spec = None
    say(f"classification {classification['classification']}; H1 {hypothesis['status']}")

    status = holdout_status()
    review = {
        "phase": PHASE,
        "dataset_id": binding.dataset_id,
        "contract_version": binding.contract_version,
        "primary_horizon": PRIMARY_HORIZON,
        "deciles": DECILES,
        "months": len(monthly),
        "executable_observations": len(executable),
        "decile_table": table,
        "monthly_contrast": splits["ALL"]["d10_minus_d1"],
        "monthly_rank_ic": splits["ALL"]["rank_ic"],
        "horizons": horizons,
        "score_threshold_stability": thresholds,
        "cross_section_size_effect": size_effect,
        "liquidity_buckets": adv,
        "score_change_diagnostic": {
            "status": "SKIPPED",
            "reason": (
                "the frozen system defines no canonical notion of a score change, and inventing "
                "one here would create a construct this phase is not allowed to create"
            ),
        },
        "regime_diagnostic": {
            "status": "REGIME_DIAGNOSTIC_NOT_AVAILABLE",
            "reason": (
                "no frozen point-in-time market regime state exists. MARKET_SIGNALS_V1 carries a "
                "per-security trend direction, not a market state, and deriving one would need "
                "thresholds this phase may not invent"
            ),
        },
        "market_signals_v1": _market_signals_note(),
        "label_versus_path": reconciliation_view,
        "statistical_interpretation": (
            "the monthly D10-D1 and the monthly rank IC both have t-statistics below 2 and "
            "bootstrap intervals that include zero. The economic magnitude, the statistical "
            "uncertainty and the realised portfolio path are three different things and are "
            "reported separately."
        ),
        "limitations": list(LIMITATIONS),
    }
    portfolio = {
        "phase": PHASE,
        "strategy_version": TOP_DECILE,
        "profit": profit,
        "profit_reconciliation_residual": reconciliation,
        "identity_concentration": names,
        "month_concentration": months,
        "holding_concentration": holding,
        "by_identity_class": by_kind,
        "terminal_sensitivity": terminal,
        "relative_path": relative,
        "rolling_performance": rolling,
        "drawdowns": {
            TOP_DECILE: drawdown_episodes(nav),
            PASSIVE: drawdown_episodes(passive_nav),
        },
        "return_distribution": {
            TOP_DECILE: return_distribution(nav),
            EQUAL_WEIGHT: return_distribution(equal_nav),
            PASSIVE: return_distribution(passive_nav),
        },
        "why_the_portfolio_beats_the_ic": _portfolio_versus_ic(
            names, table, metrics, splits, persistence
        ),
        "top_contributors": [record.as_json() for record in contributions[:25]],
        "worst_contributors": [record.as_json() for record in contributions[-25:]],
    }

    fingerprints["holdout_status"] = write_document(directory / "holdout_status.json", status)
    fingerprints["signal_review"] = write_document(
        directory / "compounder_signal_review.json", review
    )
    fingerprints["split_diagnostics"] = write_document(
        directory / "compounder_split_diagnostics.json",
        {"splits": splits, "leave_one_month_out": leave_month, "leave_one_year_out": leave_year},
    )
    fingerprints["year_diagnostics"] = write_document(
        directory / "compounder_year_diagnostics.json", {"years": years}
    )
    fingerprints["persistence"] = write_document(
        directory / "compounder_persistence.json", persistence
    )
    fingerprints["concentration"] = write_document(
        directory / "compounder_concentration.json", portfolio
    )
    fingerprints["turnover_analysis"] = write_document(
        directory / "compounder_turnover_analysis.json", turnover
    )
    fingerprints["cost_break_even"] = write_document(
        directory / "compounder_cost_break_even.json", cost
    )
    fingerprints["sample_attrition"] = write_document(
        directory / "sample_attrition.json",
        {
            "attrition": attrition,
            "current_classification_limitation": limitation,
            "score_floor": floor,
        },
    )
    fingerprints["monthly_signal"] = _jsonl(
        directory / "compounder_monthly_signal.jsonl", [record.as_json() for record in monthly]
    )
    fingerprints["hypothesis"] = write_document(directory / "r1_1_hypothesis.json", hypothesis)
    if spec is not None:
        fingerprints["r1_2_experiment_spec"] = write_document(
            directory / "r1_2_experiment_spec.json", spec
        )

    result = ReviewResult(
        directory=directory,
        classification=classification,
        hypothesis=hypothesis,
        spec=spec,
    )
    result.summary = _summary(
        binding=binding,
        review=review,
        splits=splits,
        years=years,
        persistence=persistence,
        names=names,
        months=months,
        turnover=turnover,
        cost=cost,
        relative=relative,
        attrition=attrition,
        limitation=limitation,
        floor=floor,
        by_kind=by_kind,
        classification=classification,
        hypothesis=hypothesis,
        spec=spec,
        status=status,
        metrics=metrics,
        net_metrics=net_metrics,
        leave_month=leave_month,
        leave_year=leave_year,
    )
    fingerprints["r1_1_summary"] = write_document(directory / "r1_1_summary.json", result.summary)
    result.fingerprints = dict(sorted(fingerprints.items()))
    write_document(directory / "r1_1_fingerprints.json", {"fingerprints": result.fingerprints})
    result.marker = {
        "stage": "R1_1_COMPLETE",
        "phase": PHASE,
        "dataset_id": binding.dataset_id,
        "contract_version": binding.contract_version,
        "contract_sha256": binding.contract_bindings,
        "r1_0_completion_sha256": binding.marker_sha256,
        "r1_0_result_fingerprints": binding.fingerprints,
        "r1_0_fingerprints_reverified": list(binding.verified),
        "split_sha256": binding.split_sha256,
        "preregistration_sha256": binding.preregistration_sha256,
        "diagnostic_fingerprints": result.fingerprints,
        "signal_classification": classification["classification"],
        "hypothesis_status": hypothesis["status"],
        "hypothesis_id": hypothesis["hypothesis_id"],
        "r1_2_spec_sha256": fingerprints.get("r1_2_experiment_spec"),
        "holdout_acknowledgement": status,
        "strategy_executed": False,
        "profitability_is_not_a_gate": True,
    }
    write_document(directory / "r1_1_complete.json", result.marker)
    return result


def _evidence_against(
    splits: dict[str, Any],
    years: dict[str, Any],
    table: dict[str, Any],
    horizons: dict[str, Any],
    reconciliation: dict[str, Any],
) -> list[str]:
    """What the diagnostics say against H1, measured rather than asserted.

    Every value is formatted defensively: a sample too small or too uniform to
    support a statistic yields None, and a phase must report that rather than
    fail on it.
    """
    overall = splits["ALL"]
    holdout = splits.get("HOLDOUT", {})

    def number(value: Any, digits: int = 5) -> str:
        return "not measurable" if value is None else f"{float(value):.{digits}f}"

    def percent(value: Any) -> str:
        return "not measurable" if value is None else f"{float(value):.1%}"

    contrast = overall["d10_minus_d1"]
    coefficient = overall["rank_ic"]
    return [
        f"the monthly D10-D1 has a t-statistic of {number(contrast['t_stat'], 2)} and a "
        f"bootstrap interval of [{number(contrast['ci_low'])}, {number(contrast['ci_high'])}], "
        "which includes zero"
        if (contrast["ci_low"] or 0.0) <= 0 <= (contrast["ci_high"] or 0.0)
        else f"the monthly D10-D1 has a t-statistic of {number(contrast['t_stat'], 2)}",
        f"the mean rank IC is {number(coefficient['mean'], 4)} with a t-statistic of "
        f"{number(coefficient['t_stat'], 2)}",
        f"in the interval R1.0 read last, D10-D1 is "
        f"{number(holdout.get('d10_minus_d1', {}).get('mean'))} with "
        f"{percent(holdout.get('d10_minus_d1', {}).get('positive_share'))} of months positive, "
        f"and the rank IC is {number(holdout.get('rank_ic', {}).get('mean'), 4)}",
        f"the decile profile shows {table['monotone_steps_up']} of {table['steps']} steps "
        f"rising, and in the R1.0 holdout its shape is "
        f"{holdout.get('shape', {}).get('classification', 'unknown')}",
        _weakest_year(years),
        "the 60- and 120-session statistics that motivate a medium-horizon reading rest on "
        f"windows that overlap about {horizons['60']['months_overlapped']} and "
        f"{horizons['120']['months_overlapped']} later monthly decisions, so those months are "
        "not independent observations",
        "the hypothesis was formed after the interval R1.0 read last had already been read, so "
        "that interval cannot confirm it",
        reconciliation["caution"],
    ]


def _weakest_year(years: dict[str, Any]) -> str:
    """Name the worst calendar year outright, rather than letting a block hide it."""
    scored = [
        (name, body)
        for name, body in years.items()
        if name != "ALL" and body["d10_minus_d1"]["mean"] is not None
    ]
    if not scored:
        return "no calendar year has a measurable contrast"
    name, body = min(scored, key=lambda item: float(item[1]["d10_minus_d1"]["mean"]))
    return (
        f"the weakest calendar year is {name}, where D10-D1 averages "
        f"{float(body['d10_minus_d1']['mean']):.5f} and the rank IC "
        f"{float(body['rank_ic']['mean']):.4f} over {body['months']} months, with "
        f"{body['rank_ic']['positive']} of them showing a positive IC"
    )


def _label_versus_path(
    splits: dict[str, Any], metrics: dict[str, Any], equal_metrics: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile the cross-sectional view with the realised portfolio path.

    The two disagree in the R1.0 holdout, and the disagreement is the most
    informative thing in this phase: `D10 - D1` is near zero there while the
    top-decile portfolio beat the equally weighted universe by a wide margin.
    The decile profile explains it — both tails outperformed the middle, so the
    long-short contrast cancels while the long-only top decile stays well above
    the cross-section, which is the quantity the portfolio is actually exposed
    to.

    Args:
        splits: The split-level diagnostics.
        metrics: The top-decile portfolio's R1.0 metrics.
        equal_metrics: The equal-weight baseline's R1.0 metrics.

    Returns:
        A per-block comparison and the reading it supports.
    """
    blocks: dict[str, Any] = {}
    for block in ("DEVELOPMENT", "VALIDATION", "HOLDOUT"):
        body = splits.get(block, {})
        blocks[block] = {
            "label_view": {
                "d10_minus_d1_pooled": body.get("d10_minus_d1_pooled"),
                "d10_minus_cross_section_mean_pooled": body.get(
                    "d10_minus_cross_section_mean_pooled"
                ),
                "cross_section_mean": body.get("cross_section_mean"),
                "decile_profile": body.get("decile_profile"),
                "shape": body.get("shape", {}).get("classification"),
            },
            "path_view": {
                "top_decile_total_return": metrics["splits"][block]["strategy"]["total_return"],
                "equal_weight_total_return": equal_metrics["splits"][block]["strategy"][
                    "total_return"
                ],
                "benchmark_total_return": metrics["splits"][block]["benchmark"]["total_return"],
            },
        }
    return {
        "question": (
            "why does the top-decile portfolio beat the equally weighted universe in the R1.0 "
            "holdout when D10-D1 is essentially zero there?"
        ),
        "by_block": blocks,
        "reading": (
            "D10-D1 is a long-short statistic and the strategy is long-only. Where the decile "
            "profile is U-shaped — as it is in the R1.0 holdout, with both tails above the "
            "middle — the long-short contrast cancels while D10 stays above the cross-section "
            "mean, and it is that second quantity the long-only portfolio earns. Both measures "
            "are reported for every block, alongside the full ten-decile profile, so neither "
            "can be read on its own."
        ),
        "caution": (
            "this reconciles the two views; it does not make the holdout evidence strong. "
            "D10 - cross-section mean in the holdout is smaller than in either earlier block, "
            "and the profile's shape there is itself a finding R1.2 should expect to meet again."
        ),
    }


def _terminal_sensitivity(metrics: dict[str, Any]) -> dict[str, Any]:
    """Reproduce R1.0's unknown-terminal floor, in percentage points."""
    window = metrics["window"]
    floor = metrics["floor_window"]
    return {
        "total_return": window["total_return"],
        "floor_total_return": floor["total_return"],
        "difference_pp": (
            (float(window["total_return"]) - float(floor["total_return"])) * 100.0
            if window["total_return"] is not None and floor["total_return"] is not None
            else None
        ),
        "unknown_terminal_identities": metrics["diagnostics"].get("unknown_terminal_identities", 0),
        "settled_identities": metrics["diagnostics"].get("settled_identities", 0),
    }


def _kind_contributions(
    contributions: list[Any], holdings: list[dict[str, Any]], profit: float
) -> dict[str, Any]:
    """Split the profit and the participation between the two identity classes."""
    out: dict[str, Any] = {}
    positions: dict[str, int] = {}
    for row in holdings:
        positions[str(row["kind"])] = positions.get(str(row["kind"]), 0) + 1
    for kind in ("COMPANY", "HISTORICAL"):
        members = [record for record in contributions if record.kind == kind]
        total = sum(record.pnl for record in members)
        out[kind] = {
            "identities": len(members),
            "position_months": positions.get(kind, 0),
            "profit": total,
            "share_of_profit": total / profit if profit else None,
            "winners": sum(1 for record in members if record.pnl > 0),
            "losers": sum(1 for record in members if record.pnl < 0),
        }
    return out


def _portfolio_versus_ic(
    names: dict[str, Any],
    table: dict[str, Any],
    metrics: dict[str, Any],
    splits: dict[str, Any],
    persistence: dict[str, Any],
) -> dict[str, Any]:
    """Descriptive reasons a modest rank correlation can still build a path."""
    return {
        "question": (
            "why does a mean monthly rank IC of about +0.025 accompany a benchmark-relative "
            "total return of about +50 pp?"
        ),
        "observations": {
            "the_strategy_uses_only_the_top_tenth": (
                "rank IC is measured across the whole cross-section, where the middle deciles "
                "are flat; the portfolio takes only D10, whose mean 20-session return is "
                f"{table['deciles'][str(DECILES)]['mean_return']} against "
                f"{table['deciles']['1']['mean_return']} in D1"
            ),
            "returns_compound": (
                "a small mean monthly edge compounds over 51 rebalances; the arithmetic sum of "
                "monthly edges is not the geometric path"
            ),
            "gains_are_asymmetric": {
                "winners": names["winners"],
                "losers": names["losers"],
                "win_rate": names["win_rate"],
                "gross_gains": names["gross_gains"],
                "gross_losses": names["gross_losses"],
            },
            "holdings_overlap_between_months": {
                "one_month_retention": persistence["top_decile_retention_1m"]["mean"],
                "effect": (
                    "a name retained across months is held continuously, so its full multi-month "
                    "move enters the path once rather than being re-measured each month"
                ),
            },
            "ic_is_positive_more_often_than_it_is_large": {
                "positive_ic_month_share": splits["ALL"]["rank_ic"]["positive_share"],
                "mean_ic": splits["ALL"]["rank_ic"]["mean"],
            },
        },
        "caution": (
            "these are descriptions of the realised ledger, not a decomposition. None of them "
            "establishes that the edge is repeatable, and the same asymmetry that builds the "
            "path also makes it dependent on a minority of names — which the concentration "
            "analysis quantifies rather than assumes away."
        ),
        "realised_turnover_annual": metrics["turnover"]["annual_estimate"],
    }


def _market_signals_note() -> dict[str, Any]:
    """What MARKET_SIGNALS_V1 emits, and the questions that remain open about it."""
    return {
        "status": "MARKET_SIGNALS_V1_REQUIRES_R1_1_HYPOTHESIS_DEFINITION",
        "canonical_standalone_ranking": "NO_CANONICAL_STANDALONE_RANKING",
        "performance_tested_in_r1_1": False,
        "combined_with_compounder": False,
        "frozen_output_semantics": {
            "setups": "multi-label: MOMENTUM, MEAN_REVERSION, COMPRESSION, NONE — a security "
            "may carry several at once, and NONE is a common, valid answer",
            "tradability": "three states: TRADABLE, UNTRADABLE, UNVERIFIED. UNVERIFIED is not "
            "a synonym for bad, and R1.0 already uses TRADABLE as its readiness rule",
            "trend_direction": "UP, DOWN, FLAT, UNKNOWN, reported separately from the fit's "
            "quality (trend_r2)",
            "percentiles": "cross-sectional, None below MIN_PEER_SAMPLE of 30 peers",
            "liquidity_floor_proven": "a boolean that TRADABLE already implies",
            "versions": "MARKET_SIGNALS_V1 is current; MARKET_SIGNALS_V2 exists unpromoted and "
            "differs only in the peer population a percentile is measured against",
        },
        "candidate_future_questions": [
            "does conditioning the frozen Compounder selection on a single named setup change "
            "the decile separation, measured as a diagnostic before any portfolio is built?",
            "does the UNVERIFIED tradability state hide a population whose exclusion is "
            "systematically related to the outcome?",
            "does the corrected peer policy in MARKET_SIGNALS_V2 change any percentile a "
            "research rule would read, and would promoting it be a versioned decision?",
        ],
        "not_done_here": "no scalarisation, no combination, no performance test",
    }


def _summary(**parts: Any) -> dict[str, Any]:
    """The one document a reader should be able to read on its own."""
    binding = parts["binding"]
    splits = parts["splits"]
    metrics = parts["metrics"]
    return {
        "phase": PHASE,
        "dataset_id": binding.dataset_id,
        "contract_version": binding.contract_version,
        "r1_0_completion_sha256": binding.marker_sha256,
        "holdout_status": parts["status"],
        "headline": {
            "top_decile_total_return": metrics["window"]["total_return"],
            "top_decile_cagr": metrics["window"]["cagr"],
            "benchmark_cagr": metrics["benchmark"]["cagr"],
            "benchmark_relative_total_return": metrics["benchmark_relative_return"],
            "net_10bps_total_return": parts["net_metrics"]["window"]["total_return"],
            "annual_turnover": metrics["turnover"]["annual_estimate"],
            "max_drawdown": metrics["window"]["max_drawdown"],
        },
        "cross_sectional_signal": {
            "d10_minus_d1": splits["ALL"]["d10_minus_d1"],
            "rank_ic": splits["ALL"]["rank_ic"],
            "decile_table": parts["review"]["decile_table"],
        },
        "splits": {
            block: {
                "d10_minus_d1_mean": splits.get(block, {}).get("d10_minus_d1", {}).get("mean"),
                "rank_ic_mean": splits.get(block, {}).get("rank_ic", {}).get("mean"),
                "months": splits.get(block, {}).get("months"),
            }
            for block in ("DEVELOPMENT", "VALIDATION", "HOLDOUT")
        },
        "years": {
            year: {
                "d10_minus_d1_mean": body["d10_minus_d1"]["mean"],
                "rank_ic_mean": body["rank_ic"]["mean"],
                "months": body["months"],
                "positive_ic_months": body["rank_ic"]["positive"],
            }
            for year, body in sorted(parts["years"].items())
            if year != "ALL"
        },
        "horizons": {
            horizon: {
                "d10_minus_d1_mean": body["d10_minus_d1"]["mean"],
                "rank_ic_mean": body["rank_ic"]["mean"],
                "independence": body["independence"],
            }
            for horizon, body in sorted(
                parts["review"]["horizons"].items(), key=lambda i: int(i[0])
            )
        },
        "persistence": {
            "month_to_month_rank_correlation": parts["persistence"]["rank_correlation"]["mean"],
            "top_decile_retention_1m": parts["persistence"]["top_decile_retention_1m"]["mean"],
            "top_decile_retention_3m": parts["persistence"]["top_decile_retention_3m"]["mean"],
        },
        "concentration": {
            "top_1_identity": parts["names"]["share_of_profit"]["top_1"],
            "top_5_identities": parts["names"]["share_of_profit"]["top_5"],
            "top_10_identities": parts["names"]["share_of_profit"]["top_10"],
            "top_25_identities": parts["names"]["share_of_profit"]["top_25"],
            "top_50_identities": parts["names"]["share_of_profit"]["top_50"],
            "best_month": parts["months"]["share_of_profit"]["top_1"],
            "best_3_months": parts["months"]["share_of_profit"]["top_3"],
            "best_5_months": parts["months"]["share_of_profit"]["top_5"],
        },
        "leave_one_out": {
            "months": parts["leave_month"],
            "years": parts["leave_year"],
        },
        "turnover": {
            "annual": metrics["turnover"]["annual_estimate"],
            "share_by_cause": parts["turnover"]["share_by_cause"],
            "ranking_churn_share": parts["turnover"]["ranking_churn_share"],
        },
        "cost": parts["cost"],
        "relative_path": parts["relative"],
        "sample": {
            "universe_observations": parts["attrition"]["universe_observations"],
            "executable_share": parts["attrition"]["executable_share"],
            "population": parts["attrition"]["executable_population"],
        },
        "identity_classes": parts["by_kind"],
        "current_classification_limitation": parts["limitation"],
        "score_floor": {
            "at_exact_zero": parts["floor"]["at_exact_zero"],
            "distinct_identities": parts["floor"]["distinct_identities"],
            "distinct_months": parts["floor"]["distinct_months"],
        },
        "classification": parts["classification"],
        "hypothesis": {
            "id": parts["hypothesis"]["hypothesis_id"],
            "status": parts["hypothesis"]["status"],
            "premise_verdicts": parts["hypothesis"]["premise_verdicts"],
        },
        "r1_2": (
            {
                "strategy_version": parts["spec"]["strategy_version"],
                "rules": parts["spec"]["rules"],
                "executed_in_r1_1": False,
            }
            if parts["spec"] is not None
            else None
        ),
        "limitations": list(LIMITATIONS),
    }


def mirror_r1_1(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the phase's compact metadata and read each copy back.

    Args:
        mirror: The control mirror, opened on the dataset's namespace.
        directory: The research output root.

    Returns:
        One record per mirrored file.

    Raises:
        MirrorConflictError: If a remote copy disagrees, or one does not read
            back as it was written.
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


def r1_0_directory(control_root: Path) -> Path:
    """Where R1.0 wrote its record beneath a control root."""
    return control_root / R1_0_DIR
