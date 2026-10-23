"""What the frozen protocol says about a missed observation, and what it does not.

V1 was written before anyone had tried to capture anything, and it anticipated
the ways a quarter could fail to enter the sequence — except the one that
happened. It is precise that a quarter the runner did not capture live can never
count, and precise about a missed *arming* cutoff. It says nothing about an
eligible, armed quarter whose capture was impossible because the source plane
did not exist.

So the audit here is textual rather than interpretive. `audit_v1` reads the
frozen protocol and reports which clauses bear on the case and which do not,
then returns the disposition that follows. It deliberately does not read the
arming clause as covering source unavailability: the disposition would be
convenient and the trigger is different, and §5 says not to choose on
convenience.

V2 exists only because V1 is silent. It adopts V1's own treatment of a
pre-sequence miss, names source unavailability as an operational incident class,
and changes nothing about H2 — not the rule, not the criteria, not the window
length. A source-plane failure is not a reason to ask less of a hypothesis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import RULE, RULE_VERSION
from stock_screener.prospective_protocol import (
    AGGREGATE_RULE,
    CANDIDATE_ID,
    CRITERIA,
    PROTOCOL_ID,
    REQUIRED_DECISIONS,
    REQUIRED_TRANSITIONS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DISPOSITIONS",
    "PROTOCOL_V2_ID",
    "audit_v1",
    "disposition_record",
    "protocol_v2",
]

#: The three dispositions §5 allows.
DISPOSITIONS: tuple[str, ...] = (
    "P1_CONTINUE_UNDER_V1",
    "P2_V1_WINDOW_INVALIDATED_REARM_REQUIRED",
    "P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED",
)

PROTOCOL_V2_ID = "H2_PROSPECTIVE_SHADOW_PROTOCOL_V2"


def audit_v1(protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Read V1 and report what it settles about the September 30 miss.

    Args:
        protocol: The frozen V1 protocol document.

    Returns:
        The clauses that bear on the case, what each settles, and the
        disposition that follows.
    """
    window = dict(protocol["window"])
    first = dict(protocol["first_eligible_decision"])
    retrospective = dict(protocol["retrospective_reconstruction"])
    termination = dict(protocol["early_termination"])

    settled = [
        {
            "clause": "retrospective_reconstruction",
            "text": retrospective["rule"],
            "settles": (
                "September 30 can never count. A quarter the runner did not "
                "capture live is reconstructable only as a labelled diagnostic."
            ),
        },
        {
            "clause": "window.replace_an_outage_transition_retrospectively",
            "value": window["replace_an_outage_transition_retrospectively"],
            "settles": "a missed observation is not replaced after the fact",
        },
    ]
    unsettled = [
        {
            "clause": "first_eligible_decision.if_the_arming_requirement_is_missed",
            "text": first["if_the_arming_requirement_is_missed"],
            "why_it_does_not_apply": (
                "it is triggered by a missed arming cutoff. September 30's cutoff "
                "was satisfied — the runner was armed five days early — so this "
                "clause is about a different failure. Its disposition would be "
                "convenient to borrow, which is a reason to be careful rather "
                "than a reason to borrow it."
            ),
        },
        {
            "clause": "early_termination.legitimate_reasons",
            "value": list(termination["legitimate_reasons"]),
            "why_it_does_not_apply": (
                "source-plane unavailability is not among them. The nearest are "
                "'data corruption' and 'protocol-binding failure', and neither "
                "occurred: the data was absent rather than wrong, and every "
                "binding held."
            ),
        },
        {
            "clause": "window.composition",
            "value": window["composition"],
            "why_it_is_ambiguous": (
                "it requires four quarter-to-quarter transitions but does not say "
                "whether a pre-sequence miss resets the window or merely delays "
                "its start. Nothing had entered the sequence — September 30 was "
                "to be the initial formation — so no transition was broken, but "
                "V1 does not state that consequence."
            ),
        },
    ]
    return {
        "protocol_id": str(protocol["protocol_id"]),
        "case": (
            "an eligible, armed quarter whose capture was impossible because the "
            "required prospective source plane did not exist"
        ),
        "v1_settles": settled,
        "v1_does_not_settle": unsettled,
        "v1_defines_missed_observation_handling": False,
        "disposition": "P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED",
        "reasoning": (
            "V1 settles the part that matters most — September 30 is lost and "
            "unrecoverable — and is silent on whether the window continues from "
            "the next eligible quarter or must be re-armed. The arming clause "
            "supplies a disposition for a pre-sequence miss, but it is triggered "
            "by late arming rather than absent sources, so applying it here would "
            "be an interpretation rather than a reading. A new prospective "
            "protocol version is required to state the disposition explicitly."
        ),
        "convenience_check": (
            "P1 would have been the convenient answer: no new protocol, no "
            "re-arm. It was not chosen, because V1 does not say it."
        ),
    }


