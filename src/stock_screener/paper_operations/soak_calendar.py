"""The declared session calendar for the armed paper shadow soak.

`ExecutionCalendar` takes an explicit list of sessions and never derives one,
for the reason the execution engine already gives: a calendar computed at read
time changes when the computation changes, and a soak scored against a moving
denominator is not scored at all. So the sessions the soak will be judged on are
written down here, before the first of them, and the calendar's fingerprint goes
into the frozen protocol.

These are **forward** sessions, which is why they cannot come from
`market_sessions` — that reads stored benchmark bars, so it can only describe
sessions that already happened. A soak is prospective by construction and needs
the calendar before the data exists.

Two things are therefore true and worth stating plainly rather than burying:

- **This list is a declaration, not an observation.** It is weekdays minus the
  full closures named in `DECLARED_CLOSURES`. If the exchange closes
  unexpectedly — weather, an outage, a national day of mourning — a session
  listed here will not happen, and the soak will show it as an eligible session
  with no record. That blocks promotion until someone looks, which is the
  correct outcome: a missing session should require a human to say why.
- **The two early closes are excluded, and that is a schedule decision rather
  than a calendar one.** 2026-11-27 and 2026-12-24 are real sessions — the
  market is open — but it shuts at 13:00 ET, and the soak's declared run time is
  15:45 ET, fifteen minutes before a regular close. Keeping them would schedule
  a run into a market that closed nearly three hours earlier, on two of the
  twenty sessions the protocol is scored on, with no way to correct it once the
  protocol is frozen. The alternative was to move the run time before 13:00 on
  every day, which would give up the near-close timing that makes the rest of
  the evidence worth collecting. So they are named in `EARLY_CLOSES`, left out
  of `SOAK_SESSIONS`, and the exclusion is a stated fact instead of a silent
  hazard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar

__all__ = [
    "DECLARED_CLOSURES",
    "EARLY_CLOSES",
    "SOAK_CALENDAR_NAME",
    "SOAK_SESSIONS",
    "soak_calendar",
]

SOAK_CALENDAR_NAME = "US_EQUITY_DECLARED_2026Q4_2027Q1"

#: Full closures inside the declared range. Named so the exclusions are
#: checkable rather than implied by a gap in the list.
DECLARED_CLOSURES: dict[str, str] = {
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
}

#: Early closes. The market is open on these days, so they are not closures —
#: they are excluded from `SOAK_SESSIONS` because the declared 15:45 ET run time
#: falls after their 13:00 ET close. Named rather than merely absent, so the
#: exclusion is checkable.
EARLY_CLOSES: dict[str, str] = {
    "2026-11-27": "day after Thanksgiving, 13:00 ET close",
    "2026-12-24": "Christmas Eve, 13:00 ET close",
}

#: Every session the soak may be judged on, ascending. Declared before the
#: first of them. Weekdays, minus `DECLARED_CLOSURES` and minus `EARLY_CLOSES`.
#: 76 sessions, well beyond the 20 required: a longer calendar cannot shorten
#: the soak, and a short one could strand it.
SOAK_SESSIONS: tuple[str, ...] = (
    "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
    "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
    "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02",
    "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10",
    "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18",
    "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-30",
    "2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04", "2026-12-07", "2026-12-08",
    "2026-12-09", "2026-12-10", "2026-12-11", "2026-12-14", "2026-12-15", "2026-12-16",
    "2026-12-17", "2026-12-18", "2026-12-21", "2026-12-22", "2026-12-23", "2026-12-28",
    "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-04", "2027-01-05", "2027-01-06",
    "2027-01-07", "2027-01-08", "2027-01-11", "2027-01-12", "2027-01-13", "2027-01-14",
    "2027-01-15", "2027-01-19", "2027-01-20", "2027-01-21", "2027-01-22", "2027-01-25",
    "2027-01-26", "2027-01-27", "2027-01-28", "2027-01-29",
)  # fmt: skip


def soak_calendar() -> ExecutionCalendar:
    """Build the declared soak calendar.

    Returns:
        The calendar over `SOAK_SESSIONS`. `ExecutionCalendar` validates that
        the sessions are ISO dates, ascending and unique, so a typo here fails
        at construction rather than at scoring time.
    """
    from stock_screener.execution import ExecutionCalendar

    return ExecutionCalendar(sessions=SOAK_SESSIONS, name=SOAK_CALENDAR_NAME)
