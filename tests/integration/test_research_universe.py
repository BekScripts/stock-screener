"""Building the monthly research universe end to end.

The tests that matter here are the ones asserting what the universe *refuses*:
that a hole between two observed months stays UNKNOWN rather than being filled,
that a later occupant of a ticker cannot classify an earlier one, and that
nothing censored quietly disappears from the audit.
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
from data_access.models import ListingObservation, ResearchUniverseMonth
from domain import (
    ClassEvidenceMode,
    CompanyProfile,
    CoverSecurity,
    SecurityClass,
    UniverseState,
)
from stock_screener.listing import FilingCover, attribute_observations, ingest_file
from stock_screener.universe import (
    build_universe,
    build_universe_audit,
    eligible_for_month,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

NYSE_NAMES = ("JPM", "GE", "XOM", "BA", "T", "PFE", "KO")


def workbook(symbols: Sequence[str]) -> bytes:
    """A minimal .xlsx in the published shape."""
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


def cover(symbol: str, accepted: date, title: str = "Common Stock, $0.01 par value") -> FilingCover:
    """One cover page stating a class for a symbol.

    Fixtures accept **before** the month they classify: the PIT rule requires a
    filing to be public at the month's start, so a filing accepted mid-January
    does not describe January. That is deliberate and is asserted in
    `test_a_future_filing_never_classifies_an_earlier_month`.
    """
    return FilingCover(
        accepted=accepted,
        securities=[CoverSecurity(title=title, symbol=symbol, exchange="The Nasdaq Stock Market")],
        accession=f"{symbol}-{accepted:%Y%m}",
    )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'universe.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def seed(session: Session, ticker: str, months: Sequence[str]) -> int:
    """Register a live company and ingest its observed months."""
    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker=ticker, name=ticker, exchange="NASDAQ")
    )
    for month in months:
        ingest_file(session, f"NASDAQ{month}.xlsx", workbook((ticker,)))
    attribute_observations(session)
    row = session.query(ListingObservation).filter_by(symbol=ticker).first()
    assert row is not None
    return int(row.company_id or 0)


class TestAHoleIsNeverFilled:
    """Observed January and March must not manufacture February."""

    def test_a_missing_month_is_written_as_unknown(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202203"])
            build_universe(
                session,
                {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]},
            )

            rows = {
                r.observation_month: r.state for r in session.query(ResearchUniverseMonth).all()
            }

            assert rows[date(2022, 2, 1)] == str(UniverseState.UNKNOWN)
            assert rows[date(2022, 3, 1)] == str(UniverseState.ELIGIBLE)

    def test_the_unknown_month_is_present_rather_than_omitted(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202203"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            assert session.query(ResearchUniverseMonth).count() == 3


class TestClassEvidenceBounds:
    def test_a_month_beyond_the_carry_forward_bound_is_unknown(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202202", "202203", "202204", "202205"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            rows = {
                r.observation_month: (r.state, r.class_evidence_mode)
                for r in session.query(ResearchUniverseMonth).all()
            }

            assert rows[date(2022, 2, 1)][0] == str(UniverseState.ELIGIBLE)
            assert rows[date(2022, 2, 1)][1] == str(ClassEvidenceMode.CARRIED_FORWARD)
            assert rows[date(2022, 5, 1)][0] == str(UniverseState.UNKNOWN)
            assert rows[date(2022, 5, 1)][1] == str(ClassEvidenceMode.ABSENT)

    def test_a_future_filing_never_classifies_an_earlier_month(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202202"])
            build_universe(session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2022, 12, 1))]})

            states = {r.state for r in session.query(ResearchUniverseMonth).all()}

            assert states == {str(UniverseState.UNKNOWN)}

    def test_a_non_common_class_excludes_rather_than_unknowns(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201"])
            build_universe(
                session,
                {
                    ("RESOLVED_COMPANY", cid): [
                        cover("ACME", date(2021, 12, 15), title="Redeemable Warrants")
                    ]
                },
            )

            row = session.query(ResearchUniverseMonth).one()

            assert row.state == str(UniverseState.NON_COMMON_OR_EXCLUDED)
            assert row.security_class == str(SecurityClass.WARRANT)


class TestTheDownstreamContract:
    def test_only_eligible_rows_are_offered_to_a_strategy(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202203"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            january = eligible_for_month(session, date(2022, 1, 1))
            february = eligible_for_month(session, date(2022, 2, 1))

            assert [r.symbol for r in january] == ["ACME"]
            assert february == ()

    def test_a_security_with_no_class_evidence_is_never_offered(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201"])
            build_universe(session, {("RESOLVED_COMPANY", cid): []})

            assert eligible_for_month(session, date(2022, 1, 1)) == ()


class TestNothingCensoredDisappears:
    def test_the_audit_counts_every_state_including_unknown(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202203"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            audit = build_universe_audit(session)

            assert audit.total == 3
            assert audit.by_state[str(UniverseState.UNKNOWN)] == 1
            assert audit.by_state[str(UniverseState.ELIGIBLE)] == 2

    def test_nyse_american_companies_stay_visible_in_the_audit(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            CompanyRepository(session).upsert_profile(
                CompanyProfile(ticker="SMALL", name="SMALL", exchange="AMEX")
            )
            cid = seed(session, "ACME", ["202201"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            audit = build_universe_audit(session)

            assert audit.live_censored_venue == 1

    def test_live_and_delisted_are_never_pooled(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            audit = build_universe_audit(session)

            assert audit.live_eligible == 1
            assert audit.delisted_eligible == 0


class TestDeterminism:
    def test_building_twice_produces_identical_rows(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202202"])
            covers = {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            first = build_universe(session, covers)
            snapshot = sorted(
                (r.symbol, r.observation_month, r.state)
                for r in session.query(ResearchUniverseMonth).all()
            )
            second = build_universe(session, covers)
            again = sorted(
                (r.symbol, r.observation_month, r.state)
                for r in session.query(ResearchUniverseMonth).all()
            )

            assert first.rows == second.rows
            assert snapshot == again


class TestInvariants:
    def test_every_eligible_row_has_a_supported_venue_and_common_class(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202202"])
            build_universe(
                session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2021, 12, 15))]}
            )

            for row in session.query(ResearchUniverseMonth).all():
                if row.state == str(UniverseState.ELIGIBLE):
                    assert row.venue in {"NASDAQ", "NYSE"}
                    assert row.security_class == str(SecurityClass.COMMON_STOCK)

    def test_no_eligible_row_uses_evidence_from_the_future(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            cid = seed(session, "ACME", ["202201", "202202", "202203"])
            build_universe(session, {("RESOLVED_COMPANY", cid): [cover("ACME", date(2022, 2, 10))]})

            for row in session.query(ResearchUniverseMonth).all():
                if row.class_evidence_as_of is not None:
                    assert row.class_evidence_as_of <= row.observation_month
