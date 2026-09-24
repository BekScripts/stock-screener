"""R1.0 — the leakage-safe backtest harness over the frozen research dataset.

The evidence programme is closed and `PIT_RESEARCH_DATASET_V2` is frozen under
`BACKTEST_DATA_CONTRACT_V1`. This package is the first thing built on top of
it: a harness that can take a monthly decision from what was knowable at the
time, execute it at the next session's open, account for the cash and the
shares that result, and evaluate the outcome against frozen labels — without
at any point letting the future reach backwards.

The stages are separate modules because they are separate concerns, and mixing
them is how a backtest becomes a story:

    binding        prove every frozen input, or refuse to run
    sessions       when decisions happen, and the split frozen before results
    universe       who was in the month, read from frozen evidence
    crosssection   what the decision saw: score, tradability, execution price
    portfolio      cash, shares, terminal legs, costs
    performance    the metric set, fixed in advance
    diagnostics    deciles and rank IC against the frozen labels
    registry       the registry, the preregistration, the ledger, the manifests
    audit          a hash-chosen spot audit and a post-hoc bias audit
    run            the one order all of that may be taken in

Two rules run through all of it. The decision path never reads a forward
return — the label layer is not imported by `crosssection` or `portfolio`, and
a test holds that statically. And nothing is dropped silently: every identity
that does not reach a portfolio is a row carrying the reason it stopped.
"""

from stock_screener.backtest_harness.audit import bias_audit, spot_audit
from stock_screener.backtest_harness.binding import (
    COVERAGE_FILE,
    OUTCOMES_FILE,
    BindingError,
    HarnessBindings,
    file_sha256,
    verify_bindings,
)
from stock_screener.backtest_harness.crosssection import (
    CrossSectionRow,
    MonthlyCoverage,
    Stage,
    cross_section,
    write_cross_sections,
)
from stock_screener.backtest_harness.diagnostics import (
    DECILES,
    HORIZONS,
    MIN_DECILE_CROSS_SECTION,
    PRIMARY_HORIZON,
    DecileStat,
    InformationCoefficient,
    ScoreDistribution,
    assign_deciles,
    decile_diagnostics,
    load_labels,
    rank_information_coefficient,
    score_distribution,
)
from stock_screener.backtest_harness.performance import (
    PerformanceMetrics,
    benchmark_path,
    performance,
    window,
)
from stock_screener.backtest_harness.portfolio import (
    GROSS,
    SHARE_POLICY,
    SIMPLE_10BPS_PER_SIDE,
    STARTING_CAPITAL,
    TERMINAL_LEG_POLICY,
    CostModel,
    Holding,
    PriceBook,
    Rebalance,
    SimulationResult,
    TerminalLeg,
    TerminalState,
    Trade,
    simulate,
)
from stock_screener.backtest_harness.registry import (
    BASELINES,
    PreregistrationError,
    ResearchLedger,
    baseline_registry,
    digest_document,
    first_holdout_access,
    preregistration,
    run_manifest,
    validate_manifest,
)
from stock_screener.backtest_harness.run import (
    MIRROR_GENERATION,
    MIRROR_PREFIX,
    PHASE,
    RESEARCH_DIR,
    R1Result,
    mirror_r1_0,
    run_r1_0,
)
from stock_screener.backtest_harness.sessions import (
    EXECUTION_RULE,
    SPLIT_VERSION,
    CalendarError,
    DecisionDate,
    ResearchSplit,
    SplitName,
    build_split,
    decision_calendar,
    load_split,
    write_split,
)
from stock_screener.backtest_harness.universe import (
    PitUniverse,
    UniverseExclusion,
    UniverseObservation,
)

__all__ = [
    "BASELINES",
    "COVERAGE_FILE",
    "DECILES",
    "EXECUTION_RULE",
    "GROSS",
    "HORIZONS",
    "MIN_DECILE_CROSS_SECTION",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "OUTCOMES_FILE",
    "PHASE",
    "PRIMARY_HORIZON",
    "RESEARCH_DIR",
    "SHARE_POLICY",
    "SIMPLE_10BPS_PER_SIDE",
    "SPLIT_VERSION",
    "STARTING_CAPITAL",
    "TERMINAL_LEG_POLICY",
    "BindingError",
    "CalendarError",
    "CostModel",
    "CrossSectionRow",
    "DecileStat",
    "DecisionDate",
    "HarnessBindings",
    "Holding",
    "InformationCoefficient",
    "MonthlyCoverage",
    "PerformanceMetrics",
    "PitUniverse",
    "PreregistrationError",
    "PriceBook",
    "R1Result",
    "Rebalance",
    "ResearchLedger",
    "ResearchSplit",
    "ScoreDistribution",
    "SimulationResult",
    "SplitName",
    "Stage",
    "TerminalLeg",
    "TerminalState",
    "Trade",
    "UniverseExclusion",
    "UniverseObservation",
    "assign_deciles",
    "baseline_registry",
    "benchmark_path",
    "bias_audit",
    "build_split",
    "cross_section",
    "decile_diagnostics",
    "decision_calendar",
    "digest_document",
    "file_sha256",
    "first_holdout_access",
    "load_labels",
    "load_split",
    "mirror_r1_0",
    "performance",
    "preregistration",
    "rank_information_coefficient",
    "run_manifest",
    "run_r1_0",
    "score_distribution",
    "simulate",
    "spot_audit",
    "validate_manifest",
    "verify_bindings",
    "window",
    "write_cross_sections",
    "write_split",
]
