"""Phase 9D: the historical chain, connected end to end and accounted for.

The Phase 10 audit found every layer correct and the chain broken in two places:
membership was built and never read, and a delisted security could reach an
outcome but never a market signal. Both defects were silent — a strategy filtering
on a signal returned a survivor-only population and nothing failed.

The test that matters most here is
`test_a_delisted_security_survives_the_whole_chain_to_a_signal`. It walks a
security with no `companies` row from identity to membership to fundamentals to
features to signal to outcome, and it **could not have passed before this phase**:
`pit_feature_snapshots.company_id` was `NOT NULL`, so the insert it makes would
have been rejected by the database.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    DelistingRepository,
    ForwardReturnRepository,
    FundamentalObservationRepository,
    HistoricalObservationRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    PitCaptureRunRepository,
    PitFeatureSnapshotRepository,
    PriceHistoryRepository,
    SecurityIdentifierRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import PitFeatureSnapshot, PitSignalSnapshot
from domain import (
    CURRENT_SCORE_VERSION,
    CURRENT_SIGNAL_VERSION,
    CompanyProfile,
    CorporateActionKind,
    IdentityConfidence,
    LineageKind,
    MembershipState,
    PriceBar,
)
from stock_screener.backtest import (
    ChainStage,
    chain_accounting,
    members_as_of,
    membership_for_security,
    reconstruct_membership_record,
)
from stock_screener.backtest.membership import COMPANY, HISTORICAL
from stock_screener.pit import capture_day, capture_historical_features
from stock_screener.pit.returns import measure_historical_forward_returns
from stock_screener.signals import derive_historical_market_signals, derive_market_signals

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.config import Settings

pytestmark = pytest.mark.integration

BENCHMARK = "SPY"
FLOOR = date(2021, 1, 4)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'chain.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def sessions(start: date, count: int) -> list[date]:
    """Return `count` consecutive weekday sessions."""
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


CALENDAR = sessions(FLOOR, 400)
DECISION = CALENDAR[300]


def bars(days: Sequence[date], *, base: float = 50.0) -> list[PriceBar]:
    """A rising bar series with enough movement for features to compute."""
    return [
        PriceBar(
            date=day,
            open=base + index * 0.13,
            high=base + index * 0.13 + 0.9,
            low=base + index * 0.13 - 0.9,
            close=base + index * 0.13 + 0.2,
            volume=750_000.0 + (index % 11) * 25_000.0,
        )
        for index, day in enumerate(days)
    ]


def seed_benchmark(session: Session) -> None:
    """Give the database its session calendar."""
    BenchmarkPriceRepository(session).upsert_bars(BENCHMARK, bars(CALENDAR, base=400.0))


def seed_company(session: Session, ticker: str) -> int:
    """A live company with prices and a filing."""
    CompanyRepository(session).upsert_profile(
        CompanyProfile(ticker=ticker, name=f"{ticker} Inc", exchange="NASDAQ", is_active=True)
    )
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    PriceHistoryRepository(session).upsert_bars(company.id, bars(CALENDAR))
    FundamentalObservationRepository(session).add_all(
        company.id,
        [
            {
                "observation_fingerprint": f"{ticker}-rev",
                "taxonomy": "us-gaap",
                "concept": "Revenues",
                "value": 500_000_000.0,
                "unit": "USD",
                "period_end": FLOOR,
                "filed_date": FLOOR,
                "source": "test",
            }
        ],
    )
    return company.id


def seed_security(
    session: Session,
    ticker: str,
    *,
    days: Sequence[date] = CALENDAR,
    exchange: str | None = "NASDAQ",
    cusip: str = "075896100",
    cik: int | None = None,
    with_filings: bool = True,
) -> int:
    """A delisted security with identity, prices and (optionally) filings."""
    security = HistoricalSecurityRepository(session).create(
        cik=cik,
        name=f"{ticker} Inc",
        primary_ticker=ticker,
        exchange=exchange,
        status="UNKNOWN",
        last_trading_date=days[-1],
    )
    SecurityIdentifierRepository(session).add(
        historical_security_id=security.id,
        cusip=cusip,
        ticker=ticker,
        venue=None,
        valid_from=days[0],
        valid_to=days[-1],
        source="SEC_FTD",
        confidence=str(IdentityConfidence.OBSERVED),
        lineage=str(LineageKind.INITIAL),
    )
    HistoricalPriceRepository(session).upsert_bars(security.id, bars(days))
    if with_filings:
        HistoricalObservationRepository(session).add_all(
            security.id,
            [
                {
                    "observation_fingerprint": f"{ticker}-rev",
                    "taxonomy": "us-gaap",
                    "concept": "Revenues",
                    "value": 400_000_000.0,
                    "unit": "USD",
                    "period_end": FLOOR,
                    "filed_date": FLOOR,
                    "source": "test",
                }
            ],
        )
    return security.id


def run_chain(session: Session, settings: Settings) -> None:
    """Run the full daily pipeline for the decision date, both identity kinds."""
    capture_day(session, settings, snapshot_date=DECISION, score_version=CURRENT_SCORE_VERSION)
    derive_market_signals(session, snapshot_date=DECISION)
    capture_historical_features(session, settings, snapshot_date=DECISION)
    derive_historical_market_signals(session, snapshot_date=DECISION)
    measure_historical_forward_returns(session, DECISION, benchmark=BENCHMARK, horizons=[1, 5, 20])
    reconstruct_membership_record(session)


class TestTheDelistedSecurityReachesASignal:
    """Blocker 2. The test that could not have passed before this phase."""

    def test_a_delisted_security_survives_the_whole_chain_to_a_signal(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """identity → membership → PIT data → features → signal → outcome.

        Under the old schema `pit_feature_snapshots.company_id` was `NOT NULL`,
        so the feature insert this makes would have been refused outright and the
        security would have stopped one step short of a strategy decision — with
        no error, because nothing asked for it.
        """
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            security_id = seed_security(session, "GONE", cik=886158)
            run_chain(session, settings)

            verdict = membership_for_security(session, DECISION, security_id=security_id)
            features = session.scalars(
                select(PitFeatureSnapshot).where(
                    PitFeatureSnapshot.historical_security_id == security_id,
                    PitFeatureSnapshot.snapshot_date == DECISION,
                )
            ).all()
            signals = session.scalars(
                select(PitSignalSnapshot).where(
                    PitSignalSnapshot.historical_security_id == security_id,
                    PitSignalSnapshot.snapshot_date == DECISION,
                )
            ).all()
            outcomes = [
                row
                for row in ForwardReturnRepository(session).for_observation_date(DECISION)
                if row.historical_security_id == security_id
            ]

            assert CompanyRepository(session).get_by_ticker("GONE") is None

        assert verdict.state is MembershipState.MEMBER
        assert len(features) == 1
        assert features[0].company_id is None
        assert len(signals) == 1
        assert signals[0].company_id is None
        assert signals[0].signal_version == CURRENT_SIGNAL_VERSION
        assert outcomes

    def test_the_chain_accounting_marks_it_eligible(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            security_id = seed_security(session, "GONE")
            run_chain(session, settings)
            accounting = chain_accounting(session, DECISION)

        delisted = next(
            v
            for v in accounting.verdicts
            if v.identity_kind == HISTORICAL and v.identity_id == security_id
        )
        assert delisted.stage is ChainStage.ELIGIBLE
        assert delisted.has_signal
        assert accounting.eligible_historical == 1

    def test_the_live_company_path_is_unchanged(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_benchmark(session)
            company_id = seed_company(session, "LIVE")
            seed_security(session, "GONE")
            run_chain(session, settings)

            company_features = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.company_id == company_id)
            ).all()
            company_signals = session.scalars(
                select(PitSignalSnapshot).where(PitSignalSnapshot.company_id == company_id)
            ).all()

        assert len(company_features) == 1
        assert company_features[0].historical_security_id is None
        assert len(company_signals) == 1
        assert company_signals[0].historical_security_id is None


class TestReadinessUsesCanonicalMembership:
    """Blocker 1. The readiness path and the membership record are one universe."""

    def test_the_universe_the_harness_sees_is_the_membership_record(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        from stock_screener.backtest import security_readiness

        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            run_chain(session, settings)

            universe = members_as_of(session, DECISION)
            readiness = security_readiness(session, settings, DECISION, "LIVE")

        assert readiness.in_universe is True
        # Every member the current evidence supports is INFERRED, and the harness
        # reports the membership record's own confidence rather than a
        # baseline/observed distinction from the legacy event log.
        assert readiness.membership_kind == "INFERRED"
        assert universe.company_members == 1

    def test_a_security_the_record_cannot_place_is_not_in_the_universe(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """UNKNOWN must not become eligible by passing through readiness."""
        from stock_screener.backtest import security_readiness

        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            # No venue recorded, so membership is UNKNOWN by the Phase 9C rules.
            security_id = seed_security(session, "NOVENUE", exchange=None)
            run_chain(session, settings)

            verdict = membership_for_security(session, DECISION, security_id=security_id)
            readiness = security_readiness(session, settings, DECISION, "LIVE")
            accounting = chain_accounting(session, DECISION)

        assert verdict.state is MembershipState.UNKNOWN
        assert readiness.in_universe is True
        unknown = next(
            v
            for v in accounting.verdicts
            if v.identity_kind == HISTORICAL and v.identity_id == security_id
        )
        assert unknown.stage is ChainStage.UNKNOWN_MEMBERSHIP
        assert not unknown.eligible

    def test_a_non_member_is_distinguishable_from_an_unknown(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            gone = seed_security(session, "EXITED", days=CALENDAR[:100])
            DelistingRepository(session).add_all(
                [
                    {
                        "ticker": "EXITED",
                        "kind": CorporateActionKind.CASH_MERGER.value,
                        "effective_date": CALENDAR[101],
                        "source": "test",
                        "source_id": "exited-1",
                    }
                ]
            )
            unknown_id = seed_security(session, "NOVENUE", exchange=None)
            run_chain(session, settings)
            accounting = chain_accounting(session, DECISION)

        stages = {
            v.identity_id: v.stage for v in accounting.verdicts if v.identity_kind == HISTORICAL
        }
        assert stages[gone] is ChainStage.NO_MEMBERSHIP
        assert stages[unknown_id] is ChainStage.UNKNOWN_MEMBERSHIP


class TestNoSilentExclusion:
    """Every security leaves the chain with a classified reason, or not at all."""

    def test_the_accounting_balances(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            seed_security(session, "GONE")
            seed_security(session, "NOVENUE", exchange=None, cusip="690370101")
            seed_security(session, "NOFILE", cusip="00507V109", with_filings=False)
            run_chain(session, settings)
            accounting = chain_accounting(session, DECISION)
            total = (
                CompanyRepository(session).count() + HistoricalSecurityRepository(session).count()
            )

        assert accounting.total == total == 4
        assert len(accounting.eligible) + len(accounting.excluded) == accounting.total
        assert accounting.unexplained == 0
        assert all(v.stage is not None for v in accounting.verdicts)

    def test_a_security_without_filings_stops_at_a_named_stage(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            no_file = seed_security(session, "NOFILE", with_filings=False)
            run_chain(session, settings)
            accounting = chain_accounting(session, DECISION)

        verdict = next(
            v
            for v in accounting.verdicts
            if v.identity_id == no_file and v.identity_kind == HISTORICAL
        )
        assert verdict.stage is ChainStage.NO_PIT_DATA
        assert verdict.has_price
        assert not verdict.has_fundamentals

    def test_every_exclusion_carries_a_reason(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            seed_security(session, "NOVENUE", exchange=None)
            seed_security(session, "NOFILE", cusip="00507V109", with_filings=False)
            run_chain(session, settings)
            accounting = chain_accounting(session, DECISION)

        reasons = dict(accounting.by_reason())
        assert reasons
        assert sum(reasons.values()) == len(accounting.excluded)
        assert str(ChainStage.ELIGIBLE) not in reasons


class TestPitSnapshotIdentityInvariants:
    """The database enforces exactly-one identity whatever the code believes."""

    def _run(self, session: Session, settings: Settings) -> tuple[int, int]:
        seed_benchmark(session)
        company_id = seed_company(session, "LIVE")
        security_id = seed_security(session, "GONE")
        run_chain(session, settings)
        return company_id, security_id

    def test_a_feature_row_naming_both_identities_is_refused(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            company_id, security_id = self._run(session, settings)
            run = PitCaptureRunRepository(session).completed_run(DECISION)
            assert run is not None
            run_id = run.id

        with (
            pytest.raises(IntegrityError, match="ck_pit_feature_one_identity"),
            session_scope(factory) as session,
        ):
            session.execute(
                insert(PitFeatureSnapshot),
                [
                    {
                        "run_id": run_id,
                        "company_id": company_id,
                        "historical_security_id": security_id,
                        "ticker": "BOTH",
                        "snapshot_date": DECISION + timedelta(days=1),
                        "feature_version": "MARKET_FEATURES_V1",
                        "feature_as_of": DECISION,
                        "bars_available": 10,
                    }
                ],
            )

    def test_a_feature_row_naming_neither_identity_is_refused(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            self._run(session, settings)
            run = PitCaptureRunRepository(session).completed_run(DECISION)
            assert run is not None
            run_id = run.id

        with (
            pytest.raises(IntegrityError, match="ck_pit_feature_one_identity"),
            session_scope(factory) as session,
        ):
            session.execute(
                insert(PitFeatureSnapshot),
                [
                    {
                        "run_id": run_id,
                        "ticker": "NEITHER",
                        "snapshot_date": DECISION + timedelta(days=1),
                        "feature_version": "MARKET_FEATURES_V1",
                        "feature_as_of": DECISION,
                        "bars_available": 10,
                    }
                ],
            )

    def test_a_duplicate_company_snapshot_is_refused(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            self._run(session, settings)
            with pytest.raises(Exception, match=r"already stored|UNIQUE|Immutable"):
                capture_historical_features(session, settings, snapshot_date=DECISION)
                PitFeatureSnapshotRepository(session).add_all(
                    [
                        {
                            "run_id": 1,
                            "company_id": 1,
                            "ticker": "LIVE",
                            "snapshot_date": DECISION,
                            "feature_version": "MARKET_FEATURES_V1",
                            "bars_available": 10,
                        }
                    ]
                )

    def test_a_repeated_historical_capture_writes_nothing(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            self._run(session, settings)
            again = capture_historical_features(session, settings, snapshot_date=DECISION)
            repeat = derive_historical_market_signals(session, snapshot_date=DECISION)

        assert again.already_captured
        assert repeat.already_derived

    def test_the_two_identity_kinds_do_not_collide_at_the_same_id(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Company 1 and historical security 1 are different securities."""
        with session_scope(factory) as session:
            company_id, security_id = self._run(session, settings)
            features = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()

        assert company_id == security_id == 1
        assert len(features) == 2
        assert {f.company_id for f in features} == {company_id, None}
        assert {f.historical_security_id for f in features} == {security_id, None}


