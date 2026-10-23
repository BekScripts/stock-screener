"""Folding the SEC fails-to-deliver history into identity evidence.

The published record is 433 archives, about 600 MB, and tens of millions of
lines. Its *identity* content is far smaller: one row per distinct
`(cusip, symbol, issuer_name)` triple, with the settlement window it was seen
over and the count behind it. This module reads the first and writes the second.

Three properties govern the ingest, and each is a decision rather than a detail:

**It aggregates rather than transcribes.** Storing every published line would add
tens of millions of rows carrying a failure quantity this project has no use for,
to answer a question — *who was this symbol* — that the aggregate answers exactly
as well. What is preserved instead is everything needed to go back: each archive's
name, URL and SHA-256 in `ftd_source_files`, and a digest of each triple beside
the aggregate. The lines are reproducible from the files; the files are pinned by
their digests.

**It is resumable, and re-running writes nothing new.** Archives already recorded
are skipped by name, and the triples upsert — widening the window and adding to
the count — so a run interrupted at file 300 continues at 301, and a complete
re-run over the same evidence changes no window and no total.

**It never concludes anything.** No security is created here and no symbol is
resolved. This module writes down what the files say; `resolution` decides what
it means, and can be re-run against the same evidence when the rules change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from api_clients.errors import ProviderError
from data_access import (
    FtdIdentityObservationRepository,
    FtdSourceFileRepository,
    session_scope,
)
from domain import cusip_issuer_base, is_valid_cusip

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date

    from sqlalchemy.orm import Session, sessionmaker

    from api_clients import FtdArchive, FtdFileContent, SecFailsToDeliver

log = structlog.get_logger(__name__)


def evidence_hash(cusip: str, symbol: str, issuer_name: str) -> str:
    """Digest one identity triple, so an interval can name the evidence behind it.

    Args:
        cusip: The identifier as printed.
        symbol: The symbol as printed.
        issuer_name: The description field.

    Returns:
        A hex SHA-256 over the three, separated by a character none of them can
        contain, so two different triples cannot collide by concatenation.
    """
    return hashlib.sha256(f"{cusip}\x1f{symbol}\x1f{issuer_name}".encode()).hexdigest()


@dataclass
class _Aggregate:
    """One triple's running window and count, while a file is being folded in."""

    first: date
    last: date
    count: int


@dataclass(frozen=True)
class FtdFileResult:
    """What one archive contributed.

    Attributes:
        name: The archive's file name.
        rows: How many published lines it held.
        triples: How many distinct identity triples those lines carried.
        skipped: Lines that were not data rows.
        first_settlement: Earliest settlement date in the file, or None.
        last_settlement: Latest settlement date in the file, or None.
        already_read: True when the archive was recorded by an earlier run and
            was not re-read.
        error: Why the archive could not be read, or None.
    """

    name: str
    rows: int = 0
    triples: int = 0
    skipped: int = 0
    first_settlement: date | None = None
    last_settlement: date | None = None
    already_read: bool = False
    error: str | None = None

    def summary(self) -> str:
        """One line for a progress display."""
        if self.error is not None:
            return f"{self.name}: FAILED — {self.error}"
        if self.already_read:
            return f"{self.name}: already read"
        window = (
            f" {self.first_settlement}..{self.last_settlement}"
            if self.first_settlement is not None
            else ""
        )
        return f"{self.name}: {self.rows:,} rows, {self.triples:,} triples{window}"


@dataclass(frozen=True)
class FtdIngestReport:
    """What a whole ingest run did, in the terms the Phase 9A audit asks for.

    Attributes:
        results: One entry per archive, in the order processed.
    """

    results: tuple[FtdFileResult, ...] = field(default_factory=tuple)

    @property
    def processed(self) -> int:
        """Archives actually read this run."""
        return sum(1 for r in self.results if not r.already_read and r.error is None)

    @property
    def skipped(self) -> int:
        """Archives an earlier run had already recorded."""
        return sum(1 for r in self.results if r.already_read)

    @property
    def failed(self) -> tuple[FtdFileResult, ...]:
        """Archives that could not be read."""
        return tuple(r for r in self.results if r.error is not None)

    @property
    def rows(self) -> int:
        """Published lines read this run."""
        return sum(r.rows for r in self.results)

    @property
    def first_settlement(self) -> date | None:
        """Earliest settlement date seen this run."""
        return min(
            (r.first_settlement for r in self.results if r.first_settlement is not None),
            default=None,
        )

    @property
    def last_settlement(self) -> date | None:
        """Latest settlement date seen this run."""
        return max(
            (r.last_settlement for r in self.results if r.last_settlement is not None),
            default=None,
        )

    def summary(self) -> str:
        """One line describing the whole run."""
        window = (
            f", {self.first_settlement}..{self.last_settlement}"
            if self.first_settlement is not None
            else ""
        )
        return (
            f"ftd-ingest: {self.processed} archives read, {self.skipped} already recorded, "
            f"{len(self.failed)} failed, {self.rows:,} rows{window}"
        )


