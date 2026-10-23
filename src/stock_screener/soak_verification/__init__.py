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

from .aggregate import DerivedSoakEvidence, evidence_for_store
from .contracts import (
    ACTIVATION_FAMILY,
    PROTOCOL_FAMILY,
    PaperShadowSoakActivation,
    PaperShadowSoakProtocol,
    canonical_gate_ids,
    canonical_thresholds,
)
from .derivations import (
    DERIVATION_REGISTRY_VERSION,
    Derivation,
    DerivationContext,
    DerivedMetric,
    GateScope,
    MetricStatus,
    derivation_registry,
    derivation_registry_fingerprint,
)
from .drills_v2 import (
    DRILL_EVIDENCE_V2_VERSION,
    REQUIRED_DRILL_TYPES,
    DrillRecordV2,
    DrillStoreV2,
    DrillTypeV2,
    admissible_drills,
)
from .errors import Code, SoakContractError
from .evidence_contract import (
    EVIDENCE_CONTRACT_VERSION,
    GateEvidenceBinding,
    SoakGateEvidenceContract,
    evidence_contract,
    evidence_contract_fingerprint,
    require_evidence_contract,
)
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
from .observations import (
    SESSION_OBSERVATION_V3_VERSION,
    DerivedSessionObservation,
    observation_from_body,
)
from .preflight import (
    PREFLIGHT_VERSION,
    SoakPreflightReport,
    preflight_protocol,
    run_soak_preflight,
)
from .scorer import SCORER_VERSION, SoakScore, SoakVerdictV2, score_soak
from .session_evidence import (
    SESSION_GATE_EVIDENCE_VERSION,
    SessionGateEvidence,
    derive_session_evidence,
)
from .sources import (
    SOURCE_SCHEMA_VERSION,
    SOURCE_SCHEMAS,
    OperationalRecords,
    SourceRead,
    SourceSchema,
    known_source_schema,
)
from .store import (
    RESERVED_NAMESPACES,
    SOAK_ROOT_DIRECTORY,
    VersionedSoakStore,
    validate_namespace,
)
from .synthetic import (
    SYNTHETIC_RECORD_BUILDER_VERSION,
    Breach,
    SoakFixture,
    SyntheticSession,
    build_soak_fixture,
    write_session_records,
)

__all__ = [
    "ACTIVATION_FAMILY",
    "DERIVATION_REGISTRY_VERSION",
    "DRILL_EVIDENCE_V2_VERSION",
    "EVIDENCE_CONTRACT_VERSION",
    "GATE_REGISTRY_VERSION",
    "PREFLIGHT_VERSION",
    "PROTOCOL_FAMILY",
    "REQUIRED_DRILL_TYPES",
    "RESERVED_NAMESPACES",
    "SCORER_VERSION",
    "SESSION_GATE_EVIDENCE_VERSION",
    "SESSION_OBSERVATION_V3_VERSION",
    "SOAK_ROOT_DIRECTORY",
    "SOURCE_SCHEMAS",
    "SOURCE_SCHEMA_VERSION",
    "SYNTHETIC_RECORD_BUILDER_VERSION",
    "Breach",
    "Code",
    "Derivation",
    "DerivationContext",
    "DerivedMetric",
    "DerivedSessionObservation",
    "DerivedSoakEvidence",
    "DrillRecordV2",
    "DrillStoreV2",
    "DrillTypeV2",
    "EvidenceKind",
    "GateBound",
    "GateClass",
    "GateDefinition",
    "GateEvidenceBinding",
    "GateOutcome",
    "GateScope",
    "MetricStatus",
    "OperationalRecords",
    "PaperShadowSoakActivation",
    "PaperShadowSoakProtocol",
    "SessionGateEvidence",
    "SoakContractError",
    "SoakEvidence",
    "SoakFixture",
    "SoakGateEvidenceContract",
    "SoakPreflightReport",
    "SoakScore",
    "SoakVerdictV2",
    "SourceRead",
    "SourceSchema",
    "SyntheticSession",
    "VersionedSoakStore",
    "admissible_drills",
    "build_soak_fixture",
    "canonical_gate_ids",
    "canonical_thresholds",
    "derivation_registry",
    "derivation_registry_fingerprint",
    "derive_session_evidence",
    "evidence_contract",
    "evidence_contract_fingerprint",
    "evidence_for_store",
    "gate_registry",
    "known_source_schema",
    "observation_from_body",
    "preflight_protocol",
    "registry_fingerprint",
    "require_bijection",
    "require_evidence_contract",
    "run_soak_preflight",
    "score_soak",
    "validate_namespace",
    "write_session_records",
]
