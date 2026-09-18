"""The V2 derived index: every trusted artifact read once, proven, parsed, ledgered.

`extract.build_index` reads a corpus into the derived SQLite index and was
written for a directory on a local drive: one pass, one process, start again
if anything interrupts it. The V2 corpus is 37 GB in a bucket, and a pass
over it is measured in hours, so this module runs the *same* extraction —
`extract_filing`, `catalogue_from_document`, the same schema, the same row
meaning — under three rules the original did not need.

## One download does two jobs

The manifest already records every artifact's size and SHA-256. So the body
that is fetched to be parsed is the body that is verified: `read_bytes`,
compare the length, hash, compare the digest, and only then parse. There is
no separate `digest()` pass and no HEAD; a build of 38,146 artifacts costs
38,146 GETs. Bytes that do not match are never parsed, never inserted and
never ledgered — the build stops, because an artifact the bucket cannot
reproduce is a question about the evidence, not about the index.

## The ledger is the resume state

Every artifact's derived rows and its ledger row commit in one transaction,
in bounded batches, so an interruption loses at most one batch and never
leaves a half-recorded artifact. On resume, a ledgered artifact whose
recorded key and expected digest still match the manifest is skipped
without a download; everything else is processed. The build's bindings —
generation, manifest digest, parser version, schema version, input order
and the trusted counts — are written before the first artifact and checked
before any resume. A different manifest is a different corpus, and the
resume refuses rather than mixing two generations in one index.

## Derived, not authoritative

The index is a rebuildable cache under the control root's `derived/`. It is
never mirrored: the manifest and the bucket are enough to rebuild it, and a
37 GB pass reproduces the same rows. What *is* mirrored is small — the
build metadata, the semantic fingerprints and the completion marker — so
that a later phase can prove which index it read. The fingerprints are the
reproducibility contract: SHA-256 over each table's rows in a canonical
order, independent of SQLite's page layout, so two builds of the same corpus
agree even when their files do not.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.backend import ArtifactNotFoundError, validate_artifact_key
from stock_screener.sec_evidence.extract import (
    PARSER_VERSION,
    catalogue_from_document,
    extract_filing,
    open_index,
)
from stock_screener.sec_evidence.generation import GENERATION_ID
from stock_screener.sec_evidence.storage import StorageUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.manifest import Entry, Manifest
    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "INDEXED_KINDS",
    "INDEX_BUILDING_FILE",
    "INDEX_COMPLETE_MARKER",
    "INDEX_FILE",
    "INDEX_FINGERPRINTS_FILE",
    "INDEX_META_FILE",
    "INDEX_SCHEMA_VERSION",
    "INPUT_ORDER",
    "ArtifactOutcome",
    "BuildBindings",
    "IndexBuildError",
    "IndexBuildReport",
    "IndexIntegrityError",
    "IndexValidationError",
    "ResumeRefusedError",
    "TableFingerprint",
    "bindings_for",
    "build_derived_index",
    "index_complete_facts",
    "mirror_index_metadata",
    "planned_inputs",
    "semantic_fingerprints",
]

#: The finished index, beneath the control root's `derived/`.
INDEX_FILE = "sec_evidence_index_v2.db"
#: The in-progress build. Promoted to `INDEX_FILE` by one atomic rename.
INDEX_BUILDING_FILE = "sec_evidence_index_v2.building.db"
#: Small build metadata beside the index. Mirrored; the index is not.
INDEX_META_FILE = "sec_evidence_index_v2.meta.json"
INDEX_FINGERPRINTS_FILE = "sec_evidence_index_v2.fingerprints.json"
#: Deterministic completion marker. Written last, after every validation.
INDEX_COMPLETE_MARKER = "derived_index_complete.json"

#: The derived tables plus the build ledger. Bumped when either changes shape
#: or meaning, so an in-progress build under an older layout refuses to resume.
INDEX_SCHEMA_VERSION = "B2.12/1"

#: The frozen input order: submissions catalogues before filing documents
#: (filings need the acceptance stamps the catalogues carry), each kind by its
#: storage key. A resumed build and a fresh one walk the same sequence.
INPUT_ORDER = "kind:SUBMISSIONS,FILING;then:path"

#: Artifact kinds the index consumes. Any other trusted kind is ledgered as
#: `NOT_INDEXED` without a download, so it is accounted and never read.
INDEXED_KINDS = ("SUBMISSIONS", "FILING")

#: Artifacts per SQLite transaction. Bounded so a 37 GB pass is never one
#: transaction, and small enough that an interruption forfeits minutes.
DEFAULT_BATCH_SIZE = 250

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS build_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS build_ledger (
    path TEXT PRIMARY KEY, url TEXT NOT NULL, kind TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL, expected_bytes INTEGER NOT NULL,
    status TEXT NOT NULL, detail TEXT, committed_at TEXT NOT NULL
);
"""

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")

#: Every fingerprinted projection, in a total order. `securities` orders by
#: all of its columns because two rows of one filing may be identical, and
#: `cover` is the raw evidence alone — what the filing said, before any
#: normalisation or classification policy touched it — so a policy change
#: moves `securities` and leaves `cover` still.
_FINGERPRINTS: dict[str, str] = {
    "catalogue": (
        "SELECT cik, accession, form, filing_date, accepted_at, report_date, primary_document,"
        " url FROM catalogue ORDER BY cik, accession"
    ),
    "catalogue_stats": (
        "SELECT cik, recent_rows, older_blocks, qualifying FROM catalogue_stats ORDER BY cik"
    ),
    "filings": (
        "SELECT url, path, sha256, bytes, cik, accession, form, filing_date, accepted_at,"
        " report_date, evidence_date, date_source, registrant, cover_securities, dimensioned,"
        " ambiguous, parsed, parse_error, symbol_provenance, parser_version"
        " FROM filings ORDER BY url"
    ),
    "securities": (
        "SELECT url, cik, accession, evidence_date, raw_title, normalized_title, raw_symbol,"
        " normalized_symbol, raw_exchange, normalized_exchange, member, no_trading_symbol,"
        " security_class, classification_reason, source_path, source_sha256, parser_version"
        " FROM securities ORDER BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"
    ),
    "cover": (
        "SELECT url, cik, accession, evidence_date, raw_title, raw_symbol, raw_exchange, member,"
        " no_trading_symbol FROM securities ORDER BY 1,2,3,4,5,6,7,8,9"
    ),
}

