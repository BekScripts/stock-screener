"""The ranking views: what to research today, and what changed.

Four views over one score, not four scores. Hidden Gems and Great Company,
Wrong Price are filters over the same snapshots the main ranking reads — a
separate scoring system per view would mean four things to calibrate and four
ways to disagree about the same company.

Rows are returned as plain values rather than ORM objects. A command reads its
rankings inside a session and prints them after it has closed, and a detached
instance would raise there rather than here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from data_access import PRELIMINARY, CompanyRepository, ScoreSnapshotRepository
from domain import CURRENT_SCORE_VERSION, RiskLevel

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from data_access import Company, ScoreSnapshot

log = structlog.get_logger(__name__)

DEFAULT_RANKING_LIMIT = 50
"""Rows a ranking returns unless asked for more.

Fifty is the number the specification asks to be reviewed by hand, and a review
that does not happen is worth nothing.
"""

SHORT_CHANGE_WINDOW_DAYS = 7
LONG_CHANGE_WINDOW_DAYS = 30


@dataclass(frozen=True, slots=True)
class HiddenGemsCriteria:
    """What makes a company a hidden gem rather than merely a good one.

    A filter over the standard score, not a second score. The thresholds are
    the specification's, and they encode one idea: a strong company small enough
    that the market may not have finished looking at it.

    Attributes:
        max_market_cap: Upper bound on size.
        min_final_score: How good the company has to be.
        min_revenue_growth: Latest year-over-year growth required.
        excluded_risk_levels: Risk labels to leave out entirely.
    """

    max_market_cap: float = 5_000_000_000.0
    min_final_score: float = 70.0
    min_revenue_growth: float = 0.20
    excluded_risk_levels: tuple[RiskLevel, ...] = (RiskLevel.VERY_HIGH,)


@dataclass(frozen=True, slots=True)
class GreatCompanyCriteria:
    """A company worth owning at a price it is not currently offered at.

    Strong growth and strong quality with a weak valuation score. The point of
    the view is a watchlist: these are the companies to have an opinion about
    before the price moves, not the ones to buy today.

    Attributes:
        min_growth_score: Out of 35.
        min_quality_score: Out of 25.
        max_valuation_score: Out of 25 — an upper bound, because the view exists
            to find the expensive ones.
    """

    min_growth_score: float = 28.0
    min_quality_score: float = 20.0
    max_valuation_score: float = 10.0


@dataclass(frozen=True, slots=True)
class RankingRow:
    """One row of a ranking, flattened for display and export.

    Attributes:
        rank: Position in this view, starting at 1.
        ticker: The company's symbol.
        name: Registered company name.
        sector: Sector classification, when known.
        scoring_status: Always `SCORED` in a ranking; carried for the export.
        final_score: The risk-adjusted score.
        raw_score: Before risk penalties.
        growth_score: Out of 35.
        quality_score: Out of 25.
        valuation_score: Out of 25.
        momentum_score: Out of 15.
        risk_penalty: Zero or negative.
        risk_level: The risk label.
        score_category: The research-priority band.
        data_coverage: Share of scoring metrics available, 0-1.
        market_cap: Market capitalisation at the time of scoring.
        revenue_growth_yoy: Latest year-over-year revenue growth.
        revenue_growth_acceleration: In decimal percentage points.
        enterprise_value: None when debt or cash was unknown.
        valuation_basis: Which multiple valuation used.
        market_cap_source: Whether the market cap was supplied by a provider or
            multiplied out from filings and a price.
        volume_basis: What the liquidity figure behind the score represented.
        ranking_state: `PRELIMINARY` until the company has been through
            candidate enrichment, `FINAL` afterwards.
        score_change_7d: Against the nearest snapshot a week or more old.
        score_change_30d: Against the nearest snapshot a month or more old.
        score_change_window_days: The window `score_change_30d` actually covers.
            Thirty everywhere except `improving_fast`, which can be asked for a
            different one — a column named `30d` holding a 90-day change would
            otherwise be silently wrong.
        warnings: Caveats recorded with the score.
    """

    rank: int
    ticker: str
    name: str
    sector: str | None
    scoring_status: str

    final_score: float | None
    raw_score: float | None
    growth_score: float | None
    quality_score: float | None
    valuation_score: float | None
    momentum_score: float | None

    risk_penalty: float | None
    risk_level: str | None
    score_category: str | None
    data_coverage: float | None

    market_cap: float | None
    revenue_growth_yoy: float | None
    revenue_growth_acceleration: float | None
    enterprise_value: float | None
    valuation_basis: str | None
    market_cap_source: str | None = None
    volume_basis: str | None = None
    ranking_state: str = PRELIMINARY

    score_change_7d: float | None = None
    score_change_30d: float | None = None
    score_change_window_days: int = LONG_CHANGE_WINDOW_DAYS
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoreDetail:
    """One company's latest score with its full explanation.

    Attributes:
        ticker: The company's symbol.
        name: Registered company name.
        score_date: The day the score describes.
        score_version: The formula the score was produced by.
        scoring_status: Whether there is a number, and why not when there is not.
        final_score: The risk-adjusted score, None unless scored.
        score_change_7d: Against the nearest snapshot a week or more old.
        score_change_30d: Against the nearest snapshot a month or more old.
        breakdown: The complete `CompanyScore` as stored, component by
            component and metric by metric.
    """

    ticker: str
    name: str
    score_date: date
    score_version: str
    scoring_status: str
    final_score: float | None
    score_change_7d: float | None = None
    score_change_30d: float | None = None
    breakdown: dict[str, Any] = field(default_factory=dict)


def top_opportunities(
    session: Session,
    *,
    score_version: str = CURRENT_SCORE_VERSION,
    score_date: date | None = None,
    limit: int | None = DEFAULT_RANKING_LIMIT,
    min_score: float | None = None,
) -> list[RankingRow]:
    """Return the highest-scoring companies, best first.

    Args:
        session: Open database session.
        score_version: The formula version to read.
        score_date: The day to rank. Defaults to the most recent day stored.
        limit: Maximum rows. None returns the whole scored universe.
        min_score: Only companies at or above this final score.

    Returns:
        Ranked rows, empty when nothing has been scored for this version.
    """
    repository = ScoreSnapshotRepository(session)
    as_of = score_date or repository.latest_score_date(score_version=score_version)
    if as_of is None:
        return []

    pairs = repository.list_scored(
        score_version=score_version,
        score_date=as_of,
        min_final_score=min_score,
        limit=limit,
    )
    return _with_changes(repository, pairs, as_of, score_version)


def hidden_gems(
    session: Session,
    *,
    criteria: HiddenGemsCriteria | None = None,
    score_version: str = CURRENT_SCORE_VERSION,
    score_date: date | None = None,
    limit: int | None = DEFAULT_RANKING_LIMIT,
) -> list[RankingRow]:
    """Return small, fast-growing companies that already score well.

    Args:
        session: Open database session.
        criteria: The thresholds to apply. Defaults to the documented ones.
        score_version: The formula version to read.
        score_date: The day to rank. Defaults to the most recent day stored.
        limit: Maximum rows.

    Returns:
        Ranked rows, ordered by final score like every other view.
    """
    rules = criteria or HiddenGemsCriteria()
    repository = ScoreSnapshotRepository(session)
    as_of = score_date or repository.latest_score_date(score_version=score_version)
    if as_of is None:
        return []

    pairs = repository.list_scored(
        score_version=score_version,
        score_date=as_of,
        min_final_score=rules.min_final_score,
        max_market_cap=rules.max_market_cap,
        min_revenue_growth=rules.min_revenue_growth,
        exclude_risk_levels=[level.value for level in rules.excluded_risk_levels],
        limit=limit,
    )
    return _with_changes(repository, pairs, as_of, score_version)


def great_company_wrong_price(
    session: Session,
    *,
    criteria: GreatCompanyCriteria | None = None,
    score_version: str = CURRENT_SCORE_VERSION,
    score_date: date | None = None,
    limit: int | None = DEFAULT_RANKING_LIMIT,
) -> list[RankingRow]:
    """Return strong companies whose valuation score is poor.

    Args:
        session: Open database session.
        criteria: The thresholds to apply. Defaults to the documented ones.
        score_version: The formula version to read.
        score_date: The day to rank. Defaults to the most recent day stored.
        limit: Maximum rows.

    Returns:
        Ranked rows. A low final score is expected here — the valuation
        component is dragging it down, which is the point of the view.
    """
    rules = criteria or GreatCompanyCriteria()
    repository = ScoreSnapshotRepository(session)
    as_of = score_date or repository.latest_score_date(score_version=score_version)
    if as_of is None:
        return []

    pairs = repository.list_scored(
        score_version=score_version,
        score_date=as_of,
        min_growth_score=rules.min_growth_score,
        min_quality_score=rules.min_quality_score,
        max_valuation_score=rules.max_valuation_score,
        limit=limit,
    )
    return _with_changes(repository, pairs, as_of, score_version)


def improving_fast(
    session: Session,
    *,
    score_version: str = CURRENT_SCORE_VERSION,
    score_date: date | None = None,
    window_days: int = LONG_CHANGE_WINDOW_DAYS,
    min_change: float = 0.0,
    limit: int | None = DEFAULT_RANKING_LIMIT,
) -> list[RankingRow]:
    """Return companies whose score has risen most over the window.

    The comparison is against the nearest snapshot at or before the target date,
    not a snapshot exactly `window_days` old: score history has gaps — weekends,
    a failed run, a company that only became scoreable last month — and
    demanding an exact date would silently drop most of the market.

    Only snapshots from the same `score_version` are compared. Subtracting a v1
    score from a v2 one would report a change in the formula as a change in the
    business.

    Args:
        session: Open database session.
        score_version: The formula version to read.
        score_date: The day to rank from. Defaults to the most recent stored.
        window_days: How far back to compare.
        min_change: Minimum improvement to include, in score points.
        limit: Maximum rows.

    Returns:
        Rows ordered by improvement, largest first. Companies with no earlier
        snapshot are absent rather than shown as unchanged.
    """
    repository = ScoreSnapshotRepository(session)
    as_of = score_date or repository.latest_score_date(score_version=score_version)
    if as_of is None:
        return []

    pairs = repository.list_scored(score_version=score_version, score_date=as_of)
    rows = _with_changes(repository, pairs, as_of, score_version, long_window_days=window_days)

    improved = [
        row
        for row in rows
        if row.score_change_30d is not None and row.score_change_30d >= min_change
    ]
    improved.sort(
        key=lambda row: (-(row.score_change_30d or 0.0), -(row.final_score or 0.0), row.ticker)
    )
    ranked = improved[:limit] if limit is not None else improved
    return [_renumber(row, rank) for rank, row in enumerate(ranked, start=1)]


def latest_score(
    session: Session, ticker: str, *, score_version: str = CURRENT_SCORE_VERSION
) -> ScoreDetail | None:
    """Return one company's most recent score and its full breakdown.

    Args:
        session: Open database session.
        ticker: The symbol to look up.
        score_version: The formula version to read.

    Returns:
        The detail, or None when the company is unknown or has never been
        scored. A company with a status of `UNSUPPORTED_SECTOR` or
        `INSUFFICIENT_DATA` returns a detail with no number, which is the answer
        to "why is it not in the ranking".
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        return None

    repository = ScoreSnapshotRepository(session)
    snapshot = repository.latest_for_company(company.id, score_version=score_version)
    if snapshot is None:
        return None

    changes = _changes_for(repository, [company.id], snapshot.score_date, score_version)
    short, long = changes.get(company.id, (None, None))
    breakdown = snapshot.breakdown if isinstance(snapshot.breakdown, dict) else {}

    return ScoreDetail(
        ticker=company.ticker,
        name=company.name,
        score_date=snapshot.score_date,
        score_version=snapshot.score_version,
        scoring_status=snapshot.scoring_status,
        final_score=snapshot.final_score,
        score_change_7d=short,
        score_change_30d=long,
        breakdown=dict(breakdown),
    )


