"""What a run did, what the system looks like now, and the manifest binding both.

Three artefacts, all immutable and all fingerprinted, and one rule running
through them: a blocked run is not a successful run. The temptation in an
operational report is to show green when nothing broke, and "nothing broke"
includes "a gate correctly refused to trade" — which is the system working and
is emphatically not the same as a completed run. `RunStatus.is_success` draws
that line, and the status report shows the last *clean reconciliation* and the
last *successful* run separately so neither can stand in for the other.

Nothing here carries a credential, an account number or an authorization header.
The status report is the artefact most likely to be pasted into a chat window,
so it holds account *fingerprints* only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .versions import PAPER_OPERATIONS_ENGINE_VERSION, operations_policy

if TYPE_CHECKING:
    from .models import OperationalInput, OperationsMode, RunStatus

__all__ = [
    "PAPER_OPERATIONS_MANIFEST_VERSION",
    "OperationsStatusReport",
    "PaperOperationsRunReport",
    "paper_operations_manifest",
]

#: The operational manifest contract.
PAPER_OPERATIONS_MANIFEST_VERSION = "PAPER_OPERATIONS_MANIFEST_V1"


@dataclass(frozen=True)
class PaperOperationsRunReport:
    """What one run did, start to finish.

    Attributes:
        run_id: The derived run id.
        schedule_id: Which schedule.
        session: Which session it was for.
        mode: Which mode it ran in.
        status: How it ended.
        scheduled_for: When it was due, ISO-8601.
        started_at: When it actually began.
        ended_at: When it finished.
        health_before: The gate's verdict before acting.
        reconciliation_before: The three-way verdict before acting.
        reconciliation_after: The three-way verdict afterwards.
        inputs: The inputs it froze, with their freshness.
        planned_orders: How many orders the plan authorised.
        orders_submitted: How many reached the venue.
        orders_filled: How many filled.
        would_submit: In observe-only, what it *would* have sent. Marked
            `NOT_SUBMITTED` so a reader cannot mistake a plan for a trade.
        external_mutations: How many mutating requests were made. Zero in
            observe-only, asserted rather than asserted-about.
        execution_manifest_fingerprint: The execution artefact, when it traded.
        accounting_before: The book's state fingerprint before.
        accounting_after: The book's state fingerprint after.
        kill_state: The control state at the end.
        incidents: Incidents this run recorded.
        blockers: Why it stopped, when it did.
    """

    run_id: str
    schedule_id: str
    session: str
    mode: OperationsMode
    status: RunStatus
    scheduled_for: str = ""
    started_at: str = ""
    ended_at: str = ""
    health_before: str = ""
    reconciliation_before: str = ""
    reconciliation_after: str = ""
    inputs: tuple[OperationalInput, ...] = field(default_factory=tuple)
    planned_orders: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    would_submit: tuple[str, ...] = field(default_factory=tuple)
    external_mutations: int = 0
    execution_manifest_fingerprint: str = ""
    accounting_before: str = ""
    accounting_after: str = ""
    kill_state: str = ""
    incidents: tuple[str, ...] = field(default_factory=tuple)
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        """Whether this run did what it set out to do.

        A blocked run is not a success, however correct the block was.
        """
        return self.status.is_success

    def body(self) -> dict[str, Any]:
        """Return the report's semantic content."""
        return {
            "operations_engine_version": PAPER_OPERATIONS_ENGINE_VERSION,
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "session": self.session,
            "mode": str(self.mode),
            "status": str(self.status),
            "succeeded": self.succeeded,
            "scheduled_for": self.scheduled_for,
            "health_before": self.health_before,
            "reconciliation_before": self.reconciliation_before,
            "reconciliation_after": self.reconciliation_after,
            "inputs": [row.as_json() for row in self.inputs],
            "planned_orders": self.planned_orders,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "would_submit": [f"{row} (NOT_SUBMITTED)" for row in self.would_submit],
            "external_mutations": self.external_mutations,
            "execution_manifest_fingerprint": self.execution_manifest_fingerprint,
            "accounting_before": self.accounting_before,
            "accounting_after": self.accounting_after,
            "kill_state": self.kill_state,
            "incidents": list(self.incidents),
            "blockers": list(self.blockers),
            "live_trading": "NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the report. Excludes wall-clock start and end."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the report with its timestamps and fingerprint."""
        return {
            **self.body(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "run_report_fingerprint": self.fingerprint(),
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal."""
        lines = [
            f"run                : {self.run_id}",
            f"schedule / session : {self.schedule_id} / {self.session}",
            f"mode               : {self.mode}",
            f"status             : {self.status} (success: {self.succeeded})",
            f"health before      : {self.health_before or '(not evaluated)'}",
            f"reconcile before   : {self.reconciliation_before or '(not run)'}",
            f"reconcile after    : {self.reconciliation_after or '(not run)'}",
            f"orders             : {self.planned_orders} planned, "
            f"{self.orders_submitted} submitted, {self.orders_filled} filled",
            f"external mutations : {self.external_mutations}",
            f"kill state         : {self.kill_state}",
        ]
        for row in self.would_submit:
            lines.append(f"  WOULD SUBMIT (NOT_SUBMITTED): {row}")
        for row in self.blockers:
            lines.append(f"  BLOCKED: {row}")
        for row in self.incidents:
            lines.append(f"  INCIDENT: {row}")
        return tuple(lines)


@dataclass(frozen=True)
class OperationsStatusReport:
    """What the system looks like right now, safe to hand to an operator.

    Attributes:
        mode: The widest mode currently authorised.
        armed: Whether operations are armed at all.
        authorization_id: Which grant stands, if any.
        authorization_expires_at: When it lapses, if it does.
        kill_state: The control state.
        kill_reason: Why, if it is not active.
        account_binding_fingerprint: Which account. Never an account number.
        health_state: The last evaluated health.
        last_clean_reconciliation: When the three-way sweep was last clean.
        last_successful_run: The last run that actually succeeded. Kept apart
            from `last_run` so a string of blocked runs cannot read as activity.
        last_run: The last run of any outcome.
        current_run: The run in flight, if one is.
        open_incidents: Open incident ids with their severities.
        owned_open_orders: Remote orders this system can prove it placed.
        residual_positions: External paper holdings this book accounts for.
        next_scheduled_slot: When the next run is due.
        code_commit: Which build is running.
    """

    mode: OperationsMode
    armed: bool
    kill_state: str
    account_binding_fingerprint: str
    authorization_id: str = ""
    authorization_expires_at: str | None = None
    kill_reason: str = ""
    health_state: str = ""
    last_clean_reconciliation: str = ""
    last_successful_run: str = ""
    last_run: str = ""
    current_run: str = ""
    open_incidents: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    owned_open_orders: tuple[str, ...] = field(default_factory=tuple)
    residual_positions: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    next_scheduled_slot: str = ""
    code_commit: str = ""

    def as_json(self) -> dict[str, Any]:
        """Return the status as a JSON-safe mapping. Carries no credential."""
        return {
            "operations_engine_version": PAPER_OPERATIONS_ENGINE_VERSION,
            "mode": str(self.mode),
            "armed": self.armed,
            "authorization_id": self.authorization_id,
            "authorization_expires_at": self.authorization_expires_at,
            "kill_state": self.kill_state,
            "kill_reason": self.kill_reason,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "health_state": self.health_state,
            "last_clean_reconciliation": self.last_clean_reconciliation,
            "last_successful_run": self.last_successful_run,
            "last_run": self.last_run,
            "current_run": self.current_run,
            "open_incidents": [list(pair) for pair in self.open_incidents],
            "owned_open_orders": list(self.owned_open_orders),
            "residual_positions": [list(pair) for pair in self.residual_positions],
            "next_scheduled_slot": self.next_scheduled_slot,
            "code_commit": self.code_commit,
            "live_trading": "NOT_AUTHORIZED",
            "auto_liquidation": "NONE",
            "auto_repair": "NONE",
            "provider_outage_fallback": "NONE",
            "scheduled_run_backfill": "NONE",
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the status for a terminal."""
        lines = [
            f"mode                  : {self.mode}",
            f"armed                 : {self.armed}"
            + (f" ({self.authorization_id})" if self.authorization_id else ""),
            f"kill state            : {self.kill_state}"
            + (f" ({self.kill_reason})" if self.kill_reason else ""),
            f"account binding       : {self.account_binding_fingerprint[:16] or '(none)'}",
            f"health                : {self.health_state or '(not evaluated)'}",
            f"last clean reconcile  : {self.last_clean_reconciliation or 'never'}",
            f"last successful run   : {self.last_successful_run or 'never'}",
            f"last run              : {self.last_run or 'never'}",
            f"current run           : {self.current_run or '(none)'}",
            f"next scheduled slot   : {self.next_scheduled_slot or '(none due)'}",
            f"owned open orders     : {len(self.owned_open_orders)}",
            f"residual positions    : {len(self.residual_positions)}",
            f"open incidents        : {len(self.open_incidents)}",
            "live trading          : NOT_AUTHORIZED",
            "auto liquidation      : NONE",
            "auto repair           : NONE",
        ]
        for incident_id, severity in self.open_incidents:
            lines.append(f"  [{severity}] {incident_id}")
        return tuple(lines)


def paper_operations_manifest(
    *,
    run_id: str,
    schedule_fingerprint: str,
    authorization_fingerprint: str,
    kill_state_fingerprint: str,
    health_fingerprint: str,
    incident_set_fingerprint: str,
    account_binding_fingerprint: str,
    accounting_before: str = "",
    accounting_after: str = "",
    reconciliation_before: str = "",
    reconciliation_after: str = "",
    execution_manifest_fingerprint: str = "",
    operations_journal_head: str = "",
    code_commit: str = "",
) -> dict[str, Any]:
    """Bind everything a run operated under into one fingerprinted artefact.

    Args:
        run_id: The derived run id.
        schedule_fingerprint: Which schedule.
        authorization_fingerprint: Which grant.
        kill_state_fingerprint: The control state.
        health_fingerprint: The gate's verdict.
        incident_set_fingerprint: The open incidents.
        account_binding_fingerprint: Which account.
        accounting_before: The book before.
        accounting_after: The book after.
        reconciliation_before: The three-way verdict before.
        reconciliation_after: The three-way verdict after.
        execution_manifest_fingerprint: The execution artefact, if it traded.
        operations_journal_head: The operational journal's chain head.
        code_commit: Which build.

    Returns:
        The manifest, with its own fingerprint.
    """
    from stock_screener.execution import fingerprint_of

    body: dict[str, Any] = {
        "manifest_version": PAPER_OPERATIONS_MANIFEST_VERSION,
        "policies": operations_policy(),
        "bindings": {
            "run_id": run_id,
            "schedule_fingerprint": schedule_fingerprint,
            "authorization_fingerprint": authorization_fingerprint,
            "kill_state_fingerprint": kill_state_fingerprint,
            "health_fingerprint": health_fingerprint,
            "incident_set_fingerprint": incident_set_fingerprint,
            "account_binding_fingerprint": account_binding_fingerprint,
            "accounting_state_before": accounting_before,
            "accounting_state_after": accounting_after,
            "reconciliation_before": reconciliation_before,
            "reconciliation_after": reconciliation_after,
            "execution_manifest_fingerprint": execution_manifest_fingerprint,
            "operations_journal_head": operations_journal_head,
            "code_commit": code_commit,
        },
        "authority": {
            "operations": "observes and gates; invents nothing",
            "strategy_and_risk": "decides what may be held",
            "execution": "decides how an authorised delta becomes an order",
            "broker": "decides what fills",
            "accounting": "decides holdings, cash and P&L",
        },
        "live_trading": "NOT_AUTHORIZED",
    }
    body["manifest_fingerprint"] = fingerprint_of(body)
    return body
