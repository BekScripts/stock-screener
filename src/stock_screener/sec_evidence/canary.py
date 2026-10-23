"""A bounded, isolated, live exercise of the artifact backend.

Every B2 test before this one ran against a fake. This is the one that does
not: a canary writes a few kilobytes of synthetic, deterministic objects into
a namespace reserved for the purpose, exercises all six contract operations
against them, runs one real harvest into a temporary control root, replays
it, reconciles it, and then removes what it wrote. It touches nothing else.

## The namespace

Everything a canary writes lives under `_canary/<run-id>/`. Production
artifacts live under `filings/`, `submissions/` and `fulltext/` at the bucket
root, and the two cannot collide: `_canary` is not an artifact prefix, so a
production reconciliation never sees a canary object, and a run id is unique
to one invocation, so two canaries never see each other.

The production key mapping is not changed to achieve that. `CanaryNamespace`
is an `EvidenceStorage` that wraps the real backend and prepends the run's
prefix to every logical key on the way in and strips it on the way out. The
harvester, the audit and the manifest see ordinary `filings/...` keys and
behave exactly as they do in production; the bucket sees
`_canary/<run-id>/filings/...`. Both layers validate the key — the namespace
before it adds the prefix, the backend after — so no key the contract would
refuse reaches the wire.

## What it proves, in order

1. Preflight, read-only: a listing of the empty run prefix. Endpoint, TLS,
   credentials, bucket and `listFiles` all answer before anything is written.
2. Absence: a key nobody wrote is `False` and raises on read.
3. For each object: absent before, `write_bytes` returns a receipt whose
   digest equals the one computed here, present after, `stat` reports the
   length, `read_bytes` returns the bytes, `digest` recomputes the digest
   from the download.
4. Listing: recursive, sorted, complete, segment-bounded, nothing foreign.
5. One real `harvest_artifacts` into a temporary control root: no record
   before, exactly one trusted record after, then a replay that fetches and
   uploads nothing.
6. Reconciliation: clean over the trusted set, then one orphan object and one
   record for an object that was never written, each counted once.
7. Cleanup, if the caller supplies one: only under the run prefix, and the
   prefix must list empty afterwards.

Nothing is fetched from the SEC. The harvest's "document source" serves a
synthetic payload from memory.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from botocore.exceptions import BotoCoreError, ClientError

from api_clients.sec_documents import FetchedDocument
from stock_screener.sec_evidence.audit import reconcile, reconcile_subset
from stock_screener.sec_evidence.b2 import B2EvidenceStorage, build_b2_client
from stock_screener.sec_evidence.backend import (
    ArtifactNotFoundError,
    ArtifactStat,
    StoredArtifact,
    validate_key,
)
from stock_screener.sec_evidence.harvest import Budget, Target, harvest_artifacts
from stock_screener.sec_evidence.manifest import Entry, Manifest, Outcome
from stock_screener.sec_evidence.storage import (
    ARTIFACT_PREFIXES,
    RequestKind,
    StorageUnavailableError,
)
from stock_screener.sec_evidence.wiring import (
    StorageBackend,
    StorageConfigurationError,
    b2_configuration,
    open_evidence_layout,
    storage_backend,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mypy_boto3_s3 import S3Client

    from stock_screener.config import Settings
    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot

log = structlog.get_logger(__name__)

__all__ = [
    "CANARY_ROOT",
    "CanaryError",
    "CanaryNamespace",
    "CanaryObject",
    "CanaryReport",
    "CanaryStep",
    "canary_dataset",
    "delete_canary_prefix",
    "new_run_id",
    "run_canary",
    "run_live_canary",
    "validate_cleanup_prefix",
]

#: The reserved root every canary object lives under. Not an artifact prefix.
CANARY_ROOT = "_canary"

#: The one filing the canary harvest fetches, from memory. A real-looking
#: identity so `_key_for` produces an ordinary filing key beneath the
#: namespace; nothing about it reaches the SEC.
HARVEST_TARGET = Target(
    url="https://www.sec.gov/Archives/edgar/data/1/000000000124000001/canary-10k.htm",
    kind=RequestKind.FILING,
    cik=1,
    accession="0000000001-24-000001",
    document="canary-10k.htm",
    form="10-K",
    filing_date="2024-01-15",
)
HARVEST_PAYLOAD = b"<html><body>compounder radar B2 canary filing</body></html>\n"


class CanaryError(RuntimeError):
    """A canary step observed something other than what the contract promises."""


@dataclass(frozen=True)
class CanaryObject:
    """One synthetic object: a logical key and deterministic bytes.

    Attributes:
        key: The logical key, beneath which the namespace adds its prefix.
        payload: The bytes. Deterministic, so the expected digest is a fact.
    """

    key: str
    payload: bytes

    @property
    def sha256(self) -> str:
        """The digest the receipt, the manifest and the download must all agree on."""
        return hashlib.sha256(self.payload).hexdigest()


def canary_dataset() -> tuple[CanaryObject, ...]:
    """The synthetic objects a canary writes. Small, deterministic, realistic.

    Four shapes — text, binary, empty, and two megabytes — under keys shaped
    exactly as production keys are (a padded CIK, a dashless accession, a
    document name), one under each artifact prefix, and two documents in one
    accession beside a second accession so that listing one accession must
    not return the other.
    """
    return (
        CanaryObject(
            "filings/0000000001/000000000124000002/a.txt",
            b"compounder radar B2 canary: text payload\n" * 8,
        ),
        CanaryObject("filings/0000000001/000000000124000002/b.bin", bytes(range(256)) * 4),
        CanaryObject("filings/0000000001/000000000124000003/c.txt", b"second accession\n"),
        CanaryObject("submissions/CIK0000000001.json", b""),
        CanaryObject(f"fulltext/{'0' * 64}.json", bytes(range(256)) * 8192),
    )


def new_run_id(now: datetime | None = None, token: str | None = None) -> str:
    """A run id unique to one invocation: `b28-<UTC stamp>-<random hex>`."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"b28-{stamp}-{token or secrets.token_hex(4)}"


