"""Planning the move of a trusted corpus to a bucket, one manifest key at a time.

Nothing here moves bytes. This module answers one question per trusted
artifact — *how would this one get into the bucket, and can we prove it
arrived?* — and writes the answers down deterministically, so that the phase
that does move bytes has nothing to decide and nothing to discover.

## The manifest is the only source of trust

Migration starts from the evidence manifest's trusted entries and from
nothing else. A file that happens to exist on the old volume is not an input
unless the manifest vouches for it; an object that happens to exist in the
bucket is not evidence unless the manifest vouches for it and its bytes hash
to what the manifest recorded. The migration reproduces *the bytes already
trusted* — `Entry.bytes` and `Entry.sha256` — not whatever a URL serves
today. A refetch that returns different bytes is a `REFETCH_SHA_MISMATCH`,
never a quietly new trusted artifact.

## Exactly one action per trusted key

Every trusted key receives exactly one `Action`, decided in a fixed order:

1. The manifest itself is ambiguous about the key → `CONFLICT`.
2. The bucket holds the key and its downloaded bytes hash to the manifest
   digest → `REMOTE_VERIFIED`. Holds it with the wrong length or digest →
   `CONFLICT`.
3. The local source holds it and the bytes were hashed and match →
   `LOCAL_VERIFIED_COPY_CANDIDATE`; present with the right length but not
   hashed → `LOCAL_COPY_CANDIDATE`; present but wrong → `CONFLICT`.
4. Otherwise, if the manifest carries the URL, the digest and the length
   needed to refetch and verify → `REFETCH_REQUIRED`; if not →
   `SOURCE_UNRECOVERABLE`.

`sum(actions) == trusted keys`, and the plan file has one row per key,
sorted by key. The same manifest, the same remote state and the same source
observations produce the same bytes and the same digest.

## The old volume is treated as unreliable

The source that held this corpus failed under load. The planner therefore
never walks it: it asks only about manifest-known keys, and only as much as
the chosen probe allows — `NONE` asks nothing, `METADATA` asks existence and
size, `VERIFY` reads and hashes. If the source stops answering mid-probe,
every key already answered keeps its answer, every key not yet asked is
recorded `UNAVAILABLE`, and no further request is made. A plan is always
produced; source availability is a field in it, not a precondition of it.

## The bucket is read, never written

Remote discovery is a listing of the three artifact prefixes. When they are
empty — the expected state before migration — no object is asked about
individually. When they are not, each object with a trusted key is verified
by length and by a downloaded digest, and each object without one is a
`REMOTE_ORPHAN` that blocks execution until explained. The planner issues
no PUT, no DELETE, no COPY.

## Planner classification is not execution state

`Action` says what should happen. `ExecutionState` and `FailureCategory`,
defined here so the ledger's vocabulary is frozen with the plan's, say what
did happen when a later phase runs it. They are never mixed in one field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.backend import (
    ArtifactNotFoundError,
    UnsafeStorageKeyError,
    validate_artifact_key,
)
from stock_screener.sec_evidence.storage import ARTIFACT_PREFIXES, StorageUnavailableError
from stock_screener.sec_evidence.targets import held_from_manifest, load_manifest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot
    from stock_screener.sec_evidence.manifest import Entry, Manifest

log = structlog.get_logger(__name__)

__all__ = [
    "MIGRATION_PLAN_VERSION",
    "A3Accounting",
    "Action",
    "ExecutionState",
    "FailureCategory",
    "ManifestAudit",
    "MigrationBatch",
    "MigrationPlan",
    "PlanRow",
    "RemoteAudit",
    "RemoteStatus",
    "SourceProbe",
    "SourceStatus",
    "TrustedCorpus",
    "a3_accounting",
    "audit_manifest",
    "audit_remote",
    "batches_for",
    "build_plan",
    "plan_paths",
    "probe_source",
    "trusted_corpus",
    "write_migration_plan",
]

#: The plan schema. Rows carry it so a reader knows what the fields mean.
MIGRATION_PLAN_VERSION = "SEC_B2_MIGRATION_V1"

#: The file stem under the control root's `migration/`.
PLAN_STEM = "sec_b2_migration_plan_v1"

#: Objects per execution batch. At the corpus's average object size this is
#: on the order of half a gigabyte and, at canary latencies, a quarter of an
#: hour of uploads plus verification — small enough to lose without pain,
#: large enough that a hundred thousand objects is a hundred batches.
DEFAULT_BATCH_SIZE = 1000


class Action(StrEnum):
    """What the migration should do for one trusted key. Exactly one per key."""

    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    """The bucket already holds bytes that hash to the manifest digest. Skip."""

    LOCAL_VERIFIED_COPY_CANDIDATE = "LOCAL_VERIFIED_COPY_CANDIDATE"
    """The local source holds it; the bytes were read and hash correctly. Upload."""

    LOCAL_COPY_CANDIDATE = "LOCAL_COPY_CANDIDATE"
    """The local source holds it at the right length; not yet hashed. Verify, then upload."""

    REFETCH_REQUIRED = "REFETCH_REQUIRED"
    """No usable local copy; the URL, digest and length allow a verified refetch."""

    SOURCE_UNRECOVERABLE = "SOURCE_UNRECOVERABLE"
    """No usable local copy and not enough provenance to refetch and verify."""

    CONFLICT = "CONFLICT"
    """Manifest, source or bucket disagree in a way that needs a person."""


class SourceStatus(StrEnum):
    """What the planner learned about one key's local source copy."""

    UNKNOWN = "UNKNOWN"
    """No source configured, or the probe was `NONE`."""

    UNAVAILABLE = "UNAVAILABLE"
    """The source could not be asked — not mounted, or it failed mid-probe."""

    MISSING = "MISSING"
    """The source answered: nothing at this key."""

    PRESENT = "PRESENT"
    """Present at the manifest's length; bytes not read."""

    VERIFIED = "VERIFIED"
    """Present, read, and hashes to the manifest digest."""

    SIZE_MISMATCH = "SIZE_MISMATCH"
    SHA_MISMATCH = "SHA_MISMATCH"


