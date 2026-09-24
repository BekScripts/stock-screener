"""R1.1 — interpreting the R1.0 baseline, and deciding what one experiment follows.

R1.0 built a harness and ran four preregistered baselines. It produced numbers.
This package asks the only questions that matter about those numbers before
anyone builds anything else on them:

- is the cross-sectional signal broad, persistent and stable, or is it one
  quarter, one regime and a handful of names wearing a summary statistic?
- what population does the result actually describe, once the funnel from
  164,193 universe observations to 39,001 tradable ones is accounted for?
- which limitations are load-bearing rather than decorative?
- and given all of that, is exactly one further hypothesis-driven experiment
  warranted — and what would falsify it?

Nothing here executes a strategy, searches a parameter, combines a signal or
touches the frozen score. The classification rubric and the hypothesis's
falsification criteria are written as code in `review` before the diagnostics
that feed them are computed, and the one forward-looking artifact — a frozen
specification for R1.2 — refuses to exist unless its hypothesis survived.

One fact is recorded in the open rather than left implicit: R1.0 legitimately
read the 2025-01…2026-03 interval once, under its own preregistration. That
interval is therefore **not** an untouched holdout for anything derived
afterwards, and every artifact here says so.
"""

from stock_screener.signal_review.binding import (
    DOCUMENT_FINGERPRINTS,
    LEDGER_FINGERPRINTS,
    R1_0_DIR,
    R1Binding,
    ReviewBindingError,
    bind_r1_0,
    cross_section_fingerprint,
    file_sha256,
    ledger_fingerprint,
    read_jsonl,
)
from stock_screener.signal_review.portfolio_review import (
    ROLLING_SESSIONS,
    IdentityPnL,
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
    CLASSIFICATIONS,
    HOLDOUT_STATUS,
    RUBRIC,
    classification_limitation,
    classify_signal,
    evaluate_h1,
    experiment_spec,
    holdout_status,
    hypothesis_record,
    sample_attrition,
    score_floor_audit,
)
from stock_screener.signal_review.run import (
    MIRROR_GENERATION,
    MIRROR_PREFIX,
    PHASE,
    REVIEW_DIR,
    ReviewResult,
    mirror_r1_1,
    r1_0_directory,
    review_r1_0,
)
from stock_screener.signal_review.signal import (
    HORIZON_OVERLAP,
    MonthlySignal,
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
from stock_screener.signal_review.statistics import (
    bootstrap_mean,
    quantile,
    spearman,
    spread,
    std,
)

__all__ = [
    "CLASSIFICATIONS",
    "DOCUMENT_FINGERPRINTS",
    "HOLDOUT_STATUS",
    "HORIZON_OVERLAP",
    "LEDGER_FINGERPRINTS",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "R1_0_DIR",
    "REVIEW_DIR",
    "ROLLING_SESSIONS",
    "RUBRIC",
    "IdentityPnL",
    "MonthlySignal",
    "R1Binding",
    "ReviewBindingError",
    "ReviewResult",
    "adv_bucket_diagnostics",
    "bind_r1_0",
    "bootstrap_mean",
    "classification_limitation",
    "classify_signal",
    "concentration",
    "cost_break_even",
    "cross_section_fingerprint",
    "cross_section_size_effect",
    "decile_table",
    "drawdown_episodes",
    "evaluate_h1",
    "experiment_spec",
    "file_sha256",
    "holding_concentration",
    "holdout_status",
    "horizon_diagnostics",
    "hypothesis_record",
    "identity_contributions",
    "leave_one_out",
    "ledger_fingerprint",
    "mirror_r1_1",
    "month_contributions",
    "monthly_signal",
    "quantile",
    "r1_0_directory",
    "rank_persistence",
    "read_jsonl",
    "relative_path",
    "return_distribution",
    "review_r1_0",
    "rolling_performance",
    "sample_attrition",
    "score_floor_audit",
    "score_threshold_stability",
    "spearman",
    "spread",
    "std",
    "subset_diagnostics",
    "turnover_sources",
]
