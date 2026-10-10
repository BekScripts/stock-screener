"""When a run is due, and why a missed one is never made up.

Two decisions carry the weight here.

**The run id is derived, not generated.** It is a digest of the run's semantic
inputs — the schedule, the session it is for, the account, the mode, the
operations version — so invoking the scheduler twice for the same slot yields
the same id, and the second invocation recovers the first run instead of
starting a competitor. A UUID would make every invocation a new run, which is
the single easiest way to submit the same order twice.

**A missed run is recorded, never backfilled.** If the process starts after the
slot's execution window has closed, the run is `MISSED` and nothing is
submitted. The temptation is obvious and the reason to resist it is simple: an
order planned against yesterday's prices, sizing and risk state is not a late
version of yesterday's decision, it is a new and unreviewed decision wearing
yesterday's authorisation. The window is explicit so that "late" is a fact
rather than a judgement.

Sessions come from an explicit calendar. Never `date + 1`, never a weekday
guess, never a hard-coded holiday list — a Friday slot's successor is Monday,
and arithmetic would schedule a run on a day the market was shut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import parse_instant
from .errors import Code, OperationsError
from .versions import (
    PAPER_OPERATIONS_ENGINE_VERSION,
    RUN_ID_DERIVATION,
    SCHEDULE_CONTRACT,
)

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

    from .models import OperationsMode

__all__ = [
    "PaperOperationsSchedule",
    "ScheduledSlot",
    "operations_run_id",
]


@dataclass(frozen=True)
class PaperOperationsSchedule:
    """When the coordinator should wake up.

    Deliberately holds no strategy. A schedule says *when* to look; what to do
    when looking is decided by the portfolio and risk layers, and a schedule
    that embedded a strategy would make changing one mean re-versioning the
    other.

    Attributes:
        schedule_id: A stable name.
        timezone: The zone the daily time is expressed in. Explicit because
            "16:00" means four different instants across the year otherwise.
        run_at_local: The local time of day a run becomes due, `HH:MM`.
        execution_window_minutes: How long after that time a run may still
            start. Past it the slot is `MISSED`. Explicit so lateness is a fact
            rather than a judgement.
        calendar: The sessions the market is open. An explicit list.
        contract: The schedule contract version.
    """

    schedule_id: str
    timezone: str
    run_at_local: str
    calendar: ExecutionCalendar
    execution_window_minutes: int = 60
    contract: str = SCHEDULE_CONTRACT

    def __post_init__(self) -> None:
        """Refuse a schedule that could not be evaluated.

        Raises:
            OperationsError: `INVALID_OPERATIONS_SCHEDULE` for an unknown
                timezone, an unreadable time of day, a non-positive window, or
                an empty calendar. Each would otherwise surface as a run that
                silently never fires or fires at the wrong instant.
        """
        if not self.schedule_id.strip():
            raise OperationsError(Code.INVALID_OPERATIONS_SCHEDULE, "a schedule needs an id")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                f"{self.timezone!r} is not a known timezone. It is named explicitly "
                "rather than assumed, because a local time of day is four different "
                "instants across a year",
            ) from exc
        try:
            hours, _, minutes = self.run_at_local.partition(":")
            time(int(hours), int(minutes))
        except (TypeError, ValueError) as exc:
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                f"{self.run_at_local!r} is not a local time of day as HH:MM",
            ) from exc
        if self.execution_window_minutes <= 0:
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                f"the execution window must be positive, got "
                f"{self.execution_window_minutes}. A zero window means every run is "
                "missed; an unbounded one means a run is never too late, and yesterday's "
                "plan would execute today",
            )
        if not self.calendar.sessions:
            raise OperationsError(
                Code.INVALID_OPERATIONS_SCHEDULE,
                "a schedule needs an explicit session calendar. Sessions are never "
                "derived by adding a day to a date: a Friday slot's successor is "
                "Monday, and arithmetic would schedule a run on a closed market",
            )

    @property
    def zone(self) -> ZoneInfo:
        """The schedule's timezone."""
        return ZoneInfo(self.timezone)

    def due_at(self, session: str) -> datetime:
        """The instant a session's run becomes due.

        Args:
            session: The session date, ISO.

        Returns:
            The instant, timezone-aware.

        Raises:
            OperationsError: `UNKNOWN_OPERATIONS_SESSION` if the calendar does
                not contain that session. Refused rather than computed — a run
                for a day the market was shut has nothing to execute against.
        """
        if session not in self.calendar.sessions:
            raise OperationsError(
                Code.UNKNOWN_OPERATIONS_SESSION,
                f"{session} is not a session in calendar {self.calendar.name!r}. A "
                "run is never scheduled for a day the calendar does not contain",
            )
        hours, _, minutes = self.run_at_local.partition(":")
        return datetime.combine(
            date.fromisoformat(session), time(int(hours), int(minutes)), tzinfo=self.zone
        )

    def window_closes_at(self, session: str) -> datetime:
        """When a session's run becomes too late to start.

        Args:
            session: The session date.

        Returns:
            The instant the window shuts.
        """
        return self.due_at(session) + timedelta(minutes=self.execution_window_minutes)

    def slot_for(self, now: str) -> ScheduledSlot | None:
        """Find the slot an instant falls in, if any.

        Args:
            now: The operational instant, ISO-8601.

        Returns:
            The slot whose window contains this instant, or `None` when no run
            is due. Looking at the wrong moment is the ordinary case, not an
            error.
        """
        instant = parse_instant(now)
        local_session = instant.astimezone(self.zone).date().isoformat()
        for session in (local_session,):
            if session not in self.calendar.sessions:
                continue
            if self.due_at(session) <= instant <= self.window_closes_at(session):
                return ScheduledSlot(schedule=self, session=session)
        return None

    def missed_slot_for(self, now: str) -> ScheduledSlot | None:
        """Find a slot whose window has closed unrun, if any.

        Args:
            now: The operational instant.

        Returns:
            The slot, or `None`. Reported so a missed run can be *recorded* as
            missed rather than silently forgotten — and never so it can be run
            late.
        """
        instant = parse_instant(now)
        session = instant.astimezone(self.zone).date().isoformat()
        if session not in self.calendar.sessions:
            return None
        if instant > self.window_closes_at(session):
            return ScheduledSlot(schedule=self, session=session)
        return None

    def body(self) -> dict[str, Any]:
        """Return the schedule's semantic content."""
        return {
            "contract": self.contract,
            "schedule_id": self.schedule_id,
            "timezone": self.timezone,
            "run_at_local": self.run_at_local,
            "execution_window_minutes": self.execution_window_minutes,
            "calendar": self.calendar.fingerprint(),
            "calendar_name": self.calendar.name,
            "holds_no_strategy": True,
            "session_derivation": "explicit calendar; never date + 1",
            "backfill": "NONE — a missed slot is recorded, never run late",
        }

    def fingerprint(self) -> str:
        """Digest the schedule."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the schedule with its own fingerprint."""
        return {**self.body(), "schedule_fingerprint": self.fingerprint()}


