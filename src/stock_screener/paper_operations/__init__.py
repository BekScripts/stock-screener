"""The operational control plane for repeated paper running.

`PAPER_OPERATIONS_ENGINE_V1`. P2.0 to P2.2 proved that *one* authorised external
paper execution can be made safe. This package is what makes running it again,
unattended, on a schedule, also safe — and most of what it contains is refusal.

**It gains no authority.** The authority chain is unchanged: strategy and
research find opportunities, portfolio and risk decide what may be held,
execution decides how an authorised delta becomes an order, the broker decides
what fills, accounting decides holdings and cash. This plane sits beside all of
it and answers one question — *may the system act right now* — then records what
happened. It cannot invent a target, an order, a fill, a position, a cash
balance or a risk approval, and the coordinator takes planning and submission as
injected callables specifically so it cannot acquire the ability.

**The default is no.** A fresh install is `NOT_ARMED`. A missing kill-switch
file is `KILLED`. A corrupt one is `KILLED`. A tampered one is `KILLED`. Absent
control state is never permission, because the alternative is a disk error
granting the right to trade.

**Permission is not cached.** The kill switch is re-read before every single
order, not once per run. An operator who kills mid-run must not have to wait for
the run to finish, and a process armed at startup has not been armed for the
rest of time.

**A kill stops activity; it does not trade.** It blocks new submissions and
cancels open orders it can *prove* it placed. It never liquidates a position — a
filled position is an accounting fact an authorised decision created, and only
another authorised decision may unwind it. A kill switch that flattened the book
would be the largest unreviewed trade the system ever made, at exactly the
moment something is already known to be wrong. And it never touches an order it
cannot prove it owns: that belongs to somebody else.

**Nothing is substituted for missing truth.** A stale input blocks rather than
trading on a figure that no longer describes the market. A missing input blocks
rather than being guessed or reused from the last run. An unreachable venue
blocks rather than falling back to the local simulator — the venue is bound for
the run. A missed slot is recorded and never run late, because an order planned
against yesterday's prices is a new unreviewed decision wearing an old
authorisation. A reconciliation break blocks and is never repaired.

**Runs are identified, not generated.** A run id is derived from its slot's
semantic inputs, so invoking the scheduler twice for one slot recovers the first
run instead of starting a competitor — which is the easiest way to submit one
decision as two orders. A durable lease stops two processes overlapping, and a
stale lease is never taken on age: a stale lease and a crashed run look
identical from outside, and the crashed run may already have submitted.

What this package never does: trade live, hold a credential, invent any
financial fact, liquidate anything, cancel a stranger's order, repair a
reconciliation break, fall back to a simulator, backfill a missed run, or treat
unreadable control state as permission.
"""

from __future__ import annotations

from .arming import ARMING_FILENAME, ArmingStore, PaperOperationsAuthorization
from .clock import Clock, FakeClock, SystemClock, parse_instant
from .coordinator import (
    OperationsEnvironment,
    PaperOperationsCoordinator,
    RunOutcome,
    SubmissionGate,
)
from .errors import OPERATIONS_ERROR_CODES, Code, OperationsError
from .health import (
    REQUIRED_HEALTH_CHECKS,
    HealthCheck,
    HealthState,
    PaperOperationsHealthReport,
)
from .incidents import (
    INCIDENTS_DIRECTORY,
    IncidentEvent,
    IncidentSeverity,
    IncidentStatus,
    IncidentStore,
    OperationalIncident,
)
from .kill_switch import (
    CRITICAL_AUTO_KILL_REASONS,
    KILL_FILENAME,
    KillState,
    KillSwitchStore,
    PaperKillSwitch,
)
from .leases import LEASE_FILENAME, LeaseState, RunLease, RunLeaseStore
from .models import (
    MUTATION_CAPABLE_MODES,
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATES,
    FreshnessPolicy,
    OperationalInput,
    OperationsMode,
    RunState,
    RunStatus,
    require_run_transition,
)
from .persistence import (
    FORBIDDEN_OPERATIONS_ROOTS,
    OPERATIONS_DIRECTORY,
    atomic_write_verified,
    guard_operations_root,
    read_json,
)
from .reports import (
    PAPER_OPERATIONS_MANIFEST_VERSION,
    OperationsStatusReport,
    PaperOperationsRunReport,
    paper_operations_manifest,
)
from .run_journal import (
    ANCHOR_FILENAME,
    GENESIS_HASH,
    JOURNAL_DIRECTORY,
    OperationsJournalEntry,
    OperationsRunJournal,
    RunEventType,
    chain_link,
)
from .schedule import PaperOperationsSchedule, ScheduledSlot, operations_run_id
from .versions import (
    PAPER_OPERATIONS_ENGINE_VERSION,
    REQUIRED_ACCOUNTING_VERSION,
    REQUIRED_EXECUTION_VERSION,
    operations_policy,
)

__all__ = [
    "ANCHOR_FILENAME",
    "ARMING_FILENAME",
    "CRITICAL_AUTO_KILL_REASONS",
    "FORBIDDEN_OPERATIONS_ROOTS",
    "GENESIS_HASH",
    "INCIDENTS_DIRECTORY",
    "JOURNAL_DIRECTORY",
    "KILL_FILENAME",
    "LEASE_FILENAME",
    "MUTATION_CAPABLE_MODES",
    "OPERATIONS_DIRECTORY",
    "OPERATIONS_ERROR_CODES",
    "PAPER_OPERATIONS_ENGINE_VERSION",
    "PAPER_OPERATIONS_MANIFEST_VERSION",
    "REQUIRED_ACCOUNTING_VERSION",
    "REQUIRED_EXECUTION_VERSION",
    "REQUIRED_HEALTH_CHECKS",
    "RUN_TRANSITIONS",
    "TERMINAL_RUN_STATES",
    "ArmingStore",
    "Clock",
    "Code",
    "FakeClock",
    "FreshnessPolicy",
    "HealthCheck",
    "HealthState",
    "IncidentEvent",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentStore",
    "KillState",
    "KillSwitchStore",
    "LeaseState",
    "OperationalIncident",
    "OperationalInput",
    "OperationsEnvironment",
    "OperationsError",
    "OperationsJournalEntry",
    "OperationsMode",
    "OperationsRunJournal",
    "OperationsStatusReport",
    "PaperKillSwitch",
    "PaperOperationsAuthorization",
    "PaperOperationsCoordinator",
    "PaperOperationsHealthReport",
    "PaperOperationsRunReport",
    "PaperOperationsSchedule",
    "RunEventType",
    "RunLease",
    "RunLeaseStore",
    "RunOutcome",
    "RunState",
    "RunStatus",
    "ScheduledSlot",
    "SubmissionGate",
    "SystemClock",
    "atomic_write_verified",
    "chain_link",
    "guard_operations_root",
    "operations_policy",
    "operations_run_id",
    "paper_operations_manifest",
    "parse_instant",
    "read_json",
    "require_run_transition",
]
