"""What the evidence layer asks of storage, and the one place that answers it.

The harvester, the parser and the reconciliation each need a handful of things
from wherever artifacts live: put bytes under a key, get them back, learn how
big they are, recompute their digest, and enumerate what sits under a prefix.
Until now every one of those was a `Path` operation against a directory on an
external drive, and when that drive failed, so did the ability to say anything
about the evidence at all.

`EvidenceStorage` is the contract. It is deliberately small — six operations,
none of which mention a filesystem, a bucket, or a mount point — because
anything larger would be designed around a backend we have not written yet.
`LocalEvidenceStorage` is the only implementation today, and it is the existing
writer behind a new door: same temp-then-rename write, same root anchoring,
same SHA-256, same layout.

## A key is not a path

A storage key is `filings/0000320193/000032019324000123/aapl-20240928.htm`. It
uses forward slashes, it is relative, and it means the same thing under any
backend. Only the local backend turns it into `<root>/filings/...`, and nothing
above the backend ever sees that absolute path. The manifest already records
keys in exactly this shape — Phase B2.0 confirmed no absolute path is persisted
anywhere — so the abstraction changes what callers *hold*, not what is written.

## Keys are refused, not repaired

A key that could escape the namespace — `../`, an absolute path, a drive letter,
a dot segment — is rejected with `UnsafeStorageKeyError`. It is never
normalised into something acceptable: a quietly rewritten key would no longer
name the artifact the manifest claims, which is the mis-attribution this whole
layer exists to prevent. This holds regardless of backend; object stores are
just as happy to store a key called `../etc/passwd` as a filesystem is.

## Storage does not decide trust

`write_bytes` returns when the backend can vouch for the bytes. It does not
append the manifest, and it must not: provenance is the manifest's job, and the
trusted-write ordering — store, verify, *then* record — belongs to the caller
that holds both. A backend that recorded its own success would be a backend
that could never be caught lying.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat as stat_mode
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from stock_screener.sec_evidence.storage import (
    ARTIFACT_PREFIXES,
    EvidenceWriteError,
    StorageUnavailableError,
    store_atomically,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

log = structlog.get_logger(__name__)

__all__ = [
    "ArtifactNotFoundError",
    "ArtifactStat",
    "EvidenceStorage",
    "LocalEvidenceStorage",
    "StorageUnavailableError",
    "StoredArtifact",
    "UnsafeStorageKeyError",
    "validate_artifact_key",
    "validate_key",
]


class ArtifactNotFoundError(LookupError):
    """The backend successfully established that the object is absent."""


class UnsafeStorageKeyError(ValueError):
    """A key that could leave the evidence namespace. Never normalised."""


@dataclass(frozen=True)
class ArtifactStat:
    """What a backend knows about a stored artifact without reading it.

    Attributes:
        key: The artifact's storage key.
        bytes: Its size.
    """

    key: str
    bytes: int


@dataclass(frozen=True)
class StoredArtifact:
    """What a successful write vouches for.

    Attributes:
        key: Where the bytes now live.
        bytes: How many were stored.
        sha256: Their digest, computed from the bytes that were written.
    """

    key: str
    bytes: int
    sha256: str


class EvidenceStorage(Protocol):
    """The six things the evidence layer asks of wherever artifacts live.

    Every operation takes a key — never a path, never a URL — and every
    implementation must reject an unsafe key before touching anything.

    `exists() == False` means only definite absence. Read, stat and digest
    raise `ArtifactNotFoundError` for that same state. Backend uncertainty,
    including I/O, authentication, network and service failures, raises
    `StorageUnavailableError`; a failed write raises `EvidenceWriteError`.
    Invalid logical keys raise `UnsafeStorageKeyError` on every operation.
    Listing failures propagate, including failures after a partial listing.

    ETag is never evidence SHA-256. Neither `StoredArtifact.sha256`, `digest`
    nor manifest SHA-256 may be populated from an object-store ETag.
    """

    def exists(self, key: str) -> bool:
        """Return false only for definite absence; raise on backend uncertainty."""
        ...

    def stat(self, key: str) -> ArtifactStat:
        """Size of a stored artifact.

        Raises:
            ArtifactNotFoundError: If nothing is stored under the key.
            StorageUnavailableError: If storage cannot reliably answer.
        """
        ...

    def read_bytes(self, key: str) -> bytes:
        """The complete artifact.

        Raises:
            ArtifactNotFoundError: If nothing is stored under the key.
            StorageUnavailableError: If storage cannot reliably answer.
        """
        ...

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """Store a complete artifact, or store nothing.

        Returns only when the backend can vouch for the bytes. Does not touch
        the manifest.

        Raises:
            EvidenceWriteError: If the write did not complete successfully.
        """
        ...

    def digest(self, key: str) -> str:
        """SHA-256 of the stored bytes, recomputed from storage.

        Raises:
            ArtifactNotFoundError: If nothing is stored under the key.
            StorageUnavailableError: If storage cannot reliably answer.
        """
        ...

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """Every stored key beneath a prefix, in ascending lexicographic order.

        The prefix `""` enumerates everything. Ordering is part of the contract
        so a caller comparing two listings can rely on it.

        Raises:
            StorageUnavailableError: If enumeration cannot complete reliably.
        """
        ...


def validate_key(key: str) -> str:
    """Return a key that cannot leave the evidence namespace, or raise.

    Canonical form is relative, forward-slash separated, with no empty, `.` or
    `..` segments. Dot-prefixed segments are reserved for backend internals so
    a successful artifact write can never disappear from listing. Anything
    else is refused outright rather than cleaned up —
    see the module docstring for why a repaired key is worse than a rejected
    one.

    Args:
        key: The proposed storage key.

    Returns:
        The same key, once it has passed.

    Raises:
        UnsafeStorageKeyError: For anything that is not canonical.
    """
    if not key or not key.strip():
        raise UnsafeStorageKeyError("a storage key cannot be empty")
    if "\\" in key:
        raise UnsafeStorageKeyError(f"{key!r} uses a backslash; keys are forward-slash only")
    if key.startswith("/"):
        raise UnsafeStorageKeyError(f"{key!r} is absolute; keys are relative to the namespace")
    if len(key) >= 2 and key[1] == ":" and key[0].isalpha():
        raise UnsafeStorageKeyError(f"{key!r} looks like a drive-letter path")
    if "\x00" in key:
        raise UnsafeStorageKeyError("a storage key cannot contain NUL")
    segments = key.split("/")
    for segment in segments:
        if segment == "":
            raise UnsafeStorageKeyError(
                f"{key!r} has an empty segment (leading, trailing or doubled separator)"
            )
        if segment in {".", ".."}:
            raise UnsafeStorageKeyError(f"{key!r} contains a dot segment")
        if segment.startswith("."):
            raise UnsafeStorageKeyError(f"{key!r} uses a reserved hidden segment")
    return key


def validate_artifact_key(key: str) -> str:
    """Validate a manifest artifact key, refusing control-plane locations.

    Raises:
        UnsafeStorageKeyError: If the key is invalid or outside artifact prefixes.
    """
    validate_key(key)
    if "/" not in key or key.split("/", 1)[0] not in ARTIFACT_PREFIXES:
        raise UnsafeStorageKeyError(f"{key!r} is outside the artifact prefixes")
    return key


class LocalEvidenceStorage:
    """The existing filesystem writer, behind the storage contract.

    Nothing about how bytes reach disk has changed: `write_bytes` is the same
    temp-file, flush, `fsync`, atomic-rename sequence `store_atomically` has
    always performed, anchored to a root that must already exist. What has
    changed is that callers hand this class a *key* and never learn the
    absolute path it becomes.

    The root is whatever the caller names. This class knows nothing about any
    particular volume, drive, filesystem or enclosure, and must never learn.

    Args:
        root: A directory that already exists. Not created here — an absent
            root is the unmounted-volume case, and creating it would put
            evidence on the wrong disk.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.absolute()

    @property
    def root(self) -> Path:
        """The local directory keys are resolved beneath. Local backend only."""
        return self._root

    def _path(self, key: str) -> Path:
        """Resolve a validated key to its local path. Never exposed to callers."""
        validate_key(key)
        self._require_root()
        path = self._root
        try:
            for segment in key.split("/"):
                path = path / segment
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    break
                if stat_mode.S_ISLNK(mode):
                    raise UnsafeStorageKeyError(f"{key!r} traverses a symbolic link")
        except OSError as exc:
            raise StorageUnavailableError(f"cannot resolve {key!r}: {exc}") from exc
        return self._root / key

    def _require_root(self) -> None:
        try:
            if not stat_mode.S_ISDIR(self._root.stat().st_mode):
                raise StorageUnavailableError(f"evidence root {self._root} is not a directory")
        except OSError as exc:
            raise StorageUnavailableError(f"evidence root {self._root} unavailable: {exc}") from exc

    def exists(self, key: str) -> bool:
        """Whether a regular file exists under the key."""
        try:
            self.stat(key)
        except ArtifactNotFoundError:
            return False
        return True

    def stat(self, key: str) -> ArtifactStat:
        """Size of the file under the key."""
        path = self._path(key)
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            self._require_root()
            raise ArtifactNotFoundError(key) from exc
        except OSError as exc:
            raise StorageUnavailableError(f"cannot stat {key!r}: {exc}") from exc
        if not stat_mode.S_ISREG(info.st_mode):
            raise StorageUnavailableError(f"{key!r} is not a regular artifact")
        return ArtifactStat(key=key, bytes=info.st_size)

    def read_bytes(self, key: str) -> bytes:
        """The file's complete contents."""
        path = self._path(key)
        self.stat(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            self._require_root()
            raise ArtifactNotFoundError(key) from exc
        except OSError as exc:
            raise StorageUnavailableError(f"cannot read {key!r}: {exc}") from exc

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """Write atomically beneath the root, exactly as before.

        Delegates to `store_atomically`, which refuses a root that has stopped
        being a directory and a destination outside it. Those refusals surface
        as `EvidenceWriteError`, which is a `StorageUnavailableError`.
        """
        try:
            path = self._path(key)
            written = store_atomically(path, data, root=self._root)
        except (OSError, StorageUnavailableError) as exc:
            raise EvidenceWriteError(f"write of {key!r} did not complete: {exc}") from exc
        return StoredArtifact(key=key, bytes=written, sha256=hashlib.sha256(data).hexdigest())

    def digest(self, key: str) -> str:
        """SHA-256 of the file, streamed from disk."""
        path = self._path(key)
        self.stat(key)
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except FileNotFoundError as exc:
            self._require_root()
            raise ArtifactNotFoundError(key) from exc
        except OSError as exc:
            raise StorageUnavailableError(f"cannot digest {key!r}: {exc}") from exc
        return digest.hexdigest()

    def free_bytes(self) -> int:
        """Free space on the volume holding the root. Local backend only.

        Not part of `EvidenceStorage`: an object store has no free bytes worth
        reporting, and a contract that demanded a number would get an invented
        one. The sweep's preflight asks for this only from a backend that can
        answer — see `sweep.LocalCapacity`.

        Raises:
            StorageUnavailableError: If the root is missing or cannot be
                measured — the unmounted-volume case.
        """
        self._require_root()
        try:
            return shutil.disk_usage(self._root).free
        except OSError as exc:
            raise StorageUnavailableError(
                f"cannot measure free space at {self._root}: {exc}"
            ) from exc

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """Every file key beneath a prefix, sorted, as keys rather than paths.

        Two local-only rules live here and nowhere else. AppleDouble `._`
        sidecars — which macOS writes beside every file on exFAT — are not
        artifacts and are skipped by name before any stat. And an in-flight
        temporary file (`.<name>.<random>` from `store_atomically`) is skipped
        too, since it is a write that has not completed. Neither concept exists
        for an object store, which is exactly why they are contained in this
        class.
        """
        # A prefix naturally ends in a separator; a key never does.
        if prefix:
            validate_key(prefix.removesuffix("/"))
        prefix = prefix.removesuffix("/")
        base = self._path(prefix) if prefix else self._root
        self._require_root()
        try:
            try:
                mode = base.stat().st_mode
            except FileNotFoundError:
                self._require_root()
                return
            if stat_mode.S_ISREG(mode):
                return
            keys: list[str] = []
            pending = [base]
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        if entry.name.startswith("."):
                            continue
                        key = (self._root / entry.path).relative_to(self._root).as_posix()
                        if entry.is_symlink():
                            raise UnsafeStorageKeyError(f"{key!r} traverses a symbolic link")
                        info = entry.stat(follow_symlinks=False)
                        if stat_mode.S_ISDIR(info.st_mode):
                            pending.append(self._root / key)
                        elif stat_mode.S_ISREG(info.st_mode):
                            keys.append(validate_key(key))
                        else:
                            raise StorageUnavailableError(f"{key!r} is not a regular artifact")
        except OSError as exc:
            raise StorageUnavailableError(f"cannot list {prefix!r}: {exc}") from exc
        self._require_root()
        yield from sorted(keys)
