"""Durable storage for the execution journal, its anchor and its reports.

One JSON file per record under `.execution/`, for the same reason the accounting
ledger chose it: a database would also offer `UPDATE`, which is the operation an
append-only journal forbids, and the guarantee would then rest on discipline
rather than on the format. Plain files also mean a journal can be inspected and
verified years later with nothing but this module.

The commit protocol applies P1.4's two hard-won lessons.

**Admissibility is decided before anything durable is written.** The journal's
own append checks the chain, the ordering and the event id; whether the event can
be *applied* — is this transition legal, would this fill overfill the order — is
the state machine's judgement, and it is made first. Persisting a record the
state machine will always refuse leaves a journal that can never be replayed
again, which is far worse than the refusal it was trying to record.

**The anchor is mandatory, not scheduled.** Every committed event updates it, so
an event without a valid anchor is `EXECUTION_RECOVERY_REQUIRED` rather than
quietly committed, and a journal shorter than its anchor is detected instead of
accepted.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from .anchor import (
    ExecutionJournalHeadAnchor,
    JournalAnchorStatus,
    classify_journal_anchor,
)
from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .journal import GENESIS_HASH, ExecutionJournal, JournalRecord, verify_journal
from .serde import event_from_json
from .versions import EXECUTION_ENGINE_VERSION

if TYPE_CHECKING:
    from .events import ExecutionEvent
    from .journal import OrderLedger

log = structlog.get_logger(__name__)

__all__ = [
    "ANCHOR_FILENAME",
    "DEFAULT_JOURNAL_GENERATION",
    "FORBIDDEN_ROOTS",
    "STORE_VERSION",
    "CommitResult",
    "ExecutionStore",
    "order_state_fingerprint",
]

#: The storage contract.
STORE_VERSION = "EXECUTION_STORE_V1"

JOURNAL_DIRNAME = "journal"
ANCHOR_TRAIL_DIRNAME = "anchors"
REPORT_DIRNAME = "reports"
ANCHOR_FILENAME = "head.json"
GENERATION_FILENAME = "generation.json"

#: Identifies a journal lineage, so an anchor from an unrelated journal is
#: refused rather than compared.
DEFAULT_JOURNAL_GENERATION = "EXECUTION_JOURNAL_GENERATION_1"

#: Path fragments an execution store may never be rooted in. Execution records
#: are operational trading records; writing them into the research evidence trees
#: would mix them into the frozen point-in-time corpus the research programme
#: rests on, and no later cleanup could reliably separate them again.
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
        ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` if the path lies in a
            research or evidence namespace.
    """
    lowered = str(root).replace("\\", "/").lower()
    for fragment in FORBIDDEN_ROOTS:
        if fragment in lowered:
            raise ExecutionError(
                Code.FORBIDDEN_EXECUTION_NAMESPACE,
                f"{root} lies in the {fragment!r} namespace. Execution records are "
                "operational trading records; writing them into the research "
                "evidence trees would mix them into the frozen point-in-time "
                "corpus, and no later cleanup could reliably separate them.",
            )


