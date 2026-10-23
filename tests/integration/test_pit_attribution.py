"""Attributing a listing observation to whoever held the symbol *that month*.

Every test here is a regression against one defect: the attribution pass asked
"is this symbol in `companies`?" before it asked "what month is this?", so the
current holder of a reused ticker was credited with every month the ticker ever
had. `BBBY` is the worked case and the first class below is the proof — Bed Bath
& Beyond's Nasdaq months through 2023-05 and Beyond, Inc.'s NYSE months from
2025-08 must come back as two occupants, and neither may appear in the other's
months.

The fixtures are the real record, not invented shapes: the CUSIPs, issuer names,
settlement windows and last trading dates are the ones stored in the staging
database, so a test passing here is a statement about data this project holds.
"""

from __future__ import annotations

import zipfile
from datetime import date
from io import BytesIO
from typing import TYPE_CHECKING

import pytest

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import (
    Company,
    FtdIdentityObservation,
    HistoricalSecurity,
    ListingObservation,
    ResearchUniverseMonth,
    SecurityIdentifier,
)
from domain import CompanyProfile, CoverSecurity, UniverseState
from stock_screener.listing import FilingCover, attribute_observations, ingest_file
from stock_screener.listing.attribution import (
    RESOLVED_COMPANY,
    RESOLVED_HISTORICAL,
    REUSED,
)
from stock_screener.universe import build_universe

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

#: The Tape A sentinels a `Listed` file must carry to be read as NYSE.
NYSE_SENTINELS = ("JPM", "GE", "XOM", "BA", "T", "PFE", "KO")


def workbook(symbols: Sequence[str]) -> bytes:
    """A minimal .xlsx in the published market-share shape."""
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

    def idx(v: str) -> int:
        if v not in strings:
            strings.append(v)
        return strings.index(v)

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rows = [
        '<row r="1">'
        + "".join(f'<c r="{chr(65 + c)}1" t="s"><v>{idx(v)}</v></c>' for c, v in enumerate(header))
        + "</row>"
    ]
    for r, sym in enumerate(symbols, start=2):
        cells = [f'<c r="A{r}" t="s"><v>{idx(sym)}</v></c>']
        cells += [f'<c r="{chr(65 + c)}{r}"><v>1</v></c>' for c in range(1, 6)]
        cells.append(f'<c r="G{r}"><v>1000</v></c>')
        rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{ns}"><sheetData>{"".join(rows)}</sheetData></worksheet>'
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
    """A session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'pit.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def nasdaq(session: Session, symbol: str, months: Sequence[str]) -> None:
    """Observe a symbol on Nasdaq in each month, e.g. `202201`."""
    for month in months:
        ingest_file(session, f"NASDAQ{month}.xlsx", workbook((symbol,)))


def nasdaq_together(session: Session, symbols: Sequence[str], month: str) -> None:
    """Observe several symbols in one month's file.

    One published file per month is the real shape, and the importer refuses a
    second file of the same name — so siblings share a workbook rather than each
    re-importing the month.
    """
    ingest_file(session, f"NASDAQ{month}.xlsx", workbook(symbols))


def nyse(session: Session, symbol: str, months: Sequence[str]) -> None:
    """Observe a symbol on NYSE in each month, with the Tape A sentinels."""
    for month in months:
        ingest_file(session, f"Listed{month}.xlsx", workbook((symbol, *NYSE_SENTINELS)))


def company(session: Session, ticker: str, name: str, exchange: str = "NASDAQ") -> int:
    """Register a live company and return its id."""
    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker=ticker, name=name, exchange=exchange)
    )
    session.flush()
    stored = session.query(Company).filter_by(ticker=ticker).one()
    return int(stored.id)


def delisted(
    session: Session,
    ticker: str,
    name: str,
    *,
    last_trading_date: date | None,
    cik: int | None = None,
) -> int:
    """Store a delisted security and return its id."""
    security = HistoricalSecurity(
        primary_ticker=ticker,
        name=name,
        cik=cik,
        last_trading_date=last_trading_date,
        status="UNKNOWN",
    )
    session.add(security)
    session.flush()
    return int(security.id)


def ftd(
    session: Session,
    symbol: str,
    cusip: str,
    issuer_name: str,
    first: date,
    last: date,
) -> None:
    """Record one issuer's published failure window under a symbol."""
    session.add(
        FtdIdentityObservation(
            cusip=cusip,
            issuer_base=cusip[:6],
            symbol=symbol,
            issuer_name=issuer_name,
            first_settlement_date=first,
            last_settlement_date=last,
            observation_count=100,
            cusip_valid=True,
            first_source_file="cnsfails.zip",
            last_source_file="cnsfails.zip",
            evidence_hash=f"{cusip}{first}",
        )
    )
    session.flush()


