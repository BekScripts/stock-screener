"""Where a SEC artifact lives on disk, and how it gets there whole.

Two rules shape every function here, and both exist because the corpus lives on
an external drive.

**The root is never created.** `SEC_EVIDENCE_DIR` names a directory on a volume
that may not be mounted, and `mkdir(parents=True)` on an absent volume builds
the whole chain on the boot disk instead — silently, with no error, filling the
machine the external drive was chosen to spare. So the root must already exist;
only the known subdirectories beneath a *verified* root are created here.

**An artifact is whole or it is absent.** Bytes go to a temporary file in the
destination directory, are flushed and fsynced, and are renamed into place. A
rename within one filesystem is atomic, so a reader sees the complete file or no
file — never the half a crashed download left behind.

## The layout

This is the **legacy colocated layout** — artifacts and the control plane on
one root. Since B2.5 the control plane (`manifests/`, `checkpoints/`,
`derived/`, plans) can live under `SEC_CONTROL_DIR` on another device instead;
see `stock_screener.sec_evidence.control`. The artifact half is unchanged.

    $SEC_EVIDENCE_DIR/
    ├── filings/<cik-padded>/<accession-no-dashes>/<document>
    ├── submissions/CIK<cik-padded>.json
    ├── fulltext/<query-digest>.json
    └── manifests/evidence.jsonl

The CIK is zero-padded to ten digits and the accession has its dashes stripped,
which is the SEC's own archive form — the path a person can read back to a URL.
This convention is new in Phase 9K.13; nothing in the repository stored SEC
documents on disk before it.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from stock_screener.evidence import resolve_sec_evidence_dir
from stock_screener.sec_evidence.control import CONTROL_SUBDIRECTORIES, EvidenceControlRoot

if TYPE_CHECKING:
    from collections.abc import Iterator

    from stock_screener.config import Settings
    from stock_screener.sec_evidence.backend import (
        ArtifactStat,
        LocalEvidenceStorage,
        StoredArtifact,
    )

log = structlog.get_logger(__name__)

#: The prefixes that hold source evidence — the *artifact plane*. This tuple is
#: the definition of the artifact namespace: reconciliation enumerates exactly
#: these and nothing else, which is how manifests, checkpoints, derived output
#: and diagnostics stay out of the orphan count by construction rather than by
#: exclusion list. Frozen in B2.4; no other artifact kind exists.
FILINGS = "filings"
SUBMISSIONS = "submissions"
FULLTEXT = "fulltext"
ARTIFACT_PREFIXES = (FILINGS, SUBMISSIONS, FULLTEXT)

#: The subdirectories a verified *legacy* root carries: the three artifact
#: prefixes plus the control-plane directories that the colocated layout kept
#: beside them. A root configured through `SEC_CONTROL_DIR` carries only the
#: control half — see `stock_screener.sec_evidence.control`.
SUBDIRECTORIES = (FILINGS, SUBMISSIONS, FULLTEXT, *CONTROL_SUBDIRECTORIES)

#: Where storage qualification writes its test files. Not an artifact prefix: a
#: sustained-load test has to write real bytes to the real volume to mean
#: anything, and those bytes are emphatically not evidence. Keeping them under
#: the artifact root — rather than somewhere the volume check does not cover —
#: is what makes the test exercise the path the corpus actually uses.
DIAGNOSTICS = "diagnostics"


class RequestKind(StrEnum):
    """What an artifact is, which decides where it is stored."""

    FILING = "FILING"
    """One document inside a filing, under `filings/`."""

    SUBMISSIONS = "SUBMISSIONS"
    """A filer's submissions index, under `submissions/`."""

    FULLTEXT = "FULLTEXT"
    """A full-text search response, under `fulltext/`."""


