"""Fetching a bounded set of SEC artifacts, resumably, and recording all of it.

The full corpus is expected to reach ~122,000 filings and ~359 GB, so this is
built around the two things that matter at that size: **never fetch the same
bytes twice**, and **stop where you were told to, cleanly enough to resume**.

## What "already have it" means

Not `the file exists`. A file that exists proves only that something was written
there once. An artifact counts as cached when the manifest records it as stored
*and* the bytes on disk still hash to the digest the manifest recorded. Anything
else is `INVALID_CACHE` and is fetched again — a corrupted or half-written file
is worse than a missing one, because a missing one is obviously missing.

An artifact on disk with **no manifest entry at all** is treated conservatively
as absent and re-fetched. It could be adopted by hashing it, but its digest
would then be evidence of nothing: there would be no record of what URL produced
it or when, and a provenance record invented after the fact is not provenance.
Re-fetching costs one request and yields a file that can be defended.

## An empty response is not an artifact

An HTTP 200 carrying no body is recorded `FAILED` and nothing is written. The
contract already says `FAILED` means "the request did not produce a usable
artifact", and no SEC document is legitimately empty. The alternative is worse
than it looks: a zero-byte file hashes to the digest of the empty string, that
digest matches on every subsequent run, and a momentary empty response would be
trusted permanently and never re-fetched.

## Budgets

`max_requests` and `max_bytes` are checked *before* each request, and a
`Content-Length` the server has already declared is checked against the
remaining byte budget before the body is read. Reaching either limit ends the
run with `SKIPPED` entries for everything untouched, which is what makes the
next run a continuation rather than a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from api_clients.errors import ProviderBudgetError, ProviderError
from stock_screener.sec_evidence.backend import ArtifactNotFoundError
from stock_screener.sec_evidence.manifest import (
    Entry,
    Manifest,
    ManifestState,
    Outcome,
)
from stock_screener.sec_evidence.storage import (
    EvidenceIntegrityError,
    RequestKind,
    filing_key,
    fulltext_key,
    submissions_key,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from api_clients.sec_documents import FetchedDocument
    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.storage import EvidenceRoot

log = structlog.get_logger(__name__)

#: A byte budget of this size means "no byte budget".
UNLIMITED = 0


class DocumentSource(Protocol):
    """Anything that can fetch one SEC document and describe the response.

    `api_clients.SecDocuments` is the real one. Stating the requirement as a
    protocol rather than that class is what lets a test drive the harvester with
    canned bytes and no network — the harvester needs one method, and depending
    on the whole client would make the seam narrower than the need.
    """

    def fetch(self, url: str) -> FetchedDocument:
        """Fetch one document and return it with its provenance."""
        ...


@dataclass(frozen=True)
class Target:
    """One artifact a harvest intends to obtain.

    Attributes:
        url: Where to get it.
        kind: Which part of the layout it belongs to.
        cik: Filer number, where known.
        accession: Accession number, where known.
        document: File name within the filing, for `FILING` targets.
        form: Form type, where known.
        filing_date: Filing date, where known.
        query: The search string, for `FULLTEXT` targets.
    """

    url: str
    kind: RequestKind
    cik: int | None = None
    accession: str | None = None
    document: str | None = None
    form: str | None = None
    filing_date: str | None = None
    query: str | None = None


@dataclass
class Budget:
    """How much a run may spend before it stops.

    **The request count is of actual HTTP attempts, not logical targets.** An
    earlier version incremented it once per target inside the harvest loop,
    which undercounted twice over: a target that retried three times counted
    once, and the CIK resolver's fetch of the SEC ticker file — a request the
    harvest causes but does not itself make — was not counted at all. A run
    given `max_requests=15` issued 16. Budget now implements `RequestMeter` and
    is passed into the HTTP clients, so it is consulted immediately before every
    attempt reaches the network and `max_requests` means what it says.

    Attributes:
        max_requests: HTTP attempts allowed. Zero means unlimited.
        max_bytes: Bytes allowed to be stored. Zero means unlimited.
        requests_made: Attempts recorded so far, by the meter.
        bytes_written: Bytes stored so far.
    """

    max_requests: int = 0
    max_bytes: int = 0
    requests_made: int = 0
    bytes_written: int = 0

    @property
    def requests_remaining(self) -> int | None:
        """Requests left, or None when unlimited."""
        return None if self.max_requests == UNLIMITED else self.max_requests - self.requests_made

    @property
    def bytes_remaining(self) -> int | None:
        """Bytes left, or None when unlimited."""
        return None if self.max_bytes == UNLIMITED else self.max_bytes - self.bytes_written

    @property
    def exhausted(self) -> bool:
        """Whether another request may be started."""
        remaining = self.requests_remaining
        if remaining is not None and remaining <= 0:
            return True
        left = self.bytes_remaining
        return left is not None and left <= 0

    def would_exceed(self, size: int) -> bool:
        """Whether storing `size` more bytes would cross the byte budget."""
        left = self.bytes_remaining
        return left is not None and size > left

    def allow(self) -> bool:
        """Whether another HTTP attempt may be made. The `RequestMeter` contract."""
        remaining = self.requests_remaining
        return remaining is None or remaining > 0

    def record(self) -> None:
        """Record one HTTP attempt. The `RequestMeter` contract."""
        self.requests_made += 1

    def spend(self, *, stored: int = 0) -> None:
        """Record bytes stored. Requests are recorded by `record`."""
        self.bytes_written += stored


@dataclass
class HarvestReport:
    """What one run did.

    Attributes:
        by_outcome: How many targets reached each outcome.
        bytes_written: Bytes stored this run.
        requests_made: Network requests issued this run.
        stopped_on_budget: Whether a limit ended the run early.
        failures: `(url, reason)` for everything that failed.
    """

    by_outcome: dict[str, int] = field(default_factory=dict)
    bytes_written: int = 0
    requests_made: int = 0
    stopped_on_budget: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)

    def record(self, outcome: Outcome) -> None:
        """Count one target."""
        self.by_outcome[str(outcome)] = self.by_outcome.get(str(outcome), 0) + 1

    def summary(self) -> str:
        """One line for a report."""
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.by_outcome.items()))
        stopped = " (stopped on budget)" if self.stopped_on_budget else ""
        return (
            f"{sum(self.by_outcome.values())} targets — {parts}; "
            f"{self.requests_made} requests, {self.bytes_written} bytes{stopped}"
        )


def harvest(
    targets: Sequence[Target],
    root: EvidenceRoot,
    documents: DocumentSource | None,
    *,
    budget: Budget | None = None,
    dry_run: bool = False,
) -> HarvestReport:
    """Obtain each target into a legacy colocated evidence root.

    The root supplies both halves of what a harvest needs — the artifact store
    and, beside it, the manifest that vouches for it. That is the pre-B2.5
    layout and this wrapper exists for it. `harvest_artifacts` is the one that
    does the work and takes the two halves separately; production wiring
    hands it the artifact backend and a manifest opened from the control root,
    so that losing the artifact volume cannot take the provenance with it.

    Args:
        targets: What to fetch, in order.
        root: A verified evidence root.
        documents: The SEC client. May be None only for a dry run.
        budget: Spending limits. Unlimited when omitted.
        dry_run: Plan only — no request is issued and nothing is written, the
            manifest included.

    Returns:
        What happened.

    Raises:
        ValueError: If a real run is asked for without a client.
    """
    return harvest_artifacts(
        targets,
        root,
        Manifest(root.manifest_path),
        documents,
        budget=budget,
        dry_run=dry_run,
    )


def harvest_artifacts(
    targets: Sequence[Target],
    storage: EvidenceStorage,
    manifest: Manifest,
    documents: DocumentSource | None,
    *,
    budget: Budget | None = None,
    dry_run: bool = False,
    verify_written: bool = False,
    state: ManifestState | None = None,
    refetch_invalid: bool = True,
) -> HarvestReport:
    """Obtain each target, skipping what is already provably held.

    Artifact bytes go to `storage`; provenance goes to `manifest`. They are
    separate arguments because they are separate responsibilities — a backend
    never appends the manifest, and the manifest never decides where bytes
    live. Nothing in here holds a filesystem path.

    Args:
        targets: What to fetch, in order.
        storage: Where artifact bytes are stored and verified.
        manifest: The provenance log that makes an artifact trusted.
        documents: The SEC client. May be None only for a dry run.
        budget: Spending limits. Unlimited when omitted.
        dry_run: Plan only — no request is issued and nothing is written, the
            manifest included.
        verify_written: Read every newly stored artifact back and hash it
            before its manifest entry is appended. Backend-neutral: a local
            directory re-reads a file, a bucket downloads the object. Costs
            one read per new artifact and buys the guarantee that no trusted
            row describes bytes the backend does not hold.
        state: A replayed manifest to work from, for a caller that harvests
            one target at a time and would otherwise replay the whole log
            per target. Every entry this call appends is recorded into it,
            so it stays the truth for the next call. Replayed here when
            omitted.
        refetch_invalid: What to do with a trusted artifact whose stored
            bytes no longer hash to its record. The historical answer is to
            fetch it again and overwrite (`INVALID_CACHE`, then
            `DOWNLOADED`). `False` reports `INVALID_CACHE` and leaves the
            artifact exactly as found, for a caller whose policy is to stop
            and look rather than repair.

    Returns:
        What happened.

    Raises:
        ValueError: If a real run is asked for without a client.
        EvidenceIntegrityError: If `verify_written` finds stored bytes that do not
            hash to the fetched bytes. The run stops; nothing was recorded.
    """
    if documents is None and not dry_run:
        message = "a harvest that is not a dry run needs a SEC client"
        raise ValueError(message)

    limits = budget or Budget()
    if state is None:
        state = manifest.replay()
    known = state.latest
    report = HarvestReport()

    for target in targets:
        key = _key_for(target)
        outcome = _classify(target, key, storage, known)

        if dry_run:
            report.record(Outcome.CACHE_HIT if outcome is Outcome.CACHE_HIT else Outcome.SKIPPED)
            continue

        if outcome is Outcome.CACHE_HIT:
            report.record(Outcome.CACHE_HIT)
            continue

        if outcome is Outcome.INVALID_CACHE and not refetch_invalid:
            log.error("trusted artifact no longer reproduces its digest", url=target.url, key=key)
            report.record(Outcome.INVALID_CACHE)
            report.failures.append(
                (target.url, "INVALID_CACHE: stored bytes differ from the record")
            )
            continue

        if limits.exhausted:
            report.stopped_on_budget = True
            report.record(Outcome.SKIPPED)
            continue

        if documents is None:  # pragma: no cover — guarded at entry
            message = "a harvest that is not a dry run needs a SEC client"
            raise ValueError(message)
        entry = _fetch_one(
            target, key, storage, documents, limits, manifest, state, verify_written=verify_written
        )
        # A re-fetch caused by a corrupt cache is reported as `INVALID_CACHE`
        # rather than `DOWNLOADED`: the bytes were downloaded either way, and
        # what the operator needs to see is that something on disk was wrong.
        if entry.outcome is Outcome.SKIPPED:
            report.stopped_on_budget = True
        report.record(
            Outcome.INVALID_CACHE
            if outcome is Outcome.INVALID_CACHE and entry.outcome is Outcome.DOWNLOADED
            else entry.outcome
        )
        if entry.outcome is Outcome.FAILED:
            report.failures.append((target.url, entry.error or "unknown"))

    report.bytes_written = limits.bytes_written
    report.requests_made = limits.requests_made
    log.info("sec evidence harvest", summary=report.summary())
    return report


def _fetch_one(
    target: Target,
    key: str,
    storage: EvidenceStorage,
    documents: DocumentSource,
    limits: Budget,
    manifest: Manifest,
    state: ManifestState,
    *,
    verify_written: bool = False,
) -> Entry:
    """Fetch, store and record one target. Never raises for a request failure.

    Raises:
        EvidenceIntegrityError: If `verify_written` is set and the bytes read back
            from storage do not hash to the bytes that were sent. That is an
            integrity fault in the backend, not a request failure, and it stops
            the run before any manifest entry exists for the artifact.
    """
    attempt = state.next_attempt(target.url)
    # A metered client records each network attempt itself, retries included, so
    # `requests_made` moves during the call. A client that is *not* metered — a
    # fake, or a future source someone forgets to wire — would otherwise spend
    # nothing and run past the ceiling unchecked. Comparing the counter across
    # the call closes that: the true count wins where it exists, and one attempt
    # is assumed where it does not. Never both, so nothing is counted twice.
    before = limits.requests_made
    try:
        fetched = documents.fetch(target.url)
    except ProviderBudgetError:
        _ensure_counted(limits, before)
        # The meter refused mid-flight. Nothing went wrong with the network, so
        # this is a stop rather than a failure and the next run re-fetches it.
        entry = Entry(
            url=target.url,
            outcome=Outcome.SKIPPED,
            request_kind=str(target.kind),
            cik=target.cik,
            accession=target.accession,
            error="the HTTP request budget was spent",
            attempt=attempt,
        )
        _persist(manifest, state, entry)
        return entry
    except ProviderError as exc:
        _ensure_counted(limits, before)
        entry = Entry(
            url=target.url,
            outcome=Outcome.FAILED,
            request_kind=str(target.kind),
            cik=target.cik,
            accession=target.accession,
            form=target.form,
            filing_date=target.filing_date,
            error=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
        )
        _persist(manifest, state, entry)
        return entry

    _ensure_counted(limits, before)

    if fetched.bytes_received == 0:
        # `FAILED` is documented as "the request did not produce a usable
        # artifact", and an empty body is precisely that: no SEC submissions
        # index or filing document is legitimately zero bytes. Storing it would
        # create an artifact whose digest is the digest of nothing, which then
        # verifies forever and is never re-fetched — a transient empty response
        # would silently become permanent. Refused rather than trusted.
        entry = Entry(
            url=target.url,
            outcome=Outcome.FAILED,
            status=fetched.status,
            request_kind=str(target.kind),
            cik=target.cik,
            accession=target.accession,
            form=target.form,
            filing_date=target.filing_date,
            error=f"HTTP {fetched.status} returned an empty body",
            attempt=attempt,
        )
        _persist(manifest, state, entry)
        return entry

    if limits.would_exceed(fetched.bytes_received):
        # The bytes are in hand but storing them would cross the budget, so they
        # are dropped rather than written. Recorded as skipped so the next run
        # fetches it again rather than believing it was kept.
        entry = Entry(
            url=target.url,
            outcome=Outcome.SKIPPED,
            status=fetched.status,
            request_kind=str(target.kind),
            error=f"storing {fetched.bytes_received} bytes would exceed the byte budget",
            attempt=attempt,
        )
        _persist(manifest, state, entry)
        return entry

    # Trust ordering, unchanged: the bytes are stored and vouched for by the
    # backend first; only then is the manifest entry built and appended. With
    # `verify_written` the caller asks for one more step in between — the
    # stored bytes are read back and hashed, independently of whatever the
    # backend's own write verification did — so that a backend which accepted
    # the write and then holds something else can never become a trusted row.
    stored = storage.write_bytes(key, fetched.body)
    if verify_written:
        readback = storage.digest(key)
        if readback != fetched.sha256:
            log.error(
                "stored bytes do not hash to the fetched bytes; not recording",
                url=target.url,
                key=key,
            )
            raise EvidenceIntegrityError(
                f"{key!r}: the backend holds bytes hashing to {readback[:16]}…, "
                f"not the {fetched.sha256[:16]}… that were fetched; no manifest entry written"
            )
    limits.spend(stored=stored.bytes)
    entry = Entry(
        url=target.url,
        outcome=Outcome.DOWNLOADED,
        status=fetched.status,
        sha256=fetched.sha256,
        bytes=stored.bytes,
        retrieved_at=fetched.retrieved_at.isoformat(),
        path=stored.key,
        request_kind=str(target.kind),
        content_type=fetched.content_type,
        etag=fetched.etag,
        last_modified=fetched.last_modified,
        cik=target.cik,
        accession=target.accession,
        form=target.form,
        filing_date=target.filing_date,
        attempt=attempt,
    )
    _persist(manifest, state, entry)
    return entry


def _persist(manifest: Manifest, state: ManifestState, entry: Entry) -> None:
    """Append one entry, then let the run see it.

    The order is the contract: the artifact is already on disk, `append` makes
    its provenance durable and raises if it cannot, and only then does the entry
    become visible to later targets in this run. Nothing is trusted in memory
    that the manifest does not already record.
    """
    manifest.append(entry)
    state.record(entry)


def _ensure_counted(limits: Budget, before: int) -> None:
    """Charge one attempt when the client did not charge for itself."""
    if limits.requests_made == before:
        limits.record()


def _classify(
    target: Target, key: str, storage: EvidenceStorage, known: dict[str, Entry]
) -> Outcome:
    """Decide whether a target is already held, by checking the bytes.

    Returns `CACHE_HIT` only when the manifest claims the artifact is stored and
    the backend still hashes the stored bytes to the recorded digest.
    """
    entry = known.get(target.url)
    if entry is None or not entry.is_stored or entry.sha256 is None:
        return Outcome.DOWNLOADED
    try:
        actual = storage.digest(key)
    except ArtifactNotFoundError:
        # The manifest says it is here and it is not. Something removed it, or
        # the store the manifest describes is not the one reachable now.
        log.warning("manifest names an artifact that is not there", url=target.url)
        return Outcome.INVALID_CACHE
    if actual != entry.sha256:
        log.warning("stored artifact does not match its digest", url=target.url)
        return Outcome.INVALID_CACHE
    return Outcome.CACHE_HIT


def key_for(target: Target) -> str:
    """The storage key one target's bytes belong under.

    Derived from the target alone — no root, no backend — so the same target
    yields the same key wherever the artifact ends up living. Public so a
    caller can ask the backend about a target *before* harvesting it.
    """
    return _key_for(target)


def _key_for(target: Target) -> str:
    """See `key_for`."""
    if target.kind is RequestKind.SUBMISSIONS:
        if target.cik is None:
            message = "a submissions target needs a CIK"
            raise ValueError(message)
        return submissions_key(target.cik)
    if target.kind is RequestKind.FULLTEXT:
        if target.query is None:
            message = "a full-text target needs a query"
            raise ValueError(message)
        return fulltext_key(target.query)
    if target.cik is None or target.accession is None or target.document is None:
        message = "a filing target needs a CIK, an accession and a document name"
        raise ValueError(message)
    return filing_key(target.cik, target.accession, target.document)
