"""A second copy of the control plane, in the bucket, verified by read-back.

The first generation kept one copy of its manifest, its checkpoints and its
plans, on the same drive as the artifacts, and lost all of it at once. The
second generation keeps two: the control root on the internal disk remains
the operational primary, and this module mirrors the small recovery-critical
files — generation metadata, plans, resolution snapshots, chunk manifests,
checkpoints, manifest snapshots — into a namespace of the bucket that no
evidence operation ever looks at.

## Not evidence

`control/` is not an artifact prefix. Reconciliation enumerates
`filings/`, `submissions/` and `fulltext/` and nothing else, so a mirrored
control file can never be an orphan, a trusted artifact, or evidence of
anything. Evidence trust comes from the V2 manifest and verified artifact
bytes; the mirror is what lets that manifest be recovered if the machine
holding it disappears. The two must not be confused, and the key namespace
is what keeps them apart.

## Trust, for control files

Small files, so full read-back is affordable and required:

```text
local bytes → local SHA-256 → PUT (the backend's HEAD verification)
→ GET, hashed → equal to the local digest, or the mirror failed
```

An existing remote key is never overwritten blindly. If its downloaded
bytes hash to the local digest it is a `MIRROR_HIT` and costs no PUT; if
they do not it is a `CONFLICT`, the local file stays where it is, and the
run stops — an unexplained disagreement between two copies of recovery
metadata is the one thing a backup must not paper over.

## Immutable snapshots for mutable files

The manifest grows. A single remote "latest" copy that is overwritten on
every mirror would leave exactly one recovery copy, and a bad overwrite
would destroy it. So mutable files are mirrored as **digest-addressed,
timestamped snapshots**: the bytes at `manifests/evidence-<sha16>.jsonl`,
content-addressed so identical bytes are one object, beside a
`manifests/evidence-<stamp>.snapshot.json` descriptor naming that key, its
digest, the record count and the trusted count — and neither is ever
overwritten. Recovery lists the descriptors, takes the newest, and reads
the file it names; every earlier snapshot is still there to compare against.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from stock_screener.sec_evidence.backend import ArtifactNotFoundError, validate_key
from stock_screener.sec_evidence.storage import ARTIFACT_PREFIXES

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage

log = structlog.get_logger(__name__)

__all__ = [
    "CONTROL_ROOT",
    "ControlMirror",
    "MirrorConflictError",
    "MirrorOutcome",
    "MirrorRecord",
]

#: The bucket namespace for control mirrors. Not an artifact prefix.
CONTROL_ROOT = "control"


class MirrorOutcome(StrEnum):
    """What one mirror operation did."""

    MIRRORED = "MIRRORED"
    """Uploaded, verified by HEAD, then read back and hashed: equal."""

    MIRROR_HIT = "MIRROR_HIT"
    """The remote copy already hashed to the local bytes. No PUT."""

    CONFLICT = "CONFLICT"
    """The remote copy holds different bytes. Nothing overwritten."""


class MirrorConflictError(RuntimeError):
    """A remote control file disagrees with the local one. Stop and look."""


@dataclass(frozen=True)
class MirrorRecord:
    """One mirrored file, for the operator and the mirror ledger.

    Attributes:
        key: The remote key.
        local_path: The file mirrored.
        bytes: Its size.
        sha256: Its digest — local, and confirmed by remote read-back.
        outcome: What happened.
        verified_at: When the remote copy was last read back and matched.
    """

    key: str
    local_path: str
    bytes: int
    sha256: str
    outcome: str
    verified_at: str


@dataclass
class MirrorReport:
    """Every mirror operation of one run, and the request counts it cost."""

    records: list[MirrorRecord] = field(default_factory=list)
    puts: int = 0
    reads: int = 0
    bytes_uploaded: int = 0
    bytes_read_back: int = 0

    def counts(self) -> dict[str, int]:
        """Records per outcome."""
        counts = {outcome.value: 0 for outcome in MirrorOutcome}
        for record in self.records:
            counts[record.outcome] += 1
        return counts


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ControlMirror:
    """Mirrors control files into `control/<generation>/` and proves each one.

    Args:
        storage: The bucket backend. Used for `write_bytes`, `digest`,
            `read_bytes`, `exists` and `list_prefix` under the control
            namespace only.
        generation_id: The generation whose control plane this is.
    """

    def __init__(self, storage: EvidenceStorage, generation_id: str) -> None:
        validate_key(f"{CONTROL_ROOT}/{generation_id}")
        self._storage = storage
        self._prefix = f"{CONTROL_ROOT}/{generation_id}"
        self.report = MirrorReport()

    @property
    def prefix(self) -> str:
        """The remote namespace, without a trailing separator."""
        return self._prefix

    def key_for(self, relative: str) -> str:
        """The remote key for a control file, refusing anything outside the namespace."""
        relative = relative.strip("/")
        validate_key(relative)
        if relative.split("/", 1)[0] in ARTIFACT_PREFIXES:
            raise MirrorConflictError(f"{relative!r} would land in an artifact prefix; refused")
        return f"{self._prefix}/{relative}"

    def mirror(self, local: Path, relative: str) -> MirrorRecord:
        """Mirror one file, or prove the mirror already holds it.

        Args:
            local: The file on the control root.
            relative: Its key beneath the generation namespace.

        Returns:
            The record. `MIRRORED` or `MIRROR_HIT`.

        Raises:
            MirrorConflictError: If the remote copy holds different bytes, or
                if the uploaded copy reads back with a different digest.
            StorageUnavailableError: If the bucket cannot answer.
        """
        key = self.key_for(relative)
        data = local.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        now = datetime.now(UTC).isoformat()
        try:
            remote = self._storage.digest(key)
            self.report.reads += 1
            self.report.bytes_read_back += len(data)
        except ArtifactNotFoundError:
            remote = None
        if remote is not None:
            if remote == digest:
                record = MirrorRecord(
                    key, str(local), len(data), digest, MirrorOutcome.MIRROR_HIT, now
                )
                self.report.records.append(record)
                return record
            record = MirrorRecord(key, str(local), len(data), digest, MirrorOutcome.CONFLICT, now)
            self.report.records.append(record)
            log.error("control mirror conflict", key=key, local=digest[:16], remote=remote[:16])
            raise MirrorConflictError(
                f"{key} already holds bytes hashing to {remote[:16]}…, local is {digest[:16]}…; "
                "nothing overwritten"
            )
        stored = self._storage.write_bytes(key, data)
        self.report.puts += 1
        self.report.bytes_uploaded += stored.bytes
        readback = self._storage.digest(key)
        self.report.reads += 1
        self.report.bytes_read_back += len(data)
        if readback != digest:
            raise MirrorConflictError(
                f"{key} read back as {readback[:16]}… after upload of {digest[:16]}…"
            )
        record = MirrorRecord(key, str(local), stored.bytes, digest, MirrorOutcome.MIRRORED, now)
        self.report.records.append(record)
        log.info("control file mirrored", key=key, bytes=stored.bytes)
        return record

    def mirror_snapshot(
        self, local: Path, kind: str, **facts: object
    ) -> tuple[MirrorRecord, MirrorRecord]:
        """Mirror a mutable file as an immutable, digest-addressed snapshot.

        The bytes go to `<kind>/<stem>-<sha16><suffix>` — content-addressed,
        so identical bytes mirrored twice are one object and a `MIRROR_HIT`
        the second time — and a descriptor goes to
        `<kind>/<stem>-<UTC stamp, microseconds>.snapshot.json`, naming the
        file's key, its digest, its size and whatever facts the caller
        supplies (record counts, trusted counts, the stage label). Neither is
        ever overwritten: a snapshot is a new descriptor every time, and the
        newest descriptor is what recovery reads first.

        Returns:
            The records for the file and its descriptor.
        """
        data = local.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        file_relative = f"{kind}/{local.stem}-{digest[:16]}{local.suffix}"
        descriptor = local.parent / f".{local.stem}-{stamp}.snapshot.json"
        descriptor.write_text(
            json.dumps(
                {
                    "file": local.name,
                    "key": file_relative,
                    "sha256": digest,
                    "bytes": len(data),
                    "stamp": stamp,
                    **facts,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            file_record = self.mirror(local, file_relative)
            meta_record = self.mirror(descriptor, f"{kind}/{local.stem}-{stamp}.snapshot.json")
        finally:
            descriptor.unlink(missing_ok=True)
        return file_record, meta_record

    def newest_snapshot(self, kind: str) -> tuple[dict[str, object], bytes] | None:
        """The newest snapshot descriptor under `kind`, and the bytes it names.

        Recovery's entry point: list, take the latest descriptor by stamp,
        read the file it names, and refuse it unless the bytes hash to the
        digest the descriptor recorded.

        Raises:
            MirrorConflictError: If the named file is missing or hashes
                differently — the mirror is internally inconsistent.
        """
        descriptors = sorted(
            key
            for key in self.listing()
            if key.startswith(f"{kind}/") and key.endswith(".snapshot.json")
        )
        if not descriptors:
            return None
        facts: dict[str, object] = json.loads(self.read(descriptors[-1]).decode("utf-8"))
        file_key = str(facts["key"])
        try:
            data = self.read(file_key)
        except ArtifactNotFoundError as exc:
            raise MirrorConflictError(
                f"{descriptors[-1]} names {file_key}, which the mirror does not hold"
            ) from exc
        if hashlib.sha256(data).hexdigest() != facts["sha256"]:
            raise MirrorConflictError(
                f"{file_key} does not hash to what {descriptors[-1]} recorded"
            )
        return facts, data

    def verify(self, local: Path, relative: str) -> bool:
        """Whether the remote copy's downloaded bytes hash to the local file's."""
        key = self.key_for(relative)
        self.report.reads += 1
        try:
            remote = self._storage.digest(key)
        except ArtifactNotFoundError:
            return False
        return remote == _sha256_of(local)

    def listing(self) -> list[str]:
        """Every key under the generation namespace, as relative keys."""
        cut = len(self._prefix) + 1
        return [key[cut:] for key in self._storage.list_prefix(self._prefix)]

    def read(self, relative: str) -> bytes:
        """Download one mirrored file. Recovery reads through this.

        Raises:
            ArtifactNotFoundError: If the mirror holds no such file.
            StorageUnavailableError: If the bucket cannot answer.
        """
        self.report.reads += 1
        return self._storage.read_bytes(self.key_for(relative))

    def write_ledger(self, path: Path) -> None:
        """Append this run's records to a local ledger, one JSON line each."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in self.report.records:
                handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
