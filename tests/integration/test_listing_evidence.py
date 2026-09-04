"""Adversarial tests for the free listing evidence layer — Phase 9K.1.

The source is a monthly *market share* file, and the ways it misleads are all
represented here: a family whose meaning changed mid-series, a monthly grain that
looks daily, an absence that looks like a delisting, and a ticker that looks like
an identity.

Three tests carry the most weight. `test_a_tape_b_file_is_quarantined_not_imported`
is the one that stops five years of NYSE Arca ETFs being recorded as NYSE
listings. `test_absence_from_a_month_is_unknown_not_not_listed` is the rule the
whole layer exists to protect. And `TestIdentityIsNotCollapsedByTicker` asserts
that Phase 9J's distinctions survive contact with this source.
"""

from __future__ import annotations

import hashlib
import zipfile
from datetime import date
from io import BytesIO
from typing import TYPE_CHECKING

import pytest

from api_clients.errors import ProviderDataError
from api_clients.nasdaq_market_share import parse_file_name, parse_market_share
from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import ListingObservation, ListingSourceFile
from domain import CompanyProfile, ListingEvidenceState, SourceFamily, Venue
from stock_screener.listing import (
    IMPORTED,
    QUARANTINED,
    build_listing_audit,
    evidence_state_on,
    ingest_file,
    ingest_files,
    runs_for_symbol,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

NYSE_NAMES = ("JPM", "GE", "XOM", "BA", "T", "PFE", "KO")
ARCA_NAMES = ("SPY", "GDX", "IWM")


def workbook(symbols: Sequence[str], volumes: Mapping[str, float] | None = None) -> bytes:
    """Build a minimal .xlsx in the published shape: shared strings and the
    seven-column layout the real files use."""
    volumes = volumes or {}
    header = [
        "Issue",
        "NASDAQ Matched Volume",
        "NASDAQ TRF Volume",
        "Total NASDAQ Volume",
        "Routed Volume",
        "NASDAQ Handled Volume",
        "Consolidated Volume",
    ]
    strings: list[str] = []

    def index_of(value: str) -> int:
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rows_xml: list[str] = []
    header_cells = "".join(
        f'<c r="{chr(65 + c)}1" t="s"><v>{index_of(v)}</v></c>' for c, v in enumerate(header)
    )
    rows_xml.append(f'<row r="1">{header_cells}</row>')
    for r, symbol in enumerate(symbols, start=2):
        cells = [f'<c r="A{r}" t="s"><v>{index_of(symbol)}</v></c>']
        cells += [f'<c r="{chr(65 + c)}{r}"><v>1</v></c>' for c in range(1, 6)]
        cells.append(f'<c r="G{r}"><v>{volumes.get(symbol, 1000.0)}</v></c>')
        rows_xml.append(f'<row r="{r}">{"".join(cells)}</row>')

    sheet = f'<worksheet xmlns="{ns}"><sheetData>{"".join(rows_xml)}</sheetData></worksheet>'
    shared = (
        f'<sst xmlns="{ns}" count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{v}</t></si>" for v in strings)
        + "</sst>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", shared)
    return buffer.getvalue()


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'listing.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


class TestTapeIdentityIsValidatedAgainstContents:
    """A file's name and date state what it should be. Only its contents prove it."""

    def test_a_tape_a_file_in_the_tape_a_era_is_imported(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            result = ingest_file(session, "Listed202203.xlsx", workbook(NYSE_NAMES))

        assert result.status == IMPORTED
        assert result.venue == str(Venue.NYSE)

    def test_a_tape_b_file_is_quarantined_not_imported(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Reading these as NYSE would relabel five years of Arca ETFs."""
        with session_scope(factory) as session:
            result = ingest_file(session, "Listed202203.xlsx", workbook(ARCA_NAMES))

            assert result.status == QUARANTINED
            assert "tape mismatch" in (result.reason or "")
            assert session.query(ListingObservation).count() == 0

    def test_a_pre_2022_listed_file_never_becomes_nyse_evidence(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            result = ingest_file(session, "Listed202006.xlsx", workbook(ARCA_NAMES))

            assert result.status == QUARANTINED
            assert result.venue is None
            assert session.query(ListingObservation).count() == 0

    def test_a_nasdaq_file_is_imported_as_nasdaq(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            result = ingest_file(session, "NASDAQ202203.xlsx", workbook(("AAPL", "MSFT")))

        assert result.status == IMPORTED
        assert result.venue == str(Venue.NASDAQ)

    def test_the_quarantined_file_keeps_its_row_and_its_reason(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            ingest_file(session, "Listed202203.xlsx", workbook(ARCA_NAMES))
            stored = session.query(ListingSourceFile).one()

            assert stored.status == QUARANTINED
            assert stored.expected_tape == "A"
            assert stored.observed_tape == "B"
            assert stored.reason


class TestSourceParsing:
    """What the reader accepts, and what it refuses."""

    def test_a_filename_yields_its_family_and_month(self) -> None:
        assert parse_file_name("Listed202201.xlsx") == (SourceFamily.LISTED, date(2022, 1, 1))
        assert parse_file_name("NASDAQ202607.xlsx") == (SourceFamily.NASDAQ, date(2026, 7, 1))

    def test_an_unrecognised_filename_is_refused(self) -> None:
        with pytest.raises(ProviderDataError, match="not a Nasdaq market-share filename"):
            parse_file_name("something_else.xlsx")

    def test_a_malformed_payload_is_refused(self) -> None:
        with pytest.raises(ProviderDataError, match="not a readable workbook"):
            parse_market_share("NASDAQ202203.xlsx", b"not a zip file at all")

    def test_an_unexpected_column_layout_is_refused(self) -> None:
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet = (
            f'<worksheet xmlns="{ns}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Ticker</t></is></c></row>'
            "</sheetData></worksheet>"
        )
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", sheet)

        with pytest.raises(ProviderDataError, match="unexpected layout"):
            parse_market_share("NASDAQ202203.xlsx", buffer.getvalue())

    def test_the_recorded_checksum_is_the_sha256_of_the_bytes(self) -> None:
        payload = workbook(("AAPL",))

        parsed = parse_market_share("NASDAQ202203.xlsx", payload)

        assert parsed.sha256 == hashlib.sha256(payload).hexdigest()


class TestAbsenceNeverBecomesNonMembership:
    """The rule the layer exists to protect."""

    def test_absence_from_a_month_is_unknown_not_not_listed(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            ingest_file(session, "NASDAQ202201.xlsx", workbook(("AAPL",)))

            state = evidence_state_on(session, "GONE", Venue.NASDAQ, date(2022, 1, 15))

            assert state is ListingEvidenceState.UNKNOWN

    def test_a_hole_leaves_two_runs_rather_than_one_filled_span(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            for month in ("202201", "202202", "202205"):
                ingest_file(session, f"NASDAQ{month}.xlsx", workbook(("ACME",)))

            runs = runs_for_symbol(session, "ACME", Venue.NASDAQ)

            assert runs == (
                (date(2022, 1, 1), date(2022, 2, 1)),
                (date(2022, 5, 1), date(2022, 5, 1)),
            )

    def test_a_month_between_two_observed_months_stays_unknown(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            for month in ("202201", "202203"):
                ingest_file(session, f"NASDAQ{month}.xlsx", workbook(("ACME",)))

            assert evidence_state_on(session, "ACME", Venue.NASDAQ, date(2022, 2, 10)) is (
                ListingEvidenceState.UNKNOWN
            )


class TestIdentityIsNotCollapsedByTicker:
    """Phase 9J's distinctions must survive this source."""

    def test_goog_and_googl_remain_separate_observations(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            ingest_file(session, "NASDAQ202203.xlsx", workbook(("GOOG", "GOOGL")))

            symbols = {row.symbol for row in session.query(ListingObservation).all()}

            assert symbols == {"GOOG", "GOOGL"}

    def test_bkep_and_bkepp_remain_separate_observations(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            ingest_file(session, "NASDAQ202203.xlsx", workbook(("BKEP", "BKEPP")))

            symbols = {row.symbol for row in session.query(ListingObservation).all()}

            assert symbols == {"BKEP", "BKEPP"}

    def test_one_symbol_on_two_venues_is_two_observations(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A transfer, kept as separate venue evidence rather than one history."""
        with session_scope(factory) as session:
            ingest_file(session, "NASDAQ202203.xlsx", workbook(("XFER",)))
            ingest_file(session, "Listed202204.xlsx", workbook((*NYSE_NAMES, "XFER")))

            nasdaq = runs_for_symbol(session, "XFER", Venue.NASDAQ)
            nyse = runs_for_symbol(session, "XFER", Venue.NYSE)

            assert nasdaq == ((date(2022, 3, 1), date(2022, 3, 1)),)
            assert nyse == ((date(2022, 4, 1), date(2022, 4, 1)),)


class TestIngestionIsIdempotent:
    """A second run must change nothing at all."""

    def test_reimporting_the_same_corpus_inserts_nothing(
        self, factory: sessionmaker[Session]
    ) -> None:
        payloads = {
            "NASDAQ202201.xlsx": workbook(("AAPL", "MSFT")),
            "Listed202201.xlsx": workbook(NYSE_NAMES),
        }
        with session_scope(factory) as session:
            first = ingest_files(session, payloads)
        with session_scope(factory) as session:
            second = ingest_files(session, payloads)
            observations = session.query(ListingObservation).count()
            sources = session.query(ListingSourceFile).count()

        assert first.inserted > 0
        assert second.inserted == 0
        assert observations == first.inserted
        assert sources == 2

    def test_a_duplicate_file_name_is_not_imported_twice(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            ingest_file(session, "NASDAQ202201.xlsx", workbook(("AAPL",)))
            again = ingest_file(session, "NASDAQ202201.xlsx", workbook(("AAPL",)))

            assert again.inserted == 0
            assert session.query(ListingSourceFile).count() == 1


class TestTheAuditCountsWhatItCannotSee:
    """A blind spot that is not counted is indistinguishable from an empty one."""

    def test_nyse_american_companies_are_counted_as_censored(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            for ticker, exchange in (("AAPL", "NASDAQ"), ("SMALL", "AMEX")):
                CompanyRepository(session).upsert_profile(
                    CompanyProfile(ticker=ticker, name=ticker, exchange=exchange)
                )
            ingest_file(session, "NASDAQ202201.xlsx", workbook(("AAPL",)))

            audit = build_listing_audit(session, scope_from=date(2022, 1, 1))

            assert audit.live_censored == 1
            assert audit.live_observed == 1
            assert audit.live_unknown == 1