def _atomic_write(path: Path, payload: Any) -> None:
    """Write JSON durably and atomically.

    Flush, `fsync`, atomic rename, then `fsync` the directory, so a crash leaves
    either the old state or the new one and never a half-written record that
    would fail to parse and take the whole journal with it.

    Args:
        path: Where the record goes.
        payload: What to write.

    Raises:
        ExecutionError: `EXECUTION_PARTIAL_RECORD` if the write could not be made
            durable. The temporary file is removed, so a failed write leaves
            nothing that looks like a record.
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
        raise ExecutionError(
            Code.EXECUTION_PARTIAL_RECORD,
            f"{path.name} could not be written durably: {exc}. Nothing was left "
            "behind, so the journal is still whatever it was before this append.",
        ) from exc


def order_state_fingerprint(derived: OrderLedger) -> str:
    """Digest every order's derived state, for the anchor to witness.

    Covers what the events *mean* — each order's state, cumulative fill,
    remaining quantity and whether each fill has reached the book — rather than
    how they are stored. The journal's own position in history is excluded,
    because the chain already binds that.

    Args:
        derived: The replayed order state.

    Returns:
        SHA-256 over the canonical form.
    """
    body = derived.as_json()
    for operational in ("journal_sequence", "journal_head"):
        body.pop(operational, None)
    return fingerprint_of(body)


@dataclass(frozen=True)
class CommitResult:
    """What one anchored commit did.

    Attributes:
        record: The committed journal record.
        written: False when the event was already committed — the ordinary
            answer after a retry.
        anchor: The head anchor as it stands afterwards.
        path: Where the record lives.
    """

    record: JournalRecord
    written: bool
    anchor: ExecutionJournalHeadAnchor
    path: Path

    def as_json(self) -> dict[str, Any]:
        """Return the result as a JSON-safe mapping."""
        return {
            "sequence": self.record.sequence,
            "written": self.written,
            "journal_head": self.record.record_hash,
            "anchor": self.anchor.as_json(),
        }


class ExecutionStore:
    """Durable, append-only storage for one execution journal.

    Attributes:
        root: The `.execution` directory this store owns.
        generation: The journal lineage.
        execution_plan_id: Which plan this journal executes.
    """

    def __init__(
        self,
        root: Path,
        *,
        generation: str | None = None,
        execution_plan_id: str = "",
    ) -> None:
        """Open a store.

        Args:
            root: The `.execution` directory. Created if absent.
            generation: The journal lineage. Defaults to the stored one, or to
                `DEFAULT_JOURNAL_GENERATION` for a new store.
            execution_plan_id: The plan this journal executes.

        Raises:
            ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` for a research root;
                `EXECUTION_GENERATION_MISMATCH` if a generation is passed that
                disagrees with the stored one.
        """
        _guard_root(root)
        self.root = root
        self._journal_dir = root / JOURNAL_DIRNAME
        self._anchor_path = root / ANCHOR_FILENAME
        self._anchor_trail_dir = root / ANCHOR_TRAIL_DIRNAME
        self._report_dir = root / REPORT_DIRNAME
        self._generation_path = root / GENERATION_FILENAME
        self.generation = self._resolve_generation(generation)
        self.execution_plan_id = execution_plan_id

    def _resolve_generation(self, requested: str | None) -> str:
        """Settle which lineage this store describes.

        Args:
            requested: The caller's generation, if any.

        Returns:
            The lineage.

        Raises:
            ExecutionError: `EXECUTION_GENERATION_MISMATCH` if the caller names a
                lineage the store does not hold. Adopting the caller's name would
                let an anchor from one journal validate another.
        """
        stored: str | None = None
        if self._generation_path.is_file():
            body = json.loads(self._generation_path.read_text(encoding="utf-8"))
            stored = str(body.get("generation", "")) or None
        if stored is not None and requested is not None and stored != requested:
            raise ExecutionError(
                Code.EXECUTION_GENERATION_MISMATCH,
                f"this store holds generation {stored!r} and was opened as "
                f"{requested!r}. Adopting the caller's name would let an anchor "
                "from one journal validate a different one.",
            )
        return stored or requested or DEFAULT_JOURNAL_GENERATION

    def ensure_generation(self) -> Path:
        """Write this store's lineage marker if it is not already present.

        The marker is normally written alongside the first head anchor, which
        means a journal that has never recorded an event leaves nothing on disk.
        That is fine for a one-shot plan and wrong for a deployed runtime: a
        runtime binds a journal *lineage*, and an operator checking that the
        lineage exists should not have to execute a trade to find out.

        An empty journal with a declared generation and no journal at all are
        different states, and only the first is a bound runtime that has not
        traded yet.

        Returns:
            Where the marker is.
        """
        if not self._generation_path.is_file():
            _atomic_write(
                self._generation_path,
                {
                    "store_version": STORE_VERSION,
                    "engine_version": EXECUTION_ENGINE_VERSION,
                    "generation": self.generation,
                    "execution_plan_id": self.execution_plan_id,
                },
            )
        return self._generation_path

    def record_paths(self) -> tuple[Path, ...]:
        """Every stored journal record, in sequence order."""
        if not self._journal_dir.is_dir():
            return ()
        return tuple(sorted(self._journal_dir.glob("*.json")))

    def load_journal(self) -> ExecutionJournal:
        """Load and verify the stored journal.

        Returns:
            The journal.

        Raises:
            ExecutionError: `EXECUTION_PARTIAL_RECORD` for an unparseable record
                — refused rather than skipped, since skipping it would be a
                truncation the store performed on itself; whatever the chain
                verification refuses.
        """
        records: list[JournalRecord] = []
        for path in self.record_paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ExecutionError(
                    Code.EXECUTION_PARTIAL_RECORD,
                    f"{path.name} is not valid JSON ({exc}). A record that cannot "
                    "be parsed cannot be verified or replayed, so the journal is "
                    "refused rather than loaded without it.",
                ) from exc
            records.append(
                JournalRecord(
                    sequence=int(payload["sequence"]),
                    event=event_from_json(payload["event"]),
                    event_fingerprint=str(payload["event_fingerprint"]),
                    previous_hash=str(payload["previous_hash"]),
                    record_hash=str(payload["record_hash"]),
                )
            )
        return ExecutionJournal(records=records)

    def append_record(self, record: JournalRecord) -> Path:
        """Persist one journal record and verify it landed.

        Args:
            record: The committed record.

        Returns:
            Where it was written.

        Raises:
            ExecutionError: `EXECUTION_PARTIAL_RECORD` if a different record
                already occupies that sequence — the append-only guarantee in
                the filesystem, not only in memory;
                `EXECUTION_PERSISTENCE_VERIFY_FAILED` if it does not read back
                intact.
        """
        path = self._journal_dir / f"{record.sequence:012d}.json"
        payload = record.as_json()
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ExecutionError(
                    Code.EXECUTION_PARTIAL_RECORD,
                    f"{path.name} already exists and is not valid JSON ({exc}); it "
                    "is not overwritten, because a damaged record is evidence of a "
                    "failed write rather than a free slot",
                ) from exc
            if existing.get("record_hash") == record.record_hash:
                return path
            raise ExecutionError(
                Code.EXECUTION_PARTIAL_RECORD,
                f"sequence {record.sequence} is already stored with hash "
                f"{str(existing.get('record_hash'))[:16]}…, and this record hashes "
                f"to {record.record_hash[:16]}…. Overwriting it would rewrite "
                "history, which an append-only journal must refuse.",
            )
        _atomic_write(path, payload)
        written = json.loads(path.read_text(encoding="utf-8"))
        if written != payload:
            raise ExecutionError(
                Code.EXECUTION_PERSISTENCE_VERIFY_FAILED,
                f"sequence {record.sequence} was written but reads back "
                "differently; the write succeeded and landed wrong, which is "
                "caught here while it is one record",
            )
        return path

    def read_anchor(self) -> ExecutionJournalHeadAnchor | None:
        """The stored head anchor, if any.

        Returns:
            The anchor.

        Raises:
            ExecutionError: If the anchor exists and cannot be trusted. An
                unreadable anchor is never treated as an absent one: absence
                means completeness was never asserted, corruption means an
                assertion was made and then damaged.
        """
        if not self._anchor_path.is_file():
            return None
        return ExecutionJournalHeadAnchor.from_json(
            json.loads(self._anchor_path.read_text(encoding="utf-8"))
        )

    def write_anchor(self, anchor: ExecutionJournalHeadAnchor) -> Path:
        """Persist a head anchor durably, then read it back and verify.

        Args:
            anchor: The anchor.

        Returns:
            Where it was written.

        Raises:
            ExecutionError: `EXECUTION_JOURNAL_TRUNCATION_DETECTED` if the anchor
                would move backward — lowering it would erase the only local
                evidence that committed records are missing;
                `EXECUTION_PERSISTENCE_VERIFY_FAILED` if it does not read back.
        """
        existing = self.read_anchor()
        if existing is not None and anchor.sequence < existing.sequence:
            raise ExecutionError(
                Code.EXECUTION_JOURNAL_TRUNCATION_DETECTED,
                f"the stored anchor witnessed sequence {existing.sequence} and this "
                f"one claims {anchor.sequence}. An anchor is never moved backward: "
                "doing so would erase the only local evidence that committed "
                "records are missing, and turn a detected truncation into a clean "
                "journal.",
            )
        _atomic_write(self._anchor_path, anchor.as_json())
        _atomic_write(self._anchor_trail_dir / f"{anchor.sequence:012d}.json", anchor.as_json())
        if not self._generation_path.is_file():
            _atomic_write(
                self._generation_path,
                {
                    "store_version": STORE_VERSION,
                    "engine_version": EXECUTION_ENGINE_VERSION,
                    "generation": self.generation,
                    "execution_plan_id": self.execution_plan_id,
                },
            )
        written = self.read_anchor()
        if written is None or written.fingerprint() != anchor.fingerprint():
            raise ExecutionError(
                Code.EXECUTION_PERSISTENCE_VERIFY_FAILED,
                f"the head anchor for sequence {anchor.sequence} was written but "
                "does not read back intact, so the commit cannot be considered "
                "complete",
            )
        log.info(
            "execution.anchor",
            sequence=anchor.sequence,
            head=anchor.journal_head_hash[:16],
        )
        return self._anchor_path

    def anchor_trail(self) -> tuple[ExecutionJournalHeadAnchor, ...]:
        """Every anchor this store has written, in sequence order."""
        if not self._anchor_trail_dir.is_dir():
            return ()
        return tuple(
            ExecutionJournalHeadAnchor.from_json(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self._anchor_trail_dir.glob("*.json"))
        )

    def anchor_for(
        self, journal: ExecutionJournal, derived: OrderLedger
    ) -> ExecutionJournalHeadAnchor:
        """Build the anchor that witnesses a journal and its derived state."""
        previous = self.read_anchor()
        records = journal.records
        return ExecutionJournalHeadAnchor(
            journal_generation=self.generation,
            sequence=records[-1].sequence if records else 0,
            journal_head_hash=records[-1].record_hash if records else GENESIS_HASH,
            order_state_fingerprint=order_state_fingerprint(derived),
            execution_plan_id=self.execution_plan_id,
            previous_anchor_fingerprint=previous.fingerprint() if previous else None,
        )

    def commit(self, journal: ExecutionJournal, event: ExecutionEvent) -> CommitResult:
        """Append an execution event and anchor it.

        The ordering is the point:

        1. the event's admissibility is decided — nothing durable yet;
        2. the journal record is written, fsynced and read back;
        3. the head anchor is written atomically, fsynced and read back.

        Only after step 3 is the event committed. A crash between 2 and 3 leaves
        the journal one record ahead of its anchor, which recovery resolves.

        Args:
            journal: The in-memory journal.
            event: The event to commit.

        Returns:
            What was committed, with `written=False` for an already-committed id.

        Raises:
            ExecutionError: Whatever the journal or the state machine refuses.
                The journal, the anchor and the in-memory state are all left
                untouched on every refusal.
        """
        existed = journal.has_event(event.event_id)
        if not existed:
            # Decided before anything durable is written. A record the state
            # machine will always refuse would otherwise sit in the journal
            # forever, making every later replay fail.
            journal.validate(event)
        record = journal.append(event)
        path = self.append_record(record)
        derived = journal.derive()
        anchor = self.anchor_for(journal, derived)
        if existed:
            stored = self.read_anchor()
            if stored is not None:
                return CommitResult(record=record, written=False, anchor=stored, path=path)
        self.write_anchor(anchor)
        log.info(
            "execution.journal.append",
            sequence=record.sequence,
            event_type=str(event.event_type),
            written=not existed,
        )
        return CommitResult(record=record, written=not existed, anchor=anchor, path=path)

    def classify(self) -> dict[str, Any]:
        """Classify the stored anchor against the stored journal.

        Returns:
            The classification. Reads only.
        """
        journal = self.load_journal()
        records = journal.records
        anchor = self.read_anchor()
        fingerprint: str | None = None
        if anchor is not None and anchor.sequence == (records[-1].sequence if records else 0):
            fingerprint = order_state_fingerprint(journal.derive())
        return classify_journal_anchor(
            anchor,
            records,
            journal_generation=self.generation,
            derived_state_fingerprint=fingerprint,
        )

    def verify(self) -> dict[str, Any]:
        """Report on the store: the chain and the head anchor.

        Returns:
            The report. Chain integrity and tail completeness are separate
            claims, so they are reported separately.
        """
        journal = self.load_journal()
        chain = verify_journal(journal.records)
        anchor_result = self.classify()
        return {
            "store_version": STORE_VERSION,
            "engine_version": EXECUTION_ENGINE_VERSION,
            "root": str(self.root),
            "generation": self.generation,
            "chain": chain,
            "record_count": len(journal),
            "anchor": anchor_result,
            "verified": bool(chain["verified"]),
            "tail_completeness_established": bool(anchor_result["consistent"]),
            "truncation_detected": (
                str(anchor_result["status"]) == str(JournalAnchorStatus.TRUNCATION_DETECTED)
            ),
            "coordinated_rollback_detection": "NOT_GUARANTEED_WITHOUT_EXTERNAL_ANCHOR",
        }

    def write_report(self, report: Any, *, name: str) -> Path:
        """Persist an execution report.

        Args:
            report: Anything with `as_json`, or a mapping.
            name: A filesystem-safe name.

        Returns:
            Where it was written.
        """
        body = report.as_json() if hasattr(report, "as_json") else report
        path = self._report_dir / f"{name}.json"
        _atomic_write(path, body)
        return path
