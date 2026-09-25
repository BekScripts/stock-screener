"""The prospective state machine, and the state that must survive a restart.

A quarterly experiment spends almost all of its life waiting, which means almost
every interesting failure happens while nothing is running. So the state lives
on disk rather than in a process, every transition is checked against a fixed
table, and the observation count is derived from the packets that exist rather
than from a counter someone could increment twice.

The states are ordered and none may be skipped. In particular there is no path
from `ARMED` to a captured decision that does not pass through waiting for the
decision instant, which is what stops a runner from capturing a quarter early.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "LEGAL_TRANSITIONS",
    "ProspectiveState",
    "RunnerState",
    "StateError",
    "load_state",
    "save_state",
]


class ProspectiveState(StrEnum):
    """Where the prospective experiment has got to."""

    DEFINED = "DEFINED"
    ACTIVATED = "ACTIVATED"
    ARMED = "ARMED"
    WAITING_FOR_DECISION = "WAITING_FOR_DECISION"
    DECISION_CAPTURED = "DECISION_CAPTURED"
    WAITING_FOR_EXECUTION_REFERENCE = "WAITING_FOR_EXECUTION_REFERENCE"
    EXECUTION_CAPTURED = "EXECUTION_CAPTURED"
    WINDOW_COMPLETE = "WINDOW_COMPLETE"
    EVALUATED = "EVALUATED"
    INVALIDATED_BY_DEFECT = "INVALIDATED_BY_DEFECT"


#: Every transition the runner may make. Anything absent is refused, so a new
#: path has to be added deliberately rather than arrived at.
LEGAL_TRANSITIONS: dict[ProspectiveState, frozenset[ProspectiveState]] = {
    ProspectiveState.DEFINED: frozenset({ProspectiveState.ACTIVATED}),
    ProspectiveState.ACTIVATED: frozenset({ProspectiveState.ARMED}),
    ProspectiveState.ARMED: frozenset({ProspectiveState.WAITING_FOR_DECISION}),
    ProspectiveState.WAITING_FOR_DECISION: frozenset(
        {ProspectiveState.DECISION_CAPTURED, ProspectiveState.INVALIDATED_BY_DEFECT}
    ),
    ProspectiveState.DECISION_CAPTURED: frozenset(
        {
            ProspectiveState.WAITING_FOR_EXECUTION_REFERENCE,
            ProspectiveState.INVALIDATED_BY_DEFECT,
        }
    ),
    ProspectiveState.WAITING_FOR_EXECUTION_REFERENCE: frozenset(
        {ProspectiveState.EXECUTION_CAPTURED, ProspectiveState.INVALIDATED_BY_DEFECT}
    ),
    ProspectiveState.EXECUTION_CAPTURED: frozenset(
        {
            ProspectiveState.WAITING_FOR_DECISION,
            ProspectiveState.WINDOW_COMPLETE,
            ProspectiveState.INVALIDATED_BY_DEFECT,
        }
    ),
    ProspectiveState.WINDOW_COMPLETE: frozenset({ProspectiveState.EVALUATED}),
    ProspectiveState.EVALUATED: frozenset(),
    ProspectiveState.INVALIDATED_BY_DEFECT: frozenset(),
}


class StateError(RuntimeError):
    """Raised on an illegal transition or an unreadable state."""


@dataclass
class RunnerState:
    """The durable prospective state.

    Attributes:
        state: Where the experiment has got to.
        stream_id: The prospective stream.
        candidate_id: The frozen candidate.
        protocol_id: The frozen protocol.
        code_commit: The immutable commit the runner is bound to.
        semantic_fingerprint: The semantic code fingerprint at activation.
        activated_at: When activation happened, UTC.
        armed_at: When arming happened, UTC.
        first_eligible_decision: The derived first decision session.
        decisions: Captured decision sessions, in order.
        executions: Captured execution sessions, in order.
        skipped_quarters: Quarters that never entered the sequence, with reasons.
        incidents: Operational incidents, append-only.
    """

    state: ProspectiveState = ProspectiveState.DEFINED
    stream_id: str = ""
    candidate_id: str = ""
    protocol_id: str = ""
    code_commit: str = ""
    semantic_fingerprint: str = ""
    activated_at: str | None = None
    armed_at: str | None = None
    first_eligible_decision: str | None = None
    decisions: list[str] = field(default_factory=list)
    executions: list[str] = field(default_factory=list)
    skipped_quarters: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)

    @property
    def observations(self) -> int:
        """How many decisions have been captured. Derived, never counted."""
        return len(self.decisions)

    @property
    def completed_transitions(self) -> int:
        """Transitions between consecutive captured decisions."""
        return max(len(self.decisions) - 1, 0)

    def require(self, expected: ProspectiveState) -> None:
        """Refuse unless the runner is in a given state.

        Args:
            expected: The state required.

        Raises:
            StateError: If the runner is elsewhere.
        """
        if self.state is not expected:
            raise StateError(f"the runner is {self.state}, not {expected}")

    def transition(self, to: ProspectiveState) -> None:
        """Move to a new state, or refuse.

        Args:
            to: The state to move to.

        Raises:
            StateError: If the transition is not in the table.
        """
        allowed = LEGAL_TRANSITIONS[self.state]
        if to not in allowed:
            raise StateError(
                f"{self.state} may not become {to}; legal: {sorted(allowed) or 'none'}"
            )
        self.state = to

    def as_json(self) -> dict[str, Any]:
        """Return the state as a JSON-safe mapping."""
        return {
            "state": str(self.state),
            "stream_id": self.stream_id,
            "candidate_id": self.candidate_id,
            "protocol_id": self.protocol_id,
            "code_commit": self.code_commit,
            "semantic_fingerprint": self.semantic_fingerprint,
            "activated_at": self.activated_at,
            "armed_at": self.armed_at,
            "first_eligible_decision": self.first_eligible_decision,
            "decisions": list(self.decisions),
            "executions": list(self.executions),
            "observations": self.observations,
            "completed_transitions": self.completed_transitions,
            "skipped_quarters": list(self.skipped_quarters),
            "incidents": list(self.incidents),
        }


def load_state(path: Path) -> RunnerState:
    """Read the durable state, or return a fresh one.

    Args:
        path: The state file.

    Returns:
        The state as it was left.

    Raises:
        StateError: If the file exists but cannot be read as state.
    """
    if not path.is_file():
        return RunnerState()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"the prospective state at {path} is unreadable: {exc}") from exc
    return RunnerState(
        state=ProspectiveState(str(body["state"])),
        stream_id=str(body.get("stream_id", "")),
        candidate_id=str(body.get("candidate_id", "")),
        protocol_id=str(body.get("protocol_id", "")),
        code_commit=str(body.get("code_commit", "")),
        semantic_fingerprint=str(body.get("semantic_fingerprint", "")),
        activated_at=body.get("activated_at"),
        armed_at=body.get("armed_at"),
        first_eligible_decision=body.get("first_eligible_decision"),
        decisions=list(body.get("decisions") or []),
        executions=list(body.get("executions") or []),
        skipped_quarters=list(body.get("skipped_quarters") or []),
        incidents=list(body.get("incidents") or []),
    )


def save_state(path: Path, state: RunnerState) -> str:
    """Write the durable state.

    Args:
        path: The state file.
        state: The state to persist.

    Returns:
        The state's digest.
    """
    import hashlib

    body = json.dumps(state.as_json(), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
