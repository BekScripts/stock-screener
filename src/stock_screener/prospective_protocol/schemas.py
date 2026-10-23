"""Schemas for artifacts that do not exist yet.

Writing the decision packet's shape now, before any decision, is what stops the
first real quarter from being shaped by what it happens to contain. The packet
is the prospective equivalent of a preregistration: it fixes at the decision
instant what was known, what was chosen, and by which code, and it is sealed
against everything that arrives afterwards.

`forward_return` and every other outcome field are absent by construction, not
by convention — a packet that carried one would fail its own schema.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DECISION_PACKET_SCHEMA",
    "EVALUATION_SCHEMA",
    "LEDGER_SCHEMA",
    "decision_packet_schema",
    "evaluation_schema",
    "ledger_schema",
]

_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

DECISION_PACKET_SCHEMA = "prospective_decision_packet_schema.json"
LEDGER_SCHEMA = "prospective_research_ledger_schema.json"
EVALUATION_SCHEMA = "prospective_evaluation_schema.json"


def decision_packet_schema() -> dict[str, Any]:
    """The immutable packet frozen at each prospective decision instant.

    Returns:
        A JSON Schema document.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": DECISION_PACKET_SCHEMA,
        "title": "Prospective decision packet",
        "description": (
            "Frozen at the decision instant and never rewritten. It records what "
            "was knowable then, what each strategy chose, and under which code and "
            "protocol. It carries no outcome: a packet with a forward return in it "
            "would fail this schema."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "packet_key",
            "decision_timestamp",
            "benchmark_session",
            "execution_session",
            "candidate_id",
            "protocol_id",
            "protocol_version",
            "contract_id",
            "code_commit",
            "runner_armed_at",
            "fingerprints",
            "target_count",
            "comparator_target_set",
            "h2_target_set",
            "h2_holdings_before_decision",
            "forced_exits",
            "retained_incumbents",
            "new_entrants",
            "unpriced",
            "observation_kind",
        ],
        "properties": {
            "packet_key": {
                "type": "string",
                "description": "Stable key, the decision session's ISO date.",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            },
            "decision_timestamp": {
                "type": "string",
                "description": "The decision instant, 16:00 America/New_York, with offset.",
            },
            "benchmark_session": {"type": "string"},
            "execution_session": {
                "type": "string",
                "description": "The first benchmark session strictly after the signal.",
            },
            "candidate_id": {"const": "H2_MEMBERSHIP_STABILITY_CANDIDATE_V1"},
            "protocol_id": {"const": "H2_PROSPECTIVE_SHADOW_PROTOCOL_V1"},
            "protocol_version": {"type": "string"},
            "contract_id": {"const": "PROSPECTIVE_SHADOW_CONTRACT_V1"},
            "code_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "runner_armed_at": {
                "type": "string",
                "description": (
                    "When the runner was armed. Must be no later than the close of "
                    "the session preceding this decision, or the quarter is skipped."
                ),
            },
            "fingerprints": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "universe_snapshot",
                    "feature_snapshot",
                    "price_as_of",
                    "evidence_availability",
                    "ranked_cross_section",
                ],
                "properties": {
                    "universe_snapshot": _SHA,
                    "feature_snapshot": _SHA,
                    "price_as_of": _SHA,
                    "evidence_availability": _SHA,
                    "ranked_cross_section": _SHA,
                },
            },
            "target_count": {"type": "integer", "minimum": 0},
            "comparator_target_set": {"$ref": "#/$defs/identities"},
            "h2_target_set": {"$ref": "#/$defs/identities"},
            "h2_holdings_before_decision": {"$ref": "#/$defs/identities"},
            "forced_exits": {"$ref": "#/$defs/attributed"},
            "retained_incumbents": {"$ref": "#/$defs/identities"},
            "new_entrants": {"$ref": "#/$defs/identities"},
            "unpriced": {
                "$ref": "#/$defs/identities",
                "description": "Selected identities with no valid reference open.",
            },
            "observation_kind": {
                "enum": ["PROSPECTIVE", "RETROSPECTIVE"],
                "description": (
                    "RETROSPECTIVE packets are diagnostics and never enter the "
                    "criteria; they additionally carry NOT_PROSPECTIVE_CONFIRMATION."
                ),
            },
            "labels": {
                "type": "array",
                "items": {"enum": ["RETROSPECTIVE", "NOT_PROSPECTIVE_CONFIRMATION"]},
            },
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "identity_id"],
                "properties": {
                    "kind": {"enum": ["COMPANY", "HISTORICAL"]},
                    "identity_id": {"type": "integer"},
                    "symbol": {
                        "type": "string",
                        "description": "Dated display only; never an identity.",
                    },
                    "rank": {"type": "integer", "minimum": 0},
                    "score": {"type": ["number", "null"]},
                },
            },
            "identities": {"type": "array", "items": {"$ref": "#/$defs/identity"}},
            "attributed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "identity_id", "cause"],
                    "properties": {
                        "kind": {"enum": ["COMPANY", "HISTORICAL"]},
                        "identity_id": {"type": "integer"},
                        "symbol": {"type": "string"},
                        "cause": {
                            "enum": [
                                "NOT_IN_UNIVERSE",
                                "SCORE_READINESS",
                                "FEATURE_FRESHNESS",
                                "TRADABILITY",
                                "EXECUTION_AVAILABILITY",
                                "EVIDENCE_TIMING",
                                "TERMINAL",
                                "OTHER",
                            ]
                        },
                    },
                },
            },
        },
        "immutability": (
            "a packet is addressed by packet_key. The same key with the same digest "
            "is accepted as a repeat; the same key with a different digest is a "
            "protocol conflict and is never silently overwritten."
        ),
        "forbidden_content": [
            "any forward return",
            "any price after the execution session's open",
            "any later correction or restatement",
            "any performance measurement",
        ],
    }


