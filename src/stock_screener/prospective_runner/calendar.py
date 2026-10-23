"""A frozen NYSE session calendar for dates the database does not contain.

Historically a benchmark session is a row in `benchmark_prices`. That table
stops where the frozen dataset stops, so a prospective runner cannot ask it when
the next quarter ends. The rules are therefore written out and frozen, because
the alternative — assuming a quarter ends on the last calendar day, or hardcoding
a date — is exactly what the protocol forbids.

The rules are checkable rather than asserted: `verify_against_history` replays
this calendar over the frozen benchmark sessions and reports any disagreement.
A calendar that cannot reproduce the sessions the research already used is not
one a prospective observation should be taken under.

Scope is deliberately narrow. Good Friday and the moveable holidays are computed
from rules; the irregular closures history produces without warning — a national
day of mourning, a hurricane — cannot be, so `KNOWN_IRREGULAR_CLOSURES` records
the ones already known and `irregular_closure_risk` states plainly that a future
one would be caught only by the session's absence from the price source.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CALENDAR_ID",
    "EARLY_CLOSE_TIME",
    "KNOWN_IRREGULAR_CLOSURES",
    "REGULAR_CLOSE_TIME",
    "SESSION_TIMEZONE",
    "calendar_descriptor",
    "early_close",
    "holidays",
    "is_session",
    "next_session",
    "previous_session",
    "quarter_end_session",
    "sessions_between",
    "upcoming_quarter_ends",
    "verify_against_history",
]

#: The calendar's frozen identity, recorded in the activation record.
CALENDAR_ID = "NYSE_SESSION_CALENDAR_V1"

#: Every session time this project cares about is New York local.
SESSION_TIMEZONE = "America/New_York"

#: The regular close, and the decision instant the protocol fixes.
REGULAR_CLOSE_TIME = "16:00"

#: Half-day close. It changes when the close happens, never whether the day is
#: a session, so it does not affect quarter-end derivation.
EARLY_CLOSE_TIME = "13:00"

#: Unscheduled closures that no rule predicts. Recorded so the historical replay
#: is honest; a future one is a data event, not a calendar event.
KNOWN_IRREGULAR_CLOSURES: tuple[date, ...] = (
    date(2012, 10, 29),  # Hurricane Sandy
    date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),  # National day of mourning, George H. W. Bush
    date(2025, 1, 9),  # National day of mourning, Jimmy Carter
)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The nth occurrence of a weekday in a month. Monday is 0."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last occurrence of a weekday in a month."""
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Western Easter Sunday, by the anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - the algorithm's own name
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def _observed(day: date) -> date | None:
    """Move a fixed-date holiday to the observed weekday, or drop it.

    A Saturday holiday is observed the Friday before and a Sunday holiday the
    Monday after — except that a Saturday New Year's Day is not observed at all,
    which is the one case the general rule gets wrong.
    """
    if day.weekday() == 5:
        if day.month == 1 and day.day == 1:
            return None
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> set[date]:
    """Every full-day NYSE closure in a year, by rule.

    Args:
        year: The calendar year.

    Returns:
        The closure dates. Juneteenth appears from 2022, the first year the
        exchange observed it.
    """
    days: list[date | None] = [
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    ]
    if year >= 2022:
        days.append(_observed(date(year, 6, 19)))  # Juneteenth
    return {day for day in days if day is not None}


def early_close(day: date) -> bool:
    """Whether a session closes early, at 13:00 rather than 16:00.

    Args:
        day: The session.

    Returns:
        True for the day after Thanksgiving, and for Christmas Eve and the day
        before Independence Day when those fall on a session.
    """
    if not is_session(day):
        return False
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    if day == thanksgiving + timedelta(days=1):
        return True
    for eve in (date(day.year, 12, 24), date(day.year, 7, 3)):
        if day == eve and is_session(eve):
            return True
    return False


def is_session(day: date) -> bool:
    """Whether a date is a regular trading session.

    Args:
        day: The date.

    Returns:
        True when it is a weekday, not a holiday, and not a known closure.
    """
    return (
        day.weekday() < 5 and day not in holidays(day.year) and day not in KNOWN_IRREGULAR_CLOSURES
    )


def next_session(after: date) -> date:
    """The first session strictly after a date.

    Args:
        after: The date to start from, exclusive.

    Returns:
        The next session.
    """
    day = after + timedelta(days=1)
    while not is_session(day):
        day += timedelta(days=1)
    return day


