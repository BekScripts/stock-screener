"""R1.9 — the prospective source plane and the missed-observation disposition.

The package supplies the six as-of-bounded inputs a prospective decision packet
needs, an immutable store for the snapshots behind them, a readiness gate that
is conjunctive rather than indulgent, and the governance record that settles
what the frozen protocol says about the quarter R1.8 could not capture.
"""

from __future__ import annotations

from .adapters import (
    NORMALIZER_VERSION,
    SESSION_TIMEZONE,
    SourceResult,
    fetch_market,
    fetch_sec_availability,
    fetch_universe,
    session_close,
)
from .envelope import (
    Completeness,
    SnapshotEnvelope,
    StaleSourceError,
    admissible_for,
    content_digest,
    request_fingerprint,
)
from .governance import (
    DISPOSITIONS,
    PROTOCOL_V2_ID,
    audit_v1,
    disposition_record,
    protocol_v2,
)
from .readiness import (
    FRESHNESS_POLICY,
    REQUIRED_INPUTS,
    ReadinessReport,
    assess,
    preflight,
)
from .run import (
    CANARY_CLASSIFICATION,
    MIRROR_PREFIX,
    PHASE,
    READINESS_DIR,
    ReadinessResult,
    build_source_plane,
    mirror_r1_9,
    run_canary,
)
from .store import (
    FORBIDDEN_PREFIXES,
    PROSPECTIVE_STORE_ROOT,
    SnapshotStore,
    StoreConflictError,
    refuses_historical_namespace,
)

__all__ = [
    "CANARY_CLASSIFICATION",
    "DISPOSITIONS",
    "FORBIDDEN_PREFIXES",
    "FRESHNESS_POLICY",
    "MIRROR_PREFIX",
    "NORMALIZER_VERSION",
    "PHASE",
    "PROSPECTIVE_STORE_ROOT",
    "PROTOCOL_V2_ID",
    "READINESS_DIR",
    "REQUIRED_INPUTS",
    "SESSION_TIMEZONE",
    "Completeness",
    "ReadinessReport",
    "ReadinessResult",
    "SnapshotEnvelope",
    "SnapshotStore",
    "SourceResult",
    "StaleSourceError",
    "StoreConflictError",
    "admissible_for",
    "assess",
    "audit_v1",
    "build_source_plane",
    "content_digest",
    "disposition_record",
    "fetch_market",
    "fetch_sec_availability",
    "fetch_universe",
    "mirror_r1_9",
    "preflight",
    "protocol_v2",
    "refuses_historical_namespace",
    "request_fingerprint",
    "run_canary",
    "session_close",
]
