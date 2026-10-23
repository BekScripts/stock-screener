"""Stage E: the execution reference, which records a price and places nothing.

R1.11 found `execution_packet()` with zero production callers too. This is that
caller, and it is deliberately a separate stage rather than a continuation of the
decision, because it needs a fact the decision could not have: the open of the
next session.

That is the whole reason this cannot be folded into stage D. The frozen packet
requires `reference_opens`, so the execution reference genuinely has to wait for
the execution session to open. Writing it at decision time would mean either
inventing a price or moving the decision's own close backwards into it, and both
are the look-ahead the stream exists to avoid.

The session is derived, never written down. The window currently armed implies a
January session, but the code asks the frozen calendar rule rather than carrying
the date, so a holiday or an unscheduled closure moves it correctly.

Nothing here can place an order. An execution *reference* is the price the
decision would have executed at, recorded after the fact for measurement. The
stream has no broker authorisation and no order object exists anywhere in it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.prospective_runner import (
    ProspectiveState,
    execution_packet,
    finalise,
    load_state,
    next_session,
    packet_digest,
    save_state,
)
from stock_screener.prospective_source.adapters import session_close

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date
    from pathlib import Path

log = structlog.get_logger(__name__)

__all__ = [
    "EXECUTION_FILE",
    "ExecutionCapture",
    "ExecutionRefusedError",
    "capture_execution_reference",
    "derive_execution_session",
]

#: One execution packet per decision session, sharing its key.
EXECUTION_FILE = "execution_{session}.json"


class ExecutionRefusedError(RuntimeError):
    """Raised when an execution reference must not be produced."""


def derive_execution_session(decision_session: date) -> date:
    """The session whose open is the execution reference.

    Derived from the frozen calendar rule — the first session strictly after the
    decision. Never a written-down date, so a holiday or an unscheduled closure
    moves it rather than silently mispricing the reference.

    Args:
        decision_session: The session decided.

    Returns:
        The execution reference session.
    """
    return next_session(decision_session)


@dataclass(frozen=True)
class ExecutionCapture:
    """The result of a stage E capture.

    Attributes:
        decision_session: The decision this prices.
        execution_session: The session whose open was used.
        packet_path: Where the packet landed.
        packet_digest: Its digest.
        written: False when an identical packet already existed.
        capture_status: `COMPLETE`, `INCOMPLETE` or `UNAVAILABLE`.
        priced: How many targets priced.
        unpriced: How many did not.
        state: The state after the capture.
    """

    decision_session: str
    execution_session: str
    packet_path: Path
    packet_digest: str
    written: bool
    capture_status: str
    priced: int
    unpriced: int
    state: str

    def as_json(self) -> dict[str, Any]:
        """Return the capture as a JSON-safe mapping."""
        return {
            "decision_session": self.decision_session,
            "execution_session": self.execution_session,
            "packet_path": str(self.packet_path),
            "packet_sha256": self.packet_digest,
            "written": self.written,
            "capture_status": self.capture_status,
            "priced": self.priced,
            "unpriced": self.unpriced,
            "state": self.state,
            "broker_requests": 0,
            "orders_placed": 0,
        }


def capture_execution_reference(
    directory: Path,
    *,
    decision_session: str,
    opens: Mapping[str, float | None],
    source: Mapping[str, Any],
    state_path: Path,
    now: datetime,
) -> ExecutionCapture:
    """Record the execution reference for an existing decision.

    Args:
        directory: Where the window's packets live.
        decision_session: The decision to price.
        opens: `{"KIND:identity_id": open}` for the execution session, None where
            unpriced. Keyed by identity rather than by ticker, because the frozen
            packet records identities and the contract makes a ticker a dated
            display attribute. Resolving the symbol is the caller's job, so a
            renamed listing cannot silently reprice a different security here.
        source: The price provider's descriptor.
        state_path: The runner's state file.
        now: The current moment. Must be aware.

    Returns:
        The capture.

    Raises:
        ExecutionRefusedError: If no decision packet exists, or the execution
            session's open is not yet available.
    """
    decision_path = directory / f"decision_{decision_session}.json"
    if not decision_path.is_file():
        raise ExecutionRefusedError(
            f"NO_PRIOR_DECISION: {decision_path.name} does not exist. An execution "
            "reference prices a decision that was actually taken; it never stands alone."
        )
    body = json.loads(decision_path.read_text(encoding="utf-8"))
    decision_digest = packet_digest(body)

    session = derive_execution_session(datetime.fromisoformat(decision_session).date())
    if body.get("execution_session") != session.isoformat():
        raise ExecutionRefusedError(
            f"the decision packet names execution session {body.get('execution_session')} "
            f"and the calendar derives {session.isoformat()}"
        )
    # The open exists only once the session has opened. Asking earlier would
    # either fail or invite substituting the prior close, which the contract
    # forbids outright.
    if now < session_close(session).replace(hour=9, minute=30):
        raise ExecutionRefusedError(
            f"EXECUTION_SESSION_NOT_OPEN: {session.isoformat()} has not opened. The "
            "reference open is a fact about that session and is never back-filled "
            "from the decision close or the prior close."
        )

    targets = [f"{row['kind']}:{row['identity_id']}" for row in body.get("h2_target_set") or ()]
    reference: dict[str, float | None] = {}
    unpriced: list[dict[str, Any]] = []
    for identity in sorted(targets):
        price = opens.get(identity)
        reference[identity] = price
        if price is None:
            unpriced.append({"identity": identity, "reason": "UNPRICED", "substitution": "NONE"})
    status = "COMPLETE" if not unpriced else ("UNAVAILABLE" if not reference else "INCOMPLETE")

    packet = execution_packet(
        packet_key=decision_session,
        decision_packet_sha256=decision_digest,
        execution_session=session.isoformat(),
        captured_at=now.isoformat(),
        reference_opens=reference,
        unpriced=unpriced,
        source=dict(source),
        capture_status=status,
    )
    path = directory / EXECUTION_FILE.format(session=decision_session)
    sealed = finalise(path, packet)
    recovered = json.loads(path.read_text(encoding="utf-8"))
    if packet_digest(recovered) != sealed.sha256:
        raise ExecutionRefusedError(f"the written packet does not read back as written: {path}")

    state = load_state(state_path)
    if session.isoformat() not in state.executions:
        if state.state is ProspectiveState.DECISION_CAPTURED:
            state.transition(ProspectiveState.WAITING_FOR_EXECUTION_REFERENCE)
        if state.state is ProspectiveState.WAITING_FOR_EXECUTION_REFERENCE:
            state.transition(ProspectiveState.EXECUTION_CAPTURED)
        state.executions.append(session.isoformat())
        save_state(state_path, state)

    log.info(
        "prospective.execution_captured",
        decision=decision_session,
        execution=session.isoformat(),
        status=status,
        priced=len(reference) - len(unpriced),
    )
    return ExecutionCapture(
        decision_session=decision_session,
        execution_session=session.isoformat(),
        packet_path=path,
        packet_digest=sealed.sha256,
        written=sealed.written,
        capture_status=status,
        priced=len(reference) - len(unpriced),
        unpriced=len(unpriced),
        state=str(load_state(state_path).state),
    )
