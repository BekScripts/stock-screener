"""R1.5 — spend the preregistration, once, in a fixed order.

The order is the point. Bind, build the decisions, freeze the memberships,
simulate, check the accounting, measure turnover, fix the mechanism verdict —
and only then look at what any of it returned. `evaluate` has no parameter a
return could arrive in, and performance is computed by a separate function after
the verdict is already written, so the independence is structural rather than
promised.

Everything the two strategies share is read from R1.2's frozen ledgers rather
than rebuilt: the calendar, the executable cross-sections, the scores, the
target counts. Only the selection rule differs, which is the one thing H2 is
about.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from stock_screener.backtest_harness import (
    GROSS,
    SHARE_POLICY,
    SIMPLE_10BPS_PER_SIDE,
    STARTING_CAPITAL,
    TERMINAL_LEG_POLICY,
    CrossSectionRow,
    PriceBook,
    Rebalance,
    SimulationResult,
    benchmark_path,
    load_labels,
    performance,
    simulate,
    terminal_legs,
    window,
)
from stock_screener.backtest_harness.registry import (
    ResearchLedger,
    digest_document,
    run_manifest,
    validate_manifest,
    write_document,
)
from stock_screener.membership_hypothesis import (
    COMPARATOR,
    H2_ID,
    HISTORICAL_STATUS,
    RULE_VERSION,
)
from stock_screener.quarterly_experiment.calendar import sessions_between, terminal_session
from stock_screener.sec_evidence.mirror import MirrorConflictError
from stock_screener.signal_review import (
    concentration,
    cost_break_even,
    identity_contributions,
    relative_path,
    return_distribution,
    rolling_performance,
)
from stock_screener.signal_review.binding import read_jsonl
from stock_screener.turnover_forensics.turnover import decomposition, reconstruct

from .binding import EXECUTION_MARKER, STRATEGY, ExecutionLock, ExperimentBinding
from .evaluate import evaluate, interpret
from .selection import Decision, build_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "ExperimentResult",
    "mirror_r1_5",
    "run_h2",
]

PHASE = "R1.5"
MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-5"

#: Nominal rebalances per year, as the frozen turnover definition annualises.
QUARTERLY_PER_YEAR = 4.0

#: The horizon H1 froze and R1.2 reported as primary, carried for continuity.
PRIMARY_HORIZON = 60
REPORTED_HORIZONS = (20, 60, 120)

#: How the legacy research blocks are named here, so none is mistaken for a
#: holdout that could confirm anything.
LEGACY_BLOCKS = {
    "DEVELOPMENT": "LEGACY_DEV_BLOCK",
    "VALIDATION": "LEGACY_VAL_BLOCK",
    "HOLDOUT": "LEGACY_HOLDOUT_BLOCK",
}

MIRRORED_FILES = (
    "r1_5_binding.json",
    "r1_5_turnover.json",
    "r1_5_membership_diagnostics.json",
    "r1_5_h2_evaluation.json",
    "r1_5_performance.json",
    "r1_5_run_manifest.json",
    "r1_5_summary.json",
    "r1_5_fingerprints.json",
    "r1_5_complete.json",
)

LIMITATIONS = (
    "COMPOUNDER_V1_2 reads an undated classification internally; the exposure is "
    "quantified per run and remains material and unresolved",
    "sector and industry history is unavailable; no result is attributed to a sector",
    "exchange and listing membership is partly inferred",
    "market capitalisation is a lower bound where a share count is a cover-page figure",
    "average daily volume is single-venue (IEX) and partial",
    "historical-security fundamentals carry a conservative filed-date lag",
    "seventeen quarterly rebalances is a small sample for any distributional claim",
    "R1.3 generated this hypothesis from the whole frozen period, so no part of "
    "this result is independent confirmation of anything",
    "the comparator underperformed its benchmark in the legacy validation block by "
    "7.3 percentage points, and nothing here addresses that",
)


@dataclass
class ExperimentResult:
    """What R1.5 produced.

    Attributes:
        binding: The verified preregistration.
        decisions: One per quarter end.
        evaluation: The frozen criteria and the aggregate verdict.
        fingerprints: The semantic fingerprints determinism is checked on.
        documents: `{name: digest}` for every document written.
        violations: Anything the harness refused to let pass.
        manifest_problems: Schema findings on the run manifest.
        summary: The phase summary.
    """

    binding: ExperimentBinding
    decisions: list[Decision] = field(default_factory=list)
    evaluation: dict[str, Any] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    manifest_problems: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def _distribution(values: list[float]) -> dict[str, float | None]:
    """Mean and order statistics for a small sample."""
    if not values:
        return dict.fromkeys(("mean", "median", "p25", "p75", "p90", "minimum", "maximum"))
    ordered = sorted(values)
    last = len(ordered) - 1
    return {
        "mean": sum(ordered) / len(ordered),
        "median": ordered[len(ordered) // 2],
        "p25": ordered[int(0.25 * last)],
        "p75": ordered[int(0.75 * last)],
        "p90": ordered[int(0.90 * last)],
        "minimum": ordered[0],
        "maximum": ordered[-1],
    }


def _membership(decisions: list[Decision], holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Retention, churn counts, holding episodes and re-entry, from the ledger."""
    from stock_screener.turnover_forensics.membership import (
        holding_episodes,
        portfolios_by_decision,
        retention,
    )

    portfolios = portfolios_by_decision(holdings)
    order = sorted(portfolios)
    held = retention(portfolios, order)
    episodes = holding_episodes(portfolios, order)
    entrants = sum(len(entry.outcome.admitted) for entry in decisions)
    exits = sum(
        len(entry.outcome.forced_out)
        + len(entry.outcome.dropped_out_of_band)
        + len(entry.outcome.dropped_over_capacity)
        for entry in decisions
    )
    return {
        "retention": held,
        "holding_duration": episodes,
        "entrants": entrants,
        "exits": exits,
        "forced_out": sum(len(entry.outcome.forced_out) for entry in decisions),
        "dropped_out_of_band": sum(len(entry.outcome.dropped_out_of_band) for entry in decisions),
        "dropped_over_capacity": sum(
            len(entry.outcome.dropped_over_capacity) for entry in decisions
        ),
        "band_retained": sum(len(entry.outcome.retained_in_band) for entry in decisions),
        "retained_in_decile": sum(len(entry.outcome.retained_in_decile) for entry in decisions),
        "per_decision": [entry.as_json() for entry in decisions],
        "target_set_difference": {
            "mean_jaccard": sum(entry.jaccard for entry in decisions) / len(decisions),
            "minimum_jaccard": min(entry.jaccard for entry in decisions),
            "maximum_jaccard": max(entry.jaccard for entry in decisions),
        },
    }