def previous_session(before: date) -> date:
    """The last session strictly before a date.

    Args:
        before: The date to start from, exclusive.

    Returns:
        The previous session.
    """
    day = before - timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day


def sessions_between(first: date, last: date) -> list[date]:
    """Every session in a closed interval, in order.

    Args:
        first: The first date, inclusive.
        last: The last date, inclusive.

    Returns:
        The sessions.
    """
    days: list[date] = []
    day = first
    while day <= last:
        if is_session(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def quarter_end_session(year: int, quarter: int) -> date:
    """The last session of a calendar quarter.

    Args:
        year: The calendar year.
        quarter: 1 through 4.

    Returns:
        The quarter's final trading session.

    Raises:
        ValueError: If the quarter is not 1 through 4.
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1 through 4, got {quarter}")
    month = quarter * 3
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return last if is_session(last) else previous_session(last)


def upcoming_quarter_ends(after: date, count: int = 8) -> list[date]:
    """The next quarter-end sessions strictly after a date.

    Args:
        after: The date to start from, exclusive.
        count: How many to return.

    Returns:
        Quarter-end sessions in order.
    """
    found: list[date] = []
    year, quarter = after.year, (after.month - 1) // 3 + 1
    while len(found) < count:
        session = quarter_end_session(year, quarter)
        if session > after:
            found.append(session)
        quarter += 1
        if quarter == 5:
            quarter, year = 1, year + 1
    return found


def verify_against_history(sessions: Sequence[date]) -> dict[str, Any]:
    """Replay this calendar over sessions the frozen dataset already holds.

    Args:
        sessions: The benchmark sessions the research used, in order.

    Returns:
        The comparison, listing any date this calendar would have added or
        dropped. A calendar that cannot reproduce the record is not one to take
        a prospective observation under.
    """
    if not sessions:
        return {"checked": 0, "agrees": False, "reason": "no sessions supplied"}
    observed = set(sessions)
    derived = set(sessions_between(sessions[0], sessions[-1]))
    missing = sorted(observed - derived)
    extra = sorted(derived - observed)
    return {
        "checked": len(observed),
        "first": sessions[0].isoformat(),
        "last": sessions[-1].isoformat(),
        "calendar_would_drop": [day.isoformat() for day in missing],
        "calendar_would_add": [day.isoformat() for day in extra],
        "agrees": not missing and not extra,
    }


def calendar_descriptor() -> dict[str, Any]:
    """The frozen calendar rules, for the activation record.

    Returns:
        A JSON-safe description of every rule this calendar applies.
    """
    return {
        "calendar_id": CALENDAR_ID,
        "venue": "NYSE / NASDAQ regular session calendar",
        "timezone": SESSION_TIMEZONE,
        "session_definition": (
            "a weekday that is not a listed holiday and not a known irregular closure"
        ),
        "regular_close": REGULAR_CLOSE_TIME,
        "early_close": EARLY_CLOSE_TIME,
        "decision_instant": f"{REGULAR_CLOSE_TIME} {SESSION_TIMEZONE}",
        "holiday_rules": [
            "New Year's Day, observed; a Saturday New Year's Day is not observed",
            "Martin Luther King Jr Day — third Monday of January",
            "Washington's Birthday — third Monday of February",
            "Good Friday — two days before Western Easter",
            "Memorial Day — last Monday of May",
            "Juneteenth, observed, from 2022",
            "Independence Day, observed",
            "Labor Day — first Monday of September",
            "Thanksgiving — fourth Thursday of November",
            "Christmas Day, observed",
        ],
        "observance_rule": (
            "a Saturday holiday is observed the preceding Friday and a Sunday "
            "holiday the following Monday, except Saturday New Year's Day"
        ),
        "early_close_rule": (
            "the day after Thanksgiving, and Christmas Eve and 3 July when those "
            "fall on a session. An early close changes when a session ends, never "
            "whether it is a session, so it does not move a quarter end."
        ),
        "quarter_end_rule": ("the last session on or before the final calendar day of the quarter"),
        "known_irregular_closures": [day.isoformat() for day in KNOWN_IRREGULAR_CLOSURES],
        "irregular_closure_risk": (
            "an unscheduled future closure cannot be derived from any rule. It "
            "would show up as an absent session in the price source, and the "
            "runner must fail closed on a decision it cannot price rather than "
            "assume the session occurred."
        ),
        "verified_against": "the frozen benchmark sessions in the research dataset",
    }
