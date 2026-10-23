"""The append-only event ledger and its hash chain.

An accounting system is trustworthy to the degree that it can show nobody has
quietly changed its history. This ledger makes four distinct manipulations
detectable rather than merely discouraged:

- **tampering** — an edited record no longer hashes to its recorded digest;
- **deletion** — the surviving chain breaks where the removed link was;
- **reordering** — each record binds its predecessor's hash, so a swap breaks
  both links;
- **insertion** — a record spliced in has no predecessor whose hash matches.

None of that depends on a database, a signature or an external service. It is a
property of the records themselves, so a ledger copied to another machine can be
verified there with nothing but this module.

The chain is versioned (`PORTFOLIO_LEDGER_HASH_CHAIN_V1`) because the digest
inputs are a contract: changing what goes into a link would make every existing
ledger unverifiable, which is a migration rather than a bug fix.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .events import EventType, PortfolioEvent

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = [
    "GENESIS_HASH",
    "LEDGER_CHAIN_VERSION",
    "LedgerRecord",
    "PortfolioLedger",
    "chain_link",
    "verify_chain",
]

#: The hash-chain contract. Changing the digest inputs needs a new version.
LEDGER_CHAIN_VERSION = "PORTFOLIO_LEDGER_HASH_CHAIN_V1"

#: What the first record's `previous_hash` binds. A fixed, documented value, so
#: a ledger whose opening record was removed cannot pass as a shorter ledger.
GENESIS_HASH = "0" * 64


def chain_link(
    *, sequence: int, event_fingerprint: str, previous_hash: str, event_type: str, effective_at: str
) -> str:
    """Compute one link of the chain.

    Args:
        sequence: The record's position, starting at 1.
        event_fingerprint: The event's own digest.
        previous_hash: The preceding record's hash, or `GENESIS_HASH`.
        event_type: The event kind, bound so a substituted event of another kind
            cannot inherit a valid link.
        effective_at: The event's effective time, bound for the same reason.

    Returns:
        The record's hash.
    """
    payload = "|".join(
        (
            LEDGER_CHAIN_VERSION,
            str(sequence),
            event_type,
            effective_at,
            event_fingerprint,
            previous_hash,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerRecord:
    """One committed event, bound into the chain.

    Attributes:
        sequence: Position in the ledger, 1-based and gapless.
        event: The event committed.
        event_fingerprint: The event's digest as committed. Stored rather than
            recomputed on read, so a change to the event is detectable instead
            of being silently absorbed.
        previous_hash: The preceding record's hash.
        record_hash: This record's hash.
    """

    sequence: int
    event: PortfolioEvent
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
            effective_at=self.event.effective_at,
        )

    def as_json(self) -> dict[str, Any]:
        """Return the record in its persisted form."""
        return {
            "sequence": self.sequence,
            "chain_version": LEDGER_CHAIN_VERSION,
            "event": self.event.as_json(),
            "event_fingerprint": self.event_fingerprint,
            "previous_hash": self.previous_hash,
            "record_hash": self.record_hash,
        }


def _parse_effective(event: PortfolioEvent) -> datetime:
    """Parse an event's effective time.

    Args:
        event: The event.

    Returns:
        The aware moment it took effect.

    Raises:
        AccountingError: If the timestamp is unparseable or has no offset. A
            naive effective time cannot be ordered against one in another zone,
            and ordering is what the ledger exists to establish.
    """
    try:
        moment = datetime.fromisoformat(event.effective_at)
    except ValueError as exc:
        raise AccountingError(
            Code.EVENT_INVALID,
            f"{event.event_id}: effective_at {event.effective_at!r} is not ISO 8601",
        ) from exc
    if moment.tzinfo is None:
        raise AccountingError(
            Code.EVENT_INVALID,
            f"{event.event_id}: effective_at must carry a UTC offset; a naive "
            "timestamp cannot be ordered against one from another zone",
        )
    return moment


def verify_chain(records: Sequence[LedgerRecord]) -> dict[str, Any]:
    """Verify a ledger's integrity from the records alone.

    Args:
        records: The ledger, in order.

    Returns:
        The verification result, naming the first broken link when there is one.
    """
    if not records:
        return {
            "chain_version": LEDGER_CHAIN_VERSION,
            "verified": True,
            "record_count": 0,
            "head_hash": GENESIS_HASH,
            "failure": None,
        }
    previous = GENESIS_HASH
    expected_sequence = 1
    for record in records:
        if record.sequence != expected_sequence:
            return {
                "chain_version": LEDGER_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": {
                    "code": Code.LEDGER_SEQUENCE_GAP,
                    "at_sequence": record.sequence,
                    "detail": (
                        f"expected sequence {expected_sequence}, found {record.sequence}: "
                        "a record was deleted, reordered or inserted"
                    ),
                },
            }
        if record.previous_hash != previous:
            return {
                "chain_version": LEDGER_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": {
                    "code": Code.LEDGER_HASH_CHAIN_MISMATCH,
                    "at_sequence": record.sequence,
                    "detail": (
                        f"record {record.sequence} binds previous hash "
                        f"{record.previous_hash[:16]}… but its predecessor hashes to "
                        f"{previous[:16]}…"
                    ),
                },
            }
        if record.event.fingerprint() != record.event_fingerprint:
            return {
                "chain_version": LEDGER_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": {
                    "code": Code.LEDGER_HASH_CHAIN_MISMATCH,
                    "at_sequence": record.sequence,
                    "detail": (
                        f"record {record.sequence} stores fingerprint "
                        f"{record.event_fingerprint[:16]}… but its event now hashes to "
                        f"{record.event.fingerprint()[:16]}…: the event was altered"
                    ),
                },
            }
        if record.recompute() != record.record_hash:
            return {
                "chain_version": LEDGER_CHAIN_VERSION,
                "verified": False,
                "record_count": len(records),
                "head_hash": None,
                "failure": {
                    "code": Code.LEDGER_HASH_CHAIN_MISMATCH,
                    "at_sequence": record.sequence,
                    "detail": (
                        f"record {record.sequence} stores hash "
                        f"{record.record_hash[:16]}… but recomputes to "
                        f"{record.recompute()[:16]}…"
                    ),
                },
            }
        previous = record.record_hash
        expected_sequence += 1
    return {
        "chain_version": LEDGER_CHAIN_VERSION,
        "verified": True,
        "record_count": len(records),
        "head_hash": previous,
        "failure": None,
    }


class PortfolioLedger:
    """An append-only sequence of portfolio events.

    There is no update and no delete. A wrong event is corrected by appending a
    compensating one, which is the whole difference between an audit trail and a
    mutable table: the latter can be made to agree with any story.

    Append is idempotent on `event_id`. Re-presenting an event with the same id
    and the same payload is a no-op — the usual case after a retry — while the
    same id carrying a *different* payload is `EVENT_ID_CONFLICT`, because one of
    the two is wrong and the ledger cannot know which.
    """

    def __init__(self, *, records: Sequence[LedgerRecord] = ()) -> None:
        """Open a ledger over existing records.

        Args:
            records: Previously committed records, in order.

        Raises:
            AccountingError: If the records do not verify. A ledger that opens
                over a broken chain would launder the break into every
                subsequent record.
        """
        result = verify_chain(records)
        if not result["verified"]:
            failure = result["failure"]
            raise AccountingError(str(failure["code"]), str(failure["detail"]))
        self._records: list[LedgerRecord] = list(records)
        self._by_id: dict[str, LedgerRecord] = {row.event.event_id: row for row in self._records}

    @property
    def records(self) -> tuple[LedgerRecord, ...]:
        """Every committed record, in order."""
        return tuple(self._records)

    @property
    def head_hash(self) -> str:
        """The current chain head, or `GENESIS_HASH` for an empty ledger."""
        return self._records[-1].record_hash if self._records else GENESIS_HASH

    @property
    def next_sequence(self) -> int:
        """The sequence the next appended record will take."""
        return len(self._records) + 1

    def __len__(self) -> int:
        """The number of committed records."""
        return len(self._records)

    def __iter__(self) -> Iterator[LedgerRecord]:
        """Iterate the records in committed order."""
        return iter(self._records)

    def has_event(self, event_id: str) -> bool:
        """Whether an event id is already committed.

        Args:
            event_id: The id to look for.

        Returns:
            True if it is in the ledger.
        """
        return event_id in self._by_id

    @property
    def has_opening_balance(self) -> bool:
        """Whether an opening balance has been established."""
        return any(row.event.event_type is EventType.OPENING_BALANCE for row in self._records)

    def append(self, event: PortfolioEvent) -> LedgerRecord:
        """Commit an event.

        Args:
            event: The event to append.

        Returns:
            The committed record. For a duplicate id with an identical payload,
            the record already committed.

        Raises:
            AccountingError: `EVENT_ID_CONFLICT` if the id is reused with a
                different payload; `EVENT_TIME_REGRESSION` if the event takes
                effect before the ledger's last committed event;
                `OPENING_BALANCE_REQUIRED` if it is not an opening balance and
                none has been established; `OPENING_BALANCE_ALREADY_EXISTS` if
                it is a second one.
        """
        fingerprint = event.fingerprint()
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            if existing.event_fingerprint == fingerprint:
                return existing
            raise AccountingError(
                Code.EVENT_ID_CONFLICT,
                f"{event.event_id} is already committed at sequence {existing.sequence} "
                f"with fingerprint {existing.event_fingerprint[:16]}…, but the event "
                f"presented now hashes to {fingerprint[:16]}…. One of the two is wrong "
                "and the ledger cannot tell which, so neither is adopted.",
            )

        is_opening = event.event_type is EventType.OPENING_BALANCE
        if is_opening and self.has_opening_balance:
            raise AccountingError(
                Code.OPENING_BALANCE_ALREADY_EXISTS,
                f"{event.event_id}: this ledger already has an opening balance. A "
                "second one would silently restate the portfolio's history rather "
                "than record a change to it.",
            )
        if not is_opening and not self.has_opening_balance:
            raise AccountingError(
                Code.OPENING_BALANCE_REQUIRED,
                f"{event.event_id}: no opening balance has been established, so there "
                "is no state for this event to change. An implied zero balance would "
                "make an imported portfolio look like it was bought here.",
            )

        moment = _parse_effective(event)
        if self._records:
            last = self._records[-1]
            last_moment = _parse_effective(last.event)
            if moment < last_moment:
                raise AccountingError(
                    Code.EVENT_TIME_REGRESSION,
                    f"{event.event_id} takes effect at {event.effective_at}, before "
                    f"sequence {last.sequence} at {last.event.effective_at}. The "
                    "ledger is ordered by effect, and inserting into the past would "
                    "change figures already reported. Record a correcting event "
                    "instead.",
                )

        record = LedgerRecord(
            sequence=self.next_sequence,
            event=event,
            event_fingerprint=fingerprint,
            previous_hash=self.head_hash,
            record_hash=chain_link(
                sequence=self.next_sequence,
                event_fingerprint=fingerprint,
                previous_hash=self.head_hash,
                event_type=str(event.event_type),
                effective_at=event.effective_at,
            ),
        )
        self._records.append(record)
        self._by_id[event.event_id] = record
        return record

    def verify(self) -> dict[str, Any]:
        """Verify this ledger's chain.

        Returns:
            The verification result.
        """
        return verify_chain(self._records)

    def through(self, *, sequence: int) -> tuple[LedgerRecord, ...]:
        """The records up to and including a sequence.

        Args:
            sequence: The last record to include.

        Returns:
            The prefix, which is itself a verifiable ledger.
        """
        return tuple(row for row in self._records if row.sequence <= sequence)

    def as_json(self) -> dict[str, Any]:
        """Return the ledger in its persisted form."""
        return {
            "chain_version": LEDGER_CHAIN_VERSION,
            "record_count": len(self._records),
            "head_hash": self.head_hash,
            "records": [row.as_json() for row in self._records],
        }
