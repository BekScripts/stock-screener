"""A clean-room second generation of the SEC evidence corpus, planned from what survived.

The first generation — 38,355 baseline artifacts, two validation batches and
eighteen Plan A3 batches, with the manifest that vouched for all of it — lived
on one external drive, and the drive is gone. Nothing on it is coming back,
and nothing about it can be trusted from memory: a number in a report is not
a manifest record. This module starts the second generation from the only
inputs that survived on the internal disk, says exactly what those inputs can
and cannot reproduce, and writes the answer down so that the phases that fetch
bytes have a frozen, verifiable starting point.

## What survived, and what it decides

- `stage9k.db`, read-only, holds the production universe: 8,919 identities in
  a total order whose digest reproduces exactly. That is the root of every
  target the first generation ever requested, and it is intact.
- The same database holds 402 stored CIKs (delisted securities). The other
  8,517 identities were resolved to filers through the SEC's ticker map at
  harvest time and that map was never persisted; resolving them again is a
  network step. So the baseline is **partially** reproducible offline: the
  identity plan and the stored-CIK submissions targets are exact; the rest of
  the submissions targets, and every filing-document target beneath them,
  require the SEC.
- Plan A3 is **not** reproducible offline. Its inputs were the parsed
  baseline corpus — candidate filings from the submissions catalogues, cover
  rows for class attribution and form yield, measured sizes — none of which
  survive, and the code that assembled those inputs into `PlanningInputs` ran
  from working files that were never committed. The planner's algorithm is in
  the repository; its inputs and its harness are not. A3 is regenerated after
  the baseline is rebuilt and re-indexed, and the resulting digest is
  *compared* to the frozen one, never forced to match.

## What this module refuses

It fetches nothing, writes nothing to the bucket, and carries no first-
generation progress forward: the second generation starts with zero trusted
artifacts and zero completed batches, and a checkpoint claiming otherwise
over an empty manifest is reported as a contradiction, not honoured.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

from api_clients.sec_documents import submissions_url
from stock_screener.sec_evidence.binding import PRODUCTION_IDENTITIES, bind_source
from stock_screener.sec_evidence.manifest import Manifest
from stock_screener.sec_evidence.sample import production_universe
from stock_screener.sec_evidence.storage import ARTIFACT_PREFIXES
from stock_screener.sec_evidence.sweep import ChunkStage, load_checkpoint

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot

log = structlog.get_logger(__name__)

__all__ = [
    "A3_EXPECTED_SHA256",
    "A3_EXPECTED_TARGETS",
    "GENERATION_ID",
    "PRODUCTION_CHUNK",
    "A3Status",
    "CikResolution",
    "GenerationMetadata",
    "GenerationPlan",
    "IdentityRow",
    "Prerequisite",
    "PrerequisiteStatus",
    "build_generation_plan",
    "identity_plan",
    "prerequisites",
    "v2_progress",
    "write_generation_plan",
]

#: The second generation. Never part of an artifact key: keys stay
#: `filings/<cik>/<accession>/<document>` in every generation, so the manifest
#: and the bucket layout do not change when the corpus is rebuilt.
GENERATION_ID = "SEC_EVIDENCE_GENERATION_V2"

#: The first generation, as history. Unavailable; never reused for trust.
PREVIOUS_GENERATION_ID = "SEC_EVIDENCE_GENERATION_V1"

#: The production sweep's chunk: 36 chunks of 250 over 8,919 identities, the
#: last holding 169. Recorded here so the identity plan carries the chunk each
#: identity belonged to, in the same arithmetic the sweep uses.
PRODUCTION_CHUNK = 250

#: The frozen Plan A3, as a reproducibility target. Sixty-one thousand nine
#: hundred and forty-one targets and this digest. Not a claim that the second
#: generation can reach it; the claim is that when a plan is regenerated, it
#: is compared to this and the comparison is reported either way.
A3_EXPECTED_TARGETS = 61_941
A3_EXPECTED_SHA256 = "53e3f0ca98ac54068232c13f6b0c214e969ff6e6f204000d5a050c43be3bdcd9"


class CikResolution(StrEnum):
    """How an identity's filer number can be known."""

    STORED = "STORED"
    """The database holds the CIK. Offline, exact."""

    EDGAR_REQUIRED = "EDGAR_REQUIRED"
    """Only the SEC's ticker map can say. A network step, not performed here."""


