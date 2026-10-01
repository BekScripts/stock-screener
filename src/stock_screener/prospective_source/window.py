"""Certifying a prospective window, and arming it under V2.

The September miss had two causes and R1.9 fixed one. It built the sources, but
left a timing hole wide enough to drive the same failure through again: a
current-only snapshot taken after the close would have passed the old
admissibility check, because its semantic time equals its retrieval time and
nothing compared that to the instant. This module closes it by classifying every
source and freezing *when* each may be read, not merely what it must contain.

The capture sequence is the artifact that matters. It says the universe and
tradability must be in hand before the instant, that prices and SEC acceptance
may be gathered afterwards, and what failure code applies when either is
violated. Frozen before the arm, so the decision day has nothing left to decide.

Nothing here constructs a portfolio. Arming is the end of the phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.prospective_protocol import CANDIDATE_ID
from stock_screener.prospective_runner import (
    SESSION_TIMEZONE,
    calendar_descriptor,
    next_session,
    previous_session,
    semantic_fingerprint,
    upcoming_quarter_ends,
)

from .adapters import session_close
from .envelope import TemporalClass, content_digest
from .governance import PROTOCOL_V2_ID
from .readiness import FRESHNESS_POLICY, REQUIRED_INPUTS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

__all__ = [
    "ACTIVATION_V2_FILE",
    "ARM_V2_FILE",
    "CAPTURE_SEQUENCE_ID",
    "MISSED_SESSIONS",
    "SOURCE_TEMPORAL_CLASS",
    "WindowError",
    "activate_v2",
    "arm_v2",
    "calendar_derivation",
    "capture_sequence",
    "certify_timing",
    "decision_day_runbook",
    "derive_window",
    "refuse_missed_session",
]

ACTIVATION_V2_FILE = "prospective_activation_v2.json"
ARM_V2_FILE = "prospective_arm_record_v2.json"

CAPTURE_SEQUENCE_ID = "PROSPECTIVE_CAPTURE_SEQUENCE_V2"

#: Sessions that were eligible, missed, and may never be produced as a decision.
#: A hard refusal rather than a convention, because the temptation to fill a gap
#: in a five-decision window will be strongest exactly when it matters most.
MISSED_SESSIONS: frozenset[str] = frozenset({"2026-09-30"})

#: What each decision-relevant source can say about a moment other than now.
SOURCE_TEMPORAL_CLASS: dict[str, TemporalClass] = {
    "universe": TemporalClass.CURRENT_AT_RETRIEVAL,
    "tradability": TemporalClass.CURRENT_AT_RETRIEVAL,
    "sec_availability": TemporalClass.EVENT_TIME,
    "market": TemporalClass.SESSION_BOUNDED,
    "score_inputs": TemporalClass.SESSION_BOUNDED,
    "ranked_cross_section": TemporalClass.SESSION_BOUNDED,
}


class WindowError(RuntimeError):
    """Raised when a window may not be certified or armed as requested."""


def refuse_missed_session(session: str) -> None:
    """Refuse to produce a decision for a session already recorded as missed.

    Args:
        session: The decision session, as an ISO date.

    Raises:
        WindowError: If the session is one of the permanently missed ones.
    """
    if session in MISSED_SESSIONS:
        raise WindowError(
            f"{session} was an eligible decision that failed closed on an "
            "unavailable source. It is MISSED_DECISION_NOT_BACKFILLABLE: no "
            "decision packet may ever be produced for it, and reconstructing one "
            "would be a backtest wearing a prospective label."
        )


def certify_timing() -> dict[str, Any]:
    """The temporal audit: what each source can prove, and when it must be read.

    Returns:
        One entry per decision-relevant source, with its class, the latest
        moment it may be retrieved, and the code that applies when it is late.
    """
    rules = {
        TemporalClass.EVENT_TIME: {
            "earliest_permitted_retrieval": "any time",
            "latest_permitted_retrieval": "any time, including after the instant",
            "availability_predicate": "per-fact availability timestamp <= T",
            "semantic_as_of_rule": "the decision instant, since the cutoff is applied",
            "why_late_retrieval_is_safe": (
                "each fact carries when it became knowable, so the cutoff can be "
                "applied after the event without trusting the clock"
            ),
            "failure_code": "SEMANTIC_AS_OF_AFTER_DECISION_INSTANT",
        },
        TemporalClass.SESSION_BOUNDED: {
            "earliest_permitted_retrieval": "after the session closes",
            "latest_permitted_retrieval": "any time, including after the instant",
            "availability_predicate": "bar.session <= decision session",
            "semantic_as_of_rule": "the decision session's close",
            "why_late_retrieval_is_safe": (
                "the session is the fact. A completed bar for the decision "
                "session does not change because it was fetched later."
            ),
            "failure_code": "SEMANTIC_AS_OF_AFTER_DECISION_INSTANT",
        },
        TemporalClass.CURRENT_AT_RETRIEVAL: {
            "earliest_permitted_retrieval": ("within the freshness bound before the instant"),
            "latest_permitted_retrieval": "AT OR BEFORE the decision instant",
            "availability_predicate": (
                "retrieved_at <= T, or a provider-supplied effective_at <= T"
            ),
            "semantic_as_of_rule": "the retrieval moment; never backdated",
            "why_late_retrieval_is_unsafe": (
                "the payload describes the present and nothing else, so a "
                "snapshot taken after the instant is evidence about the wrong "
                "moment. There is no field in it that says what was true earlier."
            ),
            "failure_code": "CURRENT_ONLY_SOURCE_TOO_LATE",
        },
    }
    return {
        "audit_id": "PROSPECTIVE_TEMPORAL_SOURCE_AUDIT_V1",
        "classes": {str(name): dict(body) for name, body in rules.items()},
        "sources": {
            name: {
                "temporal_class": str(temporal),
                **{
                    key: value
                    for key, value in rules[temporal].items()
                    if key != "why_late_retrieval_is_safe"
                },
                "freshness_bound_seconds": FRESHNESS_POLICY.get(name),
            }
            for name, temporal in sorted(SOURCE_TEMPORAL_CLASS.items())
        },
        "post_close_current_only_snapshot_admissible": False,
        "provider_effective_time_may_substitute": True,
        "provider_effective_time_must_come_from_the_provider": (
            "an effective time we assign ourselves would be the retrojection this "
            "rule forbids, wearing a field name"
        ),
        "closes_the_r1_9_gap": (
            "R1.9 checked semantic_as_of <= T, which a current-only snapshot "
            "satisfies trivially because its semantic time is its own retrieval. "
            "Retrieval time is now load-bearing for that class."
        ),
    }


def derive_window(*, after: date, armed_at: datetime) -> dict[str, Any]:
    """Derive the next eligible decision session and its arm cutoff.

    Args:
        after: The session to search strictly after.
        armed_at: When the arm is being created. Must be aware.

    Returns:
        The derived window, with every date computed rather than supplied.

    Raises:
        WindowError: If the derived session is one already missed, or the cutoff
            has already passed.
    """
    quarter = upcoming_quarter_ends(after, 1)[0]
    refuse_missed_session(quarter.isoformat())
    preceding = previous_session(quarter)
    cutoff = session_close(preceding)
    instant = session_close(quarter)
    if armed_at > cutoff:
        raise WindowError(
            f"the arm cutoff for {quarter} was {cutoff.isoformat()} and it is now "
            f"{armed_at.isoformat()}. Arming after the cutoff would claim a "
            "readiness that did not exist in time."
        )
    return {
        "decision_session": quarter.isoformat(),
        "decision_instant": instant.isoformat(),
        "preceding_session": preceding.isoformat(),
        "arm_cutoff": cutoff.isoformat(),
        "armed_at": armed_at.isoformat(),
        "cutoff_satisfied": True,
        "lead_time_seconds": (cutoff - armed_at).total_seconds(),
        "execution_reference_session": next_session(quarter).isoformat(),
        "execution_reference_derived": True,
        "timezone": SESSION_TIMEZONE,
        "derived_automatically": True,
        "manually_supplied": False,
    }


def calendar_derivation(window: dict[str, Any]) -> dict[str, Any]:
    """Record where the window's dates came from.

    Args:
        window: The output of `derive_window`.

    Returns:
        The calendar's provenance alongside the derived dates.
    """
    descriptor = calendar_descriptor()
    return {
        "calendar_id": descriptor["calendar_id"],
        "derivation_method": (
            "rules, not a table. The frozen benchmark-session table ends with the "
            "research dataset in August 2026, so it cannot answer for a future "
            "quarter; the calendar computes sessions from holiday rules and is "
            "verified by replaying every session the dataset does contain."
        ),
        "table_bound": False,
        "verified_against": descriptor["verified_against"],
        "timezone": descriptor["timezone"],
        "quarter_end_rule": descriptor["quarter_end_rule"],
        "decision_instant_rule": descriptor["decision_instant"],
        "next_eligible_decision_session": window["decision_session"],
        "decision_instant": window["decision_instant"],
        "arm_cutoff": window["arm_cutoff"],
        "execution_reference_session": window["execution_reference_session"],
        "execution_reference_rule": (
            "the first benchmark session strictly after the decision session, "
            "derived at capture rather than written down now"
        ),
        "irregular_closure_risk": descriptor["irregular_closure_risk"],
    }


def capture_sequence(window: dict[str, Any]) -> dict[str, Any]:
    """Freeze the staged acquisition order for one decision.

    Args:
        window: The derived window.

    Returns:
        The sequence, one stage at a time, with the failure code for each.
    """
    audit = certify_timing()
    return {
        "sequence_id": CAPTURE_SEQUENCE_ID,
        "decision_session": window["decision_session"],
        "decision_instant": window["decision_instant"],
        "required_inputs": list(REQUIRED_INPUTS),
        "conjunctive": True,
        "stages": [
            {
                "stage": "A_PRE_DECISION",
                "when": "before the decision instant, within the freshness bound",
                "sources": ["universe", "tradability"],
                "temporal_class": str(TemporalClass.CURRENT_AT_RETRIEVAL),
                "requirement": "retrieved_at <= decision instant",
                "seal": "the snapshot digest is sealed before the instant",
                "failure_code": "REQUIRED_PREDECISION_SOURCE_UNAVAILABLE",
                "on_failure": (
                    "miss the observation honestly. Do not wait until after the "
                    "instant and substitute a current snapshot."
                ),
            },
            {
                "stage": "B_DECISION_INSTANT",
                "when": window["decision_instant"],
                "sources": [],
                "requirement": "the time gate reaches T; nothing is ranked yet",
                "failure_code": "DECISION_INSTANT_NOT_REACHED",
            },
            {
                "stage": "C_POST_CLOSE_COMPLETION",
                "when": "after the close, once the session's bar is final",
                "sources": ["market", "sec_availability"],
                "temporal_class": (
                    f"{TemporalClass.SESSION_BOUNDED} and {TemporalClass.EVENT_TIME}"
                ),
                "requirement": ("bar.session <= decision session; accepted_at <= decision instant"),
                "failure_code": "SEMANTIC_AS_OF_AFTER_DECISION_INSTANT",
                "retry": "permitted; the semantic cutoff is never weakened to succeed",
            },
            {
                "stage": "C2_FREEZE_FINGERPRINTS",
                "when": "once every source is admissible",
                "requirement": f"all {len(REQUIRED_INPUTS)} input fingerprints frozen",
                "failure_code": "SOURCE_INCOMPLETE",
            },
            {
                "stage": "D_DECISION",
                "when": "after the fingerprints are frozen",
                "steps": [
                    "construct COMPOUNDER_V1_2 from the frozen inputs",
                    "rank the executable cross-section",
                    "apply ONE_DECILE_HYSTERESIS_V1 unchanged",
                    "write exactly one decision packet",
                    "mirror and seal it",
                ],
                "executed_by_r1_10": False,
            },
            {
                "stage": "E_EXECUTION_REFERENCE",
                "when": (
                    f"the open of {window['execution_reference_session']}, derived "
                    "rather than written down"
                ),
                "requirement": "a separate packet linking the decision by digest",
                "executed_by_r1_10": False,
            },
        ],
        "temporal_audit": audit["sources"],
        "score_may_be_computed_after_the_instant": True,
        "why": (
            "what matters is that every input was admissible at the instant, not "
            "when the arithmetic ran. The score is a function of the frozen "
            "inputs and adds no information."
        ),
        "alters_h2_semantics": False,
        "operational_only": True,
    }


def decision_day_runbook(window: dict[str, Any]) -> dict[str, Any]:
    """The ordered checklist for the armed session.

    Args:
        window: The derived window.

    Returns:
        A runbook naming what runs when, and what R1.10 does not run.
    """
    instant = datetime.fromisoformat(window["decision_instant"])
    return {
        "runbook_id": "H2_DECISION_DAY_RUNBOOK_V2",
        "decision_session": window["decision_session"],
        "decision_instant": window["decision_instant"],
        "checkpoints": [
            {
                "name": "EARLY_READINESS",
                "when": "at least a week before the session",
                "command": "prospective readiness",
                "purpose": "find a provider problem while there is time to fix it",
                "consumes_observation": False,
            },
            {
                "name": "FINAL_PRE_ARM_VERIFICATION",
                "when": "before the arm cutoff",
                "command": "prospective certify-window",
                "purpose": "bindings, fingerprint, calendar, six-of-six",
                "consumes_observation": False,
            },
            {
                "name": "PRE_DECISION_DAY_CHECK",
                "when": (f"the preceding session, {window['preceding_session']}, before its close"),
                "command": "prospective preflight",
                "purpose": "the arm cutoff is that close; nothing can be fixed after it",
                "consumes_observation": False,
            },
            {
                "name": "DECISION_DAY_PREFLIGHT",
                "when": f"before {instant.isoformat()}",
                "command": "prospective preflight",
                "purpose": "last check while Stage A is still possible",
                "consumes_observation": False,
            },
        ],
        "stages": [
            "A — capture the current-only universe and tradability snapshot and "
            "seal its digest, BEFORE the instant",
            "B — wait for the time gate to reach the instant",
            "C — after the close, complete the session bar and the "
            "acceptance-bounded SEC retrieval, then freeze six fingerprints",
            "D — score, rank, apply the frozen rule, write one decision packet",
            "E — at the next session's open, capture the execution reference as a "
            "separate linked packet",
        ],
        "r1_10_executes": ["none of D", "none of E"],
        "the_one_thing_that_cannot_be_recovered": (
            "Stage A. Prices and filings can be fetched late; the universe cannot. "
            "If Stage A is missed the observation is missed, and the honest record "
            "is REQUIRED_PREDECISION_SOURCE_UNAVAILABLE."
        ),
        "readiness_must_be_rechecked": (
            "a canary that passed in October proves nothing about December. The "
            "runner requires a fresh pre-event readiness result."
        ),
    }


@dataclass(frozen=True)
class Sealed:
    """A governance record as written.

    Attributes:
        path: Where it landed.
        sha256: Its digest.
        written: False when identical bytes were already present.
        body: The record.
    """

    path: Path
    sha256: str
    written: bool
    body: dict[str, Any]


def _seal(path: Path, body: dict[str, Any], *, label: str, binding: Sequence[str]) -> Sealed:
    """Write a governance record once, append-only.

    Idempotency keys on the *binding* fields of the stored record, never on a
    freshly generated body. These records carry a timestamp from the clock, so
    comparing whole bodies would make every re-run look like a conflicting
    rewrite — the mistake R1.7 made and fixed, repeated here and fixed again.
    What must match is what the record authorises.

    Args:
        path: Where the record belongs.
        body: The record to write.
        label: What kind of record it is, for the error.
        binding: The fields that must agree for a re-read to be idempotent.

    Returns:
        The record, written or re-read.

    Raises:
        WindowError: If a record exists whose binding differs.
    """
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        conflicts = sorted(field for field in binding if existing.get(field) != body.get(field))
        if conflicts:
            raise WindowError(
                f"a {label} already exists under a different binding ({conflicts}); "
                "these records are append-only and are never rewritten"
            )
        return Sealed(
            path=path,
            sha256=content_digest(existing),
            written=False,
            body=existing,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Sealed(path=path, sha256=content_digest(body), written=True, body=body)


def activate_v2(
    root: Path,
    *,
    code_commit: str,
    candidate_sha256: str,
    protocol_v2_sha256: str,
    contract_sha256: str,
    runner_version: str,
    capture_sequence_sha256: str,
    readiness_sha256: str,
) -> Sealed:
    """Create the V2 activation, bound to the final committed code.

    The V1 activation is immutable historical evidence bound to a commit from
    before the source plane existed. It cannot authorise semantics it never saw,
    so V2 gets its own.

    Args:
        root: The prospective runtime root.
        code_commit: The commit this binds to. Must be a full hash.
        candidate_sha256: The frozen candidate's digest.
        protocol_v2_sha256: The V2 protocol's digest.
        contract_sha256: The prospective contract's digest.
        runner_version: The runner's version.
        capture_sequence_sha256: The frozen capture sequence's digest.
        readiness_sha256: The readiness certification's digest.

    Returns:
        The activation record.

    Raises:
        WindowError: If the commit is not a full hash.
    """
    if len(code_commit) != 40:
        raise WindowError(
            "a V2 activation binds to a full immutable commit; prospective "
            "packets bind to code, and a worktree is not a commit"
        )
    moment = datetime.now(UTC)
    body = {
        "activation_version": "V2",
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_V2_ID,
        "supersedes_activation_for": "H2_PROSPECTIVE_SHADOW_PROTOCOL_V1",
        "why_a_new_activation": (
            "the V1 activation is bound to a commit from before the prospective "
            "source plane existed, and to a semantic fingerprint that has since "
            "moved. It cannot authorise semantics it never saw."
        ),
        "runner_version": runner_version,
        "code_commit": code_commit,
        "activated_at_utc": moment.isoformat(),
        "semantic_code_fingerprint": semantic_fingerprint()["semantic_fingerprint"],
        "candidate_sha256": candidate_sha256,
        "protocol_v2_sha256": protocol_v2_sha256,
        "prospective_contract_sha256": contract_sha256,
        "capture_sequence_sha256": capture_sequence_sha256,
        "source_readiness_sha256": readiness_sha256,
        "observations_at_activation": 0,
        "activation_is_not_an_observation": True,
        "strategy_variant_activated": "NONE — the frozen candidate only",
        "backdating": "REFUSED — the runtime clock is the only source",
        "broker_connectivity": "NONE",
    }
    sealed = _seal(
        root / ACTIVATION_V2_FILE,
        body,
        label="V2 activation",
        binding=(
            "candidate_id",
            "protocol_id",
            "code_commit",
            "semantic_code_fingerprint",
            "candidate_sha256",
            "protocol_v2_sha256",
            "prospective_contract_sha256",
            "capture_sequence_sha256",
        ),
    )
    log.info("prospective.v2.activated", at=body["activated_at_utc"])
    return sealed


def arm_v2(
    root: Path,
    *,
    activation: Sealed,
    window: dict[str, Any],
    candidate_sha256: str,
    protocol_v2_sha256: str,
    capture_sequence_sha256: str,
    readiness_sha256: str,
    missed_opportunities: int,
) -> Sealed:
    """Create the V2 arm for the derived window.

    Args:
        root: The prospective runtime root.
        activation: The V2 activation this arm rests on.
        window: The derived window.
        candidate_sha256: The frozen candidate's digest.
        protocol_v2_sha256: The V2 protocol's digest.
        capture_sequence_sha256: The frozen capture sequence's digest.
        readiness_sha256: The readiness certification's digest.
        missed_opportunities: How many eligible sessions were missed. Kept
            separate from the observation count so neither can absorb the other.

    Returns:
        The arm record.

    Raises:
        WindowError: If the V1 arm is offered as authority, or the cutoff passed.
    """
    if not (root / ACTIVATION_V2_FILE).is_file():
        raise WindowError("a V2 arm requires a V2 activation; none exists")
    refuse_missed_session(window["decision_session"])
    body = {
        "arm_version": "V2",
        "activation_sha256": activation.sha256,
        "candidate_id": CANDIDATE_ID,
        "candidate_sha256": candidate_sha256,
        "protocol_id": PROTOCOL_V2_ID,
        "protocol_v2_sha256": protocol_v2_sha256,
        "capture_sequence_sha256": capture_sequence_sha256,
        "source_readiness_sha256": readiness_sha256,
        "semantic_code_fingerprint": activation.body["semantic_code_fingerprint"],
        "code_commit": activation.body["code_commit"],
        **{
            key: window[key]
            for key in (
                "decision_session",
                "decision_instant",
                "preceding_session",
                "arm_cutoff",
                "armed_at",
                "cutoff_satisfied",
                "lead_time_seconds",
                "execution_reference_session",
                "derived_automatically",
                "manually_supplied",
                "timezone",
            )
        },
        "valid_observations_at_arm": 0,
        "valid_observation_numbering": (
            "the next successful capture is VALID_OBSERVATION_1. The missed "
            "session keeps its own identity and is not renumbered away."
        ),
        "missed_eligible_opportunities": missed_opportunities,
        "missed_sessions": sorted(MISSED_SESSIONS),
        "window_requirement_unchanged": True,
        "required_decisions": 5,
        "required_transitions": 4,
        "v1_arm_cannot_authorise_v2": True,
        "state": "ARMED_WAITING_FOR_DECISION",
        "creates_an_observation": False,
    }
    sealed = _seal(
        root / ARM_V2_FILE,
        body,
        label="V2 arm",
        binding=(
            "activation_sha256",
            "candidate_sha256",
            "protocol_v2_sha256",
            "capture_sequence_sha256",
            "decision_session",
            "decision_instant",
            "arm_cutoff",
            "semantic_code_fingerprint",
            "code_commit",
        ),
    )
    log.info(
        "prospective.v2.armed",
        decision_session=window["decision_session"],
        cutoff=window["arm_cutoff"],
    )
    return sealed
