"""Frozen strategy decisions, and the lifecycle that keeps them frozen.

This module exists because of one semantic that the whole phase turns on: for
`ALPHATRADER_COMPOUNDER_MONTHLY_V1` a decision and its execution are **different
market events**.

    decision   the last market session of the month, at its 16:00 ET close
    execution  the first session strictly after it, near the open

Between those two instants the decision must not change. Not because change
would be hard, but because the obvious alternative — re-ranking on execution
morning — would silently produce a different portfolio from the one that was
decided, authorised and recorded, while still carrying the decision's identity.
The ranking would be computed from data the decision never saw, which is the
same look-ahead the research contract spent a phase eliminating, reintroduced at
the operational layer.

So a decision is written down once, fingerprinted, and read back. The execution
run loads it; it does not recompute it. If current state makes the frozen
decision unexecutable, that is reported — the decision is never quietly
adjusted to fit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.strategies import StrategyDecision

__all__ = [
    "DECISIONS_DIRECTORY",
    "PERSISTED_DECISION_VERSION",
    "DecisionStatus",
    "PersistedStrategyDecision",
    "StrategyDecisionStore",
]

#: The persisted-decision contract.
PERSISTED_DECISION_VERSION = "PERSISTED_STRATEGY_DECISION_V1"

#: Where decisions live, under the runtime namespace.
DECISIONS_DIRECTORY = "decisions"


class DecisionStatus(StrEnum):
    """Where a frozen decision has got to.

    An explicit state machine rather than something inferred from broker
    positions. Inferring it would mean a reconciliation break, a partial fill or
    a risk cut could all look like "not executed yet", and the runtime would
    retry an execution that had in fact happened.
    """

    DECIDED = "DECIDED"
    """Recorded. Not yet executable — its session has not arrived."""

    PENDING_EXECUTION = "PENDING_EXECUTION"
    """Its execution session has arrived and the window is open."""

    EXECUTION_STARTED = "EXECUTION_STARTED"
    """An execution run took it. Durable before anything was submitted, so a
    crash mid-submission does not look like a decision never attempted."""

    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    """Executed. Terminal."""

    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    """A gate refused during the execution window. Not terminal in itself: the
    window may still be open. Past the window it becomes missed."""

    EXECUTION_MISSED = "EXECUTION_MISSED"
    """The execution window closed without executing. Terminal, and never
    backfilled — an afternoon fill of a decision meant for the open is a
    different trade, taken on information the decision did not have."""

    SUPERSEDED = "SUPERSEDED"
    """A later valid decision replaced it before it executed. Terminal."""

    @property
    def is_terminal(self) -> bool:
        """Whether nothing further can happen to this decision."""
        return self in {
            DecisionStatus.EXECUTION_COMPLETED,
            DecisionStatus.EXECUTION_MISSED,
            DecisionStatus.SUPERSEDED,
        }

    @property
    def may_execute(self) -> bool:
        """Whether an execution run may still take this decision."""
        return self in {
            DecisionStatus.PENDING_EXECUTION,
            DecisionStatus.EXECUTION_STARTED,
            DecisionStatus.EXECUTION_BLOCKED,
        }


#: The only transitions a decision may make. `EXECUTION_STARTED` may return to
#: `EXECUTION_BLOCKED` because a gate can refuse after the run began — a kill
#: between two orders, for instance. It may not return to `PENDING_EXECUTION`,
#: because that would let a run that had already submitted be treated as one
#: that had not.
DECISION_TRANSITIONS: dict[DecisionStatus, frozenset[DecisionStatus]] = {
    DecisionStatus.DECIDED: frozenset(
        {
            DecisionStatus.PENDING_EXECUTION,
            DecisionStatus.EXECUTION_MISSED,
            DecisionStatus.SUPERSEDED,
        }
    ),
    DecisionStatus.PENDING_EXECUTION: frozenset(
        {
            DecisionStatus.EXECUTION_STARTED,
            DecisionStatus.EXECUTION_BLOCKED,
            DecisionStatus.EXECUTION_MISSED,
            DecisionStatus.SUPERSEDED,
        }
    ),
    DecisionStatus.EXECUTION_STARTED: frozenset(
        {
            DecisionStatus.EXECUTION_COMPLETED,
            DecisionStatus.EXECUTION_BLOCKED,
            DecisionStatus.EXECUTION_MISSED,
        }
    ),
    DecisionStatus.EXECUTION_BLOCKED: frozenset(
        {
            DecisionStatus.EXECUTION_STARTED,
            DecisionStatus.EXECUTION_MISSED,
            DecisionStatus.SUPERSEDED,
        }
    ),
    DecisionStatus.EXECUTION_COMPLETED: frozenset(),
    DecisionStatus.EXECUTION_MISSED: frozenset(),
    DecisionStatus.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True)
class PersistedStrategyDecision:
    """A strategy decision, frozen, with its operational lifecycle.

    Attributes:
        decision: The strategy's own immutable decision.
        status: Where it has got to.
        runtime_id: Which runtime owns it.
        strategy_authorization_fingerprint: The grant in force when it was made.
        decision_run_id: The operational run that recorded it.
        execution_run_id: The operational run that executed it, when one did.
        recorded_at: When it was written.
        history: Every status change, append-only, as `(instant, status, why)`.
    """

    decision: StrategyDecision
    status: DecisionStatus
    runtime_id: str
    strategy_authorization_fingerprint: str
    decision_run_id: str = ""
    execution_run_id: str = ""
    recorded_at: str = ""
    history: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def decision_fingerprint(self) -> str:
        """The strategy decision's digest — the thing that must not change."""
        return self.decision.fingerprint()

    @property
    def decision_session(self) -> str:
        """The session the decision was taken in."""
        return self.decision.decision_session

    @property
    def first_execution_session(self) -> str:
        """The earliest session it may be executed in."""
        return self.decision.first_execution_session

    def advanced(
        self, status: DecisionStatus, *, at: str, why: str = "", execution_run_id: str = ""
    ) -> PersistedStrategyDecision:
        """Return this decision at a new status, refusing an illegal move.

        Args:
            status: The proposed status.
            at: When, ISO-8601.
            why: A sanitised note.
            execution_run_id: The executing run, where this records one.

        Returns:
            A new value at the new status, with the move appended to history.

        Raises:
            RuntimeBindingError: `DECISION_ALREADY_EXECUTED` when the current
                status is terminal, and the same code for an illegal move. The
                check happens before anything is written, which is the P1.4
                lesson: a record containing an impossible transition is worse
                than a refused write, because it is the record a later reader
                trusts.
        """
        if status is self.status:
            return self
        allowed = DECISION_TRANSITIONS[self.status]
        if status not in allowed:
            raise RuntimeBindingError(
                Code.DECISION_ALREADY_EXECUTED,
                f"decision {self.decision.decision_id} is {self.status} and may not "
                f"become {status}; legal moves are {sorted(str(row) for row in allowed)}",
            )
        return PersistedStrategyDecision(
            decision=self.decision,
            status=status,
            runtime_id=self.runtime_id,
            strategy_authorization_fingerprint=self.strategy_authorization_fingerprint,
            decision_run_id=self.decision_run_id,
            execution_run_id=execution_run_id or self.execution_run_id,
            recorded_at=self.recorded_at,
            history=(*self.history, (at, str(status), why)),
        )

    def body(self) -> dict[str, Any]:
        """Return the record's semantic content. Carries no credential."""
        return {
            "version": PERSISTED_DECISION_VERSION,
            "runtime_id": self.runtime_id,
            "status": str(self.status),
            "decision_run_id": self.decision_run_id,
            "execution_run_id": self.execution_run_id,
            "recorded_at": self.recorded_at,
            "strategy_authorization_fingerprint": self.strategy_authorization_fingerprint,
            "decision": self.decision.as_json(),
            "decision_fingerprint": self.decision_fingerprint,
            "history": [list(row) for row in self.history],
        }

    def as_json(self) -> dict[str, Any]:
        """Return the record as a JSON-safe mapping."""
        return self.body()

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PersistedStrategyDecision:
        """Rebuild a persisted decision, verifying it was not altered.

        The fingerprint is recomputed from the stored decision and compared
        against the stored fingerprint. That is the whole point of writing both:
        a decision whose selections were edited on disk would otherwise execute
        a portfolio nobody decided, under the identity of one somebody did.

        Args:
            body: What was written.

        Returns:
            The record.

        Raises:
            RuntimeBindingError: `DECISION_TAMPERED` if the recomputed
                fingerprint differs from the stored one, or if the record is not
                this contract version, or if a field cannot be read.
        """
        from stock_screener.portfolio_risk import SecurityIdentity
        from stock_screener.strategies import StrategyDecision, StrategySelection

        if str(body.get("version")) != PERSISTED_DECISION_VERSION:
            raise RuntimeBindingError(
                Code.DECISION_TAMPERED,
                f"decision record is {body.get('version')!r}, expected "
                f"{PERSISTED_DECISION_VERSION!r}",
            )
        try:
            raw = dict(body["decision"])
            decision = StrategyDecision(
                strategy_id=str(raw["strategy_id"]),
                strategy_version=str(raw["strategy_version"]),
                strategy_fingerprint=str(raw["strategy_fingerprint"]),
                decision_session=str(raw["decision_session"]),
                decision_at=str(raw["decision_at"]),
                first_execution_session=str(raw["first_execution_session"]),
                input_fingerprint=str(raw["input_fingerprint"]),
                universe_fingerprint=str(raw["universe_fingerprint"]),
                calendar_fingerprint=str(raw["calendar_fingerprint"]),
                eligible_count=int(raw["eligible_count"]),
                considered_count=int(raw["considered_count"]),
                target_count=int(raw["target_count"]),
                selections=tuple(
                    StrategySelection(
                        identity=_identity(row["identity"], SecurityIdentity),
                        final_score=float(row["final_score"]),
                        rank=int(row["rank"]),
                        desired_weight=float(row["desired_weight"]),
                    )
                    for row in raw.get("selections", ())
                ),
                exits=tuple(_identity(label, SecurityIdentity) for label in raw.get("exits", ())),
                previous_decision_fingerprint=raw.get("previous_decision_fingerprint"),
                excluded_counts={
                    str(k): int(v) for k, v in dict(raw.get("excluded_counts", {})).items()
                },
            )
            record = cls(
                decision=decision,
                status=DecisionStatus(str(body["status"])),
                runtime_id=str(body["runtime_id"]),
                strategy_authorization_fingerprint=str(
                    body.get("strategy_authorization_fingerprint", "")
                ),
                decision_run_id=str(body.get("decision_run_id", "")),
                execution_run_id=str(body.get("execution_run_id", "")),
                recorded_at=str(body.get("recorded_at", "")),
                history=tuple(
                    (str(row[0]), str(row[1]), str(row[2]) if len(row) > 2 else "")
                    for row in body.get("history", ())
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeBindingError(
                Code.DECISION_TAMPERED,
                f"decision record could not be read: {type(exc).__name__}. It is "
                "refused rather than partially trusted",
            ) from exc
        stored = str(body.get("decision_fingerprint", ""))
        if stored and record.decision_fingerprint != stored:
            raise RuntimeBindingError(
                Code.DECISION_TAMPERED,
                f"decision {record.decision.decision_id} recomputes to "
                f"{record.decision_fingerprint[:16]}… and was stored as {stored[:16]}…. "
                "Its contents were altered after it was recorded; executing it would "
                "trade a portfolio nobody decided under the identity of one somebody "
                "did",
            )
        return record


def _identity(raw: Any, cls: Any) -> Any:
    """Read an identity from either its label form or its mapping form.

    Args:
        raw: `"COMPANY:12"` or `{"kind": ..., "identity_id": ...}`.
        cls: The `SecurityIdentity` class.

    Returns:
        The identity.

    Raises:
        ValueError: If the label is not `KIND:id`.
    """
    if isinstance(raw, dict):
        return cls(
            kind=str(raw["kind"]),
            identity_id=int(raw["identity_id"]),
            symbol=raw.get("symbol"),
        )
    kind, _, identifier = str(raw).partition(":")
    if not identifier:
        msg = f"{raw!r} is not an identity label of the form KIND:id"
        raise ValueError(msg)
    return cls(kind=kind, identity_id=int(identifier))


@dataclass
class StrategyDecisionStore:
    """Where a runtime's frozen decisions live.

    One file per decision session, named by session, so a decision is findable
    by the session that produced it and an execution run can ask for exactly
    the one it is meant to execute.

    Attributes:
        root: The operations root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root and locate the decisions namespace."""
        from pathlib import Path as _Path

        from stock_screener.paper_operations import guard_operations_root

        from .binding import RUNTIME_DIRECTORY

        self.root = _Path(self.root)
        guard_operations_root(self.root)
        self.directory = self.root / RUNTIME_DIRECTORY / DECISIONS_DIRECTORY

    def path_for(self, decision_session: str) -> Path:
        """Where a decision session's record lives."""
        return self.directory / f"{decision_session}.json"

    def record(self, decision: PersistedStrategyDecision) -> Path:
        """Write a decision, refusing to replace a different one.

        Args:
            decision: The record.

        Returns:
            Where it was written.

        Raises:
            RuntimeBindingError: `DECISION_TAMPERED` if a decision already
                exists for this session with a different fingerprint. Recording
                the identical decision again is a no-op, which is what makes a
                repeated decision slot idempotent; replacing it with a different
                selection would be a silent re-rank.
        """
        from stock_screener.paper_operations import atomic_write_verified

        existing = self.read(decision.decision_session)
        if existing is not None and existing.decision_fingerprint != decision.decision_fingerprint:
            raise RuntimeBindingError(
                Code.DECISION_TAMPERED,
                f"session {decision.decision_session} already holds decision "
                f"{existing.decision_fingerprint[:16]}… and this one is "
                f"{decision.decision_fingerprint[:16]}…. A decision is frozen once "
                "taken; replacing it would be a silent re-rank of a session that has "
                "already been decided",
            )
        atomic_write_verified(self.path_for(decision.decision_session), decision.as_json())
        return self.path_for(decision.decision_session)

    def update(self, decision: PersistedStrategyDecision) -> Path:
        """Write a status change for an existing decision.

        Args:
            decision: The record, already advanced.

        Returns:
            Where it was written.
        """
        from stock_screener.paper_operations import atomic_write_verified

        atomic_write_verified(self.path_for(decision.decision_session), decision.as_json())
        return self.path_for(decision.decision_session)

    def read(self, decision_session: str) -> PersistedStrategyDecision | None:
        """Read one session's decision.

        Args:
            decision_session: The session.

        Returns:
            The record, or `None` when that session has none.

        Raises:
            RuntimeBindingError: `DECISION_TAMPERED` if a record exists and
                cannot be trusted.
        """
        path = self.path_for(decision_session)
        if not path.exists():
            return None
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeBindingError(
                Code.DECISION_TAMPERED,
                f"decision at {path.name} could not be parsed: {type(exc).__name__}",
            ) from exc
        return PersistedStrategyDecision.from_body(body)

    def require(self, decision_session: str) -> PersistedStrategyDecision:
        """Read one session's decision, refusing to proceed without it.

        Args:
            decision_session: The session.

        Returns:
            The record.

        Raises:
            RuntimeBindingError: `DECISION_NOT_FOUND`. An execution run that
                could not find its decision must stop, never decide one: a
                decision taken on execution morning would be taken on data the
                decision session never saw.
        """
        record = self.read(decision_session)
        if record is None:
            raise RuntimeBindingError(
                Code.DECISION_NOT_FOUND,
                f"no frozen decision for session {decision_session}. An execution run "
                "never decides: it loads what was decided, or it stops",
            )
        return record

    def all(self) -> tuple[PersistedStrategyDecision, ...]:
        """Every decision this runtime holds, oldest session first."""
        if not self.directory.exists():
            return ()
        return tuple(
            sorted(
                (
                    PersistedStrategyDecision.from_body(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                    for path in self.directory.glob("*.json")
                ),
                key=lambda row: row.decision_session,
            )
        )

    def latest(self) -> PersistedStrategyDecision | None:
        """The most recent decision by session, or `None`."""
        rows = self.all()
        return rows[-1] if rows else None

    def pending(self) -> PersistedStrategyDecision | None:
        """The decision awaiting execution, if any.

        Returns:
            The most recent non-terminal decision, or `None`. At most one should
            ever be non-terminal, because a new decision supersedes an
            unexecuted predecessor — but the newest is returned rather than
            asserting uniqueness, so a damaged store reports a pending decision
            instead of raising inside a status read.
        """
        open_rows = [row for row in self.all() if not row.status.is_terminal]
        return open_rows[-1] if open_rows else None
