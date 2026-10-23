"""Phase 9F: the `MARKET_SIGNALS_V2` peer policy, attacked case by case.

V2 changes one thing and one thing only — which securities constitute the
cross-section a percentile is measured against:

    V2 peers(D) = members_as_of(D) ∩ securities with market data on D

Everything else is the frozen V1 arithmetic, and
`test_v1_and_v2_agree_on_every_peer_independent_field` proves it on real derived
rows rather than asserting it from the source.

The seven inclusion cases are the point of the file. Each one is a way the old
policy went wrong, written as a security the new policy must place correctly:
a name that stopped trading before the date, one that had not started, a delisted
member that traded that day, one that did not, an unknown, a non-member, and a
live company. `TestNoFutureLeakage` then shows that none of it depends on
anything after the date.
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
    DelistingRepository,
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
    MARKET_SIGNALS_V1,
    MARKET_SIGNALS_V2,
    MIN_PEER_SAMPLE,
    PERCENTILE_FEATURES,
    CompanyProfile,
    CorporateActionKind,
    IdentityConfidence,
    LineageKind,
    PeerExclusion,
    PriceBar,
    build_peer_context,
    derive_signals,
    select_peers,
)
from stock_screener.backtest import reconstruct_membership_record
from stock_screener.backtest.membership import COMPANY, HISTORICAL
from stock_screener.pit import capture_day, capture_historical_features
from stock_screener.signals import (
    derive_historical_market_signals,
    derive_market_signals,
    derive_market_signals_v2,
    explain_peer_membership,
)
from stock_screener.signals.derivation import _peer_candidates, _to_features

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

#: Enough live names that percentiles are reported at all.
LIVE_COUNT = MIN_PEER_SAMPLE + 10


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Yield a session factory against a fresh file-backed database."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'v2.db'}")
    create_all(engine)
    try:
        yield build_session_factory(engine)
    finally:
        engine.dispose()


def bars(days: Sequence[date], *, base: float, slope: float = 0.05) -> list[PriceBar]:
    """A bar series whose level and slope vary per security."""
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


def seed_live_market(session: Session) -> None:
    """A benchmark and a live cross-section large enough to rank."""
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


def add_security(
    session: Session,
    ticker: str,
    days: Sequence[date],
    *,
    exchange: str | None = "NASDAQ",
    cusip: str = "075896100",
    delisted_on: date | None = None,
    bar_days: Sequence[date] | None = None,
) -> int:
    """A historical security trading over `days`, optionally with a dated exit.

    `bar_days` lets the *price* record stop earlier than the *identity* record,
    which is how a security that is still a member on the date but did not trade
    on it is constructed — the case that isolates the stale-peer rule from the
    membership rule.
    """
    security = HistoricalSecurityRepository(session).create(
        cik=None,
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
    HistoricalPriceRepository(session).upsert_bars(
        security.id, bars(list(bar_days if bar_days is not None else days), base=12.0)
    )
    if delisted_on is not None:
        DelistingRepository(session).add_all(
            [
                {
                    "ticker": ticker,
                    "kind": CorporateActionKind.CASH_MERGER.value,
                    "effective_date": delisted_on,
                    "source": "test",
                    "source_id": f"{ticker}-exit",
                }
            ]
        )
    return security.id


def run_pipeline(session: Session, settings: Settings) -> None:
    """Capture and derive both identity kinds, then V1 and V2 signals."""
    capture_day(session, settings, snapshot_date=DECISION)
    derive_market_signals(session, snapshot_date=DECISION)
    capture_historical_features(session, settings, snapshot_date=DECISION)
    derive_historical_market_signals(session, snapshot_date=DECISION)
    reconstruct_membership_record(session)
    derive_market_signals_v2(session, snapshot_date=DECISION)


def verdict_for(session: Session, security_id: int) -> PeerExclusion | None:
    """The V2 peer verdict for one historical security on the decision date."""
    return explain_peer_membership(session, DECISION, historical_security_id=security_id)


class TestTheSevenInclusionCases:
    """Step 11: every way the old peer policy went wrong, as a placed security."""

    def test_case_1_a_security_that_stopped_trading_before_the_date_is_excluded(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """The stale-peer case — the defect Phase 9E found.

        Constructed to isolate it: the security is still a **member** on the
        decision date (its identity record runs to the end of the calendar and no
        exit is recorded), and its price record stops 200 sessions earlier. Under
        V1 it was ranked anyway, on bars two hundred sessions stale.
        """
        with session_scope(factory) as session:
            seed_live_market(session)
            stale = add_security(session, "STALE", CALENDAR, bar_days=CALENDAR[:100])
            run_pipeline(session, settings)
            reason = verdict_for(session, stale)
            has_features = session.scalar(
                select(PitFeatureSnapshot.id).where(
                    PitFeatureSnapshot.snapshot_date == DECISION,
                    PitFeatureSnapshot.historical_security_id == stale,
                )
            )

        # It still has a feature row — that is why V1 ranked it...
        assert has_features is not None
        # ...and V2 keeps it out of the cross-section, by name.
        assert reason is PeerExclusion.NO_MARKET_DATA_ON_DATE

    def test_case_2_a_security_first_trading_after_the_date_is_excluded(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            later = add_security(session, "LATER", CALENDAR[320:])
            run_pipeline(session, settings)
            reason = verdict_for(session, later)

        # No feature row at all on D, so it was never even a candidate.
        assert reason is None

    def test_case_3_a_delisted_member_trading_on_the_date_is_included(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """The security the whole Phase 9 sequence exists to keep."""
        with session_scope(factory) as session:
            seed_live_market(session)
            alive = add_security(session, "ALIVE", CALENDAR, delisted_on=CALENDAR[360])
            run_pipeline(session, settings)
            reason = verdict_for(session, alive)

        assert reason is PeerExclusion.INCLUDED

    def test_case_4_a_delisted_member_not_trading_on_the_date_is_excluded(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            gone = add_security(session, "GONE", CALENDAR[:200], delisted_on=CALENDAR[201])
            run_pipeline(session, settings)
            reason = verdict_for(session, gone)

        assert reason in (PeerExclusion.NOT_A_MEMBER, PeerExclusion.NO_MARKET_DATA_ON_DATE)

    def test_case_5_unknown_membership_with_valid_market_data_is_excluded(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Trading is not membership. It never was."""
        with session_scope(factory) as session:
            seed_live_market(session)
            unknown = add_security(session, "NOVEN", CALENDAR, exchange=None)
            run_pipeline(session, settings)
            reason = verdict_for(session, unknown)

        assert reason is PeerExclusion.MEMBERSHIP_UNKNOWN

    def test_case_6_a_non_member_with_valid_market_data_is_excluded(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            # An exit dated before the decision date makes it a NON_MEMBER on D,
            # while its bars continue — the shape a reused symbol produces.
            outside = add_security(session, "OUTSIDE", CALENDAR, delisted_on=CALENDAR[200])
            run_pipeline(session, settings)
            reason = verdict_for(session, outside)

        assert reason is PeerExclusion.NOT_A_MEMBER

    def test_case_7_an_active_company_trading_on_the_date_is_included(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            run_pipeline(session, settings)
            company = CompanyRepository(session).get_by_ticker("L000")
            assert company is not None
            reason = explain_peer_membership(session, DECISION, company_id=company.id)

        assert reason is PeerExclusion.INCLUDED


class TestV1IsUntouched:
    """V2 appends beside V1; nothing existing moves."""

    def test_v1_rows_survive_a_v2_derivation_unchanged(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "ALIVE", CALENDAR)
            capture_day(session, settings, snapshot_date=DECISION)
            derive_market_signals(session, snapshot_date=DECISION)
            capture_historical_features(session, settings, snapshot_date=DECISION)
            derive_historical_market_signals(session, snapshot_date=DECISION)
            reconstruct_membership_record(session)
            before = sorted(
                (
                    str(r.company_id),
                    str(r.historical_security_id),
                    r.peer_population,
                    r.setups,
                    r.tradability,
                )
                for r in session.scalars(
                    select(PitSignalSnapshot).where(
                        PitSignalSnapshot.signal_version == MARKET_SIGNALS_V1
                    )
                )
            )

            derive_market_signals_v2(session, snapshot_date=DECISION)

            after = sorted(
                (
                    str(r.company_id),
                    str(r.historical_security_id),
                    r.peer_population,
                    r.setups,
                    r.tradability,
                )
                for r in session.scalars(
                    select(PitSignalSnapshot).where(
                        PitSignalSnapshot.signal_version == MARKET_SIGNALS_V1
                    )
                )
            )
            v2_count = session.scalar(
                select(PitSignalSnapshot.id).where(
                    PitSignalSnapshot.signal_version == MARKET_SIGNALS_V2
                )
            )

        assert after == before
        assert v2_count is not None

    def test_the_two_versions_coexist_for_one_security_and_date(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            run_pipeline(session, settings)
            company = CompanyRepository(session).get_by_ticker("L000")
            assert company is not None
            rows = session.scalars(
                select(PitSignalSnapshot).where(
                    PitSignalSnapshot.company_id == company.id,
                    PitSignalSnapshot.snapshot_date == DECISION,
                )
            ).all()

        assert {r.signal_version for r in rows} == {MARKET_SIGNALS_V1, MARKET_SIGNALS_V2}

    def test_v1_and_v2_agree_on_every_peer_independent_field(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """The Phase 9E finding, re-proven against real V2 rows."""
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "ALIVE", CALENDAR)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitSignalSnapshot).where(PitSignalSnapshot.snapshot_date == DECISION)
            ).all()

        by_identity: dict[tuple[int | None, int | None], dict[str, PitSignalSnapshot]] = {}
        for row in rows:
            key = (row.company_id, row.historical_security_id)
            by_identity.setdefault(key, {})[row.signal_version] = row

        compared = 0
        for versions in by_identity.values():
            if MARKET_SIGNALS_V1 not in versions or MARKET_SIGNALS_V2 not in versions:
                continue
            one, two = versions[MARKET_SIGNALS_V1], versions[MARKET_SIGNALS_V2]
            compared += 1
            for field in (
                "setups",
                "setup_evidence_json",
                "tradability",
                "tradability_reasons_json",
                "liquidity_floor_proven",
                "trend_direction",
                "trend_r2",
                "compression_ratio",
                "volatility_expansion_ratio",
                "distance_from_52w_high",
                "distance_from_52w_low",
                "stretch_20d",
            ):
                assert getattr(one, field) == getattr(two, field), field

        assert compared > 0


class TestNoSilentDrops:
    """Being outside the cross-section is not being outside the record."""

    def test_every_security_with_features_still_gets_a_v2_signal(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "STALE", CALENDAR[:100])
            add_security(session, "NOVEN", CALENDAR, exchange=None, cusip="690370101")
            run_pipeline(session, settings)
            features = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()
            v2 = session.scalars(
                select(PitSignalSnapshot).where(
                    PitSignalSnapshot.snapshot_date == DECISION,
                    PitSignalSnapshot.signal_version == MARKET_SIGNALS_V2,
                )
            ).all()

        assert len(v2) == len(features)

    def test_the_peer_population_is_smaller_than_the_row_count(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """Non-peers are ranked against the market without constituting it."""
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "STALE", CALENDAR[:100])
            add_security(session, "NOVEN", CALENDAR, exchange=None, cusip="690370101")
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitSignalSnapshot).where(
                    PitSignalSnapshot.snapshot_date == DECISION,
                    PitSignalSnapshot.signal_version == MARKET_SIGNALS_V2,
                )
            ).all()

        populations = {r.peer_population for r in rows}
        assert len(populations) == 1
        assert populations.pop() < len(rows)


