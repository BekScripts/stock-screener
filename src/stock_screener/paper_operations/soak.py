"""The frozen soak protocol, and the prospective evidence it collects.

P2.3 built a control plane and tested it against a stub. Everything known about
it under repetition is inference. A soak is how inference becomes observation,
and the only way that works is if the rules are fixed before the data arrives.

So this module is mostly about making it hard to cheat later. The protocol is
immutable and fingerprinted, the promotion gates are frozen inside it, and the
activation record binds the exact commit that will be judged. Three specific
temptations are closed off in code rather than by intention:

**No backfill.** A session before activation cannot count, and a session whose
window closed unobserved stays `MISSED` forever. `qualifies` refuses an
observation dated earlier than the activation instant, and there is no method
anywhere that writes an observation for a past session.

**No moving the goalposts.** The gates live inside the protocol body, so the
protocol fingerprint changes if any threshold does. A soak whose protocol
fingerprint differs from its activation record's is not the soak that was armed,
and the report says so rather than quietly scoring against new rules.

**No success rate.** Nineteen clean sessions and one duplicate order is a failed
gate, not ninety-five percent. Every zero-tolerance gate is a boolean over a
count, and the report has no percentage field to hide behind.

The cumulative state is derived from the immutable per-session observations plus
the protocol, never from a mutable summary file. A summary that could drift from
its evidence is a summary nobody should trust.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .clock import parse_instant
from .errors import Code, OperationsError
from .models import OperationsMode, RunStatus
from .persistence import atomic_write_verified, guard_operations_root, read_json
from .versions import (
    INCIDENT_POLICY,
    KILL_SWITCH_CONTRACT,
    PAPER_OPERATIONS_ENGINE_VERSION,
    REQUIRED_ACCOUNTING_VERSION,
    REQUIRED_EXECUTION_VERSION,
)

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

    from .schedule import PaperOperationsSchedule

__all__ = [
    "ACTIVATION_FILENAME",
    "DRILLS_DIRECTORY",
    "OBSERVATIONS_DIRECTORY",
    "PAPER_SHADOW_SOAK_ACTIVATION_VERSION",
    "PAPER_SHADOW_SOAK_PROTOCOL_VERSION",
    "PAPER_SHADOW_SOAK_STATE_VERSION",
    "REQUIRED_DRILLS",
    "REQUIRED_ELIGIBLE_SESSIONS",
    "SOAK_DIRECTORY",
    "STATE_FILENAME",
    "DrillRecord",
    "DrillType",
    "PaperShadowSessionObservation",
    "PaperShadowSoakActivation",
    "PaperShadowSoakProtocol",
    "SoakState",
    "SoakStore",
]

#: The protocol contract.
PAPER_SHADOW_SOAK_PROTOCOL_VERSION = "PAPER_SHADOW_SOAK_PROTOCOL_V1"

#: The soak-state contract.
PAPER_SHADOW_SOAK_STATE_VERSION = "PAPER_SHADOW_SOAK_STATE_V1"

#: The activation-artefact contract.
PAPER_SHADOW_SOAK_ACTIVATION_VERSION = "PAPER_SHADOW_SOAK_ACTIVATION_V1"

#: How many eligible market sessions must be observed prospectively. Roughly one
#: trading month. Fixed here rather than passed in, so no later run can shorten
#: it because the early sessions looked clean.
REQUIRED_ELIGIBLE_SESSIONS = 20

#: Where soak evidence lives inside the operational root. Its own namespace,
#: away from the accounting ledger, the execution journal, SEC evidence and the
#: research corpus.
SOAK_DIRECTORY = "soak"
OBSERVATIONS_DIRECTORY = "observations"
DRILLS_DIRECTORY = "drills"
ACTIVATION_FILENAME = "activation.json"
STATE_FILENAME = "state.json"


class DrillType(StrEnum):
    """A deliberate exercise of a failure path.

    Drills exist because the behaviours that matter most are the ones a normal
    session never triggers. A soak of twenty quiet days proves the system works
    when nothing goes wrong, which is not the interesting case.
    """

    KILL_RESTART = "KILL_RESTART"
    DUPLICATE_SCHEDULER = "DUPLICATE_SCHEDULER"
    CRASH_RECOVERY = "CRASH_RECOVERY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    STALE_INPUT = "STALE_INPUT"


#: Every drill that must pass before the soak can complete. Each is a failure
#: path a quiet session never exercises, and the dangerous ones — provider
#: unavailability, remote ambiguity — are deliberately driven by injected faults
#: rather than by disrupting a real order, because a real interrupted `POST` is
#: the one experiment that could itself cause the harm it is testing for.
REQUIRED_DRILLS: tuple[DrillType, ...] = tuple(DrillType)


class SoakState(StrEnum):
    """Where the soak is."""

    NOT_STARTED = "NOT_STARTED"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    HALTED = "HALTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_collecting(self) -> bool:
        """Whether prospective observations currently count."""
        return self in {SoakState.ARMED, SoakState.RUNNING}


@dataclass(frozen=True)
class PaperShadowSoakProtocol:
    """The rules, frozen before any observation exists.

    Attributes:
        protocol_id: A stable name.
        code_commit: The exact commit being judged. A soak scores one build.
        provider: Which provider.
        account_binding_fingerprint: Which paper account.
        schedule_fingerprint: Which schedule produces the slots.
        calendar_fingerprint: Which sessions are eligible.
        health_policy_fingerprint: The health gate in force.
        freshness_policy_fingerprint: The freshness limits in force.
        required_sessions: How many eligible sessions must be observed.
        operations_engine_version: The control plane under test.
        execution_engine_version: The execution engine under test.
        accounting_engine_version: The accounting rules under test.
    """

    protocol_id: str
    code_commit: str
    provider: str
    account_binding_fingerprint: str
    schedule_fingerprint: str
    calendar_fingerprint: str
    health_policy_fingerprint: str
    freshness_policy_fingerprint: str
    required_sessions: int = REQUIRED_ELIGIBLE_SESSIONS
    operations_engine_version: str = PAPER_OPERATIONS_ENGINE_VERSION
    execution_engine_version: str = REQUIRED_EXECUTION_VERSION
    accounting_engine_version: str = REQUIRED_ACCOUNTING_VERSION
    incident_policy: str = INCIDENT_POLICY
    kill_policy: str = KILL_SWITCH_CONTRACT
    version: str = PAPER_SHADOW_SOAK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        """Refuse a protocol that could not be judged.

        Raises:
            OperationsError: `INVALID_SOAK_PROTOCOL` if a binding is missing or
                the session requirement is not positive. A protocol that does
                not name the commit it judges would let any later build claim
                the evidence.
        """
        required = {
            "protocol_id": self.protocol_id,
            "code_commit": self.code_commit,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "schedule_fingerprint": self.schedule_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
        }
        missing = sorted(name for name, value in required.items() if not str(value).strip())
        if missing:
            raise OperationsError(
                Code.INVALID_SOAK_PROTOCOL,
                f"a soak protocol is missing {missing}. It must name the exact build, "
                "account and calendar it judges, or a later build could claim this "
                "evidence as its own",
            )
        if self.required_sessions < 1:
            raise OperationsError(
                Code.INVALID_SOAK_PROTOCOL,
                f"the required session count must be positive, got {self.required_sessions}",
            )

    @property
    def promotion_gates(self) -> dict[str, Any]:
        """The frozen gates, inside the protocol body.

        Inside the body on purpose: the protocol fingerprint changes if any
        threshold does, so a soak scored against altered gates is detectably not
        the soak that was armed. Each is a zero-tolerance count, and there is no
        percentage anywhere — nineteen clean sessions and one duplicate order is
        a failed gate, not ninety-five percent.
        """
        return {
            "zero_tolerance": {
                "live_endpoint_requests": 0,
                "live_order_requests": 0,
                "duplicate_scheduled_runs_causing_execution": 0,
                "duplicate_remote_orders": 0,
                "duplicate_admitted_fills": 0,
                "duplicate_accounting_events": 0,
                "stale_data_executions": 0,
                "missing_data_executions": 0,
                "missed_run_backfills": 0,
                "ignored_reconciliation_breaks": 0,
                "ignored_remote_state_unknown": 0,
                "kill_bypasses": 0,
                "unknown_remote_order_cancellations": 0,
                "automatic_reconciliation_repairs": 0,
                "operations_created_targets": 0,
                "operations_created_orders": 0,
                "broker_overwrote_accounting": 0,
            },
            "required": {
                "eligible_sessions_observed": self.required_sessions,
                "drills_passed": [str(drill) for drill in REQUIRED_DRILLS],
                "unresolved_critical_incidents": 0,
                "unresolved_blocking_incidents": 0,
            },
            "not_gated_on": [
                "profit",
                "Sharpe",
                "win rate",
                "return",
                "alpha",
                "drawdown",
                "strategy success",
                "trade frequency",
            ],
            "counting": (
                "every gate is a zero-tolerance count. There is no success rate: "
                "nineteen clean sessions and one duplicate order is a failed gate"
            ),
        }

    def body(self) -> dict[str, Any]:
        """Return the protocol's semantic content."""
        return {
            "version": self.version,
            "protocol_id": self.protocol_id,
            "code_commit": self.code_commit,
            "provider": self.provider,
            "environment": "PAPER",
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "schedule_fingerprint": self.schedule_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
            "health_policy_fingerprint": self.health_policy_fingerprint,
            "freshness_policy_fingerprint": self.freshness_policy_fingerprint,
            "incident_policy": self.incident_policy,
            "kill_policy": self.kill_policy,
            "operations_engine_version": self.operations_engine_version,
            "execution_engine_version": self.execution_engine_version,
            "accounting_engine_version": self.accounting_engine_version,
            "required_sessions": self.required_sessions,
            "promotion_gates": self.promotion_gates,
            "prospective_only": True,
            "backfill": "NONE — a session before activation cannot count",
            "code_freeze": (
                "a runtime-semantic change resets the qualifying count and needs a "
                "new protocol version"
            ),
            "live_trading": "NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the protocol, gates included."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the protocol with its own fingerprint."""
        return {**self.body(), "protocol_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PaperShadowSoakProtocol:
        """Rebuild a protocol read back from disk.

        Args:
            body: What was written.

        Returns:
            The protocol.

        Raises:
            OperationsError: `INVALID_SOAK_PROTOCOL` if it cannot be read, or if
                the stored fingerprint does not match the content. A protocol
                whose gates were edited after arming is refused rather than
                honoured — that is the whole point of fingerprinting it.
        """
        try:
            protocol = cls(
                protocol_id=str(body["protocol_id"]),
                code_commit=str(body["code_commit"]),
                provider=str(body.get("provider", "")),
                account_binding_fingerprint=str(body["account_binding_fingerprint"]),
                schedule_fingerprint=str(body["schedule_fingerprint"]),
                calendar_fingerprint=str(body["calendar_fingerprint"]),
                health_policy_fingerprint=str(body.get("health_policy_fingerprint", "")),
                freshness_policy_fingerprint=str(body.get("freshness_policy_fingerprint", "")),
                required_sessions=int(body.get("required_sessions", REQUIRED_ELIGIBLE_SESSIONS)),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.INVALID_SOAK_PROTOCOL, f"a soak protocol could not be read: {exc}"
            ) from exc
        stored = str(body.get("protocol_fingerprint", ""))
        if stored and stored != protocol.fingerprint():
            raise OperationsError(
                Code.SOAK_PROTOCOL_DRIFT,
                "the stored soak protocol does not match its own fingerprint. Its "
                "gates or bindings were changed after arming, so the evidence "
                "collected under it no longer scores against the rules that were "
                "frozen. Version the protocol and restart prospectively",
            )
        return protocol


@dataclass(frozen=True)
class PaperShadowSoakActivation:
    """The moment the rules became binding.

    Attributes:
        protocol_fingerprint: Which rules.
        code_commit: Which build.
        account_binding_fingerprint: Which account.
        activated_at: When, ISO-8601. The prospective boundary — no session
            before this counts.
        first_eligible_session: The first session that may produce evidence.
        required_sessions: How many are needed.
        calendar_fingerprint: Which sessions exist.
        schedule_fingerprint: Which slots exist.
        starting_operations_journal_head: The control plane's chain head.
        starting_execution_journal_head: The execution journal's head.
        starting_accounting_state: The book's state fingerprint.
        starting_reconciliation: The three-way verdict at activation.
        starting_health: The gate's verdict at activation.
    """

    protocol_fingerprint: str
    code_commit: str
    account_binding_fingerprint: str
    activated_at: str
    first_eligible_session: str
    required_sessions: int = REQUIRED_ELIGIBLE_SESSIONS
    calendar_fingerprint: str = ""
    schedule_fingerprint: str = ""
    starting_operations_journal_head: str = ""
    starting_execution_journal_head: str = ""
    starting_accounting_state: str = ""
    starting_reconciliation: str = ""
    starting_health: str = ""
    version: str = PAPER_SHADOW_SOAK_ACTIVATION_VERSION

    def __post_init__(self) -> None:
        """Validate the activation instant.

        Raises:
            ValueError: If `activated_at` carries no offset. The prospective
                boundary cannot be ambiguous about its timezone.
        """
        parse_instant(self.activated_at)

    def body(self) -> dict[str, Any]:
        """Return the activation's semantic content."""
        return {
            "version": self.version,
            "protocol_fingerprint": self.protocol_fingerprint,
            "code_commit": self.code_commit,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "activated_at": self.activated_at,
            "first_eligible_session": self.first_eligible_session,
            "required_sessions": self.required_sessions,
            "calendar_fingerprint": self.calendar_fingerprint,
            "schedule_fingerprint": self.schedule_fingerprint,
            "starting_operations_journal_head": self.starting_operations_journal_head,
            "starting_execution_journal_head": self.starting_execution_journal_head,
            "starting_accounting_state": self.starting_accounting_state,
            "starting_reconciliation": self.starting_reconciliation,
            "starting_health": self.starting_health,
            "prospective_boundary": (
                "no session dated before activated_at may count, enforced by SoakStore.qualifies"
            ),
            "live_trading": "NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the activation."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the activation with its own fingerprint."""
        return {**self.body(), "activation_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PaperShadowSoakActivation:
        """Rebuild an activation read back from disk.

        Args:
            body: What was written.

        Returns:
            The activation.

        Raises:
            OperationsError: `SOAK_NOT_ACTIVATED` if it cannot be read.
        """
        try:
            return cls(
                protocol_fingerprint=str(body["protocol_fingerprint"]),
                code_commit=str(body["code_commit"]),
                account_binding_fingerprint=str(body["account_binding_fingerprint"]),
                activated_at=str(body["activated_at"]),
                first_eligible_session=str(body["first_eligible_session"]),
                required_sessions=int(body.get("required_sessions", REQUIRED_ELIGIBLE_SESSIONS)),
                calendar_fingerprint=str(body.get("calendar_fingerprint", "")),
                schedule_fingerprint=str(body.get("schedule_fingerprint", "")),
                starting_operations_journal_head=str(
                    body.get("starting_operations_journal_head", "")
                ),
                starting_execution_journal_head=str(
                    body.get("starting_execution_journal_head", "")
                ),
                starting_accounting_state=str(body.get("starting_accounting_state", "")),
                starting_reconciliation=str(body.get("starting_reconciliation", "")),
                starting_health=str(body.get("starting_health", "")),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.SOAK_NOT_ACTIVATED, f"a soak activation could not be read: {exc}"
            ) from exc


@dataclass(frozen=True)
class PaperShadowSessionObservation:
    """One session's evidence, immutable once written.

    Attributes:
        protocol_fingerprint: Which rules it was collected under.
        session: The market session, ISO date.
        scheduled_for: The slot it belongs to.
        observed_at: When the observation was made.
        operations_run_id: The derived run id.
        code_commit: Which build produced it.
        account_binding_fingerprint: Which account.
        mode: Which mode the run used.
        run_status: How the run ended.
        health_before: The gate's verdict.
        reconciliation_before: The three-way verdict before acting.
        reconciliation_after: The verdict afterwards.
        inputs_fresh: Whether every required input was within policy.
        orders_authorized: How many the plan authorised.
        orders_submitted: How many reached the venue.
        provider_order_ids: The venue's ids.
        fills_observed: How many executions the venue reported.
        accounting_events: The accounting event ids the fills produced.
        incidents_opened: Incidents this session opened.
        incidents_resolved: Incidents it resolved.
        kill_state: The control state at the end.
        operations_journal_head: The control plane's chain head.
        execution_journal_head: The execution journal's head.
        accounting_state: The book's state fingerprint.
        unexpected_open_day_orders: DAY orders still working at session end.
        unexpected_positions: Holdings the book does not account for.
        evidence: Provider response fingerprints, per operation.
        superseded_by: A correction record, when one exists. The original is
            never rewritten — a correction is a new record that points back.
    """

    protocol_fingerprint: str
    session: str
    scheduled_for: str
    observed_at: str
    operations_run_id: str
    code_commit: str
    account_binding_fingerprint: str
    mode: OperationsMode
    run_status: RunStatus
    health_before: str = ""
    reconciliation_before: str = ""
    reconciliation_after: str = ""
    inputs_fresh: bool = True
    orders_authorized: int = 0
    orders_submitted: int = 0
    provider_order_ids: tuple[str, ...] = ()
    fills_observed: int = 0
    accounting_events: tuple[str, ...] = ()
    incidents_opened: tuple[str, ...] = ()
    incidents_resolved: tuple[str, ...] = ()
    kill_state: str = ""
    operations_journal_head: str = ""
    execution_journal_head: str = ""
    accounting_state: str = ""
    unexpected_open_day_orders: tuple[str, ...] = ()
    unexpected_positions: tuple[str, ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    superseded_by: str | None = None

    @property
    def observation_id(self) -> str:
        """A deterministic name for this observation.

        Derived from the protocol, the session, the run *and the content*. The
        content matters: recording identical evidence twice must be idempotent
        (same id, same file), while a correction is genuinely a different record
        and needs its own name so the original survives beside it. An id that
        ignored content would make a correction overwrite the thing it was
        meant to supersede.
        """
        digest = hashlib.sha256(
            f"{self.protocol_fingerprint}|{self.session}|{self.operations_run_id}|"
            f"{self.fingerprint()}".encode()
        ).hexdigest()
        return f"obs-{self.session}-{digest[:16]}"

    @property
    def is_mutation_capable(self) -> bool:
        """Whether this session could have submitted."""
        return self.mode.may_mutate

    @property
    def reconciled_cleanly(self) -> bool:
        """Whether the session ended with a clean three-way sweep.

        A session that submitted nothing and reconciled cleanly before acting
        counts: there was nothing for the after-sweep to find. A session that
        submitted must reconcile afterwards.
        """
        if self.orders_submitted:
            return self.reconciliation_after == "RECONCILED"
        return self.reconciliation_before in {"RECONCILED", ""}

    def body(self) -> dict[str, Any]:
        """Return the observation's semantic content."""
        return {
            "protocol_fingerprint": self.protocol_fingerprint,
            "session": self.session,
            "scheduled_for": self.scheduled_for,
            "operations_run_id": self.operations_run_id,
            "code_commit": self.code_commit,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "mode": str(self.mode),
            "run_status": str(self.run_status),
            "health_before": self.health_before,
            "reconciliation_before": self.reconciliation_before,
            "reconciliation_after": self.reconciliation_after,
            "inputs_fresh": self.inputs_fresh,
            "orders_authorized": self.orders_authorized,
            "orders_submitted": self.orders_submitted,
            "provider_order_ids": list(self.provider_order_ids),
            "fills_observed": self.fills_observed,
            "accounting_events": list(self.accounting_events),
            "incidents_opened": list(self.incidents_opened),
            "incidents_resolved": list(self.incidents_resolved),
            "kill_state": self.kill_state,
            "operations_journal_head": self.operations_journal_head,
            "execution_journal_head": self.execution_journal_head,
            "accounting_state": self.accounting_state,
            "unexpected_open_day_orders": list(self.unexpected_open_day_orders),
            "unexpected_positions": list(self.unexpected_positions),
            "evidence": [list(pair) for pair in self.evidence],
        }

    def fingerprint(self) -> str:
        """Digest the observation. Excludes `observed_at` so it is a value."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the observation with its id, timestamp and fingerprint."""
        return {
            **self.body(),
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
            "superseded_by": self.superseded_by,
            "observation_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PaperShadowSessionObservation:
        """Rebuild an observation read back from disk.

        Args:
            body: What was written.

        Returns:
            The observation.

        Raises:
            OperationsError: `SOAK_OBSERVATION_INVALID` if it cannot be read, or
                if its stored fingerprint does not match. Hand-edited evidence is
                refused rather than scored.
        """
        try:
            observation = cls(
                protocol_fingerprint=str(body["protocol_fingerprint"]),
                session=str(body["session"]),
                scheduled_for=str(body.get("scheduled_for", "")),
                observed_at=str(body.get("observed_at", "")),
                operations_run_id=str(body["operations_run_id"]),
                code_commit=str(body.get("code_commit", "")),
                account_binding_fingerprint=str(body.get("account_binding_fingerprint", "")),
                mode=OperationsMode(str(body["mode"])),
                run_status=RunStatus(str(body["run_status"])),
                health_before=str(body.get("health_before", "")),
                reconciliation_before=str(body.get("reconciliation_before", "")),
                reconciliation_after=str(body.get("reconciliation_after", "")),
                inputs_fresh=bool(body.get("inputs_fresh", True)),
                orders_authorized=int(body.get("orders_authorized", 0)),
                orders_submitted=int(body.get("orders_submitted", 0)),
                provider_order_ids=tuple(body.get("provider_order_ids", ()) or ()),
                fills_observed=int(body.get("fills_observed", 0)),
                accounting_events=tuple(body.get("accounting_events", ()) or ()),
                incidents_opened=tuple(body.get("incidents_opened", ()) or ()),
                incidents_resolved=tuple(body.get("incidents_resolved", ()) or ()),
                kill_state=str(body.get("kill_state", "")),
                operations_journal_head=str(body.get("operations_journal_head", "")),
                execution_journal_head=str(body.get("execution_journal_head", "")),
                accounting_state=str(body.get("accounting_state", "")),
                unexpected_open_day_orders=tuple(body.get("unexpected_open_day_orders", ()) or ()),
                unexpected_positions=tuple(body.get("unexpected_positions", ()) or ()),
                evidence=tuple((str(pair[0]), str(pair[1])) for pair in body.get("evidence", ())),
                superseded_by=(str(body["superseded_by"]) if body.get("superseded_by") else None),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"a soak observation could not be read: {exc}",
            ) from exc
        stored = str(body.get("observation_fingerprint", ""))
        if stored and stored != observation.fingerprint():
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"observation {observation.observation_id} does not match its own "
                "fingerprint. Evidence must come from the run pipeline, not from a "
                "hand-edited file, and a session is not made clean by rewriting it",
            )
        return observation


@dataclass(frozen=True)
class DrillRecord:
    """One deliberate exercise of a failure path.

    Attributes:
        drill_type: Which failure path.
        performed_at: When.
        run_id: The run or harness it used.
        pre_state: What was true before.
        action: What was done.
        expected: What should have happened.
        observed: What did happen.
        external_mutations: How many mutating requests it caused.
        post_state: What was true afterwards.
        reconciliation: The three-way verdict afterwards, where one applies.
        passed: Whether observed matched expected.
        injected: Whether the fault was injected rather than real. Recorded
            because an injected provider outage and a real one are different
            evidence, and only the first is safe to arrange deliberately.
        detail: A sanitised note.
    """

    drill_type: DrillType
    performed_at: str
    expected: str
    observed: str
    passed: bool
    run_id: str = ""
    pre_state: str = ""
    action: str = ""
    external_mutations: int = 0
    post_state: str = ""
    reconciliation: str = ""
    injected: bool = True
    detail: str = ""

    def body(self) -> dict[str, Any]:
        """Return the drill's semantic content."""
        return {
            "drill_type": str(self.drill_type),
            "run_id": self.run_id,
            "pre_state": self.pre_state,
            "action": self.action,
            "expected": self.expected,
            "observed": self.observed,
            "external_mutations": self.external_mutations,
            "post_state": self.post_state,
            "reconciliation": self.reconciliation,
            "passed": self.passed,
            "injected_fault": self.injected,
            "detail": self.detail,
        }

    def fingerprint(self) -> str:
        """Digest the drill."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the drill with its timestamp and fingerprint."""
        return {
            **self.body(),
            "performed_at": self.performed_at,
            "drill_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> DrillRecord:
        """Rebuild a drill read back from disk.

        Args:
            body: What was written.

        Returns:
            The drill.

        Raises:
            OperationsError: `SOAK_OBSERVATION_INVALID` if it cannot be read.
        """
        try:
            return cls(
                drill_type=DrillType(str(body["drill_type"])),
                performed_at=str(body.get("performed_at", "")),
                expected=str(body["expected"]),
                observed=str(body["observed"]),
                passed=bool(body["passed"]),
                run_id=str(body.get("run_id", "")),
                pre_state=str(body.get("pre_state", "")),
                action=str(body.get("action", "")),
                external_mutations=int(body.get("external_mutations", 0)),
                post_state=str(body.get("post_state", "")),
                reconciliation=str(body.get("reconciliation", "")),
                injected=bool(body.get("injected_fault", True)),
                detail=str(body.get("detail", "")),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID, f"a drill record could not be read: {exc}"
            ) from exc


@dataclass
class SoakStore:
    """The durable, append-only home of soak evidence.

    Attributes:
        root: The operational root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root and locate the soak namespace.

        Raises:
            OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if the root lies
                in another trust domain.
        """
        self.root = Path(self.root)
        guard_operations_root(self.root)
        self.directory = self.root / SOAK_DIRECTORY
        self.observations_directory = self.directory / OBSERVATIONS_DIRECTORY
        self.drills_directory = self.directory / DRILLS_DIRECTORY

    # -- protocol and activation --------------------------------------------

    @property
    def protocol_path(self) -> Path:
        """Where the frozen protocol lives."""
        return self.directory / "protocol.json"

    @property
    def activation_path(self) -> Path:
        """Where the activation artefact lives."""
        return self.directory / ACTIVATION_FILENAME

    @property
    def state_path(self) -> Path:
        """Where the soak state lives."""
        return self.directory / STATE_FILENAME

    def freeze_protocol(self, protocol: PaperShadowSoakProtocol) -> Path:
        """Write the protocol, refusing to change one already frozen.

        Args:
            protocol: The rules.

        Returns:
            Where it was written.

        Raises:
            OperationsError: `SOAK_PROTOCOL_DRIFT` if a different protocol is
                already frozen. Changing the rules mid-soak is the failure this
                whole module is shaped to prevent; a new protocol is a new soak
                with its count restarted, not an edit to this one.
        """
        if self.protocol_path.exists():
            existing = PaperShadowSoakProtocol.from_body(read_json(self.protocol_path))
            if existing.fingerprint() != protocol.fingerprint():
                raise OperationsError(
                    Code.SOAK_PROTOCOL_DRIFT,
                    f"protocol {existing.protocol_id} is already frozen with a "
                    "different fingerprint. A soak's rules cannot be edited once "
                    "armed — version the protocol and restart the qualifying count "
                    "from zero",
                )
            return self.protocol_path
        atomic_write_verified(self.protocol_path, protocol.as_json())
        return self.protocol_path

    def protocol(self) -> PaperShadowSoakProtocol | None:
        """Read the frozen protocol.

        Returns:
            The protocol, or `None` when none is frozen.
        """
        if not self.protocol_path.exists():
            return None
        return PaperShadowSoakProtocol.from_body(read_json(self.protocol_path))

    def activate(self, activation: PaperShadowSoakActivation) -> Path:
        """Record activation, refusing to re-activate.

        Args:
            activation: The activation artefact.

        Returns:
            Where it was written.

        Raises:
            OperationsError: `SOAK_ALREADY_ACTIVATED` if the soak is already
                activated. Re-activating would move the prospective boundary
                forward and silently discard the sessions already observed.
        """
        if self.activation_path.exists():
            raise OperationsError(
                Code.SOAK_ALREADY_ACTIVATED,
                "this soak is already activated. Re-activating would move the "
                "prospective boundary and discard the observations already "
                "collected; a fresh soak needs a new protocol",
            )
        atomic_write_verified(self.activation_path, activation.as_json())
        self.set_state(SoakState.ARMED, detail="protocol frozen and activation recorded")
        return self.activation_path

    def activation(self) -> PaperShadowSoakActivation | None:
        """Read the activation artefact.

        Returns:
            The activation, or `None` when the soak is not activated.
        """
        if not self.activation_path.exists():
            return None
        return PaperShadowSoakActivation.from_body(read_json(self.activation_path))

    # -- state ---------------------------------------------------------------

    def set_state(self, state: SoakState, *, detail: str = "") -> SoakState:
        """Record the soak state durably.

        Args:
            state: The state.
            detail: Why.

        Returns:
            The state.
        """
        atomic_write_verified(
            self.state_path,
            {
                "version": PAPER_SHADOW_SOAK_STATE_VERSION,
                "state": str(state),
                "detail": detail,
            },
        )
        return state

    def state(self) -> SoakState:
        """Read the soak state, failing safe.

        Returns:
            The state. A missing or unreadable record is `NOT_STARTED`, never
            `COMPLETED` — absent evidence of completion is not completion, and
            that is the one direction this must never fail in.
        """
        if not self.state_path.exists():
            return SoakState.NOT_STARTED
        try:
            raw = str(read_json(self.state_path).get("state", ""))
            return SoakState(raw)
        except (OperationsError, ValueError):
            return SoakState.NOT_STARTED

    # -- observations --------------------------------------------------------

    def qualifies(self, observation: PaperShadowSessionObservation) -> None:
        """Refuse an observation that cannot count toward this soak.

        The prospective boundary, enforced in code rather than by intention.

        Args:
            observation: The observation.

        Raises:
            OperationsError: `SOAK_NOT_ACTIVATED` if no activation exists;
                `SOAK_PROTOCOL_DRIFT` if it was collected under different rules
                or a different build; `SOAK_RETROSPECTIVE_OBSERVATION` if its
                session predates activation. The last is the one that matters
                most: a soak that could absorb earlier sessions is a soak whose
                evidence was chosen after the fact.
        """
        activation = self.activation()
        if activation is None:
            raise OperationsError(
                Code.SOAK_NOT_ACTIVATED,
                "no soak is activated, so there is nothing for this observation to count toward",
            )
        if observation.protocol_fingerprint != activation.protocol_fingerprint:
            raise OperationsError(
                Code.SOAK_PROTOCOL_DRIFT,
                "this observation was collected under a different protocol than the "
                "one activated. It is historical evidence for that protocol, not "
                "evidence for this one",
            )
        if observation.code_commit and observation.code_commit != activation.code_commit:
            raise OperationsError(
                Code.SOAK_PROTOCOL_DRIFT,
                f"this observation came from build {observation.code_commit[:12]} and "
                f"the soak judges {activation.code_commit[:12]}. A semantic code "
                "change restarts the qualifying count; it does not inherit the "
                "sessions already collected",
            )
        if observation.session < activation.first_eligible_session:
            raise OperationsError(
                Code.SOAK_RETROSPECTIVE_OBSERVATION,
                f"session {observation.session} precedes the first eligible session "
                f"{activation.first_eligible_session}. A soak counts only what was "
                "observed after its rules were frozen — otherwise the evidence is "
                "chosen after the fact",
            )

    def record_observation(
        self, observation: PaperShadowSessionObservation
    ) -> PaperShadowSessionObservation:
        """Append one session's evidence.

        Args:
            observation: The evidence.

        Returns:
            The observation.

        Raises:
            OperationsError: As `qualifies` does, or
                `SOAK_OBSERVATION_INVALID` if a *different* observation already
                exists for this session. Yesterday's record is never overwritten
                — a correction is a new record naming the one it supersedes.
        """
        self.qualifies(observation)
        path = self.observations_directory / f"{observation.observation_id}.json"
        existing = [row for row in self.observations() if row.session == observation.session]
        unsuperseded = [row for row in existing if row.superseded_by is None]
        if unsuperseded and unsuperseded[0].fingerprint() != observation.fingerprint():
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"session {observation.session} already has an observation "
                f"({unsuperseded[0].observation_id}) with different content. It is "
                "not overwritten: record a correction that supersedes it, so the "
                "original evidence survives",
            )
        atomic_write_verified(path, observation.as_json())
        if self.state() is SoakState.ARMED:
            self.set_state(SoakState.RUNNING, detail="first qualifying observation recorded")
        return observation

    def supersede(
        self,
        original: PaperShadowSessionObservation,
        correction: PaperShadowSessionObservation,
        *,
        reason: str,
    ) -> PaperShadowSessionObservation:
        """Record a correction without destroying the original.

        Args:
            original: What was recorded.
            correction: What should have been recorded.
            reason: Why.

        Returns:
            The correction.

        Raises:
            OperationsError: As `qualifies` does.
        """
        self.qualifies(correction)
        atomic_write_verified(
            self.observations_directory / f"{original.observation_id}.json",
            {
                **original.as_json(),
                "superseded_by": correction.observation_id,
                "supersession_reason": reason,
            },
        )
        atomic_write_verified(
            self.observations_directory / f"{correction.observation_id}.json",
            {**correction.as_json(), "supersedes": original.observation_id},
        )
        return correction

    def observations(self) -> tuple[PaperShadowSessionObservation, ...]:
        """Every observation ever recorded, ordered by session.

        Returns:
            The observations, superseded ones included — the history is the
            evidence.
        """
        if not self.observations_directory.exists():
            return ()
        rows = [
            PaperShadowSessionObservation.from_body(read_json(path))
            for path in sorted(self.observations_directory.glob("*.json"))
        ]
        return tuple(sorted(rows, key=lambda row: (row.session, row.observation_id)))

    def qualifying_observations(self) -> tuple[PaperShadowSessionObservation, ...]:
        """The observations that count: current, in-protocol, post-activation.

        Returns:
            The observations, one per session.
        """
        activation = self.activation()
        if activation is None:
            return ()
        return tuple(
            row
            for row in self.observations()
            if row.superseded_by is None
            and row.protocol_fingerprint == activation.protocol_fingerprint
            and row.session >= activation.first_eligible_session
        )

    # -- drills --------------------------------------------------------------

    def record_drill(self, drill: DrillRecord) -> DrillRecord:
        """Append one drill's evidence.

        Args:
            drill: The drill.

        Returns:
            The drill.
        """
        digest = drill.fingerprint()[:16]
        atomic_write_verified(
            self.drills_directory / f"{drill.drill_type}-{digest}.json", drill.as_json()
        )
        return drill

    def drills(self) -> tuple[DrillRecord, ...]:
        """Every drill ever recorded.

        Returns:
            The drills, ordered by type then fingerprint.
        """
        if not self.drills_directory.exists():
            return ()
        return tuple(
            DrillRecord.from_body(read_json(path))
            for path in sorted(self.drills_directory.glob("*.json"))
        )

    def passed_drills(self) -> frozenset[DrillType]:
        """Which required drills have passed.

        Returns:
            The drill types with at least one passing record.
        """
        return frozenset(drill.drill_type for drill in self.drills() if drill.passed)

    # -- session accounting --------------------------------------------------

    def eligible_sessions(
        self, *, calendar: ExecutionCalendar, schedule: PaperOperationsSchedule, now: str
    ) -> tuple[str, ...]:
        """The eligible sessions from activation up to an instant.

        Computed from the explicit calendar rather than counted by hand, and
        bounded below by the activation boundary — a session before the rules
        were frozen is not eligible however much it looks like one.

        Args:
            calendar: The frozen session calendar.
            schedule: The schedule that produces slots.
            now: The operational instant.

        Returns:
            The eligible sessions, in order.
        """
        activation = self.activation()
        if activation is None:
            return ()
        instant = parse_instant(now)
        eligible: list[str] = []
        for session in calendar.sessions:
            if session < activation.first_eligible_session:
                continue
            if schedule.window_closes_at(session) <= instant:
                eligible.append(session)
        return tuple(eligible)

    def missing_sessions(
        self, *, calendar: ExecutionCalendar, schedule: PaperOperationsSchedule, now: str
    ) -> tuple[str, ...]:
        """Eligible sessions with no observation at all.

        A session whose window closed and which produced no record is the one
        thing a soak must never have: `MISSED` is a result, silence is not.

        Args:
            calendar: The frozen calendar.
            schedule: The schedule.
            now: The operational instant.

        Returns:
            The sessions with no record.
        """
        observed = {row.session for row in self.qualifying_observations()}
        return tuple(
            session
            for session in self.eligible_sessions(calendar=calendar, schedule=schedule, now=now)
            if session not in observed
        )
