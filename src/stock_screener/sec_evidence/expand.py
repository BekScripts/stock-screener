"""Turning securities into the SEC documents that actually constitute evidence.

Phase 9K.14 harvested 25 submissions indexes and proved the writer correct. It
also exposed what the harvest could not yet do: a submissions index is a
*catalogue* — a list of a filer's accession numbers — and the corpus this
project needs is the filing **documents** the catalogue points at. Nothing
turned one into the other, and three quarters of the sample produced no target
at all because the endpoint is keyed by CIK and most securities carried none.

This module closes both gaps, reusing what already exists rather than restating
it: `SecEdgarFundamentals.cik_for` is the ticker→CIK map the scanner already
loads, and `api_clients.filings_from_submissions` is the index parser
`get_filings` already uses. Neither is reimplemented here.

## What is deliberately not done

**No CIK is invented.** A live ticker the SEC's own file does not carry stays
unresolved, and so does a delisted security whose CIK the free record never
established. Dotted class tickers (`BC.PRC`) do not match the SEC's dashed form
(`BC-PRC`) and are left alone: translating between them would be a guess about
which security is meant, and a wrong CIK is worse than none.

**Expansion is bounded before it is issued.** A single long-established filer
lists a thousand recent accessions. `filings_per_security` caps how many become
targets, and the cap is applied while building the list — not by stopping a run
that has already started fetching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from api_clients import filings_from_submissions, submissions_url
from stock_screener.sec_evidence.harvest import Target
from stock_screener.sec_evidence.storage import RequestKind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.sample import Selection
    from stock_screener.sec_evidence.storage import EvidenceRoot

log = structlog.get_logger(__name__)

#: How many filings one security contributes when expansion is enabled. Small
#: on purpose: this exists to prove the chain, not to acquire a corpus.
DEFAULT_FILINGS_PER_SECURITY = 2


class CikResolver(Protocol):
    """Anything that can map a ticker to a filer number.

    `SecEdgarFundamentals` is the real one. A protocol keeps the seam narrow
    enough for a test to resolve without loading the SEC's ticker file.
    """

    def cik_for(self, ticker: str) -> int | None:
        """Return the filer number for a ticker, or None when unmapped."""
        ...


@dataclass(frozen=True)
class Resolution:
    """One security's identifier, and where it came from.

    Attributes:
        selection: The sampled security.
        cik: The filer number, or None when nothing established one.
        source: `STORED` for a delisted security's own CIK, `EDGAR` for one the
            SEC's ticker file supplied, `UNRESOLVED` when neither did.
    """

    selection: Selection
    cik: int | None
    source: str

    @property
    def resolved(self) -> bool:
        """Whether a filer number was established."""
        return self.cik is not None


def resolve_ciks(
    selections: Sequence[Selection], resolver: CikResolver | None
) -> tuple[Resolution, ...]:
    """Attach a filer number to each security, where one can be established.

    A delisted security's stored CIK wins: it was established against that
    security's own record, while the SEC's ticker file describes whoever holds
    the symbol *now* — the `BBBY` problem, and the reason the order is this way
    round rather than the other.

    Args:
        selections: The sampled securities.
        resolver: The ticker→CIK source. None resolves stored CIKs only, which
            is what a dry run uses to avoid fetching the ticker file.

    Returns:
        One resolution per selection, in order.
    """
    out: list[Resolution] = []
    for selection in selections:
        if selection.cik is not None:
            out.append(Resolution(selection, selection.cik, "STORED"))
            continue
        found = resolver.cik_for(selection.symbol) if resolver is not None else None
        out.append(Resolution(selection, found, "EDGAR" if found is not None else "UNRESOLVED"))
    log.info(
        "cik resolution",
        securities=len(out),
        stored=sum(1 for r in out if r.source == "STORED"),
        edgar=sum(1 for r in out if r.source == "EDGAR"),
        unresolved=sum(1 for r in out if not r.resolved),
    )
    return tuple(out)


def submissions_targets(resolutions: Sequence[Resolution]) -> list[Target]:
    """One submissions-index target per resolved security."""
    return [
        Target(url=submissions_url(r.cik), kind=RequestKind.SUBMISSIONS, cik=r.cik)
        for r in resolutions
        if r.cik is not None
    ]


def filing_targets(
    root: EvidenceRoot,
    resolutions: Sequence[Resolution],
    *,
    filings_per_security: int = DEFAULT_FILINGS_PER_SECURITY,
) -> list[Target]:
    """Expand stored submissions indexes into bounded filing-document targets.

    Reads the submissions artifacts already on disk — expansion issues no
    request, so a security whose index has not been harvested yet simply
    contributes nothing rather than triggering a fetch as a side effect.

    Args:
        root: The verified evidence root.
        resolutions: Securities with their filer numbers.
        filings_per_security: Cap per security, applied while building.

    Returns:
        Targets naming actual filing documents, newest filing first. A filing
        whose index names no primary document is skipped: its URL would be the
        filing's index page, which is a listing rather than the document.
    """
    if filings_per_security <= 0:
        return []

    targets: list[Target] = []
    for resolution in resolutions:
        cik = resolution.cik
        if cik is None:
            continue
        stored = root.submissions_path(cik)
        if not stored.is_file():
            continue
        payload = _payload(stored)
        if payload is None:
            continue
        for filing in filings_from_submissions(payload, cik)[:filings_per_security]:
            if not filing.primary_document:
                continue
            targets.append(
                Target(
                    url=filing.url,
                    kind=RequestKind.FILING,
                    cik=cik,
                    accession=filing.accession,
                    document=filing.primary_document,
                    form=filing.form,
                    filing_date=filing.filed.isoformat(),
                )
            )
    log.info(
        "filing target expansion",
        securities=len(resolutions),
        targets=len(targets),
        cap=filings_per_security,
    )
    return targets


def _payload(path: Path) -> dict[str, Any] | None:
    """Parse a stored submissions document, or None when it is not usable.

    A stored artifact that will not parse is a finding, not a crash: it is
    reported and skipped, so one damaged index cannot end an expansion pass.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("stored submissions document is unreadable", path=str(path), error=str(exc))
        return None
    return raw if isinstance(raw, dict) else None