class TestIdentityAndDeterminism:
    """Identity exclusivity, and a selection that does not depend on order."""

    def test_every_v2_row_names_exactly_one_identity(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "ALIVE", CALENDAR)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitSignalSnapshot).where(
                    PitSignalSnapshot.signal_version == MARKET_SIGNALS_V2
                )
            ).all()

        assert rows
        for row in rows:
            assert (row.company_id is None) != (row.historical_security_id is None)

    def test_both_identity_kinds_appear_among_the_peers(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            alive = add_security(session, "ALIVE", CALENDAR)
            run_pipeline(session, settings)
            rows = session.scalars(
                select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
            ).all()
            selection = select_peers(_peer_candidates(session, list(rows), DECISION))

        kinds = {candidate.key[0] for candidate in selection.included}
        assert kinds == {COMPANY, HISTORICAL}
        assert (HISTORICAL, alive) in {c.key for c in selection.included}

    def test_the_selection_does_not_depend_on_candidate_order(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "ALIVE", CALENDAR)
            add_security(session, "STALE", CALENDAR[:100], cusip="690370101")
            run_pipeline(session, settings)
            rows = list(
                session.scalars(
                    select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
                ).all()
            )
            candidates = _peer_candidates(session, rows, DECISION)

        shuffled = list(candidates)
        random.Random(5).shuffle(shuffled)  # noqa: S311
        first = select_peers(candidates)
        second = select_peers(shuffled)

        assert {c.key for c in first.included} == {c.key for c in second.included}
        assert dict(first.reasons()) == dict(second.reasons())
        assert (
            build_peer_context(first.features).sorted_values
            == build_peer_context(second.features).sorted_values
        )

    def test_the_percentiles_are_reproducible(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            run_pipeline(session, settings)
            rows = list(
                session.scalars(
                    select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
                ).all()
            )
            peers = build_peer_context(
                select_peers(_peer_candidates(session, rows, DECISION)).features
            )
            feats = [_to_features(r) for r in rows]

        once = [derive_signals(f, peers, thresholds=DEFAULT_THRESHOLDS) for f in feats]
        twice = [derive_signals(f, peers, thresholds=DEFAULT_THRESHOLDS) for f in feats]
        assert once == twice


class TestNoFutureLeakage:
    """The peer set may be narrower than V1's; it must never be better informed."""

    def test_a_later_delisting_does_not_remove_a_valid_peer(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        """A future exit must not retroactively evict a member that traded on D."""
        with session_scope(factory) as session:
            seed_live_market(session)
            security_id = add_security(session, "ALIVE", CALENDAR)
            run_pipeline(session, settings)
            before = verdict_for(session, security_id)

        with session_scope(factory) as session:
            DelistingRepository(session).add_all(
                [
                    {
                        "ticker": "ALIVE",
                        "kind": CorporateActionKind.CASH_MERGER.value,
                        "effective_date": CALENDAR[380],
                        "source": "test",
                        "source_id": "alive-later-exit",
                    }
                ]
            )
            reconstruct_membership_record(session)
            after = verdict_for(session, security_id)

        assert before is PeerExclusion.INCLUDED
        assert after is PeerExclusion.INCLUDED

    def test_no_peer_feature_reaches_past_the_decision_date(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "ALIVE", CALENDAR)
            run_pipeline(session, settings)
            rows = list(
                session.scalars(
                    select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
                ).all()
            )
            selection = select_peers(_peer_candidates(session, rows, DECISION))

        assert selection.included
        # The market-data test is equality, so every peer's newest bar is D itself.
        assert all(
            c.features is not None and c.features.as_of == DECISION for c in selection.included
        )

    def test_bars_added_after_the_date_do_not_change_the_peer_set(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            security_id = add_security(session, "ALIVE", CALENDAR[:301])
            run_pipeline(session, settings)
            before = verdict_for(session, security_id)

        with session_scope(factory) as session:
            HistoricalPriceRepository(session).upsert_bars(
                security_id, bars(CALENDAR[301:], base=99.0)
            )
            after = verdict_for(session, security_id)

        assert after == before


class TestProvenance:
    """A researcher must be able to ask why, for any security and any date."""

    def test_every_exclusion_carries_a_stated_reason(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "STALE", CALENDAR[:100])
            add_security(session, "NOVEN", CALENDAR, exchange=None, cusip="690370101")
            run_pipeline(session, settings)
            rows = list(
                session.scalars(
                    select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
                ).all()
            )
            selection = select_peers(_peer_candidates(session, rows, DECISION))

        assert selection.excluded
        assert len(selection.included) + len(selection.excluded) == len(rows)
        assert all(reason is not PeerExclusion.INCLUDED for _, reason in selection.excluded)
        assert sum(dict(selection.reasons()).values()) == len(selection.excluded)

    def test_explaining_requires_exactly_one_identity(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            run_pipeline(session, settings)
            with pytest.raises(ValueError, match="exactly one identity"):
                explain_peer_membership(session, DECISION, company_id=1, historical_security_id=1)
            with pytest.raises(ValueError, match="exactly one identity"):
                explain_peer_membership(session, DECISION)


class TestTheMeasuredDifference:
    """V2 must actually differ from V1, and only where it is supposed to."""

    def test_v2_ranks_against_fewer_peers_than_v1_did(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "STALE", CALENDAR[:100])
            add_security(session, "NOVEN", CALENDAR, exchange=None, cusip="690370101")
            run_pipeline(session, settings)
            rows = list(
                session.scalars(
                    select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
                ).all()
            )
            v1_full = build_peer_context([_to_features(r) for r in rows])
            v2 = build_peer_context(
                select_peers(_peer_candidates(session, rows, DECISION)).features
            )

        assert v2.population < v1_full.population

    def test_the_percentile_difference_is_measurable(
        self, factory: sessionmaker[Session], settings: Settings
    ) -> None:
        with session_scope(factory) as session:
            seed_live_market(session)
            add_security(session, "STALE", CALENDAR[:100])
            run_pipeline(session, settings)
            rows = list(
                session.scalars(
                    select(PitFeatureSnapshot).where(PitFeatureSnapshot.snapshot_date == DECISION)
                ).all()
            )
            feats = [_to_features(r) for r in rows]
            v1_full = build_peer_context(feats)
            v2 = build_peer_context(
                select_peers(_peer_candidates(session, rows, DECISION)).features
            )

        deltas = [
            abs(a - b)
            for f in feats
            for name in PERCENTILE_FEATURES
            if (a := v1_full.percentile(name, getattr(f, name, None))) is not None
            and (b := v2.percentile(name, getattr(f, name, None))) is not None
        ]
        assert deltas
        assert max(deltas) > 0.0
