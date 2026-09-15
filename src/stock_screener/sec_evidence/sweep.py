"""Running one chunk of the universe safely, and writing down that it happened.

A chunk is not finished because a command exited zero. Phase 9K.20 established
that a harvest needs two passes — the first stores catalogues, the second expands
them into documents — so a single successful invocation leaves the chunk half
done and looking complete. This module encodes the whole sequence:

    select → preflight → pass 1 → reconcile → pass 2 → reconcile
           → converge → account → complete

and refuses to advance past any step that did not hold.

## One chunk per invocation, deliberately

There is no loop here. Thirty-four chunks remain and the decision to run them
unattended is a separate one from the decision that a single chunk is safe. An
orchestrator that advanced on its own would make that decision silently.

## The checkpoint is a record, not a source of truth

Chunk membership comes from `select_universe` and nothing else — never from the
checkpoint, never from the manifest. The checkpoint stores the *selection digest*
so a resumed run can prove it is working on the same 250 identities, and stops if
it is not. That keeps a chunk reproducible even if the record changes underneath
it: the run fails loudly rather than harvesting a different population under the
same name.

## Checkpoints are control plane; capacity is a local-backend question

A checkpoint lives under the control root, never beside the artifacts — Phase
9L.6 lost both on one drive, and the checkpoints were what would have said
which 17 batches were done. The free-space reserve, on the other hand, is a
property of a *local disk* and of nothing else: an object store has no
meaningful free bytes, so the preflight asks a backend for capacity only when
the backend can answer, and never makes it part of the storage contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from stock_screener.sec_evidence.audit import reconcile
from stock_screener.sec_evidence.control import ControlPlaneUnavailableError
from stock_screener.sec_evidence.manifest import Manifest
from stock_screener.sec_evidence.storage import StorageUnavailableError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot
    from stock_screener.sec_evidence.sample import Selection
    from stock_screener.sec_evidence.storage import EvidenceRoot

log = structlog.get_logger(__name__)

#: The free space a chunk refuses to start without.
DEFAULT_MIN_FREE_BYTES = 100 * 1024**3


class ChunkStage(StrEnum):
    """How far one chunk has got. Only `COMPLETE` means finished."""

    PENDING = "PENDING"
    PASS1_DONE = "PASS1_DONE"
    PASS2_DONE = "PASS2_DONE"
    CONVERGED = "CONVERGED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class SweepError(RuntimeError):
    """A chunk was stopped because a safety condition did not hold."""


@dataclass
class Checkpoint:
    """One chunk's durable record.

    Attributes:
        offset: Where the chunk starts in the production universe.
        limit: How many identities it covers.
        selection_digest: SHA-256 of the frozen identity list.
        selected: How many identities were selected.
        stage: How far the chunk has got.
        started_at: When the chunk was first attempted, ISO-8601 UTC.
        completed_at: When every gate passed, or None.
        requests: HTTP attempts charged, per pass.
        bytes_stored: Logical bytes stored, per pass.
        free_before: SSD free bytes at the start.
        free_after: SSD free bytes at completion.
        databases: Database digests before and after.
        accounting: Terminal state counts for the selected identities.
        records_before: Manifest lines present before this chunk fetched
            anything, which is what makes "new this chunk" answerable later.
        verification: What the tiered check actually covered.
        notes: Anything a reader would want said plainly.
    """

    offset: int
    limit: int
    selection_digest: str
    selected: int
    stage: str = ChunkStage.PENDING
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    requests: dict[str, int] = field(default_factory=dict)
    bytes_stored: dict[str, int] = field(default_factory=dict)
    free_before: int = 0
    free_after: int = 0
    databases: dict[str, str] = field(default_factory=dict)
    accounting: dict[str, int] = field(default_factory=dict)
    records_before: int = 0
    verification: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether every gate passed."""
        return self.stage == ChunkStage.COMPLETE


def checkpoint_path(control: EvidenceControlRoot, offset: int, limit: int) -> Path:
    """Where one chunk's checkpoint belongs, beneath the control root.

    A legacy `EvidenceRoot` is a control root too, so the colocated layout
    resolves to the same `checkpoints/` it always used.
    """
    return control.checkpoints / f"chunk-{offset:06d}-{limit:04d}.json"


def selection_digest(selections: Sequence[Selection]) -> str:
    """A stable digest of exactly which identities a chunk covers.

    Over the full identity tuple, not the ticker: 138 symbols exist as both a
    live row and a delisted one, so a ticker-only digest would call two different
    populations the same chunk.
    """
    joined = "\n".join(
        f"{s.symbol}\t{s.bucket}\t{s.company_id or ''}\t{s.historical_security_id or ''}"
        for s in selections
    )
    return hashlib.sha256(joined.encode()).hexdigest()