def _aggregate(content: FtdFileContent) -> dict[tuple[str, str, str], _Aggregate]:
    """Fold one archive's lines into its distinct identity triples."""
    aggregates: dict[tuple[str, str, str], _Aggregate] = {}
    for row in content.rows:
        key = (row.cusip, row.symbol, row.issuer_name)
        existing = aggregates.get(key)
        if existing is None:
            aggregates[key] = _Aggregate(
                first=row.settlement_date, last=row.settlement_date, count=1
            )
            continue
        if row.settlement_date < existing.first:
            existing.first = row.settlement_date
        if row.settlement_date > existing.last:
            existing.last = row.settlement_date
        existing.count += 1
    return aggregates


def ingest_ftd_file(
    session_factory: sessionmaker[Session], content: FtdFileContent
) -> FtdFileResult:
    """Fold one archive into the evidence tables, in its own transaction.

    One transaction per archive so a run killed mid-history leaves every
    completed file recorded and the file in flight absent — the `ftd_source_files`
    record *is* the cursor.

    **A file contributes exactly once.** The triples accumulate their counts
    across files, which is what folding 433 archives into one aggregate requires
    — and which would double-count a file read twice. The stored SHA-256 is the
    guard: an archive whose bytes are already recorded is skipped here, so this
    function is idempotent on its own and does not rely on the caller's
    name-based skip to be safe.

    Args:
        session_factory: How to open a session.
        content: The parsed archive.

    Returns:
        What the archive contributed, or an `already_read` result when these
        exact bytes were folded in before.
    """
    with session_scope(session_factory) as session:
        if FtdSourceFileRepository(session).digest_for(content.name) == content.sha256:
            return FtdFileResult(name=content.name, already_read=True)

    aggregates = _aggregate(content)
    rows = [
        {
            "cusip": cusip,
            "issuer_base": cusip_issuer_base(cusip),
            "symbol": symbol,
            "issuer_name": issuer_name,
            "first_settlement_date": aggregate.first,
            "last_settlement_date": aggregate.last,
            "observation_count": aggregate.count,
            "cusip_valid": is_valid_cusip(cusip),
            "first_source_file": content.name,
            "last_source_file": content.name,
            "evidence_hash": evidence_hash(cusip, symbol, issuer_name),
        }
        for (cusip, symbol, issuer_name), aggregate in aggregates.items()
    ]

    with session_scope(session_factory) as session:
        FtdIdentityObservationRepository(session).merge_all(rows)
        FtdSourceFileRepository(session).upsert(
            content.name,
            url=content.url,
            sha256=content.sha256,
            byte_size=content.byte_size,
            row_count=len(content.rows),
            skipped_lines=content.skipped,
            first_settlement_date=content.first_settlement,
            last_settlement_date=content.last_settlement,
        )

    return FtdFileResult(
        name=content.name,
        rows=len(content.rows),
        triples=len(rows),
        skipped=content.skipped,
        first_settlement=content.first_settlement,
        last_settlement=content.last_settlement,
    )


def ingest_ftd_history(
    session_factory: sessionmaker[Session],
    source: SecFailsToDeliver,
    *,
    archives: Sequence[FtdArchive] | None = None,
    refresh: bool = False,
    on_progress: Callable[[FtdFileResult, int, int], None] | None = None,
) -> FtdIngestReport:
    """Read the published fails-to-deliver history into the evidence tables.

    Args:
        session_factory: How to open a session.
        source: The SEC adapter.
        archives: The archives to read. Defaults to every published one.
        refresh: Re-read archives an earlier run already recorded. Off by
            default: the files are immutable once published, so a second read
            costs 600 MB of a free public server's bandwidth to learn nothing.
        on_progress: Called with each file's result, its index and the total.

    Returns:
        What the run did, per archive.
    """
    published = tuple(archives) if archives is not None else source.list_archives()
    with session_scope(session_factory) as session:
        already = FtdSourceFileRepository(session).processed_names()

    results: list[FtdFileResult] = []
    total = len(published)
    for index, archive in enumerate(published, start=1):
        if not refresh and archive.name in already:
            result = FtdFileResult(name=archive.name, already_read=True)
        else:
            try:
                content = source.fetch(archive)
            except (ProviderError, OSError) as exc:
                log.warning("ftd.archive_failed", archive=archive.name, error=str(exc))
                result = FtdFileResult(name=archive.name, error=f"{type(exc).__name__}: {exc}")
            else:
                result = ingest_ftd_file(session_factory, content)
        results.append(result)
        if on_progress is not None:
            on_progress(result, index, total)

    return FtdIngestReport(results=tuple(results))
