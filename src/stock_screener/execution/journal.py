"""The append-only execution journal, its hash chain, and its mandatory anchor.

This is the accounting ledger's design applied to orders, including the lesson
P1.4 had to learn the hard way. A hash chain proves its records are internally
consistent; it cannot prove there were no more of them, because a journal with
its last records removed is a perfectly valid shorter chain. P1.3 answered that
with a checkpoint somebody had to remember to write, which is a habit rather than
a guarantee — so the anchor here is mandatory from the start.

Four manipulations are detectable from the records alone:

    tamper      — the stored event fingerprint no longer matches the event
    deletion    — the surviving chain breaks where the removed link was
    reordering  — each record binds its predecessor, so a swap breaks both
    insertion   — a spliced record has no predecessor whose hash matches

and truncation is detectable against the anchor, which records how far the
journal actually got and never moves backward.

**Order state is derived, never stored.** Replaying the journal produces every
order's state, cumulative fill and remaining quantity. There is no field anybody
updates, which is what makes the journal the authority rather than a log beside
one.

The same honest limit applies as to the accounting ledger: a journal and its
local anchor rolled back *together* validate cleanly, because local files cannot
establish that they are the latest state of themselves.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .errors import Code, ExecutionError
from .events import ExecutionEvent, FillApplication
from .state_machine import OrderState, OrderStatus
from .versions import JOURNAL_CHAIN_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from .events import ExecutionFill
    from .models import Order

__all__ = [
    "GENESIS_HASH",
    "ExecutionJournal",
    "JournalRecord",
    "OrderLedger",
    "chain_link",
    "verify_journal",
]

#: What the first record's `previous_hash` binds. A fixed, documented value, so a
#: journal whose opening record was removed cannot pass as a shorter journal.
GENESIS_HASH = "0" * 64


def chain_link(
    *, sequence: int, event_fingerprint: str, previous_hash: str, event_type: str
) -> str:
    """Compute one link of the journal chain.

    Args:
        sequence: The record's position, from 1.
        event_fingerprint: The event's own digest.
        previous_hash: The preceding record's hash, or `GENESIS_HASH`.
        event_type: Bound so a substituted event of another kind cannot inherit
            a valid link.

    Returns:
        The record's hash.
    """
    payload = "|".join(
        (JOURNAL_CHAIN_VERSION, str(sequence), event_type, event_fingerprint, previous_hash)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JournalRecord:
    """One committed execution event, bound into the chain.

    Attributes:
        sequence: Position in the journal, 1-based and gapless.
        event: The event committed.
        event_fingerprint: Its digest as committed. Stored rather than
            recomputed on read, so an altered event is detected instead of being
            silently absorbed.
        previous_hash: The preceding record's hash.
        record_hash: This record's hash.
    """

    sequence: int
    event: ExecutionEvent
    event_fingerprint: str
    previous_hash: str
    record_hash: str

    def recompute(self) -> str:
        """Recompute this record's hash from its contents."""
        return chain_link(
            sequence=self.sequence,
            event_fingerprint=self.event_fingerprint,
            previous_hash=self.previous_hash,
            event_type=str(self.event.event_type),
        )

    def as_json(self) -> dict[str, Any]:
        """Return the record in its persisted form."""
        return {
            "sequence": self.sequence,
            "chain_version": JOURNAL_CHAIN_VERSION,
            "event": self.event.as_json(),
            "event_fingerprint": self.event_fingerprint,
            "previous_hash": self.previous_hash,
            "record_hash": self.record_hash,
        }


def _failure(code: str, sequence: int, detail: str) -> dict[str, Any]:
    """Shape a verification failure."""
    return {"code": code, "at_sequence": sequence, "detail": detail}


