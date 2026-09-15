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
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.sec_evidence.audit import reconcile
from stock_screener.sec_evidence.manifest import EVIDENCE_MANIFEST, Manifest
from stock_screener.sec_evidence.storage import CHECKPOINTS

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def checkpoint_path(root: EvidenceRoot, offset: int, limit: int) -> Path:
    """Where one chunk's checkpoint belongs."""
    return root.path / CHECKPOINTS / f"chunk-{offset:06d}-{limit:04d}.json"


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
    """
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


def free_bytes(root: EvidenceRoot) -> int:
    """Free space on the volume holding the evidence root."""
    usage = shutil.disk_usage(str(root.path))
    return usage.free


def preflight(
    root: EvidenceRoot,
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    verify_digests: bool = True,
) -> None:
    """Refuse to start a pass unless the corpus and the volume are sound.

    Args:
        root: The verified evidence root.
        min_free_bytes: The reserve a chunk will not eat into.
        verify_digests: Recompute digests during reconciliation.

    Raises:
        SweepError: If the root has gone, space is short, or the existing corpus
            does not reconcile. A chunk that starts on a broken corpus cannot
            tell afterwards which damage it caused.
    """
    if not root.path.is_dir():
        message = f"the evidence root {root.path} is not a directory — is the volume mounted?"
        raise SweepError(message)

    available = free_bytes(root)
    if available < min_free_bytes:
        message = (
            f"only {available / 1024**3:.1f} GiB free on the evidence volume, "
            f"below the {min_free_bytes / 1024**3:.0f} GiB reserve"
        )
        raise SweepError(message)

    state = reconcile(
        root, Manifest(root.manifests / EVIDENCE_MANIFEST), verify_digests=verify_digests
    )
    if not state.clean:
        message = f"the existing corpus does not reconcile: {state.problems}"
        raise SweepError(message)


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
