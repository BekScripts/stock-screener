"""Rebuilding the corpus from the SEC into the bucket, one frozen chunk at a time.

The second generation is built the way the first should have been: every
byte fetched from the SEC goes straight into the bucket, is read back and
hashed before the manifest learns of it, and every file needed to recover
the run is mirrored to the bucket's control namespace before the run is
allowed to depend on it. This module is the sequence, and it stops at the
first step that does not hold.

## The chunk

A chunk is 250 consecutive identities of the production universe, in the
universe's own order — the same arithmetic the first generation's sweep
used, so chunk 000 is the same 250 securities it was then. Nothing is
hand-picked; an unresolved identity stays in the chunk and contributes no
request, exactly as before.

## The sequence, and what gates each step

```text
preflight   offline plans verified against their digests; production prefixes
            empty (or, on resume, fully claimed); V2 manifest at zero (or resumed)
mirror      generation metadata and offline plans mirrored and read back
resolve     one SEC request for the ticker map, preserved raw; all 8,919
            identities resolved STORED → SEC_TICKER_MAP → UNRESOLVED; frozen
freeze      chunk identities written and digested; checkpoint PENDING; mirrored
pass 1      submissions catalogues → bucket → read-back digest → manifest
snapshot    manifest snapshot, checkpoint PASS1_DONE, mirrored
derive      filing targets from the trusted catalogues, twice, byte-identical
pass 2      filing documents, the same chain; snapshot; PASS2_DONE; mirrored
converge    the frozen targets replayed: no fetch, no write, every CACHE_HIT
verify      full reconciliation with every artifact downloaded and hashed
complete    checkpoint COMPLETE; final snapshot; mirrored; recovery proven
```

## Trust ordering, made stricter than the backend's

`harvest_artifacts(verify_written=True)` reads each newly stored artifact
back from the bucket and hashes it *before* appending the manifest entry.
The backend's own PUT-then-HEAD verification already runs inside
`write_bytes`; this is the independent check on top of it, so that an
object the bucket accepted and then holds differently can never become a
trusted row. Backend-neutral: the same flag re-reads a file on a local
directory.

## Resolution is not guessing

A stored CIK wins. A live company's ticker is looked up in the SEC's map,
which describes who holds the symbol now — correct for a company that is
listed now. A delisted security without a stored CIK is left `UNRESOLVED`:
the map cannot say who held its symbol on the day it mattered, and a
current occupant is not evidence about a historical security. This is
stricter than the first generation, which asked the map for every symbol;
the difference is reported, never hidden, and never "corrected" back.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from api_clients.edgar import DEFAULT_WWW_URL, load_cik_map_from_file
from api_clients.errors import ProviderAuthError, ProviderRateLimitError
from domain import normalise_ticker
from stock_screener.sec_evidence.audit import reconcile
from stock_screener.sec_evidence.expand import Resolution, filing_targets, submissions_targets
from stock_screener.sec_evidence.generation import (
    GENERATION_ID,
    PRODUCTION_CHUNK,
    IdentityRow,
    v2_progress,
)
from stock_screener.sec_evidence.harvest import Budget, HarvestReport, harvest_artifacts
from stock_screener.sec_evidence.manifest import Manifest, Outcome
from stock_screener.sec_evidence.mirror import ControlMirror, MirrorConflictError
from stock_screener.sec_evidence.sample import DELISTED, LIVE, Selection
from stock_screener.sec_evidence.storage import ARTIFACT_PREFIXES, StorageUnavailableError
from stock_screener.sec_evidence.sweep import (
    Checkpoint,
    ChunkStage,
    load_checkpoint,
    save_checkpoint,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot
    from stock_screener.sec_evidence.harvest import DocumentSource, Target

log = structlog.get_logger(__name__)

__all__ = [
    "TICKER_MAP_URL",
    "CanaryStopError",
    "RebuildReport",
    "RebuildStep",
    "ResolutionRow",
    "ResolutionSource",
    "chunk_rows",
    "fetch_ticker_map",
    "resolve_identities",
    "run_chunk",
    "write_resolution",
]

#: The SEC's ticker-to-CIK map. One request serves the whole universe.
TICKER_MAP_URL = f"{DEFAULT_WWW_URL}/files/company_tickers.json"

#: Files beneath `plans/generation_v2/` that must be mirrored before any
#: evidence download, in mirror order.
_OFFLINE_PLAN_FILES = (
    "generation.json",
    "base_identities.jsonl",
    "base_identities.sha256",
    "base_targets_offline.jsonl",
    "base_targets_offline.sha256",
    "prerequisites.json",
)


class ResolutionSource(StrEnum):
    """Where an identity's filer number came from. Nothing else is allowed."""

    STORED = "STORED"
    SEC_TICKER_MAP = "SEC_TICKER_MAP"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class ResolutionRow:
    """One identity's frozen resolution."""

    position: int
    chunk: int
    bucket: str
    symbol: str
    company_id: int | None
    historical_security_id: int | None
    cik: int | None
    source: str
    reason: str

    def line(self) -> str:
        """The row as one JSONL line, keys sorted."""
        return json.dumps(asdict(self), sort_keys=True)

    def selection(self) -> Selection:
        """The selection this row describes, with its resolved CIK."""
        return Selection(
            bucket=self.bucket,
            symbol=self.symbol,
            name="",
            cik=self.cik,
            company_id=self.company_id,
            historical_security_id=self.historical_security_id,
        )


