"""The control plane: the small metadata that lets a corpus be recovered.

Phase 9L.6 lost an external drive that held 19,350 harvested filings. The
filings were the smaller loss. On the same drive sat the manifest that said
which URL produced each of them, the checkpoints that said which batches had
completed, and the frozen plan that said what remained — so losing the corpus
also lost the only record of what the corpus had been. That is a failure of
placement, not of hardware, and this module is the fix: control-plane state
gets a root of its own, chosen independently of wherever artifacts live.

## What is control plane

Operational and recovery metadata. Small, and worth more than its size:

```text
manifests/evidence.jsonl   provenance — URL, status, SHA-256, bytes, key
checkpoints/               per-chunk sweep state, resumable
plans/                     frozen expansion plans and target manifests
migration/                 reserved for the future artifact-migration ledger
derived/                   rebuildable output: the extraction index and the
                           fingerprint that describes it
```

The first four are **critical**: a corpus can be rebuilt from them and a
network connection, and nothing can rebuild them from a corpus. `derived/`
is the exception, kept here because it is local by nature rather than because
it is precious — everything under it is recomputed from the manifest and the
artifacts, and deleting it costs a rebuild, never evidence.

## What is not

Artifacts — `filings/`, `submissions/`, `fulltext/` — are reached only through
`EvidenceStorage` and never through a path from this module. A manifest
record's `path` field is an artifact-plane logical key, and the control plane
holds that key without knowing or caring which backend resolves it. No
control-plane operation lists, reads or writes an artifact.

## Where the root comes from

`SEC_CONTROL_DIR` names it. The directory must already exist: it is never
created here, for the same reason the artifact root is never created — a
control root that could be conjured on the boot disk when the configured one
was unreachable would be a second, empty source of truth that looks exactly
like a project that has done nothing. The subdirectories beneath a verified
root *are* created, which is safe precisely because the root being present is
the proof the location is real.

When `SEC_CONTROL_DIR` is unset the historical layout applies: the control
plane is colocated with the local artifact root. That is the arrangement that
lost the recovery metadata, and it is retained only so that existing corpora
and development setups keep working unchanged. It is reported wherever the
layout is printed, and it is transitional.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

__all__ = [
    "CHECKPOINTS",
    "CONTROL_SUBDIRECTORIES",
    "DERIVED",
    "EVIDENCE_MANIFEST",
    "MANIFESTS",
    "MIGRATION",
    "PLANS",
    "ControlPlaneUnavailableError",
    "ControlState",
    "EvidenceControlRoot",
    "inspect_control_dir",
    "open_control_root",
    "same_volume",
]

#: Provenance manifests.
MANIFESTS = "manifests"
#: Per-chunk sweep checkpoints.
CHECKPOINTS = "checkpoints"
#: Frozen expansion plans and target manifests.
PLANS = "plans"
#: Reserved: the ledger a future artifact migration records itself in.
MIGRATION = "migration"
#: Rebuildable output — the extraction index and its corpus fingerprint.
DERIVED = "derived"

#: The manifest every harvest appends to, beneath `manifests/`.
EVIDENCE_MANIFEST = "evidence.jsonl"

#: Created beneath a verified control root on open. The others are made by
#: their writers on demand, so a root that has never planned or extracted
#: does not carry empty directories claiming otherwise.
CONTROL_SUBDIRECTORIES = (MANIFESTS, CHECKPOINTS)


class ControlState(StrEnum):
    """What was found at the configured control root."""

    UNCONFIGURED = "UNCONFIGURED"
    """`SEC_CONTROL_DIR` is unset — the legacy colocated layout applies."""

    UNAVAILABLE = "UNAVAILABLE"
    """The path does not exist. An unmounted volume, or a root never made."""

    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
    """The path exists and is not a directory."""

    AVAILABLE = "AVAILABLE"
    """A directory. Usable."""


class ControlPlaneUnavailableError(RuntimeError):
    """The control plane cannot be reached, as distinct from being empty.

    Raised when the configured root is missing, when a directory that must
    hold a manifest or checkpoint has gone, or when the control plane is
    needed and nothing configures one. Never raised for a manifest that has
    simply not been written yet — that is first-time initialisation, and the
    difference between the two is the whole point of this type.
    """


@dataclass(frozen=True)
class EvidenceControlRoot:
    """A verified control root and the paths beneath it.

    Only constructed by `open_control_root`, which is what makes "this exists
    and is a directory" true for every instance. Holds paths, not artifacts:
    nothing here can reach `filings/`, and nothing should.

    Attributes:
        path: The verified root.
    """

    path: Path

    @property
    def colocated(self) -> bool:
        """Whether this is the legacy layout that shares the artifact root.

        False for a root named by `SEC_CONTROL_DIR`. The local `EvidenceRoot`
        overrides this, because it *is* that layout.
        """
        return False

    @property
    def manifests(self) -> Path:
        """Where provenance manifests live."""
        return self.path / MANIFESTS

    @property
    def manifest_path(self) -> Path:
        """The evidence manifest every harvest appends to."""
        return self.manifests / EVIDENCE_MANIFEST

    @property
    def checkpoints(self) -> Path:
        """Where sweep checkpoints live."""
        return self.path / CHECKPOINTS

    @property
    def plans(self) -> Path:
        """Where frozen plans and target manifests belong."""
        return self.path / PLANS

    @property
    def migration(self) -> Path:
        """Reserved for the artifact-migration ledger. Nothing writes here yet."""
        return self.path / MIGRATION

    @property
    def derived(self) -> Path:
        """Where rebuildable output goes. Deleting it costs a rebuild, not evidence."""
        return self.path / DERIVED

    def ensure_layout(self) -> None:
        """Create the subdirectories that must exist beneath a verified root.

        Safe only because the root has been verified: creating `manifests/`
        under a root that is present cannot put a manifest anywhere but where
        it was configured to go.
        """
        for name in CONTROL_SUBDIRECTORIES:
            (self.path / name).mkdir(exist_ok=True)


def inspect_control_dir(settings: Settings) -> tuple[ControlState, Path | None]:
    """Classify the configured control root without creating anything.

    Args:
        settings: Supplies `SEC_CONTROL_DIR`.

    Returns:
        The state, and the absolute path it describes — None when unset.
    """
    raw = (settings.sec_control_dir or "").strip()
    if not raw:
        return ControlState.UNCONFIGURED, None
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        return ControlState.UNAVAILABLE, path
    if not path.is_dir():
        return ControlState.NOT_A_DIRECTORY, path
    return ControlState.AVAILABLE, path


def open_control_root(
    settings: Settings, *, colocate_with: EvidenceControlRoot | None = None
) -> EvidenceControlRoot:
    """Verify the configured control root and prepare its layout.

    Args:
        settings: Supplies `SEC_CONTROL_DIR`.
        colocate_with: The legacy fallback — the local artifact root, which is
            itself a control root — used only when `SEC_CONTROL_DIR` is unset.
            A caller that wires a remote artifact backend has no local root to
            offer, passes nothing, and gets an error rather than a colocated
            control plane it could not have meant.

    Returns:
        The verified root: a fresh `EvidenceControlRoot` when configured, or
        `colocate_with` unchanged when falling back.

    Raises:
        ControlPlaneUnavailableError: If the setting names a path that is
            missing or not a directory, or if it is unset and there is nothing
            to fall back to. The root is never created.
    """
    state, path = inspect_control_dir(settings)
    if state is ControlState.UNCONFIGURED:
        if colocate_with is None:
            message = (
                "SEC_CONTROL_DIR is unset and no local artifact root is available to "
                "colocate with; set SEC_CONTROL_DIR to the directory that holds the "
                "manifest and checkpoints"
            )
            raise ControlPlaneUnavailableError(message)
        log.warning(
            "control plane colocated with the artifact root (legacy layout)",
            root=str(colocate_with.path),
        )
        return colocate_with
    if state is ControlState.UNAVAILABLE:
        message = (
            f"SEC_CONTROL_DIR={path} does not exist — if it is on an external volume, "
            "is the volume mounted? It is never created automatically"
        )
        raise ControlPlaneUnavailableError(message)
    if state is ControlState.NOT_A_DIRECTORY or path is None:
        raise ControlPlaneUnavailableError(f"SEC_CONTROL_DIR={path} is not a directory")
    root = EvidenceControlRoot(path=path)
    root.ensure_layout()
    log.debug("control root ready", root=str(path))
    return root


def same_volume(first: Path, second: Path) -> bool:
    """Whether two existing paths sit on one device — one failure domain.

    The question the SSD loss makes worth asking of any layout: if this volume
    goes, does everything go with it? Answered from the device number, so a
    control root in a different directory on the same drive is still caught.
    Both paths must exist; a path that does not is its own, louder problem.
    """
    return first.stat().st_dev == second.stat().st_dev
