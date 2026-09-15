"""Where the SEC evidence corpus lives, and refusing to guess when it does not.

The corpus is raw research evidence — filings, submissions, full-text responses
and the manifests that give each one a URL, a digest and a retrieval time. It is
large enough to belong on external storage, which makes *absence* a normal
runtime condition rather than an exceptional one, and absence is exactly where a
cache silently does the wrong thing.

Two failures this module exists to prevent, both of which have already happened
to this project once:

- **A harvest filling the boot disk.** The next corpus pass is on the order of
  hundreds of gigabytes. A default path inside the working tree would make that
  the default destination.
- **A second, empty cache appearing beside the real one.** If the external volume
  is not mounted and the code creates its root on demand, everything downstream
  keeps working and reports zero evidence — indistinguishable from a corpus that
  genuinely holds nothing. Phase 9K lost a 19 GB corpus to an ephemeral directory
  and only noticed because a size check happened to run; a mount that quietly
  reappears as an empty directory is the same failure with no size check.

So the root is **never created here**. `resolve_sec_evidence_dir` reports which
of four states holds and raises for three of them, and the states are kept
distinct because they call for different actions: configure the setting, plug the
drive in, or run the harvest.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


class EvidenceState(StrEnum):
    """What the configured evidence root turned out to be."""

    UNCONFIGURED = "UNCONFIGURED"
    """`sec_evidence_dir` is unset. Nothing was looked for."""

    UNAVAILABLE = "UNAVAILABLE"
    """Configured, and the path does not exist — the usual shape of an external
    volume that is not mounted."""

    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
    """Configured, the path exists, and it is a file. Almost always a typo."""

    EMPTY = "EMPTY"
    """A real directory holding nothing. Distinct from `UNAVAILABLE` because the
    corpus has simply not been harvested yet, which is not an error."""

    AVAILABLE = "AVAILABLE"
    """A real directory with contents."""


class EvidenceUnavailableError(RuntimeError):
    """The evidence corpus cannot be read, with the reason attached.

    Attributes:
        state: Which of the four failing states applied.
        path: The configured path, where there was one.
    """

    def __init__(self, state: EvidenceState, path: Path | None) -> None:
        self.state = state
        self.path = path
        super().__init__(_message(state, path))


def _message(state: EvidenceState, path: Path | None) -> str:
    """Return the sentence an operator needs, naming the fix rather than the fault."""
    if state is EvidenceState.UNCONFIGURED:
        return (
            "SEC_EVIDENCE_DIR is not set. It has no default on purpose: the corpus "
            "belongs on external storage, and a default would let a harvest fill the "
            "boot disk. Set it to the evidence root, e.g. "
            "/Volumes/<drive>/stock-screener-data/sec."
        )
    if state is EvidenceState.UNAVAILABLE:
        return (
            f"SEC evidence root {path} does not exist. If it is on an external "
            "volume, that volume is probably not mounted. This is not created "
            "automatically — an empty directory here would be indistinguishable "
            "from a corpus holding nothing."
        )
    if state is EvidenceState.NOT_A_DIRECTORY:
        return f"SEC evidence root {path} exists but is a file, not a directory."
    if state is EvidenceState.EMPTY:
        return (
            f"SEC evidence root {path} exists but holds nothing. The corpus has not "
            "been harvested, or the wrong root is configured. Reported separately "
            "from an unmounted volume because the fix is different."
        )
    return f"SEC evidence root {path} is unusable."


def inspect_sec_evidence_dir(settings: Settings) -> tuple[EvidenceState, Path | None]:
    """Report the state of the configured evidence root without raising.

    For a status command or a health check — anything that wants to *describe*
    the situation rather than refuse to proceed.

    Args:
        settings: The application settings.

    Returns:
        The state and the configured path, which is None when unconfigured.
    """
    configured = (settings.sec_evidence_dir or "").strip()
    if not configured:
        return EvidenceState.UNCONFIGURED, None
    path = Path(configured).expanduser()
    if not path.exists():
        return EvidenceState.UNAVAILABLE, path
    if not path.is_dir():
        return EvidenceState.NOT_A_DIRECTORY, path
    if not any(path.iterdir()):
        return EvidenceState.EMPTY, path
    return EvidenceState.AVAILABLE, path


def resolve_sec_evidence_dir(settings: Settings, *, allow_empty: bool = True) -> Path:
    """Return the evidence root, or raise saying which way it is unusable.

    **Never creates the directory.** A caller that wants to write into the corpus
    still gets a root that must already exist, because the whole point is that an
    unmounted volume stops the run instead of starting a new cache.

    Args:
        settings: The application settings.
        allow_empty: Whether an existing but empty root is acceptable. True for a
            harvest about to populate it; False for a reader, which would
            otherwise report an empty corpus as a legitimate answer.

    Returns:
        The resolved root.

    Raises:
        EvidenceUnavailableError: Unconfigured, missing, not a directory, or
            empty when `allow_empty` is False.
    """
    state, path = inspect_sec_evidence_dir(settings)
    if state is EvidenceState.EMPTY and allow_empty:
        log.info("sec evidence root is empty", path=str(path))
        return path if path is not None else Path()
    if state is EvidenceState.AVAILABLE and path is not None:
        return path
    if state is EvidenceState.EMPTY and path is not None:
        raise EvidenceUnavailableError(EvidenceState.EMPTY, path)
    raise EvidenceUnavailableError(state, path)