@dataclass(frozen=True)
class ScheduledSlot:
    """One session's run slot.

    Attributes:
        schedule: The schedule it came from.
        session: The session date.
    """

    schedule: PaperOperationsSchedule
    session: str

    def due_at(self) -> datetime:
        """The instant this slot becomes due.

        A zero-argument accessor on the slot, matching `StrategySlot.due_at`.
        The coordinator reaches a slot's due time through this rather than
        through `slot.schedule.due_at(slot.session)`, because the two schedule
        types do not share that signature: the V1.1 strategy schedule needs a
        purpose as well as a session, so the longer form type-errors on every
        run the production CLI makes.
        """
        return self.schedule.due_at(self.session)

    def run_id(self, *, account_binding_fingerprint: str, mode: OperationsMode) -> str:
        """Derive this slot's run id.

        Args:
            account_binding_fingerprint: Which account the run acts on.
            mode: The mode the run would operate in.

        Returns:
            The id. A pure function of the slot's semantic inputs, so two
            invocations for the same slot produce the same id and the second
            recovers the first run rather than starting another.
        """
        return operations_run_id(
            schedule_fingerprint=self.schedule.fingerprint(),
            session=self.session,
            account_binding_fingerprint=account_binding_fingerprint,
            mode=mode,
        )

    def as_json(self) -> dict[str, Any]:
        """Return the slot as a JSON-safe mapping."""
        return {
            "schedule_id": self.schedule.schedule_id,
            "session": self.session,
            "due_at": self.schedule.due_at(self.session).isoformat(),
            "window_closes_at": self.schedule.window_closes_at(self.session).isoformat(),
        }


def operations_run_id(
    *,
    schedule_fingerprint: str,
    session: str,
    account_binding_fingerprint: str,
    mode: OperationsMode,
    purpose: str | None = None,
) -> str:
    """Derive an operations run id from a slot's semantic inputs.

    Pure and total, and deliberately free of any clock. A run id that moved with
    the wall clock would make a retry a different run, and two runs for one slot
    is how one scheduled decision becomes two orders.

    Args:
        schedule_fingerprint: Which schedule.
        session: Which session.
        account_binding_fingerprint: Which account.
        mode: Which mode. Part of the identity because an observe-only run and a
            paper-executing run for the same slot are different runs and must
            not recover each other.
        purpose: What the run is for, when the caller distinguishes purposes.
            Added for `PAPER_OPERATIONS_SCHEDULE_V1_1`, where one session can
            legitimately hold more than one operational event — an execution
            near the open, an observation, a decision at the close — and those
            must not recover each other.

            `None` reproduces the V1 id exactly, byte for byte: the key is
            absent from the digested body rather than present and empty, so a
            V1 caller's run ids do not move because V1.1 exists. That is the
            whole of the additive guarantee, and
            `test_a_v1_run_id_is_unchanged_by_the_purpose_parameter` is what
            keeps it true.

    Returns:
        The run id.
    """
    import hashlib

    from stock_screener.execution import fingerprint_of

    body: dict[str, Any] = {
        "derivation": RUN_ID_DERIVATION,
        "engine": PAPER_OPERATIONS_ENGINE_VERSION,
        "schedule": schedule_fingerprint,
        "session": session,
        "account": account_binding_fingerprint,
        "mode": str(mode),
    }
    if purpose is not None:
        body["purpose"] = str(purpose)
    digest = hashlib.sha256(fingerprint_of(body).encode()).hexdigest()
    return f"opsrun-{digest[:32]}"
