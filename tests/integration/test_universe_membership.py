"""Adversarial tests for historical universe membership — Phase 9C.

Membership is the easiest thing in this project to get wrong in a way that looks
right: today's roster, filtered by today's attributes, returns a plausible
universe for any past date and is survivorship bias in its purest form. Every
test here names one route to that failure and asserts the code refuses it.

Three matter most.

`test_a_security_first_traced_in_2024_is_not_a_member_in_2022` is case F: a
company that exists today must not be admitted to a past universe merely because
its row exists now.

`test_membership_survives_the_deletion_of_every_company_row` is the survivorship
invariant: the live table is emptied and the historical members are still there.

`TestPricePresenceIsNotMembership` is Rule B, asserted directly — a security with
a full price series and no listing attributes is `UNKNOWN`, never a member.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, insert

from data_access import (
    CompanyRepository,
    DelistingRepository,
    FundamentalObservationRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    PriceHistoryRepository,
    SecurityIdentifierRepository,
    UniverseMembershipRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import Company, UniverseMembershipRecord
from domain import (
    CompanyProfile,
    CorporateActionKind,
    IdentityConfidence,
    LineageKind,
    MembershipConfidence,
    MembershipReason,
    MembershipState,
    PriceBar,
)
from stock_screener.backtest import (
    members_as_of,
    membership_for_security,
    reconstruct_membership_record,
)
from stock_screener.backtest.membership import COMPANY, HISTORICAL

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

#: The record's floor in these fixtures — the earliest filing any security has.
FLOOR = date(2015, 1, 2)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'membership.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def bars(start: date, count: int, *, step: int = 7) -> list[PriceBar]:
    """A sparse bar series, enough to establish presence over a window."""
    return [
        PriceBar(
            date=start + timedelta(days=step * index),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.0,
            volume=100_000.0,
        )
        for index in range(count)
    ]


def seed_company(
    session: Session,
    ticker: str,
    *,
    exchange: str | None = "NASDAQ",
    first_filing: date = FLOOR,
    price_from: date | None = None,
    sector: str | None = None,
) -> int:
    """Insert a live company with a dated filing trace."""
    CompanyRepository(session).upsert_profile(
        CompanyProfile(
            ticker=ticker,
            name=f"{ticker} Inc",
            exchange=exchange,
            is_active=True,
            sector=sector,
        )
    )
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    FundamentalObservationRepository(session).add_all(
        company.id,
        [
            {
                "observation_fingerprint": f"{ticker}-first",
                "taxonomy": "us-gaap",
                "concept": "Revenues",
                "value": 1.0,
                "unit": "USD",
                "period_end": first_filing,
                "filed_date": first_filing,
                "source": "test",
            }
        ],
    )
    if price_from is not None:
        PriceHistoryRepository(session).upsert_bars(company.id, bars(price_from, 200))
    return company.id


def seed_security(
    session: Session,
    ticker: str,
    *,
    exchange: str | None = "NASDAQ",
    cusip: str = "075896100",
    valid_from: date,
    valid_to: date | None = None,
    last_trading_date: date | None = None,
    price_from: date | None = None,
    cik: int | None = None,
) -> int:
    """Insert a historical security with a dated identifier interval."""
    security = HistoricalSecurityRepository(session).create(
        cik=cik,
        name=f"{ticker} Inc",
        primary_ticker=ticker,
        exchange=exchange,
        status="UNKNOWN",
        last_trading_date=last_trading_date or valid_to,
    )
    SecurityIdentifierRepository(session).add(
        historical_security_id=security.id,
        cusip=cusip,
        ticker=ticker,
        venue=None,
        valid_from=valid_from,
        valid_to=valid_to,
        source="SEC_FTD",
        confidence=str(IdentityConfidence.OBSERVED),
        lineage=str(LineageKind.INITIAL),
    )
    if price_from is not None:
        HistoricalPriceRepository(session).upsert_bars(security.id, bars(price_from, 200))
    return security.id


def add_delisting(session: Session, ticker: str, effective: date) -> None:
    """Record one dated exit."""
    DelistingRepository(session).add_all(
        [
            {
                "ticker": ticker,
                "kind": CorporateActionKind.CASH_MERGER.value,
                "effective_date": effective,
                "source": "test",
                "source_id": f"{ticker}-{effective}",
            }
        ]
    )


def states(session: Session, as_of: date) -> dict[tuple[str, int], MembershipState]:
    """Return every security's state on a date, unknowns included."""
    universe = members_as_of(session, as_of)
    found: dict[tuple[str, int], MembershipState] = {}
    for kind, identity, _ in universe.members:
        found[(kind, identity)] = MembershipState.MEMBER
    for kind, identity, _ in universe.non_members:
        found[(kind, identity)] = MembershipState.NON_MEMBER
    for kind, identity in universe.unknown:
        found[(kind, identity)] = MembershipState.UNKNOWN
    return found


