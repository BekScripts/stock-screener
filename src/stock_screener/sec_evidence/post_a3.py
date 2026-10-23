"""The post-A3 derived index: the verified baseline index plus exactly the A3 delta.

B2.12 read the whole baseline corpus out of the bucket and parsed it; B2.15
body-verified every A3 artifact as it was written. Neither is repeated here.
The final index is built *incrementally*: a verified copy of the B2.12 index
is the starting point, and only the 61,628 A3 filings are read — each one
GET, its length and digest checked against the manifest before the parser
sees a byte — and parsed with the same extraction the baseline was parsed
with. The baseline index itself is never opened for writing.

## What binds the build

The build state records the final manifest digest, the baseline index and
manifest digests, the authorized plan and the A3 completion marker, the
parser and schema versions, and the delta itself — every A3 artifact's key,
expected size and digest. A resume checks all of it and refuses anything
else. The delta is derived from the final manifest and the baseline ledger
and must equal the authorized plan's identities exactly; it is never
inferred from dates, listing order or modification times.

## What the baseline invariance check proves

After the delta is committed, the fingerprint queries B2.12 defined are run
again over the rows that are *not* the delta's — `filings` and `securities`
by URL, `catalogue` and `catalogue_stats` whole — and must reproduce the
B2.12 fingerprints digit for digit. Adding the delta changed no row the
baseline parsed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.derived import (
    INDEX_COMPLETE_MARKER,
    INDEX_FILE,
    INDEX_META_FILE,
    ArtifactOutcome,
    IndexBuildReport,
    IndexIntegrityError,
    IndexValidationError,
    TableFingerprint,
    _accepted_map,
    _index_one,
    _ledger_counts,
    _ledgered,
    _open_building,
    _Prefetcher,
    _validate,
    _verify,
    semantic_fingerprints,
)
from stock_screener.sec_evidence.extract import PARSER_VERSION
from stock_screener.sec_evidence.generation import GENERATION_ID
from stock_screener.sec_evidence.storage import StorageUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.manifest import Entry, Manifest
    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "POST_A3_BUILDING_FILE",
    "POST_A3_COMPLETE_MARKER",
    "POST_A3_FINGERPRINTS_FILE",
    "POST_A3_INDEX_FILE",
    "POST_A3_META_FILE",
    "POST_A3_MIRROR_PREFIX",
    "POST_A3_SCHEMA_VERSION",
    "BaselineIndexFacts",
    "DeltaSet",
    "PostA3Bindings",
    "PostA3Error",
    "PostA3Report",
    "baseline_paths",
    "build_post_a3_index",
    "delta_entries",
    "mirror_post_a3_metadata",
    "post_a3_complete_facts",
    "verify_baseline_index",
]

#: The finished post-A3 index, beneath the control root's `derived/`.
POST_A3_INDEX_FILE = "sec_evidence_index_v2_post_a3.db"
#: The in-progress build: a copy of the baseline index plus the delta so far.
POST_A3_BUILDING_FILE = "sec_evidence_index_v2_post_a3.building.db"
POST_A3_META_FILE = "sec_evidence_index_v2_post_a3.meta.json"
POST_A3_FINGERPRINTS_FILE = "sec_evidence_index_v2_post_a3.fingerprints.json"
#: Deterministic completion marker. Written last.
POST_A3_COMPLETE_MARKER = "post_a3_index_complete.json"
#: Bumped when the incremental build's own tables change shape or meaning.
POST_A3_SCHEMA_VERSION = "B2.16/1"
#: The control namespace for the small metadata this build mirrors.
POST_A3_MIRROR_PREFIX = "post-a3-derived"
#: Artifacts per SQLite transaction.
DEFAULT_BATCH_SIZE = 250

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS post_a3_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS post_a3_delta (
    path TEXT PRIMARY KEY, url TEXT NOT NULL, cik INTEGER NOT NULL, accession TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL, expected_bytes INTEGER NOT NULL
);
"""

