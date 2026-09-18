"""The baseline rebuild: every remaining chunk, in order, then the terminal gates.

`run_chunk` proved one chunk. This module runs the rest of them the same
way — 001 through the last, one at a time, each `COMPLETE` and mirrored
before the next begins — and then closes the generation's baseline with
the checks that only make sense once: a full walk of the namespace, a
download-and-hash of every trusted artifact, a recovery proof from the
bucket alone, and a marker that says the baseline is complete.

## What is verified when

Per artifact, at write: PUT, HEAD, read-back digest (`run_chunk`).
Per chunk: every target downloaded and hashed in the replay; the chunk's
accounting and its prefixes reconciled; the chunk's control state mirrored
and read back. Nothing older is downloaded again for a later chunk.
At milestones (every fifth chunk): a structural walk — every production
object listed and compared with the trusted set, no bodies fetched — and a
recovery audit of the mirror.
At the end, once: every trusted artifact downloaded and hashed against the
manifest, full reconciliation, the final manifest snapshot mirrored, a
bucket-only recovery proof, and `BASELINE_COMPLETE`.

## Resume

Work is derived from the manifest, the checkpoints and the frozen plans,
never from a report. A chunk whose checkpoint is `COMPLETE` and whose
frozen files still hash to their digests is a no-op. Anything less is run
again through `run_chunk`, which converges what is trusted as `CACHE_HIT`
and fetches only what is not.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

import structlog

from stock_screener.sec_evidence.audit import reconcile
from stock_screener.sec_evidence.backend import (
    ArtifactStat,
    StoredArtifact,
    UnsafeStorageKeyError,
    validate_key,
)
from stock_screener.sec_evidence.generation import (
    GENERATION_ID,
    PRODUCTION_CHUNK,
    IdentityRow,
    v2_progress,
)
from stock_screener.sec_evidence.manifest import Manifest
from stock_screener.sec_evidence.mirror import ControlMirror, MirrorConflictError
from stock_screener.sec_evidence.rebuild import (
    RECOVERY_NOTE,
    CanaryStopError,
    RebuildReport,
    chunk_checkpoint_path,
    run_chunk,
)
from stock_screener.sec_evidence.storage import (
    ARTIFACT_PREFIXES,
    EvidenceIntegrityError,
    StorageUnavailableError,
)
from stock_screener.sec_evidence.sweep import ChunkStage, load_checkpoint

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot
    from stock_screener.sec_evidence.harvest import Budget, DocumentSource

log = structlog.get_logger(__name__)

T = TypeVar("T")

__all__ = [
    "BASELINE_MARKER",
    "MILESTONE_EVERY",
    "BaselineReport",
    "ChunkOutcome",
    "RetryingReads",
    "chunk_is_complete",
    "marker_facts",
    "prove_baseline_recovery",
    "run_baseline",
    "structural_reconciliation",
    "verify_frozen_prerequisites",
]

#: The control-plane marker written once the baseline's terminal gates pass.
BASELINE_MARKER = "baseline_complete.json"

#: A structural walk and a recovery audit after every this-many chunks.
MILESTONE_EVERY = 5

_CHUNK_FILES = (
    "identities.jsonl",
    "identities.sha256",
    "filing_targets.jsonl",
    "filing_targets.sha256",
)


@dataclass
class ChunkOutcome:
    """What one chunk of the baseline run did, for the operator and the report."""

    chunk: int
    stage: str
    passed: bool
    skipped: bool = False
    identities: int = 0
    resolved: int = 0
    filers: int = 0
    submissions_new: int = 0
    submissions_hit: int = 0
    filings_new: int = 0
    filings_hit: int = 0
    new_bytes: int = 0
    sec_requests: int = 0
    b2_puts: int = 0
    b2_gets: int = 0
    trusted_total: int = 0
    logical_bytes_total: int = 0
    manifest_sha256: str = ""
    elapsed_s: float = 0.0
    retries: int = 0
    failure: str | None = None

    def line(self) -> str:
        """The per-chunk progress line."""
        if self.skipped:
            return f"chunk {self.chunk:03d}: COMPLETE (no-op; frozen files verified)"
        first = self.chunk * PRODUCTION_CHUNK
        last = first + self.identities - 1
        return (
            f"chunk {self.chunk:03d}: {self.stage} positions {first}-{last} "
            f"identities {self.identities} resolved {self.resolved} filers {self.filers}; "
            f"submissions new {self.submissions_new} / hit {self.submissions_hit}; "
            f"filings new {self.filings_new} / hit {self.filings_hit}; "
            f"new bytes {self.new_bytes:,}; SEC {self.sec_requests}; "
            f"B2 PUT {self.b2_puts} GET {self.b2_gets}; "
            f"trusted {self.trusted_total:,} / {self.logical_bytes_total:,} bytes; "
            f"manifest {self.manifest_sha256[:16]}…; {self.elapsed_s:.0f} s"
            + (f"; resumed {self.retries}x after transport faults" if self.retries else "")
            + (f"; STOPPED: {self.failure}" if self.failure else "")
        )


@dataclass
class BaselineReport:
    """Everything one baseline invocation observed. No secret can be in any field."""

    generation_id: str
    start: int
    end: int
    last_chunk: int
    chunks: list[ChunkOutcome] = field(default_factory=list)
    milestones: dict[int, dict[str, object]] = field(default_factory=dict)
    terminal: dict[str, object] = field(default_factory=dict)
    b2_requests: dict[str, int] = field(default_factory=dict)
    sec_requests: int = 0
    sec_bytes: int = 0
    baseline_complete: bool = False
    failure: str | None = None
    next_incomplete: int | None = None
    elapsed_s: float = 0.0

    @property
    def passed(self) -> bool:
        """Every requested chunk completed (and the baseline closed, if the last was among them)."""
        return self.failure is None and self.next_incomplete is None

    def summary(self) -> list[str]:
        """Lines for a terminal."""
        lines = [
            f"{self.generation_id} baseline chunks {self.start:03d}-{self.end:03d} "
            f"of 000-{self.last_chunk:03d}"
        ]
        lines.extend(f"  {outcome.line()}" for outcome in self.chunks)
        for chunk, facts in sorted(self.milestones.items()):
            lines.append(f"  milestone {chunk:03d}: {facts}")
        if self.terminal:
            lines.append(f"  terminal: {self.terminal}")
        lines.append(f"  SEC requests {self.sec_requests}, bytes {self.sec_bytes:,}")
        lines.append(f"  B2 {dict(sorted(self.b2_requests.items()))}")
        lines.append(f"  BASELINE_COMPLETE: {self.baseline_complete}")
        if self.failure:
            lines.append(f"  STOPPED: {self.failure}")
        if self.next_incomplete is not None:
            lines.append(f"  next incomplete chunk: {self.next_incomplete:03d}")
        return lines


# ----------------------------------------------------------------------------
# Frozen state
# ----------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _companion_ok(path: Path) -> bool:
    companion = path.with_suffix(".sha256")
    if not path.is_file() or not companion.is_file():
        return False
    return companion.read_text(encoding="utf-8").split()[0] == _sha256_of(path)


def verify_frozen_prerequisites(control: EvidenceControlRoot) -> dict[str, str]:
    """The generation's frozen inputs, verified before any chunk beyond the first.

    The preserved ticker map must hash to its sidecar, the resolution
    snapshot to its companion, and both to what chunk 000's checkpoint
    recorded — resolution is decided once per generation, and a later chunk
    resolved from anything else would be a different generation.

    Raises:
        CanaryStopError: On any drift or absence.
    """
    generation = control.plans / "generation_v2"
    raw = generation / "prerequisites" / "company_tickers.json"
    meta = generation / "prerequisites" / "company_tickers.meta.json"
    if not raw.is_file() or not meta.is_file():
        raise CanaryStopError("the preserved ticker map is missing; refusing to fetch another")
    facts = json.loads(meta.read_text(encoding="utf-8"))
    map_sha = _sha256_of(raw)
    if map_sha != facts.get("sha256"):
        raise CanaryStopError("the preserved ticker map does not hash to its sidecar")
    resolution = generation / "filer_resolution_v1.jsonl"
    if not _companion_ok(resolution):
        raise CanaryStopError("the frozen resolution snapshot does not hash to its companion")
    resolution_sha = _sha256_of(resolution)
    first = load_checkpoint(chunk_checkpoint_path(control, 0))
    if first is None or first.stage != ChunkStage.COMPLETE:
        raise CanaryStopError("chunk 000 is not COMPLETE; the baseline starts after the canary")
    recorded = {n.split("=", 1)[0]: n.split("=", 1)[1] for n in first.notes if "=" in n}
    if recorded.get("resolution_sha256") != resolution_sha:
        raise CanaryStopError(
            f"frozen resolution drift: chunk 000 recorded "
            f"{str(recorded.get('resolution_sha256'))[:16]}…, the file is {resolution_sha[:16]}…"
        )
    return {"ticker_map_sha256": map_sha, "resolution_sha256": resolution_sha}


def _checkpoint_complete(control: EvidenceControlRoot, chunk: int) -> bool:
    """Whether the chunk's checkpoint says `COMPLETE` — the audit's notion, not the no-op's."""
    checkpoint = load_checkpoint(chunk_checkpoint_path(control, chunk))
    return checkpoint is not None and checkpoint.stage == ChunkStage.COMPLETE