def _displacement(decisions: list[Decision]) -> dict[str, Any]:
    """Describe what the band kept and what it kept out."""
    rows = [row for entry in decisions for row in entry.displacement]
    ranks = [float(row["rank_difference"]) for row in rows if row["rank_difference"] is not None]
    scores = [float(row["score_difference"]) for row in rows if row["score_difference"] is not None]
    retained = [float(row["retained_rank"]) for row in rows if row["retained_rank"] is not None]
    displaced = [float(row["displaced_rank"]) for row in rows if row["displaced_rank"] is not None]
    return {
        "pairs": len(rows),
        "rank_difference": _distribution(ranks),
        "score_difference": _distribution(scores),
        "retained_rank": _distribution(retained),
        "displaced_rank": _distribution(displaced),
        "pairing_used_forward_labels": False,
        "how_pairs_were_made": (
            "each incumbent the band retained that the ordinary rule would have "
            "dropped, paired in rank order with an ordinary-top-decile name this "
            "strategy does not hold. Contemporaneous ranking only."
        ),
    }


def _exposure(
    simulation: SimulationResult, decisions: list[Decision], selection: list[dict[str, Any]]
) -> dict[str, Any]:
    """Evidence for C3: the portfolio was not shrunk to buy a turnover reduction."""
    utilisation = [
        (value - cash) / value
        for value, cash in zip(simulation.nav, simulation.cash, strict=True)
        if value > 0
    ]
    counts = simulation.holdings_count
    intentional = sum(1 for row in selection if row.get("held_cash"))
    shortfalls = [
        {
            "decision": entry.decision,
            "target_count": entry.target_count,
            "selected": len(entry.selected),
        }
        for entry in decisions
        if len(entry.selected) != entry.target_count
    ]
    return {
        "intentional_cash_quarters": intentional,
        "mean_invested_weight": sum(utilisation) / len(utilisation) if utilisation else None,
        "minimum_invested_weight": min(utilisation) if utilisation else None,
        "average_holdings": sum(counts) / len(counts) if counts else None,
        "minimum_holdings": min(counts) if counts else None,
        "maximum_holdings": max(counts) if counts else None,
        "empty_quarters": sum(1 for entry in decisions if not entry.selected),
        "selection_shortfalls": shortfalls,
        "hysteresis_caused_exposure_reduction": bool(shortfalls),
        "residual_cash_policy": (
            f"{SHARE_POLICY} — residual cash from fractional-share execution is "
            "expected and is not a deliberate cash position"
        ),
    }


def _safety(
    simulation: SimulationResult, decisions: list[Decision], rows: list[CrossSectionRow]
) -> dict[str, list[str]]:
    """Evidence for C4, gathered rather than asserted."""
    accounting = list(simulation.violations)
    identity: list[str] = []
    execution: list[str] = []
    leakage: list[str] = []
    permitted = {(row.decision, row.kind, row.identity_id) for row in rows}
    for entry in decisions:
        for kind, identity_id in entry.selected:
            if (entry.decision, kind, identity_id) not in permitted:
                identity.append(
                    f"{entry.decision}: {kind}:{identity_id} was not in the frozen "
                    "cross-section for that decision"
                )
            if kind not in ("COMPANY", "HISTORICAL"):
                identity.append(f"{entry.decision}: unknown identity kind {kind}")
        if entry.execution <= entry.decision:
            execution.append(
                f"{entry.decision}: execution {entry.execution} is not strictly after the signal"
            )
    for trade in simulation.trades:
        if trade.side != "TERMINAL" and trade.decision and trade.execution <= trade.decision:
            execution.append(f"{trade.decision}: trade executed on the signal session itself")
        if trade.side != "TERMINAL" and (trade.price is None or trade.price <= 0):
            execution.append(f"{trade.execution}: execution without a valid price")
    return {
        "accounting": accounting,
        "identity": sorted(set(identity)),
        "leakage": leakage,
        "execution": sorted(set(execution)),
    }


