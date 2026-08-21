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
    DeepResearchReportRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ResearchReportRepository,
    ScoreSnapshotRepository,
    StoredDeepResearchReport,
    WatchlistRepository,
    to_company_profile,
    to_financial_period,
    to_price_bar,
)
from deep_research import DeepResearchReport
from domain import CURRENT_SCORE_VERSION, build_company_metrics, normalise_currency
from research import ResearchReport
from stock_screener.scoring import ScoreDetail, latest_score

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from data_access import Company
    from domain import CompanyMetrics, FxConversion
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
        currency: For a `money` metric, the currency the figure is actually in.
            Not decoration: a foreign issuer's net cash is in the currency it
            files in, and prefixing every money figure with a dollar sign would
            state that TSM holds a trillion dollars when the number is Taiwan
            dollars. None for anything that is not money, where it has no
            meaning.
    """

    key: str
    label: str
    value: float | None
    unit: str
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class StockDetail:
    """Everything the detail page needs about one company.

    Attributes:
        ticker: The company's symbol.
        name: Registered company name.
        sector: Sector classification, when known.
        industry: Industry classification, when known.
        exchange: Listing exchange, when known.
        market_cap: Market capitalisation at the time of scoring, in
            `market_cap_currency` — the currency the *listing* trades in, which
            is what a reader expects to see for a U.S.-listed security. The
            converted figure the valuation ratios were computed from is
            deliberately not shown in its place; it is an internal quantity, not
            the quoted size of the company.
        market_cap_currency: What `market_cap` is denominated in. USD for every
            listing this screen admits.
        reporting_currency: What the company's statements — and therefore its
            money metrics — are denominated in.
        fx: The rate that brought the two together, with the date and source it
            came from, or None when none was needed or none was found.
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
    market_cap_currency: str
    reporting_currency: str
    fx: dict[str, Any] | None
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
    metrics = _company_metrics(session, settings, company)

    return StockDetail(
        ticker=company.ticker,
        name=company.name,
        sector=company.sector,
        industry=company.industry,
        exchange=company.exchange,
        market_cap=snapshot.market_cap if snapshot else company.market_cap,
        market_cap_currency=normalise_currency(company.quote_currency),
        reporting_currency=normalise_currency(metrics.reported_currency),
        fx=_fx_payload(metrics.fx),
        market_cap_source=snapshot.market_cap_source if snapshot else "UNKNOWN",
        ranking_state=snapshot.ranking_state if snapshot else None,
        watched=WatchlistRepository(session).get(company.id) is not None,
        score=_score_payload(detail),
        metrics=_metric_views(metrics),
        has_research=report is not None,
    )


def _fx_payload(conversion: FxConversion | None) -> dict[str, Any] | None:
    """Flatten a conversion for JSON, keeping the date and source visible."""
    if conversion is None:
        return None
    return {
        "base": conversion.base,
        "quote": conversion.quote,
        "rate": conversion.rate,
        "rate_date": conversion.rate_date.isoformat(),
        "provider": conversion.provider,
    }


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
        "exclusion_reasons": list(detail.exclusion_reasons),
        "final_score": detail.final_score,
        "score_change_7d": detail.score_change_7d,
        "score_change_30d": detail.score_change_30d,
        "breakdown": detail.breakdown,
    }


def _company_metrics(session: Session, settings: Settings, company: Company) -> CompanyMetrics:
    """Rebuild one company's metrics from the same inputs the score used.

    No exchange rate is supplied. This is a read path serving a page, so it must
    not reach a vendor; the money metrics it displays are reported figures and
    need no conversion to be shown correctly, as long as the screen says which
    currency they are in.
    """
    periods = [
        to_financial_period(row)
        for row in FinancialSnapshotRepository(session).list_for_company(company.id)
    ]
    bars = [
        to_price_bar(row) for row in PriceHistoryRepository(session).list_for_company(company.id)
    ]
    return build_company_metrics(
        to_company_profile(company),
        periods,
        bars,
        bar_volume_basis=settings.bar_volume_basis,
    )


def _metric_views(metrics: CompanyMetrics) -> list[MetricView]:
    """Turn the derived metrics into the subset a screen shows."""
    reporting = normalise_currency(metrics.reported_currency)
    return [
        MetricView(
            key=key,
            label=label,
            value=getattr(metrics, key),
            unit=unit,
            currency=reporting if unit == "money" else None,
        )
        for key, label, unit in METRIC_FIELDS
    ]


