"""Executing the authorized Plan A3: the frozen plan, batch by batch, into the bucket.

B2.14R2 ended with a plan that is authorized and nothing downloaded. This
module consumes that plan. It does not plan: nothing here calls the planner
or the assembly, and the only decision it makes about *what* to fetch is
to refuse anything that is not the authorized plan.

## Authority is a record plus exact digests

`load_authorized_plan` reads `authoritative_a3_plan.json` beside the plan
and refuses the directory unless the record says `AUTHORIZED_FOR_EXPANSION`,
the plan hashes to the digest the caller demanded, the record names that
same digest, and no `SUPERSEDED.json` sits beside it. The newest file, the
largest plan, the most recent directory — none of those is authority.

## Batches are frozen before a byte is fetched

The plan is cut into consecutive batches in plan order, each written with
its own content digest and a record binding it to the plan, the package
and the authorization record; the set has one digest over all of them. A
resume verifies the frozen files hash as recorded and never recomputes
membership: the batch a target belongs to is decided once.

## The trust chain, per target

```text
authorized target → SEC GET → bytes, SHA-256 → B2 PUT → HEAD → body GET
    → remote SHA == fetched SHA → manifest append → checkpoint
```

The SEC side is sequential and rate-limited by the existing client. The
bucket side of several targets may be in flight at once, bounded, because
a PUT and its read-back are independent per object; the manifest is
appended in plan order, by one thread, and only after that target's
remote body has been read back and hashed. Nothing about the order of
trust is changed by the parallelism — only the wall clock.

## An object nobody recorded

An interrupted run can leave an object in the bucket with no manifest
row. On resume the target is fetched again from the SEC and the remote
body is read back; the object is adopted only when the two hash the
same and are the same length, and the manifest row then records the
fetched provenance. Different bytes at the intended key stop the run —
nothing is overwritten.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from api_clients.errors import (
    ProviderAuthError,
    ProviderBudgetError,
    ProviderError,
    ProviderRateLimitError,
)
from stock_screener.sec_evidence.backend import ArtifactNotFoundError, ArtifactStat, StoredArtifact
from stock_screener.sec_evidence.baseline import BASELINE_MARKER, structural_reconciliation
from stock_screener.sec_evidence.generation import GENERATION_ID
from stock_screener.sec_evidence.harvest import Budget, Target, key_for
from stock_screener.sec_evidence.inputs import SUPERSEDED_SIDECAR
from stock_screener.sec_evidence.manifest import Entry, Manifest, ManifestState, Outcome
from stock_screener.sec_evidence.mirror import ControlMirror
from stock_screener.sec_evidence.regenerate import A3_AUTHORITATIVE_RECORD, A3_PLAN_FILE
from stock_screener.sec_evidence.storage import (
    ARTIFACT_PREFIXES,
    EvidenceIntegrityError,
    StorageUnavailableError,
)
from stock_screener.sec_evidence.targets import ManifestTarget, load_manifest, resolve_documents

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from api_clients.sec_documents import FetchedDocument
    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot
    from stock_screener.sec_evidence.harvest import DocumentSource

log = structlog.get_logger(__name__)

__all__ = [
    "AUTHORIZED_STATUS",
    "EXPANSION_BATCH_SIZE",
    "EXPANSION_CHECKPOINT",
    "EXPANSION_DIR",
    "EXPANSION_MARKER",
    "EXPANSION_MIRROR_PREFIX",
    "MILESTONE_EVERY",
    "AuthorizedPlan",
    "BatchRecord",
    "ExpansionCheckpoint",
    "ExpansionRefusedError",
    "ExpansionReport",
    "ExpansionStage",
    "ExpansionStopError",
    "PlaneCounting",
    "acquire_batch",
    "freeze_batches",
    "load_authorized_plan",
    "load_expansion_checkpoint",
    "run_expansion",
    "save_expansion_checkpoint",
    "verify_expansion_bodies",
]

#: The statuses the authorization record must carry, both of them.
AUTHORIZED_STATUS = ("AUTHORITATIVE_V2_A3_PLAN", "AUTHORIZED_FOR_EXPANSION")
#: Targets per frozen batch.
EXPANSION_BATCH_SIZE = 1000
#: Beneath the plan directory: batches, summaries, the checkpoint, the marker.
EXPANSION_DIR = "execution"
EXPANSION_CHECKPOINT = "expansion_checkpoint.json"
EXPANSION_MARKER = "a3_expansion_complete.json"
BATCH_SET = "batch_set.json"
#: The control namespace for everything this phase mirrors.
EXPANSION_MIRROR_PREFIX = "a3-expansion-r1"
#: A structural walk of the namespace, and the caller's milestone hook, this often.
MILESTONE_EVERY = 10
#: The stage the final manifest snapshot and the marker carry.
EXPANSION_STAGE = "A3_EXPANSION_COMPLETE"


class ExpansionStage(StrEnum):
    """Where an expansion checkpoint stands."""

    PLANNED = "PLANNED"
    HARVESTING = "HARVESTING"
    EXPANSION_HARVEST_COMPLETE = "EXPANSION_HARVEST_COMPLETE"
    TERMINAL_VERIFY = "TERMINAL_VERIFY"
    COMPLETE = "COMPLETE"


class ExpansionRefusedError(RuntimeError):
    """The plan, the record or the frozen batches are not what was authorized."""


class ExpansionStopError(RuntimeError):
    """The run stopped before completion, with the verdict letter it earns.

    `B` is resumable (a transport fault, a budget); `C` is a permanent source
    failure for named targets; `D` is a remote evidence conflict; `E` is a
    baseline integrity problem. None of them is repaired here.
    """

    def __init__(self, verdict: str, message: str, targets: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.verdict = verdict
        self.targets = tuple(targets)


# ----------------------------------------------------------------------------
# Authority
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizedPlan:
    """The plan the record authorizes, with every digest that binds it.

    Attributes:
        directory: The plan directory.
        targets: The plan rows, in plan order.
        plan_sha256: SHA-256 of `plan_a3_v2.jsonl`, proven on load.
        record_sha256: SHA-256 of the authorization record, proven on load.
        record: The record's content.
        package_sha256: The frozen inputs package the plan was computed from.
        forms: Targets per form.
    """

    directory: Path
    targets: tuple[ManifestTarget, ...]
    plan_sha256: str
    record_sha256: str
    record: dict[str, object]
    package_sha256: str
    forms: dict[str, int]


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_authorized_plan(
    directory: Path,
    *,
    expect_plan_sha256: str,
    expect_rows: int | None = None,
    expect_package_sha256: str | None = None,
    expect_record_sha256: str | None = None,
) -> AuthorizedPlan:
    """Load the plan a directory holds, refusing it unless it is the authorized one.

    Args:
        directory: The plan directory.
        expect_plan_sha256: The plan digest execution is authorized for.
        expect_rows: The row count the caller requires.
        expect_package_sha256: The inputs package the record must name.
        expect_record_sha256: The authorization record's own digest, when the
            caller has it.

    Returns:
        The plan and its bindings.

    Raises:
        ExpansionRefusedError: If the directory is superseded, has no
            authorization record, the record does not authorize expansion,
            or any digest differs from what was demanded.
    """
    if (directory / SUPERSEDED_SIDECAR).exists():
        raise ExpansionRefusedError(f"{directory} is marked SUPERSEDED; it is never executed")
    record_path = directory / A3_AUTHORITATIVE_RECORD
    if not record_path.is_file():
        raise ExpansionRefusedError(f"{directory} carries no {A3_AUTHORITATIVE_RECORD}; refused")
    record_sha = _sha256_of(record_path)
    if expect_record_sha256 and record_sha != expect_record_sha256.lower():
        raise ExpansionRefusedError(
            f"the authorization record hashes to {record_sha[:16]}…, not the expected "
            f"{expect_record_sha256[:16]}…"
        )
    record: dict[str, object] = json.loads(record_path.read_text(encoding="utf-8"))
    status = record.get("status")
    if not isinstance(status, list) or any(s not in status for s in AUTHORIZED_STATUS):
        raise ExpansionRefusedError(f"{record_path} does not carry {AUTHORIZED_STATUS}; refused")
    expected = expect_plan_sha256.lower()
    if str(record.get("plan_sha256")) != expected:
        raise ExpansionRefusedError(
            f"the record authorizes plan {str(record.get('plan_sha256'))[:16]}…, "
            f"not {expected[:16]}…"
        )
    package_sha = str(record.get("package_sha256"))
    if expect_package_sha256 and package_sha != expect_package_sha256.lower():
        raise ExpansionRefusedError(
            f"the record binds package {package_sha[:16]}…, not {expect_package_sha256[:16]}…"
        )
    plan_path = directory / A3_PLAN_FILE
    companion = directory / f"{plan_path.stem}.sha256"
    if companion.is_file() and companion.read_text(encoding="utf-8").split()[:1] != [expected]:
        raise ExpansionRefusedError(f"{companion} does not name the authorized digest")
    try:
        targets, audit = load_manifest(plan_path, expect_sha256=expected, expect_rows=expect_rows)
    except Exception as exc:
        raise ExpansionRefusedError(f"the plan is not the authorized one: {exc}") from exc
    if int(str(record.get("plan_rows", len(targets)))) != len(targets):
        raise ExpansionRefusedError("the record's row count does not match the plan")
    log.info("authorized plan loaded", rows=len(targets), plan=expected[:16])
    return AuthorizedPlan(
        directory=directory,
        targets=targets,
        plan_sha256=expected,
        record_sha256=record_sha,
        record=record,
        package_sha256=package_sha,
        forms=dict(sorted(audit.forms.items())),
    )


# ----------------------------------------------------------------------------
# Batches
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchRecord:
    """One frozen batch: which rows, and the digest of their file.

    Attributes:
        batch: Zero-based batch number.
        start: First plan row (zero-based, inclusive).
        end: Last plan row (exclusive).
        count: Targets in the batch.
        sha256: SHA-256 of `batch_NNN.jsonl`.
    """

    batch: int
    start: int
    end: int
    count: int
    sha256: str

    @property
    def name(self) -> str:
        """The batch's file stem."""
        return f"batch_{self.batch:03d}"