class PrerequisiteStatus(StrEnum):
    """Whether an input to the rebuild exists, and where it would come from."""

    SURVIVING = "SURVIVING"
    """On the internal disk, verified."""

    NETWORK = "NETWORK"
    """Not on disk; obtainable from the SEC in a later, explicit phase."""

    MISSING = "MISSING"
    """Lost with the first generation; rebuilt from other inputs, never recovered."""


class A3Status(StrEnum):
    """Whether Plan A3 can be reproduced from what survived."""

    MISSING_PREREQUISITE = "MISSING_PREREQUISITE"
    """Its inputs are lost. Regenerate after the baseline is rebuilt and re-indexed."""

    REPRODUCED_EXACTLY = "A3_REPRODUCED_EXACTLY"
    """Regenerated and the digest equals the frozen one."""

    DIGEST_DIFFERS = "A3_DIGEST_DIFFERS"
    """Regenerated and the digest does not equal the frozen one. Reported, never forced."""


@dataclass(frozen=True)
class IdentityRow:
    """One production identity and how far it can be turned into a request offline.

    Attributes:
        position: Index in the production universe order.
        chunk: Which 250-identity sweep chunk it falls in.
        bucket: `LIVE` or `DELISTED`.
        symbol: Its ticker.
        name: The issuer name.
        company_id: The live company's id, when it is one.
        historical_security_id: The delisted security's id, when it is one.
        cik: The stored filer number, or None.
        resolution: `STORED` or `EDGAR_REQUIRED`.
        submissions_url: The submissions index request, when the CIK is stored.
    """

    position: int
    chunk: int
    bucket: str
    symbol: str
    name: str
    company_id: int | None
    historical_security_id: int | None
    cik: int | None
    resolution: str
    submissions_url: str | None

    def line(self) -> str:
        """The row as one JSONL line, keys sorted."""
        return json.dumps(asdict(self), sort_keys=True)


def identity_plan(session: Session) -> list[IdentityRow]:
    """The production universe as the second generation's identity plan.

    Exactly the order and membership `production_universe` yields — the same
    function the first generation's sweep used, over the same database — with
    each identity's chunk, its stored CIK if any, and the submissions request
    that CIK implies. Nothing is resolved over the network; an identity without
    a stored CIK is marked `EDGAR_REQUIRED` and carries no URL.

    Args:
        session: A read-only session on the production database.

    Returns:
        One row per identity, in universe order.
    """
    rows: list[IdentityRow] = []
    for position, selection in enumerate(production_universe(session)):
        stored = selection.cik is not None
        rows.append(
            IdentityRow(
                position=position,
                chunk=position // PRODUCTION_CHUNK,
                bucket=selection.bucket,
                symbol=selection.symbol,
                name=selection.name,
                company_id=selection.company_id,
                historical_security_id=selection.historical_security_id,
                cik=selection.cik,
                resolution=(CikResolution.STORED if stored else CikResolution.EDGAR_REQUIRED).value,
                submissions_url=submissions_url(selection.cik)
                if selection.cik is not None
                else None,
            )
        )
    return rows


@dataclass(frozen=True)
class Prerequisite:
    """One input the rebuild needs, and whether it exists.

    Attributes:
        name: What it is.
        status: Where it stands.
        needed_by: The rebuild stage that first needs it.
        detail: Where it survives, or how it is obtained, or why it is lost.
    """

    name: str
    status: str
    needed_by: str
    detail: str


