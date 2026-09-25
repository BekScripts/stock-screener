"""R1.7 — the prospective H2 shadow runner.

The package activates the frozen protocol, arms itself before the first
qualifying decision, and waits. It captures nothing in this phase: ending armed
with zero observations and a sealed verdict is the successful outcome.

It is structurally incapable of trading. `order_capability_reachable` scans this
package's own source for anything resembling an order and `bind_runner` refuses
before changing any state if it finds one.
"""

from __future__ import annotations

from .binding import (
    FORBIDDEN_CAPABILITIES,
    RUNNER_VERSION,
    RunnerBinding,
    RunnerBindingError,
    bind_runner,
    order_capability_reachable,
)
from .calendar import (
    CALENDAR_ID,
    SESSION_TIMEZONE,
    calendar_descriptor,
    is_session,
    next_session,
    previous_session,
    quarter_end_session,
    upcoming_quarter_ends,
    verify_against_history,
)
from .fingerprint import SEMANTIC_MODULES, compare, semantic_fingerprint
from .packets import (
    PacketConflictError,
    correction_record,
    decision_packet,
    execution_packet,
    finalise,
    packet_digest,
)
from .runner import (
    ACTIVATION_FILE,
    ARM_FILE,
    CRITERIA_SEAL,
    LEDGER_FILE,
    MIRROR_PREFIX,
    MIRRORED_FILES,
    PROSPECTIVE_ROOT,
    STATE_FILE,
    ActivationError,
    activate,
    arm,
    derive_first_eligible_decision,
    record_incident,
    status,
)
from .selection import ordinary_target_count, paired_targets
from .state import (
    LEGAL_TRANSITIONS,
    ProspectiveState,
    RunnerState,
    StateError,
    load_state,
    save_state,
)

__all__ = [
    "ACTIVATION_FILE",
    "ARM_FILE",
    "CALENDAR_ID",
    "CRITERIA_SEAL",
    "FORBIDDEN_CAPABILITIES",
    "LEDGER_FILE",
    "LEGAL_TRANSITIONS",
    "MIRRORED_FILES",
    "MIRROR_PREFIX",
    "PROSPECTIVE_ROOT",
    "RUNNER_VERSION",
    "SEMANTIC_MODULES",
    "SESSION_TIMEZONE",
    "STATE_FILE",
    "ActivationError",
    "PacketConflictError",
    "ProspectiveState",
    "RunnerBinding",
    "RunnerBindingError",
    "RunnerState",
    "StateError",
    "activate",
    "arm",
    "bind_runner",
    "calendar_descriptor",
    "compare",
    "correction_record",
    "decision_packet",
    "derive_first_eligible_decision",
    "execution_packet",
    "finalise",
    "is_session",
    "load_state",
    "next_session",
    "order_capability_reachable",
    "ordinary_target_count",
    "packet_digest",
    "paired_targets",
    "previous_session",
    "prospective_root",
    "quarter_end_session",
    "record_incident",
    "save_state",
    "semantic_fingerprint",
    "status",
    "upcoming_quarter_ends",
    "verify_against_history",
]


def prospective_root(control_root: object) -> object:
    """Where prospective runtime state lives beneath a control root.

    Args:
        control_root: The control plane root.

    Returns:
        The prospective state directory, kept out of `research/r1_*`.
    """
    from pathlib import Path

    return Path(str(control_root)) / PROSPECTIVE_ROOT