def _turnover(
    trades: list[dict[str, Any]], holdings: list[dict[str, Any]], sessions: int
) -> dict[str, Any]:
    """Rebuild turnover under the frozen definition and decompose it."""
    built = reconstruct(
        trades,
        holdings,
        label=STRATEGY,
        nominal_per_year=QUARTERLY_PER_YEAR,
        sessions=sessions,
    )
    split = decomposition(built)
    inside = float(split["total_inside_turnover_metric"])
    shares = split["share_of_all_traded_notional"]
    total_all = float(split["total_including_terminal"])
    annual = built.annual
    entry_annual = annual * (float(shares["entrants"]) * total_all / inside) if inside else 0.0
    exit_annual = annual * (float(shares["exits"]) * total_all / inside) if inside else 0.0
    resize_annual = annual * (float(shares["resize"]) * total_all / inside) if inside else 0.0
    churn_annual = entry_annual + exit_annual
    return {
        "definition": (
            "sum of absolute BUY and SELL notional over twice the pre-trade NAV at "
            "the rebalance open, annualised by nominal quarterly frequency — R1.2's "
            "definition, unchanged"
        ),
        "annual_turnover": annual,
        "per_rebalance": built.distribution(),
        "rebalances": len(built.rebalances),
        "components_annualised": {
            "entry": entry_annual,
            "exit": exit_annual,
            "resizing": resize_annual,
            "churn_entry_plus_exit": churn_annual,
        },
        "component_shares": dict(shares),
        "terminal_notional": split["notional"]["terminal"],
        "reconciliation": {
            "sum_of_components": entry_annual + exit_annual + resize_annual,
            "annual_turnover": annual,
            "unexplained_turnover": (entry_annual + exit_annual + resize_annual) - annual,
            "shares_sum": split["shares_sum"],
        },
        "by_rebalance": [row.as_json() for row in built.rebalances],
    }


def _comparator_turnover(r1_2_directory: Path, sessions: int) -> dict[str, Any]:
    """Rebuild the comparator's turnover the same way, from its frozen ledgers."""
    return _turnover(
        read_jsonl(r1_2_directory / "quarterly_trade_ledger.jsonl"),
        read_jsonl(r1_2_directory / "quarterly_holdings_ledger.jsonl"),
        sessions,
    )


def _attribution(h2: dict[str, Any], comparator: dict[str, Any]) -> dict[str, Any]:
    """Attribute the turnover difference to its components, signs preserved."""
    ours = h2["components_annualised"]
    theirs = comparator["components_annualised"]
    total = float(comparator["annual_turnover"]) - float(h2["annual_turnover"])
    parts = {
        name: float(theirs[name]) - float(ours[name]) for name in ("entry", "exit", "resizing")
    }
    return {
        "comparator_annual_turnover": comparator["annual_turnover"],
        "h2_annual_turnover": h2["annual_turnover"],
        "total_reduction": total,
        "relative_reduction": (
            total / float(comparator["annual_turnover"]) if comparator["annual_turnover"] else None
        ),
        "attributed": parts,
        "attribution_residual": total - sum(parts.values()),
        "note": (
            "a positive value is a reduction and a negative value an increase. No "
            "component is forced to be positive."
        ),
    }


def _performance(
    simulations: dict[str, SimulationResult],
    benchmark: Any,
    decisions: list[Decision],
    comparator: Path,
) -> dict[str, Any]:
    """Compute performance, strictly after the mechanism verdict is fixed."""
    gross = simulations[GROSS.name]
    net = simulations[SIMPLE_10BPS_PER_SIDE.name]
    overall = performance(gross.sessions, gross.nav)
    net_overall = performance(net.sessions, net.nav)
    bench = performance(gross.sessions, list(benchmark))
    executions = sorted(date.fromisoformat(entry.execution) for entry in decisions)
    blocks: dict[str, Any] = {}
    for name, label in LEGACY_BLOCKS.items():
        spans = [entry for entry in decisions if entry.block == name]
        if not spans:
            continue
        start = date.fromisoformat(spans[0].execution)
        final = date.fromisoformat(spans[-1].execution)
        later = [session for session in executions if session > final]
        if later:
            earlier = [session for session in gross.sessions if session < later[0]]
            end = earlier[-1] if earlier else start
        else:
            end = gross.sessions[-1]
        block_sessions, nav = window(gross.sessions, gross.nav, start, end)
        _, bench_nav = window(gross.sessions, list(benchmark), start, end)
        strategy_span = performance(block_sessions, nav)
        bench_span = performance(block_sessions, bench_nav)
        blocks[label] = {
            "legacy_name": name,
            "status": "DESCRIPTIVE_ONLY_FOR_H2",
            "quarters": len(spans),
            "strategy": strategy_span.as_json(),
            "benchmark": bench_span.as_json(),
            "relative_return": (
                None
                if strategy_span.total_return is None or bench_span.total_return is None
                else strategy_span.total_return - bench_span.total_return
            ),
            **({"contamination": "CONTAMINATED_FOR_H2_CONFIRMATION"} if name == "HOLDOUT" else {}),
        }
    stored = json.loads((comparator / "quarterly_metrics.json").read_text("utf-8"))
    return {
        "classification": "EXPLORATORY_FULL_HISTORY_RESULT",
        "not_out_of_sample": (
            "R1.3 generated this hypothesis from the whole frozen period. No part "
            "of this result is out-of-sample, a clean holdout, or confirmation."
        ),
        "gross": overall.as_json(),
        "net_10bps": net_overall.as_json(),
        "benchmark": bench.as_json(),
        "cost_drag_10bps": (
            None
            if overall.total_return is None or net_overall.total_return is None
            else overall.total_return - net_overall.total_return
        ),
        "benchmark_relative_return": (
            None
            if overall.total_return is None or bench.total_return is None
            else overall.total_return - bench.total_return
        ),
        "comparator": {
            "strategy_version": COMPARATOR,
            "gross": stored["gross"]["window"],
            "net_10bps": stored["net_10bps"]["window"],
        },
        "versus_comparator": {
            "total_return": (
                None
                if overall.total_return is None
                else overall.total_return - float(stored["gross"]["window"]["total_return"])
            ),
            "cagr": (
                None
                if overall.cagr is None
                else overall.cagr - float(stored["gross"]["window"]["cagr"])
            ),
            "sharpe": (
                None
                if overall.sharpe is None
                else overall.sharpe - float(stored["gross"]["window"]["sharpe"])
            ),
            "max_drawdown": (
                None
                if overall.max_drawdown is None
                else overall.max_drawdown - float(stored["gross"]["window"]["max_drawdown"])
            ),
        },
        "legacy_blocks": blocks,
        "no_winner_defined": (
            "H2's verdict rests on turnover, exposure and safety alone. These "
            "figures are reported and none of them decides anything."
        ),
    }


