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
    PitFeatureSnapshot,
    PitFeatureSnapshotRepository,
    PitSignalSnapshotRepository,
)
from domain import (
    CURRENT_SIGNAL_VERSION,
    DEFAULT_THRESHOLDS,
    MARKET_SIGNALS_V2,
    MarketFeatures,
    PeerCandidate,
    PeerExclusion,
    build_peer_context,
    derive_signals,
    explain_peer,
    select_peers,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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
        "historical_security_id",
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
        # The identity travels from the feature row unchanged. A signal describes
        # the security its features described, and a delisted security keeps its
        # `historical_security_id` all the way through — which is what stops a
        # signal filter from silently dropping the delisted population.
        "company_id": row.company_id,
        "historical_security_id": row.historical_security_id,
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


def derive_historical_market_signals(
    session: Session,
    *,
    snapshot_date: date,
    feature_version: str = "MARKET_FEATURES_V1",
    signal_version: str = CURRENT_SIGNAL_VERSION,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
) -> SignalDerivationResult:
    """Derive signals for the delisted securities on a day, leaving the rest alone.

    `derive_market_signals` refuses a day it has already described, which is the
    immutability guarantee the whole point-in-time layer rests on. Backfilling
    features for securities that left the market therefore cannot be followed by
    re-deriving the day — that would rewrite what the system said at the time.
    This derives **only the historical-security rows that have no signal yet**,
    and touches no existing row.

    `MARKET_SIGNALS_V1` is unchanged: the same `derive_signals`, the same
    thresholds, the same version stamp. What differs is which rows are written.

    **The peer basis, stated plainly.** Percentiles are cross-sectional, so they
    depend on the population they are measured against. These rows are measured
    against the day's *full* feature set — live companies and delisted securities
    together — which is the population that actually traded that day. The company
    rows already stored for that day were derived before the delisted securities
    had features, so they carry a live-only basis. Both are recorded in
    `peer_population`, so the difference is visible in the data rather than
    hidden in it, and making the whole day uniform would mean re-deriving it
    under a new signal version — which is the project's versioned path and is
    deliberately not taken here.

    Args:
        session: Open database session. The caller commits.
        snapshot_date: The day to derive.
        feature_version: Which feature definitions to read.
        signal_version: Which signal rules to stamp.
        thresholds: The cut-offs to apply, unchanged from the frozen defaults.

    Returns:
        What was written.
    """
    features_repo = PitFeatureSnapshotRepository(session)
    stored = PitSignalSnapshotRepository(session)
    captured_at = datetime.now(UTC)

    rows = features_repo.for_date(snapshot_date, feature_version=feature_version)
    if not rows:
        log.warning(
            "no features stored for this day and version",
            snapshot_date=str(snapshot_date),
            feature_version=feature_version,
        )
        return SignalDerivationResult(
            snapshot_date=snapshot_date,
            signal_version=signal_version,
            feature_version=feature_version,
        )

    # The peer context spans every security that had features that day, which is
    # the market a percentile should be measured against.
    peers = build_peer_context([_to_features(row) for row in rows])

    already = {
        row.historical_security_id
        for row in stored.for_date(snapshot_date, signal_version=signal_version)
        if row.historical_security_id is not None
    }
    pending = [
        row
        for row in rows
        if row.historical_security_id is not None and row.historical_security_id not in already
    ]
    if not pending:
        return SignalDerivationResult(
            snapshot_date=snapshot_date,
            signal_version=signal_version,
            feature_version=feature_version,
            peer_population=peers.population,
            already_derived=True,
        )

    signal_rows = [
        _signal_row(
            derive_signals(
                _to_features(row), peers, thresholds=thresholds, signal_version=signal_version
            ),
            row,
            peer_population=peers.population,
            captured_at=captured_at,
        )
        for row in pending
    ]
    written = stored.add_all(signal_rows)
    log.info(
        "historical market signals derived",
        snapshot_date=str(snapshot_date),
        derived=written,
        peers=peers.population,
    )
    return SignalDerivationResult(
        snapshot_date=snapshot_date,
        signal_version=signal_version,
        feature_version=feature_version,
        derived=written,
        peer_population=peers.population,
    )


#: What `MARKET_SIGNALS_V2` accepts as evidence that a security actually had
#: market data on the decision date.
#:
#: **A feature row whose `feature_as_of` equals the snapshot date.** That field is
#: the newest bar the feature computation used, so equality means a bar exists on
#: the date itself — verified against the price record on staging, where the two
#: tests agree exactly: 4,432 company rows and 1,532 historical rows satisfy both.
#:
#: Three properties make it the right test rather than merely a convenient one.
#: It is **stored**, so no re-derivation and no join to six million bars. It is
#: **point-in-time safe by construction**, because `feature_as_of` can never
#: exceed the snapshot date. And it **rejects stale bars**, which is the entire
#: defect Phase 9E found.
#:
#: Volume is deliberately not part of the test. 11.5% of company bars and 28.7%
#: of historical bars carry zero volume on this feed, because the free IEX tape
#: prints only its own share — measured elsewhere in this project at between
#: 0.006% and 12% of consolidated. A zero there is evidence about the feed, not
#: about whether the security traded, and excluding on it would drop two fifths
#: of the historical cross-section for an artifact.
TRADING_EVIDENCE = "feature_as_of == snapshot_date"


def _peer_candidates(
    session: Session, rows: Sequence[PitFeatureSnapshot], snapshot_date: date
) -> list[PeerCandidate]:
    """Build the peer candidates for a date from stored membership and features.

    The membership resolver is imported here rather than at module scope because
    `stock_screener.backtest.backfill` imports this module: binding the two at
    import time would close a cycle. The dependency is real and one-directional —
    the V2 peer policy needs canonical membership — so the import is deferred
    rather than the direction inverted.
    """
    from stock_screener.backtest.membership import COMPANY, HISTORICAL, members_as_of

    universe = members_as_of(session, snapshot_date)
    members = {(kind, identity) for kind, identity, _ in universe.members}
    unknown = set(universe.unknown)

    candidates: list[PeerCandidate] = []
    for row in rows:
        key = (
            (COMPANY, row.company_id)
            if row.company_id is not None
            else (HISTORICAL, row.historical_security_id or 0)
        )
        candidates.append(
            PeerCandidate(
                key=key,
                features=_to_features(row),
                is_member=key in members,
                membership_known=key not in unknown,
                has_market_data=row.feature_as_of == snapshot_date,
            )
        )
    return candidates


def derive_market_signals_v2(
    session: Session,
    *,
    snapshot_date: date,
    feature_version: str = "MARKET_FEATURES_V1",
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
) -> SignalDerivationResult:
    """Derive `MARKET_SIGNALS_V2` for a date: same rules, corrected peer policy.

    V2 changes exactly one thing. Every threshold, classification rule and
    percentile method is the frozen V1 arithmetic — this function calls the same
    `derive_signals` — and the only difference is which securities constitute the
    cross-section the percentiles are measured against:

        V2 peers(D) = members_as_of(D) ∩ securities with market data on D

    **V1 is untouched.** V2 rows are appended beside it under a different
    `signal_version`, which the two partial unique indexes already permit, so a
    researcher can compare the two and no existing row moves.

    **Every security with a feature row still gets a signal.** Being outside the
    cross-section means a security does not *constitute* the market; it does not
    mean the market has nothing to say about it. Writing rows only for peers would
    drop non-members silently, which is the class of failure this phase exists to
    end — so a non-peer is ranked against the peer universe and keeps its row.

    Args:
        session: Open database session. The caller commits.
        snapshot_date: The day to derive.
        feature_version: Which feature definitions to read.
        thresholds: The cut-offs to apply, unchanged from the frozen defaults.

    Returns:
        What was written, with the peer population the percentiles used.
    """
    features_repo = PitFeatureSnapshotRepository(session)
    stored = PitSignalSnapshotRepository(session)
    captured_at = datetime.now(UTC)

    rows = features_repo.for_date(snapshot_date, feature_version=feature_version)
    if not rows:
        log.warning(
            "no features stored for this day and version",
            snapshot_date=str(snapshot_date),
            feature_version=feature_version,
        )
        return SignalDerivationResult(
            snapshot_date=snapshot_date,
            signal_version=MARKET_SIGNALS_V2,
            feature_version=feature_version,
        )

    candidates = _peer_candidates(session, rows, snapshot_date)
    selection = select_peers(candidates)
    peers = build_peer_context(selection.features)

    already = stored.for_date(snapshot_date, signal_version=MARKET_SIGNALS_V2)
    if already:
        return SignalDerivationResult(
            snapshot_date=snapshot_date,
            signal_version=MARKET_SIGNALS_V2,
            feature_version=feature_version,
            peer_population=peers.population,
            already_derived=True,
        )

    signal_rows = [
        _signal_row(
            derive_signals(
                _to_features(row),
                peers,
                thresholds=thresholds,
                signal_version=MARKET_SIGNALS_V2,
            ),
            row,
            peer_population=peers.population,
            captured_at=captured_at,
        )
        for row in rows
    ]
    written = stored.add_all(signal_rows)
    log.info(
        "market signals derived under the V2 peer policy",
        snapshot_date=str(snapshot_date),
        derived=written,
        peers=peers.population,
        excluded=len(selection.excluded),
        reasons=dict(selection.reasons()),
    )
    return SignalDerivationResult(
        snapshot_date=snapshot_date,
        signal_version=MARKET_SIGNALS_V2,
        feature_version=feature_version,
        derived=written,
        peer_population=peers.population,
    )


def explain_peer_membership(
    session: Session,
    snapshot_date: date,
    *,
    company_id: int | None = None,
    historical_security_id: int | None = None,
    feature_version: str = "MARKET_FEATURES_V1",
) -> PeerExclusion | None:
    """Say why one security was, or was not, in the V2 cross-section on a date.

    The provenance question a researcher must be able to ask of any historical
    peer universe. It is answered from stored evidence — the membership record
    and the feature row's own `feature_as_of` — rather than from a flag, because
    both inputs are already persisted and a stored verdict could only drift from
    the rule that produced it.

    Args:
        session: An open session.
        snapshot_date: The date to explain.
        company_id: A live company's id, or None.
        historical_security_id: A historical security's id, or None.
        feature_version: Which feature definitions to read.

    Returns:
        The verdict, or None when the security had no feature row that day and so
        was never a candidate.

    Raises:
        ValueError: If neither identity or both are given.
    """
    if (company_id is None) == (historical_security_id is None):
        message = (
            "explain exactly one identity; got "
            f"company_id={company_id!r}, historical_security_id={historical_security_id!r}"
        )
        raise ValueError(message)

    from stock_screener.backtest.membership import COMPANY, HISTORICAL

    rows = PitFeatureSnapshotRepository(session).for_date(
        snapshot_date, feature_version=feature_version
    )
    key = (
        (COMPANY, company_id)
        if company_id is not None
        else (HISTORICAL, historical_security_id or 0)
    )
    return explain_peer(_peer_candidates(session, rows, snapshot_date), key)