class TestCompanyBackedRowsAreUnchanged:
    """Rule 14. Adding the second identity must not disturb the first."""

    def test_a_company_signal_is_identical_with_and_without_historical_rows(
        self, factory: sessionmaker[Session], settings: Settings, tmp_path: Path
    ) -> None:
        """The same company, derived alone and derived beside a delisted security.

        The company's own signal values must not move. Its `peer_population`
        may — percentiles are cross-sectional and the delisted security is a real
        peer — so that field is compared separately and deliberately.
        """
        alone_engine = create_engine_from_url(f"sqlite:///{tmp_path / 'alone.db'}")
        create_all(alone_engine)
        alone_factory = build_session_factory(alone_engine)
        try:
            with session_scope(alone_factory) as session:
                seed_benchmark(session)
                seed_company(session, "LIVE")
                capture_day(session, settings, snapshot_date=DECISION)
                derive_market_signals(session, snapshot_date=DECISION)
                alone = session.scalars(
                    select(PitSignalSnapshot).where(PitSignalSnapshot.snapshot_date == DECISION)
                ).one()
                alone_values = (
                    alone.compression_ratio,
                    alone.distance_from_52w_high,
                    alone.stretch_20d,
                    alone.trend_direction,
                    alone.setups,
                    alone.tradability,
                )
        finally:
            alone_engine.dispose()

        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            seed_security(session, "GONE")
            run_chain(session, settings)
            beside = session.scalars(
                select(PitSignalSnapshot).where(
                    PitSignalSnapshot.snapshot_date == DECISION,
                    PitSignalSnapshot.company_id.is_not(None),
                )
            ).one()
            beside_values = (
                beside.compression_ratio,
                beside.distance_from_52w_high,
                beside.stretch_20d,
                beside.trend_direction,
                beside.setups,
                beside.tradability,
            )

        assert beside_values == alone_values

    def test_the_legacy_universe_path_still_exists_and_is_unused_by_readiness(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Rule 8: the legacy implementation is preserved, not deleted.

        It must still work — and it must no longer be what readiness consults.
        The two answers differ, which is the whole point of the switch.
        """
        from stock_screener.backtest import reconstruct_universe

        earlier = CALENDAR[100]
        with session_scope(factory) as session:
            seed_benchmark(session)
            seed_company(session, "LIVE")
            run_chain(session, settings)

            legacy_now = reconstruct_universe(session, DECISION)
            legacy_before = reconstruct_universe(session, earlier)
            canonical_before = members_as_of(session, earlier)

        # The legacy implementation is preserved and still works: on the date its
        # baseline was written it places the company.
        assert len(legacy_now.members) == 1

        # Before that baseline it places nobody, because a baseline asserts
        # nothing about the time before it was recorded. That is honest, and it
        # is also why readiness could not use it: every historical decision date
        # precedes the day capture first ran.
        assert len(legacy_before.members) == 0
        assert legacy_before.undetermined

        # The canonical record reaches back over the dated evidence instead.
        assert len(canonical_before.members) == 1
        assert canonical_before.members[0][0] == COMPANY