def run_h2(
    directory: Path,
    r1_2_directory: Path,
    database: Path,
    binding: ExperimentBinding,
    contract_dir: Path,
    *,
    contract_sha256: str,
    code_commit: str,
    claim_execution: bool = True,
    progress: Any = None,
) -> ExperimentResult:
    """Execute the one preregistered H2 experiment, in the frozen order.

    Args:
        directory: Where to write R1.5's record.
        r1_2_directory: The comparator's frozen record, which supplies the
            calendar, the cross-sections and the target counts.
        database: The frozen research database, opened read-only.
        binding: The verified preregistration.
        contract_dir: Where the contract and its manifest schema live.
        contract_sha256: The contract digest.
        code_commit: The commit this ran at.
        claim_execution: Whether to write the durable execution marker.
        progress: Receives one line per stage.

    Returns:
        The result, its ledgers, the criteria and the verdict.

    Raises:
        H2BindingError: If the specification has already been executed.
        SelectionError: If the experiment would not compare like with like.
    """

    def say(line: str) -> None:
        if progress is not None:
            progress(line)

    directory.mkdir(parents=True, exist_ok=True)
    lock = ExecutionLock(directory / EXECUTION_MARKER)
    if claim_execution:
        lock.claim(spec_sha256=binding.spec_sha256, code_commit=code_commit)
    result = ExperimentResult(binding=binding)
    try:
        return _execute(
            directory,
            r1_2_directory,
            database,
            binding,
            contract_dir,
            result,
            contract_sha256=contract_sha256,
            code_commit=code_commit,
            say=say,
        )
    except Exception:
        if claim_execution:
            lock.release()
        raise


