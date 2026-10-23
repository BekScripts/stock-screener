"""R1.2 — execute the one experiment R1.1 froze, and evaluate it against its own criteria.

The order is short because most of the work was already done and frozen. The
quarterly decision dates are a subset of R1.0's monthly ones, so the
cross-sections are **sliced from R1.0's ledger rather than recomputed**: same
identities, same scores, same tradability verdicts, same execution prices. The
only thing that differs between this run and R1.0's top-decile baseline is how
often the portfolio rebalances, which is precisely what H1 is about.

The monthly comparator is not a new strategy. It is R1.0's frozen NAV and trade
ledgers sliced to the quarterly strategy's exact sessions, so the two paths are
measured over the same days against the same benchmark window.

Nothing here is searched. One frequency, one selection fraction, one weighting,
one execution rule, one cost level — each asserted against the frozen
specification before the first trade is simulated.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from domain.scores import COMPOUNDER_V1_2
from stock_screener.backtest_harness import (
    DECILES,
    GROSS,
    SHARE_POLICY,
    SIMPLE_10BPS_PER_SIDE,
    STARTING_CAPITAL,
    TERMINAL_LEG_POLICY,
    CrossSectionRow,
    PriceBook,
    Rebalance,
    SimulationResult,
    assign_deciles,
    benchmark_path,
    load_labels,
    performance,
    simulate,
    terminal_legs,
    window,
)
from stock_screener.backtest_harness.portfolio import MIN_CROSS_SECTION, TerminalState
from stock_screener.backtest_harness.registry import (
    ResearchLedger,
    digest_document,
    run_manifest,
    validate_manifest,
    write_document,
)
from stock_screener.quarterly_experiment.binding import (
    MONTHLY_TURNOVER_REFERENCE,
    ExperimentBinding,
)
from stock_screener.quarterly_experiment.calendar import (
    QuarterEnd,
    quarterly_calendar,
    quarters_by_block,
    sessions_between,
    terminal_session,
)
from stock_screener.quarterly_experiment.evaluate import evaluate_criteria, interpret
from stock_screener.sec_evidence.mirror import MirrorConflictError
from stock_screener.signal_review import (
    classification_limitation,
    concentration,
    cost_break_even,
    identity_contributions,
    monthly_signal,
    relative_path,
    return_distribution,
    rolling_performance,
    sample_attrition,
    subset_diagnostics,
    turnover_sources,
)
from stock_screener.signal_review.binding import read_jsonl

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "EXPERIMENT_DIR",
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "STRATEGY",
    "ExperimentResult",
    "mirror_r1_2",
    "run_experiment",
]

PHASE = "R1.2"
EXPERIMENT_DIR = "research/r1_2"
STRATEGY = "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"
COMPARATOR = "R1_0_MONTHLY_TOP_DECILE_MATCHED_WINDOW"
MONTHLY_STRATEGY = "COMPOUNDER_TOP_DECILE_V1"

MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-2"

#: The primary horizon H1 froze before this experiment ran.
PRIMARY_HORIZON = 60
REPORTED_HORIZONS = (20, 60, 120)

#: Sessions in a quarter, near enough, for the overlap warning.
_SESSIONS_PER_QUARTER = 63

MIRRORED_FILES = (
    "r1_2_experiment_binding.json",
    "r1_2_run_manifest.json",
    "quarterly_decile_diagnostics.json",
    "quarterly_metrics.json",
    "monthly_matched_window_comparison.json",
    "r1_2_hypothesis_evaluation.json",
    "r1_2_summary.json",
    "r1_2_fingerprints.json",
    "r1_2_complete.json",
)

#: Carried unchanged. A good result does not retire them.
LIMITATIONS = (
    "COMPOUNDER_V1_2 reads an undated classification internally; the exposure is quantified "
    "per run and remains material and unresolved",
    "sector and industry history is unavailable; no result is attributed to a sector",
    "exchange and listing membership is partly inferred",
    "market capitalisation is a lower bound where a share count is a cover-page figure",
    "average daily volume is single-venue (IEX) and partial",
    "historical-security fundamentals carry a conservative filed-date lag",
    "no historical FX series exists, so currency-sensitive ratios are unavailable for foreign "
    "filers at every historical decision date",
    "dividends are inside the vendor's adjusted basis rather than modelled as cash",
    "spinoffs are unsupported",
    "an unknown terminal value is carried at the last close and reported beside a floor",
    "there is no intraday model: the only decision points are the close and the next open",
    "the risk-free rate is zero because the frozen dataset holds no risk-free series",
    "seventeen quarterly cross-sections is a small sample; no statistical power is claimed",
)


@dataclass
class ExperimentResult:
    """Everything one R1.2 execution produced.

    Attributes:
        directory: Where the outputs were written.
        fingerprints: Semantic digests of every deterministic output.
        summary: The summary document.
        marker: The completion marker.
        evaluation: The H1 evaluation.
        manifest: The run manifest.
        manifest_problems: Schema failures. Empty is required.
        violations: Accounting invariants that failed. Empty is required.
    """

    directory: Path
    fingerprints: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    marker: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_problems: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(dict(sorted(row.items())), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return digest_document({"rows": [dict(sorted(row.items())) for row in rows]})


def _split_windows(
    rebalances: list[Rebalance], sessions: list[date]
) -> dict[str, tuple[date, date]]:
    """Each block's valuation window, the same construction R1.0 used."""
    ordered = sorted(rebalances, key=lambda r: r.execution)
    bounds: dict[str, tuple[date, date]] = {}
    for name in ("DEVELOPMENT", "VALIDATION", "HOLDOUT"):
        block = [r for r in ordered if r.split == name]
        if not block:
            continue
        start = block[0].execution
        later = [r for r in ordered if r.execution > block[-1].execution]
        if later:
            earlier = [session for session in sessions if session < later[0].execution]
            end = earlier[-1] if earlier else start
        else:
            end = sessions[-1]
        bounds[name] = (start, end)
    return bounds


