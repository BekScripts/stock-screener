"""What an operational run is, and the states it may legally be in.

Two enums carry most of the safety in this package.

`OperationsMode` has exactly two members and no live one. A mode is the answer
to "may this run touch the venue at all", and `OBSERVE_ONLY` answers no — not
"probably not", not "unless something goes wrong". It is checked before every
mutation rather than once at the top, because a run that was observe-only at
startup must still be observe-only ten minutes later.

`RunState` is a frozen lifecycle with an explicit transition table. The reason
for the table rather than a convention is that the dangerous transitions are the
plausible-looking ones: `EXECUTING` back to `READY` would re-run a plan whose
orders are already at the venue, and `KILLED` to `EXECUTING` would resume after
a kill. Neither is in the table, so neither can happen by a caller's mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import Code, OperationsError

__all__ = [
    "MUTATION_CAPABLE_MODES",
    "RUN_TRANSITIONS",
    "TERMINAL_RUN_STATES",
    "FreshnessPolicy",
    "OperationalInput",
    "OperationsMode",
    "RunState",
    "RunStatus",
    "require_run_transition",
]


class OperationsMode(StrEnum):
    """Whether a run may touch the venue.

    There is deliberately no live member. Adding one would not be sufficient to
    enable live trading — the transport is host-locked and the paper proof binds
    the origin — but it would be the first step, and the enum is where that step
    is refused.
    """

    OBSERVE_ONLY = "OBSERVE_ONLY"
    """Read, plan, report. Zero external mutations, asserted by counting."""

    EXTERNAL_PAPER = "EXTERNAL_PAPER"
    """May submit to the certified paper account, once every gate agrees."""

    @property
    def may_mutate(self) -> bool:
        """Whether this mode permits an external mutation at all."""
        return self is OperationsMode.EXTERNAL_PAPER

    @property
    def is_live(self) -> bool:
        """Always false. Present so a caller asserts it rather than assumes."""
        return False


#: The modes that may submit anything. One, and a test asserts the count.
MUTATION_CAPABLE_MODES: frozenset[OperationsMode] = frozenset({OperationsMode.EXTERNAL_PAPER})


class RunState(StrEnum):
    """Where an operational run is in its life."""

    SCHEDULED = "SCHEDULED"
    """Due, and nothing has been done yet."""

    RECOVERING = "RECOVERING"
    """Reading back local state before deciding anything."""

    HEALTH_CHECK = "HEALTH_CHECK"
    """Evaluating whether the system may act."""

    RECONCILING = "RECONCILING"
    """Comparing journal, book and venue."""

    READY = "READY"
    """Every gate passed. Nothing has been submitted."""

    PLANNING = "PLANNING"
    """Building targets and an authorised plan. No mutation yet."""

    EXECUTING = "EXECUTING"
    """Orders are being submitted. The only state in which a mutation happens."""

    VERIFYING = "VERIFYING"
    """Ingesting fills and reconciling afterwards."""

    COMPLETED = "COMPLETED"
    """Finished. Terminal."""

    BLOCKED = "BLOCKED"
    """A gate refused. Terminal for this run, and not a failure of the system —
    a gate refusing is the system working."""

    PAUSED = "PAUSED"
    """Stopped by an operator pause. Terminal for this run."""

    KILLED = "KILLED"
    """Stopped by the kill switch. Terminal for this run."""

    MISSED = "MISSED"
    """The slot passed before the process looked. Terminal, and never
    backfilled."""

    INCIDENT = "INCIDENT"
    """Stopped by a blocking or critical incident. Terminal."""

    FAILED = "FAILED"
    """Stopped by an unexpected error. Terminal."""


#: Which transitions are legal. The omissions are the point: `EXECUTING` has no
#: edge back to `READY` or `PLANNING`, because re-entering either would re-plan
#: or re-submit orders that are already at the venue; and no state transitions
#: *out* of `KILLED`, because resuming after a kill must go through an explicit
#: operator recovery rather than a state change.
RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.SCHEDULED: frozenset(
        {
            RunState.RECOVERING,
            RunState.MISSED,
            RunState.KILLED,
            RunState.PAUSED,
            RunState.BLOCKED,
            RunState.FAILED,
        }
    ),
    RunState.RECOVERING: frozenset(
        {
            RunState.HEALTH_CHECK,
            RunState.KILLED,
            RunState.PAUSED,
            RunState.BLOCKED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.HEALTH_CHECK: frozenset(
        {
            RunState.RECONCILING,
            RunState.KILLED,
            RunState.PAUSED,
            RunState.BLOCKED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.RECONCILING: frozenset(
        {
            RunState.READY,
            RunState.KILLED,
            RunState.PAUSED,
            RunState.BLOCKED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.READY: frozenset(
        {
            RunState.PLANNING,
            RunState.COMPLETED,
            RunState.KILLED,
            RunState.PAUSED,
            RunState.BLOCKED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.PLANNING: frozenset(
        {
            RunState.EXECUTING,
            RunState.COMPLETED,
            RunState.KILLED,
            RunState.PAUSED,
            RunState.BLOCKED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.VERIFYING,
            RunState.KILLED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.COMPLETED,
            RunState.KILLED,
            RunState.INCIDENT,
            RunState.FAILED,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.BLOCKED: frozenset(),
    RunState.PAUSED: frozenset(),
    RunState.KILLED: frozenset(),
    RunState.MISSED: frozenset(),
    RunState.INCIDENT: frozenset(),
    RunState.FAILED: frozenset(),
}

#: States a run never leaves.
TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    state for state, allowed in RUN_TRANSITIONS.items() if not allowed
)


def require_run_transition(current: RunState, proposed: RunState) -> None:
    """Refuse an illegal run transition, before anything durable is written.

    Args:
        current: Where the run is.
        proposed: Where the caller wants it.

    Raises:
        OperationsError: `INVALID_OPERATIONS_RUN_TRANSITION` if the edge is not
            in the table. Checked before the write, following the P1.4 lesson: a
            refused transition that reached disk first leaves a journal that can
            never be replayed.
    """
    if proposed not in RUN_TRANSITIONS.get(current, frozenset()):
        raise OperationsError(
            Code.INVALID_OPERATIONS_RUN_TRANSITION,
            f"a run cannot go from {current} to {proposed}. Legal next states are "
            f"{sorted(str(state) for state in RUN_TRANSITIONS.get(current, frozenset()))}"
            + (
                ". Resuming after a kill is a deliberate operator recovery, not a state transition"
                if current is RunState.KILLED
                else ""
            ),
        )


class RunStatus(StrEnum):
    """How a finished run is reported.

    Distinct from `RunState` because a reader asking "what happened" wants a
    different granularity from the state machine. The distinction that matters
    most: a blocked run is not a successful one, and `COMPLETED_NO_ACTION` is not
    the same as `BLOCKED` — the first means every gate passed and the strategy
    had nothing to do, the second means a gate refused.
    """

    COMPLETED_NO_ACTION = "COMPLETED_NO_ACTION"
    COMPLETED_PAPER_EXECUTION = "COMPLETED_PAPER_EXECUTION"
    COMPLETED_OBSERVE_ONLY = "COMPLETED_OBSERVE_ONLY"
    BLOCKED = "BLOCKED"
    MISSED = "MISSED"
    KILLED = "KILLED"
    PAUSED = "PAUSED"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"

    @property
    def is_success(self) -> bool:
        """Whether this run did what it set out to do.

        A blocked run is explicitly not a success, however correct the block
        was. Reporting it as one is how a system that has stopped working looks
        healthy on a dashboard.
        """
        return self in {
            RunStatus.COMPLETED_NO_ACTION,
            RunStatus.COMPLETED_PAPER_EXECUTION,
            RunStatus.COMPLETED_OBSERVE_ONLY,
            RunStatus.RECOVERED,
        }


@dataclass(frozen=True)
class FreshnessPolicy:
    """How old an operational input may be.

    Attributes:
        name: What the policy governs.
        max_age_seconds: The limit. Inclusive — an input exactly at the limit is
            fresh. Stated because `<` and `<=` differing between two sources is
            the kind of inconsistency that produces a run that is fresh by one
            check and stale by another.
    """

    name: str
    max_age_seconds: float

    def __post_init__(self) -> None:
        """Refuse a policy that permits anything.

        Raises:
            OperationsError: `INVALID_FRESHNESS_POLICY` if the limit is not
                positive and finite. An unbounded policy is the same as no
                policy, and "no policy" should be spelled by not having one.
        """
        if not (self.max_age_seconds > 0) or self.max_age_seconds == float("inf"):
            raise OperationsError(
                Code.INVALID_FRESHNESS_POLICY,
                f"{self.name}: a freshness limit must be positive and finite, got "
                f"{self.max_age_seconds!r}. An unbounded limit permits any input and "
                "should be spelled by having no policy, not by an infinite one",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the policy as a JSON-safe mapping."""
        return {
            "name": self.name,
            "max_age_seconds": self.max_age_seconds,
            "boundary": "inclusive — an input exactly at the limit is fresh",
        }


