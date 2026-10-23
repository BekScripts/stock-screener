"""Incidents: recorded, ranked, and never self-repairing.

An incident is a finding about the system's own state, and the discipline is
inherited from P1.3's reconciliation: it is reported, not fixed. There is no
function here that sets local positions from remote, deletes an unexpected
order, rewrites a ledger or clears a `REMOTE_STATE_UNKNOWN`. That absence is
tested for, because the permissive version is a one-liner and it destroys the
evidence that something went wrong.

The lifecycle is append-only. An incident does not go from `OPEN` to absent — it
accumulates `OPENED`, `ACKNOWLEDGED`, `RESOLVED` events, each with its own
evidence. The reason is that "this was a problem and someone looked at it" and
"this was never a problem" are different histories, and a mutable status field
cannot tell them apart a week later.

Severity decides behaviour, not tone. `BLOCKING` stops new submissions.
`CRITICAL` additionally kills, and the list of conditions that qualify is frozen
and short — see `CRITICAL_AUTO_KILL_REASONS`. Escalating ordinary trouble to
critical would train an operator to clear kills without reading them, which is
worse than not having one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Code, OperationsError
from .kill_switch import CRITICAL_AUTO_KILL_REASONS
from .persistence import atomic_write_verified, guard_operations_root, read_json
from .versions import INCIDENT_POLICY

if TYPE_CHECKING:
    from .clock import Clock

__all__ = [
    "INCIDENTS_DIRECTORY",
    "IncidentEvent",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentStore",
    "OperationalIncident",
]

#: Where incidents live inside the operational root.
INCIDENTS_DIRECTORY = "incidents"


class IncidentSeverity(StrEnum):
    """How much a finding threatens the next decision."""

    INFO = "INFO"
    """Recorded so it is not rediscovered. Changes nothing."""

    WARNING = "WARNING"
    """A real difference that does not by itself stop a run."""

    BLOCKING = "BLOCKING"
    """New submissions stop. Existing owned orders are not cancelled by this
    alone — that is the kill policy's decision, not severity's."""

    CRITICAL = "CRITICAL"
    """The system has lost track of what is true. Blocks submissions and
    activates the persistent kill switch."""

    @property
    def blocks_submission(self) -> bool:
        """Whether this severity stops a new external submission."""
        return self in {IncidentSeverity.BLOCKING, IncidentSeverity.CRITICAL}

    @property
    def triggers_kill(self) -> bool:
        """Whether this severity activates the persistent kill switch."""
        return self is IncidentSeverity.CRITICAL


class IncidentStatus(StrEnum):
    """Where an incident is in its life."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class IncidentEvent:
    """One thing that happened to an incident.

    Attributes:
        status: What the incident became.
        at: When, ISO-8601 operational time.
        actor: `OPERATOR` or `AUTOMATIC`.
        note: A sanitised explanation.
        evidence_fingerprint: What established it, where there is something.
    """

    status: IncidentStatus
    at: str
    actor: str = "AUTOMATIC"
    note: str = ""
    evidence_fingerprint: str = ""

    def as_json(self) -> dict[str, Any]:
        """Return the event as a JSON-safe mapping."""
        return {
            "status": str(self.status),
            "at": self.at,
            "actor": self.actor,
            "note": self.note,
            "evidence_fingerprint": self.evidence_fingerprint,
        }


