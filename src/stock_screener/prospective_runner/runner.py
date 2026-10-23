"""R1.7 — the prospective shadow runner: activate, arm, wait.

Activation starts a clock that cannot be restarted because a result is
disappointing, so the operations that start it are the ones most worth making
hard to get wrong. Activation reads the runtime clock and refuses a supplied
timestamp. It refuses a second activation with different bytes. Arming records
itself durably *before* any decision, because a later decision claiming the
runner was armed is worth nothing if no record predates it.

The first eligible decision is derived here and nowhere else: the first
quarter-end session strictly after activation for which the runner was armed by
the close of the preceding session. When arming misses that cutoff the quarter
is skipped and recorded as skipped — it never enters the sequence, so there is
nothing to reconstruct later.

The runner captures no decision in this phase. Ending armed, with zero
observations and a sealed verdict, is the successful outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog

from stock_screener.prospective_protocol import (
    CANDIDATE_ID,
    PROSPECTIVE_CONTRACT_ID,
    PROTOCOL_ID,
    REQUIRED_DECISIONS,
    REQUIRED_TRANSITIONS,
    STREAM_ID,
)

from .binding import RUNNER_VERSION, RunnerBinding, RunnerBindingError
from .calendar import (
    SESSION_TIMEZONE,
    previous_session,
    upcoming_quarter_ends,
)
from .fingerprint import compare
from .packets import finalise, packet_digest
from .state import ProspectiveState, StateError, load_state, save_state

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

__all__ = [
    "ACTIVATION_FILE",
    "ARM_FILE",
    "CRITERIA_SEAL",
    "LEDGER_FILE",
    "MIRRORED_FILES",
    "MIRROR_PREFIX",
    "PROSPECTIVE_ROOT",
    "STATE_FILE",
    "activate",
    "arm",
    "derive_first_eligible_decision",
    "record_incident",
    "status",
]

#: Where prospective runtime state lives. Never under research/r1_*.
PROSPECTIVE_ROOT = "prospective/h2_v1"

#: The control-plane prefix for prospective durability.
MIRROR_PREFIX = "prospective/h2-v1"

ACTIVATION_FILE = "prospective_activation.json"
ARM_FILE = "prospective_arm_record.json"
STATE_FILE = "prospective_runner_state.json"
LEDGER_FILE = "prospective_research_ledger.jsonl"
BINDING_FILE = "prospective_runner_binding.json"
SUMMARY_FILE = "prospective_runner_summary.json"

MIRRORED_FILES = (ACTIVATION_FILE, ARM_FILE, BINDING_FILE, STATE_FILE)

#: What status reports instead of an interim reading.
CRITERIA_SEAL = "SEALED_UNTIL_WINDOW_COMPLETE"


class ActivationError(RunnerBindingError):
    """Raised when activation may not happen as requested."""


def _now() -> datetime:
    """The runtime clock, in UTC. The only source of an activation time."""
    return datetime.now(UTC)


def _eastern(moment: datetime) -> datetime:
    """Render a moment in the session timezone."""
    return moment.astimezone(ZoneInfo(SESSION_TIMEZONE))


def _session_close(session: date) -> datetime:
    """The regular close of a session, as an aware moment."""
    return datetime.combine(
        session, datetime.min.time(), tzinfo=ZoneInfo(SESSION_TIMEZONE)
    ).replace(hour=16, minute=0)


def _append_ledger(root: Path, entry: dict[str, Any]) -> None:
    """Append one entry to the prospective research ledger."""
    path = root / LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


@dataclass(frozen=True)
class Activation:
    """The result of activating.

    Attributes:
        record: The immutable activation record.
        sha256: Its digest.
        written: False when an identical record already existed.
    """

    record: dict[str, Any]
    sha256: str
    written: bool


def activate(
    root: Path,
    binding: RunnerBinding,
    *,
    code_commit: str,
    supplied_timestamp: str | None = None,
) -> Activation:
    """Activate the protocol exactly once, at the runtime clock's instant.

    Args:
        root: The prospective state root.
        binding: The verified runner binding.
        code_commit: The immutable commit the runner is bound to.
        supplied_timestamp: Refused if given. Present so that supplying one is
            an explicit error rather than a silently honoured convenience.

    Returns:
        The activation record and its digest.

    Raises:
        ActivationError: If a timestamp was supplied, the commit is not a full
            hash, or an activation with different bytes already exists.
    """
    if supplied_timestamp is not None:
        raise ActivationError(
            "activation time comes from the runtime clock. A supplied timestamp "
            "could backdate the window, so it is refused rather than validated."
        )
    if len(code_commit) != 40:
        raise ActivationError(
            "activation must bind to a full immutable commit; prospective packets "
            "bind to code, and uncommitted code cannot be bound to"
        )

    state = load_state(root / STATE_FILE)
    path = root / ACTIVATION_FILE
    if path.exists():
        # Idempotency keys on the stored record, not on a freshly stamped one:
        # the clock moves between calls, and a restart must not look like a
        # second activation. What must match is the binding, not the instant.
        existing = json.loads(path.read_text(encoding="utf-8"))
        conflicts = {
            field: (existing.get(field), value)
            for field, value in (
                ("code_commit", code_commit),
                ("candidate_sha256", binding.candidate_sha256),
                ("protocol_sha256", binding.protocol_sha256),
                ("prospective_contract_sha256", binding.contract_sha256),
                (
                    "semantic_code_fingerprint",
                    binding.semantic["semantic_fingerprint"],
                ),
            )
            if existing.get(field) != value
        }
        if conflicts:
            raise ActivationError(
                "this protocol is already activated under a different binding "
                f"({sorted(conflicts)}). An activation is written once; a second "
                "one would move the window's start, which is the thing it exists "
                "to fix."
            )
        return Activation(record=existing, sha256=packet_digest(existing), written=False)

    moment = _now()
    record = {
        "stream_id": STREAM_ID,
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_ID,
        "prospective_contract_id": PROSPECTIVE_CONTRACT_ID,
        "runner_version": RUNNER_VERSION,
        "code_commit": code_commit,
        "activated_at_utc": moment.isoformat(),
        "activated_at_eastern": _eastern(moment).isoformat(),
        "activation_state": "ACTIVATED",
        "semantic_code_fingerprint": binding.semantic["semantic_fingerprint"],
        "candidate_sha256": binding.candidate_sha256,
        "protocol_sha256": binding.protocol_sha256,
        "prospective_contract_sha256": binding.contract_sha256,
        "calendar": {
            "calendar_id": binding.calendar["calendar_id"],
            "timezone": binding.calendar["timezone"],
            "session_definition": binding.calendar["session_definition"],
            "quarter_end_rule": binding.calendar["quarter_end_rule"],
            "historical_check": binding.calendar["historical_check"],
        },
        "required_decisions": REQUIRED_DECISIONS,
        "required_transitions": REQUIRED_TRANSITIONS,
        "observations_at_activation": 0,
        "activation_is_not_an_observation": (
            "activation starts the clock. It is not a decision, and the first "
            "decision is derived from it rather than coincident with it."
        ),
        "backdating": "REFUSED — the runtime clock is the only source",
        "broker_connectivity": "NONE",
        "order_capability": "NONE",
    }
    state.require(ProspectiveState.DEFINED)
    state.transition(ProspectiveState.ACTIVATED)
    written = finalise(path, record)
    state.stream_id = STREAM_ID
    state.candidate_id = CANDIDATE_ID
    state.protocol_id = PROTOCOL_ID
    state.code_commit = code_commit
    state.semantic_fingerprint = binding.semantic["semantic_fingerprint"]
    state.activated_at = record["activated_at_utc"]
    save_state(root / STATE_FILE, state)
    _append_ledger(
        root,
        {
            "event": "PROTOCOL_ACTIVATED",
            "at": record["activated_at_utc"],
            "code_commit": code_commit,
            "activation_sha256": written.sha256,
            "semantic_fingerprint": state.semantic_fingerprint,
            "observation_kind": None,
        },
    )
    log.info("prospective.activated", at=record["activated_at_utc"])
    return Activation(record=record, sha256=written.sha256, written=True)


def derive_first_eligible_decision(
    *, activated_at: datetime, armed_at: datetime, horizon: int = 8
) -> dict[str, Any]:
    """Derive the first decision the protocol allows to count.

    The rule, and the whole of it: the first quarter-end session strictly after
    activation for which the runner was armed no later than the close of the
    immediately preceding session. A quarter failing the second test is skipped,
    not reconstructed.

    Args:
        activated_at: When the protocol was activated.
        armed_at: When the runner was armed.
        horizon: How many quarter ends to consider before giving up.

    Returns:
        The first eligible decision, the quarters skipped on the way, and the
        cutoff comparison for each.
    """
    activation_day = _eastern(activated_at).date()
    skipped: list[dict[str, Any]] = []
    for quarter_end in upcoming_quarter_ends(activation_day, horizon):
        preceding = previous_session(quarter_end)
        cutoff = _session_close(preceding)
        satisfied = armed_at <= cutoff
        entry = {
            "quarter_end": quarter_end.isoformat(),
            "preceding_session": preceding.isoformat(),
            "required_arm_cutoff": cutoff.isoformat(),
            "armed_at": armed_at.isoformat(),
            "cutoff_satisfied": satisfied,
        }
        if satisfied:
            return {
                "first_eligible_decision": quarter_end.isoformat(),
                "preceding_session": preceding.isoformat(),
                "required_arm_cutoff": cutoff.isoformat(),
                "armed_at": armed_at.isoformat(),
                "cutoff_satisfied": True,
                "decision_instant": (f"{quarter_end.isoformat()}T16:00:00 {SESSION_TIMEZONE}"),
                "skipped_quarters": skipped,
                "derived_automatically": True,
                "manually_supplied": False,
            }
        entry["reason"] = "NOT_ELIGIBLE_FOR_PROSPECTIVE_CONFIRMATION — armed after the "
        "preceding session's close"
        skipped.append(entry)
    raise StateError(f"no eligible quarter end within {horizon} quarters of activation")


@dataclass(frozen=True)
class Arm:
    """The result of arming.

    Attributes:
        record: The immutable arm record.
        sha256: Its digest.
        written: False when an identical record already existed.
        first_eligible_decision: The derived decision session.
    """

    record: dict[str, Any]
    sha256: str
    written: bool
    first_eligible_decision: str


def arm(root: Path, binding: RunnerBinding, *, code_commit: str) -> Arm:
    """Arm the runner and derive the first eligible decision.

    Args:
        root: The prospective state root.
        binding: The verified runner binding.
        code_commit: The immutable commit the runner is bound to.

    Returns:
        The arm record and the derived first decision.

    Raises:
        ActivationError: If the protocol is not activated, or the semantic
            fingerprint has moved since activation.
    """
    state = load_state(root / STATE_FILE)
    activation_path = root / ACTIVATION_FILE
    if not activation_path.is_file():
        raise ActivationError("the protocol is not activated; nothing to arm")
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    if activation["code_commit"] != code_commit:
        raise ActivationError(
            f"activation is bound to {activation['code_commit']}, the runner is at "
            f"{code_commit}. Prospective packets bind to code; this is a semantic "
            "change, not a restart."
        )
    drift = compare(
        activation["semantic_code_fingerprint"],
        binding.semantic["semantic_fingerprint"],
    )
    if not drift["unchanged"]:
        raise ActivationError(
            f"the semantic code fingerprint moved since activation: {drift['consequence']}"
        )

    arm_path = root / ARM_FILE
    if arm_path.exists():
        # As with activation: a restart re-reads the arm record rather than
        # minting one whose only difference is the clock.
        existing = json.loads(arm_path.read_text(encoding="utf-8"))
        conflicts = {
            field: (existing.get(field), value)
            for field, value in (
                ("code_commit", code_commit),
                ("candidate_sha256", binding.candidate_sha256),
                ("protocol_sha256", binding.protocol_sha256),
                (
                    "semantic_code_fingerprint",
                    binding.semantic["semantic_fingerprint"],
                ),
            )
            if existing.get(field) != value
        }
        if conflicts:
            raise ActivationError(
                "an arm record already exists under a different binding "
                f"({sorted(conflicts)}); arming is recorded once and never "
                "rewritten"
            )
        return Arm(
            record=existing,
            sha256=packet_digest(existing),
            written=False,
            first_eligible_decision=str(existing["first_eligible_decision"]),
        )

    armed_at = _now()
    activated_at = datetime.fromisoformat(str(activation["activated_at_utc"]))
    derivation = derive_first_eligible_decision(activated_at=activated_at, armed_at=armed_at)
    record = {
        "stream_id": STREAM_ID,
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_ID,
        "runner_version": RUNNER_VERSION,
        "code_commit": code_commit,
        "armed_at_utc": armed_at.isoformat(),
        "armed_at_eastern": _eastern(armed_at).isoformat(),
        "candidate_sha256": binding.candidate_sha256,
        "protocol_sha256": binding.protocol_sha256,
        "semantic_code_fingerprint": binding.semantic["semantic_fingerprint"],
        "arm_status": "ARMED",
        **derivation,
        "why_this_record_exists": (
            "a later decision cannot claim the runner was armed unless a durable "
            "record predates it. This is that record."
        ),
    }
    state.require(ProspectiveState.ACTIVATED)
    state.transition(ProspectiveState.ARMED)
    written = finalise(arm_path, record)
    state.armed_at = record["armed_at_utc"]
    state.first_eligible_decision = derivation["first_eligible_decision"]
    state.skipped_quarters = list(derivation["skipped_quarters"])
    state.transition(ProspectiveState.WAITING_FOR_DECISION)
    save_state(root / STATE_FILE, state)
    _append_ledger(
        root,
        {
            "event": "RUNNER_ARMED",
            "at": record["armed_at_utc"],
            "code_commit": code_commit,
            "arm_sha256": written.sha256,
            "observation_kind": None,
        },
    )
    _append_ledger(
        root,
        {
            "event": "FIRST_ELIGIBLE_DECISION_DERIVED",
            "at": record["armed_at_utc"],
            "first_eligible_decision": derivation["first_eligible_decision"],
            "preceding_session": derivation["preceding_session"],
            "required_arm_cutoff": derivation["required_arm_cutoff"],
            "skipped_quarters": [row["quarter_end"] for row in derivation["skipped_quarters"]],
            "derived_automatically": True,
            "observation_kind": None,
        },
    )
    log.info(
        "prospective.armed",
        first_eligible_decision=derivation["first_eligible_decision"],
    )
    return Arm(
        record=record,
        sha256=written.sha256,
        written=True,
        first_eligible_decision=derivation["first_eligible_decision"],
    )


def record_incident(root: Path, *, kind: str, detail: str) -> dict[str, Any]:
    """Append an operational incident. Incidents are never deleted.

    Args:
        root: The prospective state root.
        kind: The incident class.
        detail: What happened.

    Returns:
        The incident as recorded.
    """
    state = load_state(root / STATE_FILE)
    entry = {"kind": kind, "detail": detail, "at": _now().isoformat()}
    state.incidents.append(entry)
    save_state(root / STATE_FILE, state)
    _append_ledger(root, {"event": "INCIDENT", **entry, "observation_kind": None})
    return entry


def status(root: Path, binding: RunnerBinding | None = None) -> dict[str, Any]:
    """Report where the experiment is, without exposing an interim reading.

    The criteria are reported as sealed rather than as a running total, so that
    nobody has to decide whether to look. Reading nothing is easier than reading
    something and disregarding it.

    Args:
        root: The prospective state root.
        binding: The current binding, for a drift check. Optional.

    Returns:
        A read-only view. Fetches nothing and mutates nothing.
    """
    state = load_state(root / STATE_FILE)
    activation_path = root / ACTIVATION_FILE
    arm_path = root / ARM_FILE
    activation = (
        json.loads(activation_path.read_text(encoding="utf-8"))
        if activation_path.is_file()
        else None
    )
    arm_record = json.loads(arm_path.read_text(encoding="utf-8")) if arm_path.is_file() else None
    drift: dict[str, Any] | None = None
    if binding is not None and activation is not None:
        drift = compare(
            activation["semantic_code_fingerprint"],
            binding.semantic["semantic_fingerprint"],
        )
    return {
        "stream_id": STREAM_ID,
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_ID,
        "runner_version": RUNNER_VERSION,
        "state": str(state.state),
        "activated_at": state.activated_at,
        "armed": state.armed_at is not None,
        "armed_at": state.armed_at,
        "first_eligible_decision": state.first_eligible_decision,
        "decision_instant": (
            f"{state.first_eligible_decision}T16:00:00 {SESSION_TIMEZONE}"
            if state.first_eligible_decision
            else None
        ),
        "preceding_session": (arm_record["preceding_session"] if arm_record else None),
        "required_arm_cutoff": (arm_record["required_arm_cutoff"] if arm_record else None),
        "cutoff_satisfied": arm_record["cutoff_satisfied"] if arm_record else None,
        "skipped_quarters": list(state.skipped_quarters),
        "decisions_captured": state.observations,
        "executions_captured": len(state.executions),
        "completed_transitions": state.completed_transitions,
        "required_decisions": REQUIRED_DECISIONS,
        "required_transitions": REQUIRED_TRANSITIONS,
        "criteria": CRITERIA_SEAL,
        "P1": "NOT_YET_EVALUATED",
        "P2": "NOT_YET_EVALUATED",
        "P3": "NOT_YET_EVALUATED",
        "P4": "NOT_YET_EVALUATED",
        "prospective_verdict": "PENDING",
        "performance_interpretation": "NOT_EVALUATED_FOR_RESEARCH_DECISION",
        "candidate_status": "PROSPECTIVE_CONFIRMATION_PENDING",
        "semantic_health": drift or {"checked": False},
        "incidents": len(state.incidents),
        "broker": "DISCONNECTED / NOT_AUTHORISED",
        "order_capability": "NONE",
        "fetches_data_to_display_status": False,
    }