@dataclass(frozen=True)
class OperationalInput:
    """One input a run depends on, with its age and provenance declared.

    Attributes:
        name: What it is.
        as_of: When it was good as of, ISO-8601 with an offset. Semantic time
            from the source, never the operational clock.
        source_fingerprint: What produced it.
        policy: How old it may be.
    """

    name: str
    as_of: str
    source_fingerprint: str
    policy: FreshnessPolicy

    def age_seconds(self, now: str) -> float:
        """How old this input is at a given instant.

        Args:
            now: The operational instant, ISO-8601.

        Returns:
            The age in seconds. Negative when the input claims to be from the
            future, which is reported rather than clamped — a future `as_of` is
            a clock or provenance problem and hiding it would make it permanent.
        """
        from .clock import parse_instant

        return (parse_instant(now) - parse_instant(self.as_of)).total_seconds()

    def is_fresh(self, now: str) -> bool:
        """Whether this input is within its policy.

        Args:
            now: The operational instant.

        Returns:
            True when the age is at or under the limit. An input from the future
            is not fresh: it cannot be trusted to describe the present.
        """
        age = self.age_seconds(now)
        return 0 <= age <= self.policy.max_age_seconds

    def require_fresh(self, now: str) -> None:
        """Refuse a stale or future-dated input.

        Args:
            now: The operational instant.

        Raises:
            OperationsError: `STALE_OPERATIONAL_INPUT` if it is outside its
                policy. Never traded on: an input existing is not evidence that
                it still describes the market.
        """
        if self.is_fresh(now):
            return
        age = self.age_seconds(now)
        detail = (
            f"claims an as_of {abs(age):.0f}s in the future"
            if age < 0
            else f"is {age:.0f}s old against a {self.policy.max_age_seconds:.0f}s limit"
        )
        raise OperationsError(
            Code.STALE_OPERATIONAL_INPUT,
            f"{self.name} {detail}. No order is submitted on it — an input existing "
            "is not evidence that it still describes the market",
        )

    def as_json(self) -> dict[str, Any]:
        """Return the input as a JSON-safe mapping."""
        return {
            "name": self.name,
            "as_of": self.as_of,
            "source_fingerprint": self.source_fingerprint,
            "policy": self.policy.as_json(),
        }
