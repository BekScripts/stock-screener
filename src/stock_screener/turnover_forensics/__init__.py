"""R1.3 — forensic review of the turnover premise H1 failed.

The package explains one number and produces no strategy. It reads R1.0's,
R1.1's and R1.2's frozen records, re-derives every digest before reading any
value, and writes only descriptions of what it found. No simulator is importable
from any module here, and `simulation_reachable` proves that by reading this
package's own source at bind time.
"""

from __future__ import annotations

from .binding import (
    FORBIDDEN_IMPORTS,
    R1_3_DIR,
    ForensicBinding,
    ForensicBindingError,
    bind_r1_2,
    simulation_reachable,
)
from .forensic import (
    H1_STATUSES,
    MECHANISMS,
    THRESHOLD_CLASSES,
    classify,
    h1_status,
    next_dimension,
    threshold_provenance,
)
from .membership import (
    CAUSE_BY_EXCLUSION,
    DISTANCE_BANDS,
    RANK_MOVE_BANDS,
    cause_of,
    holding_episodes,
    membership_half_life,
    portfolios_by_decision,
    retention,
    survival_curve,
    transitions,
)
from .run import (
    FORENSIC_DIR,
    FROZEN_THRESHOLD,
    MIRROR_PREFIX,
    PHASE,
    ForensicResult,
    mirror_r1_3,
    r1_2_directory,
    run_forensics,
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
    RebalanceTurnover,
    Reconstruction,
    counterfactual,
    decomposition,
    lower_bound,
    pre_trade_nav,
    reconstruct,
    required_reduction,
)

__all__ = [
    "CAUSE_BY_EXCLUSION",
    "DISTANCE_BANDS",
    "FORBIDDEN_IMPORTS",
    "FORENSIC_DIR",
    "FROZEN_THRESHOLD",
    "H1_STATUSES",
    "MECHANISMS",
    "MIRROR_PREFIX",
    "PHASE",
    "R1_3_DIR",
    "RANK_MOVE_BANDS",
    "THRESHOLD_CLASSES",
    "TURNOVER_DEFINITION",
    "ForensicBinding",
    "ForensicBindingError",
    "ForensicResult",
    "RebalanceTurnover",
    "Reconstruction",
    "bind_r1_2",
    "boundary_churn",
    "cause_of",
    "classify",
    "counterfactual",
    "cutoffs",
    "decomposition",
    "dispersion",
    "h1_status",
    "holding_episodes",
    "lower_bound",
    "membership_half_life",
    "mirror_r1_3",
    "next_dimension",
    "portfolios_by_decision",
    "pre_trade_nav",
    "r1_2_directory",
    "rank_percentiles",
    "reconstruct",
    "required_reduction",
    "retention",
    "run_forensics",
    "score_stability",
    "simulation_reachable",
    "size_effect",
    "stability",
    "survival_curve",
    "target_count_effect",
    "threshold_provenance",
    "transitions",
]