def _year_windows(sessions: list[date]) -> dict[str, tuple[date, date]]:
    years: dict[str, list[date]] = {}
    for session in sessions:
        years.setdefault(str(session.year), []).append(session)
    return {year: (dates[0], dates[-1]) for year, dates in years.items()}


def _partial(year: int, sessions: list[date]) -> bool:
    if not sessions:
        return True
    return sessions[0] > date(year, 1, 1) or sessions[-1] < date(year, 12, 31)


def _portfolio_metrics(
    simulation: SimulationResult,
    benchmark: list[float],
    rebalances: list[Rebalance],
    *,
    rebalances_per_year: float,
) -> dict[str, Any]:
    """The R1.0 metric set, computed the R1.0 way, for a path of any frequency."""
    overall = performance(simulation.sessions, simulation.nav)
    bench = performance(simulation.sessions, list(benchmark))
    holdings = simulation.holdings_count or [0]
    utilisation = [
        1.0 - (cash / nav if nav > 0 else 0.0)
        for cash, nav in zip(simulation.cash, simulation.nav, strict=True)
    ]
    turnover = sorted(simulation.turnover)
    splits: dict[str, Any] = {}
    years: dict[str, Any] = {}
    for name, (first, last) in _split_windows(rebalances, simulation.sessions).items():
        block_sessions, nav = window(simulation.sessions, simulation.nav, first, last)
        _, bench_nav = window(simulation.sessions, list(benchmark), first, last)
        strategy = performance(block_sessions, nav)
        reference = performance(block_sessions, bench_nav)
        splits[name] = {
            "strategy": strategy.as_json(),
            "benchmark": reference.as_json(),
            "benchmark_relative_return": (
                None
                if strategy.total_return is None or reference.total_return is None
                else strategy.total_return - reference.total_return
            ),
            "rebalances": sum(1 for r in rebalances if r.split == name),
        }
    for year, (first, last) in sorted(_year_windows(simulation.sessions).items()):
        block_sessions, nav = window(simulation.sessions, simulation.nav, first, last)
        _, bench_nav = window(simulation.sessions, list(benchmark), first, last)
        years[year] = {
            "strategy": performance(block_sessions, nav).as_json(),
            "benchmark": performance(block_sessions, bench_nav).as_json(),
            "partial": _partial(int(year), simulation.sessions),
        }
    return {
        "window": overall.as_json(),
        "floor_window": performance(simulation.sessions, simulation.floor_nav).as_json(),
        "benchmark": bench.as_json(),
        "benchmark_relative_return": (
            None
            if overall.total_return is None or bench.total_return is None
            else overall.total_return - bench.total_return
        ),
        "tracking_difference": (
            None if overall.cagr is None or bench.cagr is None else overall.cagr - bench.cagr
        ),
        "rebalances": simulation.rebalances,
        "average_holdings": sum(holdings) / len(holdings),
        "minimum_holdings": min(holdings),
        "maximum_holdings": max(holdings),
        "cash_utilisation": sum(utilisation) / len(utilisation) if utilisation else None,
        "turnover": {
            "per_rebalance_average": sum(turnover) / len(turnover) if turnover else None,
            "per_rebalance_median": turnover[len(turnover) // 2] if turnover else None,
            "per_rebalance_p90": turnover[int(0.9 * (len(turnover) - 1))] if turnover else None,
            "per_rebalance_maximum": max(turnover) if turnover else None,
            "annual_estimate": (
                sum(turnover) / len(turnover) * rebalances_per_year if turnover else None
            ),
            "rebalances_per_year": rebalances_per_year,
        },
        "share_policy": SHARE_POLICY,
        "diagnostics": simulation.diagnostics,
        "splits": splits,
        "years": years,
    }


def _matched_monthly(
    r1_0_directory: Path, sessions: list[date], benchmark: list[float]
) -> dict[str, Any]:
    """R1.0's frozen monthly top-decile path, sliced to the quarterly window.

    Nothing is re-simulated and no trade is altered. The frozen NAV ledger is
    restricted to the quarterly strategy's own sessions, and turnover and cost
    drag are recomputed from the frozen trade ledger over the same interval, so
    the comparison answers one question only: what changed when the rebalance
    frequency changed?
    """
    nav_rows = read_jsonl(r1_0_directory / f"run_{MONTHLY_STRATEGY}" / "nav.jsonl")
    net_rows = read_jsonl(
        r1_0_directory / f"run_{MONTHLY_STRATEGY}" / "nav.SIMPLE_10BPS_PER_SIDE_V1.jsonl"
    )
    trades = read_jsonl(r1_0_directory / f"run_{MONTHLY_STRATEGY}" / "trades.jsonl")
    wanted = {session.isoformat() for session in sessions}

    def slice_path(rows: list[dict[str, Any]]) -> tuple[list[date], list[float]]:
        kept = [row for row in rows if str(row["session"]) in wanted]
        return (
            [date.fromisoformat(str(row["session"])) for row in kept],
            [float(row["nav"]) for row in kept],
        )

    gross_sessions, gross_nav = slice_path(nav_rows)
    _, net_nav = slice_path(net_rows)
    gross = performance(gross_sessions, gross_nav)
    net = performance(gross_sessions, net_nav)
    bench = performance(sessions, list(benchmark))

    value_at = {str(row["session"]): float(row["nav"]) for row in nav_rows}
    inside = [trade for trade in trades if str(trade["execution"]) in wanted]
    exposure = 0.0
    for trade in inside:
        portfolio = value_at.get(str(trade["execution"]))
        if portfolio and portfolio > 0:
            exposure += float(trade["notional"]) / portfolio
    months = len({str(trade["month"]) for trade in inside})
    years_elapsed = max(len(sessions) - 1, 1) / 252.0
    holdings = read_jsonl(r1_0_directory / f"run_{MONTHLY_STRATEGY}" / "holdings.jsonl")
    inside_holdings = [row for row in holdings if str(row["execution"]) in wanted]
    per_month: dict[str, int] = {}
    for row in inside_holdings:
        per_month[str(row["month"])] = per_month.get(str(row["month"]), 0) + 1
    return {
        "comparator_id": COMPARATOR,
        "what_this_is": (
            "R1.0's frozen COMPOUNDER_TOP_DECILE_V1 ledgers sliced to the quarterly strategy's "
            "sessions. Not a new strategy, not a re-simulation, and not a second monthly "
            "experiment."
        ),
        "source_strategy_version": MONTHLY_STRATEGY,
        "sessions": len(gross_sessions),
        "rebalances_in_window": months,
        "gross": gross.as_json(),
        "net_10bps": net.as_json(),
        "benchmark": bench.as_json(),
        "benchmark_relative_return": (
            None
            if gross.total_return is None or bench.total_return is None
            else gross.total_return - bench.total_return
        ),
        "cost_drag_10bps": (
            None
            if gross.total_return is None or net.total_return is None
            else gross.total_return - net.total_return
        ),
        "traded_notional_over_nav": exposure,
        "annualised_traded_notional_over_nav": exposure / years_elapsed,
        "annual_turnover": (exposure / 2.0) / years_elapsed,
        "average_holdings": (sum(per_month.values()) / len(per_month) if per_month else None),
    }


def run_experiment(
    directory: Path,
    r1_0_directory: Path,
    database: Path,
    binding: ExperimentBinding,
    contract_dir: Path,
    *,
    contract_sha256: str,
    code_commit: str,
    progress: Any = None,
) -> ExperimentResult:
    """Execute the frozen quarterly experiment and evaluate H1 against it.

    Args:
        directory: Where to write the R1.2 outputs.
        r1_0_directory: R1.0's frozen record, the source of the cross-sections
            and of the matched-window comparator.
        database: The frozen research database, opened read-only.
        binding: The proven experiment binding.
        contract_dir: Where the frozen contract and its schema live.
        contract_sha256: The contract file's digest.
        code_commit: The commit this run was executed at.
        progress: Receives one line per stage.

    Returns:
        The result, its fingerprints and the H1 evaluation.
    """

    def say(line: str) -> None:
        if progress is not None:
            progress(line)

    directory.mkdir(parents=True, exist_ok=True)
    result = ExperimentResult(directory=directory)
    say(
        f"experiment bound: {binding.strategy_version} spec {binding.spec_sha256[:16]}… "
        f"hypothesis {binding.hypothesis_sha256[:16]}…"
    )

    split_document = json.loads(
        (r1_0_directory / "research_split_v1.json").read_text(encoding="utf-8")
    )
    monthly_blocks = {
        str(entry["decision"]): str(entry["split"])
        for entry in split_document.get("decision_dates", [])
    }
    raw = read_jsonl(r1_0_directory / "cross_sections.jsonl")
    rows = [CrossSectionRow(**row) for row in raw]
    say(f"R1.0 cross-sections loaded and bound: {len(rows)} rows")

    fingerprints: dict[str, str] = {}
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        calendar = quarterly_calendar(db, monthly_blocks)
        decisions = {entry.decision.isoformat() for entry in calendar}
        quarterly_rows = [row for row in rows if row.decision in decisions]
        executable = [row for row in quarterly_rows if row.stage == "EXECUTABLE"]
        say(
            f"quarterly calendar: {len(calendar)} quarter ends "
            f"{calendar[0].decision} … {calendar[-1].decision}; "
            f"{len(quarterly_rows)} universe rows, {len(executable)} executable"
        )

        r1_0_nav = read_jsonl(r1_0_directory / f"run_{MONTHLY_STRATEGY}" / "nav.jsonl")
        r1_0_last = date.fromisoformat(str(r1_0_nav[-1]["session"]))
        final = terminal_session(db, calendar[-1].decision, r1_0_last)
        sessions = sessions_between(db, calendar[0].execution, final)
        say(f"valuation window {sessions[0]} … {sessions[-1]} ({len(sessions)} sessions)")

        by_quarter: dict[str, list[CrossSectionRow]] = {}
        for row in executable:
            by_quarter.setdefault(row.decision, []).append(row)
        rebalances: list[Rebalance] = []
        selection: list[dict[str, Any]] = []
        for entry in calendar:
            pool = by_quarter.get(entry.decision.isoformat(), [])
            chosen: list[CrossSectionRow] = []
            held_cash: str | None = None
            if len(pool) < MIN_CROSS_SECTION:
                held_cash = "BELOW_MINIMUM_CROSS_SECTION"
            else:
                chosen = [row for row, bucket in assign_deciles(pool) if bucket == DECILES]
                if not chosen:
                    held_cash = "EMPTY_SELECTION"
            selection.append(
                {
                    "quarter": entry.quarter,
                    "month": entry.month,
                    "decision": entry.decision.isoformat(),
                    "execution": entry.execution.isoformat(),
                    "split": entry.split,
                    "universe": sum(
                        1 for row in quarterly_rows if row.decision == entry.decision.isoformat()
                    ),
                    "eligible": len(pool),
                    "eligible_historical": sum(1 for row in pool if row.kind == "HISTORICAL"),
                    "selected": len(chosen),
                    "selected_historical": sum(1 for row in chosen if row.kind == "HISTORICAL"),
                    "selected_unpriced": sum(1 for row in chosen if row.execution_price is None),
                    "held_cash": held_cash,
                }
            )
            if chosen:
                weight = 1.0 / len(chosen)
                rebalances.append(
                    Rebalance(
                        month=entry.month,
                        decision=entry.decision,
                        execution=entry.execution,
                        targets={row.key: weight for row in chosen},
                        split=entry.split,
                    )
                )

        keys = sorted({key for plan in rebalances for key in plan.targets})
        book = PriceBook.load(db, sessions, keys)
        benchmark = benchmark_path(db, "SPY", sessions, STARTING_CAPITAL)
        labels = load_labels(db, sorted(decisions), horizons=REPORTED_HORIZONS)
        say(
            f"selected {sum(len(p.targets) for p in rebalances)} positions over "
            f"{len(rebalances)} rebalances; {len(keys)} distinct identities"
        )

        engine = create_engine(f"sqlite:///{database}")
        try:
            with Session(engine) as orm:
                legs = terminal_legs(orm, keys)
        finally:
            engine.dispose()

        symbols = {row.key: row.symbol for row in executable}
        simulations: dict[str, SimulationResult] = {}
        metrics: dict[str, Any] = {}
        for cost in (GROSS, SIMPLE_10BPS_PER_SIDE):
            simulation = simulate(
                STRATEGY, rebalances, book, terminal=legs, symbols=symbols, cost=cost
            )
            result.violations.extend(
                f"{STRATEGY}/{cost.name}: {problem}" for problem in simulation.violations
            )
            simulations[cost.name] = simulation
            metrics[cost.name] = _portfolio_metrics(
                simulation, benchmark, rebalances, rebalances_per_year=4.0
            )
            say(f"{STRATEGY} {cost.name:<24} total {metrics[cost.name]['window']['total_return']}")

        gross_simulation = simulations[GROSS.name]
        last_session = sessions[-1].isoformat()
        held = {(str(t.kind), int(t.identity_id)) for t in gross_simulation.trades}
        prices: dict[tuple[str, int], float] = {}
        for kind, query in (
            ("COMPANY", "SELECT company_id, close FROM price_history WHERE date = ?"),
            (
                "HISTORICAL",
                "SELECT historical_security_id, close FROM historical_prices WHERE date = ?",
            ),
        ):
            for identity_id, close in db.execute(query, (last_session,)):
                key = (kind, int(identity_id))
                if key in held and close is not None:
                    prices[key] = float(close)

        matched = _matched_monthly(r1_0_directory, sessions, benchmark)

    trades = [vars(trade) for trade in gross_simulation.trades]
    holdings = [vars(holding) for holding in gross_simulation.holdings]
    contributions = identity_contributions(trades, prices)
    profit = float(gross_simulation.nav[-1]) - STARTING_CAPITAL
    names = concentration(contributions, profit)
    reconciliation = sum(record.pnl for record in contributions) - profit
    turnover = turnover_sources(trades, holdings)
    years_elapsed = max(len(sessions) - 1, 1) / 252.0
    cost_analysis = cost_break_even(
        trades,
        [
            {"session": session.isoformat(), "nav": value}
            for session, value in zip(gross_simulation.sessions, gross_simulation.nav, strict=True)
        ],
        strategy_total_return=float(metrics[GROSS.name]["window"]["total_return"]),
        benchmark_total_return=float(metrics[GROSS.name]["benchmark"]["total_return"]),
        observed_cost_bps=10.0,
        observed_total_return=float(metrics[SIMPLE_10BPS_PER_SIDE.name]["window"]["total_return"]),
        years=years_elapsed,
    )

    split_of = {entry.month: entry.split for entry in calendar}
    diagnostics = _decile_diagnostics(executable, labels, split_of)
    attrition = sample_attrition(quarterly_rows)
    limitation = classification_limitation(quarterly_rows, holdings)
    persistence = _quarter_persistence(holdings, calendar)

    block_relative = {
        name: body["benchmark_relative_return"]
        for name, body in metrics[GROSS.name]["splits"].items()
    }
    criteria = evaluate_criteria(
        binding.criteria,
        contrast_60=diagnostics["horizons"]["60"]["d10_minus_d1"]["mean"],
        decile_association=diagnostics["horizons"]["60"]["decile_rank_association"],
        annual_turnover=metrics[GROSS.name]["turnover"]["annual_estimate"],
        monthly_turnover_reference=MONTHLY_TURNOVER_REFERENCE,
        block_relative=block_relative,
        top_five_share=names["share_of_profit"]["top_5"],
    )
    interpretation = interpret(criteria, binding.aggregate_rule)
    say(
        f"H1 {interpretation['outcome']}: met {interpretation['criteria_met']}, "
        f"failed {interpretation['criteria_failed']}"
    )

    evaluation = _evaluation_document(
        binding, criteria, interpretation, diagnostics, metrics, matched, names, cost_analysis
    )
    result.evaluation = evaluation

    # --- outputs ---------------------------------------------------------
    fingerprints["experiment_binding"] = write_document(
        directory / "r1_2_experiment_binding.json",
        {
            "phase": PHASE,
            "strategy_version": binding.strategy_version,
            "experiment_spec_sha256": binding.spec_sha256,
            "hypothesis_sha256": binding.hypothesis_sha256,
            "r1_1_completion_sha256": binding.r1_1_marker_sha256,
            "r1_0_completion_sha256": binding.r1_0.marker_sha256,
            "r1_0_fingerprints_reverified": list(binding.r1_0.verified),
            "contract_sha256": contract_sha256,
            "dataset_id": binding.r1_0.dataset_id,
            "contract_version": binding.r1_0.contract_version,
            "frozen_rules": binding.rules,
            "frozen_criteria": list(binding.criteria),
            "aggregate_rule_in_frozen_spec": binding.aggregate_rule,
            "research_status": _research_status(),
        },
    )
    fingerprints["quarter_calendar"] = _jsonl(
        directory / "quarterly_decision_calendar.jsonl",
        [
            {
                "quarter": entry.quarter,
                "month": entry.month,
                "decision": entry.decision.isoformat(),
                "execution": entry.execution.isoformat(),
                "split": entry.split,
            }
            for entry in calendar
        ],
    )
    fingerprints["cross_sections"] = _jsonl(
        directory / "quarterly_cross_sections.jsonl",
        [row.as_json() for row in quarterly_rows],
    )
    fingerprints["selection"] = _jsonl(directory / "quarterly_selection.jsonl", selection)
    fingerprints["trades"] = _jsonl(directory / "quarterly_trade_ledger.jsonl", trades)
    fingerprints["holdings"] = _jsonl(directory / "quarterly_holdings_ledger.jsonl", holdings)
    fingerprints["nav"] = _jsonl(
        directory / "quarterly_nav.jsonl",
        [
            {
                "session": session.isoformat(),
                "nav": value,
                "cash": cash_value,
                "floor_nav": floor,
                "holdings": count,
            }
            for session, value, cash_value, floor, count in zip(
                gross_simulation.sessions,
                gross_simulation.nav,
                gross_simulation.cash,
                gross_simulation.floor_nav,
                gross_simulation.holdings_count,
                strict=True,
            )
        ],
    )
    fingerprints["metrics"] = write_document(
        directory / "quarterly_metrics.json",
        {
            "strategy_version": STRATEGY,
            "gross": metrics[GROSS.name],
            "net_10bps": metrics[SIMPLE_10BPS_PER_SIDE.name],
            "cost_drag_10bps": (
                float(metrics[GROSS.name]["window"]["total_return"])
                - float(metrics[SIMPLE_10BPS_PER_SIDE.name]["window"]["total_return"])
            ),
            "concentration": names,
            "profit": profit,
            "profit_reconciliation_residual": reconciliation,
            "turnover_decomposition": turnover,
            "cost_break_even": cost_analysis,
            "relative_path": relative_path(
                [
                    {"session": s.isoformat(), "nav": v}
                    for s, v in zip(gross_simulation.sessions, gross_simulation.nav, strict=True)
                ],
                [
                    {"session": s.isoformat(), "nav": v}
                    for s, v in zip(sessions, benchmark, strict=True)
                ],
            ),
            "rolling_performance": rolling_performance(
                [
                    {"session": s.isoformat(), "nav": v}
                    for s, v in zip(gross_simulation.sessions, gross_simulation.nav, strict=True)
                ],
                [
                    {"session": s.isoformat(), "nav": v}
                    for s, v in zip(sessions, benchmark, strict=True)
                ],
            ),
            "return_distribution": return_distribution(
                [
                    {"session": s.isoformat(), "nav": v}
                    for s, v in zip(gross_simulation.sessions, gross_simulation.nav, strict=True)
                ]
            ),
            "holding_persistence": persistence,
            "terminal": _terminal_summary(legs, gross_simulation),
        },
    )
    fingerprints["decile_diagnostics"] = write_document(
        directory / "quarterly_decile_diagnostics.json", diagnostics
    )
    fingerprints["matched_window"] = write_document(
        directory / "monthly_matched_window_comparison.json",
        {
            "comparator": matched,
            "quarterly": {
                "gross": metrics[GROSS.name]["window"],
                "net_10bps": metrics[SIMPLE_10BPS_PER_SIDE.name]["window"],
                "annual_turnover": metrics[GROSS.name]["turnover"]["annual_estimate"],
                "average_holdings": metrics[GROSS.name]["average_holdings"],
                "traded_notional_over_nav": cost_analysis["traded_notional_over_nav"],
            },
            "delta_quarterly_minus_monthly": _delta(metrics, matched, cost_analysis),
        },
    )
    fingerprints["attrition"] = write_document(
        directory / "quarterly_sample_attrition.json",
        {"attrition": attrition, "current_classification_limitation": limitation},
    )
    fingerprints["hypothesis_evaluation"] = write_document(
        directory / "r1_2_hypothesis_evaluation.json", evaluation
    )

    ledger = ResearchLedger(directory / "research_ledger.jsonl")
    (directory / "research_ledger.jsonl").unlink(missing_ok=True)
    for cost_model in (GROSS, SIMPLE_10BPS_PER_SIDE):
        body = metrics[cost_model.name]
        ledger.record(
            strategy=STRATEGY,
            mode="PORTFOLIO_BACKTEST",
            why="the one experiment R1.1 froze, executed unchanged"
            if cost_model is GROSS
            else "the same experiment under the single preregistered cost sensitivity",
            parameters={"cost": cost_model.name, "rebalance": "calendar quarter ends"},
            development=True,
            validation=True,
            holdout=True,
            variants_tried=1,
            parameter_sweeps=[],
            selection_criterion="NONE — preregistered in R1.1",
            outcome={
                "total_return": body["window"]["total_return"],
                "cagr": body["window"]["cagr"],
                "sharpe": body["window"]["sharpe"],
                "max_drawdown": body["window"]["max_drawdown"],
                "benchmark_relative_return": body["benchmark_relative_return"],
            },
        )
    fingerprints["research_ledger"] = digest_document(
        {
            "entries": [
                json.loads(line)
                for line in (directory / "research_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
        }
    )

    schema = json.loads(
        (contract_dir / "backtest_run_manifest_schema.json").read_text(encoding="utf-8")
    )
    result.manifest = run_manifest(
        run_id=f"R1_2-{STRATEGY}",
        strategy=STRATEGY,
        code_commit=code_commit,
        dataset_id=binding.r1_0.dataset_id,
        contract_version=binding.r1_0.contract_version,
        universe_fingerprint=binding.r1_0.contract_bindings.get("universe", ""),
        feature_fingerprints={
            "signal_observations": binding.r1_0.contract_bindings.get("signal_observations", ""),
            "r1_0_cross_sections": binding.r1_0.fingerprints.get("cross_sections", ""),
            "r1_1_experiment_spec": binding.spec_sha256,
        },
        price_fingerprint=binding.r1_0.contract_bindings.get("price_history", ""),
        label_fingerprint=binding.r1_0.contract_bindings.get("forward_return_labels", ""),
        rebalance_rule="calendar quarter ends only",
        cost={"model": GROSS.name, "basis_points_per_side": 0.0, "sensitivity": "10 bps per side"},
        date_range={"start": sessions[0].isoformat(), "end": sessions[-1].isoformat()},
        split=split_document.get("blocks", {}),
        parameters={
            "selection": "top 10%",
            "weighting": "equal",
            "score_version": COMPOUNDER_V1_2,
            "share_policy": SHARE_POLICY,
            "starting_capital": STARTING_CAPITAL,
            "signal_frequency": "calendar quarter ends",
            "experiment_spec_sha256": binding.spec_sha256,
            "holdout_contamination": "REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION",
        },
        benchmark="SPY",
        outputs=dict(sorted(fingerprints.items())),
        metrics=metrics[GROSS.name]["window"],
    )
    result.manifest_problems = validate_manifest(result.manifest, schema)
    fingerprints["run_manifest"] = write_document(
        directory / "r1_2_run_manifest.json", result.manifest
    )

    result.summary = _summary(
        binding=binding,
        calendar=calendar,
        sessions=sessions,
        metrics=metrics,
        matched=matched,
        diagnostics=diagnostics,
        names=names,
        turnover=turnover,
        cost=cost_analysis,
        evaluation=evaluation,
        attrition=attrition,
        limitation=limitation,
        persistence=persistence,
        legs=legs,
        simulation=gross_simulation,
        selection=selection,
        violations=result.violations,
    )
    fingerprints["summary"] = write_document(directory / "r1_2_summary.json", result.summary)
    result.fingerprints = dict(sorted(fingerprints.items()))
    write_document(directory / "r1_2_fingerprints.json", {"fingerprints": result.fingerprints})
    result.marker = {
        "stage": "R1_2_COMPLETE",
        "phase": PHASE,
        "dataset_id": binding.r1_0.dataset_id,
        "contract_version": binding.r1_0.contract_version,
        "contract_sha256": contract_sha256,
        "r1_1_experiment_spec_sha256": binding.spec_sha256,
        "r1_1_hypothesis_sha256": binding.hypothesis_sha256,
        "strategy_version": STRATEGY,
        "run_manifest_sha256": fingerprints["run_manifest"],
        "result_fingerprints": result.fingerprints,
        "h1_criteria": [item.as_json() for item in criteria],
        "h1_interpretation": interpretation,
        "holdout_contamination": _research_status(),
        "accounting_violations": len(result.violations),
        "manifest_problems": result.manifest_problems,
        "profitability_is_not_a_gate": True,
    }
    write_document(directory / "r1_2_complete.json", result.marker)
    return result


def _research_status() -> dict[str, Any]:
    """What kind of evidence R1.2 is, and what it is not."""
    return {
        "experiment_type": "HYPOTHESIS_DRIVEN_HISTORICAL_EXPERIMENT",
        "independent_out_of_sample_confirmation": False,
        "why": (
            "H1 was formed after R1.0's results — which included one evaluation of "
            "2025-01…2026-03 — had been seen. Re-reading that interval under a hypothesis it "
            "helped inspire cannot confirm the hypothesis."
        ),
        "r1_0_holdout_block": "REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION",
        "replacement_holdout_manufactured": False,
        "true_confirmation_requires": [
            "observations that did not exist when the R1.1 specification was frozen",
            "a paper or shadow period run under exactly these frozen rules",
        ],
        "not_permitted": "re-cutting the frozen split to manufacture a new 'untouched' period",
    }


def _decile_diagnostics(
    executable: list[CrossSectionRow],
    labels: dict[tuple[str, str, int, int], tuple[str, float | None]],
    split_of: dict[str, str],
) -> dict[str, Any]:
    """Quarter-end decile separation at the frozen horizons, 60 sessions primary."""
    out: dict[str, Any] = {}
    for horizon in REPORTED_HORIZONS:
        monthly = monthly_signal(executable, labels, split_of, horizon=horizon)
        summary = subset_diagnostics(monthly, key="split")
        overall = summary["ALL"]
        profile = overall["decile_profile"]
        out[str(horizon)] = {
            "horizon_sessions": horizon,
            "quarters": len(monthly),
            "observations": sum(sum(r.decile_counts.values()) for r in monthly),
            "decile_profile": profile,
            "cross_section_mean": overall["cross_section_mean"],
            "d10_minus_d1_pooled": overall["d10_minus_d1_pooled"],
            "d10_minus_d1": overall["d10_minus_d1"],
            "d10_minus_cross_section_mean": overall["d10_minus_cross_section_mean"],
            "rank_ic": overall["rank_ic"],
            "shape": overall["shape"],
            "decile_rank_association": _rank_association(profile),
            "by_split": {
                name: {
                    "quarters": body["months"],
                    "d10_minus_d1": body["d10_minus_d1"]["mean"],
                    "d10_minus_cross_section_mean": body["d10_minus_cross_section_mean"]["mean"],
                    "rank_ic": body["rank_ic"]["mean"],
                }
                for name, body in sorted(summary.items())
                if name != "ALL"
            },
            "quarters_overlapped": max(1, horizon // _SESSIONS_PER_QUARTER),
            "independence": (
                "quarter-end observations at this horizon do not overlap"
                if horizon <= _SESSIONS_PER_QUARTER
                else "this horizon exceeds a quarter, so adjacent quarterly observations overlap "
                "and are not independent"
            ),
            "primary": horizon == PRIMARY_HORIZON,
        }
    out["primary_horizon"] = PRIMARY_HORIZON
    out["horizons_note"] = (
        "only the horizons already frozen by R1.0 and R1.1 are reported. No horizon was chosen "
        "after seeing results."
    )
    return {"horizons": out}


def _rank_association(profile: dict[str, float | None]) -> float | None:
    """Spearman of decile index against that decile's mean return, R1.1's definition."""
    from stock_screener.signal_review import spearman

    pairs = [
        (float(bucket), profile[str(bucket)])
        for bucket in range(1, DECILES + 1)
        if profile.get(str(bucket)) is not None
    ]
    if len(pairs) < 3:
        return None
    return spearman([i for i, _ in pairs], [float(v) for _, v in pairs if v is not None])


def _quarter_persistence(
    holdings: list[dict[str, Any]], calendar: list[QuarterEnd]
) -> dict[str, Any]:
    """Quarter-to-quarter retention and how long a position is actually held."""
    by_quarter: dict[str, set[tuple[str, int]]] = {}
    for row in holdings:
        by_quarter.setdefault(str(row["month"]), set()).add(
            (str(row["kind"]), int(row["identity_id"]))
        )
    months = [entry.month for entry in calendar if entry.month in by_quarter]
    retention: list[float] = []
    for earlier, later in pairwise(months):
        held = by_quarter[earlier]
        if held:
            retention.append(len(held & by_quarter[later]) / len(held))
    runs: dict[tuple[str, int], int] = {}
    durations: list[int] = []
    previous: set[tuple[str, int]] = set()
    for month in months:
        current = by_quarter[month]
        for key in current:
            runs[key] = runs.get(key, 0) + 1
        for key in previous - current:
            durations.append(runs.pop(key, 0))
        previous = current
    durations.extend(runs.values())
    durations.sort()
    return {
        "quarter_pairs": len(retention),
        "quarter_to_quarter_retention_mean": (
            sum(retention) / len(retention) if retention else None
        ),
        "quarter_to_quarter_retention_min": min(retention) if retention else None,
        "quarter_to_quarter_retention_max": max(retention) if retention else None,
        "holding_duration_quarters": {
            "positions": len(durations),
            "mean": sum(durations) / len(durations) if durations else None,
            "median": durations[len(durations) // 2] if durations else None,
            "max": max(durations) if durations else None,
        },
        "note": "descriptive; no minimum-holding-period rule is created",
    }


def _terminal_summary(
    legs: dict[tuple[str, int], Any], simulation: SimulationResult
) -> dict[str, Any]:
    """Terminal events encountered, and how many left an unknown value."""
    return {
        "identities_resolved": len(legs),
        "settled": sum(1 for leg in legs.values() if leg.state is TerminalState.SETTLED),
        "extinguished": sum(1 for leg in legs.values() if leg.state is TerminalState.EXTINGUISHED),
        "unknown": sum(1 for leg in legs.values() if leg.state is TerminalState.UNKNOWN),
        "ongoing": sum(1 for leg in legs.values() if leg.state is TerminalState.ONGOING),
        "simulation_diagnostics": simulation.diagnostics,
        "policy": TERMINAL_LEG_POLICY,
    }


def _delta(
    metrics: dict[str, Any], matched: dict[str, Any], cost: dict[str, Any]
) -> dict[str, Any]:
    """Quarterly minus matched monthly, on the same sessions."""
    quarterly = metrics[GROSS.name]["window"]
    monthly = matched["gross"]

    def difference(field: str) -> float | None:
        left, right = quarterly.get(field), monthly.get(field)
        return None if left is None or right is None else float(left) - float(right)

    quarterly_drag = float(metrics[GROSS.name]["window"]["total_return"]) - float(
        metrics[SIMPLE_10BPS_PER_SIDE.name]["window"]["total_return"]
    )
    return {
        "total_return": difference("total_return"),
        "cagr": difference("cagr"),
        "volatility": difference("volatility"),
        "sharpe": difference("sharpe"),
        "sortino": difference("sortino"),
        "max_drawdown": difference("max_drawdown"),
        "annual_turnover": (
            float(metrics[GROSS.name]["turnover"]["annual_estimate"])
            - float(matched["annual_turnover"])
        ),
        "traded_notional_over_nav": (
            float(cost["traded_notional_over_nav"]) - float(matched["traded_notional_over_nav"])
        ),
        "cost_drag_10bps": quarterly_drag - float(matched["cost_drag_10bps"]),
        "average_holdings": (
            None
            if matched["average_holdings"] is None
            else float(metrics[GROSS.name]["average_holdings"]) - float(matched["average_holdings"])
        ),
        "reading": (
            "a difference is a difference, not a finding. Seventeen quarterly rebalances against "
            "fifty-one monthly ones over the same 1,000-odd sessions is a small sample on both "
            "sides, and none of these deltas carries a significance claim."
        ),
    }


def _evaluation_document(
    binding: ExperimentBinding,
    criteria: list[Any],
    interpretation: dict[str, Any],
    diagnostics: dict[str, Any],
    metrics: dict[str, Any],
    matched: dict[str, Any],
    names: dict[str, Any],
    cost: dict[str, Any],
) -> dict[str, Any]:
    """The H1 evaluation record: criteria, verdicts, contrary evidence, status."""
    primary = diagnostics["horizons"][str(PRIMARY_HORIZON)]
    return {
        "hypothesis_id": binding.hypothesis.get("hypothesis_id"),
        "hypothesis_statement": binding.hypothesis.get("statement"),
        "experiment_spec_sha256": binding.spec_sha256,
        "hypothesis_sha256": binding.hypothesis_sha256,
        "strategy_version": STRATEGY,
        "criteria": [item.as_json() for item in criteria],
        "interpretation": interpretation,
        "measurements": {
            "primary_horizon_sessions": PRIMARY_HORIZON,
            "quarters": primary["quarters"],
            "observations": primary["observations"],
            "d10_minus_d1_mean": primary["d10_minus_d1"]["mean"],
            "d10_minus_d1_t_stat": primary["d10_minus_d1"]["t_stat"],
            "rank_ic_mean": primary["rank_ic"]["mean"],
            "decile_rank_association": primary["decile_rank_association"],
            "annual_turnover": metrics[GROSS.name]["turnover"]["annual_estimate"],
            "monthly_turnover_reference": MONTHLY_TURNOVER_REFERENCE,
            "top_five_share_of_profit": names["share_of_profit"]["top_5"],
            "break_even_cost_per_side_bps": cost["break_even_cost_per_side_bps"],
            "matched_monthly_annual_turnover": matched["annual_turnover"],
        },
        "contrary_evidence": _contrary_evidence(diagnostics, metrics, matched),
        "limitations": list(LIMITATIONS),
        "research_status": _research_status(),
        "prospective_confirmation_status": "NOT AVAILABLE — requires data that did not exist "
        "when the R1.1 specification was frozen, or a prospective shadow period",
        "criteria_changed_after_results": False,
    }


def _contrary_evidence(
    diagnostics: dict[str, Any], metrics: dict[str, Any], matched: dict[str, Any]
) -> list[str]:
    """Measured, not asserted."""
    primary = diagnostics["horizons"][str(PRIMARY_HORIZON)]

    def number(value: Any, digits: int = 5) -> str:
        return "not measurable" if value is None else f"{float(value):.{digits}f}"

    quarterly = metrics[GROSS.name]["window"]
    return [
        f"the experiment rests on {primary['quarters']} quarterly cross-sections; a sample that "
        "small supports description, not statistical power",
        f"the primary 60-session D10-D1 has a t-statistic of "
        f"{number(primary['d10_minus_d1']['t_stat'], 2)} across those quarters",
        f"the 120-session horizon overlaps {primary['quarters_overlapped']} or more quarters, so "
        "those observations are not independent",
        f"the quarterly path returned {number(quarterly['total_return'], 4)} against the matched "
        f"monthly {number(matched['gross']['total_return'], 4)} over identical sessions — a "
        "single realised path each, not a distribution",
        "H1 was formed after the 2025-01…2026-03 interval had been read, so no part of this "
        "experiment is independent confirmation",
        "COMPOUNDER_V1_2 reads an undated classification for essentially every observation, "
        "which remains a material and unresolved data limitation whatever the return",
    ]


def _summary(**parts: Any) -> dict[str, Any]:
    """The one document a reader should be able to read on its own."""
    binding: ExperimentBinding = parts["binding"]
    metrics = parts["metrics"]
    calendar = parts["calendar"]
    sessions = parts["sessions"]
    diagnostics = parts["diagnostics"]
    selection = parts["selection"]
    executable = sum(int(row["eligible"]) for row in selection)
    return {
        "phase": PHASE,
        "strategy_version": STRATEGY,
        "dataset_id": binding.r1_0.dataset_id,
        "contract_version": binding.r1_0.contract_version,
        "research_status": _research_status(),
        "calendar": {
            "quarter_ends": len(calendar),
            "first_signal": calendar[0].decision.isoformat(),
            "last_signal": calendar[-1].decision.isoformat(),
            "first_execution": calendar[0].execution.isoformat(),
            "final_valuation": sessions[-1].isoformat(),
            "sessions": len(sessions),
            "by_block": quarters_by_block(calendar),
        },
        "sample": {
            "universe_observations": sum(int(row["universe"]) for row in selection),
            "executable": executable,
            "selected": sum(int(row["selected"]) for row in selection),
            "selected_historical": sum(int(row["selected_historical"]) for row in selection),
            "eligible_historical": sum(int(row["eligible_historical"]) for row in selection),
            "selected_unpriced": sum(int(row["selected_unpriced"]) for row in selection),
            "quarters_holding_cash": sum(1 for row in selection if row["held_cash"]),
            "cross_sections_are_sliced_from_r1_0": True,
        },
        "quarterly": {
            "gross": metrics[GROSS.name]["window"],
            "net_10bps": metrics[SIMPLE_10BPS_PER_SIDE.name]["window"],
            "benchmark": metrics[GROSS.name]["benchmark"],
            "benchmark_relative_return": metrics[GROSS.name]["benchmark_relative_return"],
            "turnover": metrics[GROSS.name]["turnover"],
            "average_holdings": metrics[GROSS.name]["average_holdings"],
            "cash_utilisation": metrics[GROSS.name]["cash_utilisation"],
            "splits": {
                name: {
                    "strategy_total_return": body["strategy"]["total_return"],
                    "benchmark_total_return": body["benchmark"]["total_return"],
                    "benchmark_relative_return": body["benchmark_relative_return"],
                    "rebalances": body["rebalances"],
                    "contaminated_for_h1_confirmation": name == "HOLDOUT",
                }
                for name, body in sorted(metrics[GROSS.name]["splits"].items())
            },
            "years": {
                year: {
                    "total_return": body["strategy"]["total_return"],
                    "benchmark_total_return": body["benchmark"]["total_return"],
                    "partial": body["partial"],
                }
                for year, body in sorted(metrics[GROSS.name]["years"].items())
            },
        },
        "matched_monthly_comparator": parts["matched"],
        "delta_quarterly_minus_monthly": _delta(metrics, parts["matched"], parts["cost"]),
        "signal": {
            horizon: {
                "d10_minus_d1": body["d10_minus_d1"]["mean"],
                "d10_minus_cross_section_mean": body["d10_minus_cross_section_mean"]["mean"],
                "rank_ic": body["rank_ic"]["mean"],
                "decile_rank_association": body["decile_rank_association"],
                "shape": body["shape"]["classification"],
                "independence": body["independence"],
                "primary": body["primary"],
            }
            for horizon, body in sorted(
                diagnostics["horizons"].items(),
                key=lambda item: int(item[0]) if item[0].isdigit() else 0,
            )
            if horizon.isdigit()
        },
        "concentration": parts["names"]["share_of_profit"],
        "turnover_decomposition": parts["turnover"]["share_by_cause"],
        "cost": {
            "break_even_cost_per_side_bps": parts["cost"]["break_even_cost_per_side_bps"],
            "monthly_reference_break_even_bps": 77.9,
            "traded_notional_over_nav": parts["cost"]["traded_notional_over_nav"],
        },
        "holding_persistence": parts["persistence"],
        "terminal": _terminal_summary(parts["legs"], parts["simulation"]),
        "current_classification_limitation": parts["limitation"],
        "sample_attrition": {
            "universe_observations": parts["attrition"]["universe_observations"],
            "executable_share": parts["attrition"]["executable_share"],
            "population": parts["attrition"]["executable_population"],
        },
        "h1": parts["evaluation"]["interpretation"],
        "accounting_violations": list(parts["violations"]),
        "limitations": list(LIMITATIONS),
        "not_production_ready": (
            "whatever the return, this is a historical experiment on a dataset whose score path "
            "depends on undated classification metadata for essentially every observation, and "
            "it has had no independent confirmation."
        ),
    }


def mirror_r1_2(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the phase's compact metadata and read each copy back.

    Args:
        mirror: The control mirror, opened on the dataset's namespace.
        directory: The experiment output root.

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


def r1_1_directory(control_root: Path) -> Path:
    """Where R1.1 wrote its record beneath a control root."""
    from stock_screener.quarterly_experiment.binding import R1_1_DIR

    return control_root / R1_1_DIR