_SECTION_LABELS: dict[str, str] = {
    "company_overview": "Company Overview",
    "current_snapshot": "Current Snapshot",
    "why_the_algorithm_likes_it": "Why the Algorithm Likes It",
    "growth_quality": "Growth Quality",
    "financial_quality": "Financial Quality",
    "valuation": "Valuation",
    "latest_earnings": "Latest Earnings",
    "recent_developments": "Recent Developments",
    "competitive_position": "Competitive Position",
    "catalysts": "Catalysts",
    "major_risks": "Major Risks",
    "bull_case": "Bull Case",
    "bear_case": "Bear Case",
    "thesis_breakers": "Thesis Breakers",
    "what_the_market_may_be_missing": "What the Market May Be Missing",
    "what_to_watch_next": "What to Watch Next",
    "research_conclusion": "Research Conclusion",
}
"""Display names for the seventeen sections, in reading order.

Written out rather than derived from the enum by title-casing, because
`Why The Algorithm Likes It` and `What The Market May Be Missing` read as
machine output. A section added to the contract without a label here shows its
raw key, which is ugly enough to notice.
"""


def _deep_report_payload(row: StoredDeepResearchReport) -> dict[str, Any]:
    """Flatten one stored deep report into what a reading surface needs.

    **Only validated content leaves this function.** The stored document is a
    `DeepResearchReport`, which is the type validation produces and nothing else
    does, so there is no path here to a draft, a rejected claim's text, a prompt
    or a provider's raw output. Issues are served as codes and section names
    only — enough to see that something was dropped, never enough to read what.

    Args:
        row: The stored report.

    Returns:
        JSON-ready data, including the section order a page should render and
        the reason behind every `UNKNOWN` section.
    """
    report = DeepResearchReport.model_validate(row.validated_report_json)
    reasons = report.unknown_reasons

    return {
        "id": row.id,
        "ticker": report.ticker,
        "status": report.status.value,
        "as_of": report.as_of.isoformat(),
        "generated_at": report.generated_at.isoformat(),
        "score_version": report.score_version,
        "contract_version": report.contract_version,
        "prompt_version": report.prompt_version,
        "model_id": report.model_id,
        "confidence": report.confidence.model_dump(mode="json"),
        "unknowns": list(report.unknowns),
        "unknown_reasons": {section: reason.value for section, reason in reasons.items()},
        "external_state": row.external_state,
        "external_collected_at": (
            row.external_collected_at.isoformat() if row.external_collected_at else None
        ),
        "sections": [
            {
                "key": section.value,
                "label": _SECTION_LABELS.get(section.value, section.value),
                "unknown_reason": reasons.get(section.value),
                "claims": [
                    {
                        "text": claim.text,
                        "basis": claim.basis.value,
                        "evidence": list(claim.evidence),
                        "unknown_reason": (
                            claim.unknown_reason.value if claim.unknown_reason else None
                        ),
                    }
                    for claim in claims
                ],
            }
            for section, claims in report.sections.iter_sections()
        ],
        "sources": [
            {
                "evidence_id": item.evidence_id,
                "source_type": item.source_type.value,
                "tier": item.tier.value,
                "publisher": item.publisher,
                "title": item.title,
                "url": item.url,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "retrieved_at": item.retrieved_at.isoformat(),
            }
            for item in report.external_evidence
        ],
        # Codes and locations only. The text a claim was rejected for saying is
        # exactly what validation refused to publish, and serving it here would
        # undo the refusal.
        "issues": [
            {
                "code": issue.code.value,
                "section": issue.section.value if issue.section else None,
            }
            for issue in report.issues
        ],
    }


def deep_research_view(session: Session, ticker: str) -> dict[str, Any] | None:
    """Return a company's most recent validated deep research report.

    Args:
        session: Open database session.
        ticker: The symbol to look up.

    Returns:
        The report as JSON-ready data, or None when the company is unknown or
        has never been researched.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        return None

    row = DeepResearchReportRepository(session).latest_for_company(company.id)
    return None if row is None else _deep_report_payload(row)


def deep_research_report(session: Session, report_id: int) -> dict[str, Any] | None:
    """Return one historical deep research report by id.

    Args:
        session: Open database session.
        report_id: The stored report to read.

    Returns:
        The report, or None when no such report exists.
    """
    row = session.get(StoredDeepResearchReport, report_id)
    return None if row is None else _deep_report_payload(row)


def deep_research_history(
    session: Session, ticker: str, *, limit: int = 20
) -> list[dict[str, Any]] | None:
    """Return a company's deep research reports, newest first.

    Summaries rather than whole documents: a history control needs enough to
    label a row and nothing more, and returning twenty full reports to render a
    dropdown would be wasteful.

    Args:
        session: Open database session.
        ticker: The symbol to look up.
        limit: Most reports to return.

    Returns:
        One summary per report, newest first, or None when the company is
        unknown. An empty list means the company exists and has never been
        researched.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        return None

    return [
        {
            "id": row.id,
            "generated_at": row.generated_at.isoformat(),
            "as_of": row.as_of.isoformat(),
            "status": row.status,
            "confidence": row.confidence,
            "model_id": row.model_id,
            "prompt_version": row.prompt_version,
            "external_state": row.external_state,
            "external_collected_at": (
                row.external_collected_at.isoformat() if row.external_collected_at else None
            ),
        }
        for row in DeepResearchReportRepository(session).history_for_company(
            company.id, limit=limit
        )
    ]
