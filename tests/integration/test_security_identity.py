"""Adversarial tests for the stored identity layer and its forward returns.

`packages/domain/tests/test_identity.py` holds the rules; this holds the
*storage* of them — the constraints the database enforces whatever the code
believes, and the boundaries the application must not cross.

Five properties are asserted here, each one a way Phase 9A could have gone quietly
wrong:

- a delisted security's identity never leaks into the live investable set;
- a measurement belongs to exactly one identity, never both and never neither;
- a duplicate measurement is refused independently for each identity kind;
- forward returns that existed before this phase are unchanged by it;
- a symbol resolves to a security only when the record actually names one.
"""

from __future__ import annotations

import zipfile
from datetime import date, timedelta
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from api_clients import parse_ftd_archive
from data_access import (
    CompanyRepository,
    DelistingRepository,
    FtdIdentityObservationRepository,
    HistoricalSecurityRepository,
    PriceHistoryRepository,
    SecurityIdentifierRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
    to_identifier_interval,
)
from data_access.models import ForwardReturnRecord
from domain import (
    CompanyProfile,
    CorporateActionKind,
    PriceBar,
    ResolutionStatus,
    resolve_by_ticker,
)
from stock_screener.identity import (
    build_identity_audit,
    detect_contaminated_companies,
    ingest_ftd_file,
    resolve_delisted_symbols,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

#: Bed Bath & Beyond, and Beyond, Inc. — the unrelated issuer that took the
#: symbol on 2025-09-02. Both are the real CUSIPs the fails record carries.
BBBY_CUSIP = "075896100"
OTHER_CUSIP = "690370101"

#: A second issue of Bed Bath's own issuer — the reverse-split shape.
BBBY_SECOND_ISSUE = "075896209"


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database.

    File-backed because the resolution pass opens a session per symbol, and a
    per-connection in-memory database would give each of them a blank world.
    """
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'identity.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def ftd_archive(name: str, rows: list[tuple[str, str, str, str]]) -> bytes:
    """Build a fails-to-deliver archive in the SEC's published format.

    Args:
        name: The member name inside the zip.
        rows: `(settlement YYYYMMDD, cusip, symbol, issuer name)` tuples.

    Returns:
        The archive's bytes, trailer records included, so the parser is
        exercised against the shape the SEC actually publishes.
    """
    lines = ["SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"]
    lines += [
        f"{when}|{cusip}|{symbol}|1000|{issuer}|10.00" for when, cusip, symbol, issuer in rows
    ]
    lines.append(f"Trailer record count {len(rows)}")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, "\n".join(lines))
    return buffer.getvalue()


def load_evidence(factory: sessionmaker[Session], rows: list[tuple[str, str, str, str]]) -> None:
    """Parse and store one archive's worth of identity evidence."""
    content = parse_ftd_archive(
        "cnsfailstest.zip", "https://example.test/x.zip", ftd_archive("cnsfailstest", rows)
    )
    ingest_ftd_file(factory, content)


def add_delisting(session: Session, ticker: str, effective: date) -> None:
    """Record one delisting, the way `discover-delistings` would."""
    DelistingRepository(session).add_all(
        [
            {
                "ticker": ticker,
                "kind": str(CorporateActionKind.WORTHLESS_REMOVAL),
                "effective_date": effective,
                "source": "test",
                "source_id": f"{ticker}-{effective}",
            }
        ]
    )


def _seed_company(session: Session, *, ticker: str = "AAA", name: str = "A Inc") -> int:
    """Insert one active company row and return its id."""
    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker=ticker, name=name, exchange="NASDAQ", is_active=True)
    )
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    return company.id