def prerequisites(session: Session, universe_rows: list[IdentityRow]) -> list[Prerequisite]:
    """What the rebuild needs, from the database and the code as they are now.

    Read-only against the database. Each answer is a fact about a table or a
    module, not a recollection.
    """
    stored = sum(1 for row in universe_rows if row.resolution == CikResolution.STORED)
    unresolved = len(universe_rows) - stored

    acceptance = _count(session, "filing_acceptance")
    months = _count(session, "research_universe_months")
    ftd_files = _count(session, "ftd_source_files")
    return [
        Prerequisite(
            "production universe (8,919 identities, total order, digest)",
            PrerequisiteStatus.SURVIVING,
            "V2.1 identity plan",
            "stage9k.db via production_universe(); digest reproduces exactly",
        ),
        Prerequisite(
            f"stored CIKs ({stored} identities)",
            PrerequisiteStatus.SURVIVING,
            "V2.1 submissions targets",
            "historical_securities.cik; submissions URLs derived offline",
        ),
        Prerequisite(
            f"ticker-to-CIK map ({unresolved} identities without a stored CIK)",
            PrerequisiteStatus.NETWORK,
            "V2.1 CIK resolution",
            "SEC company_tickers.json through the existing resolver; not persisted by V1",
        ),
        Prerequisite(
            "submissions catalogues (one per resolved CIK)",
            PrerequisiteStatus.NETWORK,
            "V2.2 baseline harvest, pass 1",
            "data.sec.gov/submissions/CIK<cik>.json; also the source of filing-document targets",
        ),
        Prerequisite(
            "baseline filing documents (cap per security, most recent first)",
            PrerequisiteStatus.NETWORK,
            "V2.2 baseline harvest, pass 2",
            "sec.gov/Archives/... primary documents named by each catalogue",
        ),
        Prerequisite(
            "derived evidence index (catalogue, filings, securities, cover rows)",
            PrerequisiteStatus.MISSING,
            "V2.3 re-index",
            "rebuilt by build_index over V2 artifacts; V1 index lost with the drive",
        ),
        Prerequisite(
            f"filing acceptance catalogue ({acceptance:,} rows)",
            PrerequisiteStatus.SURVIVING,
            "V2.3 evidence dates",
            "stage9k.db filing_acceptance (source sec-edgar); accession, accepted_at, form",
        ),
        Prerequisite(
            f"research universe months ({months:,} rows, frozen)",
            PrerequisiteStatus.SURVIVING,
            "V2.4 coverage gaps",
            "stage9k.db research_universe_months; the class-attribution output of V1, frozen",
        ),
        Prerequisite(
            "Plan A3 planning inputs (candidates, form yield, sizes, held set)",
            PrerequisiteStatus.MISSING,
            "V2.5 Plan A3 regeneration",
            "derived from the parsed baseline corpus; the harness that assembled "
            "PlanningInputs is not in the repository — plan.py holds the algorithm only",
        ),
        Prerequisite(
            "frozen Plan A3, remainder and 61 batch manifests",
            PrerequisiteStatus.MISSING,
            "V2.5 comparison",
            f"only the digest survives ({A3_EXPECTED_SHA256[:16]}…, "
            f"{A3_EXPECTED_TARGETS:,} targets); a regenerated plan is compared to it, "
            "never forced to match",
        ),
        Prerequisite(
            f"fails-to-deliver archives ({ftd_files} source files recorded)",
            PrerequisiteStatus.MISSING,
            "outside the evidence rebuild",
            "V1 cache lost with the drive; observations survive in stage9k.db; "
            "archives are public and redownloadable; scheduled separately",
        ),
    ]


_COUNTED_TABLES = {
    "filing_acceptance": text("select count(*) from filing_acceptance"),
    "research_universe_months": text("select count(*) from research_universe_months"),
    "ftd_source_files": text("select count(*) from ftd_source_files"),
}


def _count(session: Session, table: str) -> int:
    """Row count of one of a fixed set of tables, read-only."""
    return int(session.execute(_COUNTED_TABLES[table]).scalar_one())