def disposition_record(
    *,
    audit: Mapping[str, Any],
    incident: Mapping[str, Any],
    v1_digest: str,
    semantic_fingerprint_changed: bool,
) -> dict[str, Any]:
    """Build the permanent record of how the miss was disposed of.

    Args:
        audit: The output of `audit_v1`.
        incident: The R1.8 incident as recorded.
        v1_digest: The frozen V1 protocol's digest.
        semantic_fingerprint_changed: Whether building the source plane moved
            the semantic code fingerprint, which independently closes V1's
            armed window.

    Returns:
        The disposition record.
    """
    return {
        "r1_8_incident": {
            "decision_session": "2026-09-30",
            "decision_instant": "2026-09-30T16:00:00-04:00",
            "eligible": True,
            "armed": True,
            "arm_cutoff_satisfied": True,
            "captured": False,
            "reason": "REQUIRED_SOURCE_UNAVAILABLE",
            "classification": "PROSPECTIVE_DATA_PLANE_NOT_READY",
            "recorded_at": incident.get("at"),
            "decision_packets": 0,
            "execution_packets": 0,
            "observations": 0,
            "immutable": True,
        },
        "frozen_v1_digest": v1_digest,
        "v1_defines_missed_observation_handling": audit["v1_defines_missed_observation_handling"],
        "disposition": audit["disposition"],
        "reason": audit["reasoning"],
        "v1_remains_immutable": True,
        "v1_edited_in_place": False,
        "new_protocol_version_required": True,
        "new_protocol_version": PROTOCOL_V2_ID,
        "re_arm_required": True,
        "re_arm_reasons": [
            "V1's arm record names first_eligible_decision 2026-09-30, a date the "
            "runner can no longer act on, so the armed window cannot advance",
            *(
                [
                    "building the prospective source plane moved the semantic code "
                    "fingerprint, which independently closes the V1 window: the "
                    "runner refuses confirmatory capture across a semantic change"
                ]
                if semantic_fingerprint_changed
                else []
            ),
        ],
        "september_30_remains_lost": True,
        "september_30_recoverable": False,
        "september_30_backfilled": False,
        "h2_status": "PENDING PROSPECTIVE EVIDENCE",
        "h2_failed": False,
        "h2_supported": False,
        "what_this_does_not_do": (
            "it does not reinterpret September 30 as captured, shorten the "
            "confirmation window, or change anything about H2. An infrastructure "
            "failure is not evidence about a hypothesis."
        ),
    }


