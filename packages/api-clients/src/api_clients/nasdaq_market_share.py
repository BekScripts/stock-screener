"""Reading Nasdaq's public monthly Market Share by Symbol files.

The files are published at `nasdaqtrader.com` in two families — `NASDAQ*` and
`Listed*` — one file per family per month, from 2006. They are the only free
record of historical listing venue this project has found, and Phase 9K
established both what they carry and the ways they mislead.

**They are parsed with the standard library.** A `.xlsx` is a zip of XML, and
reading it here directly avoids adding an Excel dependency to the workspace for
one monthly file. Only `.xlsx` is read; the pre-2016-10 `.xls` files are a
different, binary format and are out of scope rather than silently skipped —
`SUPPORTED_FROM` in `domain.listing` is 2022-01 in any case.

**Nothing here decides what a file means.** This module parses bytes and reports
the symbols and the digest; `domain.listing.classify_tape` says which tape the
contents belong to and the application refuses a file whose tape is not the one
its month predicts. Keeping the judgement out of the reader is deliberate: the
rule is a domain rule and belongs under test with the rest of them.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from typing import TYPE_CHECKING
from xml.etree import ElementTree

import structlog

from api_clients.errors import ProviderDataError
from domain import SourceFamily

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

log = structlog.get_logger(__name__)

#: Where the files live. Recorded on every source row so a stored digest can be
#: checked against the thing it claims to describe.
BASE_URL = "https://www.nasdaqtrader.com/content/marketstatistics/marketshare"

#: Why `xml.etree` is used rather than `defusedxml`, which S314 asks for. The
#: input is one published file from `nasdaqtrader.com`, fetched over TLS and
#: checked against a stored SHA-256 before it is trusted for anything. The
#: attacks S314 guards against — entity expansion, external entity resolution —
#: need a hostile document author; here that would mean Nasdaq attacking this
#: process, at which point the listing data itself is the larger problem. Adding
#: a dependency to the workspace to parse one monthly spreadsheet is the cost
#: this note declines to pay, and the parser reads only `<v>`, `<c>`, `<row>` and
#: `<t>` elements, ignoring doctypes and entities entirely.
_XML_TRUST = "published Nasdaq file, digest-checked"

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_FILENAME = re.compile(r"^(NASDAQ|Listed)(\d{4})(\d{2})\.xlsx$", re.IGNORECASE)

#: The column the symbol is in, and the one consolidated volume is in. Both are
#: verified against the header before any row is read, because a layout change
#: that shifted them would otherwise import volumes as symbols.
_SYMBOL_COLUMN = 0
_CONSOLIDATED_COLUMN = 6
_EXPECTED_FIRST_HEADER = "issue"
_EXPECTED_CONSOLIDATED_HEADER = "consolidated volume"

#: Rows that label the sheet rather than name a security.
_NOT_SYMBOLS = frozenset({"issue", "total", ""})


@dataclass(frozen=True)
class MarketShareFile:
    """One parsed monthly file, with the provenance needed to re-verify it.

    Attributes:
        file_name: The published name, e.g. `NASDAQ202606.xlsx`.
        url: Where it was fetched from.
        family: Which published family it belongs to.
        month: The first day of its observation month, from the filename.
        sha256: Digest of the bytes as fetched.
        byte_size: How many bytes those were.
        rows: `{symbol: consolidated_volume}`, volume None where unparseable.
        skipped: Rows that were not securities — headers, totals, blanks.
    """

    file_name: str
    url: str
    family: SourceFamily
    month: date
    sha256: str
    byte_size: int
    rows: Mapping[str, float | None]
    skipped: int

    @property
    def symbols(self) -> frozenset[str]:
        """Every symbol the file names."""
        return frozenset(self.rows)

    @property
    def row_count(self) -> int:
        """How many securities the file names."""
        return len(self.rows)


def file_name_for(family: SourceFamily, month: date) -> str:
    """Return the published filename for a family and month."""
    stem = "NASDAQ" if family is SourceFamily.NASDAQ else "Listed"
    return f"{stem}{month.year:04d}{month.month:02d}.xlsx"


def url_for(family: SourceFamily, month: date) -> str:
    """Return the published URL for a family and month."""
    return f"{BASE_URL}/{month.year:04d}/{file_name_for(family, month)}"


def parse_file_name(name: str) -> tuple[SourceFamily, date]:
    """Return the family and observation month a published filename encodes.

    Args:
        name: The filename, e.g. `Listed202201.xlsx`.

    Returns:
        The family and the first day of its month.

    Raises:
        ProviderDataError: If the name is not one this module publishes for. The
            importer refuses unrecognised names rather than guessing, so a file
            from somewhere else cannot enter the record.
    """
    match = _FILENAME.match(name.strip())
    if match is None:
        raise ProviderDataError(f"{name!r} is not a Nasdaq market-share filename")
    stem, year, month = match.groups()
    family = SourceFamily.NASDAQ if stem.upper() == "NASDAQ" else SourceFamily.LISTED
    return family, date(int(year), int(month), 1)


def parse_market_share(name: str, payload: bytes, *, url: str | None = None) -> MarketShareFile:
    """Parse one published monthly file.

    Args:
        name: The published filename, which supplies the family and month.
        payload: The file's bytes exactly as fetched.
        url: Where it came from; defaults to the published location.

    Returns:
        The parsed file with its digest.

    Raises:
        ProviderDataError: If the name is unrecognised, the payload is not a
            readable workbook, or the header is not the layout this reader
            knows. A shifted column is a silent corruption otherwise.
    """
    family, month = parse_file_name(name)
    digest = hashlib.sha256(payload).hexdigest()
    rows: dict[str, float | None] = {}
    skipped = 0
    for cells in _rows(payload, name):
        if not cells:
            skipped += 1
            continue
        symbol = cells[_SYMBOL_COLUMN].strip().upper() if cells[_SYMBOL_COLUMN] else ""
        if symbol.lower() in _NOT_SYMBOLS or symbol.lower().startswith("file creation"):
            skipped += 1
            continue
        rows[symbol] = _as_float(
            cells[_CONSOLIDATED_COLUMN] if len(cells) > _CONSOLIDATED_COLUMN else None
        )
    if not rows:
        raise ProviderDataError(f"{name} named no securities")
    log.debug("parsed market share file", file=name, rows=len(rows), skipped=skipped)
    return MarketShareFile(
        file_name=name,
        url=url or url_for(family, month),
        family=family,
        month=month,
        sha256=digest,
        byte_size=len(payload),
        rows=rows,
        skipped=skipped,
    )


def _rows(payload: bytes, name: str) -> Iterator[list[str]]:
    """Yield each worksheet row as a list of cell strings, header checked first."""
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
        shared = _shared_strings(archive)
        sheets = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
        if not sheets:
            raise ProviderDataError(f"{name} contains no worksheet")
        root = ElementTree.fromstring(  # noqa: S314 — see _XML_TRUST
            archive.read(sorted(sheets)[0])
        )
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ProviderDataError(f"{name} is not a readable workbook: {exc}") from exc

    checked = False
    for row in root.iterfind(f".//{_SHEET_NS}sheetData/{_SHEET_NS}row"):
        cells = [_cell(c, shared) for c in row.iterfind(f"{_SHEET_NS}c")]
        if not checked and cells:
            _check_header(cells, name)
            checked = True
            continue
        yield cells


def _check_header(cells: list[str], name: str) -> None:
    """Refuse a file whose columns are not where this reader expects them."""
    first = cells[_SYMBOL_COLUMN].strip().lower() if cells else ""
    consolidated = (
        cells[_CONSOLIDATED_COLUMN].strip().lower().replace("\n", " ").replace("  ", " ")
        if len(cells) > _CONSOLIDATED_COLUMN
        else ""
    )
    if first != _EXPECTED_FIRST_HEADER or _EXPECTED_CONSOLIDATED_HEADER not in consolidated:
        raise ProviderDataError(
            f"{name} has an unexpected layout: first column {first!r}, "
            f"column {_CONSOLIDATED_COLUMN} {consolidated!r}"
        )


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Return the workbook's shared string table, which cells reference by index."""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(  # noqa: S314 — see _XML_TRUST
        archive.read("xl/sharedStrings.xml")
    )
    return [
        "".join(t.text or "" for t in si.iter(f"{_SHEET_NS}t"))
        for si in root.iterfind(f"{_SHEET_NS}si")
    ]


def _cell(element: ElementTree.Element, shared: list[str]) -> str:
    """Return one cell's text, resolving a shared-string reference."""
    value = element.find(f"{_SHEET_NS}v")
    if value is None or value.text is None:
        return ""
    if element.get("t") == "s":
        index = int(value.text)
        return shared[index] if 0 <= index < len(shared) else ""
    return value.text


def _as_float(value: str | None) -> float | None:
    """Return a cell as a number, or None where it is not one. Never zero."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
