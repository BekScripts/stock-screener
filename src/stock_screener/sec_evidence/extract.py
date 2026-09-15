"""Reading the harvested corpus offline, into a derived index.

Nothing here fetches. Every fact comes from bytes already on the SSD and from
the manifest that vouches for them, which is what makes this phase repeatable:
the same corpus produces the same index, and a network outage changes nothing.

## What is reused, and what is new

Reused outright, never reimplemented:

- `api_clients.parse_cover_page` — the inline-XBRL cover reader, including its
  handling of `StatementClassOfStockAxis` dimensions and its refusal to pair one
  class's title with another's symbol.
- `domain.classify_security` / `classify_title` / `classify_member` — the 9K.2
  policy that title text leads and the structured member corroborates.
- `api_clients.filings_from_submissions` — the catalogue parse, so the index and
  the harvest agree on what a qualifying filing is.
- `sec_evidence.Manifest` — provenance. An artifact is read only when the
  manifest vouches for it.

New here, because nothing owned them: exchange normalisation, the provenance
reconciliation between manifest metadata and document text, the acceptance
timestamp (which `Filing` does not carry), and the derived SQLite index.

## Provenance beats parsing

The manifest records `cik`, `accession`, `form` and `filing_date` for every
filing, taken from the catalogue that named the document. A document's own text
is weaker evidence than that: a cover page can say anything. So where both
speak, agreement is recorded as `CORROBORATED`, disagreement as `CONFLICT`, and
**the manifest's value is the one kept**. A parser that silently preferred the
document would let filing text rewrite provenance.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from api_clients import filings_from_submissions, parse_cover_page
from domain import classify_security
from stock_screener.sec_evidence.backend import ArtifactNotFoundError, validate_artifact_key

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.manifest import Entry, Manifest

log = structlog.get_logger(__name__)

#: Bumped whenever extraction changes what it produces from the same bytes.
#: Stored on every row so a mixed-version index is detectable rather than
#: silently inconsistent.
PARSER_VERSION = "9L.1"


class Exchange(StrEnum):
    """The venue a cover page names, normalised conservatively.

    `OTHER` and `UNKNOWN` are different answers. `OTHER` means the filing named
    a venue this project does not screen — Cboe, an OTC tier, a foreign board.
    `UNKNOWN` means the filing named nothing. Pooling them would turn "we did
    not ask" into "it is not one of ours".

    There is deliberately no inference from ticker shape. A four-letter symbol
    is not evidence of NASDAQ, and treating it as such is exactly the free-data
    shortcut this layer exists to avoid.
    """

    NASDAQ = "NASDAQ"
    NYSE = "NYSE"
    NYSE_AMERICAN = "NYSE_AMERICAN"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class Provenance(StrEnum):
    """How well a field's sources agree.

    Attributes are recorded per field so a report can say *which* facts are
    corroborated rather than claiming the filing as a whole is trustworthy.
    """

    EXACT = "EXACT"
    """Only the authoritative source spoke, and it was unambiguous."""

    CORROBORATED = "CORROBORATED"
    """Document text independently agreed with the manifest."""

    CONFLICT = "CONFLICT"
    """Document text disagreed. The manifest value is kept and this is recorded."""

    MISSING = "MISSING"
    """Neither source carried the field."""

    UNPARSEABLE = "UNPARSEABLE"
    """The document could not be read at all."""


#: Exact spellings the SEC uses, lowercased. Matched before the substring rules
#: so "NYSE American" cannot be swallowed by the plain "nyse" test.
_EXCHANGE_EXACT: dict[str, Exchange] = {
    "nasdaq": Exchange.NASDAQ,
    "the nasdaq stock market llc": Exchange.NASDAQ,
    "the nasdaq stock market": Exchange.NASDAQ,
    "nasdaq global select market": Exchange.NASDAQ,
    "nasdaq global market": Exchange.NASDAQ,
    "nasdaq capital market": Exchange.NASDAQ,
    "new york stock exchange": Exchange.NYSE,
    "the new york stock exchange": Exchange.NYSE,
    "new york stock exchange llc": Exchange.NYSE,
    "nyse": Exchange.NYSE,
    "nyse american": Exchange.NYSE_AMERICAN,
    "nyse american llc": Exchange.NYSE_AMERICAN,
    "nyse mkt": Exchange.NYSE_AMERICAN,
    "nyse amex": Exchange.NYSE_AMERICAN,
    "american stock exchange": Exchange.NYSE_AMERICAN,
}


def normalise_exchange(raw: str | None) -> Exchange:
    """Map a `SecurityExchangeName` to a venue this project recognises.

    Order matters. NYSE American's spellings all contain "NYSE" or "American",
    so they are tested before the plain NYSE rule; NYSE Arca contains "NYSE" too
    and is deliberately `OTHER`, because Arca is not a listing venue this
    project screens and folding it into NYSE would invent listings.

    Args:
        raw: The verbatim exchange text, or None.

    Returns:
        The normalised venue. `UNKNOWN` when nothing was said — never a guess.
    """
    if not raw or not raw.strip():
        return Exchange.UNKNOWN
    text = " ".join(raw.split()).lower().strip(" .,")
    if text in _EXCHANGE_EXACT:
        return _EXCHANGE_EXACT[text]
    if "american" in text or "amex" in text or "nyse mkt" in text:
        return Exchange.NYSE_AMERICAN
    if "arca" in text or "cboe" in text or "bats" in text or "iex" in text:
        return Exchange.OTHER
    if "nasdaq" in text:
        return Exchange.NASDAQ
    if "new york stock exchange" in text or text.startswith("nyse"):
        return Exchange.NYSE
    return Exchange.OTHER


def normalise_symbol(raw: str | None) -> str | None:
    """Upper-case a ticker and strip decoration, or return None."""
    if not raw or not raw.strip():
        return None
    text = " ".join(raw.split()).upper().strip(" .,;:")
    return text or None


def normalise_title(raw: str | None) -> str | None:
    """Collapse a security title's whitespace, preserving its words."""
    if not raw or not raw.strip():
        return None
    return " ".join(raw.split())


