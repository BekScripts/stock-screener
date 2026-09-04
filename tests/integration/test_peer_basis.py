"""Phase 9E: what the market-signal peer basis does, and does not, determine.

Phase 9D left one open question: existing company signal rows were derived
against a live-only cross-section while newly backfilled historical rows were
derived against the full one, and percentiles are cross-sectional. These tests
pin down exactly how far that reaches.

The finding they encode is narrower than it first appears and worse in one
respect than it first appears:

- **Narrower**: `derive_signals` passes `peers` to one thing only — the
  percentile dictionary. Setups, tradability, trend direction and every raw
  ratio are computed from the security's own features. The peer basis cannot
  move them, and `test_only_percentiles_depend_on_the_peer_basis` proves it
  rather than asserting it.
- **Worse**: `build_peer_context` consults no membership at all. Its population
  is whatever feature rows it is handed, so a security that left the market
  years earlier is a peer as long as it once had bars. That is not the market
  that traded on the date, and
  `test_the_peer_context_does_not_consult_membership` records it deliberately —
  the test will fail the day a peer policy is introduced, which is the point.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    HistoricalPriceRepository,
    HistoricalSecurityRepository,
    PriceHistoryRepository,
    SecurityIdentifierRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import PitFeatureSnapshot, PitSignalSnapshot
from domain import (
    DEFAULT_THRESHOLDS,
    MIN_PEER_SAMPLE,
    PERCENTILE_FEATURES,
    CompanyProfile,
    IdentityConfidence,
    LineageKind,
    MarketFeatures,
    MembershipState,
    PeerContext,
    PriceBar,
    build_peer_context,
    derive_signals,
)
from stock_screener.backtest import members_as_of, reconstruct_membership_record
from stock_screener.backtest.membership import HISTORICAL
from stock_screener.pit import capture_day, capture_historical_features
from stock_screener.signals import derive_historical_market_signals, derive_market_signals
from stock_screener.signals.derivation import _to_features

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.config import Settings

pytestmark = pytest.mark.integration

BENCHMARK = "SPY"
FLOOR = date(2021, 1, 4)


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

#: Enough securities that percentiles are reported at all — below
#: `MIN_PEER_SAMPLE` the context returns None for every feature.
LIVE_COUNT = MIN_PEER_SAMPLE + 10
HISTORICAL_COUNT = MIN_PEER_SAMPLE + 5


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'peers.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def bars(days: Sequence[date], *, base: float, slope: float) -> list[PriceBar]:
    """A bar series whose level and slope vary per security, so peers differ."""
    return [
        PriceBar(
            date=day,
            open=base + index * slope,
            high=base + index * slope + 0.8,
            low=base + index * slope - 0.8,
            close=base + index * slope + 0.15,
            volume=400_000.0 + (index % 7) * 30_000.0 + base * 900.0,
        )
        for index, day in enumerate(days)
    ]


def seed_market(session: Session, *, historical_days: Sequence[date] | None = None) -> None:
    """A benchmark, a live cross-section and a historical cross-section."""
    BenchmarkPriceRepository(session).upsert_bars(BENCHMARK, bars(CALENDAR, base=400.0, slope=0.2))
    for index in range(LIVE_COUNT):
        ticker = f"L{index:03d}"
        CompanyRepository(session).upsert_profile(
            CompanyProfile(ticker=ticker, name=f"{ticker} Inc", exchange="NASDAQ", is_active=True)
        )
        company = CompanyRepository(session).get_by_ticker(ticker)
        assert company is not None
        PriceHistoryRepository(session).upsert_bars(
            company.id, bars(CALENDAR, base=20.0 + index * 1.7, slope=0.05 + index * 0.004)
        )
    days = list(historical_days if historical_days is not None else CALENDAR)
    for index in range(HISTORICAL_COUNT):
        ticker = f"H{index:03d}"
        security = HistoricalSecurityRepository(session).create(
            cik=None,
            name=f"{ticker} Inc",
            primary_ticker=ticker,
            exchange="NASDAQ",
            status="UNKNOWN",
            last_trading_date=days[-1],
        )
        SecurityIdentifierRepository(session).add(
            historical_security_id=security.id,
            cusip=f"{index:06d}100",
            ticker=ticker,
            venue=None,
            valid_from=days[0],
            valid_to=days[-1],
            source="SEC_FTD",
            confidence=str(IdentityConfidence.OBSERVED),
            lineage=str(LineageKind.INITIAL),
        )
        HistoricalPriceRepository(session).upsert_bars(
            security.id, bars(days, base=8.0 + index * 2.3, slope=0.02 + index * 0.006)
        )


def run_pipeline(session: Session, settings: Settings) -> None:
    """Capture and derive for both identity kinds on the decision date."""
    capture_day(session, settings, snapshot_date=DECISION)
    derive_market_signals(session, snapshot_date=DECISION)
    capture_historical_features(session, settings, snapshot_date=DECISION)
    derive_historical_market_signals(session, snapshot_date=DECISION)
    reconstruct_membership_record(session)


def contexts(
    session: Session,
) -> tuple[PeerContext, PeerContext, list[MarketFeatures], list[MarketFeatures]]:
    """Return the two peer contexts and the feature sets behind them."""
    rows = session.scalars(
        select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
    ).all()
    live = [_to_features(r) for r in rows if r.company_id is not None]
    everything = [_to_features(r) for r in rows]
    return build_peer_context(live), build_peer_context(everything), live, everything


class TestWhatThePeerBasisCanAndCannotMove:
    """The scope of the divergence, established structurally and empirically."""

    def test_only_percentiles_depend_on_the_peer_basis(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Every non-percentile field is identical under both bases.

        `derive_signals` hands `peers` to the percentile dictionary and to
        nothing else — `classify_setups` and `assess_tradability` take only
        `(features, thresholds)`. This is the empirical half of that claim.
        """
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            live_peers, full_peers, live_feats, _ = contexts(session)

        moved = []
        for feats in live_feats:
            a = derive_signals(feats, live_peers, thresholds=DEFAULT_THRESHOLDS)
            b = derive_signals(feats, full_peers, thresholds=DEFAULT_THRESHOLDS)
            for field in (
                "setups",
                "setup_evidence",
                "tradability",
                "tradability_reasons",
                "liquidity_floor_proven",
                "trend_direction",
                "trend_r2",
                "compression_ratio",
                "volatility_expansion_ratio",
                "distance_from_52w_high",
                "distance_from_52w_low",
                "stretch_20d",
                "bars_available",
                "feature_as_of",
                "signal_version",
            ):
                if getattr(a, field) != getattr(b, field):
                    moved.append(field)

        assert live_feats
        assert moved == []

    def test_different_peer_populations_produce_a_measurable_difference(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """The divergence is real, not hypothetical — and it is measurable."""
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            live_peers, full_peers, live_feats, _all = contexts(session)

        assert live_peers.population == LIVE_COUNT
        assert full_peers.population == LIVE_COUNT + HISTORICAL_COUNT
        assert full_peers.population > live_peers.population

        deltas = [
            abs(a - b)
            for feats in live_feats
            for name in PERCENTILE_FEATURES
            if (a := live_peers.percentile(name, getattr(feats, name, None))) is not None
            and (b := full_peers.percentile(name, getattr(feats, name, None))) is not None
        ]
        assert deltas
        assert max(deltas) > 0.0


class TestPeerBasisProvenance:
    """A researcher must be able to tell which basis produced a row."""

    def test_the_stored_peer_population_identifies_the_basis(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitSignalSnapshot).where(PitSignalSnapshot.snapshot_date == DECISION)
            ).all()
            feature_rows = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()

        company_features = sum(1 for r in feature_rows if r.company_id is not None)
        company_pops = {r.peer_population for r in rows if r.company_id is not None}
        historical_pops = {r.peer_population for r in rows if r.historical_security_id is not None}

        # LIVE_ONLY rows carry exactly the company feature count; FULL rows carry
        # the total. The two are distinguishable without any extra column.
        assert company_pops == {company_features}
        assert historical_pops == {len(feature_rows)}
        assert company_pops != historical_pops