_TABLES = ("filings", "securities", "catalogue", "catalogue_stats", "meta", "build_ledger")


class IndexBuildError(RuntimeError):
    """The derived index build could not proceed. Nothing about the corpus changed."""


class ResumeRefusedError(IndexBuildError):
    """An in-progress build is bound to different source evidence.

    Never recovered from automatically: continuing would merge rows derived
    from one manifest with rows derived from another and call the result an
    index of either. The operator discards the in-progress build deliberately.
    """


class IndexIntegrityError(IndexBuildError):
    """A trusted artifact did not reproduce its manifest bytes or digest.

    Not a transport fault and never retried here. The bytes were received
    and hashed; they are simply not the bytes the manifest vouches for. The
    build stops with nothing recorded for the artifact.
    """


class IndexValidationError(IndexBuildError):
    """The finished tables failed a structural check. The index is not promoted."""


class ArtifactOutcome(StrEnum):
    """What the ledger records for one trusted artifact."""

    COMMITTED = "COMMITTED"
    """Verified, parsed, and its derived rows committed."""

    FAILED = "FAILED"
    """Verified, but the parser could not read it. Recorded, never hidden."""

    NOT_INDEXED = "NOT_INDEXED"
    """A trusted kind the index does not consume. Accounted; never downloaded."""


@dataclass(frozen=True)
class BuildBindings:
    """What a build is bound to. Written before the first artifact, checked on resume.

    Attributes:
        generation_id: The evidence generation.
        manifest_sha256: Digest of the manifest file the inputs came from.
        manifest_records: Lines in the manifest.
        trusted: Artifacts the manifest vouches for.
        submissions: Trusted catalogues.
        filings: Trusted filing documents.
        logical_bytes: Their recorded size, summed.
        parser_version: `extract.PARSER_VERSION`.
        schema_version: `INDEX_SCHEMA_VERSION`.
        input_order: `INPUT_ORDER`.
    """

    generation_id: str
    manifest_sha256: str
    manifest_records: int
    trusted: int
    submissions: int
    filings: int
    logical_bytes: int
    parser_version: str = PARSER_VERSION
    schema_version: str = INDEX_SCHEMA_VERSION
    input_order: str = INPUT_ORDER

    def as_state(self) -> dict[str, str]:
        """The bindings as the string pairs the `build_state` table holds."""
        return {key: str(value) for key, value in asdict(self).items()}


@dataclass(frozen=True)
class TableFingerprint:
    """A table's row count and the digest of its rows in canonical order."""

    rows: int
    sha256: str


@dataclass
class IndexBuildReport:
    """Everything one build invocation observed. No secret can be in any field.

    Per-invocation counters (`processed`, `submissions_read`, `body_reads` …)
    describe this run only; a resumed run reports the artifacts it skipped
    separately. `ledger` is cumulative — what the build as a whole has
    committed — and is what the completion marker is written from.
    """

    generation_id: str
    manifest_sha256: str
    building_path: str
    index_path: str
    planned: int = 0
    not_indexed: int = 0
    processed: int = 0
    skipped_resume: int = 0
    unprocessed: int = 0
    stopped_early: bool = False
    submissions_read: int = 0
    submissions_parsed: int = 0
    submissions_empty: int = 0
    submissions_failed: int = 0
    catalogue_rows: int = 0
    older_blocks: int = 0
    cik_conflicts: int = 0
    accepted_conflicts: int = 0
    filings_read: int = 0
    filings_parsed: int = 0
    filings_failed: int = 0
    security_rows: int = 0
    forms: dict[str, int] = field(default_factory=dict)
    body_reads: int = 0
    body_bytes: int = 0
    read_seconds: float = 0.0
    parse_seconds: float = 0.0
    sqlite_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    byte_mismatches: int = 0
    sha_mismatches: int = 0
    missing_objects: int = 0
    storage_failures: int = 0
    reads_discarded: int = 0
    failure: str | None = None
    ledger: dict[str, int] = field(default_factory=dict)
    table_counts: dict[str, int] = field(default_factory=dict)
    fingerprints: dict[str, TableFingerprint] = field(default_factory=dict)
    diagnostics: dict[str, int] = field(default_factory=dict)
    integrity_check: str | None = None
    index_sha256: str | None = None
    index_bytes: int | None = None
    complete: bool = False

    @property
    def committed(self) -> int:
        """Artifacts the ledger records as `COMMITTED`, cumulatively."""
        return sum(v for k, v in self.ledger.items() if k.endswith(f":{ArtifactOutcome.COMMITTED}"))

    @property
    def parse_failures(self) -> int:
        """Artifacts the ledger records as `FAILED`, cumulatively."""
        return sum(v for k, v in self.ledger.items() if k.endswith(f":{ArtifactOutcome.FAILED}"))

    def summary(self) -> list[str]:
        """Lines for a terminal."""
        rate = self.body_bytes / self.read_seconds if self.read_seconds else 0.0
        lines = [
            f"{self.generation_id} derived index — manifest {self.manifest_sha256[:16]}…",
            f"  inputs: planned {self.planned} (+{self.not_indexed} not indexed), "
            f"processed {self.processed}, skipped by resume {self.skipped_resume}, "
            f"unprocessed {self.unprocessed}",
            f"  submissions: read {self.submissions_read}, parsed {self.submissions_parsed}, "
            f"empty {self.submissions_empty}, failed {self.submissions_failed}, "
            f"catalogue rows {self.catalogue_rows}, older blocks {self.older_blocks}, "
            f"cik conflicts {self.cik_conflicts}, accepted conflicts {self.accepted_conflicts}",
            f"  filings: read {self.filings_read}, parsed {self.filings_parsed}, "
            f"failed {self.filings_failed}, security rows {self.security_rows}, "
            f"forms {dict(sorted(self.forms.items()))}",
            f"  integrity: reads {self.body_reads}, bytes {self.body_bytes:,}, "
            f"byte mismatches {self.byte_mismatches}, sha mismatches {self.sha_mismatches}, "
            f"missing {self.missing_objects}, storage failures {self.storage_failures}, "
            f"discarded by an early stop {self.reads_discarded}",
            f"  time: elapsed {self.elapsed_seconds:.1f}s; summed over fetches: read "
            f"{self.read_seconds:.1f}s ({rate / 1024**2:.1f} MB/s per stream, "
            f"{self.read_seconds / self.body_reads if self.body_reads else 0.0:.3f}s per GET); "
            f"parse {self.parse_seconds:.1f}s, sqlite {self.sqlite_seconds:.1f}s",
            f"  ledger: {dict(sorted(self.ledger.items()))}",
        ]
        if self.stopped_early:
            lines.append("  STOPPED EARLY by request; the build is resumable")
        if self.failure:
            lines.append(f"  STOPPED: {self.failure}")
        if self.complete:
            lines.append(f"  integrity_check: {self.integrity_check}")
            lines.append(f"  tables: {self.table_counts}")
            for name, print_ in self.fingerprints.items():
                lines.append(f"  fingerprint {name}: rows {print_.rows}, sha256 {print_.sha256}")
            lines.append(f"  diagnostics: {self.diagnostics}")
            lines.append(
                f"  index: {self.index_path} ({self.index_bytes:,} bytes) "
                f"sha256 {self.index_sha256}"
            )
            lines.append("  DERIVED INDEX COMPLETE")
        return lines


