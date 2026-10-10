"""Rendering what is due next, for an operator reading a status page.

Separate from the schedule itself because a schedule answers "is a slot open
now", and an operator wants "what happens next and when" — a different question
that needs no new semantics, only a projection forward through the calendar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stock_screener.paper_operations.strategy_schedule import (
        StrategyOperationsSchedule,
    )

__all__ = ["next_slots"]


def next_slots(
    *, schedule: StrategyOperationsSchedule, now: str, limit: int = 4
) -> tuple[str, ...]:
    """Describe the next few operational slots.

    Args:
        schedule: The schedule.
        now: The operational instant, ISO-8601 with offset.
        limit: How many slots to describe.

    Returns:
        One line per slot, earliest first. Empty when the calendar has run out,
        which is itself worth seeing on a status page: a runtime whose calendar
        ends has no further slots and will silently do nothing.
    """
    from stock_screener.paper_operations.clock import parse_instant

    instant = parse_instant(now)
    lines: list[str] = []
    for session in schedule.calendar.sessions:
        for slot in schedule.slots_for_session(session):
            if slot.window_closes_at() <= instant:
                continue
            detail = (
                f" (executes the {slot.decision_session} decision)" if slot.decision_session else ""
            )
            lines.append(f"{slot.due_at().isoformat()}  {slot.purpose}{detail}")
            if len(lines) >= limit:
                return tuple(lines)
    return tuple(lines)