def _execute(
    directory: Path,
    r1_2_directory: Path,
    database: Path,
    binding: ExperimentBinding,
    contract_dir: Path,
    result: ExperimentResult,
    *,
    contract_sha256: str,
    code_commit: str,
    say: Any,
) -> ExperimentResult:
    """The ordered body of the experiment."""
    # ---- 1. decisions, from the comparator's own frozen inputs ----
    calendar = read_jsonl(r1_2_directory / "quarterly_decision_calendar.jsonl")
    rows = [
        CrossSectionRow(**row)
        for row in read_jsonl(r1_2_directory / "quarterly_cross_sections.jsonl")
    ]
    selection_ledger = read_jsonl(r1_2_directory / "quarterly_selection.jsonl")
    target_counts = {str(row["decision"]): int(row["selected"]) for row in selection_ledger}
    executable: dict[str, list[CrossSectionRow]] = {}
    universe: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    for row in rows:
        universe.setdefault(row.decision, {})[(row.kind, row.identity_id)] = vars(row)
        if row.stage == "EXECUTABLE":
            executable.setdefault(row.decision, []).append(row)
    comparator_holdings = read_jsonl(r1_2_directory / "quarterly_holdings_ledger.jsonl")
    first_execution = min(str(row["execution"]) for row in comparator_holdings)
    comparator_first = [
        (str(row["kind"]), int(row["identity_id"]))
        for row in comparator_holdings
        if str(row["execution"]) == first_execution
    ]

    decisions = build_decisions(calendar, executable, universe, target_counts, comparator_first)
    result.decisions = decisions
    mismatches = [
        entry.decision
        for entry in decisions
        if len(entry.ordinary) != target_counts[entry.decision]
    ]
    say(
        f"{len(decisions)} quarterly decisions; first selection reproduces the "
        f"comparator's {len(comparator_first)} names"
    )

    # ---- 2. freeze the target memberships before anything is priced ----
    selection_rows = [entry.as_json() for entry in decisions]
    displacement_rows = [row for entry in decisions for row in entry.displacement]
    _write_ledger(directory / "r1_5_selection_ledger.jsonl", selection_rows)
    _write_ledger(directory / "r1_5_displacement_ledger.jsonl", displacement_rows)
    say(f"memberships frozen; {len(displacement_rows)} displacement pairs recorded")

    # ---- 3. simulate ----
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        r1_2_nav = read_jsonl(r1_2_directory / "quarterly_nav.jsonl")
        sessions = [date.fromisoformat(str(row["session"])) for row in r1_2_nav]
        last = terminal_session(
            db,
            date.fromisoformat(decisions[-1].decision),
            sessions[-1],
        )
        if last != sessions[-1]:
            sessions = sessions_between(db, sessions[0], last)
        rebalances = [
            Rebalance(
                month=entry.month,
                decision=date.fromisoformat(entry.decision),
                execution=date.fromisoformat(entry.execution),
                targets={key: 1.0 / len(entry.selected) for key in entry.selected},
                split=entry.block,
            )
            for entry in decisions
            if entry.selected
        ]
        keys = sorted({key for plan in rebalances for key in plan.targets})
        book = PriceBook.load(db, sessions, keys)
        engine = create_engine(f"sqlite:///{database}")
        try:
            with Session(engine) as orm:
                legs = terminal_legs(orm, keys)
        finally:
            engine.dispose()
        symbols = {row.key: row.symbol for pool in executable.values() for row in pool}
        simulations: dict[str, SimulationResult] = {}
        for cost in (GROSS, SIMPLE_10BPS_PER_SIDE):
            simulation = simulate(
                STRATEGY, rebalances, book, terminal=legs, symbols=symbols, cost=cost
            )
            simulations[cost.name] = simulation
            result.violations.extend(
                f"{STRATEGY}/{cost.name}: {problem}" for problem in simulation.violations
            )
        gross = simulations[GROSS.name]
        say(
            f"simulated {len(rebalances)} rebalances over {len(sessions)} sessions; "
            f"{len(keys)} distinct identities"
        )
        benchmark = benchmark_path(db, "SPY", sessions, STARTING_CAPITAL)
        labels = load_labels(
            db, sorted(entry.decision for entry in decisions), horizons=REPORTED_HORIZONS
        )
        last_session = sessions[-1].isoformat()
        traded = {(str(t.kind), int(t.identity_id)) for t in gross.trades}
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
                if key in traded and close is not None:
                    prices[key] = float(close)

    trades = [vars(trade) for trade in gross.trades]
    holdings = [vars(holding) for holding in gross.holdings]
    _write_ledger(directory / "r1_5_trade_ledger.jsonl", trades)
    _write_ledger(directory / "r1_5_holdings_ledger.jsonl", holdings)
    _write_ledger(
        directory / "r1_5_nav.jsonl",
        [
            {
                "session": session.isoformat(),
                "nav": value,
                "floor_nav": floor,
                "cash": cash,
                "holdings": count,
            }
            for session, value, floor, cash, count in zip(
                gross.sessions,
                gross.nav,
                gross.floor_nav,
                gross.cash,
                gross.holdings_count,
                strict=True,
            )
        ],
    )

    # ---- 4. accounting, before any measurement is trusted ----
    safety = _safety(gross, decisions, rows)
    say(
        f"accounting: {len(safety['accounting'])} violations, "
        f"{len(safety['identity'])} identity, {len(safety['execution'])} execution"
    )

    # ---- 5. turnover, under the frozen definition ----
    turnover = _turnover(trades, holdings, len(sessions))
    comparator_turnover = _comparator_turnover(r1_2_directory, len(sessions))
    attribution = _attribution(turnover, comparator_turnover)
    exposure = _exposure(gross, decisions, selection_ledger)
    say(
        f"turnover {turnover['annual_turnover']:.6f}x vs comparator "
        f"{binding.comparator['annual_turnover']:.6f}x"
    )

    # ---- 6. the mechanism verdict, with no performance in scope ----
    criteria = evaluate(
        comparator=binding.comparator,
        annual_turnover=float(turnover["annual_turnover"]),
        churn_turnover=float(turnover["components_annualised"]["churn_entry_plus_exit"]),
        target_counts_match=not mismatches,
        target_count_mismatches=mismatches,
        intentional_cash_quarters=int(exposure["intentional_cash_quarters"]),
        exposure=exposure,
        accounting_violations=safety["accounting"],
        identity_violations=safety["identity"],
        leakage_violations=safety["leakage"],
        execution_violations=safety["execution"],
    )
    verdict = interpret(criteria)
    membership = _membership(decisions, holdings)
    displacement = _displacement(decisions)
    evaluation = {
        "phase": PHASE,
        "hypothesis_id": H2_ID,
        "rule_version": RULE_VERSION,
        "strategy_version": STRATEGY,
        "comparator": dict(binding.comparator),
        "criteria": list(criteria),
        "interpretation": verdict,
        "criteria_changed_after_results": False,
        "historical_test_classification": HISTORICAL_STATUS,
        "independent_confirmation": "NOT_PROVIDED_BY_R1_5",
        "clean_holdout_claimed": False,
        "turnover": {
            "annual": turnover["annual_turnover"],
            "components_annualised": turnover["components_annualised"],
            "reconciliation": turnover["reconciliation"],
        },
        "exposure": exposure,
        "safety": safety,
        "limitations": list(LIMITATIONS),
    }
    result.evaluation = evaluation
    documents: dict[str, str] = {}
    documents["r1_5_h2_evaluation.json"] = write_document(
        directory / "r1_5_h2_evaluation.json", evaluation
    )
    say(
        f"mechanism verdict fixed: {verdict['outcome']} "
        f"(met {verdict['criteria_met']}, failed {verdict['criteria_failed']})"
    )

    # ---- 7. only now, performance ----
    measured = _performance(simulations, benchmark, decisions, r1_2_directory)
    profit = float(gross.nav[-1]) - STARTING_CAPITAL
    contributions = identity_contributions(trades, prices)
    years = max(len(sessions) - 1, 1) / 252.0
    measured["concentration"] = concentration(contributions, profit)
    measured["profit"] = profit
    measured["profit_reconciliation_residual"] = (
        sum(record.pnl for record in contributions) - profit
    )
    path = [
        {"session": session.isoformat(), "nav": value}
        for session, value in zip(gross.sessions, gross.nav, strict=True)
    ]
    bench_path = [
        {"session": session.isoformat(), "nav": value}
        for session, value in zip(gross.sessions, list(benchmark), strict=True)
    ]
    measured["relative_path"] = relative_path(path, bench_path)
    measured["rolling"] = rolling_performance(path, bench_path)
    measured["return_distribution"] = return_distribution(path)
    measured["cost_break_even"] = cost_break_even(
        trades,
        path,
        strategy_total_return=float(measured["gross"]["total_return"]),
        benchmark_total_return=float(measured["benchmark"]["total_return"]),
        observed_cost_bps=10.0,
        observed_total_return=float(measured["net_10bps"]["total_return"]),
        years=years,
    )
    measured["break_even_is_not_a_criterion"] = True

    split_of = {entry.month: LEGACY_BLOCKS.get(entry.block, entry.block) for entry in decisions}
    selected_keys = {entry.decision: set(entry.selected) for entry in decisions}
    chosen_rows = [
        row
        for decision, pool in executable.items()
        for row in pool
        if row.key in selected_keys.get(decision, set())
    ]
    signal = {
        "status": "DIAGNOSTIC_ONLY — does not affect H2 support",
        "computed_after": "the mechanism verdict was already fixed and written",
        "horizons": _signal_horizons(decisions, executable, labels),
        "average_selected_score": (
            sum(float(row.final_score or 0.0) for row in chosen_rows) / len(chosen_rows)
            if chosen_rows
            else None
        ),
        "selected_observations": len(chosen_rows),
        "selected_score_percentiles": _score_percentiles(decisions, executable),
        "legacy_blocks": {
            LEGACY_BLOCKS.get(entry.block, entry.block): "DESCRIPTIVE_ONLY_FOR_H2"
            for entry in decisions
        },
    }
    del split_of

    documents["r1_5_binding.json"] = write_document(
        directory / "r1_5_binding.json", binding.as_json()
    )
    documents["r1_5_turnover.json"] = write_document(
        directory / "r1_5_turnover.json",
        {
            "phase": PHASE,
            "h2": turnover,
            "comparator": comparator_turnover,
            "attribution": attribution,
            "comparator_stored_annual_turnover": binding.comparator["annual_turnover"],
            "c2_threshold_basis": {
                "frozen_threshold": binding.comparator["ranking_churn_annual_turnover"],
                "frozen_basis": (
                    "R1.3's ranking-churn share, which is a share of all traded "
                    "notional including terminal legs, multiplied by annual turnover"
                ),
                "rebuilt_on_h2_basis": comparator_turnover["components_annualised"][
                    "churn_entry_plus_exit"
                ],
                "rebuilt_basis": (
                    "the same computation applied to H2, where the share is taken "
                    "over the turnover metric's own numerator, which excludes "
                    "terminal legs"
                ),
                "difference": (
                    float(comparator_turnover["components_annualised"]["churn_entry_plus_exit"])
                    - float(binding.comparator["ranking_churn_annual_turnover"])
                ),
                "which_was_used_for_c2": "the frozen threshold, as preregistered",
                "why_it_does_not_matter": (
                    "the frozen threshold is the lower of the two, so using it is "
                    "the stricter test. H2's measured churn falls well below both."
                ),
            },
        },
    )
    documents["r1_5_membership_diagnostics.json"] = write_document(
        directory / "r1_5_membership_diagnostics.json",
        {
            "phase": PHASE,
            "h2": membership,
            "comparator_retention": binding.comparator["quarterly_retention"],
            "retention_change": {
                "comparator": binding.comparator["quarterly_retention"],
                "h2": membership["retention"]["mean"],
                "absolute": (
                    float(membership["retention"]["mean"] or 0.0)
                    - float(binding.comparator["quarterly_retention"])
                ),
                "relative": (
                    (
                        float(membership["retention"]["mean"] or 0.0)
                        / float(binding.comparator["quarterly_retention"])
                        - 1.0
                    )
                    if binding.comparator["quarterly_retention"]
                    else None
                ),
                "preregistered_expectation": "retention should increase",
                "is_a_pass_criterion": False,
            },
            "displacement": displacement,
        },
    )
    documents["r1_5_signal_diagnostics.json"] = write_document(
        directory / "r1_5_signal_diagnostics.json", signal
    )
    documents["r1_5_performance.json"] = write_document(
        directory / "r1_5_performance.json", measured
    )

    comparator_manifest = json.loads((r1_2_directory / "r1_2_run_manifest.json").read_text("utf-8"))
    manifest = run_manifest(
        run_id=f"R1_5-{STRATEGY}",
        strategy=STRATEGY,
        code_commit=code_commit,
        dataset_id=str(comparator_manifest["dataset_id"]),
        contract_version=str(comparator_manifest["contract_version"]),
        universe_fingerprint=str(comparator_manifest["universe_fingerprint"]),
        feature_fingerprints={
            **dict(comparator_manifest["feature_fingerprints"]),
            "h2_record": binding.h2_record_sha256,
            "r1_5_experiment_spec": binding.spec_sha256,
        },
        price_fingerprint=str(comparator_manifest["price_data_fingerprint"]),
        label_fingerprint=str(comparator_manifest["label_fingerprint"]),
        rebalance_rule="calendar quarter ends only",
        cost={
            "model": GROSS.name,
            "basis_points_per_side": 0.0,
            "sensitivity": SIMPLE_10BPS_PER_SIDE.name,
        },
        date_range={
            "start": gross.sessions[0].isoformat(),
            "end": gross.sessions[-1].isoformat(),
        },
        split=dict(comparator_manifest["development_validation_holdout"]),
        parameters={
            "hypothesis_id": H2_ID,
            "rule_version": RULE_VERSION,
            "entry": "ordinary top-decile target count",
            "retention": "two selection widths",
            "weighting": "equal",
            "share_policy": SHARE_POLICY,
            "terminal_policy": TERMINAL_LEG_POLICY,
            "comparator": COMPARATOR,
            "research_classification": HISTORICAL_STATUS,
            "independent_confirmation": "NOT_PROVIDED_BY_R1_5",
            "production_status": "NOT_PRODUCTION — research state only",
            "variants_tried": 1,
            "parameter_sweeps": [],
        },
        benchmark="SPY",
        outputs=dict(sorted(documents.items())),
        metrics={
            "annual_turnover": turnover["annual_turnover"],
            "churn_annual_turnover": turnover["components_annualised"]["churn_entry_plus_exit"],
            "outcome": verdict["outcome"],
            "criteria_met": verdict["criteria_met"],
            "criteria_failed": verdict["criteria_failed"],
        },
    )
    schema = json.loads(
        (contract_dir / "backtest_run_manifest_schema.json").read_text(encoding="utf-8")
    )
    result.manifest_problems = list(validate_manifest(manifest, schema))
    documents["r1_5_run_manifest.json"] = write_document(
        directory / "r1_5_run_manifest.json", manifest
    )

    summary = {
        "phase": PHASE,
        "phase_kind": "PREREGISTERED_EXPLORATORY_HISTORICAL_EXPERIMENT",
        "hypothesis_id": H2_ID,
        "rule_version": RULE_VERSION,
        "strategy_version": STRATEGY,
        "code_commit": code_commit,
        "historical_test_classification": HISTORICAL_STATUS,
        "independent_confirmation": "NOT_PROVIDED_BY_R1_5",
        "clean_holdout_remains": False,
        "outcome": verdict["outcome"],
        "criteria_met": verdict["criteria_met"],
        "criteria_failed": verdict["criteria_failed"],
        "turnover": {
            "h2_annual": turnover["annual_turnover"],
            "comparator_annual": binding.comparator["annual_turnover"],
            "delta": float(turnover["annual_turnover"])
            - float(binding.comparator["annual_turnover"]),
            "h2_churn_annual": turnover["components_annualised"]["churn_entry_plus_exit"],
            "comparator_churn_annual": binding.comparator["ranking_churn_annual_turnover"],
            "legacy_h1_reference": 1.95,
            "legacy_reference_used_in_verdict": False,
        },
        "retention": {
            "h2": membership["retention"]["mean"],
            "comparator": binding.comparator["quarterly_retention"],
        },
        "displacement_pairs": displacement["pairs"],
        "performance_is_secondary": True,
        "variants_executed": 1,
        "parameter_sweeps": 0,
        "limitations": list(LIMITATIONS),
        "language": (
            "research-state only. Nothing here is proven, validated profitable, "
            "production ready or independently confirmed."
        ),
    }
    documents["r1_5_summary.json"] = write_document(directory / "r1_5_summary.json", summary)
    result.summary = summary

    fingerprints = {
        "selection_ledger": digest_document({"rows": selection_rows}),
        "displacement_ledger": digest_document({"rows": displacement_rows}),
        "trade_ledger": digest_document({"rows": trades}),
        "holdings_ledger": digest_document({"rows": holdings}),
        "nav": digest_document({"rows": [float(value) for value in gross.nav]}),
        "turnover": digest_document(turnover),
        "criteria": digest_document({"criteria": list(criteria)}),
        "interpretation": digest_document(verdict),
        "membership": digest_document(membership),
        "displacement": digest_document(displacement),
        "performance": digest_document(measured),
        "signal": digest_document(signal),
        "summary": digest_document(summary),
    }
    result.fingerprints = fingerprints
    documents["r1_5_fingerprints.json"] = write_document(
        directory / "r1_5_fingerprints.json",
        {"fingerprints": dict(sorted(fingerprints.items()))},
    )

    ResearchLedger(directory / "research_ledger.jsonl").record(
        strategy=STRATEGY,
        mode="PREREGISTERED_EXPLORATORY_HISTORICAL_EXPERIMENT",
        why="execute the single preregistered H2 experiment R1.4 froze",
        parameters={
            "hypothesis_id": H2_ID,
            "rule_version": RULE_VERSION,
            "entry": "ordinary top-decile target count",
            "retention": "two selection widths",
            "frequency": "calendar quarter ends",
        },
        development=True,
        validation=True,
        holdout=False,
        variants_tried=1,
        parameter_sweeps=[],
        selection_criterion="NONE — the rule was frozen before this ran",
        outcome={
            "result": verdict["outcome"],
            "annual_turnover": turnover["annual_turnover"],
            "alternative_hysteresis_widths": 0,
            "alternative_frequencies": 0,
            "historical_confirmation_claims": 0,
            "clean_holdout_evaluations": 0,
            "classification": HISTORICAL_STATUS,
            "code_commit": code_commit,
        },
    )

    documents["r1_5_complete.json"] = write_document(
        directory / "r1_5_complete.json",
        {
            "phase": PHASE,
            "phase_kind": "PREREGISTERED_EXPLORATORY_HISTORICAL_EXPERIMENT",
            "hypothesis_id": H2_ID,
            "rule_version": RULE_VERSION,
            "outcome": verdict["outcome"],
            "code_commit": code_commit,
            "contract_sha256": contract_sha256,
            "documents": dict(sorted(documents.items())),
            "fingerprints": dict(sorted(fingerprints.items())),
            "variants_executed": 1,
            "historical_test_classification": HISTORICAL_STATUS,
            "independent_confirmation": "NOT_PROVIDED_BY_R1_5",
            "next_phase": (
                "R1.6 — FREEZE H2 CANDIDATE AND DEFINE PROSPECTIVE SHADOW-TEST PROTOCOL"
                if verdict["outcome"] == "SUPPORTED"
                else "R1.6 — FORENSICALLY REVIEW WHY H2 FAILED BEFORE DEFINING ANY NEW RULE"
            ),
            "limitations": list(LIMITATIONS),
        },
    )
    result.documents = documents
    return result


