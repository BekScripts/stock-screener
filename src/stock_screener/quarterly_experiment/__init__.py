"""R1.2 — executing the one experiment R1.1 froze, and judging it by its own criteria.

R1.1 wrote a specification and did not run it. This package runs exactly that
specification and nothing else: one frequency, one selection fraction, one
weighting, one execution rule, one cost level, each asserted against the frozen
document before the first trade is simulated. A specification that says
anything different stops the run.

The design choice that makes the comparison honest is structural. A quarter's
last benchmark session is also that month's last session, so every quarterly
decision date is one of R1.0's monthly decision dates. The quarterly
cross-sections are therefore **sliced out of R1.0's frozen ledger** rather than
recomputed — identical identities, scores, tradability verdicts and execution
prices — and the monthly comparator is R1.0's own NAV and trade ledgers
restricted to the quarterly strategy's sessions. Only the rebalance frequency
differs, which is the single thing H1 is about.

Two things this package will not do. It will not combine the five falsification
criteria into an aggregate verdict, because the frozen specification states no
rule for doing so and inventing one after seeing the measurements is exactly
what preregistration exists to prevent. And it will not describe the
2025-01…2026-03 block as confirmation: R1.0 read that interval before H1 was
written, and every artifact here says so.
"""

from stock_screener.quarterly_experiment.binding import (
    EXPECTED_RULES,
    MONTHLY_TURNOVER_REFERENCE,
    R1_1_DIR,
    ExperimentBinding,
    ExperimentBindingError,
    bind_experiment,
)
from stock_screener.quarterly_experiment.calendar import (
    QuarterEnd,
    QuarterlyCalendarError,
    quarter_of,
    quarterly_calendar,
    quarters_by_block,
    sessions_between,
    terminal_session,
)
from stock_screener.quarterly_experiment.evaluate import (
    OUTCOMES,
    CriterionResult,
    evaluate_criteria,
    interpret,
)
from stock_screener.quarterly_experiment.run import (
    EXPERIMENT_DIR,
    MIRROR_GENERATION,
    MIRROR_PREFIX,
    PHASE,
    PRIMARY_HORIZON,
    STRATEGY,
    ExperimentResult,
    mirror_r1_2,
    r1_1_directory,
    run_experiment,
)

__all__ = [
    "EXPECTED_RULES",
    "EXPERIMENT_DIR",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "MONTHLY_TURNOVER_REFERENCE",
    "OUTCOMES",
    "PHASE",
    "PRIMARY_HORIZON",
    "R1_1_DIR",
    "STRATEGY",
    "CriterionResult",
    "ExperimentBinding",
    "ExperimentBindingError",
    "ExperimentResult",
    "QuarterEnd",
    "QuarterlyCalendarError",
    "bind_experiment",
    "evaluate_criteria",
    "interpret",
    "mirror_r1_2",
    "quarter_of",
    "quarterly_calendar",
    "quarters_by_block",
    "r1_1_directory",
    "run_experiment",
    "sessions_between",
    "terminal_session",
]