@dataclass(frozen=True)
class EvidenceRoot(EvidenceControlRoot):
    """The legacy layout: one local directory that is both planes at once.

    An `EvidenceRoot` *is* an `EvidenceControlRoot` — the manifest, the
    checkpoints and the derived output sit beneath it — and it *is* an
    `EvidenceStorage`, structurally, by delegating to the local backend
    anchored at the same path. That is exactly the arrangement in which a
    single failing drive took the corpus and its recovery metadata together,
    and it survives here for one reason: every corpus harvested before B2.5 is
    laid out this way, and nothing moves files automatically.

    Production wiring opens a control root of its own through
    `open_control_root` and hands artifact-plane functions this object (or its
    `.storage`) and control-plane functions that one. Only when
    `SEC_CONTROL_DIR` is unset does this object serve as both, and it says so
    through `colocated`.

    Only constructed by `open_evidence_root`, which is what makes "this exists
    and is a directory" true for every instance rather than hoped for.

    Attributes:
        path: The verified root.
    """

    @property
    def colocated(self) -> bool:
        """True: this root holds the control plane beside the artifacts."""
        return True

    @property
    def filings(self) -> Path:
        """Where filing documents live. Local layout only; prefer keys."""
        return self.path / FILINGS

    @property
    def submissions(self) -> Path:
        """Where submissions indexes live. Local layout only; prefer keys."""
        return self.path / SUBMISSIONS

    @property
    def fulltext(self) -> Path:
        """Where full-text responses live. Local layout only; prefer keys."""
        return self.path / FULLTEXT

    # ------------------------------------------------------------------
    # An EvidenceRoot IS an EvidenceStorage.
    #
    # Structurally, not by inheritance: it exposes the six contract operations
    # by delegating to the local backend anchored at its path. That is what lets
    # every artifact-plane function take `storage: EvidenceStorage` and still
    # accept a root from the many callers that hold one — while a test can hand
    # the same function a memory backend and prove nothing below the seam ever
    # needed a directory. The root keeps its control-plane duties (where the
    # manifest and checkpoints live) alongside; those are not storage-contract
    # concerns and are addressed in a later phase.
    # ------------------------------------------------------------------

    @property
    def storage(self) -> LocalEvidenceStorage:
        """The concrete local backend for this root, for callers that need it."""
        from stock_screener.sec_evidence.backend import LocalEvidenceStorage

        return LocalEvidenceStorage(self.path)

    def exists(self, key: str) -> bool:
        """See `EvidenceStorage.exists`."""
        return self.storage.exists(key)

    def stat(self, key: str) -> ArtifactStat:
        """See `EvidenceStorage.stat`."""
        return self.storage.stat(key)

    def read_bytes(self, key: str) -> bytes:
        """See `EvidenceStorage.read_bytes`."""
        return self.storage.read_bytes(key)

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """See `EvidenceStorage.write_bytes`."""
        return self.storage.write_bytes(key, data)

    def digest(self, key: str) -> str:
        """See `EvidenceStorage.digest`."""
        return self.storage.digest(key)

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """See `EvidenceStorage.list_prefix`."""
        return self.storage.list_prefix(prefix)

    def free_bytes(self) -> int:
        """Free space on the volume holding this root. See `LocalEvidenceStorage`."""
        return self.storage.free_bytes()

    def filing_path(self, cik: int, accession: str, document: str) -> Path:
        """Return where one filing document belongs on this local root.

        Deprecated compatibility helper: no production code calls it, runtime
        artifact flow uses `filing_key` through the storage contract, and it is
        kept only for tests that inspect the legacy layout directly.

        Raises:
            ValueError: If the document name would escape the filing directory.
        """
        return self.path / filing_key(cik, accession, document)

    def submissions_path(self, cik: int) -> Path:
        """Return where one filer's submissions index belongs on this local root.

        Deprecated compatibility helper — see `filing_path`.
        """
        return self.path / submissions_key(cik)

    def fulltext_path(self, query: str) -> Path:
        """Return where one full-text response belongs on this local root.

        Deprecated compatibility helper — see `filing_path`.
        """
        return self.path / fulltext_key(query)


def filing_key(cik: int, accession: str, document: str) -> str:
    """The storage key for one filing document, independent of any backend.

    Args:
        cik: The filer number, unpadded.
        accession: The accession number, dashed or not.
        document: The file name within the filing.

    Returns:
        `filings/<cik 10-digit>/<accession without dashes>/<document>`.

    Raises:
        ValueError: If the document name would escape the filing directory.
    """
    return f"{FILINGS}/{cik:010d}/{accession.replace('-', '')}/{_safe(document)}"


