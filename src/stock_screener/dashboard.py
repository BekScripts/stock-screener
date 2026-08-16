"""Read models for the dashboard: one company, its metrics, its research.

Presentation only. Nothing here calculates a score, re-derives a ranking or
touches a research report — it reads what the pipeline already stored and shapes
it for a screen. The database stays the source of truth, so the dashboard and the
CLI can never disagree about what a company scored today.

Metrics are the one thing recomputed rather than read, because Phase 1 stores the
inputs rather than the derived figures. They are rebuilt from the same stored
periods and bars the scoring run used, bounded by the score's own date, so the
gross margin on the screen is the gross margin the score was given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from data_access import (
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ResearchReportRepository,
    ScoreSnapshotRepository,
    WatchlistRepository,
    to_company_profile,
    to_financial_period,
    to_price_bar,
)
from domain import CURRENT_SCORE_VERSION, build_company_metrics
from research import ResearchReport
from stock_screener.scoring import ScoreDetail, latest_score

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from data_access import Company
    from stock_screener.config import Settings

#: Metrics the detail page shows, in reading order, with how to render each.
#:
#: A deliberate subset. Every metric the engine derives is available through the
#: score breakdown; a page that listed all of them would bury the six that
#: actually answer "is this company compounding".
METRIC_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("revenue_growth_yoy", "Revenue growth (YoY)", "percent"),
    ("revenue_growth_acceleration", "Growth acceleration", "points"),
    ("revenue_cagr_3y", "Revenue CAGR (3y)", "percent"),
    ("gross_margin", "Gross margin", "percent"),
    ("operating_margin", "Operating margin", "percent"),
    ("fcf_margin", "Free cash flow margin", "percent"),
    ("net_cash", "Net cash", "money"),
    ("share_count_growth_yoy", "Share count growth (YoY)", "percent"),
    ("return_6m", "Return (6m)", "percent"),
    ("return_12m", "Return (12m)", "percent"),
    ("distance_from_52w_high", "Distance from 52-week high", "percent"),
)


@dataclass(frozen=True, slots=True)
class MetricView:
    """One metric as a screen shows it.

    Attributes:
        key: The metric's field name.
        label: What to print beside it.
        value: The figure, or None when the data cannot support it. None is
            rendered as unknown and never as zero.
        unit: How to format it — `percent`, `points` or `money`.
    """

    key: str
    label: str
    value: float | None
    unit: str


@dataclass(frozen=True, slots=True)
class StockDetail:
    """Everything the detail page needs about one company.

    Attributes:
        ticker: The company's symbol.
        name: Registered company name.
        sector: Sector classification, when known.
        industry: Industry classification, when known.
        exchange: Listing exchange, when known.
        market_cap: Market capitalisation at the time of scoring.
        market_cap_source: Whether a provider supplied it or it was multiplied
            out from filings and a price.
        ranking_state: `PRELIMINARY` until candidate enrichment has verified the
            market cap and consolidated liquidity, `FINAL` afterwards.
        watched: Whether this company is on the watchlist.
        score: The stored score and its full breakdown, or None when the company
            has never been scored — which is itself the answer to why it is
            missing from a ranking.
        metrics: The subset of derived metrics worth showing.
        has_research: Whether a validated research report exists to fetch.
    """

    ticker: str
    name: str
    sector: str | None
    industry: str | None
    exchange: str | None
    market_cap: float | None
    market_cap_source: str | None
    ranking_state: str | None
    watched: bool
    score: dict[str, Any] | None
    metrics: list[MetricView] = field(default_factory=list)
    has_research: bool = False


def stock_detail(
    session: Session,
    settings: Settings,
    ticker: str,
    *,
    score_version: str = CURRENT_SCORE_VERSION,
) -> StockDetail | None:
    """Assemble one company's detail view.

    Args:
        session: Open database session.
        settings: Supplies the liquidity basis metrics are derived under.
        ticker: The symbol to look up.
        score_version: The formula version whose score to show.

    Returns:
        The view, or None when no such company is stored. A company that exists
        but was never scored comes back with `score` as None rather than absent.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        return None

    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        company.id, score_version=score_version
    )
    detail = latest_score(session, company.ticker, score_version=score_version)
    report = ResearchReportRepository(session).latest_for_company(
        company.id, score_version=score_version
    )

    return StockDetail(
        ticker=company.ticker,
        name=company.name,
        sector=company.sector,
        industry=company.industry,
        exchange=company.exchange,
        market_cap=snapshot.market_cap if snapshot else company.market_cap,
        market_cap_source=snapshot.market_cap_source if snapshot else "UNKNOWN",
        ranking_state=snapshot.ranking_state if snapshot else None,
        watched=WatchlistRepository(session).get(company.id) is not None,
        score=_score_payload(detail),
        metrics=_metrics(session, settings, company),
        has_research=report is not None,
    )


