"""Slots that closed without being taken, recorded and never run late.

A missed slot is not a failure to be retried. It is a fact, and the only safe
thing to do with it is write it down.

The two cases differ in what the temptation would be:

- **A missed decision** tempts you to take it the next morning. That decision
  would be made on information the month-end close did not contain — a night of
  news, a gap open — and would be a different decision wearing the month's
  authority. There is no mechanism here to create one late.
- **A missed execution** tempts you to fill it that afternoon, or the next day.
  That is a different trade: the decision was sized against an opening-session
  expectation and the afternoon is not that. Worse, a pending decision left
  sitting in `PENDING_EXECUTION` is a loaded gun — the next scheduler tick that
  finds it would submit it. So a missed execution moves the decision to a
  terminal state, actively, rather than being noticed later.

Both records are permanent. Neither is backfilled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .decisions import DecisionStatus, StrategyDecisionStore

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.paper_operations.strategy_schedule import (
        StrategyOperationsSchedule,
    )

__all__ = [
    "MISSED_DIRECTORY",
    "MISSED_SLOT_VERSION",
    "MissedSlotRecord",
    "expire_pending_executions",
    "missed_records",
    "record_missed_slot",
]

#: The missed-slot contract.
MISSED_SLOT_VERSION = "MISSED_OPERATIONAL_SLOT_V1"

#: Where missed slots are recorded, under the runtime namespace.
MISSED_DIRECTORY = "missed"


@dataclass(frozen=True)
class MissedSlotRecord:
    """One slot that closed without being taken.

    Attributes:
        session: The market session.
        purpose: What the slot was for.
        reason_code: `MISSED_STRATEGY_DECISION` or `MISSED_STRATEGY_EXECUTION`.
        due_at: When the slot opened.
        window_closed_at: When it became too late.
        observed_at: When the miss was noticed.
        decision_session: For a missed execution, whose decision was lost.
        decision_fingerprint: That decision's digest.
        detail: A sanitised note.
    """

    session: str
    purpose: str
    reason_code: str
    due_at: str
    window_closed_at: str
    observed_at: str
    decision_session: str = ""
    decision_fingerprint: str = ""
    detail: str = ""
    backfilled: bool = field(default=False, init=False)

    def body(self) -> dict[str, Any]:
        """Return the record's semantic content."""
        return {
            "version": MISSED_SLOT_VERSION,
            "session": self.session,
            "purpose": self.purpose,
            "reason_code": self.reason_code,
            "due_at": self.due_at,
            "window_closed_at": self.window_closed_at,
            "observed_at": self.observed_at,
            "decision_session": self.decision_session,
            "decision_fingerprint": self.decision_fingerprint,
            "detail": self.detail,
            "backfilled": self.backfilled,
            "recoverable": False,
        }

    def fingerprint(self) -> str:
        """Digest the record."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the record with its fingerprint."""
        return {**self.body(), "record_fingerprint": self.fingerprint()}


def _directory(root: Path) -> Path:
    """Where missed slots live."""
    from .binding import RUNTIME_DIRECTORY

    return root / RUNTIME_DIRECTORY / MISSED_DIRECTORY


def record_missed_slot(root: Path, record: MissedSlotRecord) -> Path:
    """Write a missed slot permanently.

    Recording the identical miss twice is a no-op, so a scheduler that notices
    the same closed window on several ticks does not accumulate duplicates.

    Args:
        root: The operations root.
        record: The miss.

    Returns:
        Where it was written.
    """
    from stock_screener.paper_operations import atomic_write_verified

    path = _directory(root) / f"{record.session}-{record.purpose}.json"
    atomic_write_verified(path, record.as_json())
    return path


def missed_records(root: Path) -> tuple[dict[str, Any], ...]:
    """Every missed slot this runtime has recorded, oldest session first.

    Args:
        root: The operations root.

    Returns:
        The records.
    """
    directory = _directory(root)
    if not directory.exists():
        return ()
    rows = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]
    return tuple(sorted(rows, key=lambda row: (str(row.get("session")), str(row.get("purpose")))))


def expire_pending_executions(
    root: Path, *, schedule: StrategyOperationsSchedule, now: str
) -> tuple[MissedSlotRecord, ...]:
    """Move any decision whose execution window has closed to a terminal state.

    This runs *before* a slot is considered, not after. A pending decision whose
    window closed is not merely stale — it is still marked executable, and the
    next scheduler tick that looked at it would submit it. Expiring it actively
    is what makes "never late" a property of the store rather than a hope about
    scheduling.

    Args:
        root: The operations root.
        schedule: The schedule, authoritative for window boundaries.
        now: The operational instant, ISO-8601 with offset.

    Returns:
        The misses recorded, which is empty on almost every call.
    """
    from stock_screener.paper_operations.clock import parse_instant
    from stock_screener.paper_operations.strategy_schedule import RunPurpose

    instant = parse_instant(now)
    store = StrategyDecisionStore(root)
    expired: list[MissedSlotRecord] = []
    for record in store.all():
        if record.status.is_terminal:
            continue
        session = record.first_execution_session
        if session not in schedule.calendar.sessions:
            continue
        closes = schedule.window_closes_at(session, RunPurpose.STRATEGY_EXECUTION)
        if instant < closes:
            continue
        miss = MissedSlotRecord(
            session=session,
            purpose=str(RunPurpose.STRATEGY_EXECUTION),
            reason_code="MISSED_STRATEGY_EXECUTION",
            due_at=schedule.due_at(session, RunPurpose.STRATEGY_EXECUTION).isoformat(),
            window_closed_at=closes.isoformat(),
            observed_at=now,
            decision_session=record.decision_session,
            decision_fingerprint=record.decision_fingerprint,
            detail=(
                f"the execution window for {session} closed at {closes.isoformat()} "
                f"with decision {record.decision.decision_id} still "
                f"{record.status}. It is moved to a terminal state rather than left "
                "executable: a later scheduler tick would otherwise submit it, and "
                "filling it after its window is a different trade"
            ),
        )
        record_missed_slot(root, miss)
        store.update(
            record.advanced(
                DecisionStatus.EXECUTION_MISSED,
                at=now,
                why="execution window closed; never backfilled",
            )
        )
        expired.append(miss)
    return tuple(expired)