@dataclass(frozen=True)
class OperationalIncident:
    """One finding about the system's own state.

    Attributes:
        incident_id: A stable name, derived from the finding rather than
            generated, so re-detecting the same condition updates one incident
            instead of opening a new one each run.
        reason_code: What was found, as a stable code.
        severity: How much it threatens the next decision.
        detected_at: When it was first seen.
        detail: A sanitised explanation. Never a credential.
        run_id: Which run found it, where a run did.
        local_evidence_fingerprint: What the local records said.
        remote_evidence_fingerprint: What the venue said.
        history: The append-only lifecycle.
    """

    incident_id: str
    reason_code: str
    severity: IncidentSeverity
    detected_at: str
    detail: str = ""
    run_id: str = ""
    local_evidence_fingerprint: str = ""
    remote_evidence_fingerprint: str = ""
    history: tuple[IncidentEvent, ...] = field(default_factory=tuple)
    policy: str = INCIDENT_POLICY

    @property
    def status(self) -> IncidentStatus:
        """Where the incident currently stands.

        Derived from the history rather than stored, so there is no mutable
        status field that could disagree with the record of how it got there.
        """
        return self.history[-1].status if self.history else IncidentStatus.OPEN

    @property
    def is_open(self) -> bool:
        """Whether this incident still stands."""
        return self.status is not IncidentStatus.RESOLVED

    @property
    def blocks_submission(self) -> bool:
        """Whether this incident currently stops a new submission."""
        return self.is_open and self.severity.blocks_submission

    @classmethod
    def derive_id(cls, *, reason_code: str, subject: str, account_fingerprint: str) -> str:
        """Derive an incident id from the condition it describes.

        Derived rather than generated so that re-detecting the same condition on
        the next run touches the same incident. A generated id would open a
        fresh incident every run, and a hundred copies of one problem reads as a
        hundred problems.

        Args:
            reason_code: What was found.
            subject: What it concerns — an identity, a symbol, an order id.
            account_fingerprint: Which account.

        Returns:
            The id.
        """
        import hashlib

        digest = hashlib.sha256(
            f"{reason_code}|{subject}|{account_fingerprint}".encode()
        ).hexdigest()
        return f"inc-{digest[:24]}"

    def with_event(self, event: IncidentEvent) -> OperationalIncident:
        """Append a lifecycle event.

        Args:
            event: What happened.

        Returns:
            A new incident with the event appended. The original is unchanged —
            the history is append-only, so nothing is ever rewritten.

        Raises:
            OperationsError: `INCIDENT_AUTO_REPAIR_NOT_AUTHORIZED` if an
                automatic actor tries to resolve a blocking incident. A machine
                may detect and may escalate; deciding that a break is *settled*
                is an operator's call, because the alternative is a system that
                clears its own alarms.
        """
        if (
            event.status is IncidentStatus.RESOLVED
            and event.actor != "OPERATOR"
            and self.severity.blocks_submission
        ):
            raise OperationsError(
                Code.INCIDENT_AUTO_REPAIR_NOT_AUTHORIZED,
                f"{self.incident_id} ({self.reason_code}) is {self.severity} and "
                "cannot be resolved automatically. Detection and escalation are "
                "automatic; deciding a break is settled is an operator's call, "
                "because a system that clears its own alarms has no alarms",
            )
        return OperationalIncident(
            incident_id=self.incident_id,
            reason_code=self.reason_code,
            severity=self.severity,
            detected_at=self.detected_at,
            detail=self.detail,
            run_id=self.run_id,
            local_evidence_fingerprint=self.local_evidence_fingerprint,
            remote_evidence_fingerprint=self.remote_evidence_fingerprint,
            history=(*self.history, event),
        )

    def body(self) -> dict[str, Any]:
        """Return the incident's semantic content."""
        return {
            "policy": self.policy,
            "incident_id": self.incident_id,
            "reason_code": self.reason_code,
            "severity": str(self.severity),
            "status": str(self.status),
            "detail": self.detail,
            "run_id": self.run_id,
            "local_evidence_fingerprint": self.local_evidence_fingerprint,
            "remote_evidence_fingerprint": self.remote_evidence_fingerprint,
            "blocks_submission": self.blocks_submission,
            "triggers_kill": self.severity.triggers_kill,
            "auto_repair": "NONE",
        }

    def fingerprint(self) -> str:
        """Digest the incident. Excludes timestamps so it is a value."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the incident with its history and fingerprint."""
        return {
            **self.body(),
            "detected_at": self.detected_at,
            "history": [event.as_json() for event in self.history],
            "incident_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> OperationalIncident:
        """Rebuild an incident read back from disk.

        Args:
            body: What was written.

        Returns:
            The incident.

        Raises:
            OperationsError: `OPERATIONS_PARTIAL_RECORD` if it cannot be read.
        """
        try:
            return cls(
                incident_id=str(body["incident_id"]),
                reason_code=str(body["reason_code"]),
                severity=IncidentSeverity(str(body["severity"])),
                detected_at=str(body.get("detected_at", "")),
                detail=str(body.get("detail", "")),
                run_id=str(body.get("run_id", "")),
                local_evidence_fingerprint=str(body.get("local_evidence_fingerprint", "")),
                remote_evidence_fingerprint=str(body.get("remote_evidence_fingerprint", "")),
                history=tuple(
                    IncidentEvent(
                        status=IncidentStatus(str(row["status"])),
                        at=str(row.get("at", "")),
                        actor=str(row.get("actor", "AUTOMATIC")),
                        note=str(row.get("note", "")),
                        evidence_fingerprint=str(row.get("evidence_fingerprint", "")),
                    )
                    for row in body.get("history", ())
                ),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.OPERATIONS_PARTIAL_RECORD,
                f"an incident record could not be read: {exc}",
            ) from exc


@dataclass
class IncidentStore:
    """The durable, append-only home of incidents.

    Attributes:
        root: The operational root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root.

        Raises:
            OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if it lies in
                another trust domain.
        """
        self.root = Path(self.root)
        guard_operations_root(self.root)
        self.directory = self.root / INCIDENTS_DIRECTORY

    def record(
        self,
        *,
        clock: Clock,
        reason_code: str,
        severity: IncidentSeverity,
        subject: str,
        account_fingerprint: str,
        detail: str = "",
        run_id: str = "",
        local_evidence_fingerprint: str = "",
        remote_evidence_fingerprint: str = "",
    ) -> OperationalIncident:
        """Open an incident, or append a re-detection to an existing one.

        Args:
            clock: The operational clock.
            reason_code: What was found.
            severity: How much it threatens the next decision.
            subject: What it concerns.
            account_fingerprint: Which account.
            detail: A sanitised explanation.
            run_id: Which run found it.
            local_evidence_fingerprint: What the local records said.
            remote_evidence_fingerprint: What the venue said.

        Returns:
            The incident.
        """
        incident_id = OperationalIncident.derive_id(
            reason_code=reason_code, subject=subject, account_fingerprint=account_fingerprint
        )
        existing = self.get(incident_id)
        now = clock.iso()
        if existing is not None:
            incident = existing.with_event(
                IncidentEvent(
                    status=existing.status,
                    at=now,
                    actor="AUTOMATIC",
                    note=f"re-detected: {detail}" if detail else "re-detected",
                    evidence_fingerprint=remote_evidence_fingerprint,
                )
            )
        else:
            incident = OperationalIncident(
                incident_id=incident_id,
                reason_code=reason_code,
                severity=severity,
                detected_at=now,
                detail=detail,
                run_id=run_id,
                local_evidence_fingerprint=local_evidence_fingerprint,
                remote_evidence_fingerprint=remote_evidence_fingerprint,
                history=(
                    IncidentEvent(
                        status=IncidentStatus.OPEN,
                        at=now,
                        actor="AUTOMATIC",
                        note=detail,
                        evidence_fingerprint=remote_evidence_fingerprint,
                    ),
                ),
            )
        self._write(incident)
        return incident

    def acknowledge(self, incident_id: str, *, clock: Clock, note: str = "") -> OperationalIncident:
        """Record that an operator has seen an incident.

        Args:
            incident_id: Which one.
            clock: The operational clock.
            note: What they said.

        Returns:
            The incident.

        Raises:
            OperationsError: `OPERATIONS_STORE_INVALID` if there is no such
                incident.
        """
        return self._transition(
            incident_id, status=IncidentStatus.ACKNOWLEDGED, clock=clock, note=note
        )

    def resolve(self, incident_id: str, *, clock: Clock, note: str = "") -> OperationalIncident:
        """Record that an operator has settled an incident.

        Args:
            incident_id: Which one.
            clock: The operational clock.
            note: What they did. Worth recording — this is the only way a
                blocking incident stops blocking.

        Returns:
            The incident.

        Raises:
            OperationsError: `OPERATIONS_STORE_INVALID` if there is no such
                incident.
        """
        return self._transition(incident_id, status=IncidentStatus.RESOLVED, clock=clock, note=note)

    def _transition(
        self, incident_id: str, *, status: IncidentStatus, clock: Clock, note: str
    ) -> OperationalIncident:
        """Append an operator lifecycle event.

        Args:
            incident_id: Which incident.
            status: What it becomes.
            clock: The operational clock.
            note: What the operator said.

        Returns:
            The incident.

        Raises:
            OperationsError: `OPERATIONS_STORE_INVALID` if it does not exist.
        """
        existing = self.get(incident_id)
        if existing is None:
            raise OperationsError(
                Code.OPERATIONS_STORE_INVALID, f"there is no incident {incident_id}"
            )
        incident = existing.with_event(
            IncidentEvent(status=status, at=clock.iso(), actor="OPERATOR", note=note)
        )
        self._write(incident)
        return incident

    def _write(self, incident: OperationalIncident) -> None:
        """Persist an incident.

        Args:
            incident: The incident.
        """
        atomic_write_verified(self.directory / f"{incident.incident_id}.json", incident.as_json())

    def get(self, incident_id: str) -> OperationalIncident | None:
        """Read one incident.

        Args:
            incident_id: Which one.

        Returns:
            The incident, or `None`.
        """
        path = self.directory / f"{incident_id}.json"
        if not path.exists():
            return None
        return OperationalIncident.from_body(read_json(path))

    def all(self) -> tuple[OperationalIncident, ...]:
        """Every incident ever recorded, ordered by id.

        Returns:
            The incidents.
        """
        if not self.directory.exists():
            return ()
        return tuple(
            OperationalIncident.from_body(read_json(path))
            for path in sorted(self.directory.glob("*.json"))
        )

    def open_incidents(self) -> tuple[OperationalIncident, ...]:
        """Every incident that still stands.

        Returns:
            The incidents.
        """
        return tuple(incident for incident in self.all() if incident.is_open)

    def blocking(self) -> tuple[OperationalIncident, ...]:
        """Every open incident that stops a new submission.

        Returns:
            The incidents.
        """
        return tuple(incident for incident in self.all() if incident.blocks_submission)

    def fingerprint(self) -> str:
        """Digest the open incident set, so a manifest can bind it.

        Returns:
            The digest.
        """
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(
            {
                "policy": INCIDENT_POLICY,
                "open": [incident.fingerprint() for incident in self.open_incidents()],
            }
        )

    @staticmethod
    def severity_for(reason_code: str) -> IncidentSeverity:
        """Classify a reason code's severity.

        The frozen list in `CRITICAL_AUTO_KILL_REASONS` decides what is
        critical. Everything else defaults to `BLOCKING` rather than to
        something milder: an unclassified condition is one nobody has reasoned
        about, and the safe reading of "I do not know what this is" is "do not
        trade".

        Args:
            reason_code: What was found.

        Returns:
            The severity.
        """
        if reason_code in CRITICAL_AUTO_KILL_REASONS:
            return IncidentSeverity.CRITICAL
        return IncidentSeverity.BLOCKING