def identifier(
    session: Session, security_id: int, cusip: str, ticker: str, first: date, last: date
) -> None:
    """Record the dated interval a security carried a CUSIP under a symbol."""
    session.add(
        SecurityIdentifier(
            historical_security_id=security_id,
            cusip=cusip,
            ticker=ticker,
            valid_from=first,
            valid_to=last,
            source="SEC_FTD",
            confidence="OBSERVED",
            lineage="INITIAL",
            issuer_name=ticker,
        )
    )
    session.flush()


def states(session: Session, symbol: str) -> dict[date, tuple[str, int | None, int | None]]:
    """Every attributed month for a symbol, by month."""
    rows = session.query(ListingObservation).filter_by(symbol=symbol).all()
    return {
        r.observation_month: (r.identity_state, r.company_id, r.historical_security_id)
        for r in rows
    }


def seed_bbby(session: Session) -> tuple[int, int]:
    """The real `BBBY` record: Bed Bath through 2023-05, Beyond from 2025-08.

    Returns:
        `(historical security id, live company id)`.
    """
    security = delisted(
        session, "BBBY", "Bed Bath & Beyond Inc.", last_trading_date=date(2023, 5, 2), cik=886158
    )
    live = company(session, "BBBY", "Neighborhood Intelligence, Inc.", exchange="NYSE")
    ftd(session, "BBBY", "075896100", "BED BATH & BEYOND INC", date(2004, 3, 24), date(2023, 5, 3))
    ftd(session, "BBBY", "690370101", "BED BATH & BEYOND, INC", date(2025, 9, 2), date(2026, 8, 12))
    identifier(session, security, "075896100", "BBBY", date(2004, 3, 24), date(2023, 5, 3))
    nasdaq(session, "BBBY", ["202201", "202206", "202301", "202305"])
    nyse(session, "BBBY", ["202508", "202512", "202607"])
    attribute_observations(session)
    return security, live


