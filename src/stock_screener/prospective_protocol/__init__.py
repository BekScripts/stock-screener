"""R1.6 — the frozen H2 candidate and the prospective shadow protocol.

The package freezes what history established, closes what history can no longer
offer, and defines the future experiment that could supply independent mechanism
evidence. It observes nothing and can observe nothing: no simulator, data client
or broker is importable from any module here, and `capability_reachable` proves
that by reading this package's own source at bind time.
"""

from __future__ import annotations

from .binding import (
    FORBIDDEN_IMPORTS,
    R1_6_DIR,
    HistoricalBinding,
    bind_r1_5_record,
    capability_reachable,
)
from .candidate import (
    CANDIDATE_ID,
    CANDIDATE_STATE,
    HISTORICAL_CLASSIFICATION,
    PROHIBITED_MODIFICATIONS,
    PROMOTION_LADDER,
    candidate_freeze,
)
from .contract import PROSPECTIVE_CONTRACT_ID, prospective_contract
from .protocol import (
    AGGREGATE_RULE,
    COMPARATOR,
    CRITERIA,
    PROTOCOL_ID,
    REQUIRED_DECISIONS,
    REQUIRED_TRANSITIONS,
    STREAM_ID,
    evaluate_prospective,
    interpret_prospective,
    prospective_protocol,
)
from .run import (
    MIRROR_PREFIX,
    PHASE,
    PROSPECTIVE_MIRROR_PREFIX,
    PROTOCOL_DIR,
    FreezeResult,
    freeze_candidate_and_protocol,
    mirror_r1_6,
    r1_5_directory,
)
from .schemas import (
    DECISION_PACKET_SCHEMA,
    EVALUATION_SCHEMA,
    LEDGER_SCHEMA,
    decision_packet_schema,
    evaluation_schema,
    ledger_schema,
)

__all__ = [
    "AGGREGATE_RULE",
    "CANDIDATE_ID",
    "CANDIDATE_STATE",
    "COMPARATOR",
    "CRITERIA",
    "DECISION_PACKET_SCHEMA",
    "EVALUATION_SCHEMA",
    "FORBIDDEN_IMPORTS",
    "HISTORICAL_CLASSIFICATION",
    "LEDGER_SCHEMA",
    "MIRROR_PREFIX",
    "PHASE",
    "PROHIBITED_MODIFICATIONS",
    "PROMOTION_LADDER",
    "PROSPECTIVE_CONTRACT_ID",
    "PROSPECTIVE_MIRROR_PREFIX",
    "PROTOCOL_DIR",
    "PROTOCOL_ID",
    "R1_6_DIR",
    "REQUIRED_DECISIONS",
    "REQUIRED_TRANSITIONS",
    "STREAM_ID",
    "FreezeResult",
    "HistoricalBinding",
    "bind_r1_5_record",
    "candidate_freeze",
    "capability_reachable",
    "decision_packet_schema",
    "evaluate_prospective",
    "evaluation_schema",
    "freeze_candidate_and_protocol",
    "interpret_prospective",
    "ledger_schema",
    "mirror_r1_6",
    "prospective_contract",
    "prospective_protocol",
    "r1_5_directory",
]
