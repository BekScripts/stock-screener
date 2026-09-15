"""The record of what was fetched, from where, and exactly which bytes were kept.

A corpus without provenance is a pile of files. The manifest is what lets a
later audit answer the only questions that matter about an artifact: *what URL
produced this, when, what did the server say, and are these the bytes that
arrived?*

## Append-only, one JSON object per line

The full corpus is expected to reach ~122,000 filings. Rewriting a manifest of
that size after every request would cost more than the download, so entries are
appended: open, write one line, flush, fsync, close. An append of a single short
line is the cheapest crash-safe write available, and a reader that meets a
truncated final line — the one shape a crash mid-append can leave — discards it
and keeps everything before it, because every earlier line was fsynced whole.

Entries are never edited or removed. A retry of a failed URL appends a second
entry; the log is a history, not a current-state table, and `latest_by_url`
folds it into one when a current state is what a caller needs.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.sec_evidence.control import EVIDENCE_MANIFEST, ControlPlaneUnavailableError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

log = structlog.get_logger(__name__)

__all__ = [
    "EVIDENCE_MANIFEST",
    "Entry",
    "Manifest",
    "ManifestState",
    "Outcome",
]


class Outcome(StrEnum):
    """What happened to one requested artifact."""

    DOWNLOADED = "DOWNLOADED"
    """Fetched from the SEC and stored."""

    CACHE_HIT = "CACHE_HIT"
    """Already stored, and its digest matched the manifest. Not re-fetched."""

    INVALID_CACHE = "INVALID_CACHE"
    """Already stored, and its digest did **not** match. Re-fetched."""

    FAILED = "FAILED"
    """The request did not produce a usable artifact."""

    SKIPPED = "SKIPPED"
    """Not attempted — the budget stopped the run, or a dry run was asked for."""


@dataclass(frozen=True)
class Entry:
    """One line of the manifest.

    The five fields Phase 9K.13 requires of every artifact — `url`, `status`,
    `sha256`, `bytes`, `retrieved_at` — are mandatory for a `DOWNLOADED` or
    `CACHE_HIT` entry and are the reason this record exists. Everything else is
    recorded when the server or the caller supplied it and left None when not:
    an absent `etag` means the SEC sent none, never that one was not looked for.

    Attributes:
        url: The absolute URL requested.
        outcome: What happened.
        status: HTTP status, where a response arrived.
        sha256: Digest of the stored bytes.
        bytes: How many bytes were stored.
        retrieved_at: When the response arrived, ISO-8601 UTC.
        path: Location relative to the evidence root.
        request_kind: Which layout the artifact belongs to.
        content_type: Declared media type, where sent.
        etag: Entity tag, where sent.
        last_modified: `Last-Modified` header verbatim, where sent.
        cik: Filer number, where the caller knew one.
        accession: Accession number, where the caller knew one.
        form: Form type, where the caller knew one.
        filing_date: Filing date, where the caller knew one.
        error: Why a `FAILED` entry failed.
        attempt: Which attempt this entry records, counting from one.
    """

    url: str
    outcome: Outcome
    status: int | None = None
    sha256: str | None = None
    bytes: int | None = None
    retrieved_at: str | None = None
    path: str | None = None
    request_kind: str | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    cik: int | None = None
    accession: str | None = None
    form: str | None = None
    filing_date: str | None = None
    error: str | None = None
    attempt: int = 1
    recorded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_line(self) -> str:
        """Return the entry as one JSON line, without its newline."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @property
    def is_stored(self) -> bool:
        """Whether this entry claims bytes are on disk."""
        return self.outcome in {Outcome.DOWNLOADED, Outcome.CACHE_HIT}


@dataclass(frozen=True)
class ManifestState:
    """What one pass over the log tells a harvest.

    Attributes:
        latest: The last entry recorded for each URL.
        attempts: How many entries each URL has. Mutated by the harvest as it
            appends, so the next attempt number stays exact.
    """

    latest: dict[str, Entry]
    attempts: dict[str, int]

    def record(self, entry: Entry) -> None:
        """Fold a just-appended entry into the state, as a re-read would.

        **Call this only after the append has actually succeeded.** The state is
        what decides whether a later target is already held, so an entry recorded
        before its provenance is durable would make the run trust bytes the
        manifest does not vouch for — the one thing the whole evidence contract
        rests on not happening.

        This exists because one invocation can hold the same URL twice: two
        securities can share a filer, so `AIZ` and `AIZN`, or a live row and its
        delisted twin, produce identical submissions and filing URLs. Without
        this the second occurrence saw a snapshot taken before the first was
        written and fetched the same bytes again — 46 wasted requests in chunk 1
        alone. A `FAILED` or `SKIPPED` entry is recorded too, and stays
        untrusted, because `is_stored` is what confers trust rather than mere
        presence.

        Args:
            entry: The entry just appended to the log.
        """
        self.latest[entry.url] = entry

    def next_attempt(self, url: str) -> int:
        """Return the attempt number this fetch will be, and record it.

        Identical to `attempts_for(url) + 1` at the moment it is called — which
        is the whole requirement, since the harvest appends one entry per fetch
        and the old code re-counted the file each time.
        """
        attempt = self.attempts.get(url, 0) + 1
        self.attempts[url] = attempt
        return attempt