# ----------------------------------------------------------------------------
# Inputs and bindings
# ----------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def planned_inputs(manifest: Manifest) -> tuple[list[Entry], list[Entry]]:
    """The trusted artifacts, in the frozen input order.

    Every trusted record must carry a storage key inside the artifact
    prefixes, a digest and a size: an artifact that cannot be verified cannot
    be indexed, and an unsafe key is an integrity violation rather than a
    file to skip.

    Args:
        manifest: The provenance log, replayed to its latest state.

    Returns:
        `(indexed, not_indexed)` — the artifacts the index consumes, catalogues
        first and then filings, each by key; and every other trusted kind.

    Raises:
        IndexIntegrityError: If a trusted record lacks a digest or a size.
        UnsafeStorageKeyError: If a trusted record's key is not an artifact key.
    """
    trusted = [entry for entry in manifest.latest_by_url().values() if entry.is_stored]
    for entry in trusted:
        validate_artifact_key(entry.path or "")
        if not entry.sha256 or entry.bytes is None:
            raise IndexIntegrityError(
                f"{entry.path} is trusted but the manifest records no digest or size for it"
            )
    rank = {kind: position for position, kind in enumerate(INDEXED_KINDS)}
    indexed = sorted(
        (entry for entry in trusted if entry.request_kind in rank),
        key=lambda entry: (rank[entry.request_kind or ""], entry.path or ""),
    )
    other = sorted(
        (entry for entry in trusted if entry.request_kind not in rank),
        key=lambda entry: (entry.request_kind or "", entry.path or ""),
    )
    return indexed, other


def bindings_for(manifest: Manifest, *, generation_id: str = GENERATION_ID) -> BuildBindings:
    """Reduce the manifest to what a build is bound to.

    Args:
        manifest: The provenance log.
        generation_id: The generation it belongs to.

    Returns:
        The bindings, including the manifest file's own digest.
    """
    records = sum(1 for _ in manifest.entries())
    trusted = [entry for entry in manifest.latest_by_url().values() if entry.is_stored]
    return BuildBindings(
        generation_id=generation_id,
        manifest_sha256=_sha256_of(manifest.path),
        manifest_records=records,
        trusted=len(trusted),
        submissions=sum(1 for e in trusted if e.request_kind == "SUBMISSIONS"),
        filings=sum(1 for e in trusted if e.request_kind == "FILING"),
        logical_bytes=sum(e.bytes or 0 for e in trusted),
    )


def index_complete_facts(derived: Path) -> dict[str, object] | None:
    """The completion marker's facts, when a build has finished."""
    marker = derived / INDEX_COMPLETE_MARKER
    if not marker.is_file():
        return None
    facts: dict[str, object] = json.loads(marker.read_text(encoding="utf-8"))
    return facts


# ----------------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------------


def _open_building(path: Path) -> sqlite3.Connection:
    """The derived schema plus the ledger, on the in-progress file."""
    db = open_index(path)
    db.executescript(_LEDGER_SCHEMA)
    return db


def _state(db: sqlite3.Connection) -> dict[str, str]:
    return {str(k): str(v) for k, v in db.execute("SELECT key, value FROM build_state")}