class CanaryNamespace:
    """The real backend, confined to `_canary/<run-id>/`.

    An `EvidenceStorage` in its own right, so the harvester, the audit and the
    manifest run against it unchanged. Every logical key is validated here
    before the prefix is added, and the backend validates the prefixed key
    again; a listing returns logical keys with the prefix removed and never
    a key from outside the namespace.

    Args:
        inner: The backend to confine — the one production wiring built.
        run_id: This run's id.
    """

    def __init__(self, inner: EvidenceStorage, run_id: str) -> None:
        validate_key(f"{CANARY_ROOT}/{run_id}")
        self._inner = inner
        self._prefix = f"{CANARY_ROOT}/{run_id}"
        self.operations: dict[str, int] = {}

    @property
    def prefix(self) -> str:
        """The physical prefix, without a trailing separator."""
        return self._prefix

    def _physical(self, key: str) -> str:
        return f"{self._prefix}/{validate_key(key)}"

    def _count(self, operation: str) -> None:
        self.operations[operation] = self.operations.get(operation, 0) + 1

    def exists(self, key: str) -> bool:
        """See `EvidenceStorage.exists`."""
        physical = self._physical(key)
        self._count("exists")
        return self._inner.exists(physical)

    def stat(self, key: str) -> ArtifactStat:
        """See `EvidenceStorage.stat`; the reported key is the logical one."""
        physical = self._physical(key)
        self._count("stat")
        found = self._inner.stat(physical)
        return ArtifactStat(key=key, bytes=found.bytes)

    def read_bytes(self, key: str) -> bytes:
        """See `EvidenceStorage.read_bytes`."""
        physical = self._physical(key)
        self._count("read_bytes")
        return self._inner.read_bytes(physical)

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """See `EvidenceStorage.write_bytes`; the receipt names the logical key."""
        physical = self._physical(key)
        self._count("write_bytes")
        stored = self._inner.write_bytes(physical, data)
        return StoredArtifact(key=key, bytes=stored.bytes, sha256=stored.sha256)

    def digest(self, key: str) -> str:
        """See `EvidenceStorage.digest`."""
        physical = self._physical(key)
        self._count("digest")
        return self._inner.digest(physical)

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """See `EvidenceStorage.list_prefix`; keys come back logical."""
        if prefix:
            validate_key(prefix.removesuffix("/"))
        self._count("list_prefix")
        physical = f"{self._prefix}/{prefix.removesuffix('/')}" if prefix else self._prefix
        cut = len(self._prefix) + 1
        for key in self._inner.list_prefix(physical):
            if not key.startswith(self._prefix + "/"):
                raise CanaryError(f"listing returned a key outside the namespace: {key!r}")
            yield key[cut:]


