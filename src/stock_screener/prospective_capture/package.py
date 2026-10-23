"""The sealed decision-input package, and the handoff across the instant.

A prospective decision is taken in two halves separated by the instant itself.
Before it, the current-only sources must be in hand and sealed, because the
broker's asset list describes the present and cannot be asked later what it said
at four o'clock. After it, the session-bounded and event-time sources complete
the package. The halves must not contaminate each other: refreshing the universe
after the close would substitute a world the decision could not have seen, which
is the precise failure the V2 protocol exists to prevent.

So the package is append-only across the boundary. `seal_predecision` writes the
first half and nothing may replace it; `complete_post_close` adds only the
sources the frozen sequence admits after the close. A fresh process can load
either state and continue, which is what makes a crash between the halves
survivable rather than another lost quarter.

Nothing here scores anything. Sealing is the point at which the decision's inputs
stop changing, and it happens before the first rank is computed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.prospective_source.envelope import (
    Completeness,
    SnapshotEnvelope,
    TemporalClass,
    admissible_for,
    content_digest,
)
from stock_screener.prospective_source.readiness import FRESHNESS_POLICY

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from stock_screener.prospective_source import SnapshotStore
    from stock_screener.prospective_source.adapters import SourceResult

log = structlog.get_logger(__name__)

__all__ = [
    "MANIFEST_FILE",
    "POST_CLOSE_SOURCES",
    "PREDECISION_SOURCES",
    "PackageError",
    "SealedPackage",
    "complete_post_close",
    "load_package",
    "seal_predecision",
]

MANIFEST_FILE = "decision_input_package.json"

#: The current-only sources. These must be retrieved at or before the instant.
PREDECISION_SOURCES: tuple[str, ...] = ("universe", "tradability")

#: What the frozen sequence admits after the close. `filing_observations` and
#: `corporate_actions` are event-time and session-bounded respectively, so late
#: retrieval is fine; late information is not, and each carries its own bound.
POST_CLOSE_SOURCES: tuple[str, ...] = (
    "market",
    "sec_availability",
    "filing_observations",
    "corporate_actions",
)


class PackageError(RuntimeError):
    """Raised when the input package would be built unsoundly."""


@dataclass
class SealedPackage:
    """The decision's inputs, frozen and addressable.

    Attributes:
        decision_session: The session being decided.
        decision_instant: The instant, ISO with offset.
        envelopes: `{source: envelope}` for everything sealed so far.
        addresses: `{source: store address}`, so payloads are recoverable.
        sealed_at: `{source: when it was sealed}`.
        stage: `PREDECISION` or `COMPLETE`.
    """

    decision_session: str
    decision_instant: str
    envelopes: dict[str, SnapshotEnvelope] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)
    sealed_at: dict[str, str] = field(default_factory=dict)
    stage: str = "PREDECISION"

    @property
    def fingerprints(self) -> dict[str, str]:
        """Each sealed source's envelope digest."""
        return {name: envelope.fingerprint() for name, envelope in sorted(self.envelopes.items())}

    def body(self) -> dict[str, Any]:
        """The manifest, as written and as digested."""
        return {
            "decision_session": self.decision_session,
            "decision_instant": self.decision_instant,
            "stage": self.stage,
            "predecision_sources": list(PREDECISION_SOURCES),
            "post_close_sources": list(POST_CLOSE_SOURCES),
            "envelopes": {
                name: envelope.as_json() for name, envelope in sorted(self.envelopes.items())
            },
            "addresses": dict(sorted(self.addresses.items())),
            "sealed_at": dict(sorted(self.sealed_at.items())),
            "fingerprints": self.fingerprints,
            "append_only_across_the_instant": True,
            "predecision_sources_are_never_refreshed": True,
        }

    def fingerprint(self) -> str:
        """The package's own digest."""
        return content_digest(self.body())

    def write(self, directory: Path) -> Path:
        """Write the manifest so a fresh process can recover it.

        Args:
            directory: Where the window's artifacts live.

        Returns:
            The manifest's path.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / MANIFEST_FILE
        path.write_text(json.dumps(self.body(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def verify(self, store: SnapshotStore) -> dict[str, Any]:
        """Re-read every sealed payload and confirm it still digests as sealed.

        Args:
            store: The prospective snapshot store.

        Returns:
            One verdict per source, and whether all of them held.

        Raises:
            PackageError: If a sealed payload no longer matches its envelope.
        """
        verdicts: dict[str, Any] = {}
        for name, envelope in sorted(self.envelopes.items()):
            recovered = store.get(
                envelope.source_type,
                envelope.decision_session or "unbound",
                envelope.content_sha256[:16],
            )
            matches = recovered["envelope"]["content_sha256"] == envelope.content_sha256
            verdicts[name] = {
                "recovered": True,
                "content_matches": matches,
                "records": recovered["envelope"]["record_count"],
            }
            if not matches:
                raise PackageError(
                    f"{name} no longer digests as sealed; the package is not replayable"
                )
        return {"sources": verdicts, "all_recovered": True}


def _admit(name: str, envelope: SnapshotEnvelope, instant: datetime) -> None:
    """Refuse a source that the frozen timing rules do not admit."""
    verdict = admissible_for(envelope, instant, max_age=FRESHNESS_POLICY.get(name))
    if not verdict["admissible"]:
        raise PackageError(
            f"{name} is not admissible for {instant.isoformat()}: {verdict['reasons']}. "
            "The semantic cutoff is never weakened to let a capture succeed."
        )


def seal_predecision(
    store: SnapshotStore,
    directory: Path,
    *,
    decision_session: str,
    decision_instant: datetime,
    sources: Mapping[str, SourceResult],
) -> SealedPackage:
    """Seal the current-only sources before the instant.

    Args:
        store: The prospective snapshot store.
        directory: Where the window's artifacts live.
        decision_session: The session being decided.
        decision_instant: The instant these must precede.
        sources: `{source: result}` for the predecision sources.

    Returns:
        The sealed first half.

    Raises:
        PackageError: If a predecision source is missing, was retrieved after the
            instant, or falls outside its freshness bound.
    """
    missing = [name for name in PREDECISION_SOURCES if name not in sources]
    if missing:
        raise PackageError(
            f"REQUIRED_PREDECISION_SOURCE_UNAVAILABLE: {missing}. Miss the "
            "observation honestly rather than substituting a later snapshot."
        )
    package = SealedPackage(
        decision_session=decision_session,
        decision_instant=decision_instant.isoformat(),
        stage="PREDECISION",
    )
    for name in PREDECISION_SOURCES:
        result = sources[name]
        if result.envelope.temporal_class is not TemporalClass.CURRENT_AT_RETRIEVAL:
            raise PackageError(
                f"{name} declares {result.envelope.temporal_class}; a predecision "
                "source is current-only by definition and its class is not relabelled"
            )
        _admit(name, result.envelope, decision_instant)
        stored = store.put(result.envelope, result.payload)
        package.envelopes[name] = result.envelope
        package.addresses[name] = str(stored.envelope_path.parent)
        package.sealed_at[name] = result.envelope.retrieved_at.isoformat()
    package.write(directory)
    log.info(
        "prospective.sealed_predecision",
        session=decision_session,
        sources=sorted(package.envelopes),
        fingerprint=package.fingerprint()[:16],
    )
    return package


def complete_post_close(
    store: SnapshotStore,
    directory: Path,
    *,
    package: SealedPackage,
    decision_instant: datetime,
    sources: Mapping[str, SourceResult],
) -> SealedPackage:
    """Add the post-close sources without disturbing the sealed first half.

    Args:
        store: The prospective snapshot store.
        directory: Where the window's artifacts live.
        package: The sealed predecision package.
        decision_instant: The instant everything is bounded to.
        sources: `{source: result}` for the post-close sources.

    Returns:
        The completed package.

    Raises:
        PackageError: If a post-close source would replace a predecision one, if
            one is missing, or if one is inadmissible.
    """
    colliding = sorted(set(sources) & set(PREDECISION_SOURCES))
    if colliding:
        raise PackageError(
            f"POST_CLOSE_WOULD_REPLACE_PREDECISION_SOURCE: {colliding}. The "
            "current-only snapshots taken before the instant are the decision's "
            "view of the world and are never refreshed afterwards."
        )
    missing = [name for name in POST_CLOSE_SOURCES if name not in sources]
    if missing:
        raise PackageError(f"SOURCE_INCOMPLETE: post-close sources missing {missing}")
    for name in POST_CLOSE_SOURCES:
        result = sources[name]
        _admit(name, result.envelope, decision_instant)
        stored = store.put(result.envelope, result.payload)
        package.envelopes[name] = result.envelope
        package.addresses[name] = str(stored.envelope_path.parent)
        package.sealed_at[name] = result.envelope.retrieved_at.isoformat()
    package.stage = "COMPLETE"
    package.write(directory)
    log.info(
        "prospective.sealed_complete",
        session=package.decision_session,
        sources=sorted(package.envelopes),
        fingerprint=package.fingerprint()[:16],
    )
    return package


def load_package(directory: Path) -> SealedPackage:
    """Recover a sealed package from disk, for a fresh process.

    Args:
        directory: Where the window's artifacts live.

    Returns:
        The package as sealed.

    Raises:
        PackageError: If no manifest exists.
    """
    path = directory / MANIFEST_FILE
    if not path.is_file():
        raise PackageError(f"no sealed input package at {path}")
    body = json.loads(path.read_text(encoding="utf-8"))
    package = SealedPackage(
        decision_session=str(body["decision_session"]),
        decision_instant=str(body["decision_instant"]),
        addresses=dict(body.get("addresses") or {}),
        sealed_at=dict(body.get("sealed_at") or {}),
        stage=str(body.get("stage") or "PREDECISION"),
    )
    for name, raw in (body.get("envelopes") or {}).items():
        package.envelopes[name] = SnapshotEnvelope(
            source_type=str(raw["source_type"]),
            source_name=str(raw["source_name"]),
            source_version=str(raw["source_version"]),
            operation=str(raw["operation"]),
            retrieved_at=datetime.fromisoformat(str(raw["retrieved_at"])),
            semantic_as_of=datetime.fromisoformat(str(raw["semantic_as_of"])),
            content_sha256=str(raw["content_sha256"]),
            request=str(raw["request_fingerprint"]),
            normalizer_version=str(raw["normalizer_version"]),
            record_count=int(raw["record_count"]),
            completeness=Completeness(str(raw["completeness"])),
            temporal_class=TemporalClass(str(raw["temporal_class"])),
            effective_at=(
                datetime.fromisoformat(str(raw["effective_at"]))
                if raw.get("effective_at")
                else None
            ),
            decision_session=raw.get("decision_session"),
            min_source_timestamp=raw.get("min_source_timestamp"),
            max_source_timestamp=raw.get("max_source_timestamp"),
            ambiguity=list(raw.get("ambiguity") or []),
            notes=list(raw.get("notes") or []),
        )
    return package