def _bind(db: sqlite3.Connection, bindings: BuildBindings, planned: Sequence[Entry]) -> bool:
    """Write the bindings on a fresh build, or verify them on a resume.

    Returns:
        Whether this is a resume of an in-progress build.

    Raises:
        ResumeRefusedError: If the in-progress build is bound to anything
            else, or its ledger names an artifact the manifest no longer
            vouches for in the same bytes.
    """
    recorded = _state(db)
    expected = bindings.as_state()
    if not recorded:
        db.executemany(
            "INSERT INTO build_state VALUES (?, ?)",
            [*expected.items(), ("started_at", datetime.now(UTC).isoformat())],
        )
        db.commit()
        return False
    drift = {
        key: (recorded.get(key), value)
        for key, value in expected.items()
        if recorded.get(key) != value
    }
    if drift:
        raise ResumeRefusedError(
            "the in-progress build is bound to different source evidence: "
            + ", ".join(f"{k} was {was!r}, now {now!r}" for k, (was, now) in sorted(drift.items()))
            + " — discard it deliberately (--fresh) rather than mixing generations"
        )
    by_path = {entry.path: entry for entry in planned}
    for path, sha, size, kind in db.execute(
        "SELECT path, expected_sha256, expected_bytes, kind FROM build_ledger"
    ):
        entry = by_path.get(str(path))
        if entry is None:
            raise ResumeRefusedError(
                f"the ledger holds {path}, which the manifest does not vouch for"
            )
        if entry.sha256 != sha or entry.bytes != size or entry.request_kind != kind:
            raise ResumeRefusedError(
                f"the ledger recorded {path} as {str(sha)[:16]}…/{size} bytes; "
                f"the manifest now says {str(entry.sha256)[:16]}…/{entry.bytes}"
            )
    return True


def _ledgered(db: sqlite3.Connection) -> set[str]:
    return {str(path) for (path,) in db.execute("SELECT path FROM build_ledger")}


def _ledger_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        f"{kind}:{status}": int(count)
        for kind, status, count in db.execute(
            "SELECT kind, status, COUNT(*) FROM build_ledger GROUP BY kind, status"
        )
    }


def _record(
    db: sqlite3.Connection, entry: Entry, outcome: ArtifactOutcome, detail: str | None
) -> None:
    db.execute(
        "INSERT INTO build_ledger VALUES (?,?,?,?,?,?,?,?)",
        (
            entry.path,
            entry.url,
            entry.request_kind,
            entry.sha256,
            entry.bytes,
            str(outcome),
            detail,
            datetime.now(UTC).isoformat(),
        ),
    )


# ----------------------------------------------------------------------------
# One artifact
# ----------------------------------------------------------------------------


def _fetch(storage: EvidenceStorage, entry: Entry) -> tuple[bytes, float]:
    """One GET, timed. Safe to run off the main thread: it touches no shared state.

    Raises:
        ArtifactNotFoundError: If the object is absent.
        StorageUnavailableError: If the backend could not answer.
    """
    key = validate_artifact_key(entry.path or "")
    started = time.perf_counter()
    data = storage.read_bytes(key)
    return data, time.perf_counter() - started


def _verify(
    entry: Entry, fetched: tuple[bytes, float] | Exception, report: IndexBuildReport
) -> bytes:
    """Account for one fetch and prove the body is the one the manifest vouches for.

    Raises:
        IndexIntegrityError: If the object is absent or its bytes do not
            reproduce the manifest's size and digest.
        StorageUnavailableError: If the backend could not answer. Propagated
            untouched so the caller can stop with a resumable build.
    """
    key = entry.path or ""
    if isinstance(fetched, ArtifactNotFoundError):
        report.missing_objects += 1
        raise IndexIntegrityError(f"{key} is trusted but absent from storage") from fetched
    if isinstance(fetched, StorageUnavailableError):
        report.storage_failures += 1
        raise fetched
    if isinstance(fetched, Exception):  # pragma: no cover - nothing else is fetched
        raise fetched
    data, seconds = fetched
    report.read_seconds += seconds
    report.body_reads += 1
    report.body_bytes += len(data)
    if len(data) != entry.bytes:
        report.byte_mismatches += 1
        raise IndexIntegrityError(f"{key} is {len(data)} bytes; the manifest records {entry.bytes}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry.sha256:
        report.sha_mismatches += 1
        raise IndexIntegrityError(
            f"{key} hashes to {digest[:16]}…; the manifest records {str(entry.sha256)[:16]}…"
        )
    return data


class _Prefetcher:
    """Bodies in the frozen order, with up to `depth` downloads in flight.

    The consumer sees exactly the sequence it would have seen fetching one
    at a time — entry, then its body or the exception its fetch raised — so
    verification, parsing and every insert stay in order. Only the waiting
    overlaps. A depth of one fetches inline on the calling thread; anything
    more runs the fetches on a pool and never more than `depth` at once, so
    memory is bounded by `depth` artifacts.

    Stopping early cancels every fetch that has not started; the ones in
    flight finish and are discarded, and their number is reported, because a
    discarded body is a GET the resume will spend again.
    """

    def __init__(self, storage: EvidenceStorage, entries: Iterable[Entry], depth: int) -> None:
        self._storage = storage
        self._entries = iter(entries)
        self._depth = max(1, depth)
        self._pending: deque[tuple[Entry, Future[tuple[bytes, float]]]] = deque()
        self._pool: ThreadPoolExecutor | None = None
        self.discarded = 0

    def __enter__(self) -> _Prefetcher:
        if self._depth > 1:
            self._pool = ThreadPoolExecutor(max_workers=self._depth, thread_name_prefix="fetch")
        return self

    def __exit__(self, *_: object) -> None:
        for _entry, future in self._pending:
            if not future.cancel():
                self.discarded += 1
        self._pending.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def _fill(self) -> None:
        assert self._pool is not None  # noqa: S101 - only called on the pooled path
        while len(self._pending) < self._depth:
            entry = next(self._entries, None)
            if entry is None:
                return
            self._pending.append((entry, self._pool.submit(_fetch, self._storage, entry)))

    def __iter__(self) -> Iterator[tuple[Entry, tuple[bytes, float] | Exception]]:
        if self._pool is None:
            for entry in self._entries:
                try:
                    yield entry, _fetch(self._storage, entry)
                except (ArtifactNotFoundError, StorageUnavailableError) as exc:
                    yield entry, exc
            return
        self._fill()
        while self._pending:
            entry, future = self._pending.popleft()
            try:
                result: tuple[bytes, float] | Exception = future.result()
            except (ArtifactNotFoundError, StorageUnavailableError) as exc:
                result = exc
            self._fill()
            yield entry, result


def _index_catalogue(
    db: sqlite3.Connection, entry: Entry, data: bytes, report: IndexBuildReport
) -> tuple[ArtifactOutcome, str | None]:
    """Parse one submissions catalogue and insert what it describes.

    Malformed and empty are different answers. Bytes that are not a JSON
    object, or an object the catalogue reader cannot walk, are `FAILED` with
    the reason; a well-formed catalogue naming no qualifying filing is
    `COMMITTED` and counted empty.
    """
    report.submissions_read += 1
    parse_started = time.perf_counter()
    try:
        document: object = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.submissions_failed += 1
        report.parse_seconds += time.perf_counter() - parse_started
        return ArtifactOutcome.FAILED, f"INVALID_JSON: {type(exc).__name__}"
    if not isinstance(document, dict):
        report.submissions_failed += 1
        report.parse_seconds += time.perf_counter() - parse_started
        return ArtifactOutcome.FAILED, f"NOT_AN_OBJECT: {type(document).__name__}"
    try:
        rows, recent_rows, older = catalogue_from_document(entry, document)
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        report.submissions_failed += 1
        report.parse_seconds += time.perf_counter() - parse_started
        return ArtifactOutcome.FAILED, f"MALFORMED_CATALOGUE: {type(exc).__name__}: {exc}"
    report.parse_seconds += time.perf_counter() - parse_started
    stated = document.get("cik")
    try:
        if stated is not None and entry.cik is not None and int(stated) != entry.cik:
            report.cik_conflicts += 1
    except (TypeError, ValueError):
        report.cik_conflicts += 1

    write_started = time.perf_counter()
    db.executemany(
        "INSERT OR REPLACE INTO catalogue VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                r.cik,
                r.accession,
                r.form,
                r.filing_date,
                r.accepted_at,
                r.report_date,
                r.primary_document,
                r.url,
            )
            for r in rows
        ],
    )
    db.execute(
        "INSERT OR REPLACE INTO catalogue_stats VALUES (?,?,?,?)",
        (entry.cik, recent_rows, older, len(rows)),
    )
    report.sqlite_seconds += time.perf_counter() - write_started
    report.submissions_parsed += 1
    report.catalogue_rows += len(rows)
    report.older_blocks += older
    if not rows:
        report.submissions_empty += 1
    return ArtifactOutcome.COMMITTED, None