@dataclass
class CanaryStep:
    """One observed step: what was done, whether it held, how long it took."""

    name: str
    ok: bool
    elapsed_ms: float
    detail: str = ""


@dataclass
class CanaryReport:
    """Everything a canary observed, for the operator and the phase report.

    Attributes:
        run_id: This run's id.
        prefix: The physical prefix every object lived under.
        steps: Every step, in order, with its verdict and latency.
        operations: Contract operations issued through the namespace, by name.
        objects: Logical keys written, with byte counts.
        cleanup: What cleanup did: `deleted`, `retained`, or `skipped`.
        remaining: Keys still under the prefix after cleanup, if any.
        failure: The first step that did not hold, or None.
        requests: SDK-level counts, `call:<Op>` per API call and
            `attempt:<Op>` per HTTP request, when the backend could report
            them. Empty against a fake client.
        control_root: The temporary control root the canary manifest lived in.
    """

    run_id: str
    prefix: str
    steps: list[CanaryStep] = field(default_factory=list)
    operations: dict[str, int] = field(default_factory=dict)
    objects: dict[str, int] = field(default_factory=dict)
    cleanup: str = "skipped"
    remaining: list[str] = field(default_factory=list)
    failure: str | None = None
    requests: dict[str, int] = field(default_factory=dict)
    control_root: str = ""

    @property
    def passed(self) -> bool:
        """Whether every step held."""
        return self.failure is None and all(step.ok for step in self.steps)

    @property
    def bytes_written(self) -> int:
        """Total payload bytes uploaded."""
        return sum(self.objects.values())

    def summary(self) -> list[str]:
        """Lines for a terminal. No credential can be in any of them."""
        lines = [f"canary {self.run_id} under {self.prefix}/"]
        for step in self.steps:
            mark = "ok  " if step.ok else "FAIL"
            lines.append(f"  {mark} {step.name:<34} {step.elapsed_ms:8.1f} ms  {step.detail}")
        lines.append(
            f"  objects {len(self.objects)}, {self.bytes_written} bytes; "
            f"operations {dict(sorted(self.operations.items()))}"
        )
        lines.append(f"  cleanup: {self.cleanup}; remaining under prefix: {len(self.remaining)}")
        if self.requests:
            lines.append(f"  requests {dict(sorted(self.requests.items()))}")
        if self.failure:
            lines.append(f"  FAILED: {self.failure}")
        return lines