class CanaryStopError(RuntimeError):
    """A stop condition. The message says which; the checkpoint says where."""


@dataclass
class RebuildStep:
    """One step of the run: name, verdict, latency, and what it saw."""

    name: str
    ok: bool
    elapsed_ms: float
    detail: str = ""


@dataclass
class RebuildReport:
    """Everything one chunk run observed. No secret can be in any field."""

    generation_id: str
    chunk: int
    steps: list[RebuildStep] = field(default_factory=list)
    sec_requests: int = 0
    sec_bytes: int = 0
    b2_requests: dict[str, int] = field(default_factory=dict)
    mirror: dict[str, int] = field(default_factory=dict)
    resolution: dict[str, int] = field(default_factory=dict)
    submissions: dict[str, int] = field(default_factory=dict)
    filings: dict[str, int] = field(default_factory=dict)
    manifest: dict[str, int | str] = field(default_factory=dict)
    reconciliation: dict[str, int] = field(default_factory=dict)
    recovery_sufficient: bool = False
    stage: str = ChunkStage.PENDING
    failure: str | None = None

    @property
    def passed(self) -> bool:
        """Whether every step held and the chunk is complete."""
        return self.failure is None and self.stage == ChunkStage.COMPLETE

    def summary(self) -> list[str]:
        """Lines for a terminal."""
        lines = [f"{self.generation_id} chunk {self.chunk:03d} — stage {self.stage}"]
        for step in self.steps:
            mark = "ok  " if step.ok else "FAIL"
            lines.append(f"  {mark} {step.name:<32} {step.elapsed_ms:9.0f} ms  {step.detail}")
        lines.append(f"  SEC requests {self.sec_requests}, bytes {self.sec_bytes}")
        lines.append(f"  B2 {dict(sorted(self.b2_requests.items()))}")
        lines.append(f"  mirror {self.mirror}")
        lines.append(f"  resolution {self.resolution}")
        lines.append(f"  submissions {self.submissions}; filings {self.filings}")
        lines.append(f"  manifest {self.manifest}")
        lines.append(f"  reconciliation {self.reconciliation}")
        lines.append(f"  recovery from mirror sufficient: {self.recovery_sufficient}")
        if self.failure:
            lines.append(f"  STOPPED: {self.failure}")
        return lines


# ----------------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------------


def fetch_ticker_map(documents: DocumentSource, directory: Path) -> tuple[Path, dict[str, object]]:
    """Fetch the SEC ticker map once and preserve it raw, or reuse the preserved copy.

    The bytes are stored exactly as received under `prerequisites/`, with a
    sidecar recording the URL, status, size, digest and time, and a `.sha256`
    companion. A preserved copy is reused on a rerun so that resolution is a
    function of one snapshot, not of whatever the SEC serves today.

    Returns:
        The path of the raw snapshot, and its sidecar facts.
    """
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / "company_tickers.json"
    meta = directory / "company_tickers.meta.json"
    if raw.is_file() and meta.is_file():
        facts: dict[str, object] = json.loads(meta.read_text(encoding="utf-8"))
        if hashlib.sha256(raw.read_bytes()).hexdigest() == facts.get("sha256"):
            log.info("ticker map snapshot reused", path=str(raw))
            return raw, facts
        raise CanaryStopError(f"the preserved ticker map at {raw} does not hash to its sidecar")
    fetched = documents.fetch(TICKER_MAP_URL)
    if fetched.status != 200 or not fetched.body:
        raise CanaryStopError(
            f"ticker map fetch returned HTTP {fetched.status} with {len(fetched.body)} bytes"
        )
    raw.write_bytes(fetched.body)
    facts = {
        "url": fetched.url,
        "status": fetched.status,
        "bytes": len(fetched.body),
        "sha256": fetched.sha256,
        "retrieved_at": fetched.retrieved_at.isoformat(),
        "content_type": fetched.content_type,
    }
    meta.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (directory / "company_tickers.sha256").write_text(
        f"{fetched.sha256}  company_tickers.json\n", encoding="utf-8"
    )
    log.info("ticker map preserved", bytes=len(fetched.body), sha=fetched.sha256[:16])
    return raw, facts


