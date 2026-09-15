"""Checking the corpus against itself: does the manifest describe what is on disk?

Every phase from 9K.14 onward has run this check, and until now it lived in
throwaway scripts. That was tolerable while a person read the output each time
and is not tolerable as a gate an orchestrator depends on: a sweep decides
whether to keep going based on this answer, so the answer needs tests.

The question is deliberately narrow. **An artifact is trusted only when the
manifest records it as stored and the bytes on disk still hash to the digest the
manifest recorded.** Everything this module reports is a way of failing that:

- an *orphan file* is bytes nobody vouches for;
- an *orphan record* is provenance for bytes that are gone;
- a *hash mismatch* is bytes that changed under a record that did not;
- a *path violation* is an artifact stored somewhere its URL does not describe.

None of these is repaired here. Reporting and repairing are separate jobs, and a
function that quietly fixed what it found would destroy the evidence that
something had gone wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.backend import UnsafeStorageKeyError, validate_artifact_key
from stock_screener.sec_evidence.manifest import Outcome
from stock_screener.sec_evidence.storage import ARTIFACT_PREFIXES

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.manifest import Entry, Manifest

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Reconciliation:
    """What one pass over the corpus found.

    Attributes:
        manifest_records: Every line the log holds, history included.
        trusted: Artifacts the manifest currently vouches for.
        submissions: Trusted submissions indexes.
        filings: Trusted filing documents.
        failed: URLs whose latest word is a failure.
        skipped: URLs whose latest word is a skip.
        files_on_disk: Evidence files present, manifest excluded.
        logical_bytes: Their combined size.
        orphan_files: Files no trusted record claims.
        orphan_records: Trusted records whose file is missing.
        hash_mismatches: Files whose digest no longer matches the record.
        byte_mismatches: Files whose size no longer matches the record.
        path_violations: Unsafe manifest keys or keys inconsistent with their URL.
            An unsafe key is counted here only, without an absence lookup.
        temporary_files: Compatibility field, always zero. Backend temporaries
            are excluded from the artifact listing.
        zero_byte: Trusted artifacts with no content.
        duplicate_urls: Trusted URLs recorded more than once.
        digests_verified: Whether digests were recomputed from disk.
    """

    manifest_records: int = 0
    trusted: int = 0
    submissions: int = 0
    filings: int = 0
    failed: int = 0
    skipped: int = 0
    files_on_disk: int = 0
    logical_bytes: int = 0
    orphan_files: int = 0
    orphan_records: int = 0
    hash_mismatches: int = 0
    byte_mismatches: int = 0
    path_violations: int = 0
    temporary_files: int = 0
    zero_byte: int = 0
    duplicate_urls: int = 0
    digests_verified: bool = True
    examples: dict[str, list[str]] = field(default_factory=dict)

    @property
    def problems(self) -> dict[str, int]:
        """Every failure category and its count. Empty when the corpus is sound."""
        found = {
            "orphan_files": self.orphan_files,
            "orphan_records": self.orphan_records,
            "hash_mismatches": self.hash_mismatches,
            "byte_mismatches": self.byte_mismatches,
            "path_violations": self.path_violations,
            "temporary_files": self.temporary_files,
            "zero_byte": self.zero_byte,
            "duplicate_urls": self.duplicate_urls,
        }
        return {name: count for name, count in found.items() if count}

    @property
    def clean(self) -> bool:
        """Whether every trusted artifact reconciles with its provenance."""
        return not self.problems

    def summary(self) -> str:
        """One line for a report."""
        state = "clean" if self.clean else f"PROBLEMS {self.problems}"
        return (
            f"{self.trusted} trusted artifacts ({self.submissions} submissions, "
            f"{self.filings} filings) over {self.manifest_records} records, "
            f"{self.logical_bytes:,} bytes — {state}"
        )


def reconcile(
    storage: EvidenceStorage, manifest: Manifest, *, verify_digests: bool = True
) -> Reconciliation:
    """Compare what the manifest claims against what storage holds.

    The whole-namespace check. Every trusted record is verified against the
    backend, and every artifact key the backend lists is checked for a record.
    That second half is O(corpus) by nature and is why this runs at scheduled
    milestones and the terminal gate rather than after every batch —
    `reconcile_subset` is the per-batch form.

    Args:
        storage: Where the artifacts live. An `EvidenceRoot` satisfies this;
            so does any other backend.
        manifest: The provenance log.
        verify_digests: Recompute every trusted artifact's SHA-256 from
            storage. Leaving this on is the point of the check; it is a
            parameter only so a caller measuring something else can skip it.

    Returns:
        The counts, and up to a few example URLs per failure category.
    """
    latest = manifest.latest_by_url()
    records = 0
    for _ in manifest.entries():
        records += 1

    trusted = [entry for entry in latest.values() if entry.is_stored]
    examples: dict[str, list[str]] = {}

    def note(category: str, url: str) -> None:
        found = examples.setdefault(category, [])
        if len(found) < 3:
            found.append(url)

    orphan_records = 0
    hash_mismatches = 0
    byte_mismatches = 0
    path_violations = 0
    zero_byte = 0
    claimed: set[str] = set()

    for entry in trusted:
        key = entry.path or ""
        claimed.add(key)
        # A key the backend would refuse is a provenance fault in its own
        # right: the record names something outside the namespace, and the
        # audit will not go looking there. Pre-B2 could only notice this for
        # a file that happened to exist at the escaped location.
        if not _key_is_safe(key):
            path_violations += 1
            note("path_violations", entry.url)
            continue
        # Every per-artifact question — is it there, how big, does it still
        # hash — is asked of the storage contract and nothing else.
        if not storage.exists(key):
            orphan_records += 1
            note("orphan_records", entry.url)
            continue
        if not _path_matches_url(entry):
            path_violations += 1
            note("path_violations", entry.url)
        size = storage.stat(key).bytes
        if size == 0:
            zero_byte += 1
            note("zero_byte", entry.url)
        if entry.bytes is not None and size != entry.bytes:
            byte_mismatches += 1
            note("byte_mismatches", entry.url)
        if verify_digests and storage.digest(key) != entry.sha256:
            hash_mismatches += 1
            note("hash_mismatches", entry.url)

    # Orphans: artifact keys the backend lists that no trusted record claims.
    # Enumeration covers exactly the artifact prefixes — manifests, checkpoints,
    # derived output and diagnostics are not under them and so are never
    # candidates. Whatever a backend has to hide to make its listing honest
    # (a macOS sidecar, an in-flight temporary) is the backend's business and
    # does not appear here.
    listed = _artifact_keys(storage)
    orphans = [key for key in listed if key not in claimed]
    for key in orphans[:3]:
        note("orphan_files", key)
    logical_bytes = sum(storage.stat(key).bytes for key in listed)

    result = Reconciliation(
        manifest_records=records,
        trusted=len(trusted),
        submissions=sum(1 for entry in trusted if entry.request_kind == "SUBMISSIONS"),
        filings=sum(1 for entry in trusted if entry.request_kind == "FILING"),
        failed=sum(1 for entry in latest.values() if entry.outcome is Outcome.FAILED),
        skipped=sum(1 for entry in latest.values() if entry.outcome is Outcome.SKIPPED),
        files_on_disk=len(listed),
        logical_bytes=logical_bytes,
        orphan_files=len(orphans),
        orphan_records=orphan_records,
        hash_mismatches=hash_mismatches,
        byte_mismatches=byte_mismatches,
        path_violations=path_violations,
        temporary_files=0,
        zero_byte=zero_byte,
        duplicate_urls=len(trusted) - len({entry.url for entry in trusted}),
        digests_verified=verify_digests,
        examples=examples,
    )
    log.info("evidence reconciliation", summary=result.summary())
    return result


def reconcile_subset(
    storage: EvidenceStorage,
    manifest: Manifest,
    urls: Collection[str],
    *,
    verify_digests: bool = True,
) -> Reconciliation:
    """Reconcile a named set of artifacts and the prefixes that hold them.

    `reconcile` walks the entire corpus. That is the right check to run at
    intervals and the wrong one to run after every batch of a long sweep: the
    walk is O(corpus), so running it per batch is O(batches x corpus), and Phase
    9L.6 measured it at 108,503 entries and 46 s per pass while still growing.
    That is the same quadratic shape Phase 9K.17 removed from manifest lookup.

    This checks the same things over a bounded slice. **Every artifact in `urls`
    is checked exactly as fully as the whole-corpus pass would check it** —
    existence, path-versus-URL, size, zero length, and digest. Nothing about the
    per-artifact guarantee is weakened; only the *scope* is.

    Orphan detection is scoped to the key prefixes the named artifacts live
    under — one filing's accession prefix, one submissions directory — and
    compares what the backend lists there against the **whole** trusted set
    rather than just this subset — so a file written by an earlier batch into the same directory is
    correctly recognised as claimed, and only genuinely unvouched-for bytes are
    reported. A stray file elsewhere in the corpus is invisible here by design,
    and is what the scheduled full walk exists to find.

    Args:
        storage: Where the artifacts live.
        manifest: The provenance log.
        urls: The artifacts to check — typically everything one batch wrote.
        verify_digests: Recompute SHA-256 from storage for each named artifact.

    Returns:
        The counts, scoped. `files_on_disk` counts only the listed prefixes.
    """
    wanted = set(urls)
    latest = manifest.latest_by_url()
    records = 0
    for _ in manifest.entries():
        records += 1

    trusted_all = [entry for entry in latest.values() if entry.is_stored]
    claimed = {entry.path or "" for entry in trusted_all}
    subset = [entry for entry in trusted_all if entry.url in wanted]

    examples: dict[str, list[str]] = {}

    def note(category: str, url: str) -> None:
        found = examples.setdefault(category, [])
        if len(found) < 3:
            found.append(url)

    orphan_records = 0
    hash_mismatches = 0
    byte_mismatches = 0
    path_violations = 0
    zero_byte = 0
    prefixes: set[str] = set()

    for entry in subset:
        key = entry.path or ""
        if not _key_is_safe(key):
            path_violations += 1
            note("path_violations", entry.url)
            continue
        prefixes.add(key.rpartition("/")[0])
        if not storage.exists(key):
            orphan_records += 1
            note("orphan_records", entry.url)
            continue
        if not _path_matches_url(entry):
            path_violations += 1
            note("path_violations", entry.url)
        size = storage.stat(key).bytes
        if size == 0:
            zero_byte += 1
            note("zero_byte", entry.url)
        if entry.bytes is not None and size != entry.bytes:
            byte_mismatches += 1
            note("byte_mismatches", entry.url)
        if verify_digests and storage.digest(key) != entry.sha256:
            hash_mismatches += 1
            note("hash_mismatches", entry.url)

    # Scoped listing: only the prefixes this batch touched, never the whole
    # namespace. That is the 9L.6R lesson: listing stays restricted to touched
    # prefixes even though manifest replay still covers the corpus. A prefix
    # outside the artifact plane has already been counted as a path violation.
    listed: list[str] = []
    for prefix in sorted(prefixes):
        if prefix.split("/", 1)[0] in ARTIFACT_PREFIXES:
            listed.extend(storage.list_prefix(prefix))
    orphans = [key for key in listed if key not in claimed]
    for key in orphans[:3]:
        note("orphan_files", key)
    logical_bytes = sum(storage.stat(key).bytes for key in listed)

    result = Reconciliation(
        manifest_records=records,
        trusted=len(subset),
        submissions=sum(1 for entry in subset if entry.request_kind == "SUBMISSIONS"),
        filings=sum(1 for entry in subset if entry.request_kind == "FILING"),
        failed=sum(1 for entry in latest.values() if entry.outcome is Outcome.FAILED),
        skipped=sum(1 for entry in latest.values() if entry.outcome is Outcome.SKIPPED),
        files_on_disk=len(listed),
        logical_bytes=logical_bytes,
        orphan_files=len(orphans),
        orphan_records=orphan_records,
        hash_mismatches=hash_mismatches,
        byte_mismatches=byte_mismatches,
        path_violations=path_violations,
        temporary_files=0,
        zero_byte=zero_byte,
        duplicate_urls=len(subset) - len({entry.url for entry in subset}),
        digests_verified=verify_digests,
        examples=examples,
    )
    log.info(
        "evidence reconciliation (subset)",
        artifacts=len(subset),
        prefixes=len(prefixes),
        summary=result.summary(),
    )
    return result


def new_urls_since(manifest: Manifest, records_before: int) -> set[str]:
    """URLs the log gained after the first `records_before` entries.

    The manifest is append-only and ordered, so "what did this chunk add" is a
    positional question and needs no extra bookkeeping.

    Args:
        manifest: The provenance log.
        records_before: How many records existed before the chunk started.

    Returns:
        Every URL appearing beyond that point.
    """
    return {entry.url for index, entry in enumerate(manifest.entries()) if index >= records_before}


def prior_sample(
    trusted: Sequence[Entry], exclude: set[str], *, minimum: int = 500, fraction: float = 0.05
) -> list[Entry]:
    """Pick a reproducible slice of the older corpus to re-verify.

    **Deterministic by construction.** The order comes from the artifacts' own
    digests, not from process state, set iteration or the clock, so the same
    corpus yields the same sample on any machine and any run — which is what
    makes "we checked these" a statement someone can audit rather than take on
    trust.

    Args:
        trusted: Every currently trusted artifact.
        exclude: URLs already verified in full this chunk.
        minimum: Floor on the sample size.
        fraction: Share of the prior corpus to take when that exceeds the floor.

    Returns:
        The chosen artifacts, fewer only when the prior corpus is smaller than
        the target.
    """
    prior = sorted(
        (e for e in trusted if e.url not in exclude), key=lambda e: (e.sha256 or "", e.url)
    )
    if not prior:
        return []
    target = min(len(prior), max(minimum, int(len(prior) * fraction)))
    # An even stride across the digest ordering, so the sample spreads over the
    # whole corpus rather than clustering in whatever was written first.
    step = len(prior) / target
    return [prior[min(len(prior) - 1, int(i * step))] for i in range(target)]


def verify_digests_of(storage: EvidenceStorage, entries: Sequence[Entry]) -> dict[str, int]:
    """Recompute SHA-256 for specific artifacts and report what disagreed.

    Args:
        storage: Where the artifacts live.
        entries: The artifacts to check.

    Returns:
        `{checked, hash_mismatches, byte_mismatches, missing}`.

    Raises:
        UnsafeStorageKeyError: If a record names an invalid artifact key. This
            digest-only summary has no path counter, so integrity errors propagate.
        StorageUnavailableError: If storage cannot reliably verify an artifact.
    """
    checked = mismatches = sizes = missing = 0
    for entry in entries:
        key = entry.path or ""
        validate_artifact_key(key)
        if not storage.exists(key):
            missing += 1
            continue
        checked += 1
        if storage.digest(key) != entry.sha256:
            mismatches += 1
            log.warning("digest mismatch", url=entry.url)
        if entry.bytes is not None and storage.stat(key).bytes != entry.bytes:
            sizes += 1
    return {
        "checked": checked,
        "hash_mismatches": mismatches,
        "byte_mismatches": sizes,
        "missing": missing,
    }


def _artifact_keys(storage: EvidenceStorage) -> list[str]:
    """Every key under the artifact prefixes, in listing order.

    A listing failure propagates. Turning it into an empty list would report
    "no orphans" for a namespace that could not be read — the one answer a
    reconciliation must never give.
    """
    keys: list[str] = []
    for prefix in ARTIFACT_PREFIXES:
        keys.extend(storage.list_prefix(prefix))
    return keys


def _key_is_safe(key: str) -> bool:
    """Whether a manifest path is a key the backend would accept.

    An unsafe record is a path violation only, never an orphan record. It is
    not looked up, because absence cannot be established for an invalid key.
    """
    try:
        validate_artifact_key(key)
    except UnsafeStorageKeyError:
        return False
    return True


def _path_matches_url(entry: Entry) -> bool:
    """Whether an artifact is stored where its URL says it should be.

    A filing lives under its padded CIK and dashless accession; a submissions
    index under `submissions/CIK<padded>.json`. Anything else means the path and
    the provenance disagree about what the bytes are.
    """
    if entry.path is None or entry.cik is None:
        return True
    if entry.request_kind == "SUBMISSIONS":
        return entry.path == f"submissions/CIK{entry.cik:010d}.json"
    if entry.request_kind == "FILING":
        plain = (entry.accession or "").replace("-", "")
        document = entry.url.rsplit("/", 1)[-1]
        return (
            f"/data/{entry.cik}/{plain}/" in entry.url
            and entry.path == f"filings/{entry.cik:010d}/{plain}/{document}"
        )
    return True