class RemoteStatus(StrEnum):
    """What the planner learned about one key in the bucket."""

    ABSENT = "ABSENT"
    VERIFIED = "VERIFIED"
    """Present, and the downloaded bytes hash to the manifest digest."""

    SIZE_MISMATCH = "SIZE_MISMATCH"
    SHA_MISMATCH = "SHA_MISMATCH"


class SourceProbe(StrEnum):
    """How much the planner may ask of the local source."""

    NONE = "none"
    """Ask nothing. Every key is `UNKNOWN`."""

    METADATA = "metadata"
    """Existence and length per manifest key. No bodies read. The default."""

    VERIFY = "verify"
    """Read and hash every present key. Explicit, because the source is unreliable."""


class ExecutionState(StrEnum):
    """The ledger's vocabulary for what a later phase did with a row.

    Distinct from `Action` on purpose: a row planned `REFETCH_REQUIRED` may
    end `REMOTE_VERIFIED` or `FAILED`, and the plan row does not change.
    """

    PENDING = "PENDING"
    COPYING = "COPYING"
    UPLOADED = "UPLOADED"
    """`write_bytes` returned; the backend's own HEAD verification passed."""

    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    """An independent download of the object hashed to the manifest digest."""

    FAILED = "FAILED"


class FailureCategory(StrEnum):
    """Why a ledger row failed. Never collapsed into one another."""

    LOCAL_MISSING = "LOCAL_MISSING"
    LOCAL_READ_ERROR = "LOCAL_READ_ERROR"
    LOCAL_SHA_MISMATCH = "LOCAL_SHA_MISMATCH"
    REFETCH_HTTP_ERROR = "REFETCH_HTTP_ERROR"
    REFETCH_BYTE_MISMATCH = "REFETCH_BYTE_MISMATCH"
    REFETCH_SHA_MISMATCH = "REFETCH_SHA_MISMATCH"
    B2_PUT_ERROR = "B2_PUT_ERROR"
    B2_VERIFY_ERROR = "B2_VERIFY_ERROR"
    B2_DIGEST_MISMATCH = "B2_DIGEST_MISMATCH"
    REMOTE_CONFLICT = "REMOTE_CONFLICT"
    REMOTE_ORPHAN = "REMOTE_ORPHAN"


