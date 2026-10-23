"""Harvesting an explicit list of filings, instead of discovering them.

The production harvest answers "which filings should this security have?" — it
resolves a CIK, fetches a submissions catalogue, and takes the six most recent
qualifying documents. That is the right shape for building a corpus from
nothing, and the wrong shape for executing a plan: Phase 9L.2 already decided
exactly which 800 filings to fetch, and re-deriving them would both waste
requests and risk selecting something else.

So this module adds one thing and reuses everything else. It turns a frozen
target manifest into the `Target` records `harvest()` already accepts, and hands
them to the existing writer. There is no second downloader, no second evidence
format, and no second provenance contract — a manifest-targeted artifact is
indistinguishable from a discovered one once stored, which is the point.

## The filing identity is the pair

`(cik, accession)`, never the accession alone. Phase 9L.2 measured 17 accessions
held under more than one CIK: co-registrant filings, where a utility parent and
its subsidiaries file one document and EDGAR serves identical bytes under each
registrant's path. Keying on the accession would silently merge those, and a
plan that asked for a subsidiary's provenance would receive the parent's.

## Nothing is resolved over the network

The document filename comes from the submissions catalogue already on disk. A
manifest replay where every target is held should issue **zero** HTTP requests —
no ticker map, no catalogue refetch, nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.harvest import Target
from stock_screener.sec_evidence.storage import RequestKind

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.manifest import Manifest

log = structlog.get_logger(__name__)


class TargetManifestError(RuntimeError):
    """The manifest is not the one the caller said to execute.

    Fatal on purpose. A validation batch whose population quietly changed is no
    longer a validation of anything, and the whole value of a frozen manifest is
    that the thing executed is the thing that was planned.
    """


@dataclass(frozen=True)
class ManifestTarget:
    """One planned filing, as the manifest states it.

    Attributes:
        cik: The filer.
        accession: The submission id.
        form: Form type.
        filing_date: Catalogue filing date. Planning metadata — the acceptance
            timestamp is only knowable once the document is in hand.
        potential_months: Month-ends the planner expected it to support.
        yield_adjusted_months: The same, discounted by observed form yield.
        estimated_bytes: What the planner budgeted for it.
    """

    cik: int
    accession: str
    form: str
    filing_date: str
    potential_months: int
    yield_adjusted_months: float
    estimated_bytes: int

    @property
    def key(self) -> tuple[int, str]:
        """The stable filing identity."""
        return (self.cik, self.accession)


@dataclass(frozen=True)
class ManifestAudit:
    """What verifying a manifest found.

    Attributes:
        rows: Records read.
        unique_keys: Distinct `(cik, accession)` pairs.
        forms: Count per form.
        years: Count per filing year.
        filers: Distinct CIKs.
        digest: SHA-256 of the file as read.
        already_held: Targets the corpus already trusts.
    """

    rows: int
    unique_keys: int
    forms: dict[str, int]
    years: dict[str, int]
    filers: int
    digest: str
    already_held: tuple[tuple[int, str], ...] = ()

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{self.rows} targets, {self.unique_keys} unique, {self.filers} filers, "
            f"forms {self.forms}, sha {self.digest[:16]}"
        )


def load_manifest(
    path: Path, *, expect_sha256: str | None = None, expect_rows: int | None = None
) -> tuple[tuple[ManifestTarget, ...], ManifestAudit]:
    """Read and verify a frozen target manifest.

    Args:
        path: The JSONL manifest.
        expect_sha256: Digest the caller requires. A mismatch raises rather than
            warns — executing a different population than the one planned is the
            failure this guards.
        expect_rows: Row count the caller requires.

    Returns:
        The targets and what verification found.

    Raises:
        TargetManifestError: Missing, malformed, mismatched, or holding
            duplicate filing identities.
    """
    if not path.is_file():
        raise TargetManifestError(f"no target manifest at {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expect_sha256 is not None and digest != expect_sha256:
        raise TargetManifestError(
            f"{path} has sha256 {digest}, expected {expect_sha256} — "
            "this is not the frozen manifest that was planned"
        )

    targets: list[ManifestTarget] = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TargetManifestError(f"{path} line {number} is not JSON: {exc}") from exc
        try:
            targets.append(
                ManifestTarget(
                    cik=int(row["cik"]),
                    accession=str(row["accession"]),
                    form=str(row["form"]),
                    filing_date=str(row["filing_date"]),
                    potential_months=int(row.get("potential_months", 0)),
                    yield_adjusted_months=float(row.get("yield_adjusted_months", 0.0)),
                    estimated_bytes=int(row.get("estimated_bytes", 0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TargetManifestError(f"{path} line {number} is malformed: {exc}") from exc

    keys = [t.key for t in targets]
    if len(set(keys)) != len(keys):
        duplicates = {k for k in keys if keys.count(k) > 1}
        raise TargetManifestError(f"{path} repeats filing identities: {sorted(duplicates)[:5]}")
    if expect_rows is not None and len(targets) != expect_rows:
        raise TargetManifestError(f"{path} holds {len(targets)} targets, expected {expect_rows}")

    forms: dict[str, int] = {}
    years: dict[str, int] = {}
    for target in targets:
        forms[target.form] = forms.get(target.form, 0) + 1
        year = target.filing_date[:4]
        years[year] = years.get(year, 0) + 1

    audit = ManifestAudit(
        rows=len(targets),
        unique_keys=len(set(keys)),
        forms=forms,
        years=dict(sorted(years.items())),
        filers=len({t.cik for t in targets}),
        digest=digest,
    )
    log.info("target manifest verified", summary=audit.summary())
    return tuple(targets), audit


def resolve_documents(
    targets: Sequence[ManifestTarget], catalogue: sqlite3.Connection
) -> tuple[tuple[Target, ...], tuple[ManifestTarget, ...]]:
    """Turn manifest targets into harvest targets, using only stored catalogues.

    The document filename and URL come from the submissions catalogues already
    on disk, so this resolves nothing over the network — no ticker map, no
    catalogue refetch. A target the catalogue cannot describe is returned as
    unresolved rather than guessed at: constructing a plausible URL for a
    document whose name we do not know would produce a 404 charged to the
    budget, and a `FAILED` record that looks like an SEC problem.

    Args:
        targets: What the manifest asked for.
        catalogue: The derived index holding the parsed catalogues.

    Returns:
        `(resolved, unresolved)`.
    """
    resolved: list[Target] = []
    unresolved: list[ManifestTarget] = []
    for target in targets:
        row = catalogue.execute(
            "SELECT url, primary_document, form, filing_date FROM catalogue"
            " WHERE cik = ? AND accession = ?",
            (target.cik, target.accession),
        ).fetchone()
        if row is None or not row[0] or not row[1]:
            unresolved.append(target)
            continue
        url, document, form, filing_date = row
        resolved.append(
            Target(
                url=str(url),
                kind=RequestKind.FILING,
                cik=target.cik,
                accession=target.accession,
                document=str(document),
                form=str(form or target.form),
                filing_date=str(filing_date or target.filing_date),
            )
        )
    log.info("manifest targets resolved", resolved=len(resolved), unresolved=len(unresolved))
    return tuple(resolved), tuple(unresolved)


def held_keys(catalogue: sqlite3.Connection) -> frozenset[tuple[int, str]]:
    """Every `(cik, accession)` the **derived index** records as a filing.

    Useful for planning against a fixed evidence snapshot. It is *not* the right
    source for "do we already have this" — the index is rebuilt on demand and can
    lag the corpus. Use `held_from_manifest` for that.
    """
    return frozenset(
        (int(cik), str(accession))
        for cik, accession in catalogue.execute(
            "SELECT cik, accession FROM filings WHERE cik IS NOT NULL AND accession IS NOT NULL"
        )
    )


def held_from_manifest(manifest: Manifest) -> frozenset[tuple[int, str]]:
    """Every `(cik, accession)` the corpus actually holds, per the live manifest.

    The manifest is the authority on what has been acquired; the derived index is
    a snapshot that may predate the most recent harvest. Checking a target list
    against the index would let a run re-request filings already on disk — which
    is exactly what a stale index caused in Phase 9L.4, caught there by an
    overlap assertion rather than by the check that was supposed to prevent it.
    """
    return frozenset(
        (int(entry.cik), entry.accession)
        for entry in manifest.latest_by_url().values()
        if entry.is_stored
        and entry.request_kind == "FILING"
        and entry.cik is not None
        and entry.accession is not None
    )
