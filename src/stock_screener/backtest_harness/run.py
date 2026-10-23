"""The R1.0 orchestration: bind, freeze, decide, execute, evaluate, record.

The order of the stages is the research protocol, not an implementation
detail, so it is written once here and cannot be taken in another order:

    contract verification
      → the decision calendar, derived from readiness
      → the frozen split, written before any performance exists
      → the baseline registry, written before any baseline runs
      → the preregistration, written and hashed
      → the monthly cross-sections (decisions only; no label is read)
      → target portfolios and next-session execution
      → holdings, cash and terminal events
      → the cost sensitivity
      → evaluation, against the frozen labels and the benchmark
      → run manifests, the research ledger and the completion marker

Two things are deliberately awkward. The holdout cannot be evaluated until
`first_holdout_access` has proved the preregistration on disk hashes to what
the caller presented, and the split is written before the first score is
reconstructed, so no boundary can be chosen with a result in view.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data_access import HistoricalSecurityRepository
from domain.scores import COMPOUNDER_V1_2
from stock_screener.backtest.outcomes import resolve_terminal_outcome
from stock_screener.backtest_harness.audit import bias_audit, spot_audit
from stock_screener.backtest_harness.crosssection import (
    CROSS_SECTION_FILE,
    SIGNAL_VERSION,
    CrossSectionRow,
    MonthlyCoverage,
    Stage,
    cross_section,
    write_cross_sections,
)
from stock_screener.backtest_harness.diagnostics import (
    DECILES,
    MIN_DECILE_CROSS_SECTION,
    PRIMARY_HORIZON,
    assign_deciles,
    contrast_series,
    decile_diagnostics,
    load_labels,
    rank_information_coefficient,
    score_distribution,
)
from stock_screener.backtest_harness.performance import (
    RISK_FREE_RATE,
    benchmark_path,
    performance,
    window,
)
from stock_screener.backtest_harness.portfolio import (
    GROSS,
    MIN_CROSS_SECTION,
    SHARE_POLICY,
    SIMPLE_10BPS_PER_SIDE,
    STARTING_CAPITAL,
    TERMINAL_LEG_POLICY,
    PriceBook,
    Rebalance,
    SimulationResult,
    TerminalLeg,
    TerminalState,
    simulate,
)
from stock_screener.backtest_harness.registry import (
    CANONICAL_STANDALONE_RANKING,
    LEDGER_FILE,
    MARKET_SIGNALS_READINESS,
    METRICS_REPORTED,
    PREREGISTRATION_FILE,
    REGISTRY_FILE,
    ResearchLedger,
    baseline_registry,
    digest_document,
    first_holdout_access,
    preregistration,
    run_manifest,
    validate_manifest,
    write_document,
)
from stock_screener.backtest_harness.sessions import (
    EXECUTION_RULE,
    SPLIT_FILE,
    DecisionDate,
    SplitName,
    build_split,
    decision_calendar,
    split_document,
    write_split,
)
from stock_screener.backtest_harness.universe import COMPANY, HISTORICAL, PitUniverse
from stock_screener.logging import configure_logging
from stock_screener.sec_evidence.mirror import MirrorConflictError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.backtest_harness.binding import HarnessBindings
    from stock_screener.config import Settings
    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "RESEARCH_DIR",
    "R1Result",
    "mirror_r1_0",
    "run_r1_0",
    "terminal_legs",
]

PHASE = "R1.0"
RESEARCH_DIR = "research/r1_0"

_PORTFOLIOS = ("BASELINE_UNIVERSE_EQUAL_WEIGHT_V1", "COMPOUNDER_TOP_DECILE_V1")

#: The control-mirror namespace. The dataset's, not the evidence generation's.
MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-0"

#: Compact, immutable metadata only. The trade and holdings ledgers are large
#: and deterministically reproducible from these, so they are not mirrored.
MIRRORED_FILES = (
    SPLIT_FILE,
    REGISTRY_FILE,
    PREREGISTRATION_FILE,
    "holdout_access.json",
    "monthly_coverage.json",
    "signal_diagnostics_compounder_v1.json",
    "spot_audit.json",
    "bias_audit.json",
    "r1_0_summary.json",
    "r1_0_fingerprints.json",
    "r1_0_complete.json",
    "run_BASELINE_PASSIVE_V1/run_manifest.json",
    "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/run_manifest.json",
    "run_COMPOUNDER_DECILES_V1/run_manifest.json",
    "run_COMPOUNDER_TOP_DECILE_V1/run_manifest.json",
    "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/selection.jsonl",
    "run_COMPOUNDER_TOP_DECILE_V1/selection.jsonl",
    "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1/run_manifest.SIMPLE_10BPS_PER_SIDE_V1.json",
    "run_COMPOUNDER_TOP_DECILE_V1/run_manifest.SIMPLE_10BPS_PER_SIDE_V1.json",
)


def mirror_r1_0(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the phase's compact metadata and read each copy back.

    Args:
        mirror: The control mirror, opened on the dataset's namespace.
        directory: The research output root.

    Returns:
        One record per mirrored file.

    Raises:
        MirrorConflictError: If a remote copy disagrees, or a copy does not
            read back as it was written.
    """
    records: list[MirrorRecord] = []
    for name in MIRRORED_FILES:
        local = directory / name
        if not local.is_file():
            raise MirrorConflictError(f"{local} does not exist; nothing to mirror")
        kind = f"{MIRROR_PREFIX}/{name.replace('/', '-').replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, phase=PHASE)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise MirrorConflictError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records


