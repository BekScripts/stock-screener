"""The daily capture pass: copy today's knowledge into tables that never change.

Reads only what is already stored. No provider is called, nothing is fetched,
and no scoring formula runs — the scores captured are the ones the score command
already computed. That is deliberate: a capture that recomputed anything would
be recording what today's code makes of the data, not what the system believed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from data_access import (
    EVENT_ACTIVE,
    EVENT_DELISTED,
    EVENT_REACTIVATED,
    OBSERVATION_BASELINE,
    OBSERVATION_OBSERVED,
    BenchmarkPriceRepository,
    CompanyRepository,
    PitCaptureRunRepository,
    PitFeatureSnapshotRepository,
    PitScoreSnapshotRepository,
    PriceHistoryRepository,
    ScoreSnapshotRepository,
    UniverseEventRepository,
    to_price_bar,
)
from domain import (
    CURRENT_SCORE_VERSION,
    Freshness,
    ScoringStatus,
    compute_market_features,
    is_rank_eligible,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from data_access import BenchmarkPrice, Company, PriceHistory, ScoreSnapshot
    from domain import MarketFeatures, PriceBar
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

#: The feature definitions in force. Bump this — never edit a definition in
#: place — when a formula or a window changes, so two rows carrying the same
#: version are always comparable. The same discipline `score_version` applies to
#: the score, and for the same reason.
FEATURE_VERSION = "MARKET_FEATURES_V1"

#: The provenance string every row this module writes carries.
_SOURCE = "pit-capture"


@dataclass(frozen=True)
class CaptureResult:
    """What one capture recorded.

    Attributes:
        snapshot_date: The day described.
        captured_at: When the process ran.
        already_captured: True when a completed run already existed and nothing
            was written. Re-running a day is a no-op, not an error.
        scores: Score snapshots written.
        features: Feature snapshots written.
        universe_events: Membership events written.
        companies_examined: Companies the pass looked at.
        score_version: The scoring rules captured.
        feature_version: The feature definitions captured.
        source_score_date: The score date the snapshots were copied from. Equal
            to `snapshot_date` when the day was scored; earlier when the capture
            ran against an older scoring run, which a reader must be able to see.
    """

    snapshot_date: date
    captured_at: datetime
    already_captured: bool = False
    scores: int = 0
    features: int = 0
    universe_events: int = 0
    companies_examined: int = 0
    score_version: str = CURRENT_SCORE_VERSION
    feature_version: str = FEATURE_VERSION
    source_score_date: date | None = None

    def summary(self) -> str:
        """Return a one-line description for a terminal."""
        if self.already_captured:
            return f"{self.snapshot_date}: already captured, nothing written"
        return (
            f"{self.snapshot_date}: {self.scores} scores, {self.features} features, "
            f"{self.universe_events} universe events "
            f"over {self.companies_examined} companies"
        )


def _fingerprint(*parts: object) -> str:
    """Return a stable hex digest over an observation's identity.

    Used instead of a unique constraint over the raw columns because several of
    those columns are nullable, and SQL treats two NULLs as distinct — so a
    repeated ingestion would accumulate duplicates rather than being ignored.
    """
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _score_row(
    snapshot: ScoreSnapshot,
    company: Company,
    *,
    run_id: int,
    snapshot_date: date,
    captured_at: datetime,
) -> dict[str, Any]:
    """Return the column mapping for one captured score.

    Copies the operational row verbatim. `rank_eligible` is the one derived
    field, and it is derived here rather than left to a reader because the rule
    belongs to the score's own version — a later reader would apply a later rule
    to an older row.
    """
    status = ScoringStatus(snapshot.scoring_status)
    freshness = Freshness(snapshot.freshness) if snapshot.freshness else Freshness.CURRENT
    return {
        "run_id": run_id,
        "company_id": snapshot.company_id,
        "ticker": company.ticker,
        "snapshot_date": snapshot_date,
        "captured_at": captured_at,
        "source_score_date": snapshot.score_date,
        "score_version": snapshot.score_version,
        "scoring_status": snapshot.scoring_status,
        "final_score": snapshot.final_score,
        "raw_score": snapshot.raw_score,
        "growth_score": snapshot.growth_score,
        "quality_score": snapshot.quality_score,
        "valuation_score": snapshot.valuation_score,
        "momentum_score": snapshot.momentum_score,
        "risk_penalty": snapshot.risk_penalty,
        "risk_level": snapshot.risk_level,
        "score_category": snapshot.score_category,
        "valuation_basis": snapshot.valuation_basis,
        "data_coverage": snapshot.data_coverage,
        "risk_coverage": snapshot.risk_coverage,
        "market_cap": snapshot.market_cap,
        "market_cap_source": snapshot.market_cap_source,
        "volume_basis": snapshot.volume_basis,
        "revenue_growth_yoy": snapshot.revenue_growth_yoy,
        "revenue_growth_acceleration": snapshot.revenue_growth_acceleration,
        "enterprise_value": snapshot.enterprise_value,
        "freshness": snapshot.freshness,
        "coverage": snapshot.coverage,
        "ranking_state": snapshot.ranking_state,
        "exclusion_reasons": snapshot.exclusion_reasons,
        "rank_eligible": is_rank_eligible(status, freshness),
    }


def _feature_row(
    features: MarketFeatures,
    company: Company,
    *,
    run_id: int,
    snapshot_date: date,
    captured_at: datetime,
    benchmark_symbol: str | None,
) -> dict[str, Any]:
    """Return the column mapping for one captured feature set."""
    values = features.model_dump()
    values.pop("as_of")
    return {
        "run_id": run_id,
        "company_id": company.id,
        "ticker": company.ticker,
        "snapshot_date": snapshot_date,
        "captured_at": captured_at,
        "feature_version": FEATURE_VERSION,
        "feature_as_of": features.as_of,
        "benchmark_symbol": benchmark_symbol,
        **values,
    }


def _universe_rows(
    companies: Sequence[Company],
    session: Session,
    *,
    snapshot_date: date,
    captured_at: datetime,
) -> list[dict[str, Any]]:
    """Return the membership events this capture observed.

    A company with no recorded history gets a `BASELINE` row: the state as first
    seen, dated the day it was first seen and asserting nothing about when it
    became true. Thereafter only a *change* is written, as an `OBSERVED` row —
    so the log stays short and every dated event in it was genuinely witnessed.

    Nothing here guesses. A company that was already inactive on the first
    capture is recorded as an inactive baseline, never as a delisting on an
    invented date.
    """
    events = UniverseEventRepository(session)
    known = events.latest_state([company.id for company in companies])

    rows: list[dict[str, Any]] = []
    for company in companies:
        previous = known.get(company.id)
        if previous is None:
            kind, event_type = (
                OBSERVATION_BASELINE,
                (EVENT_ACTIVE if company.is_active else EVENT_DELISTED),
            )
        elif previous.is_active == company.is_active:
            continue
        else:
            kind = OBSERVATION_OBSERVED
            event_type = EVENT_REACTIVATED if company.is_active else EVENT_DELISTED
        rows.append(
            {
                "company_id": company.id,
                "event_fingerprint": _fingerprint(
                    event_type, kind, snapshot_date, company.is_active
                ),
                "ticker": company.ticker,
                "event_type": event_type,
                "observation_kind": kind,
                "effective_date": snapshot_date,
                "is_active": company.is_active,
                "exchange": company.exchange,
                "source": _SOURCE,
                "observed_at": captured_at,
            }
        )
    return rows


def _bars(rows: Sequence[PriceHistory | BenchmarkPrice]) -> list[PriceBar]:
    """Convert stored rows to domain bars.

    Takes either table's rows: a company's history and the benchmark's are the
    same shape, and `to_price_bar` reads both.
    """
    return [to_price_bar(row) for row in rows]


def capture_day(
    session: Session,
    settings: Settings,
    *,
    snapshot_date: date | None = None,
    score_version: str = CURRENT_SCORE_VERSION,
) -> CaptureResult:
    """Record what the system knows today, immutably.

    The whole pass runs in the caller's transaction and the run row reaches
    `COMPLETE` inside it, so a failure anywhere leaves no snapshot at all rather
    than a day that looks complete and is not.

    Args:
        session: Open database session. The caller commits.
        settings: Supplies the benchmark symbol.
        snapshot_date: The day to describe. Defaults to today in UTC, matching
            the convention `score_market` uses for `score_date`.
        score_version: The scoring rules to capture.

    Returns:
        What was written, or a result marked `already_captured` when the day was
        already recorded and nothing was done.

    Raises:
        ImmutableSnapshotError: If rows exist for the day without a completed
            run — a previous capture that was committed halfway by a caller not
            using one transaction. Refused rather than topped up.
    """
    as_of = snapshot_date or datetime.now(UTC).date()
    captured_at = datetime.now(UTC)

    runs = PitCaptureRunRepository(session)
    existing = runs.completed_run(as_of)
    if existing is not None:
        log.info("point-in-time capture already complete", snapshot_date=str(as_of))
        return CaptureResult(
            snapshot_date=as_of,
            captured_at=captured_at,
            already_captured=True,
            score_version=existing.score_version,
            feature_version=existing.feature_version,
        )

    run = runs.start(as_of, score_version=score_version, feature_version=FEATURE_VERSION)

    companies = CompanyRepository(session).list_all()
    by_id = {company.id: company for company in companies}

    # Scores are copied from the newest market-wide run at or before the
    # snapshot date. Reading a later run would import knowledge the day did not
    # have, which is the whole failure this package exists to prevent.
    scores_repo = ScoreSnapshotRepository(session)
    source_score_date = scores_repo.latest_score_date(score_version=score_version)
    score_rows: list[dict[str, Any]] = []
    if source_score_date is not None and source_score_date <= as_of:
        for snapshot in scores_repo.list_for_date(
            score_date=source_score_date, score_version=score_version
        ):
            company = by_id.get(snapshot.company_id)
            if company is None:
                continue
            score_rows.append(
                _score_row(
                    snapshot,
                    company,
                    run_id=run.id,
                    snapshot_date=as_of,
                    captured_at=captured_at,
                )
            )
    elif source_score_date is not None:
        log.warning(
            "newest scoring run postdates the snapshot date; no scores captured",
            snapshot_date=str(as_of),
            score_date=str(source_score_date),
        )

    prices = PriceHistoryRepository(session)
    benchmark_symbol = settings.benchmark_symbol
    benchmark = _bars(BenchmarkPriceRepository(session).list_bars(benchmark_symbol, until=as_of))
    if not benchmark:
        log.warning(
            "no benchmark history at or before the snapshot date; "
            "beta, correlation and relative strength will be null",
            symbol=benchmark_symbol,
            snapshot_date=str(as_of),
        )

    feature_rows: list[dict[str, Any]] = []
    for company in companies:
        bars = _bars(prices.list_for_company(company.id, until=as_of))
        features = compute_market_features(bars, benchmark_bars=benchmark)
        if features is None:
            continue
        feature_rows.append(
            _feature_row(
                features,
                company,
                run_id=run.id,
                snapshot_date=as_of,
                captured_at=captured_at,
                benchmark_symbol=benchmark_symbol if benchmark else None,
            )
        )

    universe_rows = _universe_rows(companies, session, snapshot_date=as_of, captured_at=captured_at)

    written_scores = PitScoreSnapshotRepository(session).add_all(score_rows)
    written_features = PitFeatureSnapshotRepository(session).add_all(feature_rows)
    written_events = UniverseEventRepository(session).add_all(universe_rows)

    runs.complete(
        run,
        scores=written_scores,
        features=written_features,
        universe_events=written_events,
    )

    log.info(
        "point-in-time capture complete",
        snapshot_date=str(as_of),
        scores=written_scores,
        features=written_features,
        universe_events=written_events,
    )
    return CaptureResult(
        snapshot_date=as_of,
        captured_at=captured_at,
        scores=written_scores,
        features=written_features,
        universe_events=written_events,
        companies_examined=len(companies),
        score_version=score_version,
        source_score_date=source_score_date,
    )


def record_capture_failure(
    session: Session,
    snapshot_date: date,
    detail: str,
    *,
    score_version: str = CURRENT_SCORE_VERSION,
) -> None:
    """Record that a capture failed, in a transaction of its own.

    Called after the data transaction has been rolled back, so the failure is
    visible without any snapshot surviving it.

    Args:
        session: A session distinct from the one that failed.
        snapshot_date: The day the capture was for.
        detail: What went wrong. Free text for an operator.
        score_version: The scoring rules the run was for.
    """
    PitCaptureRunRepository(session).fail(
        snapshot_date,
        detail=detail,
        score_version=score_version,
        feature_version=FEATURE_VERSION,
    )