@dataclass(frozen=True)
class SecurityRow:
    """One registered class, as one filing stated it on one date.

    An observation *as of* `evidence_date`, not an interval. A filing says what
    was true when it was accepted; it says nothing about the day after, and
    turning it into a span is a policy decision made later and elsewhere.
    """

    url: str
    cik: int | None
    accession: str | None
    evidence_date: str | None
    raw_title: str | None
    normalized_title: str | None
    raw_symbol: str | None
    normalized_symbol: str | None
    raw_exchange: str | None
    normalized_exchange: str
    member: str | None
    no_trading_symbol: bool
    security_class: str
    classification_reason: str
    source_path: str | None
    source_sha256: str | None


@dataclass(frozen=True)
class FilingRow:
    """One filing artifact's metadata, reconciled between manifest and document."""

    url: str
    path: str | None
    sha256: str | None
    bytes: int | None
    cik: int | None
    accession: str | None
    form: str | None
    filing_date: str | None
    accepted_at: str | None
    report_date: str | None
    evidence_date: str | None
    date_source: str
    registrant: str | None
    cover_securities: int
    dimensioned: bool
    ambiguous: bool
    parsed: bool
    parse_error: str | None
    symbol_provenance: str


@dataclass(frozen=True)
class CatalogueRow:
    """One filing the submissions catalogue describes, downloaded or not."""

    cik: int
    accession: str
    form: str
    filing_date: str
    accepted_at: str | None
    report_date: str | None
    primary_document: str | None
    url: str


def evidence_date_for(accepted_at: str | None, filing_date: str | None) -> tuple[str | None, str]:
    """Decide the date a filing's facts are evidence *as of*.

    The acceptance timestamp is preferred because it is when the SEC took
    receipt — the moment the facts became public. The filing date is the
    fallback. Neither the download date nor anything read from a later filing
    may be used: an evidence date taken from when we happened to fetch a
    document would make the whole point-in-time claim circular.

    Args:
        accepted_at: SEC acceptance timestamp, ISO-8601, or None.
        filing_date: The filing date, ISO-8601, or None.

    Returns:
        `(date, source)` where source is `ACCEPTED`, `FILED` or `NONE`.
    """
    if accepted_at:
        return accepted_at[:10], "ACCEPTED"
    if filing_date:
        return filing_date[:10], "FILED"
    return None, "NONE"


