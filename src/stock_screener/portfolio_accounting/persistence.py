"""Durable storage for the ledger, checkpoints and reconciliation reports.

The storage format is one JSON file per record under `.portfolio/`, and that is a
deliberate choice rather than a placeholder. A SQLite database would give
transactions and would also give the ability to run an UPDATE, which is precisely
the operation this design forbids; the append-only guarantee would then rest on
discipline instead of on the format. Plain files also mean a ledger can be
inspected, copied and verified with no tooling at all, which matters for
something whose whole purpose is being checkable years later.

Three properties are engineered rather than assumed:

**Durability.** Every write goes to a temporary file, is flushed and `fsync`ed,
then atomically renamed into place, and the containing directory is `fsync`ed
too. A rename is atomic on POSIX, so a crash leaves either the old state or the
new one — never a half-written record that would fail to parse and take the whole
ledger with it.

**Write-then-verify.** After each append the record is read back and its hash
recomputed. A write that succeeded but landed wrong is caught immediately, where
it is one record, rather than at the next verification, where it is indivisible
from everything appended since.

**Truncation detection.** A hash chain alone cannot detect that records were
removed from the *end* — the surviving prefix is a perfectly valid shorter
ledger. Checkpoints close that hole: each records the head hash and the record
count, so a ledger that has lost its tail no longer matches its own checkpoint.

Nothing here writes outside `.portfolio/`, and the namespace guard refuses a root
inside the research evidence trees outright.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import structlog

from .errors import AccountingError, Code
from .events import (
    CashDividendEvent,
    CashFlowDirection,
    CorporateActionEvent,
    EventType,
    ExternalCashFlowEvent,
    FillEvent,
    OpeningBalanceEvent,
    OpeningPosition,
    PortfolioEvent,
    Side,
    SplitEvent,
    slices_from,
)
from .ledger import LedgerRecord, PortfolioLedger, verify_chain
from .money import as_decimal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .reconciliation import ReconciliationResult

log = structlog.get_logger(__name__)

__all__ = [
    "FORBIDDEN_ROOTS",
    "LEDGER_DIRNAME",
    "STORE_VERSION",
    "Checkpoint",
    "PortfolioStore",
    "event_from_json",
]

#: The storage contract.
STORE_VERSION = "PORTFOLIO_STORE_V1"

#: Where the store lives, relative to the root it is given.
LEDGER_DIRNAME = "ledger"
CHECKPOINT_DIRNAME = "checkpoints"
RECONCILIATION_DIRNAME = "reconciliation"

#: Path fragments a portfolio store may never be rooted in. Portfolio events are
#: operational records; writing them into the research evidence trees would mix
#: trading state into the frozen point-in-time corpus the whole research
#: programme rests on, and no later cleanup could reliably separate them again.
FORBIDDEN_ROOTS: tuple[str, ...] = (
    ".evidence",
    "pit_research_dataset",
    "research/r1_",
    "stage9k",
    "compounder_radar.db",
)


def _guard_root(root: Path) -> None:
    """Refuse a store root that would contaminate research evidence.

    Args:
        root: The proposed root.

    Raises:
        AccountingError: `FORBIDDEN_LEDGER_NAMESPACE` if the path lies in a
            research or evidence namespace.
    """
    lowered = str(root).replace("\\", "/").lower()
    for fragment in FORBIDDEN_ROOTS:
        if fragment in lowered:
            raise AccountingError(
                Code.FORBIDDEN_LEDGER_NAMESPACE,
                f"{root} lies in the {fragment!r} namespace. Portfolio events are "
                "operational records; writing them into the research evidence trees "
                "would mix trading state into the frozen point-in-time corpus, and "
                "no later cleanup could reliably separate them again.",
            )


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON durably and atomically.

    Args:
        path: Where the record goes.
        payload: What to write.

    Raises:
        AccountingError: `PARTIAL_EVENT_RECORD` if the write could not be made
            durable. The temporary file is removed, so a failed write leaves no
            trace rather than a file that looks like a record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise AccountingError(
            Code.PARTIAL_EVENT_RECORD,
            f"{path.name} could not be written durably: {exc}. Nothing was left "
            "behind, so the ledger is still whatever it was before this append.",
        ) from exc


class _Envelope(TypedDict):
    """The envelope fields every event shares, so they can be splatted typed."""

    event_id: str
    effective_at: str
    source_id: str
    source_fingerprint: str
    ingested_at: str | None


def event_from_json(payload: Mapping[str, Any]) -> PortfolioEvent:
    """Rebuild an event from its persisted form.

    Args:
        payload: The event as `PortfolioEvent.as_json` wrote it.

    Returns:
        The event.

    Raises:
        AccountingError: If the event type is unknown. An unrecognised type is
            refused rather than loaded as a generic record, because a record the
            state machine cannot apply must not sit silently in a ledger that
            replays cleanly without it.
    """
    event_type = str(payload["event_type"])
    body = payload["payload"]
    ingested = payload.get("ingested_at")
    common: _Envelope = {
        "event_id": str(payload["event_id"]),
        "effective_at": str(payload["effective_at"]),
        "source_id": str(payload["source_id"]),
        "source_fingerprint": str(payload["source_fingerprint"]),
        "ingested_at": None if ingested is None else str(ingested),
    }
    if event_type == EventType.OPENING_BALANCE:
        return OpeningBalanceEvent(
            event_type=EventType.OPENING_BALANCE,
            cash=as_decimal(body["cash"], field="cash"),
            positions=tuple(
                OpeningPosition(
                    identity=_identity_from(row["identity"]),
                    quantity=as_decimal(row["quantity"], field="quantity"),
                    cost_basis_per_share=as_decimal(
                        row["cost_basis_per_share"], field="cost basis"
                    ),
                    attribution=slices_from(row.get("attribution", ())),
                )
                for row in body.get("positions", ())
            ),
            **common,
        )
    if event_type == EventType.FILL:
        return FillEvent(
            event_type=EventType.FILL,
            identity=_identity_from(body["identity"]),
            side=Side(body["side"]),
            quantity=as_decimal(body["quantity"], field="quantity"),
            price=as_decimal(body["price"], field="price"),
            fees=as_decimal(body["fees"], field="fees"),
            attribution=slices_from(body.get("attribution", ())),
            execution_reference=body.get("execution_reference"),
            **common,
        )
    if event_type == EventType.EXTERNAL_CASH_FLOW:
        return ExternalCashFlowEvent(
            event_type=EventType.EXTERNAL_CASH_FLOW,
            direction=CashFlowDirection(body["direction"]),
            amount=as_decimal(body["amount"], field="amount"),
            **common,
        )
    if event_type == EventType.CASH_DIVIDEND:
        return CashDividendEvent(
            event_type=EventType.CASH_DIVIDEND,
            identity=_identity_from(body["identity"]),
            cash_amount=as_decimal(body["cash_amount"], field="cash amount"),
            corporate_action_reference=body.get("corporate_action_reference"),
            **common,
        )
    if event_type == EventType.SPLIT:
        return SplitEvent(
            event_type=EventType.SPLIT,
            identity=_identity_from(body["identity"]),
            numerator=as_decimal(body["numerator"], field="numerator"),
            denominator=as_decimal(body["denominator"], field="denominator"),
            **common,
        )
    if event_type == EventType.CORPORATE_ACTION:
        return CorporateActionEvent(
            event_type=EventType.CORPORATE_ACTION,
            identity=_identity_from(body["identity"]),
            action=str(body["action"]),
            terms=dict(body.get("terms", {})),
            **common,
        )
    raise AccountingError(
        Code.EVENT_INVALID,
        f"{event_type!r} is not an event type this engine can load. A record the "
        "state machine cannot apply must not sit in a ledger that would otherwise "
        "replay cleanly without it.",
    )


def _identity_from(label: str) -> Any:
    """Rebuild a durable identity from its `KIND:id` label.

    The display symbol is deliberately not persisted and so is not restored. A
    ticker is a dated display attribute: storing one would invite a later reader
    to treat a reassigned symbol as the same company, which is the identity bug
    the whole system is keyed to avoid. A reloaded state therefore carries
    `symbol=None`, and every accounting figure in it is byte-identical.

    Args:
        label: The label.

    Returns:
        The identity, without a display symbol.

    Raises:
        AccountingError: If the label is malformed.
    """
    from stock_screener.portfolio_risk import SecurityIdentity

    kind, _, raw = str(label).partition(":")
    if not kind or not raw.isdigit():
        raise AccountingError(
            Code.EVENT_INVALID, f"{label!r} is not a durable identity label of the form KIND:id"
        )
    return SecurityIdentity(kind=kind, identity_id=int(raw))


@dataclass(frozen=True)
class Checkpoint:
    """A signed-off statement of where a ledger stood.

    A checkpoint is what makes truncation detectable. A hash chain proves its own
    internal consistency, but a ledger with its last three records deleted is a
    valid chain — just a shorter one. A checkpoint recorded before the deletion
    names a head hash and a count that the truncated ledger cannot produce.

    Attributes:
        sequence: The record the checkpoint was taken at.
        head_hash: The chain head at that point.
        record_count: How many records existed.
        taken_at: When the checkpoint was written. Operational.
        nav: The NAV at that point, when a valuation was available.
        valuation_time: The instant that NAV describes.
    """

    sequence: int
    head_hash: str
    record_count: int
    taken_at: str
    nav: str | None = None
    valuation_time: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the checkpoint as a JSON-safe mapping."""
        return {
            "store_version": STORE_VERSION,
            "sequence": self.sequence,
            "head_hash": self.head_hash,
            "record_count": self.record_count,
            "taken_at": self.taken_at,
            "nav": self.nav,
            "valuation_time": self.valuation_time,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> Checkpoint:
        """Rebuild a checkpoint from its persisted form.

        Args:
            payload: The checkpoint as written.

        Returns:
            The checkpoint.
        """
        return cls(
            sequence=int(payload["sequence"]),
            head_hash=str(payload["head_hash"]),
            record_count=int(payload["record_count"]),
            taken_at=str(payload["taken_at"]),
            nav=payload.get("nav"),
            valuation_time=payload.get("valuation_time"),
        )


class PortfolioStore:
    """Durable, append-only storage for one portfolio.

    Attributes:
        root: The `.portfolio` directory this store owns.
    """

    def __init__(self, root: Path) -> None:
        """Open a store.

        Args:
            root: The `.portfolio` directory. Created if absent.

        Raises:
            AccountingError: `FORBIDDEN_LEDGER_NAMESPACE` if the root lies in a
                research or evidence namespace.
        """
        _guard_root(root)
        self.root = root
        self._ledger_dir = root / LEDGER_DIRNAME
        self._checkpoint_dir = root / CHECKPOINT_DIRNAME
        self._reconciliation_dir = root / RECONCILIATION_DIRNAME

    def append(self, record: LedgerRecord) -> Path:
        """Persist one ledger record and verify it landed.

        Args:
            record: The committed record.

        Returns:
            Where it was written.

        Raises:
            AccountingError: `PARTIAL_EVENT_RECORD` if a record already exists at
                that sequence with different content — the append-only guarantee
                in the filesystem, not just in memory; `PERSISTENCE_VERIFY_FAILED`
                if the record read back does not match what was written.
        """
        path = self._ledger_dir / f"{record.sequence:012d}.json"
        payload = record.as_json()
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("record_hash") == record.record_hash:
                return path
            raise AccountingError(
                Code.PARTIAL_EVENT_RECORD,
                f"sequence {record.sequence} is already stored with hash "
                f"{str(existing.get('record_hash'))[:16]}…, and this record hashes to "
                f"{record.record_hash[:16]}…. Overwriting it would rewrite history, "
                "which is the one thing an append-only ledger must refuse.",
            )
        _atomic_write(path, payload)
        written = json.loads(path.read_text(encoding="utf-8"))
        if written != payload:
            raise AccountingError(
                Code.PERSISTENCE_VERIFY_FAILED,
                f"sequence {record.sequence} was written but reads back differently; "
                "the write succeeded and landed wrong, which is caught here while it "
                "is one record rather than at the next verification",
            )
        reloaded = LedgerRecord(
            sequence=int(written["sequence"]),
            event=event_from_json(written["event"]),
            event_fingerprint=str(written["event_fingerprint"]),
            previous_hash=str(written["previous_hash"]),
            record_hash=str(written["record_hash"]),
        )
        if reloaded.recompute() != record.record_hash:
            raise AccountingError(
                Code.PERSISTENCE_VERIFY_FAILED,
                f"sequence {record.sequence} does not rehash to {record.record_hash[:16]}… "
                "after a round trip through storage; the serialised form has lost "
                "something the hash depends on",
            )
        log.info("portfolio.ledger.append", sequence=record.sequence, path=str(path))
        return path

    def record_paths(self) -> tuple[Path, ...]:
        """Every stored ledger record, in sequence order."""
        if not self._ledger_dir.is_dir():
            return ()
        return tuple(sorted(self._ledger_dir.glob("*.json")))

    def load_ledger(self) -> PortfolioLedger:
        """Load and verify the stored ledger.

        Returns:
            The ledger.

        Raises:
            AccountingError: If the chain does not verify, which covers tamper,
                deletion, reordering and insertion in one check.
        """
        records: list[LedgerRecord] = []
        for path in self.record_paths():
            payload = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                LedgerRecord(
                    sequence=int(payload["sequence"]),
                    event=event_from_json(payload["event"]),
                    event_fingerprint=str(payload["event_fingerprint"]),
                    previous_hash=str(payload["previous_hash"]),
                    record_hash=str(payload["record_hash"]),
                )
            )
        return PortfolioLedger(records=records)

    def write_checkpoint(self, checkpoint: Checkpoint) -> Path:
        """Persist a checkpoint.

        Args:
            checkpoint: What to record.

        Returns:
            Where it was written.
        """
        path = self._checkpoint_dir / f"{checkpoint.sequence:012d}.json"
        _atomic_write(path, checkpoint.as_json())
        log.info(
            "portfolio.checkpoint",
            sequence=checkpoint.sequence,
            head=checkpoint.head_hash[:16],
        )
        return path

    def checkpoints(self) -> tuple[Checkpoint, ...]:
        """Every stored checkpoint, in sequence order."""
        if not self._checkpoint_dir.is_dir():
            return ()
        return tuple(
            Checkpoint.from_json(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self._checkpoint_dir.glob("*.json"))
        )

    def latest_checkpoint(self) -> Checkpoint | None:
        """The highest-sequence checkpoint, if any."""
        rows = self.checkpoints()
        return rows[-1] if rows else None

    def write_reconciliation(self, result: ReconciliationResult, *, name: str) -> Path:
        """Persist a reconciliation report.

        Reports are kept whether or not they reconciled. A break that was
        investigated and resolved is part of the account's history, and a store
        that only kept clean reconciliations would show an unblemished record of
        exactly the thing it failed to record.

        Args:
            result: The result.
            name: A filesystem-safe name for the report.

        Returns:
            Where it was written.
        """
        path = self._reconciliation_dir / f"{name}.json"
        _atomic_write(path, result.as_json())
        log.info(
            "portfolio.reconciliation",
            name=name,
            reconciled=result.reconciled,
            breaks=len(result.breaks),
        )
        return path

    def verify(self) -> dict[str, Any]:
        """Verify the store: the chain, and the ledger against its checkpoints.

        Returns:
            The verification result. `truncation_detected` is the check a hash
            chain cannot make on its own.

        Raises:
            AccountingError: `CHECKPOINT_UNVERIFIABLE` if a checkpoint names a
                sequence the ledger does not reach, or a head hash that the
                ledger's record at that sequence does not match. Both mean
                records were removed from the end, which leaves a valid — and
                shorter — chain behind.
        """
        records = tuple(self.load_ledger().records)
        chain = verify_chain(records)
        by_sequence = {row.sequence: row for row in records}
        for checkpoint in self.checkpoints():
            at = by_sequence.get(checkpoint.sequence)
            if at is None:
                raise AccountingError(
                    Code.CHECKPOINT_UNVERIFIABLE,
                    f"a checkpoint names sequence {checkpoint.sequence} with "
                    f"{checkpoint.record_count} records, but the ledger now holds "
                    f"{len(records)}. Records have been removed from the end: the "
                    "surviving chain is internally valid, which is exactly why a "
                    "checkpoint is needed to notice.",
                )
            if at.record_hash != checkpoint.head_hash:
                raise AccountingError(
                    Code.CHECKPOINT_UNVERIFIABLE,
                    f"the checkpoint at sequence {checkpoint.sequence} expects head "
                    f"{checkpoint.head_hash[:16]}… but the ledger's record there "
                    f"hashes to {at.record_hash[:16]}…; the history has been rewritten "
                    "from that point",
                )
        return {
            "store_version": STORE_VERSION,
            "root": str(self.root),
            "chain": chain,
            "record_count": len(records),
            "checkpoint_count": len(self.checkpoints()),
            "truncation_detected": False,
            "verified": bool(chain["verified"]),
        }