def protocol_v2(*, disposition: Mapping[str, Any], code_commit: str) -> dict[str, Any]:
    """Build V2: V1's successor, differing only in operational governance.

    Args:
        disposition: The disposition record V2 follows from.
        code_commit: The commit V2 was written at.

    Returns:
        The V2 protocol document.
    """
    return {
        "protocol_id": PROTOCOL_V2_ID,
        "supersedes": PROTOCOL_ID,
        "status": "FROZEN — defined, not active; no observation has been taken",
        "frozen_at_code_commit": code_commit,
        "why_v2_exists": (
            "V1 is silent on an eligible, armed quarter whose capture was "
            "impossible because the source plane did not exist. V2 states that "
            "disposition explicitly. It exists for that reason and no other."
        ),
        "v1_remains": "IMMUTABLE AND UNEDITED",
        "candidate_id": CANDIDATE_ID,
        "rule_version": RULE_VERSION,
        "rule": dict(RULE),
        "criteria": list(CRITERIA),
        "aggregate_rule": dict(AGGREGATE_RULE),
        "required_decisions": REQUIRED_DECISIONS,
        "required_transitions": REQUIRED_TRANSITIONS,
        "unchanged_from_v1": [
            "the H2 candidate and ONE_DECILE_HYSTERESIS_V1",
            "the entry threshold and the two-selection-width retention band",
            "the quarterly cadence",
            "criteria P1 through P4 and the rule P1 AND P2 AND P3 AND P4",
            "five decisions and four transitions — the window is not shortened",
            "the paired comparator and the paired design",
            "the prospective-only evidence status",
            "the prohibition on reconstructing an uncaptured quarter",
            "the prohibition on claiming historical confirmation",
        ],
        "added_in_v2": {
            "missed_observation_disposition": {
                "trigger": (
                    "an eligible, armed quarter whose capture fails closed because "
                    "a required source was unavailable or too stale to describe "
                    "the decision instant"
                ),
                "incident_class": "PROSPECTIVE_DATA_PLANE_NOT_READY",
                "rule": (
                    "the quarter never enters the prospective sequence. It is not "
                    "reconstructed, not replaced and not counted. The next "
                    "eligible quarter end becomes the sequence's next decision."
                ),
                "if_it_occurs_before_the_sequence_begins": (
                    "no transition is lost, because none existed. The window "
                    "starts later rather than restarting shorter."
                ),
                "if_it_occurs_mid_sequence": (
                    "the window is invalidated. Four transitions must be four "
                    "consecutive quarter-to-quarter transitions of a single "
                    "portfolio, and a gap breaks the chain the criteria measure."
                ),
                "window_length_unchanged": True,
            },
            "readiness_precondition": {
                "rule": (
                    "a window may only be armed when all six required inputs are "
                    "READY under the prospective freshness policy, proven by the "
                    "readiness gate before the arm, not at the decision instant"
                ),
                "why": (
                    "September 30 was lost because readiness was assumed. It is "
                    "now a precondition of arming rather than a hope at capture."
                ),
            },
            "freshness_policy": {
                "applies": "PROSPECTIVELY ONLY",
                "never_applied_retroactively": True,
                "september_30_not_reassessed_under_it": True,
            },
        },
        "may_not": [
            "reinterpret September 30 as captured",
            "shorten the confirmation requirement",
            "alter H2, its rule, its criteria or its aggregate rule",
            "claim historical confirmation",
            "authorise broker, paper or live trading",
        ],
        "historical_evidence_status": {
            "the_frozen_dataset_is": "HYPOTHESIS_GENERATION_DATA_FOR_H2",
            "clean_h2_holdout_remains": False,
            "holdout_reset": False,
            "CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST": True,
            "mandatory_classification_of_a_historical_run": ("EXPLORATORY_HISTORICAL_TEST"),
        },
        "status_at_freeze": {
            "protocol_definition": "FROZEN",
            "prospective_runner": "NOT_ARMED_UNDER_V2",
            "first_eligible_decision": "NOT_YET_ASSIGNED",
            "observations": 0,
            "completed_transitions": 0,
            "prospective_verdict": "PENDING",
            "next_phase": ("R1.10 — CERTIFY THE NEXT ELIGIBLE CAPTURE WINDOW AND ARM UNDER V2"),
        },
        "disposition_followed": disposition["disposition"],
    }