#: The B2.12 fingerprint projections over the baseline's rows only. `filings`,
#: `securities` and `cover` exclude the delta by URL; the catalogue tables are
#: whole, because the delta adds no catalogue.
_BASELINE_SUBSET: dict[str, str] = {
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
        " FROM filings WHERE url NOT IN (SELECT url FROM post_a3_delta) ORDER BY url"
    ),
    "securities": (
        "SELECT url, cik, accession, evidence_date, raw_title, normalized_title, raw_symbol,"
        " normalized_symbol, raw_exchange, normalized_exchange, member, no_trading_symbol,"
        " security_class, classification_reason, source_path, source_sha256, parser_version"
        " FROM securities WHERE url NOT IN (SELECT url FROM post_a3_delta)"
        " ORDER BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"
    ),
    "cover": (
        "SELECT url, cik, accession, evidence_date, raw_title, raw_symbol, raw_exchange, member,"
        " no_trading_symbol FROM securities WHERE url NOT IN (SELECT url FROM post_a3_delta)"
        " ORDER BY 1,2,3,4,5,6,7,8,9"
    ),
}

_TABLES = ("filings", "securities", "catalogue", "catalogue_stats", "meta", "build_ledger")


class PostA3Error(RuntimeError):
    """A binding does not hold; nothing is built or resumed on it."""


# ----------------------------------------------------------------------------
# The baseline index
# ----------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise PostA3Error(f"{path} does not exist")
    payload: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return payload


@dataclass(frozen=True)
class BaselineIndexFacts:
    """The B2.12 index, re-proven: file, metadata, marker and semantics agree.

    Attributes:
        path: The index file.
        sha256: Its digest, recomputed.
        manifest_sha256: The baseline manifest it was built from.
        trusted: Artifacts that manifest vouched for.
        submissions: Trusted catalogues.
        filings: Trusted filing documents.
        fingerprints: The semantic fingerprints, recomputed and equal to the recorded ones.
        table_counts: Rows per table.
        integrity_check: SQLite's verdict.
    """

    path: Path
    sha256: str
    manifest_sha256: str
    trusted: int
    submissions: int
    filings: int
    fingerprints: dict[str, TableFingerprint]
    table_counts: dict[str, int]
    integrity_check: str


def verify_baseline_index(
    derived: Path,
    *,
    expect_sha256: str,
    expect_manifest_sha256: str,
    expect_trusted: int,
    expect_filings: int,
    expect_submissions: int,
) -> BaselineIndexFacts:
    """Re-prove the B2.12 index before it is copied: digest, metadata, marker, fingerprints.

    Raises:
        PostA3Error: If any recorded or demanded value disagrees with the
            file, or SQLite's integrity check fails. The baseline is then a
            conflict to be looked at, never a starting point.
    """
    path = derived / INDEX_FILE
    if not path.is_file():
        raise PostA3Error(f"no baseline index at {path}")
    sha = _sha256_of(path)
    if sha != expect_sha256.lower():
        raise PostA3Error(f"the baseline index hashes to {sha[:16]}…, not {expect_sha256[:16]}…")
    meta = _read_json(derived / INDEX_META_FILE)
    marker = _read_json(derived / INDEX_COMPLETE_MARKER)
    problems: list[str] = []
    for name, facts in (("meta", meta), ("marker", marker)):
        if facts.get("index_sha256") != sha:
            problems.append(f"{name}.index_sha256")
        if facts.get("manifest_sha256") != expect_manifest_sha256.lower():
            problems.append(f"{name}.manifest_sha256")
        if int(str(facts.get("trusted", 0))) != expect_trusted:
            problems.append(f"{name}.trusted")
        if facts.get("parser_version") != PARSER_VERSION:
            problems.append(f"{name}.parser_version")
    if marker.get("stage") != "DERIVED_INDEX_COMPLETE":
        problems.append("marker.stage")
    if int(str(meta.get("filings", 0))) != expect_filings:
        problems.append("meta.filings")
    if int(str(meta.get("submissions", 0))) != expect_submissions:
        problems.append("meta.submissions")
    if int(str(meta.get("parse_failures", 1))) != 0:
        problems.append("meta.parse_failures")
    if problems:
        raise PostA3Error(f"the baseline index metadata does not bind: {problems}")
    recorded = meta.get("fingerprints")
    if not isinstance(recorded, dict):
        raise PostA3Error("the baseline metadata carries no fingerprints")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        (verdict,) = db.execute("PRAGMA integrity_check").fetchone()
        prints = semantic_fingerprints(db)
        counts = {
            name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608
            for name in _TABLES
        }
    if verdict != "ok":
        raise PostA3Error(f"the baseline index fails integrity_check: {verdict}")
    drift = [
        name
        for name, print_ in prints.items()
        if dict(recorded.get(name, {})) != {"rows": print_.rows, "sha256": print_.sha256}
    ]
    if drift or set(recorded) != set(prints):
        raise PostA3Error(f"the baseline index's semantic fingerprints moved: {drift or 'tables'}")
    log.info("baseline index verified", sha256=sha[:16], tables=counts)
    return BaselineIndexFacts(
        path=path,
        sha256=sha,
        manifest_sha256=expect_manifest_sha256.lower(),
        trusted=expect_trusted,
        submissions=expect_submissions,
        filings=expect_filings,
        fingerprints=prints,
        table_counts=counts,
        integrity_check=str(verdict),
    )


