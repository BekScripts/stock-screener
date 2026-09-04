"""SEC fails-to-deliver adapter: two decades of dated CUSIP-to-symbol evidence.

The SEC publishes, twice a month since 2004, every security in which a delivery
failed to settle — settlement date, CUSIP, symbol, issuer description, quantity
and price. It exists to make short-selling failures visible, and it is being read
here for something incidental to that purpose: it is the only **free** record
that pairs a CUSIP with a trading symbol *on a date*, back to 2004, for names
that have long since left the market.

That is what makes it identity evidence. EDGAR's current ticker map only knows
what trades today; a delisting feed carries a symbol and no identifier at all.
This file carries the pairing at the time, which is exactly what a dated
identifier interval needs.

Its one asymmetry governs every use of it, and `domain.identity` encodes the
rule so this module cannot forget it: **a match is evidence, an absence is not.**
A security appears here only when someone failed to deliver it — and before
2008-09-16 only when the failing balance also cleared a reporting threshold. A
name absent from the entire twenty-two-year record may simply have settled
cleanly every day of its life.

Two shapes of archive make up the history, and both are handled here because
neither covers the whole span:

- `cnsp_sec_fails_YYYYqN.zip` — quarterly, 2004-Q1 through 2009-Q2, each holding
  one text member per month.
- `cnsfailsYYYYMM{a,b}.zip` — semi-monthly, 2009-07 onward, one member each.

The file list is read from the SEC's own page rather than generated from a date
pattern, because the paths are not derivable: the same series is served from four
different directories, and two months of 2025 sit under a path no other month
uses. Guessing them would silently skip files, and a skipped file is a security
this project would then have no evidence for.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import httpx
import structlog

from api_clients._http import RateLimiter, RetryPolicy, request_bytes, request_text
from api_clients.errors import ProviderDataError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

#: The page listing every fails-to-deliver archive. Read rather than pattern-matched.
FTD_INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"

#: Where a relative href on that page resolves against.
_SEC_ROOT = "https://www.sec.gov"

#: Both archive families, matched on the href of the index page. The alternation
#: is ordered longest-first so `cnsp_sec_fails` is never truncated to `cnsfails`.
_ARCHIVE_HREF = re.compile(r'href="(/[^"]*?/(cnsp_sec_fails_\w+?|cnsfails\w+?)\.zip)"', re.I)

#: The header line every member carries. Checked rather than assumed: a silently
#: reordered column would put a symbol where a CUSIP belongs.
_EXPECTED_HEADER = "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"

#: How many pipe-separated fields a data row has. Anything shorter is the file's
#: own trailer ("Trailer record count 57537"), not a record.
_FIELDS = 6

#: Encodings tried in order. Issuer names carry accented characters that are
#: cp1252 in the older files and utf-8 in the newer; neither covers both.
_ENCODINGS = ("utf-8", "cp1252")


@dataclass(frozen=True)
class FtdArchive:
    """One published fails-to-deliver archive, before it has been read.

    Attributes:
        name: The archive's file name, which is its stable identity across runs
            and the key an already-processed file is recognised by.
        url: Where to fetch it.
    """

    name: str
    url: str


@dataclass(frozen=True)
class FtdRow:
    """One published fails-to-deliver record, translated out of the vendor's shape.

    Only the identity fields are carried. Quantity and price are read to validate
    the row's shape and then dropped: this project reads the file for *who a
    symbol was*, and keeping a failure quantity it has no use for would invite
    someone to treat it as a signal.

    Attributes:
        settlement_date: The date the delivery failed.
        cusip: The identifier as printed, upper-cased.
        symbol: The trading symbol as printed, upper-cased.
        issuer_name: The description field, which names the issuer.
    """

    settlement_date: date
    cusip: str
    symbol: str
    issuer_name: str


@dataclass(frozen=True)
class FtdFileContent:
    """One archive, read and parsed, with the provenance to find it again.

    Attributes:
        name: The archive's file name.
        url: Where it came from.
        sha256: Digest of the archive's bytes, so a re-read can be shown to have
            read the same file.
        byte_size: The archive's size on the wire.
        rows: Every data row, in file order.
        skipped: Lines that were not data rows — the header, the trailer, blanks
            and anything malformed. Counted rather than discarded silently, so a
            file whose shape changed cannot be read as an empty one.
    """

    name: str
    url: str
    sha256: str
    byte_size: int
    rows: tuple[FtdRow, ...]
    skipped: int

    @property
    def first_settlement(self) -> date | None:
        """Earliest settlement date in the file, or None when it holds no rows."""
        return min((row.settlement_date for row in self.rows), default=None)

    @property
    def last_settlement(self) -> date | None:
        """Latest settlement date in the file, or None when it holds no rows."""
        return max((row.settlement_date for row in self.rows), default=None)


def _decode(payload: bytes) -> str:
    """Decode a member, trying the encodings the series has actually used."""
    for encoding in _ENCODINGS:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Neither encoding fits. Decoding with replacement loses accents in issuer
    # names and keeps every CUSIP, symbol and date intact — the identity content
    # is ASCII, so a mangled name is a cosmetic loss, not an identity one.
    return payload.decode("utf-8", errors="replace")


def _parse_settlement_date(value: str) -> date | None:
    """Read the `YYYYMMDD` settlement date, or None if the cell is not one."""
    digits = value.strip()
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def parse_ftd_member(payload: bytes) -> tuple[list[FtdRow], int]:
    """Parse one text member of a fails-to-deliver archive.

    Args:
        payload: The member's bytes.

    Returns:
        The data rows, and how many lines were skipped as non-data.

    Raises:
        ProviderDataError: If the header is not the one every published member
            has carried. A changed column order would put a symbol where a CUSIP
            belongs, and reading on would corrupt the identity record silently.
    """
    text = _decode(payload)
    lines = text.splitlines()
    if not lines:
        message = "fails-to-deliver member is empty"
        raise ProviderDataError(message)
    header = lines[0].strip().upper()
    if header != _EXPECTED_HEADER:
        message = f"fails-to-deliver header changed: {lines[0].strip()[:120]!r}"
        raise ProviderDataError(message)

    rows: list[FtdRow] = []
    skipped = 1
    for line in lines[1:]:
        if not line.strip():
            skipped += 1
            continue
        fields = line.split("|")
        if len(fields) < _FIELDS:
            # The file's own trailer records, which count what preceded them.
            skipped += 1
            continue
        settlement = _parse_settlement_date(fields[0])
        cusip = fields[1].strip().upper()
        symbol = fields[2].strip().upper()
        if settlement is None or not cusip or not symbol:
            skipped += 1
            continue
        rows.append(
            FtdRow(
                settlement_date=settlement,
                cusip=cusip,
                symbol=symbol,
                issuer_name=fields[4].strip(),
            )
        )
    return rows, skipped


def parse_ftd_archive(name: str, url: str, payload: bytes) -> FtdFileContent:
    """Parse a whole archive, across every text member it holds.

    Quarterly archives hold one member per month and semi-monthly archives hold
    one; both are read the same way and their rows concatenated, because the
    packaging is the SEC's business and the record is one continuous series.

    Args:
        name: The archive's file name.
        url: Where it came from.
        payload: The archive's bytes.

    Returns:
        The parsed content, carrying the digest of exactly these bytes.

    Raises:
        ProviderDataError: If the payload is not a readable zip, or a member's
            header is not the published one.
    """
    digest = hashlib.sha256(payload).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        message = f"{name} is not a readable zip archive: {exc}"
        raise ProviderDataError(message) from exc

    rows: list[FtdRow] = []
    skipped = 0
    with archive:
        for member in sorted(archive.namelist()):
            if member.endswith("/"):
                continue
            member_rows, member_skipped = parse_ftd_member(archive.read(member))
            rows.extend(member_rows)
            skipped += member_skipped

    return FtdFileContent(
        name=name,
        url=url,
        sha256=digest,
        byte_size=len(payload),
        rows=tuple(rows),
        skipped=skipped,
    )


class SecFailsToDeliver:
    """Reads the SEC's published fails-to-deliver history.

    Args:
        user_agent: The SEC requires a contacting address on every request and
            blocks clients that omit one.
        cache_dir: Where fetched archives are kept. The full history is roughly
            600 MB across 433 files, and re-downloading it to re-run a parse
            would be both slow and rude to a free public server. A cached
            archive is served from disk and its digest recomputed, so the
            provenance a caller stores is of the bytes actually parsed.
        client: An HTTP client to use instead of one built here, for tests.
        min_interval_seconds: Floor on the gap between requests. The SEC asks for
            no more than ten a second; this defaults well below that.
        timeout: Per-request timeout. Generous, because some archives are large.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        cache_dir: Path | None = None,
        client: httpx.Client | None = None,
        min_interval_seconds: float = 0.25,
        timeout: float = 180.0,
    ) -> None:
        """Build the adapter."""
        self._cache_dir = cache_dir
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._limiter = RateLimiter(min_interval_seconds)
        self._retry = RetryPolicy()

    def list_archives(self) -> tuple[FtdArchive, ...]:
        """Return every published archive, oldest name first.

        Read from the SEC's index page. Deduplicated by file name, because the
        page links a handful of months from two directories at once and fetching
        both would double-count their rows.

        Returns:
            The archives, sorted by name so a run's order is reproducible.

        Raises:
            ProviderDataError: If the page carries no archive links at all,
                which means its markup changed and a silent zero would otherwise
                be read as "the SEC published nothing".
        """
        page = request_text(
            self._client,
            "GET",
            FTD_INDEX_URL,
            provider="sec-ftd",
            limiter=self._limiter,
            retry=self._retry,
        )
        found: dict[str, FtdArchive] = {}
        for match in _ARCHIVE_HREF.finditer(page):
            path = match.group(1)
            name = path.rsplit("/", 1)[-1]
            found.setdefault(name, FtdArchive(name=name, url=f"{_SEC_ROOT}{path}"))
        if not found:
            message = f"{FTD_INDEX_URL} listed no fails-to-deliver archives"
            raise ProviderDataError(message)
        return tuple(sorted(found.values(), key=lambda archive: archive.name))

    def fetch(self, archive: FtdArchive) -> FtdFileContent:
        """Fetch and parse one archive, from the cache when it is already there.

        Args:
            archive: The archive to read.

        Returns:
            Its parsed content.

        Raises:
            ProviderDataError: If the archive is unreadable or its header changed.
            ProviderRequestError: If the fetch failed on the final attempt.
        """
        cached = None if self._cache_dir is None else self._cache_dir / archive.name
        if cached is not None and cached.is_file() and cached.stat().st_size > 0:
            return parse_ftd_archive(archive.name, archive.url, cached.read_bytes())

        payload = request_bytes(
            self._client,
            "GET",
            archive.url,
            provider="sec-ftd",
            limiter=self._limiter,
            retry=self._retry,
        )
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(payload)
        return parse_ftd_archive(archive.name, archive.url, payload)

    def iter_history(
        self, archives: Sequence[FtdArchive] | None = None
    ) -> Iterator[FtdFileContent]:
        """Yield every archive's content, one at a time.

        One at a time on purpose. The whole history parses to tens of millions of
        rows, and materialising them would need more memory than the machine
        running the ingestion has; a caller folds each file into its aggregate
        and lets it go.

        Args:
            archives: The archives to read. Defaults to every published one.

        Yields:
            Each archive's parsed content, in name order.
        """
        for archive in archives if archives is not None else self.list_archives():
            log.debug("ftd.fetch", archive=archive.name)
            yield self.fetch(archive)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
