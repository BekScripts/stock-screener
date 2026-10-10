"""The operational evidence contract: what is recorded, and by whom.

Four phases ended on the same shape. V2 declared gates with no evaluators; R2
had evaluators reading fields no record carried; R3 found event types with no
emitters. Each was a declaration with nothing behind it, and each was discovered
only when something downstream tried to use it.

So this module does two things. It declares the observability contract as a
fingerprinted artefact a future protocol can bind — so changing *how* a kill
bypass is detected becomes protocol drift rather than a quiet edit. And it
classifies every member of the operational event vocabulary as `EMITTED`,
`RESERVED` or `DEPRECATED`, with a test that proves every `EMITTED` member has a
real production emission path exercised by the real component.

That test is the guard. It would have caught all four previous defects, and it
is cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stock_screener.paper_operations.run_journal import RunEventType

__all__ = [
    "EVENT_STATUS",
    "OBSERVABILITY_VERSION",
    "EventStatus",
    "OperationalEventSpec",
    "event_vocabulary",
    "observability_contract",
    "observability_fingerprint",
]

#: The observability contract version. A future protocol binds its fingerprint.
OBSERVABILITY_VERSION = "PAPER_OPERATIONS_OBSERVABILITY_V1"


class EventStatus(StrEnum):
    """Whether an event type is expected to be produced."""

    EMITTED = "EMITTED"
    """A production path writes it, and a test exercises that path. Anything
    claiming this status without an emitter is a defect the vocabulary test
    catches."""

    RESERVED = "RESERVED"
    """Declared for a capability that does not exist yet. Explicitly not
    emitted, so its absence is a recorded decision rather than an oversight."""

    DEPRECATED = "DEPRECATED"
    """Was emitted once; kept so historical records remain readable."""


@dataclass(frozen=True)
class OperationalEventSpec:
    """What one operational event means and where it comes from.

    Attributes:
        event: The event type.
        status: Whether it is produced.
        meaning: What its presence asserts. Written out because an event name
            is not a specification — `ORDER_SUBMITTED` could plausibly mean
            "we tried" or "the venue acknowledged", and those are different
            facts with different safety consequences.
        emitter: The production component that writes it.
        required_fields: What a reader may rely on finding.
        gates_served: Which soak gates need it.
    """

    event: RunEventType
    status: EventStatus
    meaning: str
    emitter: str = ""
    required_fields: tuple[str, ...] = ()
    gates_served: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        """Return the spec's semantic content."""
        return {
            "event": str(self.event),
            "status": str(self.status),
            "meaning": self.meaning,
            "emitter": self.emitter,
            "required_fields": list(self.required_fields),
            "gates_served": list(self.gates_served),
        }