def _index_filing(
    db: sqlite3.Connection,
    entry: Entry,
    data: bytes,
    accepted: Mapping[str, str],
    report: IndexBuildReport,
) -> tuple[ArtifactOutcome, str | None]:
    """Parse one filing document and insert its row and its cover rows."""
    report.filings_read += 1
    parse_started = time.perf_counter()
    try:
        filing, securities = extract_filing(
            entry, data.decode("utf-8", errors="replace"), accepted=accepted
        )
    except (AttributeError, TypeError, ValueError, KeyError, RecursionError) as exc:
        report.filings_failed += 1
        report.parse_seconds += time.perf_counter() - parse_started
        return ArtifactOutcome.FAILED, f"PARSER_RAISED: {type(exc).__name__}: {exc}"
    report.parse_seconds += time.perf_counter() - parse_started

    write_started = time.perf_counter()
    db.execute(
        "INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            filing.url,
            filing.path,
            filing.sha256,
            filing.bytes,
            filing.cik,
            filing.accession,
            filing.form,
            filing.filing_date,
            filing.accepted_at,
            filing.report_date,
            filing.evidence_date,
            filing.date_source,
            filing.registrant,
            filing.cover_securities,
            int(filing.dimensioned),
            int(filing.ambiguous),
            int(filing.parsed),
            filing.parse_error,
            filing.symbol_provenance,
            PARSER_VERSION,
        ),
    )
    db.executemany(
        "INSERT INTO securities (url, cik, accession, evidence_date, raw_title,"
        " normalized_title, raw_symbol, normalized_symbol, raw_exchange,"
        " normalized_exchange, member, no_trading_symbol, security_class,"
        " classification_reason, source_path, source_sha256, parser_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r.url,
                r.cik,
                r.accession,
                r.evidence_date,
                r.raw_title,
                r.normalized_title,
                r.raw_symbol,
                r.normalized_symbol,
                r.raw_exchange,
                r.normalized_exchange,
                r.member,
                int(r.no_trading_symbol),
                r.security_class,
                r.classification_reason,
                r.source_path,
                r.source_sha256,
                PARSER_VERSION,
            )
            for r in securities
        ],
    )
    report.sqlite_seconds += time.perf_counter() - write_started
    report.forms[filing.form or "UNKNOWN"] = report.forms.get(filing.form or "UNKNOWN", 0) + 1
    report.security_rows += len(securities)
    if filing.parsed:
        report.filings_parsed += 1
        return ArtifactOutcome.COMMITTED, None
    report.filings_failed += 1
    return ArtifactOutcome.FAILED, f"UNPARSEABLE: {filing.parse_error}"


def _accepted_map(db: sqlite3.Connection, report: IndexBuildReport) -> dict[str, str]:
    """Acceptance stamps by accession, from the committed catalogue rows.

    Read from the table rather than carried in memory so that a resumed build
    sees exactly what a fresh one does. Where two catalogues stamp one
    accession differently — co-registrants filing one document — the later
    stamp is kept: the conservative point-in-time answer, and counted.
    """
    accepted: dict[str, str] = {}
    for accession, stamp in db.execute(
        "SELECT accession, accepted_at FROM catalogue WHERE accepted_at IS NOT NULL"
        " ORDER BY accession, cik"
    ):
        key, value = str(accession), str(stamp)
        if key in accepted and accepted[key] != value:
            report.accepted_conflicts += 1
            accepted[key] = max(accepted[key], value)
        else:
            accepted[key] = value
    return accepted