def verify_journal(records: Sequence[JournalRecord]) -> dict[str, Any]:
    """Verify a journal's integrity from its records alone.

    Args:
        records: The journal, in order.

    Returns:
        The result, naming the first broken link when there is one.
    """
    if not records:
        return {
            "chain_version": JOURNAL_CHAIN_VERSION,
            "verified": True,
            "record_count": 0,
            "head_hash": GENESIS_HASH,
            "failure": None,
        }
    previous = GENESIS_HASH
    expected = 1
    for record in records:
        if record.sequence != expected:
            return {
                "chain_version": JOURNAL_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": _failure(
                    Code.EXECUTION_JOURNAL_SEQUENCE_GAP,
                    record.sequence,
                    f"expected sequence {expected}, found {record.sequence}: a "
                    "record was deleted, reordered or inserted",
                ),
            }
        if record.previous_hash != previous:
            return {
                "chain_version": JOURNAL_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": _failure(
                    Code.EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH,
                    record.sequence,
                    f"record {record.sequence} binds previous hash "
                    f"{record.previous_hash[:16]}… but its predecessor hashes to "
                    f"{previous[:16]}…",
                ),
            }
        if record.event.fingerprint() != record.event_fingerprint:
            return {
                "chain_version": JOURNAL_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": _failure(
                    Code.EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH,
                    record.sequence,
                    f"record {record.sequence} stores fingerprint "
                    f"{record.event_fingerprint[:16]}… but its event now hashes to "
                    f"{record.event.fingerprint()[:16]}…: the event was altered",
                ),
            }
        if record.recompute() != record.record_hash:
            return {
                "chain_version": JOURNAL_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": _failure(
                    Code.EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH,
                    record.sequence,
                    f"record {record.sequence} stores hash "
                    f"{record.record_hash[:16]}… but recomputes to "
                    f"{record.recompute()[:16]}…",
                ),
            }
        previous = record.record_hash
        expected += 1
    return {
        "chain_version": JOURNAL_CHAIN_VERSION,
        "verified": True,
        "record_count": len(records),
        "head_hash": previous,
        "failure": None,
    }


@dataclass(frozen=True)
class OrderLedger:
    """Every order's state, derived by replaying the journal.

    Attributes:
        statuses: `{order_id: OrderStatus}`, in insertion order.
        fills: `{fill_id: ExecutionFill}` for every admitted fill.
        applications: `{fill_id: FillApplication}` — whether each fill has
            reached the book. This is what makes the two-phase handoff
            recoverable: a fill that is durable here and `PENDING` is a fill the
            book has not seen.
        journal_sequence: The last record replayed.
        journal_head: That record's chain hash.
    """

    statuses: dict[str, OrderStatus]
    fills: dict[str, ExecutionFill]
    applications: dict[str, FillApplication]
    journal_sequence: int
    journal_head: str

    @property
    def pending_fills(self) -> tuple[ExecutionFill, ...]:
        """Fills that are durable evidence but not yet in the book.

        Recovery applies exactly these, in fill-id order so the sequence is
        deterministic across processes.
        """
        return tuple(
            self.fills[fill_id]
            for fill_id in sorted(self.fills)
            if self.applications.get(fill_id, FillApplication.PENDING) is FillApplication.PENDING
        )

    def status(self, order_id: str) -> OrderStatus:
        """One order's state.

        Args:
            order_id: The order.

        Returns:
            Its status.

        Raises:
            ExecutionError: `ORDER_NOT_FOUND` if the journal has no such order.
        """
        found = self.statuses.get(order_id)
        if found is None:
            raise ExecutionError(
                Code.ORDER_NOT_FOUND, f"{order_id} does not appear in this journal"
            )
        return found

    def as_json(self) -> dict[str, Any]:
        """Return the derived state as a JSON-safe mapping."""
        return {
            "journal_sequence": self.journal_sequence,
            "journal_head": self.journal_head,
            "orders": [self.statuses[key].as_json() for key in sorted(self.statuses)],
            "fills": [self.fills[key].as_json() for key in sorted(self.fills)],
            "applications": {key: str(self.applications[key]) for key in sorted(self.applications)},
            "pending_fill_ids": [fill.fill_id for fill in self.pending_fills],
        }