def event_vocabulary() -> tuple[OperationalEventSpec, ...]:
    """Classify every operational event type.

    Returns:
        One spec per `RunEventType` member. The vocabulary test asserts this
        covers the enum exactly, so a new event type cannot be added without
        being classified.
    """
    return (
        OperationalEventSpec(
            event=RunEventType.RUN_SCHEDULED,
            status=EventStatus.EMITTED,
            meaning="a slot was recognised as due and a run identity derived for it",
            emitter="PaperOperationsCoordinator.run_due",
            required_fields=("run_id", "detail"),
            gates_served=("duplicate_scheduled_runs_causing_execution",),
        ),
        OperationalEventSpec(
            event=RunEventType.RUN_STATE_CHANGED,
            status=EventStatus.EMITTED,
            meaning="the run moved between two states of the frozen transition table",
            emitter="PaperOperationsCoordinator._advance",
            required_fields=("run_id", "from_state", "to_state"),
            gates_served=("missed_run_backfills",),
        ),
        OperationalEventSpec(
            event=RunEventType.HEALTH_EVALUATED,
            status=EventStatus.EMITTED,
            meaning="every health gate was evaluated and its verdict recorded",
            emitter="PaperOperationsCoordinator._execute",
            required_fields=("run_id", "evidence"),
            gates_served=("kill_bypasses",),
        ),
        OperationalEventSpec(
            event=RunEventType.INPUTS_FROZEN,
            status=EventStatus.EMITTED,
            meaning=(
                "the operational inputs a decision or execution depends on were "
                "fixed, with each source's as_of, fingerprint and freshness verdict. "
                "Its presence with a stale verdict is the evidence that a stale input "
                "was seen; its absence before a submission is the evidence that it "
                "was not checked"
            ),
            emitter="PaperOperationsCoordinator._record_inputs_frozen",
            required_fields=("run_id", "evidence"),
            gates_served=("stale_data_executions", "missing_data_executions"),
        ),
        OperationalEventSpec(
            event=RunEventType.RECONCILED,
            status=EventStatus.EMITTED,
            meaning=(
                "a three-way reconciliation was evaluated; carries the verdict, the "
                "break counts and whether execution was permitted"
            ),
            emitter="PaperOperationsCoordinator._record_reconciled",
            required_fields=("run_id", "evidence"),
            gates_served=(
                "ignored_reconciliation_breaks",
                "completed_sessions_reconcile",
                "ignored_remote_state_unknown",
            ),
        ),
        OperationalEventSpec(
            event=RunEventType.PLAN_AUTHORIZED,
            status=EventStatus.EMITTED,
            meaning=(
                "an executable plan passed portfolio and risk authority; binds the "
                "frozen strategy decision it consumed, which is what proves no "
                "re-ranking happened"
            ),
            emitter="RuntimeSubmissionAuthority._record_plan_authorized",
            required_fields=("run_id", "evidence"),
            gates_served=("reranked_executions", "risk_authority_expansions"),
        ),
        OperationalEventSpec(
            event=RunEventType.SUBMISSION_GATE_CHECKED,
            status=EventStatus.EMITTED,
            meaning=(
                "the submission gate was consulted immediately before one specific "
                "mutation, and this is the value it observed for that mutation — not "
                "a session-level result reused"
            ),
            emitter="RuntimeSubmissionAuthority._record_gate_check",
            required_fields=("run_id", "evidence"),
            gates_served=("kill_bypasses",),
        ),
        OperationalEventSpec(
            event=RunEventType.ORDER_SUBMITTED,
            status=EventStatus.EMITTED,
            meaning=(
                "a remote order became known to exist at the venue. Not every network "
                "attempt: an attempt whose fate is unknown is recorded by the request "
                "audit as UNRESOLVED and resolved by the submission protocol asking "
                "the venue"
            ),
            emitter="RuntimeSubmissionAuthority._record_order_submitted",
            required_fields=("run_id", "evidence"),
            gates_served=(
                "duplicate_remote_orders",
                "duplicate_scheduled_runs_causing_execution",
                "kill_bypasses",
                "missed_run_backfills",
                "stale_data_executions",
                "missing_data_executions",
            ),
        ),
        OperationalEventSpec(
            event=RunEventType.FILL_INGESTED,
            status=EventStatus.EMITTED,
            meaning=(
                "a provider execution was presented to accounting admission. Records "
                "the outcome including DUPLICATE_IGNORED, so a duplicate that was "
                "safely ignored is distinguishable from a duplicate never seen"
            ),
            emitter="RuntimeSubmissionAuthority._record_fill_ingested",
            required_fields=("run_id", "evidence"),
            gates_served=("duplicate_admitted_fills", "duplicate_accounting_events"),
        ),
        OperationalEventSpec(
            event=RunEventType.INCIDENT_RECORDED,
            status=EventStatus.RESERVED,
            meaning=(
                "reserved. Incidents are durable in the IncidentStore, which is the "
                "authority the gates read; mirroring them into the run journal would "
                "be a second record of one fact and the two could disagree"
            ),
            gates_served=(),
        ),
        OperationalEventSpec(
            event=RunEventType.KILL_OBSERVED,
            status=EventStatus.EMITTED,
            meaning="the control state forbade activity and the run stopped",
            emitter="PaperOperationsCoordinator._execute",
            required_fields=("run_id",),
            gates_served=("kill_bypasses",),
        ),
        OperationalEventSpec(
            event=RunEventType.RUN_COMPLETED,
            status=EventStatus.EMITTED,
            meaning="the run reached a terminal status; carries its final verdict",
            emitter="PaperOperationsCoordinator._finish",
            required_fields=("run_id", "detail"),
            gates_served=("completed_sessions_reconcile",),
        ),
    )


#: The classification, by event, for callers that want a lookup.
EVENT_STATUS: dict[RunEventType, EventStatus] = {
    row.event: row.status for row in event_vocabulary()
}


def observability_contract() -> dict[str, Any]:
    """Describe everything this runtime durably records.

    Returns:
        The contract. A future soak protocol binds its fingerprint, so a change
        to how a safety fact is detected becomes protocol drift rather than an
        edit nobody notices mid-soak.
    """
    from stock_screener.execution.external.request_audit import (
        EXTERNAL_REQUEST_AUDIT_VERSION,
        MUTATING_METHODS,
    )

    return {
        "version": OBSERVABILITY_VERSION,
        "event_vocabulary": [row.body() for row in event_vocabulary()],
        "emitted": sorted(
            str(row.event) for row in event_vocabulary() if row.status is EventStatus.EMITTED
        ),
        "reserved": sorted(
            str(row.event) for row in event_vocabulary() if row.status is EventStatus.RESERVED
        ),
        "deprecated": sorted(
            str(row.event) for row in event_vocabulary() if row.status is EventStatus.DEPRECATED
        ),
        "request_audit_version": EXTERNAL_REQUEST_AUDIT_VERSION,
        "audited_methods": sorted(MUTATING_METHODS),
        "mutation_audit_behaviour": (
            "every mutation is recorded before dispatch; a mutation whose audit "
            "cannot be persisted is not sent"
        ),
        "read_audit_behaviour": (
            "reads are audited, and a read whose audit fails proceeds — a read "
            "changes nothing, and failing a reconciliation read closed would block "
            "operations over a disk problem"
        ),
        "reconciliation_persistence": "PortfolioStore.write_reconciliation, per run",
        "execution_journal_linkage": (
            "operational execution appends through EXECUTION_JOURNAL_HASH_CHAIN_V1; "
            "no second execution ledger exists"
        ),
        "accounting_linkage": (
            "each admitted fill binds provider execution id, execution fill id and "
            "the PORTFOLIO_ACCOUNTING_V2 event id"
        ),
        "credentials_recorded": False,
        "authority_semantics_changed": (
            "none, except that a mutation whose required pre-dispatch audit cannot be "
            "persisted now fails closed"
        ),
    }


def observability_fingerprint() -> str:
    """Digest the observability contract."""
    from stock_screener.execution import fingerprint_of

    return fingerprint_of(observability_contract())