def baseline_paths(index_path: Path) -> set[str]:
    """Every artifact key the baseline build ledgered — the partition the delta is not."""
    with closing(sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)) as db:
        return {str(path) for (path,) in db.execute("SELECT path FROM build_ledger")}


# ----------------------------------------------------------------------------
# The delta
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DeltaSet:
    """The trusted filings the final manifest holds beyond the baseline, checked against the plan.

    Attributes:
        entries: The delta's manifest entries, in storage-key order — the frozen input order.
        baseline_filings: Trusted filings the baseline ledger holds.
        final_filings: Trusted filings the final manifest holds.
        missing: Plan identities with no trusted delta entry.
        extras: Delta identities the plan does not name.
        duplicates: Delta identities that appear more than once.
        co_registrant_rows: Delta entries sharing an accession with another CIK.
    """

    entries: tuple[Entry, ...]
    baseline_filings: int
    final_filings: int
    missing: tuple[tuple[int, str], ...]
    extras: tuple[tuple[int, str], ...]
    duplicates: int
    co_registrant_rows: int

    @property
    def exact(self) -> bool:
        """Whether the delta is the authorized plan, no more and no less."""
        return not self.missing and not self.extras and not self.duplicates


def delta_entries(
    manifest: Manifest, baseline: set[str], plan_keys: Sequence[tuple[int, str]]
) -> DeltaSet:
    """Partition the final manifest: what the baseline ledgered, and what came after it.

    Args:
        manifest: The final V2 manifest.
        baseline: Every key the baseline index ledgered.
        plan_keys: The authorized plan's `(cik, accession)` identities.

    Returns:
        The delta, with what did not reconcile — never raising, so the caller
        reports the numbers and decides.
    """
    trusted = [e for e in manifest.latest_by_url().values() if e.is_stored]
    filings = [e for e in trusted if e.request_kind == "FILING"]
    delta = sorted((e for e in filings if e.path not in baseline), key=lambda e: e.path or "")
    keys = [(int(e.cik or 0), str(e.accession)) for e in delta]
    wanted = set(plan_keys)
    seen = set(keys)
    by_accession: dict[str, set[int]] = {}
    for cik, accession in keys:
        by_accession.setdefault(accession, set()).add(cik)
    return DeltaSet(
        entries=tuple(delta),
        baseline_filings=sum(1 for e in filings if e.path in baseline),
        final_filings=len(filings),
        missing=tuple(sorted(wanted - seen)),
        extras=tuple(sorted(seen - wanted)),
        duplicates=len(keys) - len(seen),
        co_registrant_rows=sum(len(ciks) - 1 for ciks in by_accession.values() if len(ciks) > 1),
    )