# ----------------------------------------------------------------------------
# The trusted corpus, from the manifest and nothing else
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestAudit:
    """What a structural pass over the manifest found.

    Attributes:
        records: Every parseable line, history included.
        unreadable_lines: Lines the reader discarded.
        trusted: Entries the latest word per URL marks as stored.
        invalid_keys: Trusted entries whose path is not an artifact key.
        bad_digests: Trusted entries whose digest is not 64 hex characters.
        bad_lengths: Trusted entries with no usable byte count.
        conflicting_keys: Keys that two trusted URLs claim with different bytes.
        shared_keys: Keys that two trusted URLs claim with identical bytes.
    """

    records: int
    unreadable_lines: int
    trusted: int
    invalid_keys: tuple[str, ...]
    bad_digests: tuple[str, ...]
    bad_lengths: tuple[str, ...]
    conflicting_keys: tuple[str, ...]
    shared_keys: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        """Whether any trusted key has no single canonical state."""
        return bool(
            self.invalid_keys or self.bad_digests or self.bad_lengths or self.conflicting_keys
        )


@dataclass(frozen=True)
class TrustedCorpus:
    """One canonical trusted entry per artifact key.

    Attributes:
        by_key: Key → the entry that vouches for it.
        conflicts: Keys with no single canonical state, with the reason.
        audit: The structural pass that produced this.
    """

    by_key: dict[str, Entry]
    conflicts: dict[str, str]
    audit: ManifestAudit

    @property
    def logical_bytes(self) -> int:
        """Combined recorded size of every trusted artifact."""
        return sum(entry.bytes or 0 for entry in self.by_key.values())

    def counts_by_kind(self) -> dict[str, int]:
        """Trusted artifacts per artifact prefix."""
        counts = dict.fromkeys(ARTIFACT_PREFIXES, 0)
        for key in self.by_key:
            counts[key.split("/", 1)[0]] += 1
        return counts


def _is_sha256(value: str | None) -> bool:
    return value is not None and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def audit_manifest(manifest: Manifest) -> tuple[ManifestAudit, dict[str, Entry], dict[str, str]]:
    """Fold the manifest's history into one trusted state per key, and say what is wrong.

    Uses the manifest's own replay semantics — the latest entry per URL wins —
    so a retry after a failure reads as success and a `CACHE_HIT` after a
    `DOWNLOADED` keeps the artifact trusted. Every JSONL line is counted; no
    line is counted as an artifact.

    Args:
        manifest: The evidence manifest.

    Returns:
        The audit, the canonical trusted entries by key, and the conflicts by
        key with a reason. A conflicting key is in the second map with the
        entry that would have won, so it can still be listed; the plan gives
        it `CONFLICT`.
    """
    records = 0
    for _ in manifest.entries():
        records += 1
    unreadable = _unreadable_lines(manifest.path)
    latest = manifest.latest_by_url()
    trusted = [entry for entry in latest.values() if entry.is_stored]

    by_key: dict[str, Entry] = {}
    conflicts: dict[str, str] = {}
    invalid: list[str] = []
    bad_digest: list[str] = []
    bad_length: list[str] = []
    shared: list[str] = []
    for entry in sorted(trusted, key=lambda e: e.url):
        key = entry.path or ""
        try:
            validate_artifact_key(key)
        except UnsafeStorageKeyError:
            invalid.append(entry.url)
            continue
        if not _is_sha256(entry.sha256):
            bad_digest.append(key)
            conflicts[key] = "trusted entry has no SHA-256"
        if entry.bytes is None or entry.bytes < 0:
            bad_length.append(key)
            conflicts[key] = "trusted entry has no byte count"
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = entry
            continue
        if existing.sha256 == entry.sha256 and existing.bytes == entry.bytes:
            # Two URLs, one artifact, same bytes: a co-registrant filing or a
            # replay under a second URL. One key, one row, first URL wins.
            shared.append(key)
            continue
        conflicts[key] = (
            f"claimed by {existing.url} ({existing.bytes} bytes, {(existing.sha256 or '')[:12]}…) "
            f"and {entry.url} ({entry.bytes} bytes, {(entry.sha256 or '')[:12]}…)"
        )

    audit = ManifestAudit(
        records=records,
        unreadable_lines=unreadable,
        trusted=len(trusted),
        invalid_keys=tuple(sorted(invalid)),
        bad_digests=tuple(sorted(set(bad_digest))),
        bad_lengths=tuple(sorted(set(bad_length))),
        conflicting_keys=tuple(sorted(k for k, why in conflicts.items() if "claimed by" in why)),
        shared_keys=tuple(sorted(set(shared))),
    )
    return audit, by_key, conflicts


