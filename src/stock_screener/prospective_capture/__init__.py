"""The prospective capture pipeline: frozen snapshots to a decision packet.

R1.11 certified an armed window and found it had a documented capture sequence
with no executor. This package is that executor. It turns the sealed prospective
source snapshots into the artifacts a decision actually needs — score inputs,
`COMPOUNDER_V1_2` scores, a ranked executable cross-section, an H2 target set —
and writes the one authoritative decision packet and its execution reference.

Three rules govern it:

- **One strategy implementation.** Selection goes through
  `membership_hypothesis`, scoring through `domain.score_company`. Nothing here
  reimplements a rule, a weight or a tie-break, because a prospective copy could
  drift from the frozen original without a single test failing.
- **Readiness means capability, not derivability.** A derived component is ready
  only once a producer has emitted and validated a fingerprinted artifact. No
  required component is ever ready with a null fingerprint.
- **Sealed before scored.** The six decision inputs are frozen before the first
  rank is computed, and no network call happens after that point. That is what
  makes the decision reproducible from the artifacts alone.
"""

from __future__ import annotations

from .actions import fetch_corporate_actions
from .calendar_binding import (
    AUTHORITATIVE_CALENDAR,
    CalendarBinding,
    CalendarBindingError,
    bind_calendar,
    session_digest,
)
from .crosssection import (
    CROSS_SECTION_ARTIFACT_ID,
    CrossSectionRow,
    ProspectiveCrossSection,
    ProspectiveStage,
    build_cross_section,
)
from .decision import (
    DECISION_FILE,
    DecisionCapture,
    DecisionRefusedError,
    capture_decision,
    prior_incumbents,
)
from .execution import (
    EXECUTION_FILE,
    ExecutionCapture,
    ExecutionRefusedError,
    capture_execution_reference,
    derive_execution_session,
)
from .guards import FORBIDDEN_FUNDAMENTAL_NAMES, GuardViolationError, audit_package
from .identity import IDENTITY_EXCLUSIONS, ProspectiveIdentity, resolve_identities
from .observations import (
    OBSERVATION_EXCLUSIONS,
    admitted_accessions_by_ticker,
    fetch_filing_observations,
)
from .package import (
    MANIFEST_FILE,
    POST_CLOSE_SOURCES,
    PREDECISION_SOURCES,
    PackageError,
    SealedPackage,
    complete_post_close,
    load_package,
    seal_predecision,
)
from .provenance import (
    PROVENANCE_AUDIT_ID,
    ProvenanceViolationError,
)
from .provenance import audit as audit_fundamental_provenance
from .readiness import (
    DERIVED_COMPONENTS,
    READINESS_KIND,
    SOURCE_COMPONENTS,
    CaptureReadiness,
    ComponentState,
    ReadinessKind,
    assess_capture_readiness,
    require_six_fingerprints,
)
from .score_inputs import (
    KNOWN_DEGRADATIONS,
    SCORE_INPUT_ARTIFACT_ID,
    ScoreInput,
    ScoreInputSet,
    build_score_inputs,
)
from .scoring import (
    SCORE_ARTIFACT_ID,
    ProspectiveScore,
    ProspectiveScoreSet,
    benchmark_from_bars,
    score_cross_section,
)

__all__ = [
    "AUTHORITATIVE_CALENDAR",
    "CROSS_SECTION_ARTIFACT_ID",
    "DECISION_FILE",
    "DERIVED_COMPONENTS",
    "EXECUTION_FILE",
    "FORBIDDEN_FUNDAMENTAL_NAMES",
    "IDENTITY_EXCLUSIONS",
    "KNOWN_DEGRADATIONS",
    "MANIFEST_FILE",
    "OBSERVATION_EXCLUSIONS",
    "POST_CLOSE_SOURCES",
    "PREDECISION_SOURCES",
    "PROVENANCE_AUDIT_ID",
    "READINESS_KIND",
    "SCORE_ARTIFACT_ID",
    "SCORE_INPUT_ARTIFACT_ID",
    "SOURCE_COMPONENTS",
    "CalendarBinding",
    "CalendarBindingError",
    "CaptureReadiness",
    "ComponentState",
    "CrossSectionRow",
    "DecisionCapture",
    "DecisionRefusedError",
    "ExecutionCapture",
    "ExecutionRefusedError",
    "GuardViolationError",
    "PackageError",
    "ProspectiveCrossSection",
    "ProspectiveIdentity",
    "ProspectiveScore",
    "ProspectiveScoreSet",
    "ProspectiveStage",
    "ProvenanceViolationError",
    "ReadinessKind",
    "ScoreInput",
    "ScoreInputSet",
    "SealedPackage",
    "admitted_accessions_by_ticker",
    "assess_capture_readiness",
    "audit_fundamental_provenance",
    "audit_package",
    "benchmark_from_bars",
    "bind_calendar",
    "build_cross_section",
    "build_score_inputs",
    "capture_decision",
    "capture_execution_reference",
    "complete_post_close",
    "derive_execution_session",
    "fetch_corporate_actions",
    "fetch_filing_observations",
    "load_package",
    "prior_incumbents",
    "require_six_fingerprints",
    "resolve_identities",
    "score_cross_section",
    "seal_predecision",
    "session_digest",
]