class TestDeterminism:
    """Same inputs, same signal — whatever order the rows arrive in."""

    def test_the_peer_context_is_independent_of_row_order(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            _, _, _, everything = contexts(session)

        shuffled = list(everything)
        random.Random(7).shuffle(shuffled)  # noqa: S311
        assert (
            build_peer_context(everything).sorted_values
            == build_peer_context(shuffled).sorted_values
        )
        assert (
            build_peer_context(everything).sorted_values
            == build_peer_context(list(reversed(everything))).sorted_values
        )

    def test_the_same_inputs_produce_the_same_signal(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            _, full_peers, live_feats, everything = contexts(session)

        shuffled = list(everything)
        random.Random(3).shuffle(shuffled)  # noqa: S311
        reshuffled_peers = build_peer_context(shuffled)

        first = [derive_signals(f, full_peers, thresholds=DEFAULT_THRESHOLDS) for f in live_feats]
        again = [derive_signals(f, full_peers, thresholds=DEFAULT_THRESHOLDS) for f in live_feats]
        reordered = [
            derive_signals(f, reshuffled_peers, thresholds=DEFAULT_THRESHOLDS) for f in live_feats
        ]
        assert first == again
        assert first == reordered


class TestWhoIsInThePeerUniverse:
    """Inclusion, exclusion, and the membership question the basis never asks."""

    def test_a_historical_security_is_in_the_peer_universe(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """It must not disappear merely for lacking a `companies` row."""
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()

        historical = [r for r in rows if r.historical_security_id is not None]
        assert len(historical) == HISTORICAL_COUNT

    def test_a_live_company_is_in_the_peer_universe(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()

        assert sum(1 for r in rows if r.company_id is not None) == LIVE_COUNT

    def test_the_peer_context_does_not_consult_membership(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """The Phase 9E finding, recorded rather than described.

        `build_peer_context` ranks whatever feature rows it is given. A security
        whose membership on the date is `UNKNOWN` — or `NON_MEMBER` — is a peer
        all the same, because nothing in the path asks. This test asserts the
        behaviour as it *is*, so that introducing a peer policy fails it loudly
        instead of changing percentiles unannounced.
        """
        with session_scope(factory) as session:
            seed_market(session)
            # No venue recorded, so Phase 9C membership is UNKNOWN for this one.
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="No Venue Inc",
                primary_ticker="NOVEN",
                exchange=None,
                status="UNKNOWN",
                last_trading_date=CALENDAR[-1],
            )
            unknown_id = security.id
            HistoricalPriceRepository(session).upsert_bars(
                unknown_id, bars(CALENDAR, base=15.0, slope=0.03)
            )
            run_pipeline(session, settings)

            universe = members_as_of(session, DECISION)
            members = {(kind, identity) for kind, identity, _ in universe.members}
            in_features = session.scalar(
                select(PitFeatureSnapshot.id).where(
                    PitFeatureSnapshot.snapshot_date == DECISION,
                    PitFeatureSnapshot.historical_security_id == unknown_id,
                )
            )
            unknown_ids = {identity for kind, identity in universe.unknown if kind == HISTORICAL}

        # Membership says UNKNOWN...
        assert unknown_id in unknown_ids
        assert (HISTORICAL, unknown_id) not in members
        # ...and the security is a peer regardless. This is the defect.
        assert in_features is not None

    def test_unknown_membership_is_not_promoted_to_member(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Being a peer must not make a security a member of the universe."""
        with session_scope(factory) as session:
            seed_market(session)
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="No Venue Inc",
                primary_ticker="NOVEN",
                exchange=None,
                status="UNKNOWN",
                last_trading_date=CALENDAR[-1],
            )
            unknown_id = security.id
            HistoricalPriceRepository(session).upsert_bars(
                unknown_id, bars(CALENDAR, base=15.0, slope=0.03)
            )
            run_pipeline(session, settings)

            from stock_screener.backtest import membership_for_security

            verdict = membership_for_security(session, DECISION, security_id=unknown_id)
            universe = members_as_of(session, DECISION)
            member_ids = {identity for kind, identity, _ in universe.members if kind == HISTORICAL}

        assert verdict.state is MembershipState.UNKNOWN
        # Being ranked as a peer confers nothing: the universe still excludes it.
        assert unknown_id not in member_ids

    def test_a_security_that_stopped_trading_is_still_ranked_as_a_peer(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Stale-peer contamination, measured on a fixture.

        A security whose last bar is long before the decision date still receives
        a feature row — computed from those old bars — and so still enters the
        cross-section. On staging this reaches 2,264 of 2,562 historical peers on
        2026-03-31, 1,753 of them more than a year stale.
        """
        early = CALENDAR[:80]
        with session_scope(factory) as session:
            seed_market(session, historical_days=early)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitFeatureSnapshot).where(
                    PitFeatureSnapshot.snapshot_date == DECISION,
                    PitFeatureSnapshot.historical_security_id.is_not(None),
                )
            ).all()
            stale = [r for r in rows if r.feature_as_of < DECISION - timedelta(days=60)]

        assert rows
        # Every one of them stopped trading long before the decision date, and
        # every one of them is nonetheless a peer.
        assert len(stale) == len(rows)


class TestNoFutureLeakageIntoThePeerSet:
    """The peer set may be stale; it must never be early."""

    def test_no_peer_feature_uses_a_bar_after_the_decision_date(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_market(session)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()

        assert rows
        assert all(row.feature_as_of <= DECISION for row in rows)

    def test_a_security_first_priced_after_the_date_is_not_a_peer(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Future membership cannot reach backwards into the cross-section."""
        later = CALENDAR[320:]
        with session_scope(factory) as session:
            seed_market(session)
            security = HistoricalSecurityRepository(session).create(
                cik=None,
                name="Later Inc",
                primary_ticker="LATER",
                exchange="NASDAQ",
                status="UNKNOWN",
                last_trading_date=later[-1],
            )
            later_id = security.id
            HistoricalPriceRepository(session).upsert_bars(
                later_id, bars(later, base=30.0, slope=0.04)
            )
            run_pipeline(session, settings)

            present = session.scalar(
                select(PitFeatureSnapshot.id).where(
                    PitFeatureSnapshot.snapshot_date == DECISION,
                    PitFeatureSnapshot.historical_security_id == later_id,
                )
            )

        assert present is None