def _unreadable_lines(path: Path) -> int:
    """Lines the manifest reader would discard, counted without trusting them."""
    if not path.is_file():
        return 0
    unreadable = 0
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            text = line.strip()
            if not text:
                continue
            try:
                json.loads(text)
            except json.JSONDecodeError:
                unreadable += 1
    return unreadable


def trusted_corpus(manifest: Manifest) -> TrustedCorpus:
    """The authoritative trusted set: one entry per key, conflicts named."""
    audit, by_key, conflicts = audit_manifest(manifest)
    return TrustedCorpus(by_key=by_key, conflicts=conflicts, audit=audit)


# ----------------------------------------------------------------------------
# Remote and source discovery — read-only, and as little of it as possible
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteAudit:
    """What the bucket's artifact prefixes hold, and how each key compares.

    Attributes:
        listed: Every key under the three artifact prefixes.
        status: Trusted key → what the bucket holds for it.
        orphans: Listed keys no trusted entry claims. Any of these blocks
            execution until explained.
        digests_read: How many objects were downloaded and hashed.
    """

    listed: tuple[str, ...]
    status: dict[str, RemoteStatus]
    orphans: tuple[str, ...]
    digests_read: int

    @property
    def empty(self) -> bool:
        """Whether the production namespace holds nothing — the expected state."""
        return not self.listed


def audit_remote(remote: EvidenceStorage, corpus: TrustedCorpus) -> RemoteAudit:
    """List the artifact prefixes and verify whatever is there. Never writes.

    Listing is the whole cost when the prefixes are empty: no object is
    asked about individually. When they are not, every listed key that a
    trusted entry claims is verified by length *and* by a downloaded digest —
    a HEAD's metadata is a claim, and B2.8 established that only bytes count.

    Args:
        remote: The bucket backend. Only `list_prefix`, `stat` and `digest`
            are called.
        corpus: The trusted set to compare against.

    Returns:
        The audit.

    Raises:
        StorageUnavailableError: If a listing or a verification fails. An
            uncertain bucket is not planned against.
    """
    listed: list[str] = []
    for prefix in ARTIFACT_PREFIXES:
        listed.extend(remote.list_prefix(prefix))
    status: dict[str, RemoteStatus] = {}
    orphans: list[str] = []
    digests = 0
    for key in listed:
        entry = corpus.by_key.get(key)
        if entry is None:
            orphans.append(key)
            continue
        found = remote.stat(key)
        if found.bytes != entry.bytes:
            status[key] = RemoteStatus.SIZE_MISMATCH
            continue
        digests += 1
        status[key] = (
            RemoteStatus.VERIFIED
            if remote.digest(key) == entry.sha256
            else RemoteStatus.SHA_MISMATCH
        )
    for key in corpus.by_key:
        status.setdefault(key, RemoteStatus.ABSENT)
    return RemoteAudit(
        listed=tuple(sorted(listed)),
        status=status,
        orphans=tuple(sorted(orphans)),
        digests_read=digests,
    )


