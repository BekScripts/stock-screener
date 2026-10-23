"""An immutable, content-addressed store for prospective source snapshots.

Separate from the historical evidence namespace on purpose. Historical evidence
is frozen and must stay that way; prospective snapshots accumulate. Mixing them
would make provenance a matter of reading dates rather than reading paths, and
`refuses_historical_namespace` exists so the separation is enforced rather than
remembered.

Objects are addressed by their own digest, so writing the same payload twice is
free and writing different bytes to the same address is impossible. A decision
taken months ago stays reconstructable from exactly the bytes it used, which is
what §28 means by durability: the final ranking alone would not let anyone check
the ranking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - Path is constructed at runtime here
from typing import Any

from .envelope import SnapshotEnvelope, content_digest

__all__ = [
    "FORBIDDEN_PREFIXES",
    "PROSPECTIVE_STORE_ROOT",
    "SnapshotStore",
    "StoreConflictError",
    "refuses_historical_namespace",
]

#: Where prospective snapshots live, beneath the control root.
PROSPECTIVE_STORE_ROOT = "prospective-source"

#: Historical evidence namespaces a prospective adapter may never write into.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "filings",
    "submissions",
    "fulltext",
    "evidence",
    "manifests",
    "research",
    "control",
)


class StoreConflictError(RuntimeError):
    """Raised when a stored object would be overwritten with different bytes."""


def refuses_historical_namespace(path: Path, root: Path) -> None:
    """Refuse a write that would land in a historical evidence namespace.

    Args:
        path: The intended destination.
        root: The prospective store's root.

    Raises:
        StoreConflictError: If the destination is outside the prospective root,
            or inside a historical namespace.
    """
    resolved = path.resolve()
    base = root.resolve()
    if not str(resolved).startswith(str(base)):
        raise StoreConflictError(
            f"a prospective snapshot may only be written beneath {base}, not {resolved}"
        )
    relative = resolved.relative_to(base).parts
    for part in relative:
        if part in FORBIDDEN_PREFIXES:
            raise StoreConflictError(
                f"'{part}' is a historical evidence namespace; prospective "
                "snapshots are never written there"
            )


@dataclass(frozen=True)
class Stored:
    """A snapshot as it landed.

    Attributes:
        envelope_path: Where the envelope lives.
        payload_path: Where the raw payload lives.
        envelope_sha256: The envelope's digest.
        content_sha256: The payload's digest.
        written: False when identical bytes were already present.
    """

    envelope_path: Path
    payload_path: Path
    envelope_sha256: str
    content_sha256: str
    written: bool


@dataclass(frozen=True)
class SnapshotStore:
    """The prospective snapshot store.

    Args:
        root: The store's root directory.
    """

    root: Path

    def _destination(self, envelope: SnapshotEnvelope) -> Path:
        """Where a snapshot's objects belong, addressed by its content."""
        session = envelope.decision_session or "unbound"
        return self.root / envelope.source_type / session / envelope.content_sha256[:16]

    def put(self, envelope: SnapshotEnvelope, payload: Any) -> Stored:
        """Store a snapshot and its raw payload, once.

        Args:
            envelope: The snapshot's envelope.
            payload: The raw payload, as retrieved.

        Returns:
            Where it landed and whether anything was written.

        Raises:
            StoreConflictError: If the payload's digest disagrees with the
                envelope, or if different bytes already occupy the address.
        """
        observed = content_digest(payload)
        if observed != envelope.content_sha256:
            raise StoreConflictError(
                f"the payload digests to {observed} but the envelope claims "
                f"{envelope.content_sha256}; a snapshot must describe its own bytes"
            )
        destination = self._destination(envelope)
        refuses_historical_namespace(destination, self.root)
        destination.mkdir(parents=True, exist_ok=True)
        payload_path = destination / "payload.json"
        envelope_path = destination / "envelope.json"
        body = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
        envelope_body = json.dumps(envelope.as_json(), indent=2, sort_keys=True) + "\n"
        written = True
        if payload_path.exists():
            existing = json.loads(payload_path.read_text(encoding="utf-8"))
            if content_digest(existing) != observed:
                raise StoreConflictError(
                    f"{payload_path} already holds different bytes; a stored "
                    "snapshot is never overwritten"
                )
            written = False
        else:
            payload_path.write_text(body, encoding="utf-8")
        if not envelope_path.exists():
            envelope_path.write_text(envelope_body, encoding="utf-8")
        return Stored(
            envelope_path=envelope_path,
            payload_path=payload_path,
            envelope_sha256=envelope.fingerprint(),
            content_sha256=observed,
            written=written,
        )

    def get(self, source_type: str, session: str, digest_prefix: str) -> dict[str, Any]:
        """Read a snapshot back and verify it still digests as stored.

        Args:
            source_type: The input kind.
            session: The decision session, or `unbound`.
            digest_prefix: The first sixteen characters of the content digest.

        Returns:
            The envelope and payload.

        Raises:
            StoreConflictError: If the payload no longer matches its envelope.
        """
        destination = self.root / source_type / session / digest_prefix
        envelope = json.loads((destination / "envelope.json").read_text(encoding="utf-8"))
        payload = json.loads((destination / "payload.json").read_text(encoding="utf-8"))
        observed = content_digest(payload)
        if observed != envelope["content_sha256"]:
            raise StoreConflictError(
                f"{destination} no longer matches its envelope: payload digests to "
                f"{observed}, envelope says {envelope['content_sha256']}"
            )
        return {"envelope": envelope, "payload": payload, "verified": True}