@dataclass(frozen=True)
class GenerationMetadata:
    """What the second generation is, and where it starts from. No secrets.

    Attributes:
        generation_id: The generation.
        previous_generation: The lost one, as history.
        created_at: When this record was written.
        repo_commit: The commit the plan was produced from.
        database_path: The production database, resolved.
        database_sha256: Its digest, when computed.
        universe_count: Identities in the universe.
        universe_sha256: The universe digest.
        identities_stored_cik: Identities with an offline CIK.
        identities_edgar_required: Identities needing the SEC ticker map.
        baseline_plan_status: `PARTIAL` — identities exact, filers partial.
        identity_plan_sha256: Digest of the identity plan file.
        offline_targets: Submissions targets derivable offline.
        offline_targets_sha256: Digest of that file.
        a3_status: Whether Plan A3 is reproducible now.
        a3_expected_targets: The frozen count.
        a3_expected_sha256: The frozen digest.
        v2_trusted_artifacts: Trusted records in the V2 manifest. Zero to start.
        v2_completed_batches: COMPLETE checkpoints consistent with the manifest.
        inconsistent_checkpoints: Checkpoints claiming completion the manifest cannot support.
        production_b2_objects: Objects under the artifact prefixes, when checked.
        missing_prerequisites: Names of inputs that are lost or need the network.
    """

    generation_id: str
    previous_generation: str
    created_at: str
    repo_commit: str | None
    database_path: str
    database_sha256: str | None
    universe_count: int
    universe_sha256: str
    identities_stored_cik: int
    identities_edgar_required: int
    baseline_plan_status: str
    identity_plan_sha256: str
    offline_targets: int
    offline_targets_sha256: str
    a3_status: str
    a3_expected_targets: int
    a3_expected_sha256: str
    v2_trusted_artifacts: int
    v2_completed_batches: int
    inconsistent_checkpoints: list[str]
    production_b2_objects: dict[str, int] | None
    missing_prerequisites: list[str]


@dataclass
class GenerationPlan:
    """The identity plan, the offline targets, the prerequisites, the metadata."""

    identities: list[IdentityRow]
    prerequisites: list[Prerequisite]
    metadata: GenerationMetadata
    paths: dict[str, Path] = field(default_factory=dict)

    @property
    def offline_target_rows(self) -> list[IdentityRow]:
        """Identities whose submissions request is known without the network."""
        return [row for row in self.identities if row.submissions_url is not None]


def _digest_lines(rows: list[IdentityRow]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.line().encode() + b"\n")
    return digest.hexdigest()


def v2_progress(control: EvidenceControlRoot) -> tuple[int, int, list[str]]:
    """Trusted artifacts and completed batches in the second generation, from its own files.

    A checkpoint is counted `COMPLETE` only if the manifest holds at least as
    many trusted artifacts as the checkpoint claims to have accounted for. A
    checkpoint that says a batch finished over a manifest that vouches for
    nothing is a first-generation file that wandered in, and is reported as
    inconsistent rather than honoured.

    Returns:
        `(trusted, completed batches, inconsistent checkpoint names)`.
    """
    manifest = Manifest(control.manifest_path)
    trusted = sum(1 for entry in manifest.latest_by_url().values() if entry.is_stored)
    completed = 0
    inconsistent: list[str] = []
    if control.checkpoints.is_dir():
        for path in sorted(control.checkpoints.glob("chunk-*.json")):
            checkpoint = load_checkpoint(path)
            if checkpoint is None or checkpoint.stage != ChunkStage.COMPLETE:
                continue
            claimed = int(checkpoint.accounting.get("trusted", 0))
            if trusted == 0 or claimed > trusted:
                inconsistent.append(path.name)
            else:
                completed += 1
    return trusted, completed, inconsistent