# ----------------------------------------------------------------------------
# The build
# ----------------------------------------------------------------------------


def build_derived_index(
    storage: EvidenceStorage,
    manifest: Manifest,
    derived: Path,
    *,
    generation_id: str = GENERATION_ID,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stop_after: int | None = None,
    fresh: bool = False,
    prefetch: int = 1,
    progress: Callable[[str], None] | None = None,
    facts: Mapping[str, object] | None = None,
) -> IndexBuildReport:
    """Build, or resume building, the derived index from the trusted corpus.

    Args:
        storage: The artifact backend. Read only; never listed.
        manifest: The provenance log. Only artifacts it vouches for are read.
        derived: The control root's `derived/` directory.
        generation_id: The generation the manifest belongs to.
        batch_size: Artifacts per transaction.
        stop_after: Stop cleanly after this many artifacts have been
            processed by this invocation — the operator's interruption, and
            the resume test's.
        fresh: Discard an in-progress build and ignore an existing completion
            marker. Never touches the corpus or the manifest.
        prefetch: Downloads in flight at once. One is strictly sequential;
            more overlaps the waiting and changes nothing about the order in
            which artifacts are verified, parsed and committed.
        progress: Receives one line per committed batch.
        facts: Extra provenance recorded in the metadata file — the universe
            and resolution digests, the baseline marker, the repository commit.

    Returns:
        The report. `complete` is true only when every planned input is
        ledgered `COMMITTED`, every validation passed, the fingerprints are
        written and the index has been promoted to its final name.

    Raises:
        ResumeRefusedError: If an in-progress build is bound to other evidence.
        IndexBuildError: If a completed build already exists for these
            bindings and `fresh` was not asked for.
    """
    started = time.perf_counter()
    say = progress or (lambda line: None)
    building = derived / INDEX_BUILDING_FILE
    final = derived / INDEX_FILE
    bindings = bindings_for(manifest, generation_id=generation_id)
    indexed, other = planned_inputs(manifest)
    report = IndexBuildReport(
        generation_id=generation_id,
        manifest_sha256=bindings.manifest_sha256,
        building_path=str(building),
        index_path=str(final),
        planned=len(indexed),
        not_indexed=len(other),
    )

    if fresh:
        for stale in (building, building.with_name(building.name + "-journal")):
            stale.unlink(missing_ok=True)
    else:
        done = index_complete_facts(derived)
        if done is not None and all(
            done.get(key) == value
            for key, value in (
                ("manifest_sha256", bindings.manifest_sha256),
                ("parser_version", bindings.parser_version),
                ("schema_version", bindings.schema_version),
            )
        ):
            raise IndexBuildError(
                f"{final.name} is already complete for manifest "
                f"{bindings.manifest_sha256[:16]}… under parser {bindings.parser_version}; "
                "pass fresh=True to rebuild it"
            )

    derived.mkdir(parents=True, exist_ok=True)
    with closing(_open_building(building)) as db:
        resumed = _bind(db, bindings, [*indexed, *other])
        ledgered = _ledgered(db)
        say(
            f"{'resuming' if resumed else 'starting'} {building.name}: {len(indexed)} planned, "
            f"{len(ledgered)} already ledgered"
        )
        accepted: dict[str, str] | None = None
        in_batch = 0
        db.execute("BEGIN")
        for entry in other:
            if entry.path not in ledgered:
                _record(db, entry, ArtifactOutcome.NOT_INDEXED, f"kind={entry.request_kind}")
                in_batch += 1
        remaining = [entry for entry in indexed if entry.path not in ledgered]
        report.skipped_resume = len(indexed) - len(remaining)
        if stop_after is not None and len(remaining) > stop_after:
            report.stopped_early = True
            remaining = remaining[:stop_after]
        with _Prefetcher(storage, remaining, prefetch) as bodies:
            for entry, fetched in bodies:
                if entry.request_kind == "FILING" and accepted is None:
                    # Every catalogue precedes every filing, so the stamps are
                    # all committed once the first filing is reached; make them
                    # durable before reading them back.
                    db.commit()
                    in_batch = 0
                    accepted = _accepted_map(db, report)
                    db.execute("BEGIN")
                try:
                    data = _verify(entry, fetched, report)
                except (IndexIntegrityError, StorageUnavailableError) as exc:
                    report.failure = f"{type(exc).__name__}: {exc}"
                    log.error(
                        "derived index build stopped", key=entry.path, error=type(exc).__name__
                    )
                    break
                _index_one(db, entry, data, accepted, report)
                in_batch += 1
                if in_batch >= batch_size:
                    commit_started = time.perf_counter()
                    db.commit()
                    report.sqlite_seconds += time.perf_counter() - commit_started
                    in_batch = 0
                    say(_progress_line(report))
                    db.execute("BEGIN")
        report.reads_discarded = bodies.discarded
        commit_started = time.perf_counter()
        db.commit()
        report.sqlite_seconds += time.perf_counter() - commit_started
        report.ledger = _ledger_counts(db)
        report.unprocessed = report.planned - report.committed - report.parse_failures

    if report.failure is None and not report.stopped_early:
        try:
            _finalize(building, final, derived, bindings, report, facts or {})
        except (IndexValidationError, sqlite3.Error) as exc:
            report.failure = f"{type(exc).__name__}: {exc}"
            log.error("derived index validation failed", error=type(exc).__name__)
    report.elapsed_seconds = time.perf_counter() - started
    say(_progress_line(report))
    return report