def _with_changes(
    repository: ScoreSnapshotRepository,
    pairs: Sequence[tuple[ScoreSnapshot, Company]],
    as_of: date,
    score_version: str,
    *,
    long_window_days: int = LONG_CHANGE_WINDOW_DAYS,
) -> list[RankingRow]:
    """Flatten snapshot/company pairs into rows, attaching score changes."""
    identifiers = [snapshot.company_id for snapshot, _ in pairs]
    changes = _changes_for(
        repository, identifiers, as_of, score_version, long_window_days=long_window_days
    )

    rows: list[RankingRow] = []
    for rank, (snapshot, company) in enumerate(pairs, start=1):
        short, long = changes.get(snapshot.company_id, (None, None))
        rows.append(_row(rank, snapshot, company, short, long, long_window_days))
    return rows


def _changes_for(
    repository: ScoreSnapshotRepository,
    company_ids: Sequence[int],
    as_of: date,
    score_version: str,
    *,
    long_window_days: int = LONG_CHANGE_WINDOW_DAYS,
) -> dict[int, tuple[float | None, float | None]]:
    """Return each company's score change over the short and long windows."""
    if not company_ids:
        return {}

    current = repository.snapshots_on_or_before(company_ids, as_of, score_version=score_version)
    earlier = {
        window: repository.snapshots_on_or_before(
            company_ids, as_of - timedelta(days=window), score_version=score_version
        )
        for window in (SHORT_CHANGE_WINDOW_DAYS, long_window_days)
    }

    changes: dict[int, tuple[float | None, float | None]] = {}
    for company_id in company_ids:
        today = current.get(company_id)
        if today is None or today.final_score is None:
            changes[company_id] = (None, None)
            continue
        changes[company_id] = (
            _difference(today.final_score, earlier[SHORT_CHANGE_WINDOW_DAYS].get(company_id)),
            _difference(today.final_score, earlier[long_window_days].get(company_id)),
        )
    return changes