class TestSurvivorsAndExits:
    """Cases A, B and G — presence before an exit, absence after it."""

    def test_a_survivor_is_a_member_throughout(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            company_id = seed_company(session, "AAA", price_from=FLOOR)
            reconstruct_membership_record(session)

            for when in (date(2018, 6, 1), date(2022, 6, 1), date(2026, 1, 2)):
                assert states(session, when)[(COMPANY, company_id)] is MembershipState.MEMBER

    def test_a_delisted_security_is_a_member_before_its_exit_and_not_after(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Cases B and G together: eligible in 2024, gone from 2025."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session,
                "GONE",
                valid_from=date(2019, 3, 1),
                valid_to=date(2025, 4, 30),
                price_from=date(2019, 3, 1),
            )
            add_delisting(session, "GONE", date(2025, 5, 1))
            reconstruct_membership_record(session)

            before = states(session, date(2024, 6, 28))
            after = states(session, date(2025, 9, 30))

        assert before[(HISTORICAL, security_id)] is MembershipState.MEMBER
        assert after[(HISTORICAL, security_id)] is MembershipState.NON_MEMBER

    def test_the_exit_verdict_is_observed_not_inferred(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A dated exit is the one thing this layer can observe outright."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session, "GONE", valid_from=date(2019, 3, 1), valid_to=date(2025, 4, 30)
            )
            add_delisting(session, "GONE", date(2025, 5, 1))
            reconstruct_membership_record(session)

            verdict = membership_for_security(session, date(2025, 9, 30), security_id=security_id)

        assert verdict.state is MembershipState.NON_MEMBER
        assert verdict.confidence is MembershipConfidence.OBSERVED
        assert verdict.reason is MembershipReason.DELISTED

    def test_membership_survives_the_deletion_of_every_company_row(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Case I and the survivorship invariant, in its strongest form."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session,
                "GONE",
                valid_from=date(2019, 3, 1),
                valid_to=date(2025, 4, 30),
                price_from=date(2019, 3, 1),
            )
            reconstruct_membership_record(session)
            before = membership_for_security(session, date(2024, 6, 28), security_id=security_id)

        with session_scope(factory) as session:
            session.execute(delete(Company))

        with session_scope(factory) as session:
            assert CompanyRepository(session).count() == 0
            universe = members_as_of(session, date(2024, 6, 28))
            after = membership_for_security(session, date(2024, 6, 28), security_id=security_id)

        assert after == before
        assert after.state is MembershipState.MEMBER
        assert universe.historical_members == 1
        assert universe.company_members == 0


class TestFutureInformationCannotLeakBackward:
    """Cases D and F, and the Phase 9C invariant that guards them."""

    def test_a_security_first_traced_in_2024_is_not_a_member_in_2022(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Case F. Existing today is not evidence of existing in 2022."""
        with session_scope(factory) as session:
            seed_company(session, "OLD", price_from=FLOOR)
            new_id = seed_company(
                session, "NEW", first_filing=date(2024, 2, 1), price_from=date(2024, 2, 1)
            )
            reconstruct_membership_record(session)

            in_2022 = states(session, date(2022, 6, 30))
            in_2025 = states(session, date(2025, 6, 30))

        assert in_2022[(COMPANY, new_id)] is MembershipState.NON_MEMBER
        assert in_2025[(COMPANY, new_id)] is MembershipState.MEMBER

    def test_that_verdict_is_observed_because_the_record_reaches_further_back(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            seed_company(session, "OLD", price_from=FLOOR)
            new_id = seed_company(session, "NEW", first_filing=date(2024, 2, 1))
            reconstruct_membership_record(session)
            verdict = membership_for_security(session, date(2022, 6, 30), company_id=new_id)

        assert verdict.confidence is MembershipConfidence.OBSERVED
        assert verdict.reason is MembershipReason.NOT_YET_IN_RECORD

    def test_a_security_first_traced_at_the_record_floor_is_unknown_before_it(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The record's own reach is not evidence about the market."""
        with session_scope(factory) as session:
            company_id = seed_company(session, "OLD", first_filing=FLOOR)
            reconstruct_membership_record(session)
            verdict = membership_for_security(
                session, FLOOR - timedelta(days=365), company_id=company_id
            )

        assert verdict.state is MembershipState.UNKNOWN

    def test_a_delisting_does_not_remove_the_security_from_earlier_dates(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Rule D in the other direction: a future exit cannot rewrite the past."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session, "EXIT", valid_from=date(2019, 1, 2), valid_to=date(2025, 4, 30)
            )
            reconstruct_membership_record(session)
            before_exit = membership_for_security(
                session, date(2023, 6, 30), security_id=security_id
            )

        with session_scope(factory) as session:
            add_delisting(session, "EXIT", date(2025, 5, 1))
            reconstruct_membership_record(session)
            after_exit = membership_for_security(
                session, date(2023, 6, 30), security_id=security_id
            )
            at_exit = membership_for_security(session, date(2025, 9, 30), security_id=security_id)

        assert after_exit.state is before_exit.state is MembershipState.MEMBER
        assert at_exit.state is MembershipState.NON_MEMBER


class TestPricePresenceIsNotMembership:
    """Rule B, asserted directly rather than trusted."""

    def test_a_fully_priced_security_with_no_venue_is_unknown(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session,
                "PRICED",
                exchange=None,
                valid_from=date(2019, 1, 2),
                valid_to=date(2026, 1, 2),
                price_from=date(2019, 1, 2),
            )
            reconstruct_membership_record(session)
            verdict = membership_for_security(session, date(2023, 6, 30), security_id=security_id)

        assert verdict.state is MembershipState.UNKNOWN
        assert verdict.reason is MembershipReason.NO_DATED_EVIDENCE

    def test_an_unsupported_venue_is_a_non_member_however_well_priced(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session,
                "ARCAETF",
                exchange="ARCA",
                valid_from=date(2019, 1, 2),
                valid_to=date(2026, 1, 2),
                price_from=date(2019, 1, 2),
            )
            reconstruct_membership_record(session)
            verdict = membership_for_security(session, date(2023, 6, 30), security_id=security_id)

        assert verdict.state is MembershipState.NON_MEMBER
        assert verdict.reason is MembershipReason.UNSUPPORTED_LISTING


class TestInferenceIsNeverDressedAsObservation:
    """Rule E and section 8 — the labelling that makes the design honest."""

    def test_every_member_verdict_is_inferred_and_names_what_it_carried(
        self, factory: sessionmaker[Session]
    ) -> None:
        """No dated venue evidence exists, so no member can be observed."""
        with session_scope(factory) as session:
            seed_company(session, "AAA", price_from=FLOOR)
            reconstruct_membership_record(session)
            universe = members_as_of(session, date(2023, 6, 30))

        assert universe.members
        assert universe.observed_members == 0
        for _, _, verdict in universe.members:
            assert verdict.confidence is MembershipConfidence.INFERRED
            assert "exchange" in verdict.inferred_inputs
            assert "security_type" in verdict.inferred_inputs

    def test_the_stored_row_records_what_was_carried(self, factory: sessionmaker[Session]) -> None:
        with session_scope(factory) as session:
            company_id = seed_company(session, "AAA", price_from=FLOOR)
            reconstruct_membership_record(session)
            rows = UniverseMembershipRepository(session).for_company(company_id)

        member = next(row for row in rows if row.state == str(MembershipState.MEMBER))
        assert member.confidence == str(MembershipConfidence.INFERRED)
        assert member.inferred_inputs is not None
        assert "exchange" in member.inferred_inputs

    def test_a_security_with_no_dated_trace_has_no_intervals_at_all(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Unknown by omission, never by an invented window."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security = HistoricalSecurityRepository(session).create(
                cik=None, name="Traceless Inc", primary_ticker="NONE", status="UNKNOWN"
            )
            security_id = security.id
            reconstruct_membership_record(session)
            rows = UniverseMembershipRepository(session).for_historical_security(security_id)
            verdict = membership_for_security(session, date(2023, 6, 30), security_id=security_id)

        assert rows == []
        assert verdict.state is MembershipState.UNKNOWN
        assert verdict.reason is MembershipReason.NO_DATED_EVIDENCE

    def test_every_security_lands_in_exactly_one_group(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A universe that loses securities between its groups hides its own size.

        The first reconstruction did exactly that: a security whose covering
        interval was `UNKNOWN` — placed in the market but with no listing
        attributes to qualify it — matched neither the member nor the non-member
        branch and was not counted as unknown either, so 1,530 of 8,919
        securities silently vanished from the audit.
        """
        with session_scope(factory) as session:
            seed_company(session, "AAA", price_from=FLOOR)
            seed_company(session, "BBB", price_from=FLOOR)
            # Placed in the market by its identifier interval, but with no venue
            # to judge it by — the shape that used to disappear.
            seed_security(
                session,
                "NOVENUE",
                exchange=None,
                valid_from=date(2019, 1, 2),
                valid_to=date(2026, 1, 2),
                price_from=date(2019, 1, 2),
            )
            seed_security(session, "GONE", valid_from=date(2019, 1, 2), valid_to=date(2021, 4, 30))
            reconstruct_membership_record(session)
            universe = members_as_of(session, date(2023, 6, 30))
            total = (
                CompanyRepository(session).count() + HistoricalSecurityRepository(session).count()
            )

        counted = len(universe.members) + len(universe.non_members) + len(universe.unknown)
        assert counted == total == 4

    def test_unknown_securities_are_reported_not_dropped(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security = HistoricalSecurityRepository(session).create(
                cik=None, name="Traceless Inc", primary_ticker="NONE", status="UNKNOWN"
            )
            security_id = security.id
            reconstruct_membership_record(session)
            universe = members_as_of(session, date(2023, 6, 30))

        assert (HISTORICAL, security_id) in universe.unknown


class TestIdentityIsNeverTheTicker:
    """Cases C, D, E, J, K and L — two securities must not share a history."""

    def test_ticker_reuse_gives_two_independent_membership_histories(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Case L, the BBBY shape."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            old = seed_security(
                session,
                "BBBY",
                cusip="075896100",
                valid_from=date(2019, 1, 2),
                valid_to=date(2023, 5, 2),
                price_from=date(2019, 1, 2),
            )
            new = seed_security(
                session,
                "BBBY",
                cusip="690370101",
                valid_from=date(2025, 9, 2),
                valid_to=None,
                last_trading_date=None,
                price_from=date(2025, 9, 2),
            )
            add_delisting(session, "BBBY", date(2023, 5, 3))
            reconstruct_membership_record(session)

            during_old = states(session, date(2022, 6, 30))
            during_gap = states(session, date(2024, 6, 28))
            during_new = states(session, date(2026, 1, 5))

        assert old != new
        # Bed Bath is a member in its own era and gone afterwards.
        assert during_old[(HISTORICAL, old)] is MembershipState.MEMBER
        assert during_gap[(HISTORICAL, old)] is MembershipState.NON_MEMBER
        # Its successor is not a member before it existed, and never inherits
        # the exit recorded under the shared symbol.
        assert during_old[(HISTORICAL, new)] is MembershipState.NON_MEMBER
        assert during_new[(HISTORICAL, new)] is MembershipState.MEMBER

    def test_the_exit_attaches_only_to_the_issuer_whose_window_contains_it(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            old = seed_security(
                session,
                "BBBY",
                cusip="075896100",
                valid_from=date(2019, 1, 2),
                valid_to=date(2023, 5, 2),
            )
            new = seed_security(
                session,
                "BBBY",
                cusip="690370101",
                valid_from=date(2025, 9, 2),
                valid_to=None,
                last_trading_date=None,
            )
            add_delisting(session, "BBBY", date(2023, 5, 3))
            reconstruct_membership_record(session)

            old_rows = UniverseMembershipRepository(session).for_historical_security(old)
            new_rows = UniverseMembershipRepository(session).for_historical_security(new)

        assert any(row.reason == str(MembershipReason.DELISTED) for row in old_rows)
        assert not any(row.reason == str(MembershipReason.DELISTED) for row in new_rows)

    def test_a_ticker_change_keeps_one_continuous_membership(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Case E. One security, two symbols, one uninterrupted membership."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="Renamed Inc",
                primary_ticker="NEW",
                exchange="NASDAQ",
                status="UNKNOWN",
                last_trading_date=date(2026, 1, 2),
            )
            security_id = security.id
            repository = SecurityIdentifierRepository(session)
            for ticker, start, end in (
                ("OLD", date(2019, 1, 2), date(2022, 6, 8)),
                ("NEW", date(2022, 6, 9), None),
            ):
                repository.add(
                    historical_security_id=security_id,
                    cusip="30303M102",
                    ticker=ticker,
                    venue=None,
                    valid_from=start,
                    valid_to=end,
                    source="test",
                    confidence=str(IdentityConfidence.OBSERVED),
                    lineage=str(LineageKind.TICKER_CHANGE),
                )
            reconstruct_membership_record(session)

            before = membership_for_security(session, date(2020, 6, 30), security_id=security_id)
            after = membership_for_security(session, date(2024, 6, 28), security_id=security_id)

        assert before.state is after.state is MembershipState.MEMBER

    def test_an_acquiree_and_its_acquirer_keep_separate_memberships(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Case C. The acquiree's membership ends; the acquirer's does not."""
        with session_scope(factory) as session:
            acquirer = seed_company(session, "BUYER", price_from=FLOOR)
            acquiree = seed_security(
                session,
                "TARGET",
                cusip="00507V109",
                valid_from=date(2019, 1, 2),
                valid_to=date(2023, 10, 13),
                price_from=date(2019, 1, 2),
            )
            add_delisting(session, "TARGET", date(2023, 10, 14))
            reconstruct_membership_record(session)

            after = states(session, date(2024, 6, 28))

        assert after[(HISTORICAL, acquiree)] is MembershipState.NON_MEMBER
        assert after[(COMPANY, acquirer)] is MembershipState.MEMBER

    def test_a_corporate_action_neither_creates_nor_destroys_membership(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Case J. A reverse split is an identifier event, not a listing one."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="Split Inc",
                primary_ticker="RSPL",
                exchange="NASDAQ",
                status="UNKNOWN",
                last_trading_date=date(2026, 1, 2),
            )
            security_id = security.id
            SecurityIdentifierRepository(session).add(
                historical_security_id=security_id,
                cusip="075896100",
                ticker="RSPL",
                venue=None,
                valid_from=date(2019, 1, 2),
                valid_to=date(2023, 2, 28),
                source="test",
                confidence=str(IdentityConfidence.OBSERVED),
                lineage=str(LineageKind.INITIAL),
            )
            reconstruct_membership_record(session)
            before = membership_for_security(session, date(2024, 6, 28), security_id=security_id)

        with session_scope(factory) as session:
            # The split mints a second CUSIP against the same security.
            SecurityIdentifierRepository(session).add(
                historical_security_id=security_id,
                cusip="075896209",
                ticker="RSPL",
                venue=None,
                valid_from=date(2023, 3, 1),
                valid_to=None,
                source="test",
                confidence=str(IdentityConfidence.ASSERTED),
                lineage=str(LineageKind.REVERSE_SPLIT),
            )
            reconstruct_membership_record(session)
            after = membership_for_security(session, date(2024, 6, 28), security_id=security_id)
            intervals = UniverseMembershipRepository(session).for_historical_security(security_id)

        assert after.state is before.state is MembershipState.MEMBER
        assert intervals


class TestTheStoredRecordHoldsItsInvariants:
    """The constraints the database enforces whatever the code believes."""

    def test_a_row_naming_both_identities_is_refused(self, factory: sessionmaker[Session]) -> None:
        from sqlalchemy.exc import IntegrityError

        with session_scope(factory) as session:
            company_id = seed_company(session, "AAA")
            security_id = seed_security(session, "GONE", valid_from=date(2019, 1, 2))

        with (
            pytest.raises(IntegrityError, match="ck_universe_membership_one_identity"),
            session_scope(factory) as session,
        ):
            session.execute(
                insert(UniverseMembershipRecord),
                [
                    {
                        "company_id": company_id,
                        "historical_security_id": security_id,
                        "state": "MEMBER",
                        "confidence": "INFERRED",
                        "reason": "LISTED",
                        "valid_from": date(2020, 1, 1),
                        "source": "test",
                    }
                ],
            )

    def test_a_row_naming_neither_identity_is_refused(self, factory: sessionmaker[Session]) -> None:
        from sqlalchemy.exc import IntegrityError

        with (
            pytest.raises(IntegrityError, match="ck_universe_membership_one_identity"),
            session_scope(factory) as session,
        ):
            session.execute(
                insert(UniverseMembershipRecord),
                [
                    {
                        "state": "MEMBER",
                        "confidence": "INFERRED",
                        "reason": "LISTED",
                        "valid_from": date(2020, 1, 1),
                        "source": "test",
                    }
                ],
            )

    def test_intervals_for_one_security_never_overlap(self, factory: sessionmaker[Session]) -> None:
        """Exactly one interval must answer for any date it covers."""
        with session_scope(factory) as session:
            seed_company(session, "ANCHOR", price_from=FLOOR)
            security_id = seed_security(
                session, "GONE", valid_from=date(2019, 1, 2), valid_to=date(2025, 4, 30)
            )
            add_delisting(session, "GONE", date(2025, 5, 1))
            reconstruct_membership_record(session)
            rows = UniverseMembershipRepository(session).for_historical_security(security_id)

        spans = [(row.valid_from, row.valid_to or date.max) for row in rows]
        for index, (start, end) in enumerate(spans):
            for other_start, other_end in spans[index + 1 :]:
                assert end < other_start or other_end < start

    def test_a_rerun_is_deterministic(self, factory: sessionmaker[Session]) -> None:
        """The same evidence must produce the same intervals every time."""
        with session_scope(factory) as session:
            seed_company(session, "AAA", price_from=FLOOR)
            seed_security(session, "GONE", valid_from=date(2019, 1, 2), valid_to=date(2025, 4, 30))
            add_delisting(session, "GONE", date(2025, 5, 1))
            reconstruct_membership_record(session)
            first = _snapshot(session)
            reconstruct_membership_record(session)
            second = _snapshot(session)

        assert second == first


def _snapshot(session: Session) -> list[tuple[object, ...]]:
    """Every membership interval's values, for a determinism comparison."""
    repository = UniverseMembershipRepository(session)
    rows: Sequence[UniverseMembershipRecord] = repository.covering(date(2024, 6, 28))
    return sorted(
        (
            str(row.company_id),
            str(row.historical_security_id),
            row.state,
            row.confidence,
            row.reason,
            str(row.valid_from),
            str(row.valid_to),
            str(row.inferred_inputs),
        )
        for row in rows
    )
