"""Adversarial tests for the delisted-security outcome chain — Phase 9B.

The claim this phase makes is narrow and easy to fake: that a security which no
longer exists in `companies` travels the whole way from identity evidence to a
measured outcome, and lands in the *same* query as a survivor. Every test here
attacks one way that claim could be true on paper and false in practice.

Two of them matter more than the rest.

`test_a_historical_security_outlives_the_deletion_of_its_company_row` deletes the
live row outright and re-runs the whole chain. If anything in the path had
quietly been reading `companies` — for a ticker, a name, a calendar, an identity
— this is where it breaks.

`TestTheMixedUniverseQuery` builds a decision date holding two survivors and two
delisted securities and runs the ordinary outcome query. There is no union and no
flag; the delisted names are there because their rows are there, so a study that
forgot they existed would still get them.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    DelistingRepository,
    ForwardReturnRepository,
    HistoricalCorporateActionRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    SecurityIdentifierRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import Company, ForwardReturnRecord
from domain import (
    CompanyProfile,
    CorporateActionKind,
    DelistingReason,
    IdentityConfidence,
    LineageKind,
    OutcomeStatus,
    PriceBar,
    ScoringStatus,
    TerminalStatus,
)
from stock_screener.backtest import (
    IdentityKind,
    outcome_coverage,
    outcome_rows,
    reconstruct_historical_score,
    resolve_terminal_outcome,
)
from stock_screener.pit import measure_forward_returns, measure_historical_forward_returns

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.config import Settings

pytestmark = pytest.mark.integration

BENCHMARK = "SPY"

#: The decision date every test measures from, and the sessions around it.
DECISION = date(2023, 6, 30)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'outcomes.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def sessions(start: date, count: int) -> list[date]:
    """Return `count` consecutive weekday sessions from `start`."""
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def bars(days: Sequence[date], *, price: float = 100.0, step: float = 1.0) -> list[PriceBar]:
    """Return a rising bar series over the given sessions."""
    return [
        PriceBar(
            date=day,
            open=price + step * index,
            high=price + step * index + 1,
            low=price + step * index - 1,
            close=price + step * index,
            volume=1_000.0,
        )
        for index, day in enumerate(days)
    ]


def seed_calendar(session: Session, days: Sequence[date]) -> None:
    """Give the database a benchmark series, which is the session calendar."""
    BenchmarkPriceRepository(session).upsert_bars(BENCHMARK, bars(days))


def seed_company(session: Session, ticker: str, days: Sequence[date]) -> int:
    """Insert one live company with bars over the given sessions."""
    from data_access import PriceHistoryRepository

    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker=ticker, name=f"{ticker} Inc", exchange="NASDAQ", is_active=True)
    )
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    PriceHistoryRepository(session).upsert_bars(company.id, bars(days))
    return company.id


def seed_security(
    session: Session,
    ticker: str,
    days: Sequence[date],
    *,
    cik: int | None = None,
    status: str = "UNKNOWN",
    last_trading_date: date | None = None,
    terminal_value: float | None = None,
    terminal_source: str | None = None,
    cusip: str | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> int:
    """Insert one historical security with bars and, optionally, an identity."""
    security = HistoricalSecurityRepository(session).create(
        cik=cik,
        name=f"{ticker} Inc",
        primary_ticker=ticker,
        status=status,
        last_trading_date=last_trading_date or (days[-1] if days else None),
        terminal_value_per_share=terminal_value,
        terminal_value_source=terminal_source,
    )
    if days:
        HistoricalPriceRepository(session).upsert_bars(security.id, bars(days))
    if cusip is not None:
        SecurityIdentifierRepository(session).add(
            historical_security_id=security.id,
            cusip=cusip,
            ticker=ticker,
            venue=None,
            valid_from=valid_from or days[0],
            valid_to=valid_to,
            source="SEC_FTD",
            confidence=str(IdentityConfidence.OBSERVED),
            lineage=str(LineageKind.INITIAL),
        )
    return security.id


def add_delisting(
    session: Session,
    ticker: str,
    effective: date,
    *,
    kind: CorporateActionKind = CorporateActionKind.CASH_MERGER,
    cash_per_share: float | None = None,
    source_id: str | None = None,
) -> None:
    """Record one delisting the way `discover-delistings` would."""
    DelistingRepository(session).add_all(
        [
            {
                "ticker": ticker,
                "kind": kind.value,
                "effective_date": effective,
                "cash_per_share": cash_per_share,
                "source": "test",
                "source_id": source_id or f"{ticker}-{effective}-{kind.value}",
            }
        ]
    )


class TestASecurityWithNoCompanyRow:
    """Requirements 1 and 14. The chain must not touch `companies` at all."""

    def test_a_delisted_security_is_measured_without_a_company_row(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "GONE", calendar[:40], cusip="075896100")

            result = measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[1, 5, 20]
            )

        assert result.securities == 1
        assert result.priced == 1
        assert result.stored == 3
        with session_scope(factory) as session:
            assert CompanyRepository(session).count() == 0
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)
            assert {row.historical_security_id for row in rows} == {security_id}
            assert {row.company_id for row in rows} == {None}

    def test_a_historical_security_outlives_the_deletion_of_its_company_row(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The survivorship test. Delete the live row; the outcome must survive.

        The most important assertion in Phase 9B. A live `companies` row for the
        same symbol exists while the outcome is measured, and is then deleted
        outright — the way a real delisting removes a name from the investable
        set. Everything downstream must keep working, because none of it should
        ever have been reading that row.
        """
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            company_id = seed_company(session, "GONE", calendar[:40])
            security_id = seed_security(session, "GONE", calendar[:40], cusip="075896100")
            add_delisting(session, "GONE", calendar[39], cash_per_share=42.5)
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[1, 5, 20]
            )

        with session_scope(factory) as session:
            before = outcome_rows(session, DECISION, horizon_sessions=20)
            assert len(before) == 1

        # The live row goes away entirely, cascading through its own children.
        with session_scope(factory) as session:
            session.execute(delete(Company).where(Company.id == company_id))

        with session_scope(factory) as session:
            assert CompanyRepository(session).count() == 0
            after = outcome_rows(session, DECISION, horizon_sessions=20)

        assert len(after) == 1
        assert after[0].identity_kind == IdentityKind.HISTORICAL
        assert after[0].identity_id == security_id
        assert after[0].ticker == "GONE"
        assert after[0].ticker_is_dated
        assert after[0].terminal is not None
        assert after[0].terminal.status is TerminalStatus.SETTLED
        assert after[0].terminal.value_per_share == pytest.approx(42.5)
        assert after == before

    def test_a_cik_unresolved_security_still_gets_outcomes(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Requirement 2. No filer number, no score — and outcomes regardless.

        Price availability and score availability are separate concepts, and
        collapsing them would discard exactly the names a survivorship-aware
        study most needs.
        """
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "NOCIK", calendar[:60], cik=None)
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[1, 5]
            )

        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            assert security is not None
            assert security.cik is None
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)
            assert len(rows) == 2
            assert all(row.status == OutcomeStatus.REALISED.value for row in rows)


class TestTheMixedUniverseQuery:
    """Requirement 11. Two survivors and two delisted names, one query, no union."""

    def _seed(self, factory: sessionmaker[Session]) -> dict[str, int]:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            ids = {
                "A": seed_company(session, "AAA", calendar),
                "B": seed_company(session, "BBB", calendar),
                "C": seed_security(
                    session, "CCC", calendar[:40], cusip="075896100", status="ACQUIRED"
                ),
                "D": seed_security(
                    session, "DDD", calendar[:45], cusip="690370101", status="RECEIVERSHIP"
                ),
            }
            add_delisting(session, "CCC", calendar[39], cash_per_share=95.0)
            measure_forward_returns(session, DECISION, benchmark=BENCHMARK, horizons=[20])
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[20]
            )
        return ids

    def test_all_four_come_back_from_one_ordinary_query(
        self, factory: sessionmaker[Session]
    ) -> None:
        self._seed(factory)

        with session_scope(factory) as session:
            # The plainest possible query: a decision date, and nothing else.
            records = list(
                session.scalars(
                    select(ForwardReturnRecord).where(
                        ForwardReturnRecord.observation_date == DECISION,
                        ForwardReturnRecord.horizon_sessions == 20,
                    )
                )
            )

        assert len(records) == 4
        assert sum(1 for row in records if row.company_id is not None) == 2
        assert sum(1 for row in records if row.historical_security_id is not None) == 2

    def test_the_read_model_labels_both_kinds_without_a_flag(
        self, factory: sessionmaker[Session]
    ) -> None:
        self._seed(factory)

        with session_scope(factory) as session:
            rows = outcome_rows(session, DECISION, horizon_sessions=20)

        assert {row.ticker for row in rows} == {"AAA", "BBB", "CCC", "DDD"}
        assert sum(1 for row in rows if row.is_historical) == 2
        # A survivor has no terminal outcome; a delisted name does.
        assert all(row.terminal is None for row in rows if not row.is_historical)
        assert all(row.terminal is not None for row in rows if row.is_historical)

    def test_a_delisted_name_interrupts_only_where_it_actually_stopped(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The survivors reach the horizon; the securities that stopped do not."""
        self._seed(factory)

        with session_scope(factory) as session:
            rows = outcome_rows(session, DECISION, horizon_sessions=20)

        by_ticker = {row.ticker: row for row in rows}
        assert by_ticker["AAA"].status == OutcomeStatus.REALISED.value
        assert by_ticker["CCC"].status == OutcomeStatus.INTERRUPTED.value
        assert by_ticker["CCC"].total_return is None
        assert by_ticker["CCC"].last_seen_price is not None
        # And a delisted security that traded through its horizon realises like
        # any other. Leaving the market later does not retroactively unmeasure
        # the horizons it completed while still trading.
        assert by_ticker["DDD"].status == OutcomeStatus.REALISED.value
        assert by_ticker["DDD"].total_return is not None

    def test_coverage_counts_both_kinds(self, factory: sessionmaker[Session]) -> None:
        self._seed(factory)

        with session_scope(factory) as session:
            coverage = outcome_coverage(session, DECISION, horizon_sessions=20)

        assert coverage.companies == 2
        assert coverage.historical == 2
        assert coverage.total == 4
        # Three realise and one does not, which is the informative shape: `DDD`
        # was still trading through its whole horizon and so produces a number
        # exactly as a survivor does, while `CCC` stopped inside its horizon.
        # A delisted security is not automatically unmeasurable.
        assert coverage.realised == 3
        assert coverage.interrupted == 1
        assert coverage.terminal_settled == 1
        assert coverage.terminal_unknown == 1


class TestIdentityIsNeverTheTicker:
    """Requirements 3, 4 and 5. Two occupants of one symbol stay two securities."""

    def test_two_issuers_under_one_symbol_keep_separate_outcomes(
        self, factory: sessionmaker[Session]
    ) -> None:
        """`BBBY`: Bed Bath's series must never splice onto Beyond, Inc.'s."""
        calendar = sessions(date(2020, 1, 1), 1200)
        early = calendar[:200]
        late = calendar[900:1100]
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            old = seed_security(
                session,
                "BBBY",
                early,
                cusip="075896100",
                valid_from=early[0],
                valid_to=early[-1],
                status="BANKRUPT",
            )
            new = seed_security(
                session,
                "BBBY",
                late,
                cusip="690370101",
                valid_from=late[0],
                status="UNKNOWN",
            )

        with session_scope(factory) as session:
            old_bars = HistoricalPriceRepository(session).list_for_security(old)
            new_bars = HistoricalPriceRepository(session).list_for_security(new)

        assert old != new
        assert {bar.date for bar in old_bars}.isdisjoint({bar.date for bar in new_bars})
        assert max(bar.date for bar in old_bars) < min(bar.date for bar in new_bars)

    def test_the_symbol_resolves_to_each_on_its_own_dates(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2020, 1, 1), 1200)
        early, late = calendar[:200], calendar[900:1100]
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            old = seed_security(
                session, "BBBY", early, cusip="075896100", valid_from=early[0], valid_to=early[-1]
            )
            new = seed_security(session, "BBBY", late, cusip="690370101", valid_from=late[0])

        with session_scope(factory) as session:
            repository = SecurityIdentifierRepository(session)
            during_old = repository.resolve_ticker("BBBY", early[100])
            during_new = repository.resolve_ticker("BBBY", late[100])

        assert [row.historical_security_id for row in during_old] == [old]
        assert [row.historical_security_id for row in during_new] == [new]

    def test_a_terminal_event_is_matched_by_identity_not_by_symbol(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The old occupant's exit must not attach to the new one, or vice versa."""
        calendar = sessions(date(2020, 1, 1), 1200)
        early, late = calendar[:200], calendar[900:1100]
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            old = seed_security(
                session,
                "BBBY",
                early,
                cusip="075896100",
                valid_from=early[0],
                valid_to=early[-1],
                status="ACQUIRED",
            )
            new = seed_security(
                session, "BBBY", late, cusip="690370101", valid_from=late[0], status="UNKNOWN"
            )
            # An exit recorded under the symbol, inside the *old* occupant's window.
            add_delisting(session, "BBBY", early[-1], cash_per_share=25.0)

        with session_scope(factory) as session:
            old_security = HistoricalSecurityRepository(session).get(old)
            new_security = HistoricalSecurityRepository(session).get(new)
            assert old_security is not None
            assert new_security is not None
            old_terminal = resolve_terminal_outcome(session, old_security)
            new_terminal = resolve_terminal_outcome(session, new_security)

        assert old_terminal.status is TerminalStatus.SETTLED
        assert old_terminal.value_per_share == pytest.approx(25.0)
        # The later occupant shares the symbol and must inherit none of it.
        assert new_terminal.status is TerminalStatus.UNKNOWN
        assert new_terminal.value_per_share is None


class TestTerminalOutcomes:
    """Requirements 7, 8 and 9. What a holder got, or a stated refusal to say."""

    def test_an_acquisition_settles_at_the_stated_consideration(
        self, factory: sessionmaker[Session]
    ) -> None:
        """ATVI's shape: a cash merger at $95."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(
                session, "ATVI", calendar[:40], cik=718877, cusip="00507V109", status="ACQUIRED"
            )
            add_delisting(session, "ATVI", calendar[39], cash_per_share=95.0)

        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            assert security is not None
            outcome = resolve_terminal_outcome(session, security, entry_price=76.0)

        assert outcome.status is TerminalStatus.SETTLED
        assert outcome.reason is DelistingReason.ACQUIRED
        assert outcome.value_per_share == pytest.approx(95.0)
        assert outcome.total_return == pytest.approx(95.0 / 76.0 - 1.0)

    def test_a_receivership_has_no_terminal_value(self, factory: sessionmaker[Session]) -> None:
        """SIVB and FRC. No feed files a receivership, so the value is UNKNOWN."""
        calendar = sessions(date(2023, 1, 2), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(
                session, "SIVB", calendar[:45], cik=719739, status="RECEIVERSHIP"
            )

        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            assert security is not None
            outcome = resolve_terminal_outcome(session, security, entry_price=267.0)

        assert outcome.status is TerminalStatus.UNKNOWN
        assert outcome.reason is DelistingReason.RECEIVERSHIP
        assert outcome.value_per_share is None
        assert outcome.total_return is None

    def test_a_collapsing_price_never_becomes_a_terminal_value(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The rule that keeps -100% from being invented out of a falling tape."""
        calendar = sessions(date(2023, 1, 2), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            # A series that decays to fractions of a cent, and no reported exit.
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="Collapsed Inc",
                primary_ticker="ZERO",
                status="UNKNOWN",
                last_trading_date=calendar[40],
            )
            HistoricalPriceRepository(session).upsert_bars(
                security.id,
                [
                    PriceBar(date=day, open=v, high=v, low=v, close=v, volume=10.0)
                    for day, v in zip(
                        calendar[:41], [10.0 * (0.8**i) for i in range(41)], strict=True
                    )
                ],
            )
            outcome = resolve_terminal_outcome(session, security, entry_price=10.0)

        assert outcome.status is TerminalStatus.UNKNOWN
        assert outcome.value_per_share is None

    def test_a_reported_worthless_removal_is_extinguished_not_unknown(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A filed wind-up *is* the fact. Zero here is reported, not inferred."""
        calendar = sessions(date(2023, 1, 2), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(
                session,
                "WIND",
                calendar[:40],
                cusip="075896100",
                status=str(DelistingReason.BANKRUPT),
            )
            add_delisting(
                session,
                "WIND",
                calendar[39],
                kind=CorporateActionKind.WORTHLESS_REMOVAL,
            )

        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            assert security is not None
            outcome = resolve_terminal_outcome(session, security, entry_price=4.0)

        assert outcome.status is TerminalStatus.EXTINGUISHED
        assert outcome.value_per_share == pytest.approx(0.0)
        assert outcome.total_return == pytest.approx(-1.0)
        assert outcome.source is not None

    def test_a_cash_election_is_preferred_over_a_stock_leg(
        self, factory: sessionmaker[Session]
    ) -> None:
        """VMware's shape: three rows under one symbol, one of them stating $142.50."""
        calendar = sessions(date(2023, 6, 1), 140)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(
                session, "VMW", calendar[:120], cusip="928563402", status="ACQUIRED"
            )
            add_delisting(
                session,
                "VMW",
                calendar[117],
                kind=CorporateActionKind.STOCK_MERGER,
                source_id="vmw-leg-1",
            )
            add_delisting(
                session,
                "VMW",
                calendar[118],
                kind=CorporateActionKind.STOCK_MERGER,
                source_id="vmw-leg-2",
            )
            add_delisting(
                session,
                "VMW",
                calendar[119],
                cash_per_share=142.5,
                source_id="vmw-cash",
            )

        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            assert security is not None
            outcome = resolve_terminal_outcome(session, security)

        assert outcome.status is TerminalStatus.SETTLED
        assert outcome.value_per_share == pytest.approx(142.5)


class TestForwardReturnSemantics:
    """Requirement 10, and the statuses that must never collapse into a number."""

    def test_a_realised_horizon_carries_a_number_and_the_others_do_not(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            seed_security(session, "LIVE", calendar)
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[1, 5, 20]
            )
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)

        assert all(row.status == OutcomeStatus.REALISED.value for row in rows)
        assert all(row.total_return is not None for row in rows)

    def test_an_interrupted_horizon_carries_no_number(self, factory: sessionmaker[Session]) -> None:
        """A security that stopped trading did not go nowhere; it went unmeasured."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            seed_security(session, "STOP", calendar[:35])
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[60]
            )
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)

        assert len(rows) == 1
        assert rows[0].status == OutcomeStatus.INTERRUPTED.value
        assert rows[0].total_return is None
        assert rows[0].exit_price is None
        assert rows[0].last_seen_date is not None

    def test_the_calendar_comes_from_the_benchmark_not_from_the_security(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A security that stops cannot reach its horizon on its own bars."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            # Only 30 sessions of its own, but the market ran for 120.
            seed_security(session, "SHORT", calendar[:30])
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[20]
            )
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)

        assert rows[0].status == OutcomeStatus.INTERRUPTED.value

    def test_a_security_with_no_bars_is_measured_as_nothing_not_as_zero(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            seed_security(session, "NOPX", [], last_trading_date=calendar[20])
            result = measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[20]
            )
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)

        assert result.securities == 1
        assert result.priced == 0
        assert rows == []

    def test_a_rerun_stores_nothing_and_changes_nothing(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            seed_security(session, "IDEM", calendar)
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[1, 5]
            )
        with session_scope(factory) as session:
            before = [
                (row.id, row.status, row.total_return)
                for row in ForwardReturnRepository(session).for_observation_date(DECISION)
            ]
            again = measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[1, 5]
            )
            after = [
                (row.id, row.status, row.total_return)
                for row in ForwardReturnRepository(session).for_observation_date(DECISION)
            ]

        assert again.stored == 0
        assert after == before


class TestExactlyOneIdentity:
    """Requirement 12, at the level the database enforces it."""

    def test_the_measurement_pass_never_writes_both_identities(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            seed_company(session, "AAA", calendar)
            seed_security(session, "GONE", calendar[:40])
            measure_forward_returns(session, DECISION, benchmark=BENCHMARK, horizons=[20])
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[20]
            )
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)

        assert len(rows) == 2
        for row in rows:
            assert (row.company_id is None) != (row.historical_security_id is None)

    def test_the_two_kinds_do_not_block_one_another_at_the_same_id(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Company 1 and historical security 1 are different securities."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            company_id = seed_company(session, "AAA", calendar)
            security_id = seed_security(session, "GONE", calendar)
            measure_forward_returns(session, DECISION, benchmark=BENCHMARK, horizons=[20])
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[20]
            )
            rows = ForwardReturnRepository(session).for_observation_date(DECISION)

        assert company_id == security_id == 1
        assert len(rows) == 2


class TestNoLookAhead:
    """Requirement 13. Nothing after the decision date may shape its state."""

    def test_a_later_identifier_interval_cannot_name_the_decision_date(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A symbol the security had not yet taken up must not label an earlier date."""
        calendar = sessions(date(2020, 1, 1), 900)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="Renamed Inc",
                primary_ticker="NEW",
                status="UNKNOWN",
                last_trading_date=calendar[-1],
            )
            repository = SecurityIdentifierRepository(session)
            repository.add(
                historical_security_id=security.id,
                cusip="075896100",
                ticker="OLD",
                venue=None,
                valid_from=calendar[0],
                valid_to=calendar[400],
                source="test",
                confidence=str(IdentityConfidence.OBSERVED),
                lineage=str(LineageKind.INITIAL),
            )
            repository.add(
                historical_security_id=security.id,
                cusip="075896100",
                ticker="NEW",
                venue=None,
                valid_from=calendar[401],
                valid_to=None,
                source="test",
                confidence=str(IdentityConfidence.ASSERTED),
                lineage=str(LineageKind.TICKER_CHANGE),
            )
            HistoricalPriceRepository(session).upsert_bars(security.id, bars(calendar))
            measure_historical_forward_returns(
                session, calendar[200], benchmark=BENCHMARK, horizons=[20]
            )

        with session_scope(factory) as session:
            early = outcome_rows(session, calendar[200], horizon_sessions=20)

        assert early[0].ticker == "OLD"
        assert early[0].ticker_is_dated

    def test_a_corporate_action_after_the_date_does_not_reach_the_bars_before_it(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A split filed later must not alter what the record says was traded."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "SPLT", calendar)
            before = [
                (row.date, row.close)
                for row in HistoricalPriceRepository(session).list_for_security(
                    security_id, until=DECISION
                )
            ]
            HistoricalCorporateActionRepository(session).add_all(
                security_id,
                [
                    {
                        "action_fingerprint": "rs-1",
                        "ticker": "SPLT",
                        "action_type": CorporateActionKind.REVERSE_SPLIT.value,
                        "ex_date": calendar[100],
                        "split_factor": 0.1,
                        "source": "test",
                    }
                ],
            )
            after = [
                (row.date, row.close)
                for row in HistoricalPriceRepository(session).list_for_security(
                    security_id, until=DECISION
                )
            ]

        assert after == before

    def test_prices_after_the_decision_date_are_invisible_to_a_bounded_read(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "PIT", calendar)
            bounded = HistoricalPriceRepository(session).list_for_security(
                security_id, until=DECISION
            )

        assert bounded
        assert max(row.date for row in bounded) <= DECISION

    def test_a_delisting_after_the_decision_date_does_not_change_the_entry(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The exit may decide the outcome; it must not touch the fill."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            seed_security(session, "EXIT", calendar[:60], cusip="075896100")
            measure_historical_forward_returns(session, DECISION, benchmark=BENCHMARK, horizons=[5])
            before = ForwardReturnRepository(session).for_observation_date(DECISION)
            entry = (before[0].entry_date, before[0].entry_price)

        with session_scope(factory) as session:
            add_delisting(session, "EXIT", calendar[59], cash_per_share=12.0)

        with session_scope(factory) as session:
            rows = outcome_rows(session, DECISION, horizon_sessions=5)

        assert (rows[0].entry_date, rows[0].entry_price) == entry
        assert rows[0].terminal is not None
        assert rows[0].terminal.status is TerminalStatus.SETTLED


class TestCorporateActions:
    """Requirement 6, and the split factors that make a past price readable."""

    def test_a_reverse_split_is_recorded_against_the_security_not_the_symbol(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            first = seed_security(session, "DUP", calendar[:40], cusip="075896100")
            second = seed_security(session, "DUP", calendar[80:], cusip="690370101")
            HistoricalCorporateActionRepository(session).add_all(
                first,
                [
                    {
                        "action_fingerprint": "rs-1",
                        "ticker": "DUP",
                        "action_type": CorporateActionKind.REVERSE_SPLIT.value,
                        "ex_date": calendar[20],
                        "split_factor": 0.05,
                        "source": "test",
                    }
                ],
            )
            first_actions = HistoricalCorporateActionRepository(session).for_security(first)
            second_actions = HistoricalCorporateActionRepository(session).for_security(second)

        assert len(first_actions) == 1
        assert second_actions == []

    def test_a_forward_split_is_stored_with_its_factor(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "FWD", calendar)
            HistoricalCorporateActionRepository(session).add_all(
                security_id,
                [
                    {
                        "action_fingerprint": "fs-1",
                        "ticker": "FWD",
                        "action_type": CorporateActionKind.FORWARD_SPLIT.value,
                        "ex_date": calendar[50],
                        "split_factor": 4.0,
                        "source": "test",
                    }
                ],
            )
            actions = HistoricalCorporateActionRepository(session).for_security(security_id)

        assert [action.split_factor for action in actions] == [4.0]
        assert actions[0].action_type == CorporateActionKind.FORWARD_SPLIT.value

    def test_actions_are_appended_once_however_often_a_pass_reruns(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        row = {
            "action_fingerprint": "fs-1",
            "ticker": "FWD",
            "action_type": CorporateActionKind.FORWARD_SPLIT.value,
            "ex_date": calendar[50],
            "split_factor": 4.0,
            "source": "test",
        }
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "FWD", calendar)
            repository = HistoricalCorporateActionRepository(session)
            repository.add_all(security_id, [row])
            repository.add_all(security_id, [row])
            actions = repository.for_security(security_id)

        assert len(actions) == 1


class TestTheScoreBoundary:
    """Outcome availability and score availability are separate concepts.

    A CIK-unresolved security has no EDGAR fundamentals and can never be scored.
    That must not cost it its prices or its outcomes — dropping it for want of a
    filer number would put back exactly the survivorship bias this layer removes.
    And `CompounderScore` must not be widened to reach it: the score's inputs are
    what they were, and a security without them is `INSUFFICIENT_DATA`, not a
    guess.
    """

    def test_a_cik_unresolved_security_has_outcomes_but_no_score(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "NOCIK", calendar, cik=None)
            measure_historical_forward_returns(
                session, DECISION, benchmark=BENCHMARK, horizons=[20]
            )

            outcomes = ForwardReturnRepository(session).for_observation_date(DECISION)
            reconstruction = reconstruct_historical_score(session, settings, security_id, DECISION)

        # The outcome exists and carries a number.
        assert len(outcomes) == 1
        assert outcomes[0].status == OutcomeStatus.REALISED.value
        assert outcomes[0].total_return is not None

        # The score does not, and says why rather than inventing one.
        assert reconstruction is not None
        assert reconstruction.cik is None
        assert reconstruction.score.status is not ScoringStatus.SCORED
        assert reconstruction.score.final_score is None

    def test_the_security_keeps_its_row_and_its_identity_regardless(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "NOCIK", calendar, cik=None, cusip="075896100")

        with session_scope(factory) as session:
            security = HistoricalSecurityRepository(session).get(security_id)
            intervals = SecurityIdentifierRepository(session).for_security(security_id)

        assert security is not None
        assert security.cik is None
        assert len(intervals) == 1
        assert intervals[0].cusip == "075896100"
        assert intervals[0].confidence == str(IdentityConfidence.OBSERVED)


class TestProvenance:
    """Every identity claim must be able to say where it came from."""

    def test_an_interval_records_its_source_confidence_and_lineage(
        self, factory: sessionmaker[Session]
    ) -> None:
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security_id = seed_security(session, "PROV", calendar, cusip="075896100")
            interval = SecurityIdentifierRepository(session).for_security(security_id)[0]

            assert interval.source == "SEC_FTD"
            assert interval.confidence == str(IdentityConfidence.OBSERVED)
            assert interval.lineage == str(LineageKind.INITIAL)
            assert interval.valid_from is not None

    def test_unresolved_evidence_is_never_upgraded_to_confirmed(
        self, factory: sessionmaker[Session]
    ) -> None:
        """An inferred lineage stays inferred however often it is read back."""
        calendar = sessions(date(2023, 6, 1), 120)
        with session_scope(factory) as session:
            seed_calendar(session, calendar)
            security = HistoricalSecurityRepository(session).create(
                cik=None, name="Inferred Inc", primary_ticker="INF", status="UNKNOWN"
            )
            SecurityIdentifierRepository(session).add(
                historical_security_id=security.id,
                cusip="075896209",
                ticker="INF",
                venue=None,
                valid_from=calendar[0],
                valid_to=None,
                source="SEC_FTD",
                confidence=str(IdentityConfidence.INFERRED),
                lineage=str(LineageKind.CUSIP_CHANGE),
            )
            security_id = security.id

        with session_scope(factory) as session:
            interval = SecurityIdentifierRepository(session).for_security(security_id)[0]

        assert interval.confidence == str(IdentityConfidence.INFERRED)
        assert interval.lineage == str(LineageKind.CUSIP_CHANGE)