def _index_one(
    db: sqlite3.Connection,
    entry: Entry,
    data: bytes,
    accepted: Mapping[str, str] | None,
    report: IndexBuildReport,
) -> None:
    """Parse and insert one verified artifact and its ledger row, atomically within the batch.

    A savepoint bounds the artifact: an unexpected SQLite error rolls back
    this artifact alone, and what the batch had already inserted for earlier
    artifacts still commits. The ledger row is the last insert, so a ledgered
    artifact is always a whole one.
    """
    db.execute("SAVEPOINT artifact")
    try:
        if entry.request_kind == "SUBMISSIONS":
            outcome, detail = _index_catalogue(db, entry, data, report)
        else:
            outcome, detail = _index_filing(db, entry, data, accepted or {}, report)
        _record(db, entry, outcome, detail)
        db.execute("RELEASE artifact")
    except sqlite3.Error:
        db.execute("ROLLBACK TO artifact")
        db.execute("RELEASE artifact")
        raise
    report.processed += 1


def _progress_line(report: IndexBuildReport) -> str:
    rate = report.body_bytes / report.read_seconds if report.read_seconds else 0.0
    return (
        f"processed {report.processed}/{report.planned - report.skipped_resume}: "
        f"catalogues {report.submissions_parsed} ({report.catalogue_rows} rows), "
        f"filings {report.filings_parsed} ({report.security_rows} securities), "
        f"failed {report.submissions_failed + report.filings_failed}; "
        f"{report.body_bytes / 1024**2:,.0f} MB at {rate / 1024**2:.1f} MB/s"
    )


# ----------------------------------------------------------------------------
# Finalisation: validate, fingerprint, promote, describe
# ----------------------------------------------------------------------------


def semantic_fingerprints(db: sqlite3.Connection) -> dict[str, TableFingerprint]:
    """Digest each table's rows in canonical order — the reproducibility contract.

    Rows are serialised as compact JSON arrays, one per line, and hashed in
    the query's total order. Autoincrement ids and the build's own timestamps
    are never part of a row, so two builds of one corpus agree whatever
    SQLite did with the pages.
    """
    prints: dict[str, TableFingerprint] = {}
    for name, query in _FINGERPRINTS.items():
        digest = hashlib.sha256()
        rows = 0
        for row in db.execute(query):
            digest.update(json.dumps(list(row), separators=(",", ":"), ensure_ascii=False).encode())
            digest.update(b"\n")
            rows += 1
        prints[name] = TableFingerprint(rows=rows, sha256=digest.hexdigest())
    return prints


def _count(db: sqlite3.Connection, sql: str) -> int:
    return int(db.execute(sql).fetchone()[0])


def _validate(db: sqlite3.Connection, report: IndexBuildReport) -> None:
    """The structural checks a finished index must pass, and the diagnostics it reports.

    Raises:
        IndexValidationError: On the first hard failure.
    """
    (verdict,) = db.execute("PRAGMA integrity_check").fetchone()
    report.integrity_check = str(verdict)
    if verdict != "ok":
        raise IndexValidationError(f"SQLite integrity_check: {verdict}")
    committed_filings = report.ledger.get(f"FILING:{ArtifactOutcome.COMMITTED}", 0)
    committed_catalogues = report.ledger.get(f"SUBMISSIONS:{ArtifactOutcome.COMMITTED}", 0)
    hard = {
        "filings_without_identity": _count(
            db, "SELECT COUNT(*) FROM filings WHERE cik IS NULL OR accession IS NULL"
        ),
        "duplicate_filing_identities": _count(
            db,
            "SELECT COUNT(*) FROM (SELECT cik, accession FROM filings"
            " GROUP BY cik, accession HAVING COUNT(*) > 1)",
        ),
        "securities_without_filing": _count(
            db,
            "SELECT COUNT(*) FROM securities s LEFT JOIN filings f ON f.url = s.url"
            " WHERE f.url IS NULL",
        ),
        "catalogue_without_identity": _count(
            db, "SELECT COUNT(*) FROM catalogue WHERE cik IS NULL OR accession = ''"
        ),
        "filing_rows_vs_committed": abs(
            _count(db, "SELECT COUNT(*) FROM filings") - committed_filings
        ),
        "catalogue_stats_vs_committed": abs(
            _count(db, "SELECT COUNT(*) FROM catalogue_stats") - committed_catalogues
        ),
    }
    failed = {name: value for name, value in hard.items() if value}
    if failed:
        raise IndexValidationError(f"structural checks failed: {failed}")

    diagnostics: dict[str, int] = dict(hard)
    for source, count in db.execute(
        "SELECT date_source, COUNT(*) FROM filings GROUP BY date_source"
    ):
        diagnostics[f"date_source:{source}"] = int(count)
    diagnostics["catalogue_rows_without_accepted_at"] = _count(
        db, "SELECT COUNT(*) FROM catalogue WHERE accepted_at IS NULL"
    )
    diagnostics["filings_with_invalid_accession"] = sum(
        1
        for (accession,) in db.execute("SELECT accession FROM filings")
        if not _ACCESSION.match(str(accession))
    )
    diagnostics["catalogue_rows_with_invalid_accession"] = sum(
        1
        for (accession,) in db.execute("SELECT accession FROM catalogue")
        if not _ACCESSION.match(str(accession))
    )
    diagnostics["security12b_title_rows"] = _count(
        db, "SELECT COUNT(*) FROM securities WHERE raw_title IS NOT NULL"
    )
    diagnostics["trading_symbol_rows"] = _count(
        db, "SELECT COUNT(*) FROM securities WHERE raw_symbol IS NOT NULL"
    )
    diagnostics["security_exchange_name_rows"] = _count(
        db, "SELECT COUNT(*) FROM securities WHERE raw_exchange IS NOT NULL"
    )
    diagnostics["co_registrant_accessions"] = _count(
        db,
        "SELECT COUNT(*) FROM (SELECT accession FROM filings"
        " GROUP BY accession HAVING COUNT(DISTINCT cik) > 1)",
    )
    diagnostics["co_registrant_surplus_rows"] = _count(
        db,
        "SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(DISTINCT cik) AS n FROM filings"
        " GROUP BY accession HAVING n > 1)",
    )
    for form, count in db.execute(
        "SELECT COALESCE(form, 'UNKNOWN'), COUNT(*) FROM filings GROUP BY form"
    ):
        diagnostics[f"form:{form}"] = int(count)
    report.diagnostics = diagnostics


