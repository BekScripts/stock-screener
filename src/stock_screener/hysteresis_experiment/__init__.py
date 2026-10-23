"""R1.5 — the single preregistered H2 exploratory historical experiment.

The package executes `ONE_DECILE_HYSTERESIS_V1` exactly once, against the
comparator's own frozen cross-sections, and judges it by the four criteria R1.4
fixed before anything was measured. The mechanism verdict is computed before any
return is read and by a function that takes no performance argument, so its
independence is structural.
"""

from __future__ import annotations

from .binding import (
    EXECUTION_MARKER,
    EXPERIMENT_DIR,
    STRATEGY,
    ExecutionLock,
    ExperimentBinding,
    bind_r1_5,
)
from .evaluate import CriterionResult, evaluate, interpret
from .run import (
    LEGACY_BLOCKS,
    MIRROR_PREFIX,
    PHASE,
    ExperimentResult,
    mirror_r1_5,
    r1_4_directory,
    run_h2,
)
from .selection import Decision, SelectionError, build_decisions, ordinary_top_decile

__all__ = [
    "EXECUTION_MARKER",
    "EXPERIMENT_DIR",
    "LEGACY_BLOCKS",
    "MIRROR_PREFIX",
    "PHASE",
    "STRATEGY",
    "CriterionResult",
    "Decision",
    "ExecutionLock",
    "ExperimentBinding",
    "ExperimentResult",
    "SelectionError",
    "bind_r1_5",
    "build_decisions",
    "evaluate",
    "interpret",
    "mirror_r1_5",
    "ordinary_top_decile",
    "r1_4_directory",
    "run_h2",
]