def _batch_line(rank: int, target: Target) -> str:
    return json.dumps(
        {
            "rank": rank,
            "cik": target.cik,
            "accession": target.accession,
            "form": target.form,
            "filing_date": target.filing_date,
            "document": target.document,
            "url": target.url,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_frozen(path: Path, data: bytes) -> str:
    """Write once; a file that already exists must hold exactly these bytes."""
    digest = hashlib.sha256(data).hexdigest()
    if path.exists():
        if path.read_bytes() != data:
            raise ExpansionRefusedError(
                f"{path} exists with different content; batch membership is never regenerated"
            )
        return digest
    path.write_bytes(data)
    return digest


def freeze_batches(
    plan: AuthorizedPlan,
    resolved: Sequence[Target],
    directory: Path,
    *,
    batch_size: int = EXPANSION_BATCH_SIZE,
    generation_id: str = GENERATION_ID,
) -> tuple[list[BatchRecord], str]:
    """Cut the plan into consecutive batches and write them, once.

    Each batch is a JSONL file of its targets in plan order — rank, identity,
    form, date, document and URL — with a record beside it binding the batch
    to the plan, the package and the authorization record. `batch_set.json`
    lists every batch and carries one digest over all their digests. Files
    that already exist must be byte-identical: a resume never re-cuts.

    Args:
        plan: The authorized plan.
        resolved: The plan's targets with their documents and URLs, in plan order.
        directory: The execution directory.
        batch_size: Targets per batch.
        generation_id: Recorded in every file.

    Returns:
        The batch records and the batch-set digest.

    Raises:
        ExpansionRefusedError: If an existing frozen file differs.
    """
    if len(resolved) != len(plan.targets):
        raise ExpansionRefusedError("every plan row must resolve to exactly one target")
    batches_dir = directory / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    records: list[BatchRecord] = []
    for number, start in enumerate(range(0, len(resolved), batch_size)):
        end = min(start + batch_size, len(resolved))
        lines = [_batch_line(start + i + 1, t) for i, t in enumerate(resolved[start:end])]
        content = ("\n".join(lines) + "\n").encode("utf-8")
        record = BatchRecord(number, start, end, end - start, hashlib.sha256(content).hexdigest())
        _write_frozen(batches_dir / f"{record.name}.jsonl", content)
        _write_frozen(
            batches_dir / f"{record.name}.json",
            _json_bytes(
                {
                    "generation_id": generation_id,
                    "package_sha256": plan.package_sha256,
                    "plan_sha256": plan.plan_sha256,
                    "record_sha256": plan.record_sha256,
                    "batch": record.batch,
                    "start_row": record.start,
                    "end_row": record.end - 1,
                    "targets": record.count,
                    "content_sha256": record.sha256,
                }
            ),
        )
        records.append(record)
    set_sha = hashlib.sha256(
        "".join(f"{r.sha256}  {r.name}\n" for r in records).encode("utf-8")
    ).hexdigest()
    _write_frozen(
        directory / BATCH_SET,
        _json_bytes(
            {
                "generation_id": generation_id,
                "package_sha256": plan.package_sha256,
                "plan_sha256": plan.plan_sha256,
                "record_sha256": plan.record_sha256,
                "batch_size": batch_size,
                "batches": [asdict(r) for r in records],
                "batch_set_sha256": set_sha,
            }
        ),
    )
    log.info("expansion batches frozen", batches=len(records), set=set_sha[:16])
    return records, set_sha


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _verify_frozen(directory: Path, records: Sequence[BatchRecord]) -> None:
    for record in records:
        actual = _sha256_of(directory / "batches" / f"{record.name}.jsonl")
        if actual != record.sha256:
            raise ExpansionRefusedError(f"{record.name}.jsonl no longer hashes as frozen")


# ----------------------------------------------------------------------------
# Checkpoint
# ----------------------------------------------------------------------------


@dataclass
class ExpansionCheckpoint:
    """The durable state of one expansion, bound to everything it executes.

    Progress is the list of completed batches. A target's trust is never
    read from here — the manifest says what is trusted; this says which
    batches have been fully accounted for so a resume can skip them.
    """

    generation_id: str
    baseline_manifest_sha256: str
    baseline_marker_sha256: str
    package_sha256: str
    plan_sha256: str
    record_sha256: str
    batch_set_sha256: str
    batch_size: int
    batches: int
    stage: str = ExpansionStage.PLANNED
    completed_batches: list[int] = field(default_factory=list)
    records_before: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    terminal: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def binds(self, other: ExpansionCheckpoint) -> list[str]:
        """The binding fields on which `other` differs from this checkpoint."""
        names = (
            "generation_id",
            "baseline_manifest_sha256",
            "baseline_marker_sha256",
            "package_sha256",
            "plan_sha256",
            "record_sha256",
            "batch_set_sha256",
            "batch_size",
            "batches",
        )
        return [n for n in names if getattr(self, n) != getattr(other, n)]


def load_expansion_checkpoint(path: Path) -> ExpansionCheckpoint | None:
    """Read a checkpoint, or None when there is none yet."""
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ExpansionCheckpoint(**payload)


def save_expansion_checkpoint(path: Path, checkpoint: ExpansionCheckpoint) -> str:
    """Write the checkpoint atomically and return its digest."""
    checkpoint.updated_at = datetime.now(UTC).isoformat()
    data = _json_bytes(asdict(checkpoint))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------
# Counting the two planes apart
# ----------------------------------------------------------------------------


class PlaneCounting:
    """Count every storage call by plane — evidence or control — and operation.

    The backend's own observer sees operations, not keys. This wrapper sees
    the key, so `evidence:PUT` and `control:GET` are separate numbers and the
    report can say that no evidence prefix was read for anything but the
    targets it wrote.
    """

    def __init__(self, inner: EvidenceStorage) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}

    def _count(self, key: str, operation: str) -> None:
        plane = "evidence" if key.split("/", 1)[0] in ARTIFACT_PREFIXES else "control"
        name = f"{plane}:{operation}"
        with self._lock:
            self.counts[name] = self.counts.get(name, 0) + 1

    def exists(self, key: str) -> bool:
        """Count a HEAD and delegate."""
        self._count(key, "HEAD")
        return self._inner.exists(key)

    def stat(self, key: str) -> ArtifactStat:
        """Count a HEAD and delegate."""
        self._count(key, "HEAD")
        return self._inner.stat(key)

    def read_bytes(self, key: str) -> bytes:
        """Count a GET and delegate."""
        self._count(key, "GET")
        return self._inner.read_bytes(key)

    def digest(self, key: str) -> str:
        """Count a GET and delegate."""
        self._count(key, "GET")
        return self._inner.digest(key)

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """Count a LIST and delegate."""
        self._count(prefix, "LIST")
        return self._inner.list_prefix(prefix)

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """Count a PUT and the HEAD the backend verifies it with, and delegate."""
        self._count(key, "PUT")
        self._count(key, "HEAD")
        return self._inner.write_bytes(key, data)


# ----------------------------------------------------------------------------
# One batch
# ----------------------------------------------------------------------------


@dataclass
class BatchOutcome:
    """What acquiring one batch did."""

    targets: int = 0
    cache_hits: int = 0
    downloaded: int = 0
    adopted: int = 0
    failed: int = 0
    trusted: int = 0
    source_bytes: int = 0
    sec_attempts: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Stored:
    adopted: bool
    bytes: int


#: Bounded retries for the bucket side of one target, and the pause between them.
WRITE_ATTEMPTS = 3
WRITE_PAUSE_S = 5.0


def _adopt_if_identical(
    storage: EvidenceStorage, key: str, fetched: FetchedDocument, *, why: str
) -> _Stored:
    """Read the object at `key` back and adopt it only if it is the SEC body byte for byte.

    Raises:
        ExpansionStopError: `D` when the object holds other bytes.
    """
    remote = storage.read_bytes(key)
    if len(remote) != len(fetched.body) or hashlib.sha256(remote).hexdigest() != fetched.sha256:
        raise ExpansionStopError(
            "D",
            f"{key} exists in the bucket with other bytes than the SEC serves; not overwritten",
            [fetched.url],
        )
    log.warning("adopting an object whose upload was not recorded", key=key, why=why)
    return _Stored(adopted=True, bytes=len(remote))


def _store_and_verify(
    storage: EvidenceStorage,
    key: str,
    fetched: FetchedDocument,
    *,
    attempts: int = WRITE_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> _Stored:
    """The bucket side of one target: adopt, or PUT + HEAD + body GET; never overwrite.

    A transport fault on the PUT or on the HEAD that verifies it leaves the
    object in an unknown state, so the next attempt asks the bucket first:
    an object that is there and is the SEC body is adopted, one that is not
    there is uploaded again, and one holding other bytes stops the run. The
    bucket reporting a different length or digest for what it accepted is
    never retried.

    Raises:
        ExpansionStopError: `D` when the key holds other bytes, or the bytes
            read back after the upload are not the bytes that were sent.
        StorageUnavailableError: When every attempt met a transport fault.
    """
    last: StorageUnavailableError | None = None
    for attempt in range(1, attempts + 1):
        if storage.exists(key):
            return _adopt_if_identical(
                storage, key, fetched, why="present before the upload" if last is None else "put"
            )
        try:
            stored = storage.write_bytes(key, fetched.body)
        except EvidenceIntegrityError:
            raise
        except StorageUnavailableError as exc:
            last = exc
            log.warning(
                "upload not confirmed; asking the bucket again",
                key=key,
                attempt=attempt,
                fault=type(exc).__name__,
            )
            if attempt < attempts:
                sleep(WRITE_PAUSE_S)
            continue
        readback = storage.digest(key)
        if readback != fetched.sha256:
            raise ExpansionStopError(
                "D",
                f"{key} read back as {readback[:16]}… after an upload of {fetched.sha256[:16]}…",
                [fetched.url],
            )
        return _Stored(adopted=False, bytes=stored.bytes)
    if last is None:  # pragma: no cover — the loop returned or recorded a fault
        raise StorageUnavailableError(f"{key}: no upload attempt was made")
    raise last


def _failed_entry(target: Target, error: str, attempt: int, status: int | None = None) -> Entry:
    return Entry(
        url=target.url,
        outcome=Outcome.FAILED,
        status=status,
        request_kind=str(target.kind),
        cik=target.cik,
        accession=target.accession,
        form=target.form,
        filing_date=target.filing_date,
        error=error,
        attempt=attempt,
    )


def _downloaded_entry(
    target: Target, fetched: FetchedDocument, stored: _Stored, attempt: int
) -> Entry:
    return Entry(
        url=target.url,
        outcome=Outcome.DOWNLOADED,
        status=fetched.status,
        sha256=fetched.sha256,
        bytes=stored.bytes,
        retrieved_at=fetched.retrieved_at.isoformat(),
        path=key_for(target),
        request_kind=str(target.kind),
        content_type=fetched.content_type,
        etag=fetched.etag,
        last_modified=fetched.last_modified,
        cik=target.cik,
        accession=target.accession,
        form=target.form,
        filing_date=target.filing_date,
        error="adopted: object present before this run, identical to the SEC body"
        if stored.adopted
        else None,
        attempt=attempt,
    )


def acquire_batch(
    targets: Sequence[Target],
    storage: EvidenceStorage,
    manifest: Manifest,
    documents: DocumentSource,
    budget: Budget,
    *,
    workers: int = 4,
    state: ManifestState | None = None,
) -> BatchOutcome:
    """Acquire one batch through the trust chain; append in plan order; stop on refusal.

    A target the manifest already trusts costs one HEAD (the object is there
    at the recorded size) and no SEC request. Anything else is fetched from
    the SEC sequentially, handed to a bounded pool for the bucket side, and
    recorded in the manifest — in plan order — once its remote body has
    been read back and hashed. A failed request is recorded `FAILED` and
    reported; the caller decides whether to retry the batch.

    Args:
        targets: The batch, in plan order.
        storage: The bucket.
        manifest: The trust ledger.
        documents: The SEC client, metered by `budget`.
        budget: Request and byte limits for this batch.
        workers: Bucket-side operations in flight at once.
        state: A replayed manifest, when the caller has one.

    Returns:
        The batch's accounting.

    Raises:
        ExpansionStopError: `B` when the SEC refuses the caller (401/403/429
            on the final attempt) or the budget is spent — resumable; `D` on a
            remote conflict.
    """
    known = state if state is not None else manifest.replay()
    outcome = BatchOutcome(targets=len(targets))
    pending: list[tuple[Target, str]] = []
    for target in targets:
        key = key_for(target)
        entry = known.latest.get(target.url)
        if entry is not None and entry.is_stored:
            try:
                size = storage.stat(key).bytes
            except ArtifactNotFoundError as exc:
                raise ExpansionStopError(
                    "D",
                    f"{key} is trusted by the manifest but absent from the bucket",
                    [target.url],
                ) from exc
            if entry.bytes is not None and size != entry.bytes:
                raise ExpansionStopError(
                    "D", f"{key} is {size} bytes in the bucket, {entry.bytes} in the manifest"
                )
            outcome.cache_hits += 1
            continue
        pending.append((target, key))

    in_flight: deque[tuple[Target, FetchedDocument, int, Future[_Stored]]] = deque()
    stop: ExpansionStopError | None = None
    attempts_before = budget.requests_made

    def settle(item: tuple[Target, FetchedDocument, int, Future[_Stored]]) -> None:
        target, fetched, attempt, future = item
        stored = future.result()
        entry = _downloaded_entry(target, fetched, stored, attempt)
        manifest.append(entry)
        known.record(entry)
        budget.spend(stored=stored.bytes)
        outcome.downloaded += 1
        outcome.adopted += int(stored.adopted)
        outcome.source_bytes += stored.bytes

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        try:
            for target, key in pending:
                if budget.exhausted:
                    stop = ExpansionStopError("B", "the batch budget was spent; resume later")
                    break
                attempt = known.next_attempt(target.url)
                try:
                    fetched = documents.fetch(target.url)
                except ProviderBudgetError:
                    stop = ExpansionStopError("B", "the SEC request budget was spent; resume later")
                    break
                except ProviderError as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    entry = _failed_entry(target, reason, attempt)
                    manifest.append(entry)
                    known.record(entry)
                    outcome.failed += 1
                    outcome.failures.append((target.url, reason))
                    if isinstance(exc, ProviderAuthError | ProviderRateLimitError):
                        stop = ExpansionStopError(
                            "B", f"SEC refused the caller ({type(exc).__name__}) at {target.url}"
                        )
                        break
                    continue
                if fetched.bytes_received == 0:
                    reason = f"HTTP {fetched.status} returned an empty body"
                    entry = _failed_entry(target, reason, attempt, fetched.status)
                    manifest.append(entry)
                    known.record(entry)
                    outcome.failed += 1
                    outcome.failures.append((target.url, reason))
                    continue
                if budget.would_exceed(fetched.bytes_received):
                    stop = ExpansionStopError("B", "the batch byte budget would be exceeded")
                    break
                in_flight.append(
                    (
                        target,
                        fetched,
                        attempt,
                        pool.submit(_store_and_verify, storage, key, fetched),
                    )
                )
                while len(in_flight) >= max(1, workers):
                    settle(in_flight.popleft())
        finally:
            # Whatever reached the bucket is recorded before anything is raised:
            # a settled target is trusted, an unsettled one is an orphan the next
            # run adopts, and nothing in between is invented.
            while in_flight:
                item = in_flight.popleft()
                try:
                    settle(item)
                except ExpansionStopError as exc:
                    stop = stop or exc
                except EvidenceIntegrityError as exc:
                    stop = stop or ExpansionStopError("D", f"{type(exc).__name__}: {exc}")
                except (StorageUnavailableError, OSError) as exc:
                    stop = stop or ExpansionStopError("B", f"{type(exc).__name__}: {exc}")
    outcome.sec_attempts = budget.requests_made - attempts_before
    outcome.trusted = sum(
        1 for t in targets if (e := known.latest.get(t.url)) is not None and e.is_stored
    )
    if stop is not None:
        raise stop
    return outcome


# ----------------------------------------------------------------------------
# Terminal verification
# ----------------------------------------------------------------------------


def verify_expansion_bodies(
    storage: EvidenceStorage,
    entries: Sequence[Entry],
    *,
    workers: int = 4,
    progress: Callable[[int], None] | None = None,
) -> dict[str, int]:
    """Download every named artifact and hash it against its record. Bodies, not HEADs.

    Returns:
        `{expected, verified, bytes, digest_mismatches, size_mismatches, read_failures}`.
    """
    counts = {
        "expected": len(entries),
        "verified": 0,
        "bytes": 0,
        "digest_mismatches": 0,
        "size_mismatches": 0,
        "read_failures": 0,
    }

    def check(entry: Entry) -> tuple[str, int]:
        try:
            data = storage.read_bytes(entry.path or "")
        except (ArtifactNotFoundError, StorageUnavailableError):
            return "read_failures", 0
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            return "digest_mismatches", len(data)
        if entry.bytes is not None and len(data) != entry.bytes:
            return "size_mismatches", len(data)
        return "verified", len(data)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for kind, size in pool.map(check, entries):
            counts[kind] += 1
            counts["bytes"] += size
            done += 1
            if progress is not None and done % 1000 == 0:
                progress(done)
    return counts


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------


@dataclass
class ExpansionReport:
    """What one invocation did, for the operator and the final report."""

    stage: str = ExpansionStage.PLANNED
    batches: int = 0
    completed_batches: int = 0
    batch_lines: list[str] = field(default_factory=list)
    batch_elapsed: dict[str, float] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    terminal: dict[str, object] = field(default_factory=dict)
    recovery: dict[str, object] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    mirror: dict[str, int] = field(default_factory=dict)
    failure: str | None = None
    verdict: str = "A"
    failed_targets: tuple[str, ...] = ()
    marker_sha256: str | None = None
    final_manifest_sha256: str | None = None

    @property
    def passed(self) -> bool:
        """Whether the expansion is COMPLETE."""
        return self.failure is None and self.stage == ExpansionStage.COMPLETE


@dataclass
class _Context:
    storage: PlaneCounting
    control: EvidenceControlRoot
    manifest: Manifest
    mirror: ControlMirror
    plan: AuthorizedPlan
    resolved: tuple[Target, ...]
    records: list[BatchRecord]
    execution: Path
    checkpoint_path: Path
    checkpoint: ExpansionCheckpoint
    report: ExpansionReport
    say: Callable[[str], None]
    documents_factory: Callable[[Budget], DocumentSource]
    budget_factory: Callable[[], Budget]
    workers: int
    milestone_hook: Callable[[int], None] | None
    generation_id: str


def _say(progress: Callable[[str], None] | None) -> Callable[[str], None]:
    def say(line: str) -> None:
        log.info(line)
        if progress is not None:
            progress(line)

    return say


def _baseline_binding(
    control: EvidenceControlRoot,
    mirror: ControlMirror,
    *,
    expect_marker_sha256: str,
    expect_manifest_sha256: str,
    expect_trusted: int,
) -> dict[str, object]:
    """The B2.11 marker, locally and as mirrored; refused unless it says what it must."""
    marker = control.plans / "generation_v2" / BASELINE_MARKER
    if not marker.is_file():
        raise ExpansionRefusedError("no baseline marker; the baseline is not complete")
    local_sha = _sha256_of(marker)
    if local_sha != expect_marker_sha256.lower():
        raise ExpansionRefusedError(
            f"the baseline marker hashes to {local_sha[:16]}…, not as expected"
        )
    facts: dict[str, object] = json.loads(marker.read_text(encoding="utf-8"))
    problems = []
    if facts.get("stage") != "BASELINE_COMPLETE":
        problems.append("stage")
    if facts.get("manifest_sha256") != expect_manifest_sha256.lower():
        problems.append("manifest_sha256")
    if int(str(facts.get("trusted", 0))) != expect_trusted:
        problems.append("trusted")
    if int(str(facts.get("terminal_verified", 0))) != expect_trusted:
        problems.append("terminal_verified")
    if problems:
        raise ExpansionRefusedError(f"the baseline marker does not bind: {problems}")
    remote = mirror.read(BASELINE_MARKER)
    remote_sha = hashlib.sha256(remote).hexdigest()
    if remote_sha != local_sha:
        raise ExpansionRefusedError("the mirrored baseline marker differs from the local one")
    return {**facts, "marker_sha256": local_sha}


def _recoverable_inputs(context: _Context) -> dict[str, str]:
    """The corrected package and the authorization record, read back from the bucket."""
    inputs_kind = "a3-inputs-r1/planning_inputs_manifest-json"
    got = context.mirror.newest_snapshot(inputs_kind)
    if got is None:
        raise ExpansionRefusedError("the corrected package manifest is not in the mirror")
    package = json.loads(got[1].decode("utf-8"))
    if str(package.get("package_sha256")) != context.plan.package_sha256:
        raise ExpansionRefusedError("the mirrored package manifest names another package")
    got = context.mirror.newest_snapshot(f"a3-r1/{A3_AUTHORITATIVE_RECORD.replace('.', '-')}")
    if got is None:
        raise ExpansionRefusedError("the authorization record is not in the mirror")
    if hashlib.sha256(got[1]).hexdigest() != context.plan.record_sha256:
        raise ExpansionRefusedError("the mirrored authorization record differs from the local one")
    return {
        "package_sha256": context.plan.package_sha256,
        "record_sha256": context.plan.record_sha256,
        "plan_sha256": context.plan.plan_sha256,
    }


def run_expansion(
    storage: EvidenceStorage,
    control: EvidenceControlRoot,
    documents_factory: Callable[[Budget], DocumentSource],
    plan: AuthorizedPlan,
    catalogue: sqlite3.Connection,
    *,
    expect_baseline_marker_sha256: str,
    expect_baseline_manifest_sha256: str,
    expect_baseline_trusted: int,
    budget_factory: Callable[[], Budget] | None = None,
    batch_size: int = EXPANSION_BATCH_SIZE,
    workers: int = 4,
    progress: Callable[[str], None] | None = None,
    milestone_hook: Callable[[int], None] | None = None,
    generation_id: str = GENERATION_ID,
    stop_after_batches: int | None = None,
) -> ExpansionReport:
    """Execute the authorized plan end to end, resumably, and report.

    Args:
        storage: The bucket.
        control: The V2 control root; its manifest is the trust ledger.
        documents_factory: Builds a SEC client metered by the batch budget.
        plan: The authorized plan, already proven.
        catalogue: The V2 derived index, read-only, for document names and URLs.
        expect_baseline_marker_sha256: The B2.11 marker's digest.
        expect_baseline_manifest_sha256: The baseline manifest digest the marker must name.
        expect_baseline_trusted: The baseline's trusted count.
        budget_factory: A fresh SEC budget per batch.
        batch_size: Targets per frozen batch.
        workers: Bucket-side operations in flight per batch.
        progress: Where lines for the operator go.
        milestone_hook: Called with the batch number every `MILESTONE_EVERY`
            batches and at the end — the caller's database digest check.
        generation_id: The generation.
        stop_after_batches: Stop cleanly after this many batches complete in
            this invocation, leaving the checkpoint resumable.

    Returns:
        The report. `passed` only when the checkpoint reaches COMPLETE.
    """
    say = _say(progress)
    counting = PlaneCounting(storage)
    mirror = ControlMirror(counting, generation_id)
    execution = plan.directory / EXPANSION_DIR
    execution.mkdir(parents=True, exist_ok=True)
    report = ExpansionReport()
    manifest = Manifest(control.manifest_path)
    try:
        baseline = _baseline_binding(
            control,
            mirror,
            expect_marker_sha256=expect_baseline_marker_sha256,
            expect_manifest_sha256=expect_baseline_manifest_sha256,
            expect_trusted=expect_baseline_trusted,
        )
        say(f"baseline: BASELINE_COMPLETE {baseline['trusted']} trusted, marker mirrored and equal")
        resolved, unresolved = resolve_documents(plan.targets, catalogue)
        if unresolved:
            raise ExpansionRefusedError(
                f"{len(unresolved)} authorized targets have no catalogue document; refused"
            )
        records, set_sha = freeze_batches(
            plan, resolved, execution, batch_size=batch_size, generation_id=generation_id
        )
        _verify_frozen(execution, records)
        checkpoint_path = execution / EXPANSION_CHECKPOINT
        fresh = ExpansionCheckpoint(
            generation_id=generation_id,
            baseline_manifest_sha256=expect_baseline_manifest_sha256.lower(),
            baseline_marker_sha256=str(baseline["marker_sha256"]),
            package_sha256=plan.package_sha256,
            plan_sha256=plan.plan_sha256,
            record_sha256=plan.record_sha256,
            batch_set_sha256=set_sha,
            batch_size=batch_size,
            batches=len(records),
            records_before=sum(1 for _ in manifest.entries()),
        )
        existing = load_expansion_checkpoint(checkpoint_path)
        if existing is not None:
            drift = existing.binds(fresh)
            if drift:
                raise ExpansionRefusedError(f"the checkpoint binds other inputs: {drift}")
            checkpoint = existing
        else:
            if _sha256_of(manifest.path) != expect_baseline_manifest_sha256.lower():
                raise ExpansionRefusedError(
                    "a first expansion run must start from the baseline manifest exactly"
                )
            checkpoint = fresh
        context = _Context(
            storage=counting,
            control=control,
            manifest=manifest,
            mirror=mirror,
            plan=plan,
            resolved=tuple(resolved),
            records=records,
            execution=execution,
            checkpoint_path=checkpoint_path,
            checkpoint=checkpoint,
            report=report,
            say=say,
            documents_factory=documents_factory,
            budget_factory=budget_factory
            or (lambda: Budget(max_requests=2500, max_bytes=4 * 1024**3)),
            workers=workers,
            milestone_hook=milestone_hook,
            generation_id=generation_id,
        )
        report.batches = len(records)
        _recoverable_inputs(context)
        say(
            f"recoverable: package {plan.package_sha256[:16]}… and record read back from the mirror"
        )
        _mirror_batches(context)
        _save(context)
        _harvest(context, stop_after_batches)
        if checkpoint.stage == ExpansionStage.EXPANSION_HARVEST_COMPLETE or (
            checkpoint.stage == ExpansionStage.TERMINAL_VERIFY
        ):
            _terminal(context)
    except ExpansionStopError as exc:
        report.failure = f"{exc.verdict}: {exc}"
        report.verdict = exc.verdict
        report.failed_targets = exc.targets
        log.error("expansion stopped", verdict=exc.verdict, error=str(exc))
    except ExpansionRefusedError as exc:
        report.failure = f"REFUSED: {exc}"
        report.verdict = "FAIL"
        log.error("expansion refused", error=str(exc))
    except EvidenceIntegrityError as exc:
        report.failure = f"D: {type(exc).__name__}: {exc}"
        report.verdict = "D"
        log.error("expansion stopped on an integrity fault", error=str(exc))
    except (StorageUnavailableError, OSError) as exc:
        report.failure = f"B: {type(exc).__name__}: {exc}"
        report.verdict = "B"
        log.error("expansion interrupted by transport", error=type(exc).__name__)
    finally:
        report.counters = dict(sorted(counting.counts.items()))
        report.mirror = {
            **mirror.report.counts(),
            "puts": mirror.report.puts,
            "reads": mirror.report.reads,
        }
        mirror.write_ledger(control.plans / "generation_v2" / "mirror_ledger.jsonl")
    return report


def _save(context: _Context, label: str | None = None) -> None:
    """Checkpoint to disk, then to the mirror as a snapshot."""
    digest = save_expansion_checkpoint(context.checkpoint_path, context.checkpoint)
    context.mirror.mirror_snapshot(
        context.checkpoint_path,
        f"{EXPANSION_MIRROR_PREFIX}/checkpoints",
        generation_id=context.generation_id,
        stage=str(context.checkpoint.stage),
        completed=len(context.checkpoint.completed_batches),
        label=label or str(context.checkpoint.stage),
        sha256=digest,
    )
    context.report.stage = str(context.checkpoint.stage)
    context.report.completed_batches = len(context.checkpoint.completed_batches)


def _mirror_batches(context: _Context) -> None:
    for record in context.records:
        for suffix in (".jsonl", ".json"):
            local = context.execution / "batches" / f"{record.name}{suffix}"
            context.mirror.mirror(local, f"{EXPANSION_MIRROR_PREFIX}/batches/{record.name}{suffix}")
    context.mirror.mirror(context.execution / BATCH_SET, f"{EXPANSION_MIRROR_PREFIX}/{BATCH_SET}")
    context.say(
        f"batches: {len(context.records)} frozen, set {context.checkpoint.batch_set_sha256[:16]}…, "
        "mirrored and read back"
    )


def _harvest(context: _Context, stop_after: int | None) -> None:
    checkpoint = context.checkpoint
    if checkpoint.stage in {ExpansionStage.COMPLETE}:
        context.say("already COMPLETE; nothing to do")
        return
    done_this_run = 0
    for record in context.records:
        if record.batch in checkpoint.completed_batches:
            continue
        if stop_after is not None and done_this_run >= stop_after:
            context.say(f"stopping after {done_this_run} batches as asked; resumable")
            return
        checkpoint.stage = ExpansionStage.HARVESTING
        _one_batch(context, record)
        done_this_run += 1
        if len(checkpoint.completed_batches) % MILESTONE_EVERY == 0:
            _milestone(context, record.batch)
    checkpoint.stage = ExpansionStage.EXPANSION_HARVEST_COMPLETE
    checkpoint.notes.append("every batch COMPLETE")
    _save(context)
    context.say("EXPANSION_HARVEST_COMPLETE")


def _classify_failures(failures: Sequence[tuple[str, str]]) -> ExpansionStopError:
    permanent = [url for url, reason in failures if "HTTP 404" in reason or "HTTP 410" in reason]
    if permanent:
        return ExpansionStopError(
            "C",
            f"{len(permanent)} authorized targets are absent at the source after two passes",
            permanent,
        )
    return ExpansionStopError(
        "B",
        f"{len(failures)} targets still failed after two passes: {[u for u, _ in failures[:3]]}",
        [url for url, _ in failures],
    )


def _one_batch(context: _Context, record: BatchRecord) -> None:
    """Two passes through the chain, then the batch is COMPLETE or the run stops."""
    checkpoint = context.checkpoint
    targets = context.resolved[record.start : record.end]
    started = time.perf_counter()
    counts_before = dict(context.storage.counts)
    records_before = sum(1 for _ in context.manifest.entries())
    budget = context.budget_factory()
    documents = context.documents_factory(budget)
    state = context.manifest.replay()
    first = acquire_batch(
        targets,
        context.storage,
        context.manifest,
        documents,
        budget,
        workers=context.workers,
        state=state,
    )
    outcome = first
    if first.failures:
        retry = [t for t in targets if t.url in {u for u, _ in first.failures}]
        second = acquire_batch(
            retry,
            context.storage,
            context.manifest,
            documents,
            budget,
            workers=context.workers,
            state=state,
        )
        outcome.failed = second.failed
        outcome.failures = second.failures
        outcome.downloaded += second.downloaded
        outcome.adopted += second.adopted
        outcome.source_bytes += second.source_bytes
        outcome.sec_attempts += second.sec_attempts
        outcome.trusted = sum(
            1 for t in targets if (e := state.latest.get(t.url)) is not None and e.is_stored
        )
        if second.failures:
            raise _classify_failures(second.failures)
    if outcome.trusted != record.count:
        raise ExpansionStopError(
            "B", f"{record.name}: {outcome.trusted} of {record.count} targets trusted; not COMPLETE"
        )
    elapsed = time.perf_counter() - started
    delta = {
        k: context.storage.counts.get(k, 0) - counts_before.get(k, 0)
        for k in set(context.storage.counts) | set(counts_before)
    }
    records_after = sum(1 for _ in context.manifest.entries())
    counted = {
        "new_downloads": outcome.downloaded - outcome.adopted,
        "adopted_after_interrupted_put": outcome.adopted,
        "cache_hits": outcome.cache_hits,
        "trusted": outcome.trusted,
        "source_bytes": outcome.source_bytes,
        "sec_attempts": outcome.sec_attempts,
        "sec_retries": max(0, outcome.sec_attempts - (outcome.downloaded + outcome.failed)),
        "b2_put": delta.get("evidence:PUT", 0),
        "b2_head": delta.get("evidence:HEAD", 0),
        "b2_verification_get": delta.get("evidence:GET", 0),
    }
    summary: dict[str, object] = {
        "generation_id": context.generation_id,
        "batch": record.batch,
        "content_sha256": record.sha256,
        "targets": record.count,
        **counted,
        "failed": outcome.failed,
        "elapsed_s": round(elapsed, 3),
        "manifest_records_before": records_before,
        "manifest_records_after": records_after,
        "stage": "BATCH_COMPLETE",
    }
    summaries = context.execution / "summaries"
    summaries.mkdir(exist_ok=True)
    local = summaries / f"{record.name}.json"
    local.write_bytes(_json_bytes(summary))
    context.mirror.mirror(local, f"{EXPANSION_MIRROR_PREFIX}/summaries/{record.name}.json")
    checkpoint.completed_batches.append(record.batch)
    for name, value in counted.items():
        checkpoint.counters[name] = checkpoint.counters.get(name, 0) + value
    checkpoint.counters["elapsed_s"] = checkpoint.counters.get("elapsed_s", 0) + int(elapsed)
    _save(context, label=f"{record.name}-complete")
    context.report.batch_elapsed[record.name] = elapsed
    line = (
        f"{record.name}: {record.count} targets, {summary['new_downloads']} new, "
        f"{outcome.cache_hits} hits, {outcome.adopted} adopted, {outcome.source_bytes} bytes, "
        f"{outcome.sec_attempts} SEC attempts, {elapsed:.0f}s; manifest "
        f"{records_before}→{records_after}; BATCH_COMPLETE "
        f"({len(checkpoint.completed_batches)}/{len(context.records)})"
    )
    context.report.batch_lines.append(line)
    context.say(line)


def _milestone(context: _Context, batch: int) -> None:
    counts = structural_reconciliation(context.storage, context.manifest)
    if counts["missing"] or counts["path_violations"]:
        raise ExpansionStopError("E", f"structural reconciliation at batch {batch:03d}: {counts}")
    if counts["orphan_objects"]:
        context.checkpoint.notes.append(
            f"milestone {batch:03d}: {counts['orphan_objects']} unrecorded objects "
            "(adopted on resume)"
        )
    if context.milestone_hook is not None:
        context.milestone_hook(batch)
    records = sum(1 for _ in context.manifest.entries())
    trusted = sum(1 for e in context.manifest.latest_by_url().values() if e.is_stored)
    context.mirror.mirror_snapshot(
        context.manifest.path,
        "manifests",
        generation_id=context.generation_id,
        records=records,
        trusted=trusted,
        label=f"a3-batch-{batch:03d}",
        stage="A3_EXPANSION_IN_PROGRESS",
    )
    context.say(
        f"milestone {batch:03d}: {counts['objects']} objects / {counts['trusted']} trusted, "
        f"{counts['orphan_objects']} orphan objects, {counts['missing']} missing; "
        "manifest snapshot mirrored"
    )


def _terminal(context: _Context) -> None:
    """Every A3 artifact downloaded and hashed; the namespace walked; snapshot; marker; proof."""
    checkpoint = context.checkpoint
    checkpoint.stage = ExpansionStage.TERMINAL_VERIFY
    _save(context)
    latest = context.manifest.latest_by_url()
    entries = []
    for target in context.resolved:
        entry = latest.get(target.url)
        if entry is None or not entry.is_stored:
            raise ExpansionStopError(
                "B", f"{target.url} is not trusted; the harvest is not complete"
            )
        entries.append(entry)
    context.say(f"terminal: downloading and hashing {len(entries)} A3 artifacts")
    verified = verify_expansion_bodies(
        context.storage,
        entries,
        workers=context.workers,
        progress=lambda n: context.say(f"terminal: {n}/{len(entries)} verified"),
    )
    if verified["verified"] != len(entries):
        raise ExpansionStopError("D", f"terminal verification failed: {verified}")
    structural = structural_reconciliation(context.storage, context.manifest)
    trusted = [e for e in latest.values() if e.is_stored]
    structural["zero_byte"] = sum(1 for e in trusted if not e.bytes)
    structural["duplicate_urls"] = len(trusted) - len({e.url for e in trusted})
    structural["orphan_records"] = structural["missing"]
    if any(
        structural[k]
        for k in ("orphan_objects", "missing", "path_violations", "zero_byte", "duplicate_urls")
    ):
        raise ExpansionStopError("E", f"final structural reconciliation: {structural}")
    if context.milestone_hook is not None:
        context.milestone_hook(len(context.records))
    by_kind = {"SUBMISSIONS": 0, "FILING": 0, "FULLTEXT": 0}
    for e in trusted:
        by_kind[str(e.request_kind)] = by_kind.get(str(e.request_kind), 0) + 1
    records = sum(1 for _ in context.manifest.entries())
    logical = sum(e.bytes or 0 for e in trusted)
    final_sha = _sha256_of(context.manifest.path)
    context.mirror.mirror_snapshot(
        context.manifest.path,
        "manifests",
        generation_id=context.generation_id,
        records=records,
        trusted=len(trusted),
        logical_bytes=logical,
        label="a3-expansion-complete",
        stage=EXPANSION_STAGE,
        parent_baseline_manifest_sha256=checkpoint.baseline_manifest_sha256,
        a3_plan_sha256=checkpoint.plan_sha256,
        a3_package_sha256=checkpoint.package_sha256,
    )
    raw_chain = context.plan.record.get("chain")
    chain: dict[str, object] = dict(raw_chain) if isinstance(raw_chain, dict) else {}
    marker = {
        "generation_id": context.generation_id,
        "stage": EXPANSION_STAGE,
        "baseline_manifest_sha256": checkpoint.baseline_manifest_sha256,
        "baseline_marker_sha256": checkpoint.baseline_marker_sha256,
        "baseline_trusted": len(trusted) - len(entries),
        "package_sha256": checkpoint.package_sha256,
        "plan_sha256": checkpoint.plan_sha256,
        "record_sha256": checkpoint.record_sha256,
        "batch_set_sha256": checkpoint.batch_set_sha256,
        "batch_count": checkpoint.batches,
        "completed_batch_count": len(checkpoint.completed_batches),
        "target_count": len(context.resolved),
        "trusted_expansion_count": len(entries),
        "actual_expansion_bytes": verified["bytes"],
        "planned_estimated_bytes": sum(t.estimated_bytes for t in context.plan.targets),
        "final_trusted_count": len(trusted),
        "final_by_kind": by_kind,
        "final_logical_bytes": logical,
        "final_manifest_records": records,
        "final_manifest_sha256": final_sha,
        "terminal_expansion_verified": verified["verified"],
        "terminal_structural_reconciliation": {"status": "CLEAN", **structural},
        "superseded_plan_sha256": chain.get("superseded_plan_sha256"),
        "superseded_plan_status": ["SUPERSEDED", "NOT_AUTHORIZED_FOR_EXPANSION"],
    }
    marker_path = context.execution / EXPANSION_MARKER
    marker_path.write_bytes(_json_bytes(marker))
    record = context.mirror.mirror(marker_path, f"{EXPANSION_MIRROR_PREFIX}/{EXPANSION_MARKER}")
    checkpoint.terminal = {**verified, "structural": structural, "final_manifest_sha256": final_sha}
    context.report.terminal = {
        **verified,
        "structural": structural,
        "by_kind": by_kind,
        "logical_bytes": logical,
        "records": records,
    }
    context.report.marker_sha256 = record.sha256
    context.report.final_manifest_sha256 = final_sha
    context.report.recovery = _prove_recovery(context, final_sha, record.sha256)
    if context.report.recovery.get("A3_EXPANSION_RECOVERY_SUFFICIENT") != "YES":
        raise ExpansionStopError("B", f"recovery not proven: {context.report.recovery}")
    checkpoint.stage = ExpansionStage.COMPLETE
    checkpoint.notes.append("recovery=proven")
    _save(context, label="complete")
    context.say(f"{EXPANSION_STAGE}: marker mirrored ({record.outcome}) sha256 {record.sha256}")


def _prove_recovery(
    context: _Context, final_manifest_sha: str, marker_sha: str
) -> dict[str, object]:
    """From the bucket alone: every fact a fresh machine needs, read back and checked."""
    mirror = context.mirror
    proof: dict[str, object] = {}

    def check(name: str, ok: bool) -> None:
        proof[name] = "YES" if ok else "NO"

    baseline = json.loads(mirror.read(BASELINE_MARKER).decode("utf-8"))
    check(
        "baseline_recoverable",
        baseline.get("stage") == "BASELINE_COMPLETE"
        and baseline.get("manifest_sha256") == context.checkpoint.baseline_manifest_sha256,
    )
    got = mirror.newest_snapshot("a3-inputs-r1/planning_inputs_manifest-json")
    check(
        "corrected_inputs_recoverable",
        got is not None and json.loads(got[1]).get("package_sha256") == context.plan.package_sha256,
    )
    got = mirror.newest_snapshot(f"a3-r1/{A3_AUTHORITATIVE_RECORD.replace('.', '-')}")
    record = json.loads(got[1]) if got is not None else {}
    check(
        "authoritative_plan_recoverable",
        record.get("plan_sha256") == context.plan.plan_sha256
        and all(s in record.get("status", []) for s in AUTHORIZED_STATUS),
    )
    superseded = mirror.newest_snapshot("a3/superseded")
    old = json.loads(superseded[1]) if superseded is not None else {}
    chain = record.get("chain", {}) if isinstance(record.get("chain"), dict) else {}
    check(
        "superseded_plan_distinguishable",
        superseded is not None
        and "NOT_AUTHORIZED_FOR_EXPANSION" in old.get("status", [])
        and chain.get("superseded_plan_sha256") not in (None, context.plan.plan_sha256),
    )
    batch_set = json.loads(mirror.read(f"{EXPANSION_MIRROR_PREFIX}/{BATCH_SET}").decode("utf-8"))
    listed = set(mirror.listing())
    batch_files = all(
        f"{EXPANSION_MIRROR_PREFIX}/batches/{r.name}.jsonl" in listed
        and f"{EXPANSION_MIRROR_PREFIX}/batches/{r.name}.json" in listed
        for r in context.records
    )
    check(
        "batch_plans_recoverable",
        batch_set.get("batch_set_sha256") == context.checkpoint.batch_set_sha256 and batch_files,
    )
    got = mirror.newest_snapshot(f"{EXPANSION_MIRROR_PREFIX}/checkpoints")
    state = json.loads(got[1]) if got is not None else {}
    summaries = sum(1 for k in listed if k.startswith(f"{EXPANSION_MIRROR_PREFIX}/summaries/"))
    check(
        "completed_state_recoverable",
        len(state.get("completed_batches", [])) == len(context.records)
        and summaries == len(context.records),
    )
    descriptors = sorted(
        k for k in listed if k.startswith("manifests/") and k.endswith(".snapshot.json")
    )
    newest = json.loads(mirror.read(descriptors[-1]).decode("utf-8")) if descriptors else {}
    check(
        "final_manifest_recoverable",
        newest.get("sha256") == final_manifest_sha
        and newest.get("stage") == EXPANSION_STAGE
        and newest.get("key") in listed,
    )
    remote_marker = mirror.read(f"{EXPANSION_MIRROR_PREFIX}/{EXPANSION_MARKER}")
    check("completion_marker_recoverable", hashlib.sha256(remote_marker).hexdigest() == marker_sha)
    objects = sum(1 for p in ARTIFACT_PREFIXES for _ in context.storage.list_prefix(p))
    check(
        "evidence_inventory_recoverable",
        objects == json.loads(remote_marker).get("final_trusted_count"),
    )
    proof["A3_EXPANSION_RECOVERY_SUFFICIENT"] = (
        "YES"
        if all(v == "YES" for k, v in proof.items() if k != "A3_EXPANSION_RECOVERY_SUFFICIENT")
        else "NO"
    )
    return proof