class TestHistoricalSecuritiesStayOutOfTheLiveUniverse:
    """Requirement 9. `companies` is what is investable *now*, and only that."""

    def test_resolving_a_delisted_symbol_creates_no_company_row(
        self, factory: sessionmaker[Session]
    ) -> None:
        load_evidence(
            factory,
            [
                ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
            ],
        )
        with session_scope(factory) as session:
            add_delisting(session, "BBBY", date(2023, 5, 2))
            before = CompanyRepository(session).count()

        report = resolve_delisted_symbols(factory)

        with session_scope(factory) as session:
            assert CompanyRepository(session).count() == before == 0
            assert HistoricalSecurityRepository(session).count() == 1
        assert len(report.resolved) == 1

    def test_a_resolved_security_is_addressable_without_a_company(
        self, factory: sessionmaker[Session]
    ) -> None:
        load_evidence(
            factory,
            [
                ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
            ],
        )
        with session_scope(factory) as session:
            add_delisting(session, "BBBY", date(2023, 5, 2))
        resolve_delisted_symbols(factory)

        with session_scope(factory) as session:
            intervals = SecurityIdentifierRepository(session).resolve_ticker(
                "BBBY", date(2022, 6, 30)
            )
            assert len(intervals) == 1
            assert intervals[0].cusip == BBBY_CUSIP
            # And the security it names carries no CIK, which is requirement 6
            # in its stored form: the free record establishes none, and none was
            # invented to satisfy a column.
            security = HistoricalSecurityRepository(session).get(
                intervals[0].historical_security_id
            )
            assert security is not None
            assert security.cik is None

    def test_a_reused_symbol_resolves_by_date_and_never_splices(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Requirement 5, end to end through the database."""
        load_evidence(
            factory,
            [
                ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20250714", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
                ("20260630", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
            ],
        )
        with session_scope(factory) as session:
            add_delisting(session, "BBBY", date(2023, 5, 2))

        report = resolve_delisted_symbols(factory)

        assert len(report.resolved) == 1
        assert report.resolved[0].is_ticker_reuse
        with session_scope(factory) as session:
            # Only Bed Bath's own issue is stored against the security. The
            # later occupant's CUSIP is evidence, and is not part of this
            # security's identity.
            intervals = SecurityIdentifierRepository(session).for_security(
                report.resolved[0].security_id or 0
            )
            assert [row.cusip for row in intervals] == [BBBY_CUSIP]
            assert (
                SecurityIdentifierRepository(session).resolve_ticker("BBBY", date(2025, 9, 30))
                == []
            )

    def test_a_symbol_with_no_evidence_is_unresolved_not_invented(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Requirement 7, stored form: absence creates nothing and claims nothing."""
        with session_scope(factory) as session:
            add_delisting(session, "GHOST", date(2015, 4, 1))

        report = resolve_delisted_symbols(factory)

        assert len(report.unresolved) == 1
        assert "establishes nothing" in report.unresolved[0].detail
        with session_scope(factory) as session:
            assert HistoricalSecurityRepository(session).count() == 0

    def test_evidence_fitting_two_issuers_at_once_creates_nothing(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Requirement 8, stored form: disagreement is a status, not a choice."""
        load_evidence(
            factory,
            [
                ("20200102", BBBY_CUSIP, "CLASH", "ONE INC"),
                ("20220103", BBBY_CUSIP, "CLASH", "ONE INC"),
                ("20210104", OTHER_CUSIP, "CLASH", "TWO CORP"),
                ("20230103", OTHER_CUSIP, "CLASH", "TWO CORP"),
            ],
        )
        with session_scope(factory) as session:
            add_delisting(session, "CLASH", date(2023, 2, 1))

        report = resolve_delisted_symbols(factory)

        assert len(report.ambiguous) == 1
        with session_scope(factory) as session:
            assert HistoricalSecurityRepository(session).count() == 0
            assert SecurityIdentifierRepository(session).count() == 0


class TestForwardReturnsCarryExactlyOneIdentity:
    """Requirement 11. Two claims about one measurement, or none, are both refused."""

    def _security(self, session: Session) -> int:
        security = HistoricalSecurityRepository(session).create(
            cik=None, name="Gone Inc", primary_ticker="GONE", status="BANKRUPT"
        )
        return security.id

    def test_naming_both_identities_is_refused(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            security_id = self._security(session)
            company = _seed_company(session)

        with (
            pytest.raises(IntegrityError, match="ck_forward_return_one_identity"),
            session_scope(factory) as session,
        ):
            session.execute(
                insert(ForwardReturnRecord),
                [
                    {
                        "company_id": company,
                        "historical_security_id": security_id,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "TRUNCATED",
                    }
                ],
            )

    def test_naming_neither_identity_is_refused(self, factory: sessionmaker[Session]) -> None:
        with (
            pytest.raises(IntegrityError, match="ck_forward_return_one_identity"),
            session_scope(factory) as session,
        ):
            session.execute(
                insert(ForwardReturnRecord),
                [
                    {
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "TRUNCATED",
                    }
                ],
            )

    def test_either_identity_alone_is_accepted(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            security_id = self._security(session)
            company = _seed_company(session)

        with session_scope(factory) as session:
            session.execute(
                insert(ForwardReturnRecord),
                [
                    {
                        "company_id": company,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "TRUNCATED",
                    },
                    {
                        "historical_security_id": security_id,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "INTERRUPTED",
                    },
                ],
            )

        with session_scope(factory) as session:
            assert len(list(session.scalars(select(ForwardReturnRecord)))) == 2


class TestDuplicateMeasurementsAreRefusedPerIdentityKind:
    """Requirement 12. One rule, enforced separately for each kind of identity."""

    def test_a_repeated_company_measurement_is_refused(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            company = _seed_company(session)
        row = {
            "company_id": company,
            "observation_date": date(2024, 6, 28),
            "horizon_sessions": 20,
            "status": "TRUNCATED",
        }
        with session_scope(factory) as session:
            session.execute(insert(ForwardReturnRecord), [row])

        with (
            pytest.raises(IntegrityError, match=r"uq_forward_return_company|UNIQUE"),
            session_scope(factory) as session,
        ):
            session.execute(insert(ForwardReturnRecord), [row])

    def test_a_repeated_historical_measurement_is_refused(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).create(
                cik=None, name="Gone Inc", primary_ticker="GONE", status="BANKRUPT"
            )
            security_id = security.id
        row = {
            "historical_security_id": security_id,
            "observation_date": date(2024, 6, 28),
            "horizon_sessions": 20,
            "status": "INTERRUPTED",
        }
        with session_scope(factory) as session:
            session.execute(insert(ForwardReturnRecord), [row])

        with (
            pytest.raises(IntegrityError, match=r"uq_forward_return_historical|UNIQUE"),
            session_scope(factory) as session,
        ):
            session.execute(insert(ForwardReturnRecord), [row])

    def test_the_two_kinds_do_not_collide_with_one_another(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A company and a security sharing a primary key are still two securities."""
        with session_scope(factory) as session:
            company = _seed_company(session)
            security = HistoricalSecurityRepository(session).create(
                cik=None, name="Gone Inc", primary_ticker="GONE", status="BANKRUPT"
            )
            security_id = security.id

        with session_scope(factory) as session:
            session.execute(
                insert(ForwardReturnRecord),
                [
                    {
                        "company_id": company,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "TRUNCATED",
                    },
                    {
                        "historical_security_id": security_id,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "INTERRUPTED",
                    },
                ],
            )

        with session_scope(factory) as session:
            assert len(list(session.scalars(select(ForwardReturnRecord)))) == 2


class TestExistingMeasurementsAreUntouched:
    """Requirement 10. Phase 9A changes the shape and moves not one row."""

    def test_a_company_measurement_keeps_every_value_and_gains_a_null(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            company = _seed_company(session)
            session.execute(
                insert(ForwardReturnRecord),
                [
                    {
                        "company_id": company,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": 20,
                        "status": "REALISED",
                        "entry_date": date(2024, 7, 1),
                        "entry_price": 10.0,
                        "exit_date": date(2024, 7, 29),
                        "exit_price": 11.0,
                        "total_return": 0.1,
                    }
                ],
            )

        with session_scope(factory) as session:
            row = session.scalars(select(ForwardReturnRecord)).one()
            assert row.company_id == company
            assert row.historical_security_id is None
            assert row.status == "REALISED"
            assert row.total_return == pytest.approx(0.1)

    def test_the_audit_reports_zero_historical_backed_measurements(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            company = _seed_company(session)
            session.execute(
                insert(ForwardReturnRecord),
                [
                    {
                        "company_id": company,
                        "observation_date": date(2024, 6, 28),
                        "horizon_sessions": horizon,
                        "status": "TRUNCATED",
                    }
                    for horizon in (1, 5, 20, 60, 120)
                ],
            )

        with session_scope(factory) as session:
            audit = build_identity_audit(session)

        assert audit.forward_returns_total == 5
        assert audit.forward_returns_company == 5
        assert audit.forward_returns_historical == 0


class TestContaminationIsReportedNotRepaired:
    """The live-row finding, its two filters, and the boundary around it."""

    def _bars(self, session: Session, company_id: int, days: list[date]) -> None:
        """Give a live row stored price bars on specific dates."""
        PriceHistoryRepository(session).upsert_bars(
            company_id,
            [
                PriceBar(date=day, open=1.0, high=1.0, low=1.0, close=1.0, volume=100.0)
                for day in days
            ],
        )

    def test_a_row_whose_bars_span_a_change_of_hands_is_reported(
        self, factory: sessionmaker[Session]
    ) -> None:
        load_evidence(
            factory,
            [
                ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20250714", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
                ("20260630", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
            ],
        )
        with session_scope(factory) as session:
            company_id = _seed_company(session, ticker="BBBY", name="Neighborhood Intelligence")
            # The twenty-six-month hole in `companies` row 2147: bars on both
            # sides of a handover the row has no business spanning.
            self._bars(session, company_id, [date(2022, 6, 30), date(2025, 9, 30)])

            report = detect_contaminated_companies(session)

        assert len(report.contaminated) == 1
        assert report.contaminated[0].company_id == company_id
        assert report.contaminated[0].issuers == ("075896", "690370")
        assert report.changes_of_hands == 1
        assert report.reorganisations == 0

    def test_a_two_day_handover_is_a_reorganisation_and_not_a_finding(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Accenture Ltd became Accenture plc: new CUSIP issuer, one company."""
        load_evidence(
            factory,
            [
                ("20040326", "G1150G109", "ACN", "ACCENTURE LTD"),
                ("20090831", "G1150G109", "ACN", "ACCENTURE LTD"),
                ("20090902", "G1151C101", "ACN", "ACCENTURE PLC"),
                ("20260812", "G1151C101", "ACN", "ACCENTURE PLC"),
            ],
        )
        with session_scope(factory) as session:
            company_id = _seed_company(session, ticker="ACN", name="Accenture plc")
            self._bars(session, company_id, [date(2005, 6, 30), date(2020, 6, 30)])

            report = detect_contaminated_companies(session)

        assert report.multi_issuer == 1
        assert report.reorganisations == 1
        assert report.changes_of_hands == 0
        assert report.contaminated == ()

    def test_a_row_that_traded_through_the_gap_is_not_a_finding(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A gap in the fails record is a gap in *failures*, not in trading.

        Amalgamated's shape: the CUSIP issuer changed at a holding-company
        reorganisation, the security never stopped trading, and the fails record
        simply went quiet for two years in between. Reading that as two companies
        would put a scalpel to a row that was only ever one.
        """
        load_evidence(
            factory,
            [
                ("20200103", BBBY_CUSIP, "AMAL", "ONE INC"),
                ("20210225", BBBY_CUSIP, "AMAL", "ONE INC"),
                ("20230713", OTHER_CUSIP, "AMAL", "ONE HOLDINGS INC"),
                ("20260630", OTHER_CUSIP, "AMAL", "ONE HOLDINGS INC"),
            ],
        )
        with session_scope(factory) as session:
            company_id = _seed_company(session, ticker="AMAL", name="One Holdings")
            # Bars every fortnight straight through the handover window.
            self._bars(
                session,
                company_id,
                [date(2021, 1, 4) + timedelta(days=14 * step) for step in range(90)],
            )

            report = detect_contaminated_companies(session)

        assert report.changes_of_hands == 1
        assert report.spanned == 1
        assert report.traded_through == 1
        assert report.contaminated == ()

    def test_a_reuse_the_row_does_not_span_is_not_a_finding(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The earlier occupant never entered this row, so the row is not mixed."""
        load_evidence(
            factory,
            [
                ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20250714", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
                ("20260630", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
            ],
        )
        with session_scope(factory) as session:
            company_id = _seed_company(session, ticker="BBBY", name="An Unrelated Issuer")
            # Only the later issuer's era is stored.
            self._bars(session, company_id, [date(2025, 9, 30), date(2026, 1, 5)])

            report = detect_contaminated_companies(session)

        assert report.changes_of_hands == 1
        assert report.contaminated == ()

    def test_the_scan_writes_nothing(self, factory: sessionmaker[Session]) -> None:
        load_evidence(
            factory,
            [
                ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
                ("20250714", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
                ("20260630", OTHER_CUSIP, "BBBY", "AN UNRELATED ISSUER INC"),
            ],
        )
        with session_scope(factory) as session:
            company_id = _seed_company(session, ticker="BBBY", name="Neighborhood Intelligence")
            self._bars(session, company_id, [date(2022, 6, 30), date(2025, 9, 30)])

        with session_scope(factory) as session:
            before = (
                CompanyRepository(session).count(),
                HistoricalSecurityRepository(session).count(),
                SecurityIdentifierRepository(session).count(),
                PriceHistoryRepository(session).count(),
            )
            assert detect_contaminated_companies(session).contaminated != ()

        with session_scope(factory) as session:
            assert (
                CompanyRepository(session).count(),
                HistoricalSecurityRepository(session).count(),
                SecurityIdentifierRepository(session).count(),
                PriceHistoryRepository(session).count(),
            ) == before

    def test_a_live_row_absent_from_the_fails_record_is_not_a_finding(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A company whose deliveries never failed is absent, and that is all."""
        with session_scope(factory) as session:
            _seed_company(session, ticker="CLEAN", name="Clean Inc")
            report = detect_contaminated_companies(session)

        assert report.contaminated == ()
        assert report.checked == 1
        assert report.with_evidence == 0
        assert report.without_evidence == 1


class TestTheEvidenceIngestIsIdempotent:
    """A re-read widens no window and changes no total."""

    def test_reading_one_archive_twice_leaves_the_evidence_unchanged(
        self, factory: sessionmaker[Session]
    ) -> None:
        rows = [
            ("20220103", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
            ("20230502", BBBY_CUSIP, "BBBY", "BED BATH & BEYOND INC"),
        ]
        content = parse_ftd_archive(
            "cnsfailstest.zip", "https://example.test/x.zip", ftd_archive("cnsfailstest", rows)
        )
        ingest_ftd_file(factory, content)
        with session_scope(factory) as session:
            first = FtdIdentityObservationRepository(session).for_symbol("BBBY")
            before = [
                (row.first_settlement_date, row.last_settlement_date, row.observation_count)
                for row in first
            ]

        ingest_ftd_file(factory, content)

        with session_scope(factory) as session:
            after_rows = FtdIdentityObservationRepository(session).for_symbol("BBBY")
            after = [
                (row.first_settlement_date, row.last_settlement_date, row.observation_count)
                for row in after_rows
            ]
        assert before == [(date(2022, 1, 3), date(2023, 5, 2), 2)]
        assert after == before

    def test_a_second_issue_of_one_issuer_becomes_a_cusip_lineage(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Requirement 3's stored form: two CUSIPs, one security_id."""
        load_evidence(
            factory,
            [
                ("20180102", BBBY_CUSIP, "RSPL", "ONE INC"),
                ("20230227", BBBY_CUSIP, "RSPL", "ONE INC"),
                ("20230306", BBBY_SECOND_ISSUE, "RSPL", "ONE INC"),
                ("20240930", BBBY_SECOND_ISSUE, "RSPL", "ONE INC"),
            ],
        )
        with session_scope(factory) as session:
            add_delisting(session, "RSPL", date(2024, 10, 1))

        report = resolve_delisted_symbols(factory)

        assert len(report.resolved) == 1
        assert report.resolved[0].is_cusip_lineage
        with session_scope(factory) as session:
            intervals = SecurityIdentifierRepository(session).for_security(
                report.resolved[0].security_id or 0
            )
            assert [row.cusip for row in intervals] == [BBBY_CUSIP, BBBY_SECOND_ISSUE]
            assert len({row.historical_security_id for row in intervals}) == 1

    def test_the_stored_intervals_resolve_through_the_domain_rules(
        self, factory: sessionmaker[Session]
    ) -> None:
        load_evidence(
            factory,
            [
                ("20180102", BBBY_CUSIP, "RSPL", "ONE INC"),
                ("20230227", BBBY_CUSIP, "RSPL", "ONE INC"),
                ("20230306", BBBY_SECOND_ISSUE, "RSPL", "ONE INC"),
                ("20240930", BBBY_SECOND_ISSUE, "RSPL", "ONE INC"),
            ],
        )
        with session_scope(factory) as session:
            add_delisting(session, "RSPL", date(2024, 10, 1))
        report = resolve_delisted_symbols(factory)
        security_id = report.resolved[0].security_id

        with session_scope(factory) as session:
            stored = SecurityIdentifierRepository(session).for_security(security_id or 0)
            projected = [to_identifier_interval(row) for row in stored]

        early = resolve_by_ticker(projected, "RSPL", date(2019, 6, 3))
        late = resolve_by_ticker(projected, "RSPL", date(2024, 1, 2))
        assert early.status is ResolutionStatus.RESOLVED
        assert early.security_id == late.security_id == security_id