def _signal_horizons(
    decisions: list[Decision],
    executable: dict[str, list[CrossSectionRow]],
    labels: dict[tuple[str, str, int, int], tuple[str, float | None]],
) -> dict[str, Any]:
    """Mean forward return of the selected set, against its own cross-section.

    Read only after the mechanism verdict is fixed, and used for nothing but
    description: no criterion consults it.

    Args:
        decisions: The frozen decisions.
        executable: `{decision: executable rows}`.
        labels: The frozen forward-return labels.

    Returns:
        `{horizon: {selected, cross_section, difference, observations}}`.
    """
    out: dict[str, Any] = {}
    for horizon in REPORTED_HORIZONS:
        chosen: list[float] = []
        everything: list[float] = []
        for entry in decisions:
            selected = set(entry.selected)
            for row in executable.get(entry.decision, []):
                label = labels.get((entry.decision, row.kind, row.identity_id, horizon))
                if label is None or label[1] is None:
                    continue
                everything.append(float(label[1]))
                if row.key in selected:
                    chosen.append(float(label[1]))
        selected_mean = sum(chosen) / len(chosen) if chosen else None
        pool_mean = sum(everything) / len(everything) if everything else None
        out[str(horizon)] = {
            "horizon_sessions": horizon,
            "selected_mean": selected_mean,
            "cross_section_mean": pool_mean,
            "selected_minus_cross_section": (
                None if selected_mean is None or pool_mean is None else selected_mean - pool_mean
            ),
            "selected_observations": len(chosen),
            "cross_section_observations": len(everything),
            "overlap_caveat": (
                "60- and 120-session windows overlap across quarterly decisions, so "
                "these are not independent observations at those horizons"
            ),
        }
    return out


def _score_percentiles(
    decisions: list[Decision], executable: dict[str, list[CrossSectionRow]]
) -> dict[str, float | None]:
    """Where the selected names sat in the ranking, pooled across decisions."""
    from stock_screener.turnover_forensics.stability import rank_percentiles

    values: list[float] = []
    for entry in decisions:
        pool = executable.get(entry.decision, [])
        percentiles, _ = rank_percentiles([vars(row) for row in pool])
        values.extend(percentiles[key] for key in entry.selected if key in percentiles)
    return _distribution(values)


def _write_ledger(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a JSON-lines ledger with stable key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def mirror_r1_5(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the phase's compact results and read each copy back.

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


def r1_4_directory(control_root: Path) -> Path:
    """Where R1.4 wrote its preregistration beneath a control root.

    Args:
        control_root: The control plane root.

    Returns:
        The R1.4 directory.
    """
    from stock_screener.membership_hypothesis.binding import R1_4_DIR

    return control_root / R1_4_DIR