def _parse_instant(value: str, *, event_id: str) -> datetime:
    """Parse an event's occurrence time.

    Args:
        value: The timestamp.
        event_id: For the message.

    Returns:
        The aware moment.

    Raises:
        ExecutionError: If unparseable or naive.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            f"{event_id}: occurred_at {value!r} is not ISO 8601",
        ) from exc
    if moment.tzinfo is None:
        raise ExecutionError(
            Code.EXECUTION_EVENT_INVALID,
            f"{event_id}: occurred_at must carry a UTC offset; a naive timestamp "
            "cannot be ordered against one from another zone",
        )
    return moment


class ExecutionJournal:
    """An append-only sequence of execution events.

    There is no update and no delete. A wrong event is corrected by appending a
    compensating one, which is the difference between an audit trail and a
    mutable table.

    Append is idempotent on `event_id`: re-presenting an event with the same id
    and payload is a no-op, which is the ordinary case after a retry. The same id
    carrying a *different* payload is `EXECUTION_EVENT_ID_CONFLICT`, because one
    of the two is wrong and the journal cannot know which.
    """

    def __init__(self, *, records: Sequence[JournalRecord] = ()) -> None:
        """Open a journal over existing records.

        Args:
            records: Previously committed records, in order.

        Raises:
            ExecutionError: If the records do not verify. A journal that opened
                over a broken chain would launder the break into everything
                appended after it.
        """
        result = verify_journal(records)
        if not result["verified"]:
            failure = result["failure"]
            raise ExecutionError(str(failure["code"]), str(failure["detail"]))
        self._records: list[JournalRecord] = list(records)
        self._by_id: dict[str, JournalRecord] = {row.event.event_id: row for row in self._records}

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        """Every committed record, in order."""
        return tuple(self._records)

    @property
    def head_hash(self) -> str:
        """The current chain head, or `GENESIS_HASH` for an empty journal."""
        return self._records[-1].record_hash if self._records else GENESIS_HASH

    @property
    def next_sequence(self) -> int:
        """The sequence the next record will take."""
        return len(self._records) + 1

    def __len__(self) -> int:
        """How many records are committed."""
        return len(self._records)

    def __iter__(self) -> Iterator[JournalRecord]:
        """Iterate the records in committed order."""
        return iter(self._records)

    def has_event(self, event_id: str) -> bool:
        """Whether an event id is already committed.

        Args:
            event_id: The id.

        Returns:
            True if present.
        """
        return event_id in self._by_id

    def validate(self, event: ExecutionEvent) -> None:
        """Check an event could be appended, without appending it.

        Separated from `append` so the persistence layer can refuse an event
        *before* any durable write — the P1.4 lesson, where a record the state
        machine would always refuse reached disk first and left a journal that
        could never be replayed.

        Args:
            event: The candidate.

        Raises:
            ExecutionError: `EXECUTION_EVENT_ID_CONFLICT` if the id is reused
                with a different payload; `EXECUTION_EVENT_INVALID` on a bad
                occurrence time or an out-of-order one; whatever the order state
                machine refuses for this event.
        """
        fingerprint = event.fingerprint()
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            if existing.event_fingerprint == fingerprint:
                return
            raise ExecutionError(
                Code.EXECUTION_EVENT_ID_CONFLICT,
                f"{event.event_id} is already committed at sequence "
                f"{existing.sequence} with fingerprint "
                f"{existing.event_fingerprint[:16]}…, and the event presented now "
                f"hashes to {fingerprint[:16]}…. One of the two is wrong and the "
                "journal cannot tell which, so neither is adopted.",
            )
        moment = _parse_instant(event.occurred_at, event_id=event.event_id)
        if self._records:
            last = self._records[-1]
            if moment < _parse_instant(last.event.occurred_at, event_id=last.event.event_id):
                raise ExecutionError(
                    Code.EXECUTION_EVENT_INVALID,
                    f"{event.event_id} occurred at {event.occurred_at}, before "
                    f"sequence {last.sequence} at {last.event.occurred_at}. The "
                    "journal is ordered by occurrence, and inserting into the past "
                    "would change a lifecycle already reported.",
                )
        # The lifecycle is checked against the state the journal already implies,
        # so an impossible transition is refused here rather than discovered on a
        # later replay.
        self._replay(self._records, candidate=event)

    def append(self, event: ExecutionEvent) -> JournalRecord:
        """Commit an execution event.

        Args:
            event: The event.

        Returns:
            The committed record. For a duplicate id with an identical payload,
            the record already committed.

        Raises:
            ExecutionError: Whatever `validate` refuses.
        """
        self.validate(event)
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            return existing
        fingerprint = event.fingerprint()
        record = JournalRecord(
            sequence=self.next_sequence,
            event=event,
            event_fingerprint=fingerprint,
            previous_hash=self.head_hash,
            record_hash=chain_link(
                sequence=self.next_sequence,
                event_fingerprint=fingerprint,
                previous_hash=self.head_hash,
                event_type=str(event.event_type),
            ),
        )
        self._records.append(record)
        self._by_id[event.event_id] = record
        return record

    def verify(self) -> dict[str, Any]:
        """Verify this journal's chain.

        Returns:
            The verification result.
        """
        return verify_journal(self._records)

    def derive(self) -> OrderLedger:
        """Derive every order's state by replaying the journal.

        Returns:
            The derived state.
        """
        return self._replay(self._records)

    def _replay(
        self, records: Sequence[JournalRecord], *, candidate: ExecutionEvent | None = None
    ) -> OrderLedger:
        """Replay records, optionally with one more event appended.

        Args:
            records: The committed records.
            candidate: An event to apply after them, for validation.

        Returns:
            The derived state.

        Raises:
            ExecutionError: Whatever the order state machine refuses.
        """
        from .events import (
            FillAppliedEvent,
            OrderCreatedEvent,
            OrderFilledEvent,
        )

        # ORDER_EXPIRED needs no special case: like a cancellation it is a pure
        # state transition, and `resulting_state` plus the frozen table decide
        # whether it is legal from where the order currently stands.

        statuses: dict[str, OrderStatus] = {}
        fills: dict[str, ExecutionFill] = {}
        applications: dict[str, FillApplication] = {}
        orders: dict[str, Order] = {}

        events: list[ExecutionEvent] = [row.event for row in records]
        if candidate is not None:
            events.append(candidate)

        for event in events:
            if isinstance(event, OrderCreatedEvent):
                assert event.order is not None  # noqa: S101 - event validates it
                orders[event.order_id] = event.order
                statuses[event.order_id] = OrderStatus(order=event.order)
                continue

            if isinstance(event, FillAppliedEvent):
                if event.fill_id not in fills:
                    raise ExecutionError(
                        Code.EXECUTION_EVENT_INVALID,
                        f"{event.event_id} applies fill {event.fill_id!r}, which "
                        "this journal has no evidence of. A book entry with no "
                        "corresponding fill would be an unexplained trade.",
                    )
                applications[event.fill_id] = FillApplication.APPLIED
                continue

            status = statuses.get(event.order_id)
            if status is None:
                raise ExecutionError(
                    Code.ORDER_NOT_FOUND,
                    f"{event.event_id} concerns order {event.order_id!r}, which was "
                    "never created in this journal",
                )

            if isinstance(event, OrderFilledEvent):
                assert event.fill is not None  # noqa: S101 - event validates it
                fill = event.fill
                known = fills.get(fill.fill_id)
                if known is not None and known.fingerprint() != fill.fingerprint():
                    raise ExecutionError(
                        Code.FILL_ID_CONFLICT,
                        f"fill {fill.fill_id!r} is already recorded as "
                        f"{known.fingerprint()[:16]}… and now presents as "
                        f"{fill.fingerprint()[:16]}…. A fill id is the key the "
                        "accounting handoff is made exactly-once by, so two "
                        "different fills cannot share one.",
                    )
                if known is None:
                    statuses[event.order_id] = status.with_fill(
                        quantity=fill.quantity, price=fill.price, fee=fill.fee
                    )
                    fills[fill.fill_id] = fill
                    applications.setdefault(fill.fill_id, FillApplication.PENDING)
                continue

            target = event.resulting_state()
            if target is not None and target is not OrderState.CREATED:
                statuses[event.order_id] = status.with_state(
                    target, reason=getattr(event, "reason", None)
                )

        head = records[-1].record_hash if records else GENESIS_HASH
        return OrderLedger(
            statuses=statuses,
            fills=fills,
            applications=applications,
            journal_sequence=records[-1].sequence if records else 0,
            journal_head=head,
        )

    def as_json(self) -> dict[str, Any]:
        """Return the journal in its persisted form."""
        return {
            "chain_version": JOURNAL_CHAIN_VERSION,
            "record_count": len(self._records),
            "head_hash": self.head_hash,
            "records": [row.as_json() for row in self._records],
        }