# ----------------------------------------------------------------------------
# Bindings and the build
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PostA3Bindings:
    """What the incremental build is bound to; checked whole on resume."""

    generation_id: str
    final_manifest_sha256: str
    final_trusted: int
    final_filings: int
    final_submissions: int
    baseline_index_sha256: str
    baseline_manifest_sha256: str
    plan_sha256: str
    a3_marker_sha256: str
    delta_count: int
    parser_version: str = PARSER_VERSION
    schema_version: str = POST_A3_SCHEMA_VERSION

    def as_state(self) -> dict[str, str]:
        """The bindings as the string pairs the state table holds."""
        return {key: str(value) for key, value in asdict(self).items()}


@dataclass
class PostA3Report:
    """What one invocation did, and — when complete — what the index is."""

    build: IndexBuildReport
    baseline_sha256: str
    baseline_copied: bool = False
    copy_verified: bool = False
    delta_planned: int = 0
    invariance: dict[str, dict[str, object]] = field(default_factory=dict)
    baseline_fingerprints: dict[str, dict[str, object]] = field(default_factory=dict)
    added: dict[str, int] = field(default_factory=dict)
    marker_sha256: str | None = None

    @property
    def complete(self) -> bool:
        """Whether the index was promoted."""
        return self.build.complete

    @property
    def failure(self) -> str | None:
        """Why the build stopped, when it did."""
        return self.build.failure


def post_a3_complete_facts(derived: Path) -> dict[str, object] | None:
    """The post-A3 completion marker, when the index is complete."""
    marker = derived / POST_A3_COMPLETE_MARKER
    if not marker.is_file():
        return None
    return _read_json(marker)


def _state(db: sqlite3.Connection) -> dict[str, str]:
    return {str(k): str(v) for k, v in db.execute("SELECT key, value FROM post_a3_state")}


def _prints_as_dict(prints: Mapping[str, TableFingerprint]) -> dict[str, dict[str, object]]:
    return {name: {"rows": p.rows, "sha256": p.sha256} for name, p in prints.items()}


def _start_from_baseline(
    building: Path,
    baseline: BaselineIndexFacts,
    bindings: PostA3Bindings,
    delta: DeltaSet,
    report: PostA3Report,
) -> None:
    """Copy the verified baseline, prove the copy is it, and bind the build."""
    shutil.copyfile(baseline.path, building)
    report.baseline_copied = True
    with closing(_open_building(building)) as db:
        (verdict,) = db.execute("PRAGMA integrity_check").fetchone()
        if verdict != "ok":
            raise PostA3Error(f"the copied baseline fails integrity_check: {verdict}")
        prints = semantic_fingerprints(db)
        if prints != baseline.fingerprints:
            raise PostA3Error("the copied baseline does not reproduce the baseline fingerprints")
        counts = {
            name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608
            for name in _TABLES
        }
        if counts != baseline.table_counts:
            raise PostA3Error(f"the copied baseline's row counts differ: {counts}")
        report.copy_verified = True
        db.executescript(_STATE_SCHEMA)
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO post_a3_state VALUES (?, ?)",
            [
                *bindings.as_state().items(),
                ("baseline_fingerprints", json.dumps(_prints_as_dict(prints), sort_keys=True)),
                ("started_at", datetime.now(UTC).isoformat()),
            ],
        )
        db.executemany(
            "INSERT INTO post_a3_delta VALUES (?,?,?,?,?,?)",
            [(e.path, e.url, e.cik, e.accession, e.sha256, e.bytes) for e in delta.entries],
        )
        db.commit()


def _resume_check(db: sqlite3.Connection, bindings: PostA3Bindings, delta: DeltaSet) -> None:
    state = _state(db)
    expected = bindings.as_state()
    drift = [k for k, v in expected.items() if state.get(k) != v]
    if drift:
        raise PostA3Error(f"the in-progress build is bound to other inputs: {drift}; refused")
    held = {
        str(path): (str(sha), int(size))
        for path, sha, size in db.execute(
            "SELECT path, expected_sha256, expected_bytes FROM post_a3_delta"
        )
    }
    wanted = {str(e.path): (str(e.sha256), int(e.bytes or 0)) for e in delta.entries}
    if held != wanted:
        raise PostA3Error("the in-progress build's delta is not this delta; refused")


