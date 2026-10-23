"""R1.4 — the H2 membership-stability hypothesis, preregistered and unrun.

The package defines exactly one hypothesis and exactly one rule,
`ONE_DECILE_HYSTERESIS_V1`, and freezes the criteria and the aggregate rule that
will judge it before anything is measured. It executes nothing: no simulator and
no pricing path is importable from any module here, and `simulation_reachable`
proves that by reading this package's own source at bind time.
"""

from __future__ import annotations

from .binding import (
    FORBIDDEN_IMPORTS,
    R1_3_EXPECTED,
    R1_4_DIR,
    SINGULAR_FIELDS,
    H2Binding,
    H2BindingError,
    bind_r1_3,
    simulation_reachable,
    verify_h2,
)
from .record import (
    AGGREGATE_RULE,
    COMPARATOR,
    CRITERIA,
    H2_ID,
    HISTORICAL_STATUS,
    LEGACY_H1_TURNOVER_REFERENCE,
    PROHIBITED_VARIATIONS,
    SPEC_STATUS,
    experiment_spec,
    hypothesis_record,
)
from .rule import (
    ENTRY_WIDTHS,
    RETENTION_WIDTHS,
    RULE,
    RULE_VERSION,
    Candidate,
    Selection,
    rank,
    retention_limit,
    select,
)
from .run import (
    MIRROR_PREFIX,
    PHASE,
    PREREGISTRATION_DIR,
    PreregistrationResult,
    mirror_r1_4,
    preregister_h2,
    r1_3_directory,
)

__all__ = [
    "AGGREGATE_RULE",
    "COMPARATOR",
    "CRITERIA",
    "ENTRY_WIDTHS",
    "FORBIDDEN_IMPORTS",
    "H2_ID",
    "HISTORICAL_STATUS",
    "LEGACY_H1_TURNOVER_REFERENCE",
    "MIRROR_PREFIX",
    "PHASE",
    "PREREGISTRATION_DIR",
    "PROHIBITED_VARIATIONS",
    "R1_3_EXPECTED",
    "R1_4_DIR",
    "RETENTION_WIDTHS",
    "RULE",
    "RULE_VERSION",
    "SINGULAR_FIELDS",
    "SPEC_STATUS",
    "Candidate",
    "H2Binding",
    "H2BindingError",
    "PreregistrationResult",
    "Selection",
    "bind_r1_3",
    "experiment_spec",
    "hypothesis_record",
    "mirror_r1_4",
    "preregister_h2",
    "r1_3_directory",
    "rank",
    "retention_limit",
    "select",
    "simulation_reachable",
    "verify_h2",
]
