"""Importing monthly market-share files into the listing evidence record.

The importer's one job beyond persistence is to **refuse a file whose meaning
has changed**. Phase 9K found that the `Listed*` family silently switched from
Tape B (NYSE Arca, NYSE American, regional) to Tape A (NYSE) at 2022-01, and
that one month inside the earlier era is Tape A anyway. A calendar rule alone
would therefore mislabel at least one month, and a family-name rule would
mislabel five years of them.

So every file is checked twice: `domain.tape_for` predicts a tape from the month,
`domain.classify_tape` reads one out of the file's own contents, and only a file
where the two agree is imported. A disagreement is stored as a `QUARANTINED`
source row carrying both tapes and the reason — the record of what was refused is
as much a part of the evidence as what was accepted.

Ingestion is idempotent by construction. Observations are unique on
`(symbol, venue, observation_month)` and source files on `file_name`, so a
re-run over the same corpus inserts nothing and changes nothing.

Nothing here attributes identity; `attribution` does that separately, because a
ticker is not a security and the resolution needs a date.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from api_clients.nasdaq_market_share import parse_market_share
from data_access.models import ListingObservation, ListingSourceFile
from domain import (
    SUPPORTED_FROM,
    ListingEvidenceState,
    Tape,
    classify_tape,
    month_start,
    tape_for,
    venue_for_tape,
)

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)

#: A file that was read and whose contents matched its predicted tape.
IMPORTED = "IMPORTED"

#: A file that was read and refused. The row is kept; the observations are not.
QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class FileResult:
    """What importing one file did.

    Attributes:
        file_name: The published name.
        status: `IMPORTED` or `QUARANTINED`.
        venue: The venue its tape established, where it established one.
        rows: Securities the file named.
        inserted: Observations newly written; zero on a re-run.
        reason: Why it was refused, for a quarantined file.
    """

    file_name: str
    status: str
    venue: str | None
    rows: int
    inserted: int
    reason: str | None = None


@dataclass(frozen=True)
class IngestReport:
    """What importing a corpus did.

    Attributes:
        results: One entry per file offered.
    """

    results: tuple[FileResult, ...] = field(default_factory=tuple)

    @property
    def imported(self) -> tuple[FileResult, ...]:
        """Files whose tape matched."""
        return tuple(r for r in self.results if r.status == IMPORTED)

    @property
    def quarantined(self) -> tuple[FileResult, ...]:
        """Files read and refused.

        **Not an error.** Every Tape B file in the corpus lands here, which is
        the whole point of the check.
        """
        return tuple(r for r in self.results if r.status == QUARANTINED)

    @property
    def inserted(self) -> int:
        """Observations newly written across the run."""
        return sum(r.inserted for r in self.results)

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{len(self.results)} files — {len(self.imported)} imported, "
            f"{len(self.quarantined)} quarantined, {self.inserted} observations written"
        )


def ingest_file(
    session: Session,
    name: str,
    payload: bytes,
    *,
    url: str | None = None,
    supported_from: date = SUPPORTED_FROM,
) -> FileResult:
    """Import one monthly file, or refuse it and record why.

    Args:
        session: An open session.
        name: The published filename, which supplies the family and month.
        payload: The file's bytes exactly as fetched.
        url: Where it came from.
        supported_from: The earliest month this project accepts.

    Returns:
        What the file did.
    """
    parsed = parse_market_share(name, payload, url=url)
    existing = session.execute(
        select(ListingSourceFile).where(ListingSourceFile.file_name == parsed.file_name)
    ).scalar_one_or_none()
    if existing is not None:
        return FileResult(
            file_name=parsed.file_name,
            status=existing.status,
            venue=existing.venue,
            rows=existing.row_count,
            inserted=0,
            reason=existing.reason,
        )

    expected = tape_for(parsed.family, parsed.month)
    observed = classify_tape(parsed.symbols)
    venue = venue_for_tape(expected)
    reason: str | None = None
    if parsed.month < month_start(supported_from):
        reason = f"month {parsed.month} precedes the supported window from {supported_from}"
    elif expected is not observed:
        reason = (
            f"tape mismatch: month predicts {expected}, contents read as {observed}. "
            "The file's meaning is not what its name and date imply."
        )
    elif venue is None:
        reason = f"tape {expected} establishes no usable venue (Arca/American/regional are pooled)"

    status = QUARANTINED if reason else IMPORTED
    source = ListingSourceFile(
        file_name=parsed.file_name,
        url=parsed.url,
        source_family=str(parsed.family),
        expected_tape=str(expected),
        observed_tape=str(observed),
        venue=str(venue) if venue is not None and status == IMPORTED else None,
        observation_month=parsed.month,
        sha256=parsed.sha256,
        byte_size=parsed.byte_size,
        row_count=parsed.row_count,
        skipped_rows=parsed.skipped,
        status=status,
        reason=reason,
    )
    session.add(source)
    session.flush()

    inserted = 0
    if status == IMPORTED and venue is not None:
        present = {
            symbol
            for (symbol,) in session.execute(
                select(ListingObservation.symbol).where(
                    (ListingObservation.venue == str(venue))
                    & (ListingObservation.observation_month == parsed.month)
                )
            )
        }
        for symbol, volume in parsed.rows.items():
            if symbol in present:
                continue
            session.add(
                ListingObservation(
                    symbol=symbol,
                    venue=str(venue),
                    observation_month=parsed.month,
                    evidence_state=str(ListingEvidenceState.MARKET_SHARE_OBSERVED),
                    consolidated_volume=volume,
                    source_file_id=source.id,
                )
            )
            inserted += 1
    session.flush()
    log.info(
        "listing source file",
        file=parsed.file_name,
        status=status,
        expected_tape=str(expected),
        observed_tape=str(observed),
        inserted=inserted,
    )
    return FileResult(
        file_name=parsed.file_name,
        status=status,
        venue=str(venue) if venue is not None and status == IMPORTED else None,
        rows=parsed.row_count,
        inserted=inserted,
        reason=reason,
    )


def ingest_files(
    session: Session,
    payloads: dict[str, bytes],
    *,
    supported_from: date = SUPPORTED_FROM,
) -> IngestReport:
    """Import a corpus of monthly files, in filename order.

    Args:
        session: An open session.
        payloads: `{filename: bytes}`.
        supported_from: The earliest month to accept.

    Returns:
        One result per file.
    """
    results = [
        ingest_file(session, name, payloads[name], supported_from=supported_from)
        for name in sorted(payloads)
    ]
    report = IngestReport(results=tuple(results))
    log.info("listing ingest", summary=report.summary())
    return report


def digest(payload: bytes) -> str:
    """SHA-256 of a payload, the same digest the source rows store."""
    return hashlib.sha256(payload).hexdigest()


def unknown_tape_files(session: Session) -> tuple[str, ...]:
    """Return the files refused because their contents matched no tape at all."""
    rows = session.execute(
        select(ListingSourceFile.file_name).where(
            ListingSourceFile.observed_tape == str(Tape.UNKNOWN)
        )
    )
    return tuple(name for (name,) in rows)
