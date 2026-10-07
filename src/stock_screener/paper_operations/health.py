"""Whether the system may act, decided before it acts and checked again later.

Health is a gate, not a dashboard. Its only job is to answer "may a new external
order be submitted right now", and it answers no by default: `require_mutation`
permits a submission only when the overall state is `HEALTHY`. There is no
`DEGRADED` path to a mutation, because every "degraded but probably fine"
exception anyone has ever written is the one that later turns out to have been
the problem.

The checks are declared as a list rather than written as a chain of early
returns, so a failure does not hide the ones after it. An operator fixing one
cause and rediscovering the next is how a configuration takes four runs to
correct.

Two distinctions this module is careful about. `BLOCKED` means an external
condition stops us — the provider is unreachable, a reconciliation break stands,
an input is stale. `UNHEALTHY` means our own records do not verify. Both refuse
a submission; they need different responses from a person. And a health report
is a *moment*: a run that was healthy at T0 is not thereby healthy at T0 plus
ten minutes, which is why the coordinator re-evaluates before every order rather
than trusting the one it took at the start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import Code, OperationsError

if TYPE_CHECKING:
    from .incidents import OperationalIncident

__all__ = [
    "REQUIRED_HEALTH_CHECKS",
    "HealthCheck",
    "HealthState",
    "PaperOperationsHealthReport",
]


class HealthState(StrEnum):
    """How fit the system is to act."""

    HEALTHY = "HEALTHY"
    """Every required check passed. The only state that permits a mutation."""

    DEGRADED = "DEGRADED"
    """Something is wrong that does not stop a run from being understood, but
    does stop it from submitting. Deliberately not a mutation-capable state."""

    BLOCKED = "BLOCKED"
    """An external condition stops us: the provider, a reconciliation break, a
    stale input. Our records are fine; the world is not cooperating."""

    UNHEALTHY = "UNHEALTHY"
    """Our own records do not verify. Worse than blocked, and needs a person."""

    @property
    def may_mutate(self) -> bool:
        """Whether an external submission is permitted in this state."""
        return self is HealthState.HEALTHY


#: Every check a mutation-capable run must pass, in evaluation order. Declared
#: as data so the report can say which ones were *not* reached as well as which
#: failed, and so a new requirement cannot be added without appearing here.
REQUIRED_HEALTH_CHECKS: tuple[str, ...] = (
    "OPERATIONS_ARMED",
    "KILL_SWITCH_ACTIVE",
    "CONTROL_STATE_VERIFIED",
    "EXECUTION_JOURNAL_VERIFIED",
    "ACCOUNTING_LEDGER_VERIFIED",
    "OPERATIONS_JOURNAL_VERIFIED",
    "PAPER_ENVIRONMENT_PROVEN",
    "ACCOUNT_BINDING_MATCHED",
    "PROVIDER_REACHABLE",
    "STARTUP_RECONCILIATION_CLEAN",
    "NO_REMOTE_STATE_UNKNOWN",
    "NO_UNEXPECTED_REMOTE_ORDER",
    "NO_UNEXPLAINED_REMOTE_POSITION",
    "REQUIRED_INPUTS_PRESENT",
    "REQUIRED_INPUTS_FRESH",
    "SESSION_ELIGIBLE",
    "NO_OVERLAPPING_RUN",
    "NO_BLOCKING_INCIDENT",
)


@dataclass(frozen=True)
class HealthCheck:
    """One check and what it found.

    Attributes:
        name: Which check.
        passed: Whether it passed.
        state: What this check implies about the system when it fails.
        reason_code: A stable code for the failure.
        detail: A sanitised explanation. Never a credential.
    """

    name: str
    passed: bool
    state: HealthState = HealthState.HEALTHY
    reason_code: str = ""
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        """Return the check as a JSON-safe mapping."""
        return {
            "name": self.name,
            "passed": self.passed,
            "implies": str(self.state) if not self.passed else str(HealthState.HEALTHY),
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PaperOperationsHealthReport:
    """What the gate concluded at one moment.

    Attributes:
        checks: Every check that ran, in order.
        evaluated_at: When, ISO-8601 operational time.
        run_id: Which run it was for.
        account_binding_fingerprint: Which account.
        open_incidents: Open incidents at evaluation time.
    """

    checks: tuple[HealthCheck, ...]
    evaluated_at: str = ""
    run_id: str = ""
    account_binding_fingerprint: str = ""
    open_incidents: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> tuple[HealthCheck, ...]:
        """Every check that did not pass."""
        return tuple(check for check in self.checks if not check.passed)

    @property
    def not_reached(self) -> tuple[str, ...]:
        """Required checks that did not run.

        Reported rather than assumed passed. A check that never ran has
        established nothing, and treating silence as a pass is how a gate ends
        up permitting what it was built to stop.
        """
        ran = {check.name for check in self.checks}
        return tuple(name for name in REQUIRED_HEALTH_CHECKS if name not in ran)

    @property
    def state(self) -> HealthState:
        """The overall state.

        The worst thing found wins, and a required check that did not run counts
        as not having passed.
        """
        if self.not_reached:
            return HealthState.BLOCKED
        order = (
            HealthState.UNHEALTHY,
            HealthState.BLOCKED,
            HealthState.DEGRADED,
        )
        for state in order:
            if any(check.state is state for check in self.failures):
                return state
        return HealthState.HEALTHY if not self.failures else HealthState.BLOCKED

    @property
    def may_mutate(self) -> bool:
        """Whether an external submission is permitted."""
        return self.state.may_mutate

    def require_mutation(self, *, context: str) -> None:
        """Refuse a submission unless every required check passed.

        Fails closed, and names every failure rather than the first — fixing one
        cause and rediscovering the next is how a configuration takes four runs
        to correct.

        Args:
            context: What was being attempted.

        Raises:
            OperationsError: The most specific code the failures justify, so a
                caller can distinguish a provider outage from a reconciliation
                break from a stale input without parsing prose.
        """
        if self.may_mutate:
            return
        codes = {check.reason_code for check in self.failures if check.reason_code}
        specific = next(
            (
                code
                for code in (
                    Code.BLOCKED_LOCAL_INTEGRITY,
                    Code.BLOCKED_REMOTE_STATE_UNKNOWN,
                    Code.BLOCKED_RECONCILIATION_BREAK,
                    Code.BLOCKED_PROVIDER_UNAVAILABLE,
                    Code.STALE_OPERATIONAL_INPUT,
                    Code.REQUIRED_OPERATIONAL_INPUT_MISSING,
                    Code.BLOCKED_OPEN_INCIDENT,
                    Code.PAPER_OPERATIONS_KILLED,
                    Code.PAPER_OPERATIONS_NOT_ARMED,
                )
                if code in codes
            ),
            Code.OPERATIONS_HEALTH_NOT_READY,
        )
        detail = "; ".join(
            f"{check.name}: {check.detail or check.reason_code}" for check in self.failures
        )
        unreached = (
            f" Checks that did not run: {list(self.not_reached)}." if self.not_reached else ""
        )
        raise OperationsError(
            specific,
            f"{context}: health is {self.state} and no external order will be "
            f"submitted. {detail}{unreached}",
        )

    def body(self) -> dict[str, Any]:
        """Return the report's semantic content."""
        return {
            "state": str(self.state),
            "may_mutate": self.may_mutate,
            "run_id": self.run_id,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "required_checks": list(REQUIRED_HEALTH_CHECKS),
            "checks": [check.as_json() for check in self.checks],
            "failures": [check.name for check in self.failures],
            "not_reached": list(self.not_reached),
            "open_incidents": list(self.open_incidents),
            "mutation_gate": "HEALTHY only; there is no DEGRADED path to a submission",
        }

    def fingerprint(self) -> str:
        """Digest the report. Excludes `evaluated_at` so it is a value."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the report with its timestamp and fingerprint."""
        return {
            **self.body(),
            "evaluated_at": self.evaluated_at,
            "health_fingerprint": self.fingerprint(),
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal."""
        lines = [f"health: {self.state} (may mutate: {self.may_mutate})"]
        for check in self.checks:
            mark = "pass" if check.passed else "FAIL"
            lines.append(
                f"  [{mark}] {check.name}"
                + (f" — {check.detail or check.reason_code}" if not check.passed else "")
            )
        for name in self.not_reached:
            lines.append(f"  [----] {name} — not reached")
        return tuple(lines)

    @classmethod
    def from_incidents(
        cls,
        incidents: tuple[OperationalIncident, ...],
        *,
        checks: tuple[HealthCheck, ...],
        evaluated_at: str,
        run_id: str,
        account_binding_fingerprint: str,
    ) -> PaperOperationsHealthReport:
        """Build a report, folding open incidents into it.

        Args:
            incidents: Every open incident.
            checks: The checks that ran.
            evaluated_at: When.
            run_id: Which run.
            account_binding_fingerprint: Which account.

        Returns:
            The report, with a blocking-incident check appended.
        """
        blocking = tuple(incident for incident in incidents if incident.blocks_submission)
        incident_check = HealthCheck(
            name="NO_BLOCKING_INCIDENT",
            passed=not blocking,
            state=HealthState.BLOCKED,
            reason_code=Code.BLOCKED_OPEN_INCIDENT if blocking else "",
            detail=(
                f"{len(blocking)} blocking incident(s) open: "
                f"{[incident.reason_code for incident in blocking]}"
                if blocking
                else ""
            ),
        )
        return cls(
            checks=(*checks, incident_check),
            evaluated_at=evaluated_at,
            run_id=run_id,
            account_binding_fingerprint=account_binding_fingerprint,
            open_incidents=tuple(incident.incident_id for incident in incidents),
        )
