"""Naming the database and the corpus an offline analysis is allowed to read.

Phase 9K.25 found the failure mode this module exists to close. `.env` points
`DATABASE_URL` at the primary database, but the production research universe
lives in a staging database, and a sweep run without an override selected a
*different* 5,769 identities — then converged, reconciled clean, and wrote a
COMPLETE checkpoint. Every gate passed. Nothing in the output said the wrong
population had been harvested.

The lesson is not "remember the override". It is that **a source this important
must be named explicitly and checked against something that could contradict
it.** So `bind_source` takes a path rather than reading configuration, opens it
read-only, and refuses to return unless the universe it holds is the size and
shape the caller said to expect. A caller that guesses wrong gets an exception,
never a plausible answer over the wrong data.

The corpus half is the same idea applied to files. `fingerprint_corpus` reduces
38,000 artifacts to a handful of digests, so "did the corpus change while we
analysed it" is one comparison rather than an act of faith.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.manifest import Manifest
from stock_screener.sec_evidence.sample import production_universe
from stock_screener.sec_evidence.sweep import selection_digest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from stock_screener.sec_evidence.control import EvidenceControlRoot

log = structlog.get_logger(__name__)

#: What the production universe held when Phase 9K.25 closed. A caller states
#: this rather than discovering it, so a database holding something else is a
#: failure instead of a smaller run.
PRODUCTION_IDENTITIES = 8919

#: The fingerprint file written beneath the control root's `derived/`. It
#: describes the corpus a derived index was built from, and is classified with
#: the index it describes: a run record, rebuildable from the manifest and
#: cheap to recompute. Recovery needs the manifest, the checkpoints and the
#: plans; it does not need this file.
FINGERPRINT_FILE = "corpus-fingerprint.json"


class BindingError(RuntimeError):
    """The named source is not the one the caller said to expect.

    Deliberately fatal. Every alternative — falling back to configuration,
    warning and continuing, trimming the expectation to fit — produces an
    analysis of the wrong data that looks exactly like an analysis of the right
    data.
    """


def read_only_url(path: Path | str) -> str:
    """Build a SQLite URL that cannot write, whatever the caller then does.

    Read-only is enforced by SQLite rather than by discipline: the frozen
    databases must come out of an analysis byte-identical, and the cheapest way
    to guarantee that is to make writing impossible at the connection.

    Args:
        path: The database file.

    Returns:
        A SQLAlchemy URL opening it in `mode=ro`.
    """
    return f"sqlite:///file:{Path(path)}?mode=ro&uri=true"


@dataclass(frozen=True)
class SourceBinding:
    """Which database an analysis read, proved rather than assumed.

    Attributes:
        path: The database file, resolved.
        sha256: Its digest at bind time.
        size: Its size in bytes.
        mtime: Its modification time, epoch seconds.
        universe_count: Identities in the production universe.
        universe_digest: SHA-256 over the whole ordered universe.
        first: The first identity, as a readable label.
        last: The last identity.
    """

    path: str
    sha256: str | None
    size: int
    mtime: int
    universe_count: int
    universe_digest: str
    first: str
    last: str

    def lines(self) -> tuple[str, ...]:
        """The binding, one line each."""
        return (
            f"database      : {self.path}",
            f"database sha  : {self.sha256 or 'not computed'}",
            f"database size : {self.size:,} bytes, mtime {self.mtime}",
            f"universe      : {self.universe_count} identities",
            f"universe sha  : {self.universe_digest}",
            f"first / last  : {self.first} .. {self.last}",
        )


def bind_source(
    session: Session,
    path: Path | str,
    *,
    expect_count: int = PRODUCTION_IDENTITIES,
    expect_digest: str | None = None,
    verify_digest: bool = False,
) -> SourceBinding:
    """Verify a session really reads the database the caller named.

    Args:
        session: A session already opened against `path`, read-only.
        path: The database file the caller intends to read.
        expect_count: Identities the production universe must hold. A different
            number means a different database, or a changed population; either
            way the analysis that follows would not mean what it claims.
        expect_digest: The universe digest to require, when the caller has one
            from an earlier run. Count alone cannot detect a population that
            changed without changing size.
        verify_digest: Hash the database file too. Off by default because the
            staging database is 22 GB and hashing it costs about a minute; the
            phase report turns it on.

    Returns:
        What was bound.

    Raises:
        BindingError: The file is missing, or the universe is not what was
            expected.
    """
    file = Path(path)
    if not file.is_file():
        raise BindingError(f"no database at {file}")

    universe = production_universe(session)
    digest = selection_digest(universe)
    if len(universe) != expect_count:
        raise BindingError(
            f"{file} holds {len(universe)} production identities, expected {expect_count} — "
            "this is very likely the wrong database, and an analysis of it would be "
            "internally consistent and completely wrong"
        )
    if expect_digest is not None and digest != expect_digest:
        raise BindingError(
            f"{file} holds {len(universe)} identities but digest {digest}, expected {expect_digest}"
        )
    if not universe:
        raise BindingError(f"{file} holds no production universe")

    def label(item: object) -> str:
        bucket = getattr(item, "bucket", "?")
        symbol = getattr(item, "symbol", "?")
        return f"{bucket} {symbol}"

    binding = SourceBinding(
        path=str(file.resolve()),
        sha256=_digest_file(file) if verify_digest else None,
        size=file.stat().st_size,
        mtime=int(file.stat().st_mtime),
        universe_count=len(universe),
        universe_digest=digest,
        first=label(universe[0]),
        last=label(universe[-1]),
    )
    log.info("source bound", path=binding.path, identities=binding.universe_count)
    return binding


@dataclass(frozen=True)
class CorpusFingerprint:
    """A reduction of the whole corpus to values that can be compared.

    Two digests rather than one, because they fail differently. `urls` changes
    only when the *set* of artifacts changes; `contents` changes when any
    artifact's bytes or size changes too. Reporting both says whether something
    was added or something was altered.

    Attributes:
        manifest_records: Lines in the log, history included.
        trusted: Artifacts the manifest currently vouches for.
        submissions: Trusted submissions catalogues.
        filings: Trusted filing documents.
        logical_bytes: Their combined recorded size.
        urls: SHA-256 over every trusted URL, sorted.
        contents: SHA-256 over every trusted `(url, sha256, bytes)`, sorted.
        manifest_sha256: The manifest file's own digest.
        taken_at: When this was measured.
    """

    manifest_records: int
    trusted: int
    submissions: int
    filings: int
    logical_bytes: int
    urls: str
    contents: str
    manifest_sha256: str
    taken_at: str

    def matches(self, other: CorpusFingerprint) -> bool:
        """Whether the corpus is unchanged, ignoring when each was taken."""
        mine = asdict(self)
        theirs = asdict(other)
        del mine["taken_at"], theirs["taken_at"]
        return mine == theirs

    def lines(self) -> tuple[str, ...]:
        """The fingerprint, one line each."""
        return (
            f"records/trusted: {self.manifest_records} / {self.trusted}"
            f" ({self.submissions} submissions, {self.filings} filings)",
            f"logical bytes  : {self.logical_bytes:,}",
            f"urls digest    : {self.urls}",
            f"contents digest: {self.contents}",
            f"manifest sha   : {self.manifest_sha256}",
        )


def fingerprint_corpus(
    control: EvidenceControlRoot, manifest: Manifest | None = None
) -> CorpusFingerprint:
    """Reduce the corpus to comparable digests, reading no artifact bytes.

    The artifacts' own recorded digests are what is hashed, not the files. The
    manifest has already been reconciled against the bytes in storage —
    repeating that here would cost twenty minutes to re-answer a question
    `reconcile` answers directly. Nothing here touches the artifact plane.

    Args:
        control: The control root holding the provenance log.
        manifest: The log, opened from the control root if not supplied.

    Returns:
        The fingerprint.
    """
    manifest = manifest or Manifest(control.manifest_path)
    log_file = manifest.path
    records = 0
    for _ in manifest.entries():
        records += 1
    trusted = [entry for entry in manifest.latest_by_url().values() if entry.is_stored]

    urls = hashlib.sha256()
    contents = hashlib.sha256()
    for entry in sorted(trusted, key=lambda e: e.url):
        urls.update(entry.url.encode() + b"\n")
        contents.update(f"{entry.url}\t{entry.sha256}\t{entry.bytes}\n".encode())

    fingerprint = CorpusFingerprint(
        manifest_records=records,
        trusted=len(trusted),
        submissions=sum(1 for e in trusted if e.request_kind == "SUBMISSIONS"),
        filings=sum(1 for e in trusted if e.request_kind == "FILING"),
        logical_bytes=sum(e.bytes or 0 for e in trusted),
        urls=urls.hexdigest(),
        contents=contents.hexdigest(),
        manifest_sha256=_digest_file(log_file),
        taken_at=datetime.now(UTC).isoformat(),
    )
    log.info("corpus fingerprint", trusted=fingerprint.trusted, urls=fingerprint.urls[:16])
    return fingerprint


def derived_root(control: EvidenceControlRoot) -> Path:
    """Return the derived-output directory, creating it beneath a verified root.

    Control plane, rebuildable half: the extraction index and its fingerprint
    are recomputed from the manifest and the artifacts, so losing this
    directory costs a rebuild and never evidence. Anchored to a root that has
    already been confirmed present, so an unmounted volume cannot cause a
    second empty analysis tree to appear on the boot disk.
    """
    path = control.derived
    path.mkdir(exist_ok=True)
    return path


def save_fingerprint(control: EvidenceControlRoot, fingerprint: CorpusFingerprint) -> Path:
    """Write a fingerprint under the control root's `derived/`, atomically."""
    path = derived_root(control) / FINGERPRINT_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(fingerprint), indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_fingerprint(control: EvidenceControlRoot) -> CorpusFingerprint | None:
    """Read the stored fingerprint, or None when none has been written."""
    path = derived_root(control) / FINGERPRINT_FILE
    if not path.is_file():
        return None
    try:
        return CorpusFingerprint(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        log.warning("unreadable fingerprint", path=str(path))
        return None


def _digest_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    """SHA-256 a file without reading it whole — the staging database is 22 GB."""
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            sha.update(block)
    return sha.hexdigest()