class TestBedBathAndBeyondAreTwoOccupants:
    """The primary regression. One symbol, two companies, one boundary."""

    def test_bed_baths_months_resolve_to_the_delisted_security(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            security, _ = seed_bbby(session)

            early = {m: v for m, v in states(session, "BBBY").items() if m.year <= 2023}

            assert early
            assert all(v == (RESOLVED_HISTORICAL, None, security) for v in early.values())

    def test_beyonds_months_resolve_to_the_live_company(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Months inside Beyond's own published evidence, which begins 2025-09-02."""
        with session_scope(factory) as session:
            _, live = seed_bbby(session)

            late = {m: v for m, v in states(session, "BBBY").items() if m >= date(2025, 10, 1)}

            assert late
            assert all(v == (RESOLVED_COMPANY, live, None) for v in late.values())

    def test_the_handover_gap_month_is_refused_rather_than_guessed(
        self, factory: sessionmaker[Session]
    ) -> None:
        """2025-08 sits between the two occupants' evidence and belongs to neither.

        Bed Bath's CUSIP interval ends 2023-05-03 and Beyond's failures begin
        2025-09-02, so nothing places the month. `REUSED` records that the symbol
        changed hands without naming who held it — the conservative answer the
        `occupancy_of` rule already gives a date inside a gap.
        """
        with session_scope(factory) as session:
            seed_bbby(session)

            state, company_id, security_id = states(session, "BBBY")[date(2025, 8, 1)]

            assert state == REUSED
            assert company_id is None
            assert security_id is None

    def test_the_live_company_never_appears_in_bed_baths_months(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            _, live = seed_bbby(session)

            assert not [
                month
                for month, (_, company_id, _) in states(session, "BBBY").items()
                if company_id == live and month.year <= 2023
            ]

    def test_the_delisted_security_never_appears_in_beyonds_months(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            security, _ = seed_bbby(session)

            assert not [
                month
                for month, (_, _, security_id) in states(session, "BBBY").items()
                if security_id == security and month.year >= 2025
            ]

    def test_the_record_holds_two_occupants_rather_than_one(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            security, live = seed_bbby(session)

            occupants = {
                ("COMPANY", c) if c is not None else ("HISTORICAL", h)
                for _, c, h in states(session, "BBBY").values()
                if (c, h) != (None, None)
            }

            assert occupants == {("HISTORICAL", security), ("COMPANY", live)}

    def test_the_final_trading_month_belongs_to_the_departing_occupant(
        self, factory: sessionmaker[Session]
    ) -> None:
        """2023-05-02 was the last session, so 2023-05 is still Bed Bath's."""
        with session_scope(factory) as session:
            security, _ = seed_bbby(session)

            assert states(session, "BBBY")[date(2023, 5, 1)] == (
                RESOLVED_HISTORICAL,
                None,
                security,
            )


class TestALiveCompanyIsAClaimantNotADefault:
    def test_a_stored_exit_date_alone_never_moves_a_symbol_off_the_live_roster(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The Leggett & Platt case, and the reason the fix is not a date test.

        `historical_securities` holds a row for `LEG` with an exit date days from
        the present, because the free record's last trading date is the last
        settlement its files happened to show. 127 of the 138 symbols carrying
        both a live row and a delisted row look like this. One issuer, one
        business, one answer: the live company, for every month.
        """
        with session_scope(factory) as session:
            security = delisted(
                session, "LEG", "LEGGETT&PLATT INC", last_trading_date=date(2026, 8, 27)
            )
            live = company(session, "LEG", "Leggett & Platt, Incorporated", exchange="NYSE")
            ftd(
                session,
                "LEG",
                "524660107",
                "LEGGETT&PLATT INC",
                date(2004, 1, 5),
                date(2026, 8, 27),
            )
            identifier(session, security, "524660107", "LEG", date(2004, 1, 5), date(2026, 8, 27))
            nyse(session, "LEG", ["202201", "202506"])
            attribute_observations(session)

            attributed = states(session, "LEG")

            assert len(attributed) == 2
            assert all(v == (RESOLVED_COMPANY, live, None) for v in attributed.values())

    def test_an_undated_delisted_row_does_not_block_its_live_company(
        self, factory: sessionmaker[Session]
    ) -> None:
        """No handover evidence is not evidence of a handover."""
        with session_scope(factory) as session:
            delisted(session, "ACME", "Acme Corporation", last_trading_date=None)
            live = company(session, "ACME", "Acme Corporation")
            nasdaq(session, "ACME", ["202201", "202206"])
            attribute_observations(session)

            assert all(
                v == (RESOLVED_COMPANY, live, None) for v in states(session, "ACME").values()
            )

    def test_a_symbol_no_delisted_security_ever_held_resolves_to_its_live_company(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The ordinary case must be untouched by the fix."""
        with session_scope(factory) as session:
            live = company(session, "MSFT", "Microsoft Corporation")
            nasdaq(session, "MSFT", ["202201", "202202"])
            attribute_observations(session)

            assert all(
                v == (RESOLVED_COMPANY, live, None) for v in states(session, "MSFT").values()
            )


class TestSiblingSecuritiesAreNeverPooled:
    """A shared issuer is not a shared identity — the Phase 9J rule, held."""

    def test_goog_and_googl_resolve_to_different_companies(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            goog = company(session, "GOOG", "Alphabet Inc.")
            googl = company(session, "GOOGL", "Alphabet Inc.")
            nasdaq_together(session, ("GOOG", "GOOGL"), "202201")
            attribute_observations(session)

            assert goog != googl
            assert states(session, "GOOG")[date(2022, 1, 1)] == (RESOLVED_COMPANY, goog, None)
            assert states(session, "GOOGL")[date(2022, 1, 1)] == (RESOLVED_COMPANY, googl, None)

    def test_bkep_and_bkepp_resolve_to_different_securities(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Common units and preferred units of one partnership, sharing a base."""
        with session_scope(factory) as session:
            common = delisted(
                session,
                "BKEP",
                "Blueknight Energy Partners, L.P.",
                last_trading_date=date(2022, 8, 17),
                cik=1392091,
            )
            preferred = delisted(
                session,
                "BKEPP",
                "BLUEKNIGHT ENERGY PARTNERS L P",
                last_trading_date=date(2022, 8, 17),
            )
            ftd(
                session,
                "BKEP",
                "09625U109",
                "BLUEKNIGHT ENERGY PARTNERS L.P",
                date(2009, 12, 14),
                date(2022, 8, 22),
            )
            ftd(
                session,
                "BKEPP",
                "09625U208",
                "BLUEKNIGHT ENERGY PARTNERS L P",
                date(2011, 11, 16),
                date(2022, 7, 18),
            )
            nasdaq_together(session, ("BKEP", "BKEPP"), "202201")
            attribute_observations(session)

            assert common != preferred
            assert states(session, "BKEP")[date(2022, 1, 1)] == (
                RESOLVED_HISTORICAL,
                None,
                common,
            )
            assert states(session, "BKEPP")[date(2022, 1, 1)] == (
                RESOLVED_HISTORICAL,
                None,
                preferred,
            )


class TestASharedFilerNeverMergesTwoSecurities:
    """`SGEN`: Seattle Genetics then Seagen, one CIK, two CUSIP issuer bases."""

    def seed(self, session: Session) -> tuple[int, int]:
        seattle = delisted(
            session, "SGEN", "Seattle Genetics, Inc.", last_trading_date=date(2020, 10, 8)
        )
        seagen = delisted(
            session, "SGEN", "Seagen Inc.", last_trading_date=date(2023, 12, 14), cik=1060736
        )
        ftd(
            session,
            "SGEN",
            "812578102",
            "SEATTLE GENETICS INC",
            date(2004, 5, 24),
            date(2020, 10, 9),
        )
        ftd(
            session,
            "SGEN",
            "81181C104",
            "SEAGEN INC COM (DE)",
            date(2020, 10, 9),
            date(2023, 12, 15),
        )
        identifier(session, seattle, "812578102", "SGEN", date(2004, 5, 24), date(2020, 10, 9))
        identifier(session, seagen, "81181C104", "SGEN", date(2020, 10, 9), date(2023, 12, 15))
        nasdaq(session, "SGEN", ["202201", "202301"])
        attribute_observations(session)
        return seattle, seagen

    def test_the_later_occupant_owns_the_observed_months(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            _, seagen = self.seed(session)

            assert all(
                v == (RESOLVED_HISTORICAL, None, seagen) for v in states(session, "SGEN").values()
            )

    def test_the_two_issuer_bases_never_collapse_into_one_security(
        self, factory: sessionmaker[Session]
    ) -> None:
        """812578 and 81181C are different issuers, and stay two rows."""
        with session_scope(factory) as session:
            seattle, seagen = self.seed(session)

            assert seattle != seagen
            assert seattle not in {h for _, _, h in states(session, "SGEN").values()}


class TestAVenueTransferIsNotAChangeOfHands:
    """`PLTR` moved NYSE→Nasdaq and is observed on both in the handover month."""

    def test_a_security_seen_on_both_venues_in_one_month_stays_one_identity(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            live = company(session, "PLTR", "Palantir Technologies Inc.")
            nyse(session, "PLTR", ["202209"])
            nasdaq(session, "PLTR", ["202209", "202210"])
            attribute_observations(session)

            rows = session.query(ListingObservation).filter_by(symbol="PLTR").all()

            assert len({r.venue for r in rows}) == 2
            assert {(r.identity_state, r.company_id) for r in rows} == {(RESOLVED_COMPANY, live)}

    def test_the_handover_month_writes_one_universe_row_not_two(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            live = company(session, "PLTR", "Palantir Technologies Inc.")
            nyse(session, "PLTR", ["202209"])
            nasdaq(session, "PLTR", ["202209", "202210"])
            attribute_observations(session)
            build_universe(
                session,
                {
                    (RESOLVED_COMPANY, live): [
                        FilingCover(
                            accepted=date(2022, 8, 1),
                            securities=[
                                CoverSecurity(
                                    title="Common Stock, $0.001 par value",
                                    symbol="PLTR",
                                    exchange="New York Stock Exchange",
                                )
                            ],
                            accession="pltr-2022",
                        )
                    ]
                },
            )

            rows = session.query(ResearchUniverseMonth).filter_by(
                observation_month=date(2022, 9, 1)
            )

            assert rows.count() == 1
            assert rows.one().state == str(UniverseState.ELIGIBLE)


class TestAGapIsNotAHandover:
    def test_months_around_a_gap_keep_the_same_occupant(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A symbol absent for a year is still the same company either side."""
        with session_scope(factory) as session:
            live = company(session, "GAPY", "Gap Test Corporation")
            nasdaq(session, "GAPY", ["202201", "202302", "202303"])
            attribute_observations(session)

            attributed = states(session, "GAPY")

            assert len(attributed) == 3
            assert all(v == (RESOLVED_COMPANY, live, None) for v in attributed.values())


class TestAPredecessorsFilingNeverClassifiesItsSuccessor:
    """The mirror of the attribution defect, in the class join."""

    def test_bed_baths_cover_page_cannot_classify_beyonds_months(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            _, live = seed_bbby(session)
            build_universe(
                session,
                {
                    (RESOLVED_COMPANY, live): [
                        FilingCover(
                            accepted=date(2022, 3, 1),
                            securities=[
                                CoverSecurity(
                                    title="Common Stock, $0.01 par value",
                                    symbol="BBBY",
                                    exchange="The Nasdaq Stock Market",
                                )
                            ],
                            accession="bedbath-2022",
                        )
                    ]
                },
            )

            beyond = session.query(ResearchUniverseMonth).filter_by(company_id=live).all()

            assert beyond
            assert not [r for r in beyond if r.class_source_accession == "bedbath-2022"]
            assert all(r.state == str(UniverseState.UNKNOWN) for r in beyond)