def probe_source(
    source: EvidenceStorage | None,
    corpus: TrustedCorpus,
    *,
    probe: SourceProbe = SourceProbe.METADATA,
) -> tuple[dict[str, SourceStatus], int, str | None]:
    """Ask the local source about each trusted key, as gently as the probe allows.

    Keys are asked in sorted order, one at a time, by their manifest path —
    never by walking the tree. The first `StorageUnavailableError` ends the
    probe: every key already answered keeps its answer, every later key is
    `UNAVAILABLE`, and the fault is reported once rather than retried against
    a volume that has just failed.

    Args:
        source: The legacy local backend, or None when nothing is configured.
        corpus: The trusted set.
        probe: How much to ask.

    Returns:
        Status per key, how many keys were actually asked, and the fault that
        ended the probe if one did.
    """
    keys = sorted(corpus.by_key)
    if source is None or probe is SourceProbe.NONE:
        return dict.fromkeys(keys, SourceStatus.UNKNOWN), 0, None
    status: dict[str, SourceStatus] = {}
    asked = 0
    fault: str | None = None
    for key in keys:
        if fault is not None:
            status[key] = SourceStatus.UNAVAILABLE
            continue
        entry = corpus.by_key[key]
        try:
            asked += 1
            status[key] = _probe_one(source, key, entry, probe)
        except StorageUnavailableError as exc:
            fault = f"{type(exc).__name__}: {exc}"
            status[key] = SourceStatus.UNAVAILABLE
            log.warning("local source failed mid-probe; not retrying", key=key, fault=fault)
    return status, asked, fault


def _probe_one(source: EvidenceStorage, key: str, entry: Entry, probe: SourceProbe) -> SourceStatus:
    try:
        found = source.stat(key)
    except ArtifactNotFoundError:
        return SourceStatus.MISSING
    if found.bytes != entry.bytes:
        return SourceStatus.SIZE_MISMATCH
    if probe is SourceProbe.METADATA:
        return SourceStatus.PRESENT
    return (
        SourceStatus.VERIFIED if source.digest(key) == entry.sha256 else SourceStatus.SHA_MISMATCH
    )


# ----------------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRow:
    """One trusted key and what the migration should do about it.

    Every field is safe to write down: provenance the manifest already holds,
    and the planner's observations. No credential, no path, no bucket.
    """

    plan_version: str
    key: str
    url: str
    expected_bytes: int
    expected_sha256: str
    artifact_kind: str
    cik: int | None
    accession: str | None
    form: str | None
    filing_date: str | None
    local_source_status: str
    remote_status: str
    action: str
    reason: str

    def line(self) -> str:
        """The row as one JSONL line, keys sorted, so bytes are reproducible."""
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class MigrationBatch:
    """One execution batch: a contiguous run of plan rows in key order.

    Attributes:
        number: Zero-based.
        first_key: The first key in the batch.
        last_key: The last.
        targets: How many rows.
        logical_bytes: Their combined expected size.
        sha256: Over the rows' JSONL lines, so the batch can be proven unchanged.
    """

    number: int
    first_key: str
    last_key: str
    targets: int
    logical_bytes: int
    sha256: str

    def line(self) -> str:
        """The batch as one JSONL line."""
        return json.dumps(asdict(self), sort_keys=True)


