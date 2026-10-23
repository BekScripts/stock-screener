"""Deriving market signals: point-in-time, versioned, immutable, and inert.

The load-bearing test here is `test_a_stored_signal_cannot_change_when_...`.
Everything else in this phase is arithmetic; that one is the guarantee a future
backtest actually rests on.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

from data_access import (
    CompanyRepository,
    ImmutableSnapshotError,
    PitFeatureSnapshotRepository,
    PitSignalSnapshotRepository,
    ScoreSnapshotRepository,
)
from domain import CURRENT_SCORE_VERSION, MARKET_SIGNALS_V1, SignalThresholds
from stock_screener.config import Settings
from stock_screener.pit import FEATURE_VERSION, capture_day
from stock_screener.signals import derive_market_signals

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Seed

SNAPSHOT = date(2026, 6, 30)


@pytest.fixture
def settings() -> Settings:
    """Settings with the benchmark the seed fixture writes."""
    return Settings(benchmark_symbol="SPY")


def _seed_series(session: Session, ticker: str, closes: list[float]) -> int:
    """Store a company with a designed price series and return its id.

    The shared `make` fixture builds a constant close, which leaves most
    windows degenerate — a flat series has no trend fit and no compression
    ratio. These tests need a security that actually moves, so they write their
    own bars.
    """
    from data_access import CompanyRepository, PriceHistoryRepository
    from domain import CompanyProfile, PriceBar

    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None

    start = SNAPSHOT - timedelta(days=len(closes) - 1)
    PriceHistoryRepository(session).upsert_bars(
        stored.id,
        [
            PriceBar(
                date=start + timedelta(days=offset),
                open=close,
                high=close * 1.02,
                low=close * 0.98,
                close=close,
                volume=400_000.0,
            )
            for offset, close in enumerate(closes)
        ],
    )
    session.flush()
    return stored.id


@pytest.fixture
def captured(session: Session, settings: Settings, scored_company: str) -> str:
    """A completed capture, which is what signals are derived from."""
    # A 120-session series that halves its range in the last 60, so the
    # compression ratio is well-defined and near the V1 boundary.
    closes = [100.0 + (4.0 if index % 2 else -4.0) for index in range(60)]
    closes += [100.0 + (1.0 if index % 2 else -1.0) for index in range(60)]
    _seed_series(session, "SERIES", closes)

    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()
    return scored_company


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_signals_are_derived_from_the_captured_features(session: Session, captured: str) -> None:
    result = derive_market_signals(session, snapshot_date=SNAPSHOT)

    assert result.derived == 2
    stored = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )
    assert captured in {row.ticker for row in stored}


@pytest.mark.integration
def test_both_versions_travel_with_every_row(session: Session, captured: str) -> None:
    """A signal is only meaningful against the features it read."""
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()

    row = PitSignalSnapshotRepository(session).for_date(SNAPSHOT, signal_version=MARKET_SIGNALS_V1)[
        0
    ]
    assert row.signal_version == MARKET_SIGNALS_V1
    assert row.feature_version == FEATURE_VERSION


@pytest.mark.integration
def test_the_peer_population_is_stored_beside_the_percentiles(
    session: Session, captured: str
) -> None:
    """A percentile without its population is not a percentile."""
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()

    row = PitSignalSnapshotRepository(session).for_date(SNAPSHOT, signal_version=MARKET_SIGNALS_V1)[
        0
    ]
    assert row.peer_population >= 1


@pytest.mark.integration
def test_the_feature_watermark_is_carried_forward(session: Session, captured: str) -> None:
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()

    features = PitFeatureSnapshotRepository(session).for_date(
        SNAPSHOT, feature_version=FEATURE_VERSION
    )[0]
    signals = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )[0]
    assert signals.feature_as_of == features.feature_as_of


@pytest.mark.integration
def test_deriving_a_day_twice_writes_nothing(session: Session, captured: str) -> None:
    first = derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()
    second = derive_market_signals(session, snapshot_date=SNAPSHOT)

    assert first.already_derived is False
    assert second.already_derived is True
    assert second.derived == 0
    assert PitSignalSnapshotRepository(session).count() == first.derived


@pytest.mark.integration
def test_a_day_with_no_features_derives_nothing(session: Session) -> None:
    result = derive_market_signals(session, snapshot_date=date(2020, 1, 1))

    assert result.derived == 0
    assert PitSignalSnapshotRepository(session).count() == 0


# ---------------------------------------------------------------------------
# Point-in-time correctness
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_stored_signal_cannot_change_when_operational_tables_move(
    session: Session, captured: str, seed: type[Seed]
) -> None:
    """The guarantee the whole phase rests on.

    Signals read the immutable feature record and nothing else. Rewriting the
    operational score for the same day — which the score command does routinely
    — must leave a derived signal untouched.
    """
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()
    before = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )[0]
    original = (before.compression_ratio, before.tradability, before.setups)

    company = CompanyRepository(session).get_by_ticker(captured)
    assert company is not None
    seed.score(session, company.id, captured, final=99.0, score_date=SNAPSHOT)
    company.is_active = False
    session.flush()

    after = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )[0]
    assert (after.compression_ratio, after.tradability, after.setups) == original


@pytest.mark.integration
def test_derivation_never_reads_the_operational_score(session: Session, captured: str) -> None:
    """Signals describe the market, not the fundamentals. Deleting the score
    must not change a single derived value."""
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()
    first = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )[0]
    snapshot = (first.trend_r2, first.tradability, first.percentiles_json)

    for row in ScoreSnapshotRepository(session).list_for_date(
        score_date=SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    ):
        session.delete(row)
    session.flush()

    again = derive_market_signals(
        session, snapshot_date=SNAPSHOT, signal_version="MARKET_SIGNALS_TEST"
    )
    session.flush()
    second = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version="MARKET_SIGNALS_TEST"
    )[0]

    assert again.derived == 2
    assert (second.trend_r2, second.tradability, second.percentiles_json) == snapshot


@pytest.mark.integration
def test_a_later_capture_does_not_alter_an_earlier_days_signals(
    session: Session, settings: Settings, captured: str
) -> None:
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()
    before = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )[0]
    original = before.compression_ratio

    capture_day(session, settings, snapshot_date=date(2026, 7, 15))
    session.flush()
    derive_market_signals(session, snapshot_date=date(2026, 7, 15))
    session.flush()

    after = PitSignalSnapshotRepository(session).for_date(
        SNAPSHOT, signal_version=MARKET_SIGNALS_V1
    )[0]
    assert after.compression_ratio == original


# ---------------------------------------------------------------------------
# Versioning and immutability
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_new_version_appends_beside_the_old_one(session: Session, captured: str) -> None:
    """Changing the rules must never restate what the system said at the time."""
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()
    derive_market_signals(
        session,
        snapshot_date=SNAPSHOT,
        signal_version="MARKET_SIGNALS_V2",
        thresholds=SignalThresholds(compression_max_ratio=0.99),
    )
    session.flush()

    repository = PitSignalSnapshotRepository(session)
    # Two companies under each of two versions: nothing was replaced.
    assert repository.count() == 4
    assert len(repository.for_date(SNAPSHOT, signal_version=MARKET_SIGNALS_V1)) == 2
    assert set(repository.versions_for_date(SNAPSHOT)) == {
        MARKET_SIGNALS_V1,
        "MARKET_SIGNALS_V2",
    }


@pytest.mark.integration
def test_the_two_versions_can_disagree_and_both_survive(session: Session, captured: str) -> None:
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    # The seeded bars hold a constant close, so the 20- and 60-session ranges
    # are equal and the compression ratio is exactly 1.0. V1's 0.70 cut-off
    # rejects it; a looser version accepts it. Same day, same features, two
    # defensible answers — which is the whole reason both are kept.
    derive_market_signals(
        session,
        snapshot_date=SNAPSHOT,
        signal_version="MARKET_SIGNALS_V2",
        thresholds=SignalThresholds(compression_max_ratio=1.5),
    )
    session.flush()

    repository = PitSignalSnapshotRepository(session)
    first = {
        row.ticker: row.setups
        for row in repository.for_date(SNAPSHOT, signal_version=MARKET_SIGNALS_V1)
    }
    second = {
        row.ticker: row.setups
        for row in repository.for_date(SNAPSHOT, signal_version="MARKET_SIGNALS_V2")
    }

    assert first["SERIES"] == "NONE"
    assert second["SERIES"] == "COMPRESSION"


@pytest.mark.integration
def test_a_stored_signal_row_is_never_rewritten(session: Session, captured: str) -> None:
    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()
    row = PitSignalSnapshotRepository(session).for_date(SNAPSHOT, signal_version=MARKET_SIGNALS_V1)[
        0
    ]

    with pytest.raises(ImmutableSnapshotError):
        PitSignalSnapshotRepository(session).add_all(
            [
                {
                    "run_id": row.run_id,
                    "company_id": row.company_id,
                    "ticker": row.ticker,
                    "snapshot_date": SNAPSHOT,
                    "signal_version": MARKET_SIGNALS_V1,
                    "feature_version": FEATURE_VERSION,
                    "feature_as_of": SNAPSHOT,
                    "bars_available": 1,
                    "peer_population": 1,
                    "trend_direction": "UP",
                    "setups": "NONE",
                    "tradability": "TRADABLE",
                    "liquidity_floor_proven": True,
                }
            ]
        )
    session.rollback()


@pytest.mark.integration
def test_the_signal_repository_exposes_no_update_path() -> None:
    methods = {name for name in dir(PitSignalSnapshotRepository) if not name.startswith("_")}

    assert not methods & {"update", "upsert", "replace", "delete", "save"}


# ---------------------------------------------------------------------------
# CompounderScore isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_deriving_signals_cannot_alter_any_score(session: Session, captured: str) -> None:
    """The guard the whole architecture exists to enforce."""
    scores = ScoreSnapshotRepository(session)
    before = {
        row.company_id: (row.final_score, row.scoring_status, row.score_version)
        for row in scores.list_for_date(score_date=SNAPSHOT, score_version=CURRENT_SCORE_VERSION)
    }

    derive_market_signals(session, snapshot_date=SNAPSHOT)
    session.flush()

    after = {
        row.company_id: (row.final_score, row.scoring_status, row.score_version)
        for row in scores.list_for_date(score_date=SNAPSHOT, score_version=CURRENT_SCORE_VERSION)
    }
    assert after == before


@pytest.mark.integration
def test_the_signal_table_holds_no_score_column() -> None:
    """Structural, not conventional: there is nowhere to put a composite."""
    from data_access import PitSignalSnapshot

    columns = {column.key.lower() for column in PitSignalSnapshot.__table__.columns}
    assert not columns & {
        "final_score",
        "opportunity_score",
        "combined_score",
        "trading_score",
        "rank",
        "composite",
    }


@pytest.mark.integration
def test_the_signals_package_never_imports_the_scoring_engine() -> None:
    """A dependency here is how the separation would quietly erode."""
    from pathlib import Path

    import stock_screener.signals as package

    # Absolute: the test suite chdirs into a tmp_path, so a relative path here
    # would silently read nothing and pass.
    root = Path(package.__file__).parent
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert text

    assert "score_market" not in text
    assert "from domain import" in text
    assert "scoring" not in text