def resolve_identities(
    identities: Sequence[IdentityRow], ticker_map: dict[str, int]
) -> list[ResolutionRow]:
    """Resolve every identity from the stored CIKs and one ticker-map snapshot.

    Deterministic in its inputs: the same identity plan and the same
    snapshot produce the same rows in universe order.
    """
    rows: list[ResolutionRow] = []
    for row in identities:
        if row.cik is not None:
            source, cik, reason = (
                ResolutionSource.STORED,
                row.cik,
                "stored on the security's record",
            )
        elif row.bucket == LIVE and normalise_ticker(row.symbol) in ticker_map:
            source, cik, reason = (
                ResolutionSource.SEC_TICKER_MAP,
                ticker_map[normalise_ticker(row.symbol)],
                "live company; symbol in the SEC ticker map",
            )
        elif row.bucket == DELISTED:
            in_map = normalise_ticker(row.symbol) in ticker_map
            source, cik = ResolutionSource.UNRESOLVED, None
            reason = (
                "historical security; the current ticker map names today's holder of the "
                "symbol, not this one"
                if in_map
                else "historical security; no stored CIK and symbol absent from the ticker map"
            )
        else:
            source, cik, reason = (
                ResolutionSource.UNRESOLVED,
                None,
                "live company; symbol absent from the SEC ticker map",
            )
        rows.append(
            ResolutionRow(
                position=row.position,
                chunk=row.chunk,
                bucket=row.bucket,
                symbol=row.symbol,
                company_id=row.company_id,
                historical_security_id=row.historical_security_id,
                cik=cik,
                source=source.value,
                reason=reason,
            )
        )
    return rows


def _digest_lines(lines: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode() + b"\n")
    return digest.hexdigest()