def chunk_is_complete(control: EvidenceControlRoot, chunk: int) -> bool:
    """`COMPLETE`, mirror proven, frozen files intact: a no-op on rerun."""
    checkpoint = load_checkpoint(chunk_checkpoint_path(control, chunk))
    if checkpoint is None or checkpoint.stage != ChunkStage.COMPLETE:
        return False
    if RECOVERY_NOTE not in checkpoint.notes:
        return False
    chunk_dir = control.plans / "generation_v2" / "chunks" / f"{chunk:03d}"
    if not all((chunk_dir / name).is_file() for name in _CHUNK_FILES):
        return False
    if not _companion_ok(chunk_dir / "identities.jsonl"):
        return False
    if not _companion_ok(chunk_dir / "filing_targets.jsonl"):
        return False
    if checkpoint.selection_digest != _sha256_of(chunk_dir / "identities.jsonl"):
        return False
    targets_sha = _sha256_of(chunk_dir / "filing_targets.jsonl")
    return f"filing_targets_sha256={targets_sha}" in checkpoint.notes


# ----------------------------------------------------------------------------
# Reads that survive a broken stream
# ----------------------------------------------------------------------------


class RetryingReads:
    """A backend whose reads are retried on transport faults. Writes pass through.

    The terminal walk downloads tens of thousands of objects; one stream
    that breaks mid-body must not discard the hours before it. A read is
    idempotent, so retrying it is safe, and an `EvidenceIntegrityError` is
    never a read fault so it is never retried. Every write goes straight to
    the backend — a write's retry policy is the chunk resume, not this.

    Args:
        storage: The backend.
        attempts: Reads tried before the fault propagates.
        pause_s: The wait between attempts.
        sleep: How to wait; injectable for tests.
    """

    def __init__(
        self,
        storage: EvidenceStorage,
        *,
        attempts: int = 3,
        pause_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._storage = storage
        self._attempts = attempts
        self._pause = pause_s
        self._sleep = sleep
        self.retried = 0

    def _read(self, operation: str, key: str, call: Callable[[], T]) -> T:
        for attempt in range(1, self._attempts + 1):
            try:
                return call()
            except EvidenceIntegrityError:
                raise
            except StorageUnavailableError as exc:
                if attempt == self._attempts:
                    raise
                self.retried += 1
                log.warning(
                    "read retried after a transport fault",
                    operation=operation,
                    key=key,
                    attempt=attempt,
                    fault=type(exc).__name__,
                )
                self._sleep(self._pause)
        raise AssertionError("unreachable")  # pragma: no cover

    def exists(self, key: str) -> bool:
        """See `EvidenceStorage.exists`."""
        return self._read("exists", key, lambda: self._storage.exists(key))

    def stat(self, key: str) -> ArtifactStat:
        """See `EvidenceStorage.stat`."""
        return self._read("stat", key, lambda: self._storage.stat(key))

    def read_bytes(self, key: str) -> bytes:
        """See `EvidenceStorage.read_bytes`."""
        return self._read("read_bytes", key, lambda: self._storage.read_bytes(key))

    def digest(self, key: str) -> str:
        """See `EvidenceStorage.digest`."""
        return self._read("digest", key, lambda: self._storage.digest(key))

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """See `EvidenceStorage.list_prefix`. One page set, retried as a whole."""
        return iter(
            self._read("list_prefix", prefix, lambda: list(self._storage.list_prefix(prefix)))
        )

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """See `EvidenceStorage.write_bytes`. Never retried here."""
        return self._storage.write_bytes(key, data)


# ----------------------------------------------------------------------------
# Whole-namespace checks
# ----------------------------------------------------------------------------


def structural_reconciliation(storage: EvidenceStorage, manifest: Manifest) -> dict[str, int]:
    """List every production object and compare it with the trusted set. No bodies.

    The milestone form of reconciliation: orphans, missing artifacts and
    unsafe keys across the whole namespace at listing cost. Sizes and
    digests were proven per artifact when each was written and again in its
    chunk's replay; the terminal gate proves them all once more.
    """
    trusted = {e.path for e in manifest.latest_by_url().values() if e.is_stored and e.path}
    listed: set[str] = set()
    for prefix in ARTIFACT_PREFIXES:
        listed.update(storage.list_prefix(prefix))
    violations = 0
    for key in listed | trusted:
        try:
            validate_key(key)
        except UnsafeStorageKeyError:
            violations += 1
    counts = {
        "objects": len(listed),
        "trusted": len(trusted),
        "orphan_objects": len(listed - trusted),
        "missing": len(trusted - listed),
        "path_violations": violations,
    }
    log.info("structural reconciliation", **counts)
    return counts


def _required_control_keys(chunks: Sequence[int]) -> list[str]:
    keys = [
        "generation.json",
        "plans/base_identities.jsonl",
        "plans/base_identities.sha256",
        "plans/base_targets_offline.jsonl",
        "plans/base_targets_offline.sha256",
        "prerequisites/company_tickers.json",
        "prerequisites/company_tickers.meta.json",
        "resolution/filer_resolution_v1.jsonl",
        "resolution/filer_resolution_v1.sha256",
    ]
    for chunk in chunks:
        keys.extend(f"chunks/{chunk:03d}/{name}" for name in _CHUNK_FILES)
    return keys


def _recovery_audit(
    mirror: ControlMirror, control: EvidenceControlRoot, chunks: Sequence[int]
) -> dict[str, object]:
    """Lightweight: the mirror still holds what recovery needs, and its newest manifest is ours."""
    listing = set(mirror.listing())
    required = _required_control_keys(chunks)
    missing = [key for key in required if key not in listing]
    without_checkpoint = [
        chunk
        for chunk in chunks
        if not any(
            k.startswith(f"chunks/{chunk:03d}/checkpoints/") and k.endswith(".snapshot.json")
            for k in listing
        )
    ]
    newest = mirror.newest_snapshot("manifests")
    local_sha = _sha256_of(control.manifest_path)
    manifest_current = newest is not None and newest[0].get("sha256") == local_sha
    facts: dict[str, object] = {
        "mirrored_objects": len(listing),
        "required": len(required),
        "missing": missing,
        "chunks_without_checkpoint_snapshot": without_checkpoint,
        "newest_manifest_snapshot_is_current": manifest_current,
    }
    if missing or without_checkpoint or not manifest_current:
        raise CanaryStopError(f"control mirror cannot recover the generation: {facts}")
    return facts


def prove_baseline_recovery(
    mirror: ControlMirror, storage: EvidenceStorage, control: EvidenceControlRoot, chunks: int
) -> dict[str, object]:
    """Recovery from the bucket alone, after the terminal verification.

    Reads only the mirror and the bucket: the generation, the plans and
    their digests, the ticker map and its sidecar, the resolution snapshot,
    every chunk's identities and targets, the newest manifest snapshot and
    every chunk's newest checkpoint. The newest manifest snapshot must name
    exactly the objects the bucket holds and must hash to the local
    manifest — the one the terminal gate has just verified against every
    downloaded body, so the mirrored copy is verified by the same pass.

    Raises:
        CanaryStopError: If anything is missing, mis-hashed or inconsistent.
    """

    def read(relative: str) -> bytes:
        return mirror.read(relative)

    def companion_ok(relative: str) -> str:
        data = read(relative)
        expected = read(relative.rsplit(".", 1)[0] + ".sha256").decode("utf-8").split()[0]
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise CanaryStopError(f"mirrored {relative} does not hash to its companion")
        return actual

    generation = json.loads(read("generation.json"))
    companion_ok("plans/base_identities.jsonl")
    companion_ok("plans/base_targets_offline.jsonl")
    meta = json.loads(read("prerequisites/company_tickers.meta.json"))
    if hashlib.sha256(read("prerequisites/company_tickers.json")).hexdigest() != meta["sha256"]:
        raise CanaryStopError("mirrored ticker map does not hash to its sidecar")
    resolution_sha = companion_ok("resolution/filer_resolution_v1.jsonl")
    identities = 0
    for chunk in range(chunks):
        sha = companion_ok(f"chunks/{chunk:03d}/identities.jsonl")
        companion_ok(f"chunks/{chunk:03d}/filing_targets.jsonl")
        identities += sum(1 for _ in read(f"chunks/{chunk:03d}/identities.jsonl").splitlines())
        snapshot = mirror.newest_snapshot(f"chunks/{chunk:03d}/checkpoints")
        if snapshot is None:
            raise CanaryStopError(f"mirror holds no checkpoint snapshot for chunk {chunk:03d}")
        checkpoint = json.loads(snapshot[1])
        if checkpoint.get("stage") != ChunkStage.COMPLETE:
            raise CanaryStopError(f"mirrored checkpoint for chunk {chunk:03d} is not COMPLETE")
        if checkpoint.get("selection_digest") != sha:
            raise CanaryStopError(
                f"mirrored checkpoint for chunk {chunk:03d} names other identities"
            )
    newest = mirror.newest_snapshot("manifests")
    if newest is None:
        raise CanaryStopError("mirror holds no manifest snapshot")
    facts, remote_manifest = newest
    if facts["sha256"] != _sha256_of(control.manifest_path):
        raise CanaryStopError("the newest mirrored manifest snapshot is not the verified manifest")
    latest: dict[str, dict[str, object]] = {}
    for line in remote_manifest.decode("utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            latest[str(entry["url"])] = entry
    remote_trusted = {
        str(e["path"])
        for e in latest.values()
        if e.get("outcome") == "DOWNLOADED" and e.get("path")
    }
    bucket: set[str] = set()
    for prefix in ARTIFACT_PREFIXES:
        bucket.update(storage.list_prefix(prefix))
    if remote_trusted != bucket:
        raise CanaryStopError(
            f"the mirrored manifest names {len(remote_trusted)} artifacts; the bucket holds "
            f"{len(bucket)}"
        )
    return {
        "generation_id": generation.get("generation_id"),
        "universe_sha256": generation.get("universe_sha256"),
        "resolution_sha256": resolution_sha,
        "chunks": chunks,
        "identities": identities,
        "manifest_snapshot": facts["key"],
        "manifest_sha256": facts["sha256"],
        "trusted": len(remote_trusted),
        "bucket_objects": len(bucket),
        "sufficient": True,
    }


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------


def _manifest_totals(manifest: Manifest) -> tuple[int, int, int]:
    """Records, trusted artifacts and their logical bytes."""
    records = sum(1 for _ in manifest.entries())
    trusted = [e for e in manifest.latest_by_url().values() if e.is_stored]
    return records, len(trusted), sum(e.bytes or 0 for e in trusted)


def _outcome_from(
    report: RebuildReport, manifest: Manifest, elapsed: float, b2_delta: dict[str, int]
) -> ChunkOutcome:
    _, trusted, logical = _manifest_totals(manifest)
    return ChunkOutcome(
        chunk=report.chunk,
        stage=str(report.stage),
        passed=report.passed,
        identities=report.identities,
        resolved=report.resolved,
        filers=int(report.submissions.get("targets", 0)),
        submissions_new=int(report.submissions.get("downloaded", 0)),
        submissions_hit=int(report.submissions.get("cache_hit", 0)),
        filings_new=int(report.filings.get("downloaded", 0)),
        filings_hit=int(report.filings.get("cache_hit", 0)),
        new_bytes=report.sec_bytes,
        sec_requests=report.sec_requests,
        b2_puts=b2_delta.get("call:PutObject", 0),
        b2_gets=b2_delta.get("call:GetObject", 0),
        trusted_total=trusted,
        logical_bytes_total=logical,
        manifest_sha256=_sha256_of(manifest.path) if manifest.path.is_file() else "",
        elapsed_s=elapsed,
        failure=report.failure,
    )


def run_baseline(
    storage: EvidenceStorage,
    control: EvidenceControlRoot,
    documents_factory: Callable[[Budget], DocumentSource],
    identities: Sequence[IdentityRow],
    *,
    start: int,
    end: int,
    budget_factory: Callable[[], Budget],
    filings_per_security: int = 6,
    generation_id: str = GENERATION_ID,
    observe: Callable[[Callable[[str, str], None]], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    milestone_every: int = MILESTONE_EVERY,
    milestone_hook: Callable[[int], None] | None = None,
    universe_sha256: str | None = None,
    retries_per_chunk: int = 2,
    retry_delay_s: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BaselineReport:
    """Run chunks `start`..`end` in order; close the baseline after the last one.

    Args:
        storage: The artifact backend.
        control: The V2 control root.
        documents_factory: Builds a SEC client metered by a chunk's budget.
            A fresh client and budget per chunk keep the per-chunk limits
            honest.
        identities: The identity plan, all of it.
        start: First chunk. Never 0 — the canary built that one.
        end: Last chunk of this invocation, at most the plan's last chunk.
        budget_factory: A fresh per-chunk budget.
        filings_per_security: The production cap.
        generation_id: Mirror namespace and record label.
        observe: Registers one request observer for the whole run.
        progress: Receives one line per chunk and per milestone.
        milestone_every: Structural walk and recovery audit cadence.
        milestone_hook: Called at every milestone with the chunk number —
            the operator's own checks (database digests). Raise
            `CanaryStopError` to stop.
        universe_sha256: Recorded in the baseline marker.
        retries_per_chunk: How many times a chunk stopped by a *transport*
            fault — the bucket unreachable or silent past its timeouts — is
            resumed before the run stops. A resume is a full `run_chunk`
            with every pre-check, so a write that half-landed is caught as
            `REMOTE_ORPHAN` rather than repeated. Integrity stops are never
            retried.
        retry_delay_s: The pause before each resume.
        sleep: How to pause; injectable for tests.

    Returns:
        The report. `passed` means every requested chunk is `COMPLETE`; if
        the last chunk of the plan was among them, `baseline_complete` says
        whether the terminal gates passed too.
    """
    started = time.perf_counter()
    last_chunk = max(row.chunk for row in identities)
    if start < 1 or end < start or end > last_chunk:
        raise ValueError(
            f"chunk range {start:03d}-{end:03d} is not within 001-{last_chunk:03d} in order"
        )
    say = progress or (lambda line: None)
    counters: dict[str, int] = {}
    if observe is not None:

        def count(kind: str, operation: str) -> None:
            name = f"{kind}:{operation}"
            counters[name] = counters.get(name, 0) + 1

        observe(count)

    report = BaselineReport(
        generation_id=generation_id, start=start, end=end, last_chunk=last_chunk
    )
    manifest = Manifest(control.manifest_path)
    reads = RetryingReads(storage, sleep=sleep)
    mirror = ControlMirror(reads, generation_id)
    try:
        frozen = verify_frozen_prerequisites(control)
        say(
            f"frozen: ticker map {frozen['ticker_map_sha256'][:16]}…, "
            f"resolution {frozen['resolution_sha256'][:16]}…"
        )
        for chunk in range(start, end + 1):
            chunk_started = time.perf_counter()
            if chunk_is_complete(control, chunk):
                outcome = ChunkOutcome(
                    chunk=chunk, stage=ChunkStage.COMPLETE, passed=True, skipped=True
                )
                report.chunks.append(outcome)
                say(outcome.line())
            else:
                before = dict(counters)
                retries = 0
                while True:
                    chunk_report = _attempt(
                        storage,
                        control,
                        documents_factory,
                        identities,
                        chunk,
                        budget_factory,
                        filings_per_security,
                        generation_id,
                        counters,
                    )
                    report.sec_requests += chunk_report.sec_requests
                    report.sec_bytes += chunk_report.sec_bytes
                    if chunk_report.passed or not chunk_report.transient:
                        break
                    if retries >= retries_per_chunk:
                        break
                    retries += 1
                    say(
                        f"chunk {chunk:03d}: transport fault, resuming in {retry_delay_s:.0f} s "
                        f"({retries} of {retries_per_chunk}): {chunk_report.failure}"
                    )
                    sleep(retry_delay_s)
                delta = {
                    k: v - before.get(k, 0) for k, v in counters.items() if v - before.get(k, 0)
                }
                outcome = _outcome_from(
                    chunk_report, manifest, time.perf_counter() - chunk_started, delta
                )
                outcome.retries = retries
                report.chunks.append(outcome)
                say(outcome.line())
                if not chunk_report.passed:
                    report.failure = chunk_report.failure or f"chunk {chunk:03d} did not complete"
                    report.next_incomplete = chunk
                    break
            if chunk % milestone_every == 0 or chunk == end:
                report.milestones[chunk] = _milestone(
                    reads, manifest, mirror, control, chunk, report, milestone_hook, say
                )
        if report.failure is None and end == last_chunk:
            report.terminal = _terminal(
                reads, manifest, mirror, control, last_chunk + 1, universe_sha256, say
            )
            report.terminal["reads_retried"] = reads.retried
            report.baseline_complete = bool(report.terminal.get("baseline_complete"))
    except (CanaryStopError, StorageUnavailableError, MirrorConflictError) as exc:
        report.failure = f"{type(exc).__name__}: {exc}"
        if report.next_incomplete is None:
            report.next_incomplete = _first_incomplete(control, start, last_chunk)
        log.error("baseline rebuild stopped", error=type(exc).__name__)
        say(f"STOPPED: {report.failure}")
    finally:
        report.b2_requests = dict(counters)
        report.elapsed_s = time.perf_counter() - started
        mirror.write_ledger(control.plans / "generation_v2" / "mirror_ledger.jsonl")
    return report


def _attempt(
    storage: EvidenceStorage,
    control: EvidenceControlRoot,
    documents_factory: Callable[[Budget], DocumentSource],
    identities: Sequence[IdentityRow],
    chunk: int,
    budget_factory: Callable[[], Budget],
    filings_per_security: int,
    generation_id: str,
    counters: dict[str, int],
) -> RebuildReport:
    """One `run_chunk` with a fresh budget and client, the client closed afterwards."""
    budget = budget_factory()
    documents = documents_factory(budget)
    try:
        return run_chunk(
            storage,
            control,
            documents,
            identities,
            chunk=chunk,
            budget=budget,
            filings_per_security=filings_per_security,
            generation_id=generation_id,
            counters=counters,
        )
    finally:
        close = getattr(documents, "close", None)
        if callable(close):
            close()


def _first_incomplete(control: EvidenceControlRoot, start: int, last_chunk: int) -> int | None:
    for chunk in range(start, last_chunk + 1):
        if not chunk_is_complete(control, chunk):
            return chunk
    return None


def _milestone(
    storage: EvidenceStorage,
    manifest: Manifest,
    mirror: ControlMirror,
    control: EvidenceControlRoot,
    chunk: int,
    report: BaselineReport,
    hook: Callable[[int], None] | None,
    say: Callable[[str], None],
) -> dict[str, object]:
    """Structural walk, recovery audit, operator summary, operator hook."""
    structure = structural_reconciliation(storage, manifest)
    if structure["orphan_objects"] or structure["missing"] or structure["path_violations"]:
        raise CanaryStopError(f"structural reconciliation after chunk {chunk:03d}: {structure}")
    completed = [c for c in range(chunk + 1) if _checkpoint_complete(control, c)]
    recovery = _recovery_audit(mirror, control, completed)
    records, _, logical = _manifest_totals(manifest)
    _, completed_count, inconsistent = v2_progress(control)
    if inconsistent:
        raise CanaryStopError(f"inconsistent checkpoints after chunk {chunk:03d}: {inconsistent}")
    facts: dict[str, object] = {
        **structure,
        "completed_chunks": completed_count,
        "remaining_chunks": report.last_chunk + 1 - completed_count,
        "manifest_records": records,
        "logical_bytes": logical,
        "sec_requests_this_run": report.sec_requests,
        "b2_puts_this_run": counters_get(report, "call:PutObject"),
        "mirror_recovery": recovery,
    }
    if hook is not None:
        hook(chunk)
    say(f"milestone {chunk:03d}: {facts}")
    return facts


def counters_get(report: BaselineReport, name: str) -> int:
    """A counter from the report's cumulative B2 store, zero when absent."""
    return int(report.b2_requests.get(name, 0))


def _terminal(
    storage: EvidenceStorage,
    manifest: Manifest,
    mirror: ControlMirror,
    control: EvidenceControlRoot,
    chunks: int,
    universe_sha256: str | None,
    say: Callable[[str], None],
) -> dict[str, object]:
    """Every trusted artifact downloaded and hashed; reconciliation; snapshot; proof; marker."""
    say("terminal: downloading and hashing every trusted artifact")
    state = reconcile(storage, manifest, verify_digests=True)
    terminal: dict[str, object] = {
        "trusted": state.trusted,
        "objects": state.files_on_disk,
        "verified": state.trusted - state.hash_mismatches - state.orphan_records,
        "hash_mismatches": state.hash_mismatches,
        "byte_mismatches": state.byte_mismatches,
        "orphan_objects": state.orphan_files,
        "orphan_records": state.orphan_records,
        "path_violations": state.path_violations,
        "zero_byte": state.zero_byte,
        "duplicate_urls": state.duplicate_urls,
        "logical_bytes": state.logical_bytes,
        "manifest_records": state.manifest_records,
        "baseline_complete": False,
    }
    say(f"terminal: {state.summary()}")
    if not state.clean or state.zero_byte:
        raise CanaryStopError(f"terminal verification failed: {state.summary()}")
    records, trusted, logical = _manifest_totals(manifest)
    if trusted != state.trusted:
        raise CanaryStopError(f"terminal trusted {state.trusted} != manifest trusted {trusted}")
    manifest_sha = _sha256_of(manifest.path)
    mirror.mirror_snapshot(
        manifest.path,
        "manifests",
        generation_id=GENERATION_ID,
        records=records,
        trusted=trusted,
        logical_bytes=logical,
        chunks=chunks,
        label="baseline-complete",
        stage="BASELINE_COMPLETE",
        universe_sha256=universe_sha256,
    )
    recovery = prove_baseline_recovery(mirror, storage, control, chunks)
    terminal["recovery"] = recovery
    marker = control.plans / "generation_v2" / BASELINE_MARKER
    marker.write_text(
        json.dumps(
            {
                "generation_id": GENERATION_ID,
                "stage": "BASELINE_COMPLETE",
                "chunks": chunks,
                "manifest_records": records,
                "trusted": trusted,
                "logical_bytes": logical,
                "manifest_sha256": manifest_sha,
                "universe_sha256": universe_sha256,
                "resolution_sha256": recovery["resolution_sha256"],
                "terminal_verified": state.trusted,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    record = mirror.mirror(marker, BASELINE_MARKER)
    terminal["marker"] = {"key": record.key, "sha256": record.sha256, "outcome": record.outcome}
    terminal["manifest_sha256"] = manifest_sha
    terminal["completed_at"] = datetime.now(UTC).isoformat()
    terminal["baseline_complete"] = True
    say(f"BASELINE_COMPLETE: {marker.name} mirrored to {record.key} ({record.outcome})")
    return terminal


def marker_facts(control: EvidenceControlRoot) -> dict[str, object] | None:
    """The local baseline marker, when the baseline is complete."""
    marker = control.plans / "generation_v2" / BASELINE_MARKER
    if not marker.is_file():
        return None
    facts: dict[str, object] = json.loads(marker.read_text(encoding="utf-8"))
    return facts