def build_post_a3_index(
    storage: EvidenceStorage,
    delta: DeltaSet,
    derived: Path,
    *,
    baseline: BaselineIndexFacts,
    bindings: PostA3Bindings,
    batch_size: int = DEFAULT_BATCH_SIZE,
    prefetch: int = 1,
    stop_after: int | None = None,
    fresh: bool = False,
    progress: Callable[[str], None] | None = None,
    facts: Mapping[str, object] | None = None,
) -> PostA3Report:
    """Build, or resume building, the post-A3 index from the baseline copy and the delta.

    Args:
        storage: The bucket. Read only, and only the delta's keys.
        delta: The A3 filings, already reconciled to the plan by the caller.
        derived: The control root's `derived/` directory.
        baseline: The verified B2.12 index.
        bindings: What the build is bound to.
        batch_size: Artifacts per transaction.
        prefetch: Downloads in flight at once; the order of verification,
            parsing and commit is unchanged by it.
        stop_after: Stop cleanly after this many artifacts this invocation.
        fresh: Discard an in-progress build and any completion marker.
        progress: Receives one line per committed batch.
        facts: Extra provenance for the metadata file.

    Returns:
        The report. `complete` only when every delta artifact is ledgered
        `COMMITTED`, validation passed, the baseline invariance held and the
        index was promoted.

    Raises:
        PostA3Error: If the delta is not the plan, the baseline copy does
            not reproduce the baseline, or an in-progress build is bound to
            other inputs.
    """
    if not delta.exact:
        raise PostA3Error(
            f"the delta is not the authorized plan: missing {len(delta.missing)}, "
            f"extras {len(delta.extras)}, duplicates {delta.duplicates}"
        )
    started = time.perf_counter()
    say = progress or (lambda line: None)
    building = derived / POST_A3_BUILDING_FILE
    final = derived / POST_A3_INDEX_FILE
    build = IndexBuildReport(
        generation_id=bindings.generation_id,
        manifest_sha256=bindings.final_manifest_sha256,
        building_path=str(building),
        index_path=str(final),
        planned=len(delta.entries),
    )
    report = PostA3Report(
        build=build, baseline_sha256=baseline.sha256, delta_planned=len(delta.entries)
    )
    if fresh:
        for stale in (building, building.with_name(building.name + "-journal")):
            stale.unlink(missing_ok=True)
    else:
        done = post_a3_complete_facts(derived)
        if done is not None and all(
            done.get(key) == value
            for key, value in (
                ("final_manifest_sha256", bindings.final_manifest_sha256),
                ("parser_version", bindings.parser_version),
                ("schema_version", bindings.schema_version),
            )
        ):
            raise PostA3Error(
                f"{final.name} is already complete for manifest "
                f"{bindings.final_manifest_sha256[:16]}…; pass fresh=True to rebuild it"
            )
    derived.mkdir(parents=True, exist_ok=True)
    if not building.exists():
        _start_from_baseline(building, baseline, bindings, delta, report)
        say(
            f"baseline {baseline.sha256[:16]}… copied and re-proven; "
            f"{len(delta.entries)} delta inputs bound"
        )
    with closing(_open_building(building)) as db:
        _resume_check(db, bindings, delta)
        ledgered = _ledgered(db)
        remaining = [e for e in delta.entries if e.path not in ledgered]
        build.skipped_resume = len(delta.entries) - len(remaining)
        say(
            f"{'resuming' if build.skipped_resume else 'starting'} {building.name}: "
            f"{len(delta.entries)} delta inputs, {build.skipped_resume} already ledgered"
        )
        if stop_after is not None and len(remaining) > stop_after:
            build.stopped_early = True
            remaining = remaining[:stop_after]
        accepted = _accepted_map(db, build)
        in_batch = 0
        db.execute("BEGIN")
        with _Prefetcher(storage, remaining, prefetch) as bodies:
            for entry, fetched in bodies:
                try:
                    data = _verify(entry, fetched, build)
                except (IndexIntegrityError, StorageUnavailableError) as exc:
                    build.failure = f"{type(exc).__name__}: {exc}"
                    log.error("post-A3 build stopped", key=entry.path, error=type(exc).__name__)
                    break
                _index_one(db, entry, data, accepted, build)
                in_batch += 1
                if in_batch >= batch_size:
                    commit_started = time.perf_counter()
                    db.commit()
                    build.sqlite_seconds += time.perf_counter() - commit_started
                    in_batch = 0
                    forms = dict(sorted(build.forms.items()))
                    say(
                        f"delta: {build.processed} processed this run, "
                        f"{build.skipped_resume + build.processed}/{len(delta.entries)} "
                        f"ledgered, {build.body_bytes:,} bytes read, forms {forms}"
                    )
                    db.execute("BEGIN")
        build.reads_discarded = bodies.discarded
        commit_started = time.perf_counter()
        db.commit()
        build.sqlite_seconds += time.perf_counter() - commit_started
        build.ledger = _ledger_counts(db)
        delta_ledgered = int(
            db.execute(
                "SELECT COUNT(*) FROM build_ledger WHERE path IN (SELECT path FROM post_a3_delta)"
            ).fetchone()[0]
        )
        build.unprocessed = len(delta.entries) - delta_ledgered
    if build.failure is None and not build.stopped_early:
        try:
            _finalize(building, final, derived, baseline, bindings, report, facts or {})
        except (IndexValidationError, PostA3Error, sqlite3.Error) as exc:
            build.failure = f"{type(exc).__name__}: {exc}"
            log.error("post-A3 index validation failed", error=type(exc).__name__)
    build.elapsed_seconds = time.perf_counter() - started
    return report


