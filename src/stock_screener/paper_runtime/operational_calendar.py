"""The forward operational market calendar — an operations artefact, not research.

The research benchmark calendar is built from stored price bars, so it can only
describe sessions that already happened and it ends wherever ingestion stopped.
That is correct for research and useless for scheduling: a soak needs to know
when the next twenty sessions are, and the answer cannot come from data that
does not exist yet.

The temptation was to run `update-benchmark` so the benchmark calendar reached
far enough. That would have mutated a research artefact to solve an operations
problem, and the research record is frozen for reasons that have nothing to do
with scheduling convenience. So this is a separate, explicitly declared,
operations-owned calendar.

It carries what a schedule actually needs and the benchmark calendar never had:
the **real close time of each session**. Two sessions in this range close at
13:00 ET rather than 16:00, and a calendar that recorded only session dates
would let a scheduler wait at 15:45 for a market that shut two and three-quarter
hours earlier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

__all__ = [
    "DECLARED_CLOSURES",
    "EARLY_CLOSES",
    "OPERATIONAL_CALENDAR_VERSION",
    "REGULAR_CLOSE",
    "REGULAR_OPEN",
    "SESSION_TIMEZONE",
    "OperationalSession",
    "operational_calendar",
    "operational_sessions",
    "session_calendar",
]

#: The calendar contract. Versioned by range, because extending it is a new
#: declaration rather than an edit — a frozen soak's calendar must never move.
OPERATIONAL_CALENDAR_VERSION = "US_EQUITY_OPERATIONAL_CALENDAR_2026Q4_2027Q1_V1"

#: The exchange's zone. Named, never a fixed offset: the close moves with
#: daylight saving and a fixed -5 would be an hour wrong for most of the year.
SESSION_TIMEZONE = "America/New_York"

#: A regular session.
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

#: Full closures in range, derived from the published NYSE/Nasdaq rules and
#: named so each exclusion is checkable rather than implied by a gap:
#: Thanksgiving is the fourth Thursday of November; Martin Luther King Jr. Day
#: and Washington's Birthday are third Mondays; Good Friday is two days before
#: Easter, which falls on 2027-03-28. None of the fixed-date holidays in this
#: range lands on a weekend, so no observed-date shift applies.
DECLARED_CLOSURES: dict[str, str] = {
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
}

#: Early closes in range, at 13:00 ET. These are **open sessions** — they are
#: eligible, they count, and the only thing that differs is when they end.
EARLY_CLOSES: dict[str, str] = {
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
}

#: The declared range.
RANGE_START = "2026-10-12"
RANGE_END = "2027-03-31"

#: Where the holiday and early-close rules come from.
PROVENANCE = (
    "NYSE and Nasdaq published U.S. equity market holiday and early-close "
    "schedules for 2026 and 2027. Fixed-date holidays are listed on their "
    "observed dates; floating holidays are derived from their published rules "
    "(fourth Thursday of November; third Mondays in January and February; Good "
    "Friday two days before Easter 2027-03-28) and each derivation is checked "
    "in test_p2_4_v2_session_soak.py rather than asserted here."
)


@dataclass(frozen=True)
class OperationalSession:
    """One market session, with the times a scheduler actually needs.

    Attributes:
        session: The ISO date.
        opens_at_local: When trading opens, `HH:MM` in `SESSION_TIMEZONE`.
        closes_at_local: When it closes. 13:00 on an early close.
        early_close: Whether this session ends before the regular close.
        note: Why it is early, where it is.
    """

    session: str
    opens_at_local: str = "09:30"
    closes_at_local: str = "16:00"
    early_close: bool = False
    note: str = ""

    def opens_at(self) -> datetime:
        """The opening instant."""
        return self._instant(self.opens_at_local)

    def closes_at(self) -> datetime:
        """The closing instant. This is the decision instant for a month end."""
        return self._instant(self.closes_at_local)

    def _instant(self, clock: str) -> datetime:
        """Combine this session's date with a local time of day."""
        hours, _, minutes = clock.partition(":")
        return datetime.combine(
            date.fromisoformat(self.session),
            time(int(hours), int(minutes)),
            tzinfo=ZoneInfo(SESSION_TIMEZONE),
        )

    def body(self) -> dict[str, Any]:
        """Return the session's semantic content."""
        return {
            "session": self.session,
            "timezone": SESSION_TIMEZONE,
            "opens_at_local": self.opens_at_local,
            "closes_at_local": self.closes_at_local,
            "early_close": self.early_close,
            "note": self.note,
        }


def operational_sessions() -> tuple[OperationalSession, ...]:
    """Build every declared session in range, ascending.

    Weekdays minus `DECLARED_CLOSURES`, with `EARLY_CLOSES` carrying a 13:00
    close. Derived from the named rules rather than typed out, so a reader can
    check the rules instead of proof-reading a list of dates — and the
    derivation itself is tested.

    Returns:
        The sessions.
    """
    sessions: list[OperationalSession] = []
    day = date.fromisoformat(RANGE_START)
    end = date.fromisoformat(RANGE_END)
    while day <= end:
        iso = day.isoformat()
        if day.weekday() < 5 and iso not in DECLARED_CLOSURES:
            early = iso in EARLY_CLOSES
            sessions.append(
                OperationalSession(
                    session=iso,
                    closes_at_local="13:00" if early else "16:00",
                    early_close=early,
                    note=EARLY_CLOSES.get(iso, ""),
                )
            )
        day += timedelta(days=1)
    return tuple(sessions)


def session_calendar() -> dict[str, OperationalSession]:
    """Every declared session, by date."""
    return {row.session: row for row in operational_sessions()}


def operational_calendar() -> ExecutionCalendar:
    """Build the `ExecutionCalendar` the execution and operations layers consume.

    Returns:
        The calendar over the declared sessions. The session *times* live in
        `OperationalSession`; `ExecutionCalendar` is a membership list and is
        deliberately left that way rather than widened, so nothing in the
        frozen execution layer changes because operations learned about early
        closes.
    """
    from stock_screener.execution import ExecutionCalendar

    return ExecutionCalendar(
        sessions=tuple(row.session for row in operational_sessions()),
        name=OPERATIONAL_CALENDAR_VERSION,
    )


def calendar_body() -> dict[str, Any]:
    """Return the whole calendar's semantic content, for fingerprinting."""
    return {
        "version": OPERATIONAL_CALENDAR_VERSION,
        "kind": "OPERATIONS_ARTEFACT_NOT_RESEARCH",
        "timezone": SESSION_TIMEZONE,
        "range_start": RANGE_START,
        "range_end": RANGE_END,
        "regular_open": REGULAR_OPEN.strftime("%H:%M"),
        "regular_close": REGULAR_CLOSE.strftime("%H:%M"),
        "declared_closures": dict(sorted(DECLARED_CLOSURES.items())),
        "early_closes": dict(sorted(EARLY_CLOSES.items())),
        "provenance": PROVENANCE,
        "sessions": [row.body() for row in operational_sessions()],
        "derived_from_weekday_arithmetic": False,
        "benchmark_calendar_used": False,
    }


def calendar_fingerprint() -> str:
    """Digest the declared calendar."""
    from stock_screener.execution import fingerprint_of

    return fingerprint_of(calendar_body())
