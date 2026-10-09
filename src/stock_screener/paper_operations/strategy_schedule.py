"""A schedule that can say *why* a run is due, not only when.

`PAPER_OPERATIONS_SCHEDULE_V1` holds one time of day. That was right for a
control plane whose job was to look at the account once a session, and it cannot
express what S1.0 needs, because the strategy's decision and its execution are
two different market instants:

    decision   last market session of the month, at its 16:00 ET close
    execution  the first session strictly after that, near its open

Those cannot be one slot, and they must not be collapsed into one. A decision
executed at its own close would be executed on the data that produced it; an
execution delayed to the following close would be a different trade from the one
the research convention describes.

So this is an **additive** V1.1: `PAPER_OPERATIONS_SCHEDULE_V1` keeps its exact
meaning and its run ids, and V1.1 adds purpose-tagged slots beside it. Nothing
about the execution engine, the accounting engine or the strategy changes —
their semantics did not move, only the operational calendar's ability to
describe two events in one session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .errors import Code, OperationsError
from .versions import PAPER_OPERATIONS_ENGINE_VERSION

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

    from .models import OperationsMode

__all__ = [
    "SCHEDULE_CONTRACT_V1_1",
    "DecisionCadence",
    "RunPurpose",
    "StrategyOperationsSchedule",
    "StrategySlot",
]

#: The additive schedule contract.
SCHEDULE_CONTRACT_V1_1 = "PAPER_OPERATIONS_SCHEDULE_V1_1"


class RunPurpose(StrEnum):
    """What an operational run is for.

    Part of a run's identity, because one market session can legitimately hold
    more than one operational event and those must not recover each other. A
    decision run and an execution run on the same date are not a duplicate
    scheduler; collapsing them would be.
    """

    SESSION_OBSERVATION = "SESSION_OBSERVATION"
    """Look at the account and the records. Mutates nothing, ever."""

    STRATEGY_DECISION = "STRATEGY_DECISION"
    """Ask the strategy for a selection. Submits nothing — the decision it
    produces is not executable until a later session."""

    STRATEGY_EXECUTION = "STRATEGY_EXECUTION"
    """Execute an already-frozen decision. The only purpose that may mutate."""

    @property
    def may_mutate(self) -> bool:
        """Whether a run for this purpose may reach the venue with a mutation."""
        return self is RunPurpose.STRATEGY_EXECUTION


class DecisionCadence(StrEnum):
    """How often a decision purpose comes due."""

    EVERY_SESSION = "EVERY_SESSION"
    MONTHLY_LAST_SESSION = "MONTHLY_LAST_SESSION"
    SESSION_AFTER_MONTHLY_DECISION = "SESSION_AFTER_MONTHLY_DECISION"


@dataclass(frozen=True)
class StrategySlot:
    """One purpose, due in one session, inside one window.

    Attributes:
        schedule: The schedule that produced it.
        session: The market session, ISO date.
        purpose: What the run is for.
        decision_session: For an execution slot, the session whose decision it
            executes. Empty otherwise. Carried so an execution run can find its
            frozen decision without re-deriving which month it belongs to.
    """

    schedule: StrategyOperationsSchedule
    session: str
    purpose: RunPurpose
    decision_session: str = ""

    def run_id(self, *, account_binding_fingerprint: str, mode: OperationsMode) -> str:
        """Derive this slot's run id, including its purpose.

        Args:
            account_binding_fingerprint: Which account.
            mode: Which mode.

        Returns:
            The id. A pure function of the slot's semantic inputs.
        """
        from .schedule import operations_run_id

        return operations_run_id(
            schedule_fingerprint=self.schedule.fingerprint(),
            session=self.session,
            account_binding_fingerprint=account_binding_fingerprint,
            mode=mode,
            purpose=str(self.purpose),
        )

    def due_at(self) -> datetime:
        """When this slot becomes due."""
        return self.schedule.due_at(self.session, self.purpose)

    def window_closes_at(self) -> datetime:
        """When this slot is too late to start."""
        return self.schedule.window_closes_at(self.session, self.purpose)

    def as_json(self) -> dict[str, Any]:
        """Return the slot as a JSON-safe mapping."""
        return {
            "schedule_id": self.schedule.schedule_id,
            "session": self.session,
            "purpose": str(self.purpose),
            "decision_session": self.decision_session,
            "due_at": self.due_at().isoformat(),
            "window_closes_at": self.window_closes_at().isoformat(),
        }


@dataclass(frozen=True)
class StrategyOperationsSchedule:
    """When each operational purpose comes due, and in which session.

    Attributes:
        schedule_id: A stable name.
        timezone: The zone the times of day are expressed in.
        calendar: The sessions the market is open. Explicit, never derived.
        observation_at_local: When an ordinary observation is due, `HH:MM`.
        decision_at_local: When a monthly decision is due. The strategy's own
            decision instant is its session's close, so this must be the close.
        execution_at_local: When an execution is due. Near the open, because the
            frozen research convention executes at the open of the first session
            after the decision.
        observation_window_minutes: How long an observation may start late.
        decision_window_minutes: How long a decision may start late. Short: a
            decision taken an hour after the close is a decision on information
            the close did not contain.
        execution_window_minutes: How long an execution may start late. Bounded
            deliberately — past it the slot is missed and is never backfilled,
            because executing in the afternoon a decision meant for the open is
            a different trade.
        contract: The schedule contract version.
    """

    schedule_id: str
    timezone: str
    calendar: ExecutionCalendar
    observation_at_local: str = "12:00"
    decision_at_local: str = "16:00"
    execution_at_local: str = "09:35"
    observation_window_minutes: int = 120
    decision_window_minutes: int = 30
    execution_window_minutes: int = 25
    contract: str = SCHEDULE_CONTRACT_V1_1

    def __post_init__(self) -> None:
        """Refuse a schedule that could not be evaluated.

        Raises:
            OperationsError: `INVALID_OPERATIONS_SCHEDULE` for an unknown
                timezone, an unreadable time of day, a non-positive window, an
                empty calendar, or an execution time at or after the decision
                time. The last is the one worth checking: an execution slot
                later in the day than the decision slot would let a decision
                execute in its own session, which is the single thing the
                decision/execution split exists to prevent.
        """
        if not self.schedule_id.strip():
            raise OperationsError(Code.INVALID_OPERATIONS_SCHEDULE, "a schedule needs an id")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                f"{self.timezone!r} is not a known timezone. Named explicitly rather "
                "than assumed, because a local time of day is several different "
                "instants across a year",
            ) from exc
        for label, value in (
            ("observation_at_local", self.observation_at_local),
            ("decision_at_local", self.decision_at_local),
            ("execution_at_local", self.execution_at_local),
        ):
            try:
                _parse_time(value)
            except ValueError as exc:
                raise OperationsError(
                    Code.INVALID_OPERATIONS_SCHEDULE,
                    f"{label}={value!r} is not a local time of day as HH:MM",
                ) from exc
        for label, window in (
            ("observation_window_minutes", self.observation_window_minutes),
            ("decision_window_minutes", self.decision_window_minutes),
            ("execution_window_minutes", self.execution_window_minutes),
        ):
            if window <= 0:
                raise OperationsError(
                    Code.INVALID_OPERATIONS_SCHEDULE,
                    f"{label} must be positive, got {window}. A zero window means "
                    "every run is missed; an unbounded one means a run is never too "
                    "late, and yesterday's plan would execute today",
                )
        if not self.calendar.sessions:
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                "a schedule needs an explicit session calendar. A session's successor "
                "is never derived by adding a day: a Friday's is Monday, and a "
                "pre-holiday session's is the day after the holiday",
            )
        if _parse_time(self.execution_at_local) >= _parse_time(self.decision_at_local):
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                f"execution_at_local={self.execution_at_local!r} is not before "
                f"decision_at_local={self.decision_at_local!r}. An execution slot later "
                "in the day than the decision slot would let a month-end decision "
                "execute in its own session, on the very data that produced it",
            )

    def time_of_day(self, purpose: RunPurpose) -> str:
        """The local time a purpose is due."""
        return {
            RunPurpose.SESSION_OBSERVATION: self.observation_at_local,
            RunPurpose.STRATEGY_DECISION: self.decision_at_local,
            RunPurpose.STRATEGY_EXECUTION: self.execution_at_local,
        }[purpose]

    def window_minutes(self, purpose: RunPurpose) -> int:
        """How long a purpose may start late."""
        return {
            RunPurpose.SESSION_OBSERVATION: self.observation_window_minutes,
            RunPurpose.STRATEGY_DECISION: self.decision_window_minutes,
            RunPurpose.STRATEGY_EXECUTION: self.execution_window_minutes,
        }[purpose]

    def due_at(self, session: str, purpose: RunPurpose) -> datetime:
        """When a purpose becomes due in a session.

        Args:
            session: The session, ISO date.
            purpose: The purpose.

        Returns:
            The instant, in this schedule's zone.
        """
        from datetime import date as _date

        clock = _parse_time(self.time_of_day(purpose))
        return datetime.combine(_date.fromisoformat(session), clock, tzinfo=ZoneInfo(self.timezone))

    def window_closes_at(self, session: str, purpose: RunPurpose) -> datetime:
        """When a purpose is too late to start in a session."""
        return self.due_at(session, purpose) + timedelta(minutes=self.window_minutes(purpose))

    def is_decision_session(self, session: str) -> bool:
        """Whether a session is a monthly decision session.

        Read from the calendar, never from weekday arithmetic: a holiday on the
        31st moves the month end, and the rule has to agree with the exchange.

        Args:
            session: The session, ISO date.

        Returns:
            True when it is the last session of its month in this calendar.
        """
        if session not in self.calendar.sessions:
            return False
        month = session[:7]
        return not [row for row in self.calendar.sessions if row[:7] == month and row > session]

    def execution_session_for(self, decision_session: str) -> str | None:
        """The session a decision becomes executable in.

        Args:
            decision_session: The session the decision was taken in.

        Returns:
            The first calendar session strictly after it, or `None` when the
            calendar ends there. `None` is not an error here — the caller
            decides whether a decision with nowhere to execute is refused.
        """
        later = [row for row in self.calendar.sessions if row > decision_session]
        return later[0] if later else None

    def decision_session_for(self, execution_session: str) -> str | None:
        """The decision session an execution session would execute.

        Args:
            execution_session: The candidate execution session.

        Returns:
            The immediately preceding calendar session when that session was a
            monthly decision session; `None` otherwise. Only the *immediately*
            preceding one qualifies, which is what makes a missed execution
            permanently missed rather than quietly eligible a day later.
        """
        earlier = [row for row in self.calendar.sessions if row < execution_session]
        if not earlier:
            return None
        previous = earlier[-1]
        return previous if self.is_decision_session(previous) else None

    def slots_for_session(self, session: str) -> tuple[StrategySlot, ...]:
        """Every slot a session holds, in the order they come due.

        A session can hold two: an execution near the open when it follows a
        decision session, and either a decision at the close when it is a month
        end or an observation otherwise. That is not duplicate scheduling — the
        run ids differ by purpose.

        Args:
            session: The session, ISO date.

        Returns:
            The slots, earliest first. Empty when the session is not a market
            session.
        """
        if session not in self.calendar.sessions:
            return ()
        slots: list[StrategySlot] = []
        decision_session = self.decision_session_for(session)
        if decision_session is not None:
            slots.append(
                StrategySlot(
                    schedule=self,
                    session=session,
                    purpose=RunPurpose.STRATEGY_EXECUTION,
                    decision_session=decision_session,
                )
            )
        if self.is_decision_session(session):
            slots.append(
                StrategySlot(schedule=self, session=session, purpose=RunPurpose.STRATEGY_DECISION)
            )
        else:
            slots.append(
                StrategySlot(schedule=self, session=session, purpose=RunPurpose.SESSION_OBSERVATION)
            )
        return tuple(sorted(slots, key=lambda slot: slot.due_at()))

    def slot_for(self, now: str, purpose: RunPurpose | None = None) -> StrategySlot | None:
        """The slot open at an instant, if any.

        Args:
            now: The operational instant, ISO-8601 with offset.
            purpose: Restrict to one purpose. `None` returns whichever slot is
                open, preferring the earliest still inside its window.

        Returns:
            The open slot, or `None` when nothing is due.
        """
        from .clock import parse_instant

        instant = parse_instant(now)
        session = instant.astimezone(ZoneInfo(self.timezone)).date().isoformat()
        for slot in self.slots_for_session(session):
            if purpose is not None and slot.purpose is not purpose:
                continue
            if slot.due_at() <= instant < slot.window_closes_at():
                return slot
        return None

    def missed_slot_for(self, now: str, purpose: RunPurpose | None = None) -> StrategySlot | None:
        """The most recent slot whose window closed without being taken.

        Args:
            now: The operational instant.
            purpose: Restrict to one purpose.

        Returns:
            The missed slot, or `None`. A missed slot is reported so lateness is
            a recorded fact; it is never run.
        """
        from .clock import parse_instant

        instant = parse_instant(now)
        session = instant.astimezone(ZoneInfo(self.timezone)).date().isoformat()
        closed = [
            slot
            for slot in self.slots_for_session(session)
            if slot.window_closes_at() <= instant and (purpose is None or slot.purpose is purpose)
        ]
        return closed[-1] if closed else None

    def body(self) -> dict[str, Any]:
        """Return the schedule's semantic content."""
        return {
            "contract": self.contract,
            "engine": PAPER_OPERATIONS_ENGINE_VERSION,
            "schedule_id": self.schedule_id,
            "timezone": self.timezone,
            "observation_at_local": self.observation_at_local,
            "decision_at_local": self.decision_at_local,
            "execution_at_local": self.execution_at_local,
            "observation_window_minutes": self.observation_window_minutes,
            "decision_window_minutes": self.decision_window_minutes,
            "execution_window_minutes": self.execution_window_minutes,
            "calendar_fingerprint": self.calendar.fingerprint(),
            "decision_cadence": str(DecisionCadence.MONTHLY_LAST_SESSION),
            "execution_cadence": str(DecisionCadence.SESSION_AFTER_MONTHLY_DECISION),
            "backfill": "NONE — a missed slot is recorded and never run",
        }

    def fingerprint(self) -> str:
        """Digest the schedule."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the schedule with its fingerprint."""
        return {**self.body(), "schedule_fingerprint": self.fingerprint()}


def _parse_time(value: str) -> time:
    """Read an `HH:MM` local time of day.

    Args:
        value: The time of day.

    Returns:
        The time.

    Raises:
        ValueError: If it is not `HH:MM`.
    """
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes))