def load_checkpoint(path: Path) -> Checkpoint | None:
    """Read a chunk's checkpoint, or None when it has never been attempted.

    A checkpoint that will not parse is treated as absent and reported, rather
    than raising: a damaged record should not make the chunk unrunnable, and the
    selection digest is re-derived and re-checked on every run anyway.

    A checkpoint whose *directory* is gone is a different thing from one that
    was never written. The first means the control root is unreachable, and
    treating it as "never attempted" would restart a sweep from zero over a
    corpus that is seventeen batches in. That raises.

    Raises:
        ControlPlaneUnavailableError: If the checkpoint directory is missing.
    """
    if not path.parent.is_dir():
        message = (
            f"the checkpoint directory is gone: {path.parent} — the control root is "
            "unavailable, not a chunk that has never run"
        )
        raise ControlPlaneUnavailableError(message)
    if not path.is_file():
        return None
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("unreadable checkpoint, treating the chunk as unattempted", path=str(path))
        return None
    known = set(Checkpoint.__dataclass_fields__)
    return Checkpoint(**{k: v for k, v in payload.items() if k in known})


def save_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    """Write a checkpoint atomically.

    Temporary file in the same directory, flushed, fsynced, renamed. A crash
    leaves either the previous checkpoint or the new one, never half of either —
    the same discipline the evidence writer uses, for the same reason.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(asdict(checkpoint), stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@runtime_checkable
class LocalCapacity(Protocol):
    """A backend that can say how much room its local volume has left.

    Deliberately separate from `EvidenceStorage`. Free bytes are a property of
    a disk; an object store has none worth asking for, and putting the question
    in the artifact contract would force a remote backend to invent an answer.
    The local backend satisfies this; the preflight checks for it and skips
    the reserve when a backend does not.
    """

    def free_bytes(self) -> int:
        """Free space, in bytes, on the volume holding the artifacts."""
        ...


def free_bytes(root: EvidenceRoot) -> int:
    """Free space on the volume holding a local root. Compatibility form."""
    return root.free_bytes()


def check_local_capacity(storage: LocalCapacity, *, min_free_bytes: int) -> int:
    """Refuse to proceed if a local volume is below its reserve.

    Args:
        storage: A backend that reports local free space.
        min_free_bytes: The reserve a chunk will not eat into.

    Returns:
        The free bytes found, for the caller to record.

    Raises:
        SweepError: If space is short, or the volume cannot be asked — the
            unmounted-drive case surfaces as a `StorageUnavailableError` from
            the backend and is reported here in the sweep's own terms.
    """
    try:
        available = storage.free_bytes()
    except StorageUnavailableError as exc:
        message = (
            "the artifact root is not a directory that can report free space — "
            f"is the volume mounted? ({exc})"
        )
        raise SweepError(message) from exc
    if available < min_free_bytes:
        message = (
            f"only {available / 1024**3:.1f} GiB free on the artifact volume, "
            f"below the {min_free_bytes / 1024**3:.0f} GiB reserve"
        )
        raise SweepError(message)
    return available


def preflight_corpus(
    storage: EvidenceStorage,
    manifest: Manifest,
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    verify_digests: bool = True,
) -> None:
    """Refuse to start a pass unless the corpus, and the volume if any, are sound.

    The reserve check runs only against a backend that has a local volume to
    report on. An object-store backend is not asked and is not failed for
    having no answer; the corpus check runs against every backend alike.

    Args:
        storage: Where the artifacts live.
        manifest: The provenance log, from the control root.
        min_free_bytes: The reserve a local chunk will not eat into.
        verify_digests: Recompute digests during reconciliation.

    Raises:
        SweepError: If a local volume has gone or is short of space, or the
            existing corpus does not reconcile. A chunk that starts on a broken
            corpus cannot tell afterwards which damage it caused.
    """
    if isinstance(storage, LocalCapacity):
        check_local_capacity(storage, min_free_bytes=min_free_bytes)

    state = reconcile(storage, manifest, verify_digests=verify_digests)
    if not state.clean:
        message = f"the existing corpus does not reconcile: {state.problems}"
        raise SweepError(message)


def preflight(
    root: EvidenceRoot,
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    verify_digests: bool = True,
) -> None:
    """`preflight_corpus` for the legacy colocated layout.

    The root supplies both the local backend and the manifest beside it.
    Production wiring calls `preflight_corpus` with a control-root manifest.

    Raises:
        SweepError: As `preflight_corpus`.
    """
    preflight_corpus(
        root,
        Manifest(root.manifest_path),
        min_free_bytes=min_free_bytes,
        verify_digests=verify_digests,
    )


def guard_selection(checkpoint: Checkpoint | None, digest: str, offset: int) -> None:
    """Refuse to resume a chunk whose membership has changed.

    Raises:
        SweepError: If a stored checkpoint names a different selection. The
            chunk is defined by its identities, so a changed digest means the
            same offset now covers different securities — continuing would file
            one population's evidence under another's name.
    """
    if checkpoint is None or checkpoint.selection_digest == digest:
        return
    message = (
        f"chunk at offset {offset} was recorded with selection {checkpoint.selection_digest[:16]}… "
        f"but now selects {digest[:16]}… — the population changed underneath it"
    )
    raise SweepError(message)
