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

from .anchor import AnchorStatus, LedgerHeadAnchor, classify_anchor, state_fingerprint
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
from .ledger import GENESIS_HASH, LedgerRecord, PortfolioLedger, verify_chain
from .money import as_decimal
from .versions import AccountingVersion, resolve_version

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .reconciliation import ReconciliationResult

log = structlog.get_logger(__name__)

__all__ = [
    "ANCHOR_FILENAME",
    "DEFAULT_GENERATION",
    "FORBIDDEN_ROOTS",
    "LEDGER_DIRNAME",
    "STORE_VERSION",
    "AppendResult",
    "Checkpoint",
    "PortfolioStore",
    "RecoveryOutcome",
    "event_from_json",
]

#: The storage contract.
STORE_VERSION = "PORTFOLIO_STORE_V1"

#: Where the store lives, relative to the root it is given.
LEDGER_DIRNAME = "ledger"
CHECKPOINT_DIRNAME = "checkpoints"
RECONCILIATION_DIRNAME = "reconciliation"

#: The mandatory head anchor, and the trail of the anchors it replaced.
ANCHOR_FILENAME = "head.json"
ANCHOR_TRAIL_DIRNAME = "anchors"

#: Identifies this ledger lineage. Written once when the store is established
#: and compared on every open, so an anchor from an unrelated ledger is refused
#: rather than compared — two ledgers can legitimately share a sequence number.
GENERATION_FILENAME = "generation.json"
DEFAULT_GENERATION = "PORTFOLIO_LEDGER_GENERATION_1"

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


#: Which refusal each unrecoverable anchor status raises.
_STATUS_CODES: dict[AnchorStatus, str] = {
    AnchorStatus.LEDGER_TRUNCATION_DETECTED: Code.LEDGER_TRUNCATION_DETECTED,
    AnchorStatus.LEDGER_HEAD_ANCHOR_MISMATCH: Code.LEDGER_HEAD_ANCHOR_MISMATCH,
    AnchorStatus.STATE_FINGERPRINT_MISMATCH: Code.STATE_FINGERPRINT_MISMATCH,
    AnchorStatus.GENERATION_MISMATCH: Code.LEDGER_GENERATION_MISMATCH,
    AnchorStatus.ANCHOR_MISSING: Code.ANCHOR_MISSING,
}


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


@dataclass(frozen=True)
class AppendResult:
    """What one anchored append did.

    Attributes:
        record: The committed ledger record.
        written: False when the event was already committed. A retry is a no-op
            rather than a second sequence, and says so.
        anchor: The head anchor as it stands after the append.
        path: Where the record lives.
    """

    record: LedgerRecord
    written: bool
    anchor: LedgerHeadAnchor
    path: Path

    def as_json(self) -> dict[str, Any]:
        """Return the result as a JSON-safe mapping."""
        return {
            "sequence": self.record.sequence,
            "written": self.written,
            "ledger_head": self.record.record_hash,
            "anchor": self.anchor.as_json(),
        }


