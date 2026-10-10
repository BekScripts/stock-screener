"""Verifiable soak scoring, bound drill evidence, and the pre-arming self-check.

This package exists because `PAPER_SHADOW_SOAK_PROTOCOL_V2` was frozen, tagged
and armed against a real paper account while declaring twenty-two gates that no
code could evaluate. The protocol was unscoreable from the moment it was sealed,
and nothing objected, because a gate declared as a string looks exactly like a
gate that works.

Three pieces close that:

- `gates` binds every gate id to an evaluator and refuses a protocol whose
  declared gates and available evaluators do not correspond exactly, in both
  directions.
- `scorer` evaluates a session-based soak mechanically into one of four
  verdicts. No prose, no thresholds passed in, no pass rate.
- `preflight` proves, before arming, that the gates have evaluators, that the
  store can supply the evidence, that the required drills are recordable, and
  that all four verdicts — `A` included — are mechanically reachable.

`drills_v2` is the fourth: drill evidence that binds the protocol, the
qualifying commit, the operations engine and the relevant policies, so a record
can prove which system it tested.

This package evaluates and records evidence. It contains no strategy, no risk,
no accounting mutation and no broker call, and a static test asserts each
absence.
"""

from __future__ import annotations

from .drills_v2 import (
    DRILL_EVIDENCE_V2_VERSION,
    REQUIRED_DRILL_TYPES,
    DrillRecordV2,
    DrillStoreV2,
    DrillTypeV2,
    admissible_drills,
)
from .errors import Code, SoakContractError
from .gates import (
    GATE_REGISTRY_VERSION,
    EvidenceKind,
    GateBound,
    GateClass,
    GateDefinition,
    GateOutcome,
    SoakEvidence,
    gate_registry,
    registry_fingerprint,
    require_bijection,
)
from .preflight import PREFLIGHT_VERSION, SoakPreflightReport, run_soak_preflight
from .scorer import SCORER_VERSION, SoakScore, SoakVerdictV2, score_soak

__all__ = [
    "DRILL_EVIDENCE_V2_VERSION",
    "GATE_REGISTRY_VERSION",
    "PREFLIGHT_VERSION",
    "REQUIRED_DRILL_TYPES",
    "SCORER_VERSION",
    "Code",
    "DrillRecordV2",
    "DrillStoreV2",
    "DrillTypeV2",
    "EvidenceKind",
    "GateBound",
    "GateClass",
    "GateDefinition",
    "GateOutcome",
    "SoakContractError",
    "SoakEvidence",
    "SoakPreflightReport",
    "SoakScore",
    "SoakVerdictV2",
    "admissible_drills",
    "gate_registry",
    "registry_fingerprint",
    "require_bijection",
    "run_soak_preflight",
    "score_soak",
]