def ledger_schema() -> dict[str, Any]:
    """The prospective research ledger's shape.

    Returns:
        A JSON Schema document.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": LEDGER_SCHEMA,
        "title": "Prospective research ledger",
        "description": (
            "Append-only provenance for the prospective window. No prospective "
            "result may exist without a ledger entry behind it."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "protocol_id",
            "candidate_id",
            "activation",
            "entries",
            "window_progress",
        ],
        "properties": {
            "protocol_id": {"const": "H2_PROSPECTIVE_SHADOW_PROTOCOL_V1"},
            "candidate_id": {"const": "H2_MEMBERSHIP_STABILITY_CANDIDATE_V1"},
            "activation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["active_at", "armed_at", "code_commit"],
                "properties": {
                    "active_at": {"type": ["string", "null"]},
                    "armed_at": {"type": ["string", "null"]},
                    "code_commit": {"type": "string"},
                },
            },
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "decision_date",
                        "packet_sha256",
                        "code_commit",
                        "observation_kind",
                        "semantic_change_status",
                    ],
                    "properties": {
                        "decision_date": {"type": "string"},
                        "packet_sha256": _SHA,
                        "execution_reference_sha256": _SHA,
                        "code_commit": {"type": "string"},
                        "observation_kind": {"enum": ["PROSPECTIVE", "RETROSPECTIVE"]},
                        "semantic_change_status": {
                            "enum": [
                                "NONE",
                                "NON_SEMANTIC_INFRASTRUCTURE",
                                "SEMANTIC_RESEARCH_CHANGE",
                            ]
                        },
                        "operational_incidents": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "data_gaps": {"type": "array", "items": {"type": "string"}},
                        "skipped": {"type": "boolean"},
                        "skip_reason": {"type": "string"},
                    },
                },
            },
            "window_progress": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decisions_captured", "completed_transitions", "verdict"],
                "properties": {
                    "decisions_captured": {"type": "integer", "minimum": 0},
                    "completed_transitions": {"type": "integer", "minimum": 0, "maximum": 4},
                    "verdict": {
                        "enum": [
                            "PENDING",
                            "PROSPECTIVE_MECHANISM_CONFIRMED",
                            "PROSPECTIVE_MECHANISM_NOT_CONFIRMED",
                            "PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT",
                        ]
                    },
                },
            },
            "post_decision_corrections": {
                "type": "array",
                "description": (
                    "Corrections recorded separately. They never alter a frozen "
                    "packet and become ordinary input to later decisions."
                ),
                "items": {
                    "type": "object",
                    "required": ["observed_at", "affects_decisions", "description"],
                    "properties": {
                        "observed_at": {"type": "string"},
                        "affects_decisions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "description": {"type": "string"},
                        "rewrote_a_frozen_packet": {"const": False},
                    },
                },
            },
        },
    }


def evaluation_schema() -> dict[str, Any]:
    """The prospective evaluation's shape.

    Returns:
        A JSON Schema document.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": EVALUATION_SCHEMA,
        "title": "Prospective mechanism evaluation",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "protocol_id",
            "candidate_id",
            "completed_transitions",
            "criteria",
            "interpretation",
            "performance_entered_verdict",
        ],
        "properties": {
            "protocol_id": {"const": "H2_PROSPECTIVE_SHADOW_PROTOCOL_V1"},
            "candidate_id": {"const": "H2_MEMBERSHIP_STABILITY_CANDIDATE_V1"},
            "completed_transitions": {"type": "integer", "minimum": 0, "maximum": 4},
            "criteria": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "required": ["id", "status"],
                    "properties": {
                        "id": {
                            "enum": [
                                "P1_TURNOVER_DIRECTION",
                                "P2_CHURN_DIRECTION",
                                "P3_EXPOSURE_PRESERVATION",
                                "P4_SAFETY_AND_PROTOCOL_INTEGRITY",
                            ]
                        },
                        "measured": {},
                        "comparator_value": {},
                        "status": {"enum": ["PENDING", "MET", "NOT_MET"]},
                    },
                },
            },
            "interpretation": {
                "type": "object",
                "required": ["outcome"],
                "properties": {
                    "outcome": {
                        "enum": [
                            "PENDING",
                            "PROSPECTIVE_MECHANISM_CONFIRMED",
                            "PROSPECTIVE_MECHANISM_NOT_CONFIRMED",
                        ]
                    }
                },
            },
            "performance_entered_verdict": {"const": False},
            "performance_observation": {
                "type": "object",
                "description": "PROSPECTIVE_PERFORMANCE_OBSERVATION. Decides nothing.",
            },
            "diagnostics": {"type": "object"},
        },
    }