def _difference(current: float, earlier: ScoreSnapshot | None) -> float | None:
    """Return the score change against an earlier snapshot, or None."""
    if earlier is None or earlier.final_score is None:
        return None
    return round(current - earlier.final_score, 2)


def _row(
    rank: int,
    snapshot: ScoreSnapshot,
    company: Company,
    score_change_7d: float | None,
    score_change_30d: float | None,
    score_change_window_days: int = LONG_CHANGE_WINDOW_DAYS,
) -> RankingRow:
    """Flatten one snapshot and its company into a ranking row."""
    breakdown = snapshot.breakdown if isinstance(snapshot.breakdown, dict) else {}
    stored_warnings = breakdown.get("warnings")
    warnings = stored_warnings if isinstance(stored_warnings, list) else []

    return RankingRow(
        rank=rank,
        ticker=company.ticker,
        name=company.name,
        sector=company.sector,
        scoring_status=snapshot.scoring_status,
        final_score=snapshot.final_score,
        raw_score=snapshot.raw_score,
        growth_score=snapshot.growth_score,
        quality_score=snapshot.quality_score,
        valuation_score=snapshot.valuation_score,
        momentum_score=snapshot.momentum_score,
        risk_penalty=snapshot.risk_penalty,
        risk_level=snapshot.risk_level,
        score_category=snapshot.score_category,
        data_coverage=snapshot.data_coverage,
        market_cap=snapshot.market_cap,
        revenue_growth_yoy=snapshot.revenue_growth_yoy,
        revenue_growth_acceleration=snapshot.revenue_growth_acceleration,
        enterprise_value=snapshot.enterprise_value,
        valuation_basis=snapshot.valuation_basis,
        market_cap_source=snapshot.market_cap_source,
        volume_basis=snapshot.volume_basis,
        ranking_state=snapshot.ranking_state,
        score_change_7d=score_change_7d,
        score_change_30d=score_change_30d,
        score_change_window_days=score_change_window_days,
        warnings=tuple(str(warning) for warning in warnings),
    )


def _renumber(row: RankingRow, rank: int) -> RankingRow:
    """Return the row with a new rank, after a re-sort changed the order."""
    return replace(row, rank=rank)
