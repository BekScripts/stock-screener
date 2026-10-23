"""Which session follows which, from an explicit list of sessions.

A residual that did not fill today rolls to the *next session*, and the next
session is never `date + 1`. A Friday residual belongs to Monday, a residual
before a holiday belongs to the day after it, and arithmetic on dates gets both
wrong — it would place a child order on a day the market was shut, fill it
against a snapshot that cannot exist, and produce a plausible result from an
impossible trade.

So the calendar is supplied, fingerprinted, and asked. A session it does not
contain is `UNKNOWN_EXECUTION_SESSION` rather than interpolated.

This is deliberately a small, self-contained list rather than a reference to the
research layer's trading calendar. The execution engine must not import the H2 or
prospective modules: those are a frozen research apparatus bound to a sealed
window, and an execution dependency on them would couple a live trading path to
artefacts that exist to be immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["CALENDAR_CONTRACT", "ExecutionCalendar"]

#: The calendar contract.
CALENDAR_CONTRACT = "EXECUTION_SESSION_CALENDAR_V1"


@dataclass(frozen=True)
class ExecutionCalendar:
    """An explicit ordered list of market sessions.

    Attributes:
        sessions: ISO dates, in order, each a session the market was open.
        name: What this calendar is.
    """

    sessions: tuple[str, ...]
    name: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        """Validate and canonicalise the sessions.

        Raises:
            ExecutionError: `UNKNOWN_EXECUTION_SESSION` on an empty calendar, a
                non-ISO date, or a duplicate. An empty calendar would make every
                rollover impossible, and a duplicate would make "the next
                session" ambiguous.
        """
        if not self.sessions:
            raise ExecutionError(
                Code.UNKNOWN_EXECUTION_SESSION,
                "an execution calendar must contain at least one session; an empty "
                "one makes every rollover impossible rather than merely bounded",
            )
        parsed: list[date] = []
        for value in self.sessions:
            try:
                parsed.append(date.fromisoformat(value))
            except ValueError as exc:
                raise ExecutionError(
                    Code.UNKNOWN_EXECUTION_SESSION,
                    f"{value!r} is not an ISO date",
                ) from exc
        if len(set(parsed)) != len(parsed):
            raise ExecutionError(
                Code.UNKNOWN_EXECUTION_SESSION,
                "an execution calendar may not repeat a session; a duplicate makes "
                "'the next session' ambiguous",
            )
        object.__setattr__(self, "sessions", tuple(row.isoformat() for row in sorted(parsed)))

    def __contains__(self, session: str) -> bool:
        """Whether the calendar holds a session."""
        return session in self.sessions

    def index_of(self, session: str) -> int:
        """Where a session sits in the calendar.

        Args:
            session: The session.

        Returns:
            Its index.

        Raises:
            ExecutionError: `UNKNOWN_EXECUTION_SESSION` if absent. An unknown
                session is refused rather than placed by arithmetic: a child
                order on a day the market was shut would fill against a snapshot
                that cannot exist.
        """
        try:
            return self.sessions.index(session)
        except ValueError as exc:
            raise ExecutionError(
                Code.UNKNOWN_EXECUTION_SESSION,
                f"{session} is not in calendar {self.name!r}. The next session is "
                "never computed as date + 1: a Friday residual belongs to Monday "
                "and a pre-holiday residual to the day after the holiday, and "
                "arithmetic gets both wrong.",
            ) from exc

    def next_session(self, session: str) -> str | None:
        """The session after one, or None at the end of the calendar.

        Args:
            session: The current session.

        Returns:
            The next session, or None when the calendar has no more. None is a
            real answer — a residual with nowhere to roll stops rather than
            inventing a date.

        Raises:
            ExecutionError: `UNKNOWN_EXECUTION_SESSION` if the session is absent.
        """
        index = self.index_of(session)
        if index + 1 >= len(self.sessions):
            return None
        return self.sessions[index + 1]

    def sessions_from(self, session: str, *, limit: int) -> tuple[str, ...]:
        """Up to `limit` sessions starting at one.

        Args:
            session: The first session.
            limit: How many at most.

        Returns:
            The sessions available, which may be fewer than asked for.
        """
        index = self.index_of(session)
        return self.sessions[index : index + limit]

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "contract": CALENDAR_CONTRACT,
            "name": self.name,
            "session_count": len(self.sessions),
            "first": self.sessions[0],
            "last": self.sessions[-1],
            "sessions": list(self.sessions),
        }

    def fingerprint(self) -> str:
        """The calendar's deterministic digest."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the calendar as a JSON-safe mapping."""
        return {**self.body(), "calendar_fingerprint": self.fingerprint()}

    @classmethod
    def of(cls, sessions: Sequence[str], *, name: str = "UNSPECIFIED") -> ExecutionCalendar:
        """Build a calendar from a sequence of sessions.

        Args:
            sessions: ISO dates.
            name: What this calendar is.

        Returns:
            The calendar.
        """
        return cls(sessions=tuple(sessions), name=name)