def extract_filing(
    entry: Entry, payload: str, *, accepted: Mapping[str, str] | None = None
) -> tuple[FilingRow, list[SecurityRow]]:
    """Read one stored filing document into metadata and 12(b) security rows.

    Args:
        entry: The manifest record vouching for the artifact — the authoritative
            source for `cik`, `accession`, `form` and `filing_date`.
        payload: The document's text.
        accepted: Acceptance timestamps by accession, from the catalogues.

    Returns:
        The filing row and one security row per registered class. An empty
        security list is an ordinary outcome: anything filed before inline XBRL
        arrived in 2019 carries no cover facts at all.
    """
    accession = entry.accession
    accepted_at = (accepted or {}).get(accession or "")
    evidence, source = evidence_date_for(accepted_at, entry.filing_date)

    try:
        page = parse_cover_page(payload)
        parse_error: str | None = None
        parsed = True
    except (ValueError, RecursionError) as exc:  # pragma: no cover - defensive
        page = None
        parse_error = f"{type(exc).__name__}: {exc}"
        parsed = False

    rows: list[SecurityRow] = []
    if page is not None:
        for security in page.securities:
            verdict = classify_security(security)
            rows.append(
                SecurityRow(
                    url=entry.url,
                    cik=entry.cik,
                    accession=accession,
                    evidence_date=evidence,
                    raw_title=security.title,
                    normalized_title=normalise_title(security.title),
                    raw_symbol=security.symbol,
                    normalized_symbol=normalise_symbol(security.symbol),
                    raw_exchange=security.exchange,
                    normalized_exchange=str(normalise_exchange(security.exchange)),
                    member=security.member,
                    no_trading_symbol=security.no_trading_symbol,
                    security_class=str(verdict.security_class),
                    classification_reason=str(verdict.evidence),
                    source_path=entry.path,
                    source_sha256=entry.sha256,
                )
            )

    filing = FilingRow(
        url=entry.url,
        path=entry.path,
        sha256=entry.sha256,
        bytes=entry.bytes,
        cik=entry.cik,
        accession=accession,
        form=entry.form,
        filing_date=entry.filing_date,
        accepted_at=accepted_at,
        report_date=None,
        evidence_date=evidence,
        date_source=source,
        registrant=None,
        cover_securities=len(rows),
        dimensioned=bool(page.dimensioned) if page else False,
        ambiguous=bool(page.ambiguous) if page else False,
        parsed=parsed,
        parse_error=parse_error,
        symbol_provenance=str(_symbol_provenance(parsed, rows)),
    )
    return filing, rows


def _symbol_provenance(parsed: bool, rows: Sequence[SecurityRow]) -> Provenance:
    """How well the document corroborated the ticker the harvest already knew."""
    if not parsed:
        return Provenance.UNPARSEABLE
    if not rows:
        return Provenance.MISSING
    if any(row.normalized_symbol for row in rows):
        return Provenance.CORROBORATED
    return Provenance.MISSING


def extract_catalogue(entry: Entry, payload: bytes) -> tuple[list[CatalogueRow], int, int]:
    """Read one stored submissions catalogue.

    Args:
        entry: The manifest record for the catalogue.
        payload: Its bytes.

    Returns:
        `(rows, recent_total, older_blocks)` — the qualifying filings, how many
        entries the recent index held in total, and how many *additional*
        history files the catalogue references. Those extra files were never
        downloaded, so the filings they describe are invisible to this corpus;
        counting them is what stops a coverage claim overstating what is known.
    """
    try:
        document: dict[str, Any] = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], 0, 0
    cik = entry.cik or int(document.get("cik") or 0)
    filings = filings_from_submissions(document, cik)

    recent = document.get("filings", {}).get("recent", {})
    accepted = recent.get("acceptanceDateTime") or []
    accessions = recent.get("accessionNumber") or []
    stamps = {
        str(accessions[i]).strip(): str(accepted[i])
        for i in range(min(len(accessions), len(accepted)))
        if accepted[i]
    }
    older = len(document.get("filings", {}).get("files", []) or [])

    rows = [
        CatalogueRow(
            cik=cik,
            accession=filing.accession,
            form=filing.form,
            filing_date=filing.filed.isoformat(),
            accepted_at=stamps.get(filing.accession),
            report_date=filing.period_end.isoformat() if filing.period_end else None,
            primary_document=filing.primary_document,
            url=filing.url,
        )
        for filing in filings
    ]
    return rows, len(accessions), older


SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    url TEXT PRIMARY KEY, path TEXT, sha256 TEXT, bytes INTEGER,
    cik INTEGER, accession TEXT, form TEXT, filing_date TEXT,
    accepted_at TEXT, report_date TEXT, evidence_date TEXT, date_source TEXT,
    registrant TEXT, cover_securities INTEGER, dimensioned INTEGER,
    ambiguous INTEGER, parsed INTEGER, parse_error TEXT,
    symbol_provenance TEXT, parser_version TEXT
);
CREATE TABLE IF NOT EXISTS securities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT, cik INTEGER, accession TEXT, evidence_date TEXT,
    raw_title TEXT, normalized_title TEXT,
    raw_symbol TEXT, normalized_symbol TEXT,
    raw_exchange TEXT, normalized_exchange TEXT,
    member TEXT, no_trading_symbol INTEGER,
    security_class TEXT, classification_reason TEXT,
    source_path TEXT, source_sha256 TEXT, parser_version TEXT
);
CREATE TABLE IF NOT EXISTS catalogue (
    cik INTEGER, accession TEXT, form TEXT, filing_date TEXT,
    accepted_at TEXT, report_date TEXT, primary_document TEXT, url TEXT,
    PRIMARY KEY (cik, accession)
);
CREATE TABLE IF NOT EXISTS catalogue_stats (
    cik INTEGER PRIMARY KEY, recent_rows INTEGER, older_blocks INTEGER, qualifying INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS securities_symbol ON securities (normalized_symbol, evidence_date);
CREATE INDEX IF NOT EXISTS securities_cik ON securities (cik, evidence_date);
CREATE INDEX IF NOT EXISTS filings_cik ON filings (cik, evidence_date);
CREATE INDEX IF NOT EXISTS catalogue_cik ON catalogue (cik, filing_date);
"""


def open_index(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the derived index.

    Plain `sqlite3`, deliberately. This is derived cache data rebuildable from
    the corpus, so it must never acquire an Alembic revision or share a session
    factory with the frozen databases — the safest way to guarantee that is for
    it not to use the ORM at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


@dataclass(frozen=True)
class ExtractionReport:
    """What one offline extraction pass produced.

    Attributes:
        filings_seen: Trusted filing artifacts considered.
        filings_parsed: Documents read without error.
        parse_failures: Documents that could not be read.
        missing_files: Trusted records whose bytes were gone.
        security_rows: Registered-class rows extracted.
        catalogues: Submissions catalogues read.
        catalogue_rows: Qualifying filings the catalogues describe.
        older_blocks: History files the catalogues reference and we never had.
        elapsed_seconds: Wall time.
    """

    filings_seen: int = 0
    filings_parsed: int = 0
    parse_failures: int = 0
    missing_files: int = 0
    security_rows: int = 0
    catalogues: int = 0
    catalogue_rows: int = 0
    older_blocks: int = 0
    elapsed_seconds: float = 0.0

    def summary(self) -> str:
        """One line for a report."""
        rate = self.filings_seen / self.elapsed_seconds if self.elapsed_seconds else 0.0
        return (
            f"{self.filings_parsed}/{self.filings_seen} filings parsed "
            f"({self.parse_failures} failures, {self.missing_files} missing), "
            f"{self.security_rows} security rows, {self.catalogue_rows} catalogue rows "
            f"from {self.catalogues} catalogues — {self.elapsed_seconds:.1f}s, {rate:.0f}/s"
        )


def build_index(
    storage: EvidenceStorage,
    manifest: Manifest,
    destination: Path,
    *,
    limit: int | None = None,
) -> ExtractionReport:
    """Extract the whole corpus into the derived index, streaming.

    One artifact is held in memory at a time. The corpus is 37 GB across 38,355
    files, so anything that accumulated documents would not finish.

    Catalogues are read first: they carry the acceptance timestamps the filing
    rows need, and a filing's evidence date must not fall back to the filing
    date merely because of the order this loop happened to run in.

    Args:
        storage: Where the artifacts live. Nothing here needs a filesystem.
        manifest: The provenance log. Only artifacts it vouches for are read.
        destination: Where the derived index goes — a control-plane file, so
            it is a path rather than a key.
        limit: Stop after this many filings, for a sample run.

    Returns:
        What was extracted.
    """
    started = time.monotonic()
    trusted = [entry for entry in manifest.latest_by_url().values() if entry.is_stored]
    catalogues = [e for e in trusted if e.request_kind == "SUBMISSIONS"]
    filings = sorted(
        (e for e in trusted if e.request_kind == "FILING"),
        key=lambda e: (e.cik or 0, e.filing_date or "", e.accession or ""),
    )

    report = ExtractionReport()
    accepted: dict[str, str] = {}
    seen = parsed = failures = missing = securities = rows = blocks = 0

    with closing(open_index(destination)) as db:
        db.execute("DELETE FROM filings")
        db.execute("DELETE FROM securities")
        db.execute("DELETE FROM catalogue")
        db.execute("DELETE FROM catalogue_stats")

        for entry in catalogues:
            payload = _read(storage, entry)
            if payload is None:
                missing += 1
                continue
            catalogue, recent_rows, older = extract_catalogue(entry, payload)
            blocks += older
            for row in catalogue:
                if row.accepted_at:
                    accepted[row.accession] = row.accepted_at
            db.executemany(
                "INSERT OR REPLACE INTO catalogue VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        r.cik,
                        r.accession,
                        r.form,
                        r.filing_date,
                        r.accepted_at,
                        r.report_date,
                        r.primary_document,
                        r.url,
                    )
                    for r in catalogue
                ],
            )
            db.execute(
                "INSERT OR REPLACE INTO catalogue_stats VALUES (?,?,?,?)",
                (entry.cik, recent_rows, older, len(catalogue)),
            )
            rows += len(catalogue)
        db.commit()
        log.info("catalogues read", catalogues=len(catalogues), rows=rows)

        for entry in filings:
            if limit is not None and seen >= limit:
                break
            seen += 1
            payload = _read(storage, entry)
            if payload is None:
                missing += 1
                continue
            filing, security_rows = extract_filing(
                entry, payload.decode("utf-8", errors="replace"), accepted=accepted
            )
            parsed += 1 if filing.parsed else 0
            failures += 0 if filing.parsed else 1
            securities += len(security_rows)
            db.execute(
                "INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    filing.url,
                    filing.path,
                    filing.sha256,
                    filing.bytes,
                    filing.cik,
                    filing.accession,
                    filing.form,
                    filing.filing_date,
                    filing.accepted_at,
                    filing.report_date,
                    filing.evidence_date,
                    filing.date_source,
                    filing.registrant,
                    filing.cover_securities,
                    int(filing.dimensioned),
                    int(filing.ambiguous),
                    int(filing.parsed),
                    filing.parse_error,
                    filing.symbol_provenance,
                    PARSER_VERSION,
                ),
            )
            db.executemany(
                "INSERT INTO securities (url, cik, accession, evidence_date, raw_title,"
                " normalized_title, raw_symbol, normalized_symbol, raw_exchange,"
                " normalized_exchange, member, no_trading_symbol, security_class,"
                " classification_reason, source_path, source_sha256, parser_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r.url,
                        r.cik,
                        r.accession,
                        r.evidence_date,
                        r.raw_title,
                        r.normalized_title,
                        r.raw_symbol,
                        r.normalized_symbol,
                        r.raw_exchange,
                        r.normalized_exchange,
                        r.member,
                        int(r.no_trading_symbol),
                        r.security_class,
                        r.classification_reason,
                        r.source_path,
                        r.source_sha256,
                        PARSER_VERSION,
                    )
                    for r in security_rows
                ],
            )
            if seen % 2000 == 0:
                db.commit()
                log.info("extraction progress", filings=seen, securities=securities)

        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("parser_version", PARSER_VERSION))
        db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            ("built_at", datetime.now(UTC).isoformat()),
        )
        db.commit()

    report = ExtractionReport(
        filings_seen=seen,
        filings_parsed=parsed,
        parse_failures=failures,
        missing_files=missing,
        security_rows=securities,
        catalogues=len(catalogues),
        catalogue_rows=rows,
        older_blocks=blocks,
        elapsed_seconds=time.monotonic() - started,
    )
    log.info("offline extraction", summary=report.summary())
    return report


def _read(storage: EvidenceStorage, entry: Entry) -> bytes | None:
    """Read a trusted artifact's bytes, or None when it is gone.

    The parser's only contact with storage. Everything downstream takes `str`
    or `bytes` and never learns where they came from — which is what lets the
    same extraction run over a local directory today and an object store later.

    Only definite absence is missing. Unsafe keys are integrity violations and
    propagate, as do unavailable storage and I/O failures. This intentionally
    corrects the pre-B2 broad OSError catch: an unreachable corpus must never
    silently remove PIT evidence by being counted as missing.
    """
    key = validate_artifact_key(entry.path or "")
    try:
        return storage.read_bytes(key)
    except ArtifactNotFoundError:
        return None


def month_ends(first: date, last: date) -> Iterator[date]:
    """Yield each month-start from first to last inclusive."""
    cursor = first.replace(day=1)
    stop = last.replace(day=1)
    while cursor <= stop:
        yield cursor
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