@dataclass
class MigrationPlan:
    """The whole plan, with the inputs that shaped it.

    Attributes:
        rows: One per trusted key, sorted by key.
        corpus: The trusted set the rows came from.
        remote: The bucket audit, or None when the bucket was not consulted.
        source_probe: How the source was asked.
        source_asked: How many keys the source was asked about.
        source_fault: The fault that ended the probe, if one did.
        batches: The execution batches.
    """

    rows: list[PlanRow]
    corpus: TrustedCorpus
    remote: RemoteAudit | None
    source_probe: SourceProbe
    source_asked: int
    source_fault: str | None
    batches: list[MigrationBatch] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Rows per action, every action present, in a fixed order."""
        counts = {action.value: 0 for action in Action}
        for row in self.rows:
            counts[row.action] += 1
        return counts

    def source_counts(self) -> dict[str, int]:
        """Rows per source status."""
        counts = {status.value: 0 for status in SourceStatus}
        for row in self.rows:
            counts[row.local_source_status] += 1
        return counts

    @property
    def complete(self) -> bool:
        """One row per trusted key, every key once, every row classified."""
        keys = [row.key for row in self.rows]
        return (
            len(keys) == len(self.corpus.by_key)
            and len(set(keys)) == len(keys)
            and set(keys) == set(self.corpus.by_key)
            and all(row.action in Action.__members__ for row in self.rows)
        )

    @property
    def remote_orphans(self) -> tuple[str, ...]:
        """Bucket objects no trusted entry claims. Execution is blocked while any exist."""
        return self.remote.orphans if self.remote is not None else ()

    @property
    def refetch_unrecoverable(self) -> int:
        """Rows that neither a source nor a verified refetch can satisfy."""
        return self.counts()[Action.SOURCE_UNRECOVERABLE]

    def sha256(self) -> str:
        """Digest over the rows' JSONL lines, in order."""
        digest = hashlib.sha256()
        for row in self.rows:
            digest.update(row.line().encode() + b"\n")
        return digest.hexdigest()


def _refetchable(entry: Entry) -> bool:
    """Whether the manifest holds enough to refetch and *verify* this artifact."""
    return bool(entry.url) and _is_sha256(entry.sha256) and entry.bytes is not None


def _decide(
    entry: Entry, conflict: str | None, remote: RemoteStatus | None, source: SourceStatus
) -> tuple[Action, str]:
    if conflict is not None:
        return Action.CONFLICT, f"manifest: {conflict}"
    if remote is RemoteStatus.VERIFIED:
        return Action.REMOTE_VERIFIED, "bucket bytes hash to the manifest digest"
    if remote in {RemoteStatus.SIZE_MISMATCH, RemoteStatus.SHA_MISMATCH}:
        return (
            Action.CONFLICT,
            f"bucket holds the key with {remote.value.lower().replace('_', ' ')}",
        )
    if source is SourceStatus.VERIFIED:
        return Action.LOCAL_VERIFIED_COPY_CANDIDATE, "local bytes hash to the manifest digest"
    if source is SourceStatus.PRESENT:
        return (
            Action.LOCAL_COPY_CANDIDATE,
            "local copy present at the recorded length; hash before upload",
        )
    if source in {SourceStatus.SIZE_MISMATCH, SourceStatus.SHA_MISMATCH}:
        return Action.CONFLICT, f"local copy {source.value.lower().replace('_', ' ')}"
    if _refetchable(entry):
        return Action.REFETCH_REQUIRED, (
            f"no usable local copy ({source.value.lower()}); refetch {entry.url} and require "
            f"{entry.bytes} bytes hashing to the manifest digest"
        )
    return (
        Action.SOURCE_UNRECOVERABLE,
        "no usable local copy and no digest or URL to refetch against",
    )