def submissions_key(cik: int) -> str:
    """The storage key for one filer's submissions index."""
    return f"{SUBMISSIONS}/CIK{cik:010d}.json"


def fulltext_key(query: str) -> str:
    """The storage key for one full-text response.

    The query is digested rather than spelled out: it can carry slashes,
    quotes and arbitrary length, none of which survive as a key segment.
    """
    return f"{FULLTEXT}/{hashlib.sha256(query.encode()).hexdigest()}.json"


def open_evidence_root(settings: Settings) -> EvidenceRoot:
    """Verify the configured artifact root and prepare its subdirectories.

    The root itself must already exist — see the module docstring. Its known
    subdirectories are created, which is safe precisely because the root being
    present proves the volume is mounted. The control-plane directories among
    them are for the legacy colocated layout; a root that will be paired with
    a separate `SEC_CONTROL_DIR` simply leaves them empty.

    Args:
        settings: Supplies `SEC_EVIDENCE_DIR`.

    Returns:
        The verified root.

    Raises:
        EvidenceUnavailableError: If the setting is unset, or the path is
            missing or not a directory. An unmounted drive lands here.
    """
    root = resolve_sec_evidence_dir(settings, allow_empty=True)
    for name in SUBDIRECTORIES:
        (root / name).mkdir(exist_ok=True)
    log.debug("evidence root ready", root=str(root))
    return EvidenceRoot(path=root)


def store_atomically(destination: Path, payload: bytes, *, root: Path) -> int:
    """Write bytes to their final location, atomically, or not at all.

    **Anchored to a verified root on purpose.** An earlier draft created any
    missing parent by walking up to the nearest directory that exists, which is
    no protection at all: `/Volumes` always exists, so an unmounted drive would
    have had `/Volumes/BEK/stock-screener-data/sec/...` built underneath it on
    the boot disk — the exact failure this module is for. Directories are
    therefore only ever created *below* a root that has already been verified to
    exist, and a destination outside that root is refused.

    Args:
        destination: Where the artifact belongs. Must be inside `root`.
        payload: Its bytes, complete.
        root: The verified evidence root, from `open_evidence_root`.

    Returns:
        How many bytes were written.

    Raises:
        EvidenceWriteError: If the root is no longer a directory — the drive was
            unmounted mid-run — or the destination lies outside it.
    """
    if not root.is_dir():
        message = (
            f"the evidence root {root} is no longer a directory. On an external drive this "
            "means the volume was unmounted; writing would put evidence on the boot disk."
        )
        raise EvidenceWriteError(message)
    if not destination.is_relative_to(root):
        message = f"{destination} is outside the evidence root {root}"
        raise EvidenceWriteError(message)

    directory = destination.parent
    # Safe now, and only now: every level being created sits under a root that
    # was just confirmed present, so no ancestor of it can be invented.
    directory.mkdir(parents=True, exist_ok=True)

    handle, temporary_name = tempfile.mkstemp(dir=directory, prefix=f".{destination.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(payload)


def digest_of(path: Path) -> str | None:
    """Return a stored file's SHA-256, or None when it is not there."""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class StorageUnavailableError(RuntimeError):
    """Storage cannot reliably answer or complete an operation.

    Includes an unavailable root, I/O failure, or a future backend's auth,
    network or service failure. Never interpreted as an absent artifact.
    """


class EvidenceWriteError(StorageUnavailableError):
    """An artifact write did not complete successfully; no provenance is earned."""


class EvidenceIntegrityError(EvidenceWriteError):
    """The backend holds bytes that do not match what was sent.

    Not a transport fault: the write went through and the bucket answered,
    but what it answered with is wrong. A caller that would retry a
    transport fault must not retry this one — retrying means overwriting.
    """


def _safe(name: str) -> str:
    """Return a document name that cannot escape its directory.

    SEC document names come from a remote index. A name containing a separator
    or `..` would place the artifact somewhere the layout does not describe, so
    it is refused rather than sanitised — a quietly renamed file would no longer
    be the document the manifest claims.

    Raises:
        ValueError: If the name is empty or contains a path component.
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        message = f"{name!r} is not a usable SEC document name"
        raise ValueError(message)
    return name