def _delta_failures(db: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {"path": p, "url": u, "cik": c, "accession": a, "sha256": s, "detail": d}
        for p, u, c, a, s, d in db.execute(
            "SELECT l.path, l.url, d.cik, d.accession, l.expected_sha256, l.detail"
            " FROM build_ledger l JOIN post_a3_delta d ON d.path = l.path"
            " WHERE l.status = ? ORDER BY l.path",
            (str(ArtifactOutcome.FAILED),),
        )
    ]


def _finalize(
    building: Path,
    final: Path,
    derived: Path,
    baseline: BaselineIndexFacts,
    bindings: PostA3Bindings,
    report: PostA3Report,
    facts: Mapping[str, object],
) -> None:
    """Refuse unless everything is committed, sound and baseline-invariant; then promote."""
    build = report.build
    with closing(_open_building(building)) as db:
        failures = _delta_failures(db)
        if failures:
            build.failure = (
                f"{len(failures)} delta artifact(s) ledgered FAILED; the index is not promoted: "
                f"{[f['path'] for f in failures[:3]]}"
            )
            return
        if build.unprocessed:
            build.failure = f"{build.unprocessed} delta input(s) are not ledgered"
            return
        _validate(db, build)
        build.fingerprints = semantic_fingerprints(db)
        subset = {}
        for name, query in _BASELINE_SUBSET.items():
            digest = hashlib.sha256()
            rows = 0
            for row in db.execute(query):
                digest.update(
                    json.dumps(list(row), separators=(",", ":"), ensure_ascii=False).encode()
                )
                digest.update(b"\n")
                rows += 1
            subset[name] = TableFingerprint(rows=rows, sha256=digest.hexdigest())
        report.baseline_fingerprints = _prints_as_dict(baseline.fingerprints)
        report.invariance = {
            name: {
                "rows": subset[name].rows,
                "before": baseline.fingerprints[name].sha256,
                "after": subset[name].sha256,
                "changed": subset[name] != baseline.fingerprints[name],
            }
            for name in _BASELINE_SUBSET
        }
        changed = [name for name, facts_ in report.invariance.items() if facts_["changed"]]
        if changed:
            raise PostA3Error(f"the delta changed baseline-derived rows: {changed}")
        build.table_counts = {
            name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608
            for name in _TABLES
        }
        report.added = {
            name: build.table_counts[name] - baseline.table_counts[name] for name in _TABLES
        }
        completed_at = datetime.now(UTC).isoformat()
        db.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            [
                ("post_a3_manifest_sha256", bindings.final_manifest_sha256),
                ("post_a3_schema_version", bindings.schema_version),
                ("post_a3_parent_index_sha256", bindings.baseline_index_sha256),
                ("post_a3_plan_sha256", bindings.plan_sha256),
                ("post_a3_built_at", completed_at),
            ],
        )
        db.commit()
        started_at = _state(db).get("started_at")
        build.table_counts["meta"] = int(db.execute("SELECT COUNT(*) FROM meta").fetchone()[0])

    building.replace(final)
    build.index_bytes = final.stat().st_size
    build.index_sha256 = _sha256_of(final)
    prints = _prints_as_dict(build.fingerprints)
    meta: dict[str, object] = {
        **bindings.as_state(),
        **dict(facts),
        "index_file": final.name,
        "index_sha256": build.index_sha256,
        "index_bytes": build.index_bytes,
        "build_started_at": started_at,
        "build_completed_at": completed_at,
        "baseline_index_file": baseline.path.name,
        "baseline_artifacts": baseline.trusted,
        "delta_artifacts": bindings.delta_count,
        "final_artifacts": bindings.final_trusted,
        "ledger": build.ledger,
        "parse_failures": build.parse_failures,
        "table_counts": build.table_counts,
        "baseline_table_counts": baseline.table_counts,
        "added_rows": report.added,
        "fingerprints": prints,
        "baseline_fingerprints": report.baseline_fingerprints,
        "baseline_invariance": report.invariance,
        "integrity_check": build.integrity_check,
        "diagnostics": build.diagnostics,
        "forms_this_invocation": dict(sorted(build.forms.items())),
        "this_invocation": {
            "processed": build.processed,
            "skipped_resume": build.skipped_resume,
            "body_reads": build.body_reads,
            "body_bytes": build.body_bytes,
            "read_seconds": round(build.read_seconds, 3),
            "parse_seconds": round(build.parse_seconds, 3),
            "sqlite_seconds": round(build.sqlite_seconds, 3),
        },
    }
    _write_json(derived / POST_A3_META_FILE, meta)
    _write_json(
        derived / POST_A3_FINGERPRINTS_FILE,
        {
            "generation_id": bindings.generation_id,
            "final_manifest_sha256": bindings.final_manifest_sha256,
            "baseline_index_sha256": bindings.baseline_index_sha256,
            "parser_version": bindings.parser_version,
            "schema_version": bindings.schema_version,
            "fingerprints": prints,
            "baseline_fingerprints": report.baseline_fingerprints,
        },
    )
    marker = {
        "stage": "POST_A3_INDEX_COMPLETE",
        **bindings.as_state(),
        "index_file": final.name,
        "index_sha256": build.index_sha256,
        "index_bytes": build.index_bytes,
        "delta_processed": bindings.delta_count,
        "parse_failures": build.parse_failures,
        "table_counts": build.table_counts,
        "fingerprints": prints,
        "baseline_invariance": {k: v["changed"] for k, v in report.invariance.items()},
    }
    _write_json(derived / POST_A3_COMPLETE_MARKER, marker)
    report.marker_sha256 = _sha256_of(derived / POST_A3_COMPLETE_MARKER)
    build.complete = True
    log.info(
        "post-A3 index complete",
        index=str(final),
        sha256=build.index_sha256[:16],
        tables=build.table_counts,
    )


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


_MIRRORED = (
    (POST_A3_META_FILE, "index-meta"),
    (POST_A3_FINGERPRINTS_FILE, "fingerprints"),
    (POST_A3_COMPLETE_MARKER, "complete"),
)


def mirror_post_a3_metadata(
    mirror: ControlMirror, derived: Path, *, generation_id: str = GENERATION_ID
) -> list[MirrorRecord]:
    """Mirror the small metadata as snapshots under `post-a3-derived/`, and read each back."""
    records = []
    for name, kind in _MIRRORED:
        local = derived / name
        if not local.is_file():
            raise PostA3Error(f"{local} does not exist; nothing to mirror")
        record, _ = mirror.mirror_snapshot(
            local, f"{POST_A3_MIRROR_PREFIX}/{kind}", generation_id=generation_id
        )
        newest = mirror.newest_snapshot(f"{POST_A3_MIRROR_PREFIX}/{kind}")
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise PostA3Error(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records