def _write_lines(path: Path, lines: Sequence[str]) -> str:
    """Write JSONL lines and a `.sha256` companion; return the digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    sha = _digest_lines(lines)
    path.with_suffix(".sha256").write_text(f"{sha}  {path.name}\n", encoding="utf-8")
    return sha


def write_resolution(rows: Sequence[ResolutionRow], directory: Path) -> tuple[Path, str]:
    """Write `filer_resolution_v1.jsonl`, its digest and a summary."""
    path = directory / "filer_resolution_v1.jsonl"
    sha = _write_lines(path, [row.line() for row in rows])
    counts = {source.value: 0 for source in ResolutionSource}
    declined = 0
    for row in rows:
        counts[row.source] += 1
        if row.source == ResolutionSource.UNRESOLVED and "today's holder" in row.reason:
            declined += 1
    summary = {
        "rows": len(rows),
        "sha256": sha,
        "by_source": counts,
        "delisted_symbols_in_map_declined": declined,
        "policy": "STORED > SEC_TICKER_MAP (live only) > UNRESOLVED; never guessed",
    }
    (directory / "filer_resolution_v1.summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path, sha


def chunk_rows(rows: Sequence[ResolutionRow], chunk: int) -> list[ResolutionRow]:
    """The rows of one production chunk, in universe order."""
    return [row for row in rows if row.chunk == chunk]


def _chunk_targets(rows: Sequence[ResolutionRow]) -> tuple[list[Resolution], list[Target]]:
    """Submissions targets for a chunk: one per resolved CIK, first occurrence wins.

    Two identities can share a filer — a company with two listed share
    classes, or a live company and the delisted security it once was — and
    the filer's catalogue is one artifact. The resolutions returned are the
    unique ones, so filing derivation sees each filer once as well.
    """
    seen: set[int] = set()
    unique: list[Resolution] = []
    for row in rows:
        if row.cik is None or row.cik in seen:
            continue
        seen.add(row.cik)
        unique.append(Resolution(row.selection(), row.cik, row.source))
    return unique, submissions_targets(unique)


def _dedupe_filings(targets: Sequence[Target]) -> list[Target]:
    """One target per `(CIK, accession)`, first occurrence wins, order kept."""
    seen: set[tuple[int | None, str | None]] = set()
    kept: list[Target] = []
    for target in targets:
        pair = (target.cik, target.accession)
        if pair in seen:
            continue
        seen.add(pair)
        kept.append(target)
    return kept


def _target_line(target: Target) -> str:
    return json.dumps(
        {
            "url": target.url,
            "kind": str(target.kind),
            "cik": target.cik,
            "accession": target.accession,
            "document": target.document,
            "form": target.form,
            "filing_date": target.filing_date,
        },
        sort_keys=True,
    )


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------


@dataclass
class _Context:
    storage: EvidenceStorage
    control: EvidenceControlRoot
    manifest: Manifest
    mirror: ControlMirror
    documents: DocumentSource
    budget: Budget
    generation_dir: Path
    chunk_dir: Path
    checkpoint_path: Path
    checkpoint: Checkpoint
    report: RebuildReport
    b2_counts: dict[str, int]


def run_chunk(
    storage: EvidenceStorage,
    control: EvidenceControlRoot,
    documents: DocumentSource,
    identities: Sequence[IdentityRow],
    *,
    chunk: int = 0,
    budget: Budget | None = None,
    filings_per_security: int = 6,
    generation_id: str = GENERATION_ID,
    observe: Callable[[Callable[[str, str], None]], bool] | None = None,
) -> RebuildReport:
    """Rebuild one chunk of the second generation, end to end, and report.

    Args:
        storage: The artifact backend — the bucket in production.
        control: The V2 control root. Its manifest is the V2 manifest.
        documents: The SEC client.
        identities: The identity plan (`identity_plan`), all of it.
        chunk: Which 250-identity chunk. Only 000 in the canary.
        budget: SEC request and byte limits for this run. When the SEC client
            is metered by the same object, every HTTP attempt is counted here.
            Defaults to 4,000 requests and 2 GiB.
        filings_per_security: The production cap.
        generation_id: Mirror namespace and record label.
        observe: Registers a request observer on the backend, when it has one.

    Returns:
        The report. `passed` is true only for `COMPLETE`.
    """
    b2_counts: dict[str, int] = {}
    if observe is not None:

        def count(kind: str, operation: str) -> None:
            name = f"{kind}:{operation}"
            b2_counts[name] = b2_counts.get(name, 0) + 1

        observe(count)

    generation_dir = control.plans / "generation_v2"
    chunk_dir = generation_dir / "chunks" / f"{chunk:03d}"
    checkpoint_path = (
        control.checkpoints / f"chunk-{chunk * PRODUCTION_CHUNK:06d}-{PRODUCTION_CHUNK:04d}.json"
    )
    existing = load_checkpoint(checkpoint_path)
    report = RebuildReport(generation_id=generation_id, chunk=chunk)
    if existing is not None:
        report.stage = existing.stage
    context = _Context(
        storage=storage,
        control=control,
        manifest=Manifest(control.manifest_path),
        mirror=ControlMirror(storage, generation_id),
        documents=documents,
        budget=budget or Budget(max_requests=4000, max_bytes=2 * 1024**3),
        generation_dir=generation_dir,
        chunk_dir=chunk_dir,
        checkpoint_path=checkpoint_path,
        checkpoint=existing
        or Checkpoint(
            offset=chunk * PRODUCTION_CHUNK, limit=PRODUCTION_CHUNK, selection_digest="", selected=0
        ),
        report=report,
        b2_counts=b2_counts,
    )
    try:
        _run(context, identities, filings_per_security)
    except (CanaryStopError, StorageUnavailableError, MirrorConflictError) as exc:
        report.failure = f"{type(exc).__name__}: {exc}"
        context.checkpoint.notes.append(f"stopped: {type(exc).__name__}: {exc}")
        save_checkpoint(checkpoint_path, context.checkpoint)
        log.error("rebuild canary stopped", chunk=chunk, error=type(exc).__name__)
    finally:
        report.stage = context.checkpoint.stage
        report.sec_requests = context.budget.requests_made
        report.sec_bytes = context.budget.bytes_written
        report.b2_requests = dict(b2_counts)
        report.mirror = {
            **context.mirror.report.counts(),
            "puts": context.mirror.report.puts,
            "reads": context.mirror.report.reads,
            "bytes_uploaded": context.mirror.report.bytes_uploaded,
        }
        context.mirror.write_ledger(generation_dir / "mirror_ledger.jsonl")
    return report


def _step(context: _Context, name: str, check: Callable[[], str]) -> None:
    started = time.perf_counter()
    detail = check()
    context.report.steps.append(
        RebuildStep(name, True, (time.perf_counter() - started) * 1000, detail)
    )


def _mirror_offline_plans(context: _Context) -> str:
    for name in _OFFLINE_PLAN_FILES:
        path = context.generation_dir / name
        if not path.is_file():
            raise CanaryStopError(
                f"offline plan {name} is missing; run `evidence generation-plan` first"
            )
        if name.endswith(".jsonl"):
            expected = (path.with_suffix(".sha256")).read_text().split()[0]
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise CanaryStopError(
                    f"{name} hashes to {actual[:16]}…, its companion says {expected[:16]}…"
                )
        context.mirror.mirror(path, name if name == "generation.json" else f"plans/{name}")
    return f"{len(_OFFLINE_PLAN_FILES)} files mirrored and read back"


def _preflight(context: _Context) -> str:
    trusted, completed, inconsistent = v2_progress(context.control)
    if inconsistent:
        raise CanaryStopError(
            f"checkpoints claim completion the manifest cannot support: {inconsistent}"
        )
    listed = {p: sum(1 for _ in context.storage.list_prefix(p)) for p in ARTIFACT_PREFIXES}
    fresh = context.checkpoint.selected == 0
    if fresh and (trusted or any(listed.values())):
        raise CanaryStopError(
            f"a fresh chunk run requires an empty generation: trusted={trusted}, bucket={listed}"
        )
    if not fresh:
        state = reconcile(context.storage, context.manifest, verify_digests=False)
        if not state.clean:
            raise CanaryStopError(f"resuming over an unclean corpus: {state.problems}")
    return f"trusted={trusted} completed={completed} bucket={listed} resume={not fresh}"


def _resolve(context: _Context, identities: Sequence[IdentityRow]) -> list[ResolutionRow]:
    raw, facts = fetch_ticker_map(context.documents, context.generation_dir / "prerequisites")
    ticker_map = load_cik_map_from_file(str(raw))
    rows = resolve_identities(identities, ticker_map)
    again = resolve_identities(identities, ticker_map)
    if [r.line() for r in rows] != [r.line() for r in again]:
        raise CanaryStopError("resolution is not deterministic from one snapshot")
    _, sha = write_resolution(rows, context.generation_dir)
    counts = {
        source.value: sum(1 for r in rows if r.source == source) for source in ResolutionSource
    }
    context.report.resolution = {
        **counts,
        "total": len(rows),
        "ticker_map_bytes": int(str(facts["bytes"])),
    }
    context.report.manifest["resolution_sha256"] = sha
    context.report.manifest["ticker_map_sha256"] = str(facts["sha256"])
    return rows


def _freeze_chunk(context: _Context, rows: Sequence[ResolutionRow]) -> list[ResolutionRow]:
    mine = chunk_rows(rows, context.report.chunk)
    if not mine:
        raise CanaryStopError(f"chunk {context.report.chunk} holds no identities")
    lines = [row.line() for row in mine]
    sha = _write_lines(context.chunk_dir / "identities.jsonl", lines)
    if _digest_lines(lines) != sha:
        raise CanaryStopError("chunk identity digest did not reproduce")
    checkpoint = context.checkpoint
    if checkpoint.selected == 0:
        checkpoint.selected = len(mine)
        checkpoint.selection_digest = sha
        checkpoint.notes.append(f"generation={context.report.generation_id}")
        checkpoint.notes.append(f"identity_plan_sha256={sha}")
        checkpoint.notes.append(f"resolution_sha256={context.report.manifest['resolution_sha256']}")
        checkpoint.records_before = sum(1 for _ in context.manifest.entries())
    elif checkpoint.selection_digest != sha:
        raise CanaryStopError(
            f"chunk identities changed under the checkpoint: {checkpoint.selection_digest[:16]}… "
            f"→ {sha[:16]}…"
        )
    save_checkpoint(context.checkpoint_path, checkpoint)
    _write_summary(context, mine)
    return mine


def _write_summary(context: _Context, rows: Sequence[ResolutionRow], **extra: object) -> Path:
    summary_path = context.chunk_dir / "summary.json"
    previous: dict[str, object] = (
        json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    )
    counts: dict[str, object] = (
        {"identities": len(rows), "resolved": sum(1 for r in rows if r.cik is not None)}
        if rows
        else {}
    )
    summary: dict[str, object] = {
        **previous,
        "generation_id": context.report.generation_id,
        "chunk": context.report.chunk,
        **counts,
        "identity_plan_sha256": context.checkpoint.selection_digest,
        "resolution_sha256": context.report.manifest.get("resolution_sha256"),
        "stage": context.checkpoint.stage,
        **extra,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary_path


def _snapshot_and_mirror(context: _Context, label: str) -> str:
    """Manifest snapshot, checkpoint and chunk summary, mirrored and read back."""
    manifest_path = context.manifest.path
    records = sum(1 for _ in context.manifest.entries())
    trusted = sum(1 for e in context.manifest.latest_by_url().values() if e.is_stored)
    if manifest_path.is_file():
        context.mirror.mirror_snapshot(
            manifest_path,
            "manifests",
            generation_id=context.report.generation_id,
            records=records,
            trusted=trusted,
            label=label,
        )
    save_checkpoint(context.checkpoint_path, context.checkpoint)
    context.mirror.mirror_snapshot(
        context.checkpoint_path, f"chunks/{context.report.chunk:03d}/checkpoints", label=label
    )
    summary = _write_summary(context, [], manifest_records=records, trusted=trusted)
    context.mirror.mirror_snapshot(
        summary, f"chunks/{context.report.chunk:03d}/summaries", label=label
    )
    context.report.manifest.update({"records": records, "trusted": trusted})
    return (
        f"{label}: manifest {records} records / {trusted} trusted; checkpoint and summary mirrored"
    )


def _harvest_pass(context: _Context, targets: Sequence[Target], label: str) -> HarvestReport:
    """One pass with the V2 chain; a second pass for anything FAILED; then stop conditions.

    Targets go through the harvester one at a time so that a `403` or a
    sustained `429` stops the run *before* the next request, not at the end
    of the pass — the SEC's refusal is a stop condition, and every request
    after it would be a request the caller was told not to make.
    """
    requests_before = context.budget.requests_made
    bytes_before = context.budget.bytes_written
    try:
        first = _one_at_a_time(context, targets)
        report = first
        if first.failures:
            second = _one_at_a_time(context, targets)
            report = second
            if second.failures:
                raise CanaryStopError(
                    f"{label}: {len(second.failures)} targets still failed after a second pass: "
                    f"{[url for url, _ in second.failures[:3]]}"
                )
        if report.stopped_on_budget:
            raise CanaryStopError(f"{label}: the request or byte budget was reached")
    finally:
        # Whatever the pass spent stays on the record, stop or not, and
        # accumulates across resumes: a replay that fetches nothing adds nothing.
        requests = context.checkpoint.requests
        stored = context.checkpoint.bytes_stored
        requests[label] = requests.get(label, 0) + context.budget.requests_made - requests_before
        stored[label] = stored.get(label, 0) + context.budget.bytes_written - bytes_before
    return report


def _one_at_a_time(context: _Context, targets: Sequence[Target]) -> HarvestReport:
    """Harvest each target separately, merging the reports; stop on refusal or budget."""
    merged = HarvestReport()
    for target in targets:
        part = harvest_artifacts(
            [target],
            context.storage,
            context.manifest,
            context.documents,
            budget=context.budget,
            verify_written=True,
        )
        for outcome, count in part.by_outcome.items():
            merged.by_outcome[outcome] = merged.by_outcome.get(outcome, 0) + count
        merged.failures.extend(part.failures)
        merged.stopped_on_budget = merged.stopped_on_budget or part.stopped_on_budget
        _stop_on_auth_or_ratelimit(part)
        if part.stopped_on_budget:
            break
    merged.bytes_written = context.budget.bytes_written
    merged.requests_made = context.budget.requests_made
    return merged


_STAGE_ORDER = (
    ChunkStage.PENDING,
    ChunkStage.PASS1_DONE,
    ChunkStage.PASS2_DONE,
    ChunkStage.CONVERGED,
    ChunkStage.COMPLETE,
)


def _advance(checkpoint: Checkpoint, stage: ChunkStage) -> None:
    """Move the checkpoint forward to `stage`, never backwards.

    A resume walks every step again, and a chunk already `PASS2_DONE` must
    not read `PASS1_DONE` in the snapshot mirrored after its replayed first
    pass. `FAILED` is set directly by the step that fails, and a rerun over
    a `FAILED` checkpoint that passes its steps moves forward again.
    """
    current = checkpoint.stage
    if current not in _STAGE_ORDER or _STAGE_ORDER.index(stage) > _STAGE_ORDER.index(current):
        checkpoint.stage = stage


def _stop_on_auth_or_ratelimit(report: HarvestReport) -> None:
    for url, reason in report.failures:
        if reason.startswith((ProviderAuthError.__name__, ProviderRateLimitError.__name__)):
            raise CanaryStopError(f"SEC refused the caller ({reason.split(':', 1)[0]}) at {url}")


def _run(context: _Context, identities: Sequence[IdentityRow], cap: int) -> None:
    report = context.report
    checkpoint = context.checkpoint

    _step(context, "preflight", lambda: _preflight(context))
    _step(context, "mirror offline plans", lambda: _mirror_offline_plans(context))

    rows: list[ResolutionRow] = []
    _step(context, "resolve filers", lambda: _describe_resolution(context, rows, identities))
    mine: list[ResolutionRow] = []
    _step(context, "freeze chunk", lambda: _describe_freeze(context, rows, mine))
    _step(
        context,
        "mirror chunk plan",
        lambda: (
            _mirror_paths(
                context,
                [
                    (
                        context.generation_dir / "filer_resolution_v1.jsonl",
                        "resolution/filer_resolution_v1.jsonl",
                    ),
                    (
                        context.generation_dir / "filer_resolution_v1.sha256",
                        "resolution/filer_resolution_v1.sha256",
                    ),
                    (
                        context.generation_dir / "filer_resolution_v1.summary.json",
                        "resolution/filer_resolution_v1.summary.json",
                    ),
                    (
                        context.generation_dir / "prerequisites" / "company_tickers.json",
                        "prerequisites/company_tickers.json",
                    ),
                    (
                        context.generation_dir / "prerequisites" / "company_tickers.meta.json",
                        "prerequisites/company_tickers.meta.json",
                    ),
                    (
                        context.chunk_dir / "identities.jsonl",
                        f"chunks/{report.chunk:03d}/identities.jsonl",
                    ),
                    (
                        context.chunk_dir / "identities.sha256",
                        f"chunks/{report.chunk:03d}/identities.sha256",
                    ),
                ],
            )
            + "; "
            + _snapshot_and_mirror(context, "initialised")
        ),
    )

    resolutions, submissions = _chunk_targets(mine)

    def pass_one() -> str:
        outcome = _harvest_pass(context, submissions, "submissions")
        trusted_now = {e.url for e in context.manifest.latest_by_url().values() if e.is_stored}
        missing = [t.url for t in submissions if t.url not in trusted_now]
        if missing:
            raise CanaryStopError(f"submissions not trusted after pass 1: {missing[:3]}")
        report.submissions = {
            "targets": len(submissions),
            **{k.lower(): v for k, v in outcome.by_outcome.items()},
            "bytes": outcome.bytes_written,
        }
        _advance(checkpoint, ChunkStage.PASS1_DONE)
        return f"{len(submissions)} targets: {outcome.summary()}"

    _step(context, "pass 1: submissions", pass_one)
    _step(context, "snapshot after submissions", lambda: _snapshot_and_mirror(context, "pass1"))

    filings: list[Target] = []

    def derive() -> str:
        once = _dedupe_filings(
            filing_targets(context.storage, resolutions, filings_per_security=cap)
        )
        twice = _dedupe_filings(
            filing_targets(context.storage, resolutions, filings_per_security=cap)
        )
        if [_target_line(t) for t in once] != [_target_line(t) for t in twice]:
            raise CanaryStopError("filing target derivation is not deterministic")
        filings.extend(once)
        lines = [_target_line(t) for t in once]
        sha = _write_lines(context.chunk_dir / "filing_targets.jsonl", lines)
        keys = {(t.cik, t.accession) for t in once}
        report.filings = {"targets": len(once), "unique_cik_accession": len(keys)}
        report.manifest["filing_targets_sha256"] = sha
        note = f"filing_targets_sha256={sha}"
        if note not in checkpoint.notes:
            checkpoint.notes.append(note)
        return (
            f"{len(once)} targets over {len(keys)} (CIK, accession) pairs, cap {cap}, "
            f"sha {sha[:16]}…"
        )

    _step(context, "derive filing targets", derive)
    _step(
        context,
        "mirror filing plan",
        lambda: (
            _mirror_paths(
                context,
                [
                    (
                        context.chunk_dir / "filing_targets.jsonl",
                        f"chunks/{report.chunk:03d}/filing_targets.jsonl",
                    ),
                    (
                        context.chunk_dir / "filing_targets.sha256",
                        f"chunks/{report.chunk:03d}/filing_targets.sha256",
                    ),
                ],
            )
            + "; "
            + _snapshot_and_mirror(context, "targets-frozen")
        ),
    )

    def pass_two() -> str:
        outcome = _harvest_pass(context, filings, "filings")
        trusted_now = {e.url for e in context.manifest.latest_by_url().values() if e.is_stored}
        missing = [t.url for t in filings if t.url not in trusted_now]
        if missing:
            raise CanaryStopError(f"filings not trusted after pass 2: {missing[:3]}")
        report.filings.update(
            {
                **{k.lower(): v for k, v in outcome.by_outcome.items()},
                "bytes": outcome.bytes_written,
            }
        )
        _advance(checkpoint, ChunkStage.PASS2_DONE)
        return f"{len(filings)} targets: {outcome.summary()}"

    _step(context, "pass 2: filings", pass_two)
    _step(context, "snapshot after filings", lambda: _snapshot_and_mirror(context, "pass2"))

    def converge() -> str:
        before_requests = context.budget.requests_made
        before_bytes = context.budget.bytes_written
        before_puts = context.b2_counts.get("call:PutObject", 0)
        outcome = harvest_artifacts(
            [*submissions, *filings],
            context.storage,
            context.manifest,
            context.documents,
            budget=context.budget,
            verify_written=True,
        )
        fetched = context.budget.requests_made - before_requests
        written = context.budget.bytes_written - before_bytes
        puts = context.b2_counts.get("call:PutObject", 0) - before_puts
        hits = outcome.by_outcome.get(str(Outcome.CACHE_HIT), 0)
        if fetched or written or puts or hits != len(submissions) + len(filings):
            raise CanaryStopError(
                f"replay did not converge: {fetched} fetches, {written} bytes, {puts} PUTs, "
                f"{hits} cache hits of {len(submissions) + len(filings)}"
            )
        _advance(checkpoint, ChunkStage.CONVERGED)
        return f"{hits} CACHE_HIT, 0 fetches, 0 bytes, {puts} PUTs"

    _step(context, "converge: replay", converge)

    def verify() -> str:
        state = reconcile(context.storage, context.manifest, verify_digests=True)
        report.reconciliation = {
            "trusted": state.trusted,
            "objects": state.files_on_disk,
            "orphan_files": state.orphan_files,
            "orphan_records": state.orphan_records,
            "byte_mismatches": state.byte_mismatches,
            "hash_mismatches": state.hash_mismatches,
            "path_violations": state.path_violations,
            "zero_byte": state.zero_byte,
            "duplicate_urls": state.duplicate_urls,
        }
        if not state.clean or state.zero_byte:
            checkpoint.stage = ChunkStage.FAILED
            raise CanaryStopError(f"full verification failed: {state.summary()}")
        if state.trusted != len(submissions) + len(filings):
            checkpoint.stage = ChunkStage.FAILED
            raise CanaryStopError(
                f"trusted {state.trusted} != chunk targets {len(submissions) + len(filings)}"
            )
        checkpoint.accounting = {
            "trusted": state.trusted,
            "submissions": state.submissions,
            "filings": state.filings,
            "manifest_records": state.manifest_records,
        }
        checkpoint.verification = {"full_corpus": state.trusted}
        checkpoint.completed_at = datetime.now(UTC).isoformat()
        _advance(checkpoint, ChunkStage.COMPLETE)
        report.manifest["logical_bytes"] = state.logical_bytes
        return f"{state.summary()}; every artifact downloaded and hashed"

    _step(context, "verify: full reconciliation", verify)
    _step(context, "snapshot: complete", lambda: _snapshot_and_mirror(context, "complete"))
    _step(context, "recovery: prove the mirror", lambda: _prove_recovery(context, mine))


def _describe_resolution(
    context: _Context, rows: list[ResolutionRow], identities: Sequence[IdentityRow]
) -> str:
    rows.extend(_resolve(context, identities))
    return f"{context.report.resolution}"


def _describe_freeze(
    context: _Context, rows: Sequence[ResolutionRow], mine: list[ResolutionRow]
) -> str:
    mine.extend(_freeze_chunk(context, rows))
    resolved = sum(1 for r in mine if r.cik is not None)
    return (
        f"{len(mine)} identities, {resolved} resolved, sha "
        f"{context.checkpoint.selection_digest[:16]}…; checkpoint {context.checkpoint.stage}"
    )


def _mirror_paths(context: _Context, pairs: Sequence[tuple[Path, str]]) -> str:
    outcomes = [context.mirror.mirror(path, key).outcome for path, key in pairs]
    counts = {outcome: outcomes.count(outcome) for outcome in outcomes}
    return f"{len(pairs)} files: {counts}"


def _prove_recovery(context: _Context, mine: Sequence[ResolutionRow]) -> str:
    """Read-only: could a new machine rebuild the control plane from the mirror alone?"""
    listing = context.mirror.listing()
    chunk = f"chunks/{context.report.chunk:03d}"
    required = [
        "generation.json",
        "plans/base_identities.jsonl",
        "plans/base_identities.sha256",
        "plans/base_targets_offline.jsonl",
        "plans/base_targets_offline.sha256",
        "prerequisites/company_tickers.json",
        "prerequisites/company_tickers.meta.json",
        "resolution/filer_resolution_v1.jsonl",
        "resolution/filer_resolution_v1.sha256",
        f"{chunk}/identities.jsonl",
        f"{chunk}/identities.sha256",
        f"{chunk}/filing_targets.jsonl",
        f"{chunk}/filing_targets.sha256",
    ]
    missing = [key for key in required if key not in listing]
    if missing:
        raise CanaryStopError(f"mirror cannot recover the generation: missing {missing}")
    # Every required file's remote digest against the local file.
    local_for = {
        "generation.json": context.generation_dir / "generation.json",
        "resolution/filer_resolution_v1.jsonl": context.generation_dir
        / "filer_resolution_v1.jsonl",
        f"{chunk}/identities.jsonl": context.chunk_dir / "identities.jsonl",
        f"{chunk}/filing_targets.jsonl": context.chunk_dir / "filing_targets.jsonl",
    }
    for key, path in local_for.items():
        if not context.mirror.verify(path, key):
            raise CanaryStopError(f"mirror copy of {key} does not hash to the local file")
    # The newest manifest snapshot must describe exactly what the bucket holds.
    manifest_snapshot = context.mirror.newest_snapshot("manifests")
    if manifest_snapshot is None:
        raise CanaryStopError("mirror holds no manifest snapshot")
    _, remote_manifest = manifest_snapshot
    remote_trusted = {
        json.loads(line)["path"]
        for line in remote_manifest.decode("utf-8").splitlines()
        if line.strip() and json.loads(line).get("outcome") == "DOWNLOADED"
    }
    remote_objects = {k for p in ARTIFACT_PREFIXES for k in context.storage.list_prefix(p)}
    if remote_trusted != remote_objects:
        raise CanaryStopError(
            f"the mirrored manifest names {len(remote_trusted)} artifacts; the bucket holds "
            f"{len(remote_objects)}"
        )
    checkpoint_snapshot = context.mirror.newest_snapshot(f"{chunk}/checkpoints")
    if checkpoint_snapshot is None:
        raise CanaryStopError("mirror holds no checkpoint snapshot")
    _, remote_checkpoint = checkpoint_snapshot
    stage = json.loads(remote_checkpoint).get("stage")
    if stage != ChunkStage.COMPLETE:
        raise CanaryStopError(f"the mirrored checkpoint says {stage}, not COMPLETE")
    context.report.recovery_sufficient = True
    return (
        f"{len(listing)} mirrored objects; required files present and hashed; newest manifest "
        f"snapshot names {len(remote_trusted)} artifacts == {len(remote_objects)} bucket objects; "
        f"mirrored checkpoint COMPLETE; {len(mine)} identities recoverable"
    )
