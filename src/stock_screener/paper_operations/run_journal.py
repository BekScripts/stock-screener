"""The append-only record of what the control plane decided, and when.

Same durability pattern as the execution journal and the accounting ledger,
for the same reason and with the same honest limit.

A hash chain makes tampering, deletion, reordering and insertion detectable from
the records alone: each record carries the previous record's hash, so changing
any one of them breaks every link after it. What a chain *cannot* detect is its
own truncation — a chain that has had its last three records removed is a
perfectly valid shorter chain. Only an anchor recording how long the chain is
supposed to be catches that, which is why the anchor is mandatory rather than
optional and why it may never move backward.

And the limit worth stating rather than glossing: a coordinated local rollback
of the journal *and* its anchor together, to a mutually consistent earlier
state, is undetectable from local files alone. Nothing here claims otherwise.
Closing that needs an anchor held somewhere this process cannot reach.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Code, OperationsError
from .models import RunState, require_run_transition
from .persistence import atomic_write_verified, guard_operations_root, read_json
from .versions import (
    OPERATIONS_JOURNAL_ANCHOR_VERSION,
    OPERATIONS_JOURNAL_CHAIN_VERSION,
)

if TYPE_CHECKING:
    from .clock import Clock

__all__ = [
    "ANCHOR_FILENAME",
    "GENESIS_HASH",
    "JOURNAL_DIRECTORY",
    "OperationsJournalEntry",
    "OperationsRunJournal",
    "RunEventType",
    "chain_link",
]

#: Where run-journal records live inside the operational root.
JOURNAL_DIRECTORY = "run_journal"

#: Where the head anchor lives. One per operational root.
ANCHOR_FILENAME = "run_journal_anchor.json"

#: What the first record links to.
GENESIS_HASH = "0" * 64


class RunEventType(StrEnum):
    """What kind of operational fact a journal entry records."""

    RUN_SCHEDULED = "RUN_SCHEDULED"
    RUN_STATE_CHANGED = "RUN_STATE_CHANGED"
    HEALTH_EVALUATED = "HEALTH_EVALUATED"
    RECONCILED = "RECONCILED"
    INPUTS_FROZEN = "INPUTS_FROZEN"
    PLAN_AUTHORIZED = "PLAN_AUTHORIZED"
    SUBMISSION_GATE_CHECKED = "SUBMISSION_GATE_CHECKED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    FILL_INGESTED = "FILL_INGESTED"
    INCIDENT_RECORDED = "INCIDENT_RECORDED"
    KILL_OBSERVED = "KILL_OBSERVED"
    RUN_COMPLETED = "RUN_COMPLETED"


def chain_link(previous_hash: str, body: dict[str, Any]) -> str:
    """Compute one record's chain hash.

    Args:
        previous_hash: The hash of the record before it.
        body: This record's semantic content.

    Returns:
        The chain hash, binding this record to the whole history before it.
    """
    from stock_screener.execution import fingerprint_of

    return hashlib.sha256(
        f"{OPERATIONS_JOURNAL_CHAIN_VERSION}|{previous_hash}|{fingerprint_of(body)}".encode()
    ).hexdigest()


@dataclass(frozen=True)
class OperationsJournalEntry:
    """One operational fact, linked to every fact before it.

    Attributes:
        sequence: Its position, from 1.
        event_type: What kind of fact.
        run_id: Which run.
        occurred_at: When, ISO-8601 operational time.
        detail: A sanitised description. Never a credential.
        from_state: The run state before, where a transition.
        to_state: The run state after, where a transition.
        evidence: Fingerprints of whatever established the fact.
        previous_hash: The record before it.
        record_hash: This record's chain hash.
    """

    sequence: int
    event_type: RunEventType
    run_id: str
    occurred_at: str
    detail: str = ""
    from_state: RunState | None = None
    to_state: RunState | None = None
    evidence: tuple[tuple[str, str], ...] = ()
    previous_hash: str = GENESIS_HASH
    record_hash: str = ""

    def body(self) -> dict[str, Any]:
        """Return the entry's semantic content, excluding its own hash."""
        return {
            "sequence": self.sequence,
            "event_type": str(self.event_type),
            "run_id": self.run_id,
            "occurred_at": self.occurred_at,
            "detail": self.detail,
            "from_state": str(self.from_state) if self.from_state else None,
            "to_state": str(self.to_state) if self.to_state else None,
            "evidence": [list(pair) for pair in self.evidence],
        }

    def as_json(self) -> dict[str, Any]:
        """Return the entry with its chain links."""
        return {
            **self.body(),
            "previous_hash": self.previous_hash,
            "record_hash": self.record_hash,
            "chain_version": OPERATIONS_JOURNAL_CHAIN_VERSION,
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> OperationsJournalEntry:
        """Rebuild an entry read back from disk.

        Args:
            body: What was written.

        Returns:
            The entry.

        Raises:
            OperationsError: `OPERATIONS_PARTIAL_RECORD` if it cannot be read.
        """
        try:
            return cls(
                sequence=int(body["sequence"]),
                event_type=RunEventType(str(body["event_type"])),
                run_id=str(body["run_id"]),
                occurred_at=str(body.get("occurred_at", "")),
                detail=str(body.get("detail", "")),
                from_state=RunState(str(body["from_state"])) if body.get("from_state") else None,
                to_state=RunState(str(body["to_state"])) if body.get("to_state") else None,
                evidence=tuple((str(pair[0]), str(pair[1])) for pair in body.get("evidence", ())),
                previous_hash=str(body.get("previous_hash", GENESIS_HASH)),
                record_hash=str(body.get("record_hash", "")),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.OPERATIONS_PARTIAL_RECORD,
                f"a run-journal record could not be read: {exc}",
            ) from exc


@dataclass
class OperationsRunJournal:
    """The append-only operational journal, with a mandatory head anchor.

    Attributes:
        root: The operational root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root.

        Raises:
            OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if it lies in
                another trust domain.
        """
        self.root = Path(self.root)
        guard_operations_root(self.root)
        self.directory = self.root / JOURNAL_DIRECTORY
        self.anchor_path = self.root / ANCHOR_FILENAME

    def record_paths(self) -> tuple[Path, ...]:
        """Every journal record, in sequence order."""
        if not self.directory.exists():
            return ()
        return tuple(sorted(self.directory.glob("*.json")))

    def entries(self) -> tuple[OperationsJournalEntry, ...]:
        """Read the whole journal.

        Returns:
            The entries, in order.
        """
        return tuple(
            OperationsJournalEntry.from_body(read_json(path)) for path in self.record_paths()
        )

    @property
    def head_hash(self) -> str:
        """The chain head, or genesis for an empty journal."""
        entries = self.entries()
        return entries[-1].record_hash if entries else GENESIS_HASH

    def append(
        self,
        *,
        clock: Clock,
        event_type: RunEventType,
        run_id: str,
        detail: str = "",
        from_state: RunState | None = None,
        to_state: RunState | None = None,
        evidence: tuple[tuple[str, str], ...] = (),
    ) -> OperationsJournalEntry:
        """Append one fact, validating it before anything is written.

        The order matters and is the P1.4 lesson: an illegal transition is
        refused *before* the record reaches disk. A journal containing a
        transition the state machine rejects can never be replayed, so the
        validation has to happen first even though it means the caller finds out
        before the write rather than after.

        Args:
            clock: The operational clock.
            event_type: What kind of fact.
            run_id: Which run.
            detail: A sanitised description.
            from_state: The state before, for a transition.
            to_state: The state after, for a transition.
            evidence: Fingerprints of what established it.

        Returns:
            The appended entry.

        Raises:
            OperationsError: `INVALID_OPERATIONS_RUN_TRANSITION` if the states
                are not a legal edge; `OPERATIONS_PARTIAL_RECORD` if the write
                could not be made durable.
        """
        if from_state is not None and to_state is not None:
            require_run_transition(from_state, to_state)

        entries = self.entries()
        sequence = len(entries) + 1
        previous = entries[-1].record_hash if entries else GENESIS_HASH
        draft = OperationsJournalEntry(
            sequence=sequence,
            event_type=event_type,
            run_id=run_id,
            occurred_at=clock.iso(),
            detail=detail,
            from_state=from_state,
            to_state=to_state,
            evidence=evidence,
            previous_hash=previous,
        )
        entry = OperationsJournalEntry(
            sequence=draft.sequence,
            event_type=draft.event_type,
            run_id=draft.run_id,
            occurred_at=draft.occurred_at,
            detail=draft.detail,
            from_state=draft.from_state,
            to_state=draft.to_state,
            evidence=draft.evidence,
            previous_hash=previous,
            record_hash=chain_link(previous, draft.body()),
        )
        atomic_write_verified(self.directory / f"{sequence:012d}.json", entry.as_json())
        self._write_anchor(entry)
        return entry

    def _write_anchor(self, entry: OperationsJournalEntry) -> None:
        """Advance the head anchor, refusing to move it backward.

        Args:
            entry: The newly appended entry.

        Raises:
            OperationsError: `OPERATIONS_ANCHOR_REGRESSION` if the anchor would
                move backward. An anchor that can retreat cannot detect
                truncation, which is the only thing it exists for.
        """
        existing = self.read_anchor()
        if existing is not None and int(existing.get("sequence", 0)) > entry.sequence:
            raise OperationsError(
                Code.OPERATIONS_ANCHOR_REGRESSION,
                f"the head anchor stands at sequence {existing.get('sequence')} and "
                f"this append would move it back to {entry.sequence}. An anchor that "
                "can retreat cannot detect truncation, which is the only thing it is "
                "for",
            )
        atomic_write_verified(
            self.anchor_path,
            {
                "anchor_version": OPERATIONS_JOURNAL_ANCHOR_VERSION,
                "sequence": entry.sequence,
                "head_hash": entry.record_hash,
                "run_id": entry.run_id,
                "observed_at": entry.occurred_at,
            },
        )

    def read_anchor(self) -> dict[str, Any] | None:
        """Read the head anchor.

        Returns:
            The anchor, or `None` for an empty journal.
        """
        if not self.anchor_path.exists():
            return None
        return read_json(self.anchor_path)

    def verify(self) -> dict[str, Any]:
        """Verify the chain and the anchor together.

        Returns:
            What was established: the length, the head, and whether each check
            passed.

        Raises:
            OperationsError: `OPERATIONS_JOURNAL_CHAIN_BROKEN` for a broken
                link, a sequence gap or a reordering;
                `OPERATIONS_JOURNAL_TRUNCATED` when the journal is shorter than
                its anchor — the one failure a chain alone cannot see.
        """
        entries = self.entries()
        previous = GENESIS_HASH
        for index, entry in enumerate(entries, start=1):
            if entry.sequence != index:
                raise OperationsError(
                    Code.OPERATIONS_JOURNAL_CHAIN_BROKEN,
                    f"record {index} carries sequence {entry.sequence}; the journal "
                    "has a gap or has been reordered",
                )
            if entry.previous_hash != previous:
                raise OperationsError(
                    Code.OPERATIONS_JOURNAL_CHAIN_BROKEN,
                    f"record {entry.sequence} does not link to the record before it; "
                    "something was inserted, removed or altered",
                )
            if entry.record_hash != chain_link(previous, entry.body()):
                raise OperationsError(
                    Code.OPERATIONS_JOURNAL_CHAIN_BROKEN,
                    f"record {entry.sequence} does not match its own hash; its "
                    "content has been altered",
                )
            previous = entry.record_hash

        anchor = self.read_anchor()
        anchored = int(anchor.get("sequence", 0)) if anchor else 0
        if anchored > len(entries):
            raise OperationsError(
                Code.OPERATIONS_JOURNAL_TRUNCATED,
                f"the anchor records {anchored} entries and only {len(entries)} are "
                "present. The tail has been truncated — a chain alone cannot detect "
                "this, which is why the anchor is mandatory",
            )
        if (
            anchor
            and entries
            and anchored == len(entries)
            and str(anchor.get("head_hash")) != entries[-1].record_hash
        ):
            raise OperationsError(
                Code.OPERATIONS_JOURNAL_CHAIN_BROKEN,
                "the anchor names a different head than the journal's last record",
            )
        return {
            "chain_version": OPERATIONS_JOURNAL_CHAIN_VERSION,
            "anchor_version": OPERATIONS_JOURNAL_ANCHOR_VERSION,
            "entries": len(entries),
            "head_hash": previous,
            "anchored_sequence": anchored,
            "chain_verified": True,
            "anchor_verified": True,
            "not_detectable": (
                "a coordinated local rollback of the journal and this anchor "
                "together, to a mutually consistent earlier state, cannot be "
                "detected from local files alone"
            ),
        }

    def entries_for(self, run_id: str) -> tuple[OperationsJournalEntry, ...]:
        """Every entry belonging to one run.

        Args:
            run_id: Which run.

        Returns:
            The entries, in order.
        """
        return tuple(entry for entry in self.entries() if entry.run_id == run_id)

    def last_state_of(self, run_id: str) -> RunState | None:
        """The last state a run reached, from the journal rather than a cache.

        Derived from the append-only record on purpose. A separately stored
        "current state" would be a second authority that could disagree with
        the history of how it got there.

        Args:
            run_id: Which run.

        Returns:
            The state, or `None` if the run has no recorded transition.
        """
        for entry in reversed(self.entries_for(run_id)):
            if entry.to_state is not None:
                return entry.to_state
        return None