def research_view(
    session: Session, ticker: str, *, score_version: str = CURRENT_SCORE_VERSION
) -> dict[str, Any] | None:
    """Return the latest validated research report for one company.

    Only what validation accepted is served. There is no path here to a draft:
    the stored row holds a `ResearchReport`, and nothing else can be persisted
    as one.

    Args:
        session: Open database session.
        ticker: The symbol to look up.
        score_version: The formula version whose report to read.

    Returns:
        The report as JSON-ready data, plus the section order a page should
        render, or None when the company has no report.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        return None

    row = ResearchReportRepository(session).latest_for_company(
        company.id, score_version=score_version
    )
    if row is None:
        return None

    report = ResearchReport.model_validate(row.report)
    payload = report.model_dump(mode="json")
    payload["sections"] = [
        {
            "key": section.value,
            "label": section.value.replace("_", " ").title(),
            "claims": [claim.model_dump(mode="json") for claim in claims],
        }
        for section, claims in report.sections.iter_sections()
    ]
    payload["generated_at"] = report.generated_at.isoformat()
    return payload


def watchlist_view(
    session: Session, *, score_version: str = CURRENT_SCORE_VERSION
) -> list[dict[str, Any]]:
    """Return the watchlist with each company's current score.

    The score is read rather than recomputed, and is None for a company that has
    since stopped being scored — a watched company that fell out of coverage is
    still watched, and saying so is more useful than dropping it from the list.

    Args:
        session: Open database session.
        score_version: The formula version to read scores under.

    Returns:
        One entry per watched company, most recently added first.
    """
    snapshots = ScoreSnapshotRepository(session)
    entries = []
    for entry in WatchlistRepository(session).list_all():
        company = entry.company
        snapshot = snapshots.latest_for_company(company.id, score_version=score_version)
        entries.append(
            {
                "ticker": company.ticker,
                "name": company.name,
                "sector": company.sector,
                "note": entry.note,
                "added_at": entry.added_at.isoformat(),
                "final_score": snapshot.final_score if snapshot else None,
                "score_category": snapshot.score_category if snapshot else None,
                "ranking_state": snapshot.ranking_state if snapshot else None,
                "scoring_status": snapshot.scoring_status if snapshot else None,
            }
        )
    return entries


def _score_payload(detail: ScoreDetail | None) -> dict[str, Any] | None:
    """Flatten a `ScoreDetail` for JSON, or return None when unscored."""
    if detail is None:
        return None

    return {
        "score_date": detail.score_date.isoformat(),
        "score_version": detail.score_version,
        "scoring_status": detail.scoring_status,
        "final_score": detail.final_score,
        "score_change_7d": detail.score_change_7d,
        "score_change_30d": detail.score_change_30d,
        "breakdown": detail.breakdown,
    }


def _metrics(session: Session, settings: Settings, company: Company) -> list[MetricView]:
    """Rebuild the displayed metrics from the same inputs the score used."""
    periods = [
        to_financial_period(row)
        for row in FinancialSnapshotRepository(session).list_for_company(company.id)
    ]
    bars = [
        to_price_bar(row) for row in PriceHistoryRepository(session).list_for_company(company.id)
    ]
    metrics = build_company_metrics(
        to_company_profile(company),
        periods,
        bars,
        bar_volume_basis=settings.bar_volume_basis,
    )
    return [
        MetricView(key=key, label=label, value=getattr(metrics, key), unit=unit)
        for key, label, unit in METRIC_FIELDS
    ]