def build_plan(
    manifest: Manifest,
    *,
    remote: EvidenceStorage | None,
    source: EvidenceStorage | None,
    probe: SourceProbe = SourceProbe.METADATA,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> MigrationPlan:
    """Classify every trusted key and lay out the batches. Writes nothing anywhere.

    Args:
        manifest: The authoritative evidence manifest.
        remote: The bucket backend, read-only, or None to skip remote discovery.
        source: The legacy local backend, or None when none is configured.
        probe: How much to ask of the source.
        batch_size: Rows per execution batch.

    Returns:
        The plan. `complete` says whether every trusted key has exactly one row.

    Raises:
        StorageUnavailableError: If the bucket cannot be listed or verified.
            Nothing about the bucket is assumed.
    """
    corpus = trusted_corpus(manifest)
    remote_audit = audit_remote(remote, corpus) if remote is not None else None
    source_status, asked, fault = probe_source(source, corpus, probe=probe)

    rows: list[PlanRow] = []
    for key in sorted(corpus.by_key):
        entry = corpus.by_key[key]
        remote_status = remote_audit.status.get(key) if remote_audit is not None else None
        action, reason = _decide(
            entry, corpus.conflicts.get(key), remote_status, source_status[key]
        )
        rows.append(
            PlanRow(
                plan_version=MIGRATION_PLAN_VERSION,
                key=key,
                url=entry.url,
                expected_bytes=entry.bytes or 0,
                expected_sha256=entry.sha256 or "",
                artifact_kind=key.split("/", 1)[0],
                cik=entry.cik,
                accession=entry.accession,
                form=entry.form,
                filing_date=entry.filing_date,
                local_source_status=source_status[key].value,
                remote_status=(remote_status or RemoteStatus.ABSENT).value
                if remote_audit is not None
                else "NOT_CONSULTED",
                action=action.value,
                reason=reason,
            )
        )
    plan = MigrationPlan(
        rows=rows,
        corpus=corpus,
        remote=remote_audit,
        source_probe=probe,
        source_asked=asked,
        source_fault=fault,
    )
    plan.batches = batches_for(plan.rows, batch_size)
    log.info(
        "migration plan built",
        trusted=len(corpus.by_key),
        actions=plan.counts(),
        remote_orphans=len(plan.remote_orphans),
        batches=len(plan.batches),
    )
    return plan


def batches_for(rows: list[PlanRow], batch_size: int) -> list[MigrationBatch]:
    """Cut the sorted rows into contiguous batches, each with its own digest."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    batches: list[MigrationBatch] = []
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        digest = hashlib.sha256()
        for row in chunk:
            digest.update(row.line().encode() + b"\n")
        batches.append(
            MigrationBatch(
                number=len(batches),
                first_key=chunk[0].key,
                last_key=chunk[-1].key,
                targets=len(chunk),
                logical_bytes=sum(row.expected_bytes for row in chunk),
                sha256=digest.hexdigest(),
            )
        )
    return batches


def plan_paths(control: EvidenceControlRoot) -> dict[str, Path]:
    """Where the plan's files go beneath the control root's `migration/`."""
    directory = control.migration
    return {
        "plan": directory / f"{PLAN_STEM}.jsonl",
        "sha256": directory / f"{PLAN_STEM}.sha256",
        "batches": directory / f"{PLAN_STEM}.batches.jsonl",
        "summary": directory / f"{PLAN_STEM}.summary.json",
    }


def write_migration_plan(
    plan: MigrationPlan, control: EvidenceControlRoot
) -> tuple[dict[str, Path], str]:
    """Write the plan, its digest, its batches and a summary under `migration/`.

    Deterministic by construction: rows in key order, keys sorted within each
    JSON object, one line per row, and the digest computed over exactly those
    bytes. The summary records the observations that shaped the plan — the
    source probe and any fault — so a reader can tell whether a differing
    digest reflects a different corpus or a different day at the source.

    Args:
        plan: The plan.
        control: The control root; `migration/` is created beneath it.

    Returns:
        The paths written, and the plan digest.
    """
    paths = plan_paths(control)
    control.migration.mkdir(exist_ok=True)
    with paths["plan"].open("w", encoding="utf-8") as handle:
        for row in plan.rows:
            handle.write(row.line() + "\n")
    sha = plan.sha256()
    paths["sha256"].write_text(f"{sha}  {PLAN_STEM}.jsonl\n", encoding="utf-8")
    with paths["batches"].open("w", encoding="utf-8") as handle:
        for batch in plan.batches:
            handle.write(batch.line() + "\n")
    summary = {
        "plan_version": MIGRATION_PLAN_VERSION,
        "plan_sha256": sha,
        "rows": len(plan.rows),
        "complete": plan.complete,
        "actions": plan.counts(),
        "source_probe": plan.source_probe.value,
        "source_asked": plan.source_asked,
        "source_fault": plan.source_fault,
        "source_status": plan.source_counts(),
        "remote_consulted": plan.remote is not None,
        "remote_listed": len(plan.remote.listed) if plan.remote is not None else None,
        "remote_orphans": list(plan.remote_orphans),
        "remote_digests_read": plan.remote.digests_read if plan.remote is not None else None,
        "manifest": {
            "records": plan.corpus.audit.records,
            "unreadable_lines": plan.corpus.audit.unreadable_lines,
            "trusted": plan.corpus.audit.trusted,
            "trusted_keys": len(plan.corpus.by_key),
            "logical_bytes": plan.corpus.logical_bytes,
            "by_kind": plan.corpus.counts_by_kind(),
            "conflicting_keys": list(plan.corpus.audit.conflicting_keys),
            "shared_keys": len(plan.corpus.audit.shared_keys),
            "invalid_keys": list(plan.corpus.audit.invalid_keys),
        },
        "batches": len(plan.batches),
        "batch_size": plan.batches[0].targets if plan.batches else 0,
    }
    paths["summary"].write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log.info("migration plan written", rows=len(plan.rows), sha=sha[:16], path=str(paths["plan"]))
    return paths, sha


def read_plan_rows(path: Path) -> Iterator[PlanRow]:
    """Read a written plan back, row by row."""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield PlanRow(**json.loads(line))


# ----------------------------------------------------------------------------
# Plan A3 accounting — recomputed, never assumed
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class A3Accounting:
    """Where a frozen target plan stands against the trusted manifest.

    Recomputed from the frozen plan and the live manifest by `(CIK, accession)`
    set arithmetic — never from a remembered number. The two invariants that
    make the numbers meaningful are properties, so a report cannot state a
    count without them.

    Attributes:
        frozen: Targets in the frozen plan.
        trusted: Frozen targets the manifest holds as trusted filings.
        remaining: Frozen targets it does not.
        plan_sha256: The frozen plan file's digest, as loaded.
    """

    frozen: frozenset[tuple[int, str]]
    trusted: frozenset[tuple[int, str]]
    remaining: frozenset[tuple[int, str]]
    plan_sha256: str

    @property
    def partitioned(self) -> bool:
        """`trusted | remaining == frozen` and `trusted & remaining` is empty."""
        return (self.trusted | self.remaining) == self.frozen and not (
            self.trusted & self.remaining
        )


def a3_accounting(plan_path: Path, manifest: Manifest) -> A3Accounting:
    """Partition a frozen target plan into held and remaining, from the manifest.

    Membership in the plan and trust in the evidence are separate questions:
    a filing is *remaining* until the manifest vouches for its bytes, however
    many times it was attempted. No request is made.

    Args:
        plan_path: The frozen target manifest (JSONL, as `load_manifest` reads).
        manifest: The evidence manifest.

    Returns:
        The accounting, with its invariants as properties.
    """
    targets, audit = load_manifest(plan_path)
    frozen = frozenset(target.key for target in targets)
    held = held_from_manifest(manifest)
    trusted = frozen & held
    return A3Accounting(
        frozen=frozen,
        trusted=trusted,
        remaining=frozen - trusted,
        plan_sha256=audit.digest,
    )