def _finalize(
    building: Path,
    final: Path,
    derived: Path,
    bindings: BuildBindings,
    report: IndexBuildReport,
    facts: Mapping[str, object],
) -> None:
    """Refuse unless everything is committed and sound; then promote and describe."""
    if report.parse_failures:
        report.failure = (
            f"{report.parse_failures} artifact(s) ledgered FAILED; the index is not promoted"
            " — see build_ledger in the in-progress file"
        )
        return
    if report.unprocessed:
        report.failure = f"{report.unprocessed} planned input(s) are not ledgered"
        return

    with closing(_open_building(building)) as db:
        _validate(db, report)
        report.fingerprints = semantic_fingerprints(db)
        report.table_counts = {
            name: _count(db, f"SELECT COUNT(*) FROM {name}")  # noqa: S608 - fixed table names
            for name in _TABLES
        }
        completed_at = datetime.now(UTC).isoformat()
        db.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [
                ("parser_version", PARSER_VERSION),
                ("built_at", completed_at),
                ("generation_id", bindings.generation_id),
                ("manifest_sha256", bindings.manifest_sha256),
                ("schema_version", bindings.schema_version),
                ("input_order", bindings.input_order),
            ],
        )
        db.commit()
        started_at = _state(db).get("started_at")
        report.table_counts["meta"] = _count(db, "SELECT COUNT(*) FROM meta")

    building.replace(final)
    report.index_bytes = final.stat().st_size
    report.index_sha256 = _sha256_of(final)
    prints = {name: asdict(print_) for name, print_ in report.fingerprints.items()}

    meta: dict[str, object] = {
        **bindings.as_state(),
        "manifest_records": bindings.manifest_records,
        "trusted": bindings.trusted,
        "submissions": bindings.submissions,
        "filings": bindings.filings,
        "logical_bytes": bindings.logical_bytes,
        **dict(facts),
        "index_file": final.name,
        "index_sha256": report.index_sha256,
        "index_bytes": report.index_bytes,
        "build_started_at": started_at,
        "build_completed_at": completed_at,
        "planned": report.planned,
        "not_indexed": report.not_indexed,
        "ledger": report.ledger,
        "parse_failures": report.parse_failures,
        "table_counts": report.table_counts,
        "fingerprints": prints,
        "integrity_check": report.integrity_check,
        "diagnostics": report.diagnostics,
        "this_invocation": {
            "processed": report.processed,
            "skipped_resume": report.skipped_resume,
            "body_reads": report.body_reads,
            "body_bytes": report.body_bytes,
            "read_seconds": round(report.read_seconds, 3),
            "parse_seconds": round(report.parse_seconds, 3),
            "sqlite_seconds": round(report.sqlite_seconds, 3),
        },
    }
    _write_json(derived / INDEX_META_FILE, meta)
    _write_json(
        derived / INDEX_FINGERPRINTS_FILE,
        {
            "generation_id": bindings.generation_id,
            "manifest_sha256": bindings.manifest_sha256,
            "parser_version": bindings.parser_version,
            "schema_version": bindings.schema_version,
            "fingerprints": prints,
        },
    )
    _write_json(
        derived / INDEX_COMPLETE_MARKER,
        {
            "stage": "DERIVED_INDEX_COMPLETE",
            "generation_id": bindings.generation_id,
            "manifest_sha256": bindings.manifest_sha256,
            "parser_version": bindings.parser_version,
            "schema_version": bindings.schema_version,
            "trusted": bindings.trusted,
            "planned": report.planned,
            "parsed": report.committed,
            "parse_failures": report.parse_failures,
            "index_file": final.name,
            "index_sha256": report.index_sha256,
            "index_bytes": report.index_bytes,
            "fingerprints": prints,
        },
    )
    report.complete = True
    log.info(
        "derived index complete",
        index=str(final),
        sha256=report.index_sha256[:16],
        tables=report.table_counts,
    )


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Write a small control file atomically, sorted, with a trailing newline."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


# ----------------------------------------------------------------------------
# The mirror
# ----------------------------------------------------------------------------

#: Each small file goes to its own snapshot kind so the newest descriptor of a
#: kind is always the newest copy of that file, never of a neighbour.
_MIRRORED = (
    (INDEX_META_FILE, "derived/index-meta"),
    (INDEX_FINGERPRINTS_FILE, "derived/index-fingerprints"),
    (INDEX_COMPLETE_MARKER, "derived/index-complete"),
)


def mirror_index_metadata(
    mirror: ControlMirror, derived: Path, *, generation_id: str = GENERATION_ID
) -> list[MirrorRecord]:
    """Mirror the build's small metadata as immutable snapshots, and read each back.

    The index itself is never mirrored — it is rebuildable from the manifest
    and the corpus, and a 200 MB object in the control namespace would be
    a recovery-critical file that recovery does not need.

    Args:
        mirror: The control mirror for the generation.
        derived: The `derived/` directory holding the three files.
        generation_id: Recorded in each snapshot descriptor.

    Returns:
        The file records, one per mirrored file.

    Raises:
        IndexBuildError: If a file is missing, or the newest remote snapshot
            of it does not hash to the local bytes after the mirror.
        MirrorConflictError: From the mirror, on a read-back mismatch.
    """
    records = []
    for name, kind in _MIRRORED:
        local = derived / name
        if not local.is_file():
            raise IndexBuildError(f"{local} does not exist; nothing to mirror")
        file_record, _ = mirror.mirror_snapshot(local, kind, generation_id=generation_id)
        newest = mirror.newest_snapshot(kind)
        if newest is None or newest[0].get("sha256") != file_record.sha256:
            raise IndexBuildError(f"the newest mirrored snapshot of {name} is not the local file")
        records.append(file_record)
    return records
