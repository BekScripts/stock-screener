"""The quarter-end decision calendar, derived rather than assumed.

A quarter-end signal is the last benchmark session of a calendar quarter. That
session is also, necessarily, the last benchmark session of its month — so
every quarterly decision date is one of R1.0's monthly decision dates, and this
module proves that rather than relying on it.

The consequence is worth stating, because it is what makes the comparison in
R1.2 honest: the quarterly cross-sections are a **subset of R1.0's frozen
cross-sections**, not a recomputation of them. Identity set, score, tradability
verdict and execution price at each quarter end are the same bytes R1.0 froze.
Only the rebalance frequency differs, which is the single thing the hypothesis
is about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

__all__ = [
    "QuarterEnd",
    "QuarterlyCalendarError",
    "quarter_of",
    "quarterly_calendar",
    "terminal_session",
]


class QuarterlyCalendarError(RuntimeError):
    """The frozen record cannot supply a quarterly calendar R1.2 can stand on."""


def quarter_of(when: date) -> tuple[int, int]:
    """The `(year, quarter)` a date falls in."""
    return (when.year, (when.month - 1) // 3 + 1)


@dataclass(frozen=True)
class QuarterEnd:
    """One quarterly decision, and the session that executes it.

    Attributes:
        quarter: `YYYYQn`.
        month: The calendar month the decision falls in, `YYYY-MM`.
        decision: The last benchmark session of the quarter.
        execution: The first benchmark session strictly after it.
        split: The R1.0 block the decision month belongs to.
    """

    quarter: str
    month: str
    decision: date
    execution: date
    split: str


def quarterly_calendar(db: sqlite3.Connection, monthly: Mapping[str, str]) -> list[QuarterEnd]:
    """Derive the quarter-end decisions inside R1.0's usable window.

    Args:
        db: The research database, opened read-only.
        monthly: `{decision ISO date: block}` from R1.0's frozen split. A
            quarter end outside this mapping is outside the window R1.0
            established as usable for both identity classes, and is dropped.

    Returns:
        The quarterly decisions, in order.

    Raises:
        QuarterlyCalendarError: If a derived quarter end is not one of R1.0's
            decision dates, or if none survive.
    """
    sessions = [
        date.fromisoformat(str(row[0]))
        for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
    ]
    if not sessions:
        raise QuarterlyCalendarError("the benchmark series is empty")
    successor = dict(pairwise(sessions))
    last_of_quarter: dict[tuple[int, int], date] = {}
    for session in sessions:
        last_of_quarter[quarter_of(session)] = session

    out: list[QuarterEnd] = []
    for (year, quarter), session in sorted(last_of_quarter.items()):
        block = monthly.get(session.isoformat())
        if block is None:
            continue
        if session not in successor:
            continue
        out.append(
            QuarterEnd(
                quarter=f"{year}Q{quarter}",
                month=session.isoformat()[:7],
                decision=session,
                execution=successor[session],
                split=block,
            )
        )
    if not out:
        raise QuarterlyCalendarError("no quarter end falls inside the frozen research window")
    return out


def terminal_session(db: sqlite3.Connection, last: date, not_after: date) -> date:
    """The final valuation session: the month end after the last decision.

    The same rule R1.0 used, so the two paths end on the same session and a
    matched-window comparison is a comparison of the same days.

    Args:
        db: The research database, opened read-only.
        last: The final decision session.
        not_after: R1.0's own final valuation session, which this must not pass.

    Returns:
        The final valuation session.
    """
    year, month = (last.year + 1, 1) if last.month == 12 else (last.year, last.month + 1)
    row = db.execute(
        "SELECT max(date) FROM benchmark_prices WHERE substr(date,1,7) = ?",
        (f"{year:04d}-{month:02d}",),
    ).fetchone()
    candidate = date.fromisoformat(str(row[0])) if row and row[0] else not_after
    return min(candidate, not_after)


def sessions_between(db: sqlite3.Connection, first: date, last: date) -> list[date]:
    """Every benchmark session in a closed interval, in order.

    Args:
        db: The research database, opened read-only.
        first: The first session.
        last: The last session.

    Returns:
        The sessions.
    """
    return [
        date.fromisoformat(str(row[0]))
        for row in db.execute(
            "SELECT date FROM benchmark_prices WHERE date >= ? AND date <= ? ORDER BY date",
            (first.isoformat(), last.isoformat()),
        )
    ]


def quarters_by_block(calendar: Sequence[QuarterEnd]) -> dict[str, int]:
    """How many quarterly decisions fall in each R1.0 block."""
    counts: dict[str, int] = {}
    for entry in calendar:
        counts[entry.split] = counts.get(entry.split, 0) + 1
    return dict(sorted(counts.items()))
