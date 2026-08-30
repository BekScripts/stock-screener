"""Read one day's features, derive the signals, store them immutably.

No provider is called and no bar is recomputed. Everything here starts from
rows Phase 8B already wrote and finished with, which is what makes a signal for
a past date reproducible rather than merely plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from data_access import (
    PitFeatureSnapshotRepository,
    PitSignalSnapshotRepository,
)
from domain import (
    CURRENT_SIGNAL_VERSION,
    DEFAULT_THRESHOLDS,
    MarketFeatures,
    build_peer_context,
    derive_signals,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from data_access import PitFeatureSnapshot
    from domain import MarketSignals, SignalThresholds

log = structlog.get_logger(__name__)

#: Feature columns that are not part of `MarketFeatures` and must not be passed
#: to it. The snapshot row carries storage and provenance fields beside the
#: features themselves.
_NOT_FEATURES = frozenset(
    {
        "id",
        "run_id",
        "company_id",
        "ticker",
        "snapshot_date",
        "captured_at",
        "feature_version",
        "feature_as_of",
        "benchmark_symbol",
    }
)


@dataclass(frozen=True)
class SignalDerivationResult:
    """What one derivation pass produced.

    Attributes:
        snapshot_date: The day described.
        signal_version: The rules applied.
        feature_version: The feature definitions read.
        derived: Signal rows written.
        peer_population: Companies the percentiles were measured against.
        already_derived: True when this day and version were already stored and
            nothing was written.
        setups: How many companies carried each setup label.
        tradability: How many companies fell into each verdict.
    """

    snapshot_date: date
    signal_version: str = CURRENT_SIGNAL_VERSION
    feature_version: str = ""
    derived: int = 0
    peer_population: int = 0
    already_derived: bool = False
    setups: dict[str, int] | None = None
    tradability: dict[str, int] | None = None

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        if self.already_derived:
            return (
                f"{self.snapshot_date}: already derived under "
                f"{self.signal_version}, nothing written"
            )
        verdicts = self.tradability or {}
        return (
            f"{self.snapshot_date}: {self.derived} signals over "
            f"{self.peer_population} peers — "
            f"{verdicts.get('TRADABLE', 0)} tradable, "
            f"{verdicts.get('UNTRADABLE', 0)} untradable, "
            f"{verdicts.get('UNVERIFIED', 0)} unverified"
        )


def _to_features(row: PitFeatureSnapshot) -> MarketFeatures:
    """Rebuild the domain features from one stored row.

    `as_of` comes from `feature_as_of` — the newest bar the computation used,
    not the day it was filed under. A security whose last bar is months old
    keeps that watermark, so a signal derived from it is visibly stale rather
    than silently current.
    """
    values = {
        column.key: getattr(row, column.key)
        for column in row.__table__.columns
        if column.key not in _NOT_FEATURES
    }
    return MarketFeatures(as_of=row.feature_as_of, **values)


def _signal_row(
    signals: MarketSignals,
    row: PitFeatureSnapshot,
    *,
    peer_population: int,
    captured_at: datetime,
) -> dict[str, Any]:
    """Return the column mapping for one derived signal."""
    return {
        "run_id": row.run_id,
        "company_id": row.company_id,
        "ticker": row.ticker,
        "snapshot_date": row.snapshot_date,
        "captured_at": captured_at,
        "signal_version": signals.signal_version,
        "feature_version": row.feature_version,
        "feature_as_of": row.feature_as_of,
        "bars_available": row.bars_available,
        "peer_population": peer_population,
        "compression_ratio": signals.compression_ratio,
        "volatility_expansion_ratio": signals.volatility_expansion_ratio,
        "distance_from_52w_high": signals.distance_from_52w_high,
        "distance_from_52w_low": signals.distance_from_52w_low,
        "stretch_20d": signals.stretch_20d,
        "trend_direction": signals.trend_direction.value,
        "trend_r2": signals.trend_r2,
        "percentiles_json": dict(signals.percentiles),
        "setups": "|".join(setup.value for setup in signals.setups),
        "setup_evidence_json": list(signals.setup_evidence),
        "tradability": signals.tradability.value,
        "tradability_reasons_json": list(signals.tradability_reasons),
        "liquidity_floor_proven": signals.liquidity_floor_proven,
    }


def derive_market_signals(
    session: Session,
    *,
    snapshot_date: date | None = None,
    feature_version: str = "MARKET_FEATURES_V1",
    signal_version: str = CURRENT_SIGNAL_VERSION,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
) -> SignalDerivationResult:
    """Derive and store one day's market signals.

    Reads only `pit_feature_snapshots`, so nothing a later operational write
    changes can reach the result.

    Args:
        session: Open database session. The caller commits.
        snapshot_date: The day to derive. Defaults to the most recent day with
            stored features.
        feature_version: Which feature definitions to read.
        signal_version: Which signal rules to stamp on the output. Deriving a
            second version for the same day appends beside the first.
        thresholds: The cut-offs to apply. All provisional; see
            `domain.signals.SignalThresholds`.

    Returns:
        What was written, or a result marked `already_derived`.

    Raises:
        ImmutableSnapshotError: If rows already exist for the day and version.
    """
    features_repo = PitFeatureSnapshotRepository(session)
    as_of = snapshot_date or features_repo.latest_snapshot_date(feature_version=feature_version)
    captured_at = datetime.now(UTC)

    if as_of is None:
        log.warning("no stored features to derive signals from")
        return SignalDerivationResult(
            snapshot_date=captured_at.date(),
            signal_version=signal_version,
            feature_version=feature_version,
        )

    stored = PitSignalSnapshotRepository(session)
    if stored.for_date(as_of, signal_version=signal_version):
        log.info(
            "market signals already derived",
            snapshot_date=str(as_of),
            signal_version=signal_version,
        )
        return SignalDerivationResult(
            snapshot_date=as_of,
            signal_version=signal_version,
            feature_version=feature_version,
            already_derived=True,
        )

    rows = features_repo.for_date(as_of, feature_version=feature_version)
    if not rows:
        log.warning(
            "no features stored for this day and version",
            snapshot_date=str(as_of),
            feature_version=feature_version,
        )
        return SignalDerivationResult(
            snapshot_date=as_of,
            signal_version=signal_version,
            feature_version=feature_version,
        )

    features = [_to_features(row) for row in rows]
    # Built once, from this day's rows alone. A context spanning two dates would
    # measure a security against a market that had already moved.
    peers = build_peer_context(features)

    setup_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    payload: list[dict[str, Any]] = []
    for row, item in zip(rows, features, strict=True):
        signals = derive_signals(item, peers, thresholds=thresholds, signal_version=signal_version)
        for setup in signals.setups:
            setup_counts[setup.value] = setup_counts.get(setup.value, 0) + 1
        verdict = signals.tradability.value
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        payload.append(
            _signal_row(signals, row, peer_population=peers.population, captured_at=captured_at)
        )

    written = stored.add_all(payload)
    log.info(
        "market signals derived",
        snapshot_date=str(as_of),
        signal_version=signal_version,
        derived=written,
        peers=peers.population,
        **verdict_counts,
    )
    return SignalDerivationResult(
        snapshot_date=as_of,
        signal_version=signal_version,
        feature_version=feature_version,
        derived=written,
        peer_population=peers.population,
        setups=setup_counts,
        tradability=verdict_counts,
    )