@dataclass
class R1Result:
    """Everything one R1.0 execution produced.

    Attributes:
        directory: Where the outputs were written.
        fingerprints: Semantic digests, excluding run ids and timestamps.
        summary: The summary document.
        marker: The completion marker.
        manifests: One run manifest per executed strategy.
        cost_manifests: One further manifest per strategy under the
            preregistered cost sensitivity, keyed `<strategy>.<cost>`.
        manifest_problems: Schema failures, by strategy. Empty is required.
        violations: Accounting invariants that failed. Empty is required.
        coverage: The monthly funnel.
        notes: Anything a reader must be told.
    """

    directory: Path
    fingerprints: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    marker: dict[str, Any] = field(default_factory=dict)
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    cost_manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest_problems: dict[str, list[str]] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    coverage: list[MonthlyCoverage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(dict(sorted(row.items())), sort_keys=True) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")
    return digest_document({"rows": [dict(sorted(row.items())) for row in rows]})


def _chunk(
    payload: tuple[str, str, list[dict[str, Any]], Settings],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the cross-sections for one contiguous slice of the calendar.

    Runs in the parent process when a single worker is asked for, and in a
    subprocess otherwise. The slice is contiguous so the worker holds only the
    months it needs, and the verdicts do not depend on which slice they fall
    in: every month is independent of every other.

    Args:
        payload: The database path, the generation root, the slice's decision
            dates as plain mappings, and the settings.

    Returns:
        The rows and the coverage funnels, as plain mappings.
    """
    database, generation, dates, settings = payload
    configure_logging(settings)
    months = [str(item["month"]) for item in dates]
    universe = PitUniverse.load(Path(generation), months)
    rows: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    engine = create_engine(f"sqlite:///{database}")
    try:
        with (
            closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db,
            Session(engine) as orm,
        ):
            for item in dates:
                when = DecisionDate(
                    month=str(item["month"]),
                    decision=date.fromisoformat(str(item["decision"])),
                    execution=date.fromisoformat(str(item["execution"])),
                    company_features=int(item["company_features"]),
                    historical_features=int(item["historical_features"]),
                )
                built, funnel = cross_section(
                    orm, db, settings, universe, when, split=str(item["split"])
                )
                rows.extend(row.as_json() for row in built)
                coverage.append(funnel.as_json())
    finally:
        engine.dispose()
    return rows, coverage


def terminal_legs(
    session: Session, keys: Sequence[tuple[str, int]]
) -> dict[tuple[str, int], TerminalLeg]:
    """Resolve, by identity, what a holder of each historical security received."""
    legs: dict[tuple[str, int], TerminalLeg] = {}
    repository = HistoricalSecurityRepository(session)
    for kind, identity_id in keys:
        if kind != HISTORICAL:
            continue
        security = repository.get(identity_id)
        if security is None:
            continue
        outcome = resolve_terminal_outcome(session, security)
        legs[(kind, identity_id)] = TerminalLeg(
            key=(kind, identity_id),
            state=TerminalState(str(outcome.status)),
            effective_date=outcome.effective_date,
            value_per_share=outcome.value_per_share,
            reason=str(outcome.reason),
            source=outcome.source,
        )
    return legs


def _targets(
    strategy: str, rows: Sequence[CrossSectionRow]
) -> tuple[dict[tuple[str, int], float], str | None]:
    """The target portfolio for one strategy on one date, and why it is empty."""
    executable = [row for row in rows if row.stage == str(Stage.EXECUTABLE)]
    if len(executable) < MIN_CROSS_SECTION:
        return {}, "BELOW_MINIMUM_CROSS_SECTION"
    if strategy == "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1":
        chosen = executable
    else:
        chosen = [row for row, bucket in assign_deciles(executable) if bucket == DECILES]
    if not chosen:
        return {}, "EMPTY_SELECTION"
    weight = 1.0 / len(chosen)
    return {row.key: weight for row in chosen}, None


def _partial(year: int, sessions: Sequence[date]) -> bool:
    """Whether the study covers only part of a calendar year.

    Measured against the study's own span, not against 1 January: a year whose
    first session is the third of January is a complete year, and labelling it
    partial would make the honest label on 2026 mean nothing.

    Args:
        year: The calendar year.
        sessions: Every valuation session of the whole path, in order.

    Returns:
        True when the path begins after the year began or ends before it ended.
    """
    if not sessions:
        return True
    return sessions[0] > date(year, 1, 1) or sessions[-1] < date(year, 12, 31)


def _year_windows(sessions: Sequence[date]) -> dict[str, tuple[date, date]]:
    years: dict[str, list[date]] = defaultdict(list)
    for session in sessions:
        years[str(session.year)].append(session)
    return {year: (dates[0], dates[-1]) for year, dates in years.items()}


def _split_windows(
    rebalances: Sequence[Rebalance], sessions: Sequence[date]
) -> dict[str, tuple[date, date]]:
    """Each block's valuation window: its first execution to the next block's eve."""
    ordered = sorted(rebalances, key=lambda r: r.execution)
    bounds: dict[str, tuple[date, date]] = {}
    for name in (SplitName.DEVELOPMENT, SplitName.VALIDATION, SplitName.HOLDOUT):
        block = [r for r in ordered if r.split == str(name)]
        if not block:
            continue
        start = block[0].execution
        later = [r for r in ordered if r.execution > block[-1].execution]
        if later:
            earlier = [session for session in sessions if session < later[0].execution]
            end = earlier[-1] if earlier else start
        else:
            end = sessions[-1]
        bounds[str(name)] = (start, end)
    return bounds


def _portfolio_metrics(
    result: SimulationResult,
    benchmark: Sequence[float],
    rebalances: Sequence[Rebalance],
) -> dict[str, Any]:
    """Every metric the preregistration promised, over the whole window and each block."""
    overall = performance(result.sessions, result.nav)
    bench = performance(result.sessions, list(benchmark))
    holdings = result.holdings_count or [0]
    utilisation = [
        1.0 - (cash / nav if nav > 0 else 0.0)
        for cash, nav in zip(result.cash, result.nav, strict=True)
    ]
    turnover = sorted(result.turnover)
    metrics: dict[str, Any] = {
        "window": overall.as_json(),
        "floor_window": performance(result.sessions, result.floor_nav).as_json(),
        "benchmark": bench.as_json(),
        "benchmark_relative_return": (
            None
            if overall.total_return is None or bench.total_return is None
            else overall.total_return - bench.total_return
        ),
        "tracking_difference": (
            None if overall.cagr is None or bench.cagr is None else overall.cagr - bench.cagr
        ),
        "rebalances": result.rebalances,
        "average_holdings": sum(holdings) / len(holdings),
        "minimum_holdings": min(holdings),
        "maximum_holdings": max(holdings),
        "cash_utilisation": sum(utilisation) / len(utilisation) if utilisation else None,
        "turnover": {
            "monthly_average": sum(turnover) / len(turnover) if turnover else None,
            "monthly_median": turnover[len(turnover) // 2] if turnover else None,
            "monthly_p90": turnover[int(0.9 * (len(turnover) - 1))] if turnover else None,
            "monthly_maximum": max(turnover) if turnover else None,
            "annual_estimate": (sum(turnover) / len(turnover) * 12.0) if turnover else None,
        },
        "risk_free_rate": RISK_FREE_RATE,
        "diagnostics": result.diagnostics,
        "splits": {},
        "years": {},
    }
    for name, (first, last) in _split_windows(rebalances, result.sessions).items():
        sessions, nav = window(result.sessions, result.nav, first, last)
        _, bench_nav = window(result.sessions, list(benchmark), first, last)
        metrics["splits"][name] = {
            "strategy": performance(sessions, nav).as_json(),
            "benchmark": performance(sessions, bench_nav).as_json(),
        }
    for year, (first, last) in sorted(_year_windows(result.sessions).items()):
        sessions, nav = window(result.sessions, result.nav, first, last)
        _, bench_nav = window(result.sessions, list(benchmark), first, last)
        metrics["years"][year] = {
            "strategy": performance(sessions, nav).as_json(),
            "benchmark": performance(sessions, bench_nav).as_json(),
            "partial": _partial(int(year), result.sessions),
        }
    return metrics


def run_r1_0(
    directory: Path,
    settings: Settings,
    bindings: HarnessBindings,
    generation_dir: Path,
    contract_dir: Path,
    *,
    contract_sha256: str,
    code_commit: str,
    stride: int = 1,
    workers: int = 1,
    progress: Any = None,
) -> R1Result:
    """Execute the whole phase into one directory and return what it produced.

    Args:
        directory: The research output root.
        settings: Supplies the benchmark, thresholds and volume basis.
        bindings: The proven frozen bindings. Nothing runs without them.
        generation_dir: The evidence generation's plan root.
        contract_dir: Where the frozen contract and its schema live.
        contract_sha256: The contract file's digest, frozen into the
            preregistration so the run cannot later claim another contract.
        code_commit: The commit the run is defined at.
        stride: Take every nth decision month. One is the whole calendar;
            anything else is a smoke run and is recorded as such.
        workers: Processes to build the cross-sections with. Months are
            independent, so this changes speed and nothing else; one keeps the
            whole build in this process.
        progress: Receives one line per stage.

    Returns:
        The result, with its fingerprints, manifests and violations.
    """

    def say(line: str) -> None:
        if progress is not None:
            progress(line)

    directory.mkdir(parents=True, exist_ok=True)
    database = bindings.database_path
    result = R1Result(directory=directory)

    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        calendar = decision_calendar(db)
        if stride > 1:
            calendar = calendar[::stride]
        split = build_split(calendar)
        split_path = write_split(directory, split, calendar)
        split_doc = split_document(split, calendar)
        split_sha = digest_document(split_doc)
        say(f"split frozen: {split_path.name} {split_sha[:16]}…")

        registry = baseline_registry(
            dataset_id=bindings.dataset_id, contract_version=bindings.contract_version
        )
        registry_sha = write_document(directory / REGISTRY_FILE, registry)
        say(f"baseline registry written: {registry_sha[:16]}…")

        prereg = preregistration(
            dataset_id=bindings.dataset_id,
            contract_version=bindings.contract_version,
            contract_sha256=contract_sha256,
            split_sha256=split_sha,
            split=split_doc,
            registry_sha256=registry_sha,
            benchmark=settings.benchmark_symbol,
            code_commit=code_commit,
            bindings={
                "universe": bindings.universe_fingerprint,
                "price_history": bindings.price_fingerprint,
                "historical_prices": bindings.historical_price_fingerprint,
                "forward_return_labels": bindings.label_fingerprint,
                "signal_observations": bindings.signal_fingerprint,
                "pit_coverage": bindings.coverage_sha256,
                "pit_month_outcomes": bindings.outcomes_sha256,
                "database": bindings.bindings.database_sha256,
                "evidence_manifest": bindings.bindings.evidence_manifest_sha256,
                "post_a3_index": bindings.bindings.post_a3_index_sha256,
                "pit_marker": bindings.bindings.pit_marker_sha256,
            },
        )
        prereg_sha = write_document(directory / PREREGISTRATION_FILE, prereg)
        say(f"preregistration frozen: {prereg_sha[:16]}…")

        universe = PitUniverse.load(generation_dir)
        say(f"frozen universe loaded: {len(universe.months())} months")

        tasks = [
            {
                "month": when.month,
                "decision": when.decision.isoformat(),
                "execution": when.execution.isoformat(),
                "company_features": when.company_features,
                "historical_features": when.historical_features,
                "split": str(split.assign(when.month)),
            }
            for when in calendar
        ]
        slices = _slices(tasks, max(workers, 1))
        payloads = [(database, str(generation_dir), chunk, settings) for chunk in slices]
        raw: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        if workers <= 1:
            for payload in payloads:
                raw.append(_chunk(payload))
                say(f"cross-sections: {len(raw)} of {len(payloads)} slices")
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for done in pool.map(_chunk, payloads):
                    raw.append(done)
                    say(f"cross-sections: {len(raw)} of {len(payloads)} slices")
        rows = [CrossSectionRow(**item) for chunk, _ in raw for item in chunk]
        coverage = [MonthlyCoverage(**item) for _, chunk in raw for item in chunk]
        rows.sort(key=lambda row: (row.decision, row.kind, row.identity_id))
        coverage.sort(key=lambda item: item.decision)
        result.coverage = coverage
        executable_keys = sorted({row.key for row in rows if row.stage == str(Stage.EXECUTABLE)})
        engine = create_engine(f"sqlite:///{database}")
        try:
            with Session(engine) as orm:
                legs = terminal_legs(orm, executable_keys)
        finally:
            engine.dispose()
        for item in coverage:
            say(
                f"{item.month} {item.decision} universe {item.universe:>5} "
                f"scored {item.score_ready:>5} tradable {item.tradable:>5} "
                f"executable {item.executable:>5}"
            )

        cross_path = directory / CROSS_SECTION_FILE
        write_cross_sections(cross_path, rows)
        cross_fingerprint = digest_document(
            {
                "rows": [
                    {
                        "kind": row.kind,
                        "identity_id": row.identity_id,
                        "decision": row.decision,
                        "stage": row.stage,
                        "score": row.final_score,
                        "tradability": row.tradability,
                        "exclusion": row.exclusion,
                    }
                    for row in rows
                ]
            }
        )
        coverage_sha = write_document(
            directory / "monthly_coverage.json",
            {"months": [item.as_json() for item in coverage]},
        )
        say(f"cross-sections built: {len(rows)} rows {cross_fingerprint[:16]}…")

        by_month: dict[str, list[CrossSectionRow]] = defaultdict(list)
        for row in rows:
            by_month[row.month].append(row)

        plans: dict[str, list[Rebalance]] = {}
        held_out: dict[str, dict[str, int]] = {}
        selection: dict[str, list[dict[str, Any]]] = {}
        for strategy in _PORTFOLIOS:
            built: list[Rebalance] = []
            empty: dict[str, int] = defaultdict(int)
            chosen: list[dict[str, Any]] = []
            for when in calendar:
                month_rows = by_month.get(when.month, [])
                eligible = [row for row in month_rows if row.stage == str(Stage.EXECUTABLE)]
                targets, why = _targets(strategy, month_rows)
                picked = [row for row in eligible if row.key in targets]
                chosen.append(
                    {
                        "month": when.month,
                        "decision": when.decision.isoformat(),
                        "execution": when.execution.isoformat(),
                        "split": str(split.assign(when.month)),
                        "universe": sum(1 for row in month_rows),
                        "eligible": len(eligible),
                        "eligible_historical": sum(1 for row in eligible if row.kind == HISTORICAL),
                        "selected": len(picked),
                        "selected_historical": sum(1 for row in picked if row.kind == HISTORICAL),
                        "selected_unpriced": sum(
                            1 for row in picked if row.execution_price is None
                        ),
                        "held_cash": why,
                    }
                )
                if why is not None:
                    empty[why] += 1
                    continue
                built.append(
                    Rebalance(
                        month=when.month,
                        decision=when.decision,
                        execution=when.execution,
                        targets=targets,
                        split=str(split.assign(when.month)),
                    )
                )
            plans[strategy] = built
            held_out[strategy] = dict(empty)
            selection[strategy] = chosen

        first_execution = calendar[0].execution
        terminal_session = _terminal_session(db, calendar[-1].decision)
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute(
                "SELECT date FROM benchmark_prices WHERE date >= ? AND date <= ? ORDER BY date",
                (first_execution.isoformat(), terminal_session.isoformat()),
            )
        ]
        say(f"valuation window {sessions[0]} .. {sessions[-1]} ({len(sessions)} sessions)")

        symbols = {row.key: row.symbol for row in rows}
        book = PriceBook.load(db, sessions, executable_keys)
        benchmark = benchmark_path(db, settings.benchmark_symbol, sessions, STARTING_CAPITAL)

        access = first_holdout_access(
            directory,
            preregistration_sha256=prereg_sha,
            strategies=[*_PORTFOLIOS, "COMPOUNDER_DECILES_V1", "BASELINE_PASSIVE_V1"],
        )
        recorded = str(access["preregistration_sha256"])[:16]
        say(f"first holdout access recorded under preregistration {recorded}…")

        ledger = ResearchLedger(directory / LEDGER_FILE)
        (directory / LEDGER_FILE).unlink(missing_ok=True)
        fingerprints: dict[str, str] = {
            "research_split_v1": split_sha,
            "baseline_registry": registry_sha,
            "r1_0_preregistration": prereg_sha,
            "monthly_coverage": coverage_sha,
            "cross_sections": cross_fingerprint,
        }

        # --- the passive reference -----------------------------------------
        passive = performance(sessions, list(benchmark))
        passive_splits: dict[str, Any] = {}
        passive_years: dict[str, Any] = {}
        passive_metrics: dict[str, Any] = {
            "window": passive.as_json(),
            "benchmark": passive.as_json(),
            "benchmark_relative_return": 0.0,
            "tracking_difference": 0.0,
            "rebalances": 0,
            "average_holdings": 1.0,
            "minimum_holdings": 1,
            "maximum_holdings": 1,
            "cash_utilisation": 1.0,
            "turnover": {
                "monthly_average": 0.0,
                "monthly_median": 0.0,
                "monthly_p90": 0.0,
                "monthly_maximum": 0.0,
                "annual_estimate": 0.0,
            },
            "risk_free_rate": RISK_FREE_RATE,
            "splits": passive_splits,
            "years": passive_years,
        }
        for name, (first, last) in _split_windows(
            [
                Rebalance(
                    month=when.month,
                    decision=when.decision,
                    execution=when.execution,
                    targets={},
                    split=str(split.assign(when.month)),
                )
                for when in calendar
            ],
            sessions,
        ).items():
            block_sessions, block_nav = window(sessions, list(benchmark), first, last)
            passive_splits[name] = {"strategy": performance(block_sessions, block_nav).as_json()}
        for year, (first, last) in sorted(_year_windows(sessions).items()):
            block_sessions, block_nav = window(sessions, list(benchmark), first, last)
            passive_years[year] = {
                "strategy": performance(block_sessions, block_nav).as_json(),
                "partial": _partial(int(year), sessions),
            }
        passive_dir = directory / "run_BASELINE_PASSIVE_V1"
        nav_sha = _jsonl(
            passive_dir / "nav.jsonl",
            [
                {"session": session.isoformat(), "nav": value}
                for session, value in zip(sessions, benchmark, strict=True)
            ],
        )
        metrics_sha = write_document(passive_dir / "metrics.json", passive_metrics)
        fingerprints["BASELINE_PASSIVE_V1.nav"] = nav_sha
        fingerprints["BASELINE_PASSIVE_V1.metrics"] = metrics_sha
        result.manifests["BASELINE_PASSIVE_V1"] = _manifest(
            "BASELINE_PASSIVE_V1",
            bindings,
            code_commit,
            sessions,
            split_doc,
            {"benchmark": settings.benchmark_symbol, "holding": "buy and hold"},
            {"model": GROSS.name, "basis_points_per_side": 0.0},
            {"nav": nav_sha, "metrics": metrics_sha},
            passive_metrics["window"],
            settings.benchmark_symbol,
            rebalance_rule="none — a reference, held for the whole window",
        )
        ledger.record(
            strategy="BASELINE_PASSIVE_V1",
            mode="REFERENCE",
            why="the passive reference every other result is measured against",
            parameters={"benchmark": settings.benchmark_symbol},
            development=True,
            validation=True,
            holdout=True,
            variants_tried=1,
            parameter_sweeps=[],
            selection_criterion="NONE",
            outcome={
                "total_return": passive.total_return,
                "cagr": passive.cagr,
                "max_drawdown": passive.max_drawdown,
            },
        )

        # --- the two portfolios, gross and under the cost sensitivity -------
        simulations: dict[str, SimulationResult] = {}
        for strategy in _PORTFOLIOS:
            for cost in (GROSS, SIMPLE_10BPS_PER_SIDE):
                simulation = simulate(
                    strategy,
                    plans[strategy],
                    book,
                    terminal=legs,
                    symbols=symbols,
                    cost=cost,
                )
                result.violations.extend(
                    f"{strategy}/{cost.name}: {problem}" for problem in simulation.violations
                )
                metrics = _portfolio_metrics(simulation, benchmark, plans[strategy])
                metrics["empty_cross_sections"] = held_out[strategy]
                metrics["share_policy"] = SHARE_POLICY
                run_dir = directory / f"run_{strategy}"
                suffix = "" if cost is GROSS else f".{cost.name}"
                nav_digest = _jsonl(
                    run_dir / f"nav{suffix}.jsonl",
                    [
                        {
                            "session": session.isoformat(),
                            "nav": value,
                            "cash": cash,
                            "floor_nav": floor,
                            "holdings": count,
                        }
                        for session, value, cash, floor, count in zip(
                            simulation.sessions,
                            simulation.nav,
                            simulation.cash,
                            simulation.floor_nav,
                            simulation.holdings_count,
                            strict=True,
                        )
                    ],
                )
                trades_digest = _jsonl(
                    run_dir / f"trades{suffix}.jsonl",
                    [vars(trade) for trade in simulation.trades],
                )
                holdings_digest = _jsonl(
                    run_dir / f"holdings{suffix}.jsonl",
                    [vars(holding) for holding in simulation.holdings],
                )
                metrics_digest = write_document(run_dir / f"metrics{suffix}.json", metrics)
                if cost is GROSS:
                    executed: dict[str, int] = defaultdict(int)
                    executed_historical: dict[str, int] = defaultdict(int)
                    carried: dict[str, int] = defaultdict(int)
                    for holding in simulation.holdings:
                        executed[holding.month] += 1
                        if holding.kind == HISTORICAL:
                            executed_historical[holding.month] += 1
                        if holding.target_weight <= 0.0:
                            carried[holding.month] += 1
                    for record in selection[strategy]:
                        month = str(record["month"])
                        record["executed"] = executed.get(month, 0)
                        record["executed_historical"] = executed_historical.get(month, 0)
                        # A position the strategy no longer wants but could not
                        # sell: the execution session had no opening price for
                        # it. Counted, never quietly dropped from the book.
                        record["carried_off_target"] = carried.get(month, 0)
                    fingerprints[f"{strategy}.selection"] = _jsonl(
                        run_dir / "selection.jsonl", selection[strategy]
                    )
                label = f"{strategy}{suffix}"
                fingerprints[f"{label}.nav"] = nav_digest
                fingerprints[f"{label}.trades"] = trades_digest
                fingerprints[f"{label}.holdings"] = holdings_digest
                fingerprints[f"{label}.metrics"] = metrics_digest
                if cost is GROSS:
                    simulations[strategy] = simulation
                    result.manifests[strategy] = _manifest(
                        strategy,
                        bindings,
                        code_commit,
                        simulation.sessions,
                        split_doc,
                        {
                            "weighting": "equal",
                            "selection": (
                                "top decile by COMPOUNDER_V1_2"
                                if strategy == "COMPOUNDER_TOP_DECILE_V1"
                                else "the whole executable cross-section"
                            ),
                            "score_version": COMPOUNDER_V1_2,
                            "share_policy": SHARE_POLICY,
                            "starting_capital": STARTING_CAPITAL,
                            "minimum_cross_section": MIN_CROSS_SECTION,
                        },
                        {"model": GROSS.name, "basis_points_per_side": 0.0},
                        {
                            "nav": nav_digest,
                            "trades": trades_digest,
                            "holdings": holdings_digest,
                            "metrics": metrics_digest,
                        },
                        metrics["window"],
                        settings.benchmark_symbol,
                        rebalance_rule="monthly, at the open after the month's last session",
                    )
                    ledger.record(
                        strategy=strategy,
                        mode="PORTFOLIO_BACKTEST",
                        why="a preregistered R1.0 baseline",
                        parameters={"cost": cost.name, "weighting": "equal"},
                        development=True,
                        validation=True,
                        holdout=True,
                        variants_tried=1,
                        parameter_sweeps=[],
                        selection_criterion="NONE",
                        outcome={
                            "total_return": metrics["window"]["total_return"],
                            "cagr": metrics["window"]["cagr"],
                            "sharpe": metrics["window"]["sharpe"],
                            "max_drawdown": metrics["window"]["max_drawdown"],
                            "benchmark_relative_return": metrics["benchmark_relative_return"],
                        },
                    )
                else:
                    result.cost_manifests[f"{strategy}.{cost.name}"] = _manifest(
                        strategy,
                        bindings,
                        code_commit,
                        simulation.sessions,
                        split_doc,
                        {
                            "weighting": "equal",
                            "selection": (
                                "top decile by COMPOUNDER_V1_2"
                                if strategy == "COMPOUNDER_TOP_DECILE_V1"
                                else "the whole executable cross-section"
                            ),
                            "score_version": COMPOUNDER_V1_2,
                            "share_policy": SHARE_POLICY,
                            "starting_capital": STARTING_CAPITAL,
                            "minimum_cross_section": MIN_CROSS_SECTION,
                        },
                        {
                            "model": cost.name,
                            "basis_points_per_side": cost.bps_per_side,
                            "statement": "a stated sensitivity, not a market model",
                        },
                        {
                            "nav": nav_digest,
                            "trades": trades_digest,
                            "holdings": holdings_digest,
                            "metrics": metrics_digest,
                        },
                        metrics["window"],
                        settings.benchmark_symbol,
                        rebalance_rule="monthly, at the open after the month's last session",
                    )
                    ledger.record(
                        strategy=strategy,
                        mode="PORTFOLIO_BACKTEST",
                        why="the single preregistered cost sensitivity",
                        parameters={"cost": cost.name, "basis_points_per_side": cost.bps_per_side},
                        development=True,
                        validation=True,
                        holdout=True,
                        variants_tried=1,
                        parameter_sweeps=[],
                        selection_criterion="NONE",
                        outcome={
                            "total_return": metrics["window"]["total_return"],
                            "cagr": metrics["window"]["cagr"],
                        },
                    )
                say(
                    f"{strategy:<34} {cost.name:<24} "
                    f"total {metrics['window']['total_return']} "
                    f"cagr {metrics['window']['cagr']}"
                )

        # --- the signal diagnostic ------------------------------------------
        split_of = {when.month: str(split.assign(when.month)) for when in calendar}
        labels = load_labels(db, [when.decision.isoformat() for when in calendar])
        executable = [row for row in rows if row.stage == str(Stage.EXECUTABLE)]
        deciles, skipped, decile_months = decile_diagnostics(executable, labels, split_of)
        coefficients, monthly_ic = rank_information_coefficient(executable, labels, split_of)
        distribution = score_distribution(executable, executable)
        decile_dir = directory / "run_COMPOUNDER_DECILES_V1"
        deciles_sha = _jsonl(
            directory / "compounder_deciles.jsonl", [stat.as_json() for stat in deciles]
        )
        contrast = _contrast(deciles)
        contrast_monthly, contrast_summary = contrast_series(executable, labels, split_of)
        diagnostics_doc = {
            "strategy_version": "COMPOUNDER_DECILES_V1",
            "score_version": COMPOUNDER_V1_2,
            "signal_version": SIGNAL_VERSION,
            "primary_horizon": PRIMARY_HORIZON,
            "deciles": DECILES,
            "minimum_cross_section": MIN_DECILE_CROSS_SECTION,
            "observations": len(executable),
            "months": len(decile_months),
            "months_cut": sum(1 for month in decile_months if month["cut"]),
            "months_skipped": skipped,
            "monthly": decile_months,
            "contrast": contrast,
            "contrast_monthly": contrast_monthly,
            "contrast_uncertainty": contrast_summary,
            "uncertainty_unit": "months; a security-month is not an independent draw",
            "information_coefficient": [item.as_json() for item in coefficients],
            "monthly_information_coefficient": monthly_ic,
            "score_distribution": [item.as_json() for item in distribution],
            "label_statuses_excluded_from_returns": [
                "TRUNCATED",
                "INTERRUPTED",
                "UNPRICED",
                "NO_ENTRY",
                "ABSENT",
            ],
        }
        diagnostics_sha = write_document(
            directory / "signal_diagnostics_compounder_v1.json", diagnostics_doc
        )
        fingerprints["compounder_deciles"] = deciles_sha
        fingerprints["signal_diagnostics"] = diagnostics_sha
        result.manifests["COMPOUNDER_DECILES_V1"] = _manifest(
            "COMPOUNDER_DECILES_V1",
            bindings,
            code_commit,
            [when.decision for when in calendar],
            split_doc,
            {
                "deciles": DECILES,
                "primary_horizon": PRIMARY_HORIZON,
                "score_version": COMPOUNDER_V1_2,
                "minimum_cross_section": MIN_DECILE_CROSS_SECTION,
            },
            {"model": "NOT_APPLICABLE", "basis_points_per_side": 0.0},
            {"deciles": deciles_sha, "diagnostics": diagnostics_sha},
            {"contrast": contrast},
            settings.benchmark_symbol,
            rebalance_rule="monthly cross-section; no portfolio",
        )
        write_document(decile_dir / "run_manifest.json", result.manifests["COMPOUNDER_DECILES_V1"])
        ledger.record(
            strategy="COMPOUNDER_DECILES_V1",
            mode="CROSS_SECTIONAL_SIGNAL_DIAGNOSTICS",
            why="the preregistered signal diagnostic",
            parameters={"deciles": DECILES, "horizon": PRIMARY_HORIZON},
            development=True,
            validation=True,
            holdout=True,
            variants_tried=1,
            parameter_sweeps=[],
            selection_criterion="NONE",
            outcome=contrast,
        )

        # --- audits ----------------------------------------------------------
        audit_engine = create_engine(f"sqlite:///{database}")
        try:
            with Session(audit_engine) as orm:
                audit = spot_audit(orm, db, settings, universe, rows, calendar, split)
        finally:
            audit_engine.dispose()
        audit_sha = write_document(directory / "spot_audit.json", audit)
        bias = bias_audit(rows, simulations, legs)
        bias_sha = write_document(directory / "bias_audit.json", bias)
        fingerprints["spot_audit"] = audit_sha
        fingerprints["bias_audit"] = bias_sha

        # --- manifests, summary, marker --------------------------------------
        schema = json.loads(
            (contract_dir / "backtest_run_manifest_schema.json").read_text(encoding="utf-8")
        )
        for strategy, manifest in result.manifests.items():
            problems = validate_manifest(manifest, schema)
            if problems:
                result.manifest_problems[strategy] = problems
            run_dir = directory / f"run_{strategy}"
            digest = write_document(run_dir / "run_manifest.json", manifest)
            fingerprints[f"{strategy}.run_manifest"] = digest
        for label, manifest in result.cost_manifests.items():
            strategy, _, cost_name = label.partition(".")
            problems = validate_manifest(manifest, schema)
            if problems:
                result.manifest_problems[label] = problems
            digest = write_document(
                directory / f"run_{strategy}" / f"run_manifest.{cost_name}.json", manifest
            )
            fingerprints[f"{label}.run_manifest"] = digest

        ledger_sha = digest_document(
            {
                "entries": [
                    json.loads(line)
                    for line in (directory / LEDGER_FILE).read_text(encoding="utf-8").splitlines()
                ]
            }
        )
        fingerprints["research_ledger"] = ledger_sha

        result.summary = _summary(
            bindings=bindings,
            settings=settings,
            calendar=calendar,
            split_doc=split_doc,
            coverage=coverage,
            rows=rows,
            simulations=simulations,
            passive=passive_metrics,
            diagnostics=diagnostics_doc,
            legs=legs,
            held_out=held_out,
            selection=selection,
            violations=result.violations,
            sessions=sessions,
        )
        summary_sha = write_document(directory / "r1_0_summary.json", result.summary)
        fingerprints["r1_0_summary"] = summary_sha
        result.fingerprints = dict(sorted(fingerprints.items()))
        result.marker = {
            "stage": "R1_0_COMPLETE",
            "phase": PHASE,
            "dataset_id": bindings.dataset_id,
            "contract_version": bindings.contract_version,
            "contract_bindings": {
                "universe": bindings.universe_fingerprint,
                "price_history": bindings.price_fingerprint,
                "historical_prices": bindings.historical_price_fingerprint,
                "forward_return_labels": bindings.label_fingerprint,
                "signal_observations": bindings.signal_fingerprint,
                "database": bindings.bindings.database_sha256,
            },
            "split_sha256": split_sha,
            "preregistration_sha256": prereg_sha,
            "baseline_registry_sha256": registry_sha,
            "strategy_versions": sorted(result.manifests),
            "cost_sensitivity_runs": sorted(result.cost_manifests),
            "run_manifest_sha256": {
                strategy: fingerprints[f"{strategy}.run_manifest"]
                for strategy in sorted(result.manifests)
            },
            "result_fingerprints": result.fingerprints,
            "holdout_evaluated": True,
            "holdout_evaluated_after_preregistration": True,
            "accounting_violations": len(result.violations),
            "manifest_problems": result.manifest_problems,
            "execution_rule": EXECUTION_RULE,
            "terminal_leg_policy": TERMINAL_LEG_POLICY,
            "metrics_reported": list(METRICS_REPORTED),
            "market_signals_v1_readiness": MARKET_SIGNALS_READINESS,
            "profitability_is_not_a_gate": True,
        }
        write_document(directory / "r1_0_complete.json", result.marker)
        write_document(directory / "r1_0_fingerprints.json", {"fingerprints": result.fingerprints})
    return result


def _slices(tasks: Sequence[dict[str, Any]], count: int) -> list[list[dict[str, Any]]]:
    """Cut the calendar into `count` contiguous, non-empty slices."""
    if count <= 1 or len(tasks) <= 1:
        return [list(tasks)]
    size = math.ceil(len(tasks) / count)
    return [list(tasks[start : start + size]) for start in range(0, len(tasks), size)]


def _terminal_session(db: sqlite3.Connection, last_decision: date) -> date:
    """The last valuation session: the end of the month after the last decision."""
    row = db.execute(
        "SELECT max(date) FROM benchmark_prices WHERE substr(date,1,7) = ?",
        (_next_month(last_decision),),
    ).fetchone()
    if row and row[0]:
        return date.fromisoformat(str(row[0]))
    latest = db.execute("SELECT max(date) FROM benchmark_prices").fetchone()
    return date.fromisoformat(str(latest[0]))


def _next_month(when: date) -> str:
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    return f"{year:04d}-{month:02d}"


def _contrast(deciles: Sequence[Any]) -> dict[str, Any]:
    """`D10 - D1` at the primary horizon, over the whole period and each block."""
    out: dict[str, Any] = {}
    for scope in sorted({stat.split for stat in deciles}):
        low = next(
            (
                stat
                for stat in deciles
                if stat.decile == 1 and stat.horizon == PRIMARY_HORIZON and stat.split == scope
            ),
            None,
        )
        high = next(
            (
                stat
                for stat in deciles
                if stat.decile == DECILES
                and stat.horizon == PRIMARY_HORIZON
                and stat.split == scope
            ),
            None,
        )
        out[scope] = {
            "d1_mean": None if low is None else low.mean,
            "d10_mean": None if high is None else high.mean,
            "d10_minus_d1": (
                None
                if low is None or high is None or low.mean is None or high.mean is None
                else high.mean - low.mean
            ),
        }
    return out


def _manifest(
    strategy: str,
    bindings: HarnessBindings,
    code_commit: str,
    sessions: Sequence[date],
    split_doc: Mapping[str, Any],
    parameters: Mapping[str, Any],
    cost: Mapping[str, Any],
    outputs: Mapping[str, str],
    metrics: Mapping[str, Any],
    benchmark: str,
    *,
    rebalance_rule: str,
) -> dict[str, Any]:
    return run_manifest(
        run_id=f"R1_0-{strategy}",
        strategy=strategy,
        code_commit=code_commit,
        dataset_id=bindings.dataset_id,
        contract_version=bindings.contract_version,
        universe_fingerprint=bindings.universe_fingerprint,
        feature_fingerprints={
            "signal_observations": bindings.signal_fingerprint,
            "pit_coverage": bindings.coverage_sha256,
            "pit_month_outcomes": bindings.outcomes_sha256,
        },
        price_fingerprint=bindings.price_fingerprint,
        label_fingerprint=bindings.label_fingerprint,
        rebalance_rule=rebalance_rule,
        cost=cost,
        date_range={
            "start": sessions[0].isoformat() if sessions else "",
            "end": sessions[-1].isoformat() if sessions else "",
        },
        split=split_doc.get("blocks", {}),
        parameters=parameters,
        benchmark=benchmark,
        outputs=outputs,
        metrics=metrics,
    )


def _summary(
    *,
    bindings: HarnessBindings,
    settings: Settings,
    calendar: Sequence[Any],
    split_doc: Mapping[str, Any],
    coverage: Sequence[MonthlyCoverage],
    rows: Sequence[CrossSectionRow],
    simulations: Mapping[str, SimulationResult],
    passive: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    legs: Mapping[tuple[str, int], TerminalLeg],
    held_out: Mapping[str, Mapping[str, int]],
    selection: Mapping[str, Sequence[Mapping[str, Any]]],
    violations: Sequence[str],
    sessions: Sequence[date],
) -> dict[str, Any]:
    """The one document a reader should be able to read on its own."""
    exclusions: dict[str, int] = defaultdict(int)
    universe_exclusions: dict[str, int] = defaultdict(int)
    for month in coverage:
        for reason, count in month.exclusions.items():
            exclusions[reason] += count
        for reason, count in month.universe_exclusions.items():
            universe_exclusions[reason] += count
    executable = [row for row in rows if row.stage == str(Stage.EXECUTABLE)]
    return {
        "phase": PHASE,
        "dataset_id": bindings.dataset_id,
        "contract_version": bindings.contract_version,
        "benchmark": settings.benchmark_symbol,
        "score_version": COMPOUNDER_V1_2,
        "signal_version": SIGNAL_VERSION,
        "decision_dates": len(calendar),
        "first_decision": calendar[0].decision.isoformat(),
        "last_decision": calendar[-1].decision.isoformat(),
        "valuation_window": {
            "start": sessions[0].isoformat(),
            "end": sessions[-1].isoformat(),
            "sessions": len(sessions),
        },
        "split": split_doc.get("blocks", {}),
        "sample": {
            "frozen_universe_rows": sum(month.frozen_universe_rows for month in coverage),
            "universe_observations": sum(month.universe for month in coverage),
            "company": sum(month.company for month in coverage),
            "historical": sum(month.historical for month in coverage),
            "feature_ready": sum(month.feature_ready for month in coverage),
            "score_ready": sum(month.score_ready for month in coverage),
            "rankable": sum(month.rankable for month in coverage),
            "tradable": sum(month.tradable for month in coverage),
            "executable": sum(month.executable for month in coverage),
            "executable_company": sum(1 for row in executable if row.kind == COMPANY),
            "executable_historical": sum(1 for row in executable if row.kind == HISTORICAL),
            "universe_exclusions": dict(sorted(universe_exclusions.items())),
            "exclusions": dict(sorted(exclusions.items())),
        },
        "terminal": {
            "identities_resolved": len(legs),
            "settled": sum(1 for leg in legs.values() if leg.state is TerminalState.SETTLED),
            "extinguished": sum(
                1 for leg in legs.values() if leg.state is TerminalState.EXTINGUISHED
            ),
            "unknown": sum(1 for leg in legs.values() if leg.state is TerminalState.UNKNOWN),
            "ongoing": sum(1 for leg in legs.values() if leg.state is TerminalState.ONGOING),
        },
        "empty_cross_sections": {name: dict(counts) for name, counts in held_out.items()},
        "participation": {
            name: {
                "rebalances": len(records),
                "eligible": sum(int(r["eligible"]) for r in records),
                "eligible_historical": sum(int(r["eligible_historical"]) for r in records),
                "selected": sum(int(r["selected"]) for r in records),
                "selected_historical": sum(int(r["selected_historical"]) for r in records),
                "selected_unpriced": sum(int(r["selected_unpriced"]) for r in records),
                "executed": sum(int(r.get("executed", 0)) for r in records),
                "executed_historical": sum(int(r.get("executed_historical", 0)) for r in records),
                "carried_off_target": sum(int(r.get("carried_off_target", 0)) for r in records),
                "held_cash_months": sum(1 for r in records if r["held_cash"] is not None),
            }
            for name, records in sorted(selection.items())
        },
        "results": {
            "BASELINE_PASSIVE_V1": passive["window"],
            **{
                name: {
                    "window": performance(sim.sessions, sim.nav).as_json(),
                    "rebalances": sim.rebalances,
                    "trades": len(sim.trades),
                    "diagnostics": sim.diagnostics,
                }
                for name, sim in sorted(simulations.items())
            },
        },
        "diagnostics": {
            "contrast": diagnostics.get("contrast"),
            "information_coefficient": [
                item
                for item in diagnostics.get("information_coefficient", [])
                if item.get("horizon") == PRIMARY_HORIZON
            ],
        },
        "market_signals_v1": {
            "readiness": MARKET_SIGNALS_READINESS,
            "canonical_standalone_ranking": CANONICAL_STANDALONE_RANKING,
            "baseline_executed": False,
            "reason": (
                "MARKET_SIGNALS_V1 emits multi-label setups, a three-state tradability verdict "
                "and peer percentiles. Its tradability verdict is used as the frozen readiness "
                "rule every portfolio here honours; it defines no scalar ranking, and none was "
                "invented."
            ),
        },
        "accounting_violations": list(violations),
        "limitations": _limitations(),
    }


def _limitations() -> list[str]:
    """Every frozen limitation a result here is read under. Never only in a README."""
    return [
        "sector and industry history is unavailable; no result is attributed to a sector, and "
        "the frozen COMPOUNDER_V1_2 reads an undated classification field internally, which is "
        "recorded per observation as classification_is_current",
        "exchange and listing membership is partly inferred; a MEMBER verdict is INFERRED",
        "market capitalisation is a lower bound where a share count is a cover-page figure",
        "average daily volume is single-venue (IEX) and partial; it is reported, never imposed",
        "historical-security fundamentals carry a conservative filed-date lag, and an identity "
        "with a filing dated on the decision session is excluded from that session",
        "no historical FX series exists in the frozen dataset, so currency-sensitive ratios are "
        "unavailable for foreign filers at every historical decision date, exactly as the live "
        "path degrades",
        "dividends are inside the vendor's adjusted basis rather than modelled as cash, so a "
        "portfolio return here is an adjusted-basis total return with implicit reinvestment",
        "spinoffs are unsupported",
        "a delisted security whose consideration nobody published carries an UNKNOWN terminal "
        "value; the result is reported beside a floor that values every such leg at zero",
        "there is no intraday model: the only decision points are the close and the next open",
        "the risk-free rate is zero because the frozen dataset holds no risk-free series",
    ]