def build_generation_plan(
    session: Session,
    database: Path,
    control: EvidenceControlRoot,
    *,
    expect_universe_count: int = PRODUCTION_IDENTITIES,
    expect_universe_sha256: str | None = None,
    repo_commit: str | None = None,
    remote: EvidenceStorage | None = None,
    verify_database_digest: bool = False,
) -> GenerationPlan:
    """Plan the second generation from the surviving database and control root.

    Args:
        session: A read-only session on the production database.
        database: The database file, for binding.
        control: The second generation's control root. Its manifest and
            checkpoints define the starting state; they are expected empty.
        expect_universe_count: Identities the universe must hold; the production
            figure by default, stated rather than discovered.
        expect_universe_sha256: Refuse a database whose universe digest differs.
        repo_commit: Recorded in the metadata.
        remote: The bucket, read-only, to count objects under the artifact
            prefixes. None skips the check.
        verify_database_digest: Hash the database file (22 GB; about a minute).

    Returns:
        The plan, unwritten. `write_generation_plan` writes it.

    Raises:
        BindingError: If the database is not the production universe.
    """
    binding = bind_source(
        session,
        database,
        expect_count=expect_universe_count,
        expect_digest=expect_universe_sha256,
        verify_digest=verify_database_digest,
    )
    identities = identity_plan(session)
    offline = [row for row in identities if row.submissions_url is not None]
    needed = prerequisites(session, identities)
    trusted, completed, inconsistent = v2_progress(control)
    remote_objects = (
        {prefix: sum(1 for _ in remote.list_prefix(prefix)) for prefix in ARTIFACT_PREFIXES}
        if remote is not None
        else None
    )
    metadata = GenerationMetadata(
        generation_id=GENERATION_ID,
        previous_generation=PREVIOUS_GENERATION_ID,
        created_at=datetime.now(UTC).isoformat(),
        repo_commit=repo_commit,
        database_path=binding.path,
        database_sha256=binding.sha256,
        universe_count=binding.universe_count,
        universe_sha256=binding.universe_digest,
        identities_stored_cik=len(offline),
        identities_edgar_required=len(identities) - len(offline),
        baseline_plan_status="PARTIAL",
        identity_plan_sha256=_digest_lines(identities),
        offline_targets=len(offline),
        offline_targets_sha256=_digest_lines(offline),
        a3_status=A3Status.MISSING_PREREQUISITE.value,
        a3_expected_targets=A3_EXPECTED_TARGETS,
        a3_expected_sha256=A3_EXPECTED_SHA256,
        v2_trusted_artifacts=trusted,
        v2_completed_batches=completed,
        inconsistent_checkpoints=inconsistent,
        production_b2_objects=remote_objects,
        missing_prerequisites=[p.name for p in needed if p.status != PrerequisiteStatus.SURVIVING],
    )
    log.info(
        "generation plan built",
        generation=GENERATION_ID,
        identities=len(identities),
        offline_targets=len(offline),
        v2_trusted=trusted,
    )
    return GenerationPlan(identities=identities, prerequisites=needed, metadata=metadata)


def write_generation_plan(plan: GenerationPlan, control: EvidenceControlRoot) -> dict[str, Path]:
    """Write the plan under the control root's `plans/generation_v2/`.

    Deterministic: rows in universe order, keys sorted, one line each, and a
    `.sha256` companion over exactly those bytes for each JSONL file. The
    metadata carries `created_at`, so it is the one file that differs between
    two otherwise identical runs; the plans and their digests do not.
    """
    directory = control.plans / "generation_v2"
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "identities": directory / "base_identities.jsonl",
        "identities_sha256": directory / "base_identities.sha256",
        "offline_targets": directory / "base_targets_offline.jsonl",
        "offline_targets_sha256": directory / "base_targets_offline.sha256",
        "prerequisites": directory / "prerequisites.json",
        "generation": directory / "generation.json",
    }
    for name, rows in (
        ("identities", plan.identities),
        ("offline_targets", plan.offline_target_rows),
    ):
        with paths[name].open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row.line() + "\n")
        sha = _digest_lines(rows)
        paths[f"{name}_sha256"].write_text(f"{sha}  {paths[name].name}\n", encoding="utf-8")
    paths["prerequisites"].write_text(
        json.dumps([asdict(p) for p in plan.prerequisites], indent=2) + "\n", encoding="utf-8"
    )
    paths["generation"].write_text(
        json.dumps(asdict(plan.metadata), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    plan.paths = paths
    log.info("generation plan written", directory=str(directory))
    return paths