@dataclass(frozen=True)
class RecoveryOutcome:
    """What opening a store found, and what it did about it.

    Attributes:
        status: How the anchor related to the ledger on open.
        recovered: Whether recovery advanced the anchor over a verified tail.
        from_sequence: Where the anchor stood before recovery.
        to_sequence: Where it stands now.
        detail: What happened, for the audit trail.
    """

    status: AnchorStatus
    recovered: bool
    from_sequence: int
    to_sequence: int
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the outcome as a JSON-safe mapping."""
        return {
            "status": str(self.status),
            "recovered": self.recovered,
            "from_sequence": self.from_sequence,
            "to_sequence": self.to_sequence,
            "detail": self.detail,
        }


class PortfolioStore:
    """Durable, append-only storage for one portfolio, with a mandatory anchor.

    The commit protocol is the substance of this class:

        write the event -> fsync -> read it back and rehash
        -> replay the state -> write the head anchor atomically -> fsync
        -> read the anchor back and verify

    An event is operationally committed only when both the record and the anchor
    are durable. That ordering is deliberate: a record without an anchor is a
    recoverable crash, while an anchor without its record would be an assertion
    about history that does not exist. The recoverable failure is the one worth
    having, so the record goes first.

    Attributes:
        root: The `.portfolio` directory this store owns.
        accounting_version: Which rules this store's ledger runs under.
        generation: The ledger lineage, so an anchor from another ledger is
            refused rather than compared.
    """

    def __init__(
        self,
        root: Path,
        *,
        accounting_version: str | AccountingVersion | None = None,
        generation: str | None = None,
        external_anchor: Any = None,
    ) -> None:
        """Open a store.

        Args:
            root: The `.portfolio` directory. Created if absent.
            accounting_version: The rules to replay under. Defaults to V1 so an
                existing store keeps its meaning.
            generation: The ledger lineage. Defaults to the stored one, or to
                `DEFAULT_GENERATION` for a new store.
            external_anchor: An optional `ExternalAnchorSink` to mirror each head
                to. Injected rather than constructed, so the accounting package
                holds no network capability of its own.

        Raises:
            AccountingError: `FORBIDDEN_LEDGER_NAMESPACE` if the root lies in a
                research or evidence namespace; `LEDGER_GENERATION_MISMATCH` if a
                generation is passed that disagrees with the stored one.
        """
        _guard_root(root)
        self.root = root
        self._ledger_dir = root / LEDGER_DIRNAME
        self._checkpoint_dir = root / CHECKPOINT_DIRNAME
        self._reconciliation_dir = root / RECONCILIATION_DIRNAME
        self._anchor_path = root / ANCHOR_FILENAME
        self._anchor_trail_dir = root / ANCHOR_TRAIL_DIRNAME
        self._generation_path = root / GENERATION_FILENAME
        self.accounting_version = str(resolve_version(accounting_version))
        self.generation = self._resolve_generation(generation)
        self._external_anchor = external_anchor

    def _resolve_generation(self, requested: str | None) -> str:
        """Settle which lineage this store describes.

        Args:
            requested: The caller's generation, if any.

        Returns:
            The lineage.

        Raises:
            AccountingError: `LEDGER_GENERATION_MISMATCH` if the caller names a
                lineage the store does not hold. Silently adopting the caller's
                name would let an anchor from one ledger validate another.
        """
        stored: str | None = None
        if self._generation_path.is_file():
            body = json.loads(self._generation_path.read_text(encoding="utf-8"))
            stored = str(body.get("generation", "")) or None
        if stored is not None and requested is not None and stored != requested:
            raise AccountingError(
                Code.LEDGER_GENERATION_MISMATCH,
                f"this store holds generation {stored!r} and was opened as "
                f"{requested!r}. Adopting the caller's name would let an anchor "
                "from one ledger validate a different one.",
            )
        return stored or requested or DEFAULT_GENERATION

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
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AccountingError(
                    Code.PARTIAL_EVENT_RECORD,
                    f"{path.name} already exists and is not valid JSON ({exc}); it is "
                    "not overwritten, because a damaged record is evidence of a "
                    "failed write rather than a free slot",
                ) from exc
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
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AccountingError(
                    Code.PARTIAL_EVENT_RECORD,
                    f"{path.name} is not valid JSON ({exc}). A record that cannot be "
                    "parsed cannot be verified or replayed, so the ledger is refused "
                    "rather than loaded without it — silently skipping it would be a "
                    "truncation this store performed on itself.",
                ) from exc
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

    # ---------------------------------------------------------------- anchor

    def read_anchor(self) -> LedgerHeadAnchor | None:
        """The stored head anchor, if any.

        Returns:
            The anchor.

        Raises:
            AccountingError: `ANCHOR_INVALID` or `ANCHOR_FINGERPRINT_MISMATCH` if
                the anchor file exists and cannot be trusted. An unreadable
                anchor is never treated as an absent one: absence means
                completeness was never asserted, while corruption means an
                assertion was made and then damaged.
        """
        if not self._anchor_path.is_file():
            return None
        return LedgerHeadAnchor.from_json(json.loads(self._anchor_path.read_text(encoding="utf-8")))

    def write_anchor(self, anchor: LedgerHeadAnchor) -> Path:
        """Persist a head anchor durably, then read it back and verify.

        Args:
            anchor: The anchor to write.

        Returns:
            Where it was written.

        Raises:
            AccountingError: `LEDGER_TRUNCATION_DETECTED` if the anchor would
                move backward — the anchor is the record of how far the ledger
                got, and lowering it would erase the evidence that something is
                missing; `PERSISTENCE_VERIFY_FAILED` if it does not read back
                intact.
        """
        existing = self.read_anchor()
        if existing is not None and anchor.sequence < existing.sequence:
            raise AccountingError(
                Code.LEDGER_TRUNCATION_DETECTED,
                f"the stored anchor witnessed sequence {existing.sequence} and this "
                f"one claims {anchor.sequence}. An anchor is never moved backward: "
                "doing so would erase the only local evidence that committed records "
                "are missing, and turn a detected truncation into a clean ledger.",
            )
        _atomic_write(self._anchor_path, anchor.as_json())
        _atomic_write(self._anchor_trail_dir / f"{anchor.sequence:012d}.json", anchor.as_json())
        if not self._generation_path.is_file():
            _atomic_write(
                self._generation_path,
                {
                    "store_version": STORE_VERSION,
                    "generation": self.generation,
                    "accounting_version": self.accounting_version,
                },
            )
        written = self.read_anchor()
        if written is None or written.fingerprint() != anchor.fingerprint():
            raise AccountingError(
                Code.PERSISTENCE_VERIFY_FAILED,
                f"the head anchor for sequence {anchor.sequence} was written but does "
                "not read back intact, so the commit cannot be considered complete",
            )
        if self._external_anchor is not None:
            self._external_anchor.publish(
                sequence=anchor.sequence,
                ledger_head=anchor.ledger_head_hash,
                anchor_fingerprint=anchor.fingerprint(),
            )
        log.info(
            "portfolio.anchor",
            sequence=anchor.sequence,
            head=anchor.ledger_head_hash[:16],
            fingerprint=anchor.fingerprint()[:16],
        )
        return self._anchor_path

    def anchor_trail(self) -> tuple[LedgerHeadAnchor, ...]:
        """Every anchor this store has written, in sequence order.

        An append-only trail of how the head advanced. It does not defend against
        a coordinated rollback — anyone rewriting the ledger and the anchor can
        rewrite the trail too — and exists for seeing the history rather than for
        proving it.
        """
        if not self._anchor_trail_dir.is_dir():
            return ()
        return tuple(
            LedgerHeadAnchor.from_json(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self._anchor_trail_dir.glob("*.json"))
        )

    def _anchor_for(self, state: Any, records: Sequence[LedgerRecord]) -> LedgerHeadAnchor:
        """Build the anchor that witnesses a replayed state.

        Args:
            state: The replayed state.
            records: The ledger's records.

        Returns:
            The anchor.
        """
        previous = self.read_anchor()
        return LedgerHeadAnchor(
            ledger_generation=self.generation,
            sequence=records[-1].sequence if records else 0,
            ledger_head_hash=records[-1].record_hash if records else GENESIS_HASH,
            state_fingerprint=state_fingerprint(state),
            engine_version=self.accounting_version,
            previous_anchor_fingerprint=previous.fingerprint() if previous else None,
        )

    def commit(self, ledger: PortfolioLedger, event: Any) -> AppendResult:
        """Append an event and anchor it, or report that it was already committed.

        This is the operational write path. The ordering is the whole point:

        1. the ledger record is written, fsynced and read back;
        2. the state is replayed from the whole verified ledger;
        3. the head anchor is written atomically, fsynced and read back.

        Only after step 3 is the event committed. A crash between 1 and 3 leaves
        the ledger one record ahead of its anchor, which `recover` resolves by
        verifying and replaying that tail — no event is applied twice, because
        the ledger is the authority for what happened and the anchor only records
        how far it got.

        Args:
            ledger: The in-memory ledger to append to.
            event: The event to commit.

        Returns:
            What was committed, with `written=False` for an already-committed id.

        Raises:
            AccountingError: `EVENT_ID_CONFLICT` for a reused id with a different
                payload, and whatever the ledger or the state machine refuses.
                The anchor is left untouched on every refusal.
        """
        from .accounting import apply_event, replay

        existed = ledger.has_event(event.event_id)
        applied = None
        if not existed:
            # Admissibility is decided before anything durable is written. The
            # ledger's own append only checks the chain, the ordering and the
            # event id; whether the event can actually be *applied* — enough
            # cash, enough of the right owner's shares, a modelled corporate
            # action — is the state machine's judgement, and it has to be made
            # first. Persisting a record the state machine will always refuse
            # would leave a ledger that can never be replayed again, which is a
            # far worse outcome than the refusal it was trying to record.
            #
            # The resulting state is kept and used for the anchor. Replaying a
            # second time would be the same arithmetic over the same events, so
            # the only thing a second pass could add is the possibility of the
            # two disagreeing.
            applied = apply_event(
                replay(ledger, accounting_version=self.accounting_version),
                event,
                sequence=ledger.next_sequence,
            )
        record = ledger.append(event)
        if existed:
            # A retry. The record is verified rather than rewritten, and the
            # anchor is checked rather than advanced: the event already counted.
            self.append(record)
            anchor = self.read_anchor()
            if anchor is None:
                anchor = self._anchor_for(
                    replay(ledger, accounting_version=self.accounting_version),
                    ledger.records,
                )
                self.write_anchor(anchor)
            return AppendResult(
                record=record,
                written=False,
                anchor=anchor,
                path=self._ledger_dir / f"{record.sequence:012d}.json",
            )

        path = self.append(record)
        assert applied is not None  # noqa: S101 - established by the branch above
        anchor = self._anchor_for(applied.state, ledger.records)
        self.write_anchor(anchor)
        return AppendResult(record=record, written=True, anchor=anchor, path=path)

    def classify(self) -> dict[str, Any]:
        """Classify the stored anchor against the stored ledger.

        Returns:
            The classification. Reads only; advances nothing.

        Raises:
            AccountingError: If the chain itself does not verify, which is an
                earlier and different failure than anything the anchor can say.
        """
        from .accounting import replay

        records = tuple(self.load_ledger().records)
        anchor = self.read_anchor()
        fingerprint: str | None = None
        if anchor is not None and anchor.sequence == (records[-1].sequence if records else 0):
            fingerprint = state_fingerprint(
                replay(records, accounting_version=self.accounting_version)
            )
        return classify_anchor(
            anchor,
            records,
            ledger_generation=self.generation,
            replayed_state_fingerprint=fingerprint,
        )

    def recover(self) -> RecoveryOutcome:
        """Resolve the anchor against the ledger on open, or fail closed.

        Returns:
            What was found and what was done.

        Raises:
            AccountingError: `LEDGER_TRUNCATION_DETECTED` when records the anchor
                witnessed are gone; `LEDGER_HEAD_ANCHOR_MISMATCH` when the
                history was rewritten in place; `STATE_FINGERPRINT_MISMATCH` when
                the same events replay to a different portfolio;
                `LEDGER_GENERATION_MISMATCH` for an anchor from another ledger;
                `ANCHOR_MISSING` for records nothing witnesses. Each fails closed
                rather than being repaired, because each means a person needs to
                decide what happened.
        """
        from .accounting import replay

        result = self.classify()
        status = AnchorStatus(str(result["status"]))
        anchor_sequence = int(result["anchor_sequence"] or 0)
        ledger_sequence = int(result["ledger_sequence"])

        if status is AnchorStatus.CONSISTENT:
            return RecoveryOutcome(
                status=status,
                recovered=False,
                from_sequence=anchor_sequence,
                to_sequence=ledger_sequence,
            )

        if status is AnchorStatus.RECOVERY_REQUIRED:
            records = tuple(self.load_ledger().records)
            state = replay(records, accounting_version=self.accounting_version)
            advanced = self._anchor_for(state, records)
            self.write_anchor(advanced)
            log.info(
                "portfolio.anchor.recovered",
                from_sequence=anchor_sequence,
                to_sequence=ledger_sequence,
            )
            return RecoveryOutcome(
                status=status,
                recovered=True,
                from_sequence=anchor_sequence,
                to_sequence=ledger_sequence,
                detail=(
                    f"the ledger held sequence {ledger_sequence} while the anchor "
                    f"witnessed {anchor_sequence}. The tail verified against the "
                    "chain and replayed cleanly, so the anchor was advanced over it. "
                    "No event was applied twice."
                ),
            )

        raise AccountingError(str(_STATUS_CODES[status]), str(result["detail"]))

    def verify(self) -> dict[str, Any]:
        """Report on the store: the chain, the checkpoints and the head anchor.

        `verified` answers P1.3's question — does the hash chain hold and do the
        checkpoints agree with it. `tail_completeness_established` answers P1.4's
        — does a durable anchor witness how far the ledger got. They are separate
        because a store written before anchors existed has the first and not the
        second, and reporting one number would have to lie about one of them.

        Enforcement lives on the write path: `commit` always writes an anchor, and
        `recover` refuses to open a store whose anchor is missing, behind, ahead
        or disagreeing.

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
        anchor_result = self.classify()
        anchor_ok = bool(anchor_result["consistent"])
        for checkpoint in self.checkpoints():
            if anchor_result["anchor_sequence"] is not None and checkpoint.sequence > int(
                anchor_result["anchor_sequence"]
            ):
                raise AccountingError(
                    Code.CHECKPOINT_UNVERIFIABLE,
                    f"a checkpoint claims sequence {checkpoint.sequence} but the head "
                    f"anchor only witnesses {anchor_result['anchor_sequence']}; a "
                    "checkpoint cannot assert more history than the anchor does",
                )
        return {
            "store_version": STORE_VERSION,
            "root": str(self.root),
            "generation": self.generation,
            "accounting_version": self.accounting_version,
            "chain": chain,
            "record_count": len(records),
            "checkpoint_count": len(self.checkpoints()),
            "anchor": anchor_result,
            "anchor_present": anchor_result["anchor_sequence"] is not None,
            "truncation_detected": (
                str(anchor_result["status"]) == str(AnchorStatus.LEDGER_TRUNCATION_DETECTED)
            ),
            "checkpoints_required_for_completeness": False,
            "coordinated_rollback_detection": "NOT_GUARANTEED_WITHOUT_EXTERNAL_ANCHOR",
            # `verified` keeps its P1.3 meaning: the chain and the checkpoints
            # hold. Tail completeness is reported separately because it is a
            # different claim — a store written before anchors existed has an
            # intact chain and no witness to its length, and conflating the two
            # would either call that store corrupt or call an unanchored ledger
            # complete. Anchored operation goes through `commit`, which always
            # writes an anchor, and through `recover`, which fails closed when one
            # is absent; this function reports rather than enforces.
            "verified": bool(chain["verified"]),
            "anchor_verified": anchor_ok,
            "tail_completeness_established": anchor_ok,
        }