class _MemorySource:
    """A `DocumentSource` that serves one payload from memory. No SEC."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(self, url: str) -> FetchedDocument:
        self.calls += 1
        return FetchedDocument(
            url=url,
            status=200,
            body=self.payload,
            sha256=hashlib.sha256(self.payload).hexdigest(),
            content_type="text/html",
            etag=None,
            last_modified=None,
            retrieved_at=datetime.now(UTC),
        )


def validate_cleanup_prefix(prefix: str) -> str:
    """Refuse any cleanup prefix that is not exactly one canary run.

    Cleanup deletes, and delete is the one operation the evidence contract
    does not have. It is allowed here on one condition: the prefix names a
    single `_canary/<run-id>/` subtree and nothing else — not the bucket, not
    the canary root, not any artifact prefix.

    Args:
        prefix: The physical prefix to delete beneath.

    Returns:
        The prefix, with its trailing separator.

    Raises:
        CanaryError: For anything else.
    """
    stripped = prefix.strip()
    if not stripped or stripped == "/":
        raise CanaryError("cleanup refuses an empty prefix: that is the whole bucket")
    normalised = stripped.removesuffix("/")
    parts = normalised.split("/")
    if parts[0] in ARTIFACT_PREFIXES:
        raise CanaryError(f"cleanup refuses a production artifact prefix: {prefix!r}")
    if len(parts) != 2 or parts[0] != CANARY_ROOT or not parts[1]:
        raise CanaryError(
            f"cleanup accepts exactly one run — {CANARY_ROOT}/<run-id>/ — not {prefix!r}"
        )
    validate_key(normalised)
    return normalised + "/"


def run_canary(
    storage: EvidenceStorage,
    control: EvidenceControlRoot,
    *,
    run_id: str,
    cleanup: Callable[[str], int] | None = None,
) -> CanaryReport:
    """Exercise the backend end to end under one canary run, and report.

    Stops at the first step that does not hold — a canary that continued
    past a failed verification would be writing on top of a finding — but
    always attempts cleanup before returning, so a failed run leaves no more
    behind than a passed one.

    Args:
        storage: The real artifact backend, as production wiring built it.
        control: A temporary control root. Never the production one.
        run_id: This run's id; every object goes under `_canary/<run_id>/`.
        cleanup: Deletes everything under a physical prefix and returns how
            many objects it removed; runs only against this run's prefix.
            None leaves the objects in place and says so.

    Returns:
        The report. `passed` says whether every step held.
    """
    namespace = CanaryNamespace(storage, run_id)
    report = CanaryReport(run_id=run_id, prefix=namespace.prefix)
    manifest = Manifest(control.manifest_path)

    def step(name: str, check: Callable[[], str]) -> None:
        if report.failure is not None:
            return
        started = time.perf_counter()
        try:
            detail = check()
        except (CanaryError, StorageUnavailableError, ArtifactNotFoundError) as exc:
            elapsed = (time.perf_counter() - started) * 1000
            report.steps.append(CanaryStep(name, False, elapsed, f"{type(exc).__name__}: {exc}"))
            report.failure = f"{name}: {type(exc).__name__}: {exc}"
            log.error("canary step failed", step=name, error=type(exc).__name__)
            return
        elapsed = (time.perf_counter() - started) * 1000
        report.steps.append(CanaryStep(name, True, elapsed, detail))

    try:
        _run_steps(namespace, manifest, report, step)
    finally:
        _cleanup(namespace, report, cleanup)
        report.operations = dict(namespace.operations)
    return report


def _run_steps(
    namespace: CanaryNamespace,
    manifest: Manifest,
    report: CanaryReport,
    step: Callable[[str, Callable[[], str]], None],
) -> None:
    dataset = canary_dataset()
    absent = "filings/0000000009/000000000924000009/never-written.htm"

    def preflight() -> str:
        listed = list(namespace.list_prefix(""))
        if listed:
            raise CanaryError(f"run prefix is not empty before the run: {listed[:3]}")
        return "empty run prefix listed; endpoint, credentials, bucket and listFiles answered"

    step("preflight: list empty run prefix", preflight)

    def absence() -> str:
        if namespace.exists(absent):
            raise CanaryError("a key nobody wrote reports present")
        try:
            namespace.read_bytes(absent)
        except ArtifactNotFoundError:
            return "exists False; read raised ArtifactNotFoundError"
        raise CanaryError("reading a key nobody wrote did not raise")

    step("absence: unknown key", absence)

    def unsafe() -> str:
        before = dict(namespace.operations)
        for key in ("../escape", "/abs", "filings//x"):
            try:
                namespace.exists(key)
            except ValueError:
                continue
            raise CanaryError(f"unsafe key {key!r} was not refused")
        if namespace.operations != before:
            raise CanaryError("an unsafe key reached the backend")
        return "three unsafe keys refused before any request"

    step("unsafe keys refused offline", unsafe)

    for obj in dataset:

        def write(obj: CanaryObject = obj) -> str:
            if namespace.exists(obj.key):
                raise CanaryError(f"{obj.key!r} exists before the run wrote it")
            stored = namespace.write_bytes(obj.key, obj.payload)
            if stored.key != obj.key:
                raise CanaryError(f"receipt names {stored.key!r}, not {obj.key!r}")
            if stored.bytes != len(obj.payload):
                raise CanaryError(f"receipt says {stored.bytes} bytes, sent {len(obj.payload)}")
            if stored.sha256 != obj.sha256:
                raise CanaryError("receipt digest is not the digest of the bytes sent")
            report.objects[obj.key] = len(obj.payload)
            manifest.append(_trusted_entry(obj))
            return f"{len(obj.payload)} bytes, receipt verified"

        step(f"write {obj.key}", write)

        def verify(obj: CanaryObject = obj) -> str:
            if not namespace.exists(obj.key):
                raise CanaryError("absent after a verified write")
            found = namespace.stat(obj.key)
            if found.key != obj.key or found.bytes != len(obj.payload):
                raise CanaryError(f"stat reports {found.bytes} bytes for {found.key!r}")
            downloaded = namespace.read_bytes(obj.key)
            if downloaded != obj.payload:
                raise CanaryError("downloaded bytes differ from the payload")
            if hashlib.sha256(downloaded).hexdigest() != obj.sha256:
                raise CanaryError("downloaded bytes hash to a different digest")
            if namespace.digest(obj.key) != obj.sha256:
                raise CanaryError("digest() disagrees with the digest of the payload")
            return "exists, stat, read-back bytes and SHA, digest all agree"

        step(f"verify {obj.key}", verify)

    def listing() -> str:
        expected = sorted(obj.key for obj in dataset)
        listed = list(namespace.list_prefix(""))
        if listed != expected:
            raise CanaryError(f"listing {listed} != expected {expected}")
        if len(set(listed)) != len(listed):
            raise CanaryError("listing repeated a key")
        accession = "filings/0000000001/000000000124000002"
        narrow = list(namespace.list_prefix(accession))
        if narrow != sorted(k for k in expected if k.startswith(accession + "/")):
            raise CanaryError(f"one accession's listing is wrong: {narrow}")
        filings = list(namespace.list_prefix("filings"))
        if filings != sorted(k for k in expected if k.startswith("filings/")):
            raise CanaryError(f"prefix listing wrong: {filings}")
        return f"{len(listed)} keys, sorted, recursive, segment-bounded"

    step("list_prefix", listing)

    source = _MemorySource(HARVEST_PAYLOAD)

    def harvest() -> str:
        trusted_before = [e for e in manifest.latest_by_url().values() if e.is_stored]
        if any(e.url == HARVEST_TARGET.url for e in trusted_before):
            raise CanaryError("the harvest target is trusted before it was harvested")
        outcome = harvest_artifacts(
            [HARVEST_TARGET], namespace, manifest, source, budget=Budget(10, 10**6)
        )
        if outcome.by_outcome != {"DOWNLOADED": 1}:
            raise CanaryError(f"harvest outcome {outcome.by_outcome}")
        entries = [e for e in manifest.latest_by_url().values() if e.url == HARVEST_TARGET.url]
        if len(entries) != 1:
            raise CanaryError(f"{len(entries)} records for the harvested URL")
        entry = entries[0]
        expected_key = "filings/0000000001/000000000124000001/canary-10k.htm"
        if entry.path != expected_key:
            raise CanaryError(f"manifest path {entry.path!r} != {expected_key!r}")
        if entry.sha256 != hashlib.sha256(HARVEST_PAYLOAD).hexdigest():
            raise CanaryError("manifest digest is not the digest of the fetched bytes")
        if namespace.digest(expected_key) != entry.sha256:
            raise CanaryError("the bucket's bytes do not hash to the manifest digest")
        report.objects[expected_key] = len(HARVEST_PAYLOAD)
        return "no record before; one trusted record after; bucket bytes match it"

    step("harvest: write then record", harvest)

    def replay() -> str:
        writes = namespace.operations.get("write_bytes", 0)
        outcome = harvest_artifacts(
            [HARVEST_TARGET], namespace, manifest, source, budget=Budget(10, 10**6)
        )
        if outcome.by_outcome != {"CACHE_HIT": 1}:
            raise CanaryError(f"replay outcome {outcome.by_outcome}")
        if source.calls != 1:
            raise CanaryError(f"replay fetched again ({source.calls} fetches)")
        if namespace.operations.get("write_bytes", 0) != writes:
            raise CanaryError("replay wrote")
        return "CACHE_HIT; no fetch, no write; trusted by digest of the bucket bytes"

    step("replay: cache hit", replay)

    def clean_audit() -> str:
        state = reconcile(namespace, manifest, verify_digests=True)
        expected_trusted = len(dataset) + 1
        problems = {
            "orphan_files": state.orphan_files,
            "orphan_records": state.orphan_records,
            "byte_mismatches": state.byte_mismatches,
            "hash_mismatches": state.hash_mismatches,
            "path_violations": state.path_violations,
        }
        if state.trusted != expected_trusted or any(problems.values()):
            raise CanaryError(f"trusted={state.trusted} problems={problems}")
        if state.zero_byte != 1:
            raise CanaryError(f"zero_byte={state.zero_byte}, expected 1 for the empty object")
        scoped = reconcile_subset(namespace, manifest, [HARVEST_TARGET.url], verify_digests=True)
        if not scoped.clean or scoped.trusted != 1:
            raise CanaryError(f"subset audit: {scoped.summary()}")
        return (
            f"trusted={state.trusted} objects={state.files_on_disk} orphans=0 missing=0 "
            f"mismatches=0 zero_byte=1 (the empty object, expected)"
        )

    step("reconcile: clean", clean_audit)

    orphan_key = "filings/0000000002/000000000224000002/orphan.htm"

    def orphan() -> str:
        namespace.write_bytes(orphan_key, b"nobody vouches for this")
        report.objects[orphan_key] = len(b"nobody vouches for this")
        state = reconcile(namespace, manifest, verify_digests=True)
        if state.orphan_files != 1 or state.orphan_records != 0:
            raise CanaryError(
                f"orphan_files={state.orphan_files} orphan_records={state.orphan_records}"
            )
        return "one unrecorded object → orphan_files=1; trusted objects still valid"

    step("reconcile: orphan object", orphan)

    def missing() -> str:
        manifest.append(_trusted_entry(CanaryObject(absent, b"never written")))
        state = reconcile(namespace, manifest, verify_digests=True)
        if state.orphan_records != 1 or state.orphan_files != 1:
            raise CanaryError(
                f"orphan_records={state.orphan_records} orphan_files={state.orphan_files}"
            )
        if state.byte_mismatches or state.hash_mismatches or state.path_violations:
            raise CanaryError(state.summary())
        return "one record for an object never written → orphan_records=1"

    step("reconcile: missing object", missing)


def _cleanup(
    namespace: CanaryNamespace, report: CanaryReport, cleanup: Callable[[str], int] | None
) -> None:
    if cleanup is None:
        report.cleanup = "skipped: no cleanup supplied; objects retained"
        report.remaining = _remaining(namespace)
        return
    prefix = validate_cleanup_prefix(namespace.prefix + "/")
    try:
        deleted = cleanup(prefix)
    except StorageUnavailableError as exc:
        report.cleanup = f"retained: cleanup refused by the bucket ({exc})"
        report.remaining = _remaining(namespace)
        return
    report.remaining = _remaining(namespace)
    report.cleanup = f"deleted {deleted} objects under {prefix}"
    if report.remaining:
        report.failure = report.failure or f"cleanup left {len(report.remaining)} objects behind"


def _remaining(namespace: CanaryNamespace) -> list[str]:
    try:
        return list(namespace.list_prefix(""))
    except StorageUnavailableError:
        return ["<listing unavailable>"]


def _trusted_entry(obj: CanaryObject) -> Entry:
    """A DOWNLOADED record for a canary object, consistent with its key.

    The audit checks that a record's path agrees with its URL, so the URL is
    derived from the key the way the SEC's archive lays it out.
    """
    segments = obj.key.split("/")
    cik: int | None = None
    accession: str | None = None
    if segments[0] == "filings":
        cik, accession = int(segments[1]), segments[2]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{segments[3]}"
        kind = "FILING"
    elif segments[0] == "submissions":
        cik = int(segments[1].removeprefix("CIK").removesuffix(".json"))
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        kind = "SUBMISSIONS"
    else:
        url = f"https://efts.sec.gov/LATEST/search-index?q=canary-{segments[-1]}"
        kind = "FULLTEXT"
    return Entry(
        url=url,
        outcome=Outcome.DOWNLOADED,
        status=200,
        sha256=obj.sha256,
        bytes=len(obj.payload),
        retrieved_at=datetime.now(UTC).isoformat(),
        path=obj.key,
        request_kind=kind,
        cik=cik,
        accession=accession,
    )


def delete_canary_prefix(client: S3Client, bucket: str, prefix: str) -> int:
    """Delete every object under exactly one canary run. Nothing else, ever.

    Outside the evidence contract on purpose: `EvidenceStorage` has no
    delete, and routine evidence operation never needs one. This is a test
    utility that works on the raw client, and it will not run against any
    prefix `validate_cleanup_prefix` refuses.

    Args:
        client: The raw S3 client, built from the same configuration.
        bucket: The configured bucket.
        prefix: `_canary/<run-id>/`, and only that.

    Returns:
        How many objects were deleted.

    Raises:
        CanaryError: If the prefix is not one canary run.
        StorageUnavailableError: If the bucket refuses — the application key
            has no delete capability, most likely. The objects stay.
    """
    prefix = validate_cleanup_prefix(prefix)
    try:
        keys: list[str] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item["Key"])
                if not key.startswith(prefix):
                    raise CanaryError(f"listing under {prefix!r} returned {key!r}; refusing")
                keys.append(key)
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        raise StorageUnavailableError(f"cleanup of {prefix!r} refused: {code}") from exc
    except BotoCoreError as exc:
        raise StorageUnavailableError(
            f"cleanup of {prefix!r} failed: {type(exc).__name__}"
        ) from exc
    log.info("canary cleanup", prefix=prefix, deleted=len(keys))
    return len(keys)


def run_live_canary(settings: Settings, *, control_dir: Path, cleanup: bool = True) -> CanaryReport:
    """Run a canary against the configured bucket through production wiring.

    The backend is built by `open_evidence_layout` from the real settings —
    the same path every evidence command takes — with one override: the
    control root is a temporary directory, so the canary manifest is never
    the production manifest. Requests are counted through the backend's
    observer, and cleanup uses a second raw client built from the same
    configuration.

    Args:
        settings: Application settings. `SEC_STORAGE_BACKEND` must be `b2`.
        control_dir: An existing temporary directory for the canary's
            manifest and checkpoints.
        cleanup: Whether to delete the run's objects afterwards.

    Returns:
        The report.

    Raises:
        StorageConfigurationError: If the backend is not `b2`, or B2 is
            misconfigured.
        ControlPlaneUnavailableError: If `control_dir` does not exist.
    """
    if storage_backend(settings) is not StorageBackend.B2:
        raise StorageConfigurationError("the B2 canary requires SEC_STORAGE_BACKEND=b2")
    canary_settings = settings.model_copy(update={"sec_control_dir": str(control_dir)})
    layout = open_evidence_layout(canary_settings)
    storage = layout.storage
    requests: dict[str, int] = {}

    def observed(kind: str, operation: str) -> None:
        name = f"{kind}:{operation}"
        requests[name] = requests.get(name, 0) + 1

    if isinstance(storage, B2EvidenceStorage):
        storage.observe_requests(observed)

    cleaner: Callable[[str], int] | None = None
    if cleanup:
        config = b2_configuration(settings)
        raw = build_b2_client(config)

        def cleaner(prefix: str) -> int:
            return delete_canary_prefix(raw, config.bucket, prefix)

    report = run_canary(storage, layout.control, run_id=new_run_id(), cleanup=cleaner)
    report.requests = dict(requests)
    report.control_root = str(layout.control.path)
    return report