class Manifest:
    """An append-only provenance log.

    Control-plane data. The file holds artifact-plane keys in each record's
    `path`, but the file itself lives wherever the control root puts it and
    knows nothing about where those keys resolve.

    Two absences are kept apart, because confusing them is how a project
    silently starts over. A manifest whose directory exists but which has not
    been written yet is first-time initialisation: it reads as empty and the
    first append creates it. A manifest whose *directory* is gone is the
    control plane having vanished — an unmounted volume, a mistyped root — and
    reading it as empty would present a corpus of a hundred thousand vouched
    artifacts as a corpus of none. That raises instead.

    Args:
        path: The `.jsonl` file. Its directory must already exist — the caller
            gets one from a verified control root, so a missing directory here
            means the root vanished and must fail rather than be made.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """Where the log lives."""
        return self._path

    def _require_directory(self) -> None:
        """Refuse to read or write when the manifest's directory has gone."""
        if not self._path.parent.is_dir():
            message = (
                f"the directory holding the evidence manifest is gone: {self._path.parent} "
                "— the control root is unavailable, not empty"
            )
            raise ControlPlaneUnavailableError(message)

    def append(self, entry: Entry) -> None:
        """Add one entry, durably.

        Flushed and fsynced before returning, so an entry this call reported as
        written survives losing power on the next one. Creates the file on
        first use; never creates its directory.

        Raises:
            ControlPlaneUnavailableError: If the manifest's directory is gone.
        """
        self._require_directory()
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(entry.as_line() + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def entries(self) -> Iterator[Entry]:
        """Yield every readable entry, oldest first.

        A truncated final line is discarded with a warning rather than raising:
        it is the signature of a crash during append, and the entries before it
        are intact and worth keeping. A manifest not yet written yields nothing.

        Raises:
            ControlPlaneUnavailableError: If the manifest's directory is gone —
                that is not an empty log, and is never reported as one.
        """
        self._require_directory()
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload: dict[str, Any] = json.loads(text)
                except json.JSONDecodeError:
                    log.warning(
                        "discarding an unreadable manifest line",
                        path=str(self._path),
                        line=number,
                    )
                    continue
                entry = _entry_from(payload)
                if entry is not None:
                    yield entry

    def replay(self) -> ManifestState:
        """Read the log once and return everything a harvest needs from it.

        Both answers come from a single pass on purpose. `latest_by_url` and
        `attempts_for` each walk the whole file, and `attempts_for` was being
        called once per target — a full re-read and re-parse per fetch, which is
        quadratic in the number of targets. Measured at 100,000 entries one such
        lookup costs 0.6 s, so a full-corpus run would have spent well over a
        hundred hours doing nothing but re-reading its own manifest.

        The counts this returns are a starting point, not a final tally: a
        harvest appends as it goes, so the caller increments the map when it
        writes an entry. That is what keeps the numbers identical to the old
        per-target scan, including when one run holds the same URL twice.

        Returns:
            The last entry per URL, and how many entries each URL already has.
        """
        latest: dict[str, Entry] = {}
        attempts: dict[str, int] = {}
        for entry in self.entries():
            latest[entry.url] = entry
            attempts[entry.url] = attempts.get(entry.url, 0) + 1
        return ManifestState(latest=latest, attempts=attempts)

    def latest_by_url(self) -> dict[str, Entry]:
        """Fold the history into the last thing said about each URL.

        Later entries win, which is what makes a retry after a failure read as
        success rather than leaving both.
        """
        latest: dict[str, Entry] = {}
        for entry in self.entries():
            latest[entry.url] = entry
        return latest

    def attempts_for(self, url: str) -> int:
        """How many entries the log already holds for one URL.

        A standalone lookup that reads the whole file. Correct, and fine for a
        caller asking about one URL; a harvest uses `replay` instead, because
        asking this once per target is what made the pass quadratic.
        """
        return sum(1 for entry in self.entries() if entry.url == url)


def _entry_from(payload: dict[str, Any]) -> Entry | None:
    """Rebuild an entry from a parsed line, or None when it is not one.

    Unknown keys are dropped rather than raising, so a manifest written by a
    later revision carrying extra fields still reads here.
    """
    known = set(Entry.__dataclass_fields__)
    fields = {k: v for k, v in payload.items() if k in known}
    if "url" not in fields or "outcome" not in fields:
        return None
    try:
        fields["outcome"] = Outcome(fields["outcome"])
    except ValueError:
        return None
    return Entry(**fields)
