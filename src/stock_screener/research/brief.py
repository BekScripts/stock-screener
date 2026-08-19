"""Turning stored rows into the evidence a model is allowed to reason from.

Everything a `ResearchBrief` contains already exists in the database. Nothing
here calls a provider, and nothing here scores anything: the CompounderScore is
read back from the snapshot that recorded it, and both it and the metrics arrive
as evidence rather than as something to revise.

Three rules shape the assembly.

**The score's date is the data boundary.** A brief explains one snapshot, so it
is built from what was knowable on that snapshot's day: price bars up to
`score_date`, reporting periods ending on or before it, filings submitted by it,
and earlier scores of the same version. Without that boundary a brief assembled a
week later would explain an 81 using this week's numbers, and every discrepancy
between the two would look like the model's error.

**Where the score preserved a figure, the score's figure wins.** The stored
breakdown carries the value each sub-score was calculated from. Those values are
used verbatim rather than recomputed, so the metric a claim cites and the
sub-score it explains are the same number. Every other metric is recalculated
from the bounded inputs by the unmodified Phase 1 engine, and says so through its
`source`.

**Filing references are metadata.** They come from the stored filing index, which
the nightly pass fills from EDGAR's submissions document. No document is fetched
here — `excerpt` stays None for this contract version, and a section that needs
filing text answers `UNKNOWN`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from data_access import (
    CompanyRepository,
    FilingExcerptRepository,
    FilingRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ScoreSnapshotRepository,
    to_company_profile,
    to_filing,
    to_financial_period,
    to_price_bar,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyScore,
    MarketCapSource,
    MetricUnit,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    ValuationBasis,
    VolumeBasis,
    build_company_metrics,
)
from research import (
    EnrichmentFacts,
    EvidenceKind,
    FilingReference,
    FilingText,
    MetricFact,
    RankingState,
    ReportedPeriod,
    ResearchBrief,
    ScoreEvidence,
    ScoreItem,
    ScorePoint,
    SelectionReason,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from data_access import (
        Company,
        FilingExcerptRecord,
        FilingRecord,
        FinancialSnapshot,
        ScoreSnapshot,
    )
    from domain import CompanyMetrics
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

DEFAULT_QUARTERS = 8
"""Reporting periods supplied per brief, oldest first.

Two years is enough to see a trend and to check the score's own account of one,
and short enough that the evidence stays readable.
"""

DEFAULT_HISTORY = 8
"""Earlier score points supplied per brief."""

DEFAULT_FILINGS = 8
"""Filing references supplied per brief, newest first."""

MAX_EXCERPTS = 5
"""Extracted sections supplied per brief.

Five reaches back across roughly a quarter of filings — the latest periodic
report's discussion plus the 8-Ks since — which is what the three filing-dependent
sections need. More would mostly repeat risk factors that change once a year.
"""

MAX_EXCERPT_CHARS = 1200
"""Characters of any one extracted section.

Two or three paragraphs: enough to state what a company does or what it
announced, and short enough that five of them do not dominate a brief. Stored
excerpts run longer, and are cut here rather than at extraction time so the bound
can change without re-reading a single filing.
"""


def _heading(pattern: str) -> re.Pattern[str]:
    """Compile an anchor that only matches the phrase used as a heading.

    A heading ends its own block, which survives extraction as a line break, a
    run of spaces, or the first figure of the table beneath it. The same words
    inside a sentence — "may harm our business, results of operations, and
    financial condition" — run straight on into a comma, and starting an excerpt
    there drops the reader mid-clause into a paragraph about something else.

    What follows the phrase is the discriminator, so what precedes it is only
    required not to be the middle of a word.
    """
    return re.compile(r"(?:^|\s)(" + pattern + r")(?=\s{2,}|\n|\$)", re.IGNORECASE | re.M)


_MDA_ANCHORS = (
    _heading(r"(?:company|business)\s+overview"),
    _heading(r"overview"),
    _heading(r"results\s+of\s+operations"),
    _heading(r"executive\s+overview"),
)
"""Headings that mark where an MD&A stops being preamble, best first.

Every MD&A opens with the same page of throat-clearing: read this with the
audited statements, the following contains forward-looking statements, actual
results may differ. Dell's runs past a thousand characters, which is the whole
excerpt budget — the model received the section's letterhead and answered
`company_summary` with UNKNOWN while the description of the business sat just
past the cut.

So the window starts at the first of these headings that the section contains.
Nothing is summarised or reordered: the excerpt is still one verbatim run of the
filer's own text, taken from a better place to start.

Each is matched as a heading rather than as a phrase. `results of operations`
appears in the risk language of most MD&As long before the section of that name,
and anchoring on the prose copy put ACTG's window in the middle of a sentence
about acquisition risk instead of on the table that explains its quarter.
"""


_SECTION_RANK = {"business": 0, "mda": 1, "risk_factors": 2}
"""How useful a section is to a research brief, lowest first.

Date order alone had a failure mode worth naming: a filer with a busy month of
8-Ks filled every slot with governance minutiae while the MD&A explaining the
quarter sat unread in the database. Durable context earns its place first —
what the company does, what management says about the period, what it says could
go wrong — and 8-K items take the slots that remain, which on a five-excerpt
brief is still most of them.
"""

_EIGHT_K_RANK = len(_SECTION_RANK)
"""Where an 8-K item sorts: after the periodic sections, before nothing."""

MAX_EXCERPT_TOTAL_CHARS = 6000
"""Total filing text in one brief, across all excerpts.

The binding limit. A rendered brief was about 10,800 characters before
extraction — roughly 7,800 prompt tokens — so 6,000 characters adds around a
quarter to the input of a call that costs about eight cents, and leaves the
8,000-token output ceiling untouched. Sections are taken newest-first until this
is reached, so a company with a long 10-K spends its budget on the 10-K and a
quiet company spends it on 8-Ks.
"""

SCORE_SOURCE = "score_snapshot"
"""Marks a fact taken verbatim from the stored breakdown, at score time."""

DERIVED_SOURCE = "derived"
"""Marks a fact recalculated from inputs bounded by the score's own date."""

_COMPONENT_LABELS = {
    "growth": "Growth",
    "quality": "Financial quality",
    "valuation": "Valuation",
    "momentum": "Market confirmation",
}

#: Every metric the brief carries, with the unit it must be read in.
#:
#: The unit is load-bearing rather than decorative: the contract's numeric check
#: refuses a figure written as a percentage unless it came from a `PERCENT`
#: metric, which is what stops `0.17` of acceleration being restated as `17%` of
#: growth. Listed explicitly rather than reflected off `CompanyMetrics` so that
#: adding a field there is a deliberate decision here, not an automatic one.
_METRIC_FACTS: tuple[tuple[str, str, MetricUnit], ...] = (
    ("price", "Latest close", MetricUnit.MONEY),
    ("market_cap", "Market capitalisation", MetricUnit.MONEY),
    ("calculated_market_cap", "Market capitalisation, calculated", MetricUnit.MONEY),
    ("market_cap_discrepancy", "Market capitalisation discrepancy", MetricUnit.PERCENT),
    ("average_dollar_volume_20d", "Average daily dollar volume, 20d", MetricUnit.MONEY),
    ("trading_days_used", "Trading days behind the liquidity figure", MetricUnit.COUNT),
    ("revenue_growth_yoy", "Revenue growth, year over year", MetricUnit.PERCENT),
    ("previous_revenue_growth_yoy", "Revenue growth, prior quarter", MetricUnit.PERCENT),
    ("revenue_growth_acceleration", "Revenue growth acceleration", MetricUnit.POINTS),
    ("ttm_revenue", "Revenue, trailing twelve months", MetricUnit.MONEY),
    ("ttm_revenue_growth", "Revenue growth, trailing twelve months", MetricUnit.PERCENT),
    ("revenue_cagr_3y", "Revenue CAGR, three years", MetricUnit.PERCENT),
    ("gross_margin", "Gross margin", MetricUnit.PERCENT),
    ("gross_margin_change", "Gross margin change", MetricUnit.POINTS),
    ("gross_profit_growth_yoy", "Gross profit growth, year over year", MetricUnit.PERCENT),
    ("operating_margin", "Operating margin", MetricUnit.PERCENT),
    ("operating_margin_change", "Operating margin change", MetricUnit.POINTS),
    ("fcf_margin", "Free cash flow margin", MetricUnit.PERCENT),
    ("fcf_margin_change", "Free cash flow margin change", MetricUnit.POINTS),
    ("ttm_free_cash_flow", "Free cash flow, trailing twelve months", MetricUnit.MONEY),
    ("cash", "Cash and equivalents", MetricUnit.MONEY),
    ("debt", "Total debt", MetricUnit.MONEY),
    ("net_cash", "Net cash", MetricUnit.MONEY),
    ("enterprise_value", "Enterprise value", MetricUnit.MONEY),
    ("share_count_growth_yoy", "Share count growth, year over year", MetricUnit.PERCENT),
    ("return_6m", "Price return, six months", MetricUnit.PERCENT),
    ("return_12m", "Price return, twelve months", MetricUnit.PERCENT),
    ("high_52w", "52-week high", MetricUnit.MONEY),
    ("low_52w", "52-week low", MetricUnit.MONEY),
    ("distance_from_52w_high", "Distance from the 52-week high", MetricUnit.PERCENT),
)

#: Sub-scores whose `observed` value **is** a metric the brief also carries.
#:
#: Keyed `component.subscore`, valued by the `CompanyMetrics` field it preserves.
#: These are the figures the score was actually calculated from, so the brief
#: quotes them rather than recomputing something a shade different — otherwise a
#: claim citing `M.gross_margin` and the sub-score it explains would state two
#: different numbers for the same thing.
#:
#: Sub-scores whose `observed` is derived rather than stored — the net-cash
#: ratio, the valuation multiple, relative strength, the persistence count — are
#: deliberately absent. They stay citable as `S.*` items; they simply have no
#: `M.*` twin to keep in step. A test asserts every name here still matches what
#: the scoring engine emits, so a renamed sub-score fails loudly rather than
#: silently reverting a metric to the recomputed value.
_PRESERVED_SUBSCORES: dict[str, str] = {
    "growth.revenue_growth": "revenue_growth_yoy",
    "growth.growth_acceleration": "revenue_growth_acceleration",
    "growth.revenue_cagr_3y": "revenue_cagr_3y",
    "growth.gross_profit_growth": "gross_profit_growth_yoy",
    "quality.gross_margin": "gross_margin",
    "quality.fcf_margin": "fcf_margin",
    "quality.operating_margin_trend": "operating_margin_change",
    "momentum.position_52w": "distance_from_52w_high",
}

#: Columns the score snapshot copied off the metrics at scoring time, and the
#: metric field each preserves. Used where no sub-score carries the figure:
#: market capitalisation and enterprise value are inputs to the valuation
#: component rather than observations of it.
_PRESERVED_COLUMNS: dict[str, str] = {
    "market_cap": "market_cap",
    "enterprise_value": "enterprise_value",
    "revenue_growth_yoy": "revenue_growth_yoy",
    "revenue_growth_acceleration": "revenue_growth_acceleration",
}


def assemble_brief(
    session: Session,
    settings: Settings,
    ticker: str,
    *,
    selection: SelectionReason = SelectionReason.TOP_RANKED,
    score_version: str = CURRENT_SCORE_VERSION,
    quarters: int = DEFAULT_QUARTERS,
) -> ResearchBrief | None:
    """Assemble one company's research brief from stored data.

    Args:
        session: Open database session.
        settings: Supplies the volume basis the metric engine needs, and the
            provider name recorded on enrichment.
        ticker: The company to describe.
        selection: Why this company qualified for research.
        score_version: The formula version whose score the brief explains.
        quarters: Reporting periods to supply.

    Returns:
        The brief, or None when the company is unknown, has never been scored
        under this version, or stored a breakdown that can no longer be read.
    """
    briefs = assemble_briefs(
        session,
        settings,
        [(ticker, selection)],
        score_version=score_version,
        quarters=quarters,
    )
    return briefs[0] if briefs else None


def assemble_briefs(
    session: Session,
    settings: Settings,
    requests: Sequence[tuple[str, SelectionReason]],
    *,
    score_version: str = CURRENT_SCORE_VERSION,
    quarters: int = DEFAULT_QUARTERS,
) -> list[ResearchBrief]:
    """Assemble briefs for several companies from stored data.

    Args:
        session: Open database session.
        settings: Supplies the volume basis and the provider name.
        requests: Ticker and selection reason per company.
        score_version: The formula version whose scores the briefs explain.
        quarters: Reporting periods to supply per brief.

    Returns:
        One brief per company that could be assembled, in request order.
        Companies that could not be are logged and left out rather than
        represented by a brief with holes in it.
    """
    if not requests:
        return []

    companies = CompanyRepository(session)
    snapshots = ScoreSnapshotRepository(session)
    briefs: list[ResearchBrief] = []

    for ticker, selection in requests:
        company = companies.get_by_ticker(ticker)
        if company is None:
            log.warning("brief skipped: unknown company", ticker=ticker)
            continue

        snapshot = snapshots.latest_for_company(company.id, score_version=score_version)
        if snapshot is None:
            log.warning("brief skipped: never scored", ticker=ticker, score_version=score_version)
            continue

        score = parse_score_breakdown(snapshot)
        if score is None:
            continue

        briefs.append(_build(session, settings, company, snapshot, score, selection, quarters))

    return briefs


@dataclass(frozen=True, slots=True)
class DeterministicEvidence:
    """One company's stored evidence, bounded by the score date it explains.

    Everything a brief may rest on that this system calculated or the company
    filed — assembled once, from stored rows, with no network access. Extracted
    from `_build` so that a second kind of brief can be built from exactly the
    same evidence rather than from a second implementation of it: two assemblers
    would be two things to keep in agreement, and the one that drifted would
    quietly disagree with the score it claimed to explain.

    Attributes:
        as_of: The score date bounding every field here.
        score: The stored breakdown, read-only.
        facts: Derived metrics, preferring the figures the score recorded.
        quarters: Reported periods, oldest first.
        score_history: Earlier scores under the same version.
        filings: Filing metadata, newest first.
        excerpts: Extracted filing text.
        enrichment: Vendor-supplied fields, when the metered pass reached it.
        metrics: The recomputed metric set the facts were drawn from. Carried so
            a caller needing a figure the brief does not expose — a freshness
            date, a liquidity basis — does not have to rebuild it.
    """

    as_of: date
    score: ScoreEvidence
    facts: tuple[MetricFact, ...]
    quarters: tuple[ReportedPeriod, ...]
    score_history: tuple[ScorePoint, ...]
    filings: tuple[FilingReference, ...]
    excerpts: tuple[FilingText, ...]
    enrichment: EnrichmentFacts | None
    metrics: CompanyMetrics


def assemble_deterministic_evidence(
    session: Session,
    settings: Settings,
    company: Company,
    snapshot: ScoreSnapshot,
    score: CompanyScore,
    *,
    quarters: int = DEFAULT_QUARTERS,
) -> DeterministicEvidence:
    """Read one company's stored evidence, bounded by its score date.

    The shared half of every brief this application builds. Performs no network
    access whatsoever: everything it returns came out of the database, which is
    the property that keeps brief assembly reproducible and testable.

    Args:
        session: Open database session.
        settings: Supplies the volume basis the metric engine needs, and the
            provider name recorded on enrichment.
        company: The company row.
        snapshot: The score snapshot the evidence is bounded by.
        score: The parsed breakdown from that snapshot.
        quarters: Reporting periods to supply.

    Returns:
        The evidence, every field bounded by `snapshot.score_date`.
    """
    as_of = snapshot.score_date
    periods = FinancialSnapshotRepository(session)
    prices = PriceHistoryRepository(session)
    filings = FilingRepository(session)
    excerpts = FilingExcerptRepository(session)
    snapshots = ScoreSnapshotRepository(session)

    stored_periods = [
        row for row in periods.list_for_company(company.id) if row.period_end <= as_of
    ]
    bars = [to_price_bar(row) for row in prices.list_for_company(company.id, until=as_of)]
    metrics = build_company_metrics(
        to_company_profile(company),
        [to_financial_period(row) for row in stored_periods],
        bars,
        bar_volume_basis=settings.bar_volume_basis,
    )

    return DeterministicEvidence(
        as_of=as_of,
        score=_score_evidence(snapshot, score),
        facts=_metric_facts(metrics, _preserved(snapshot, score)),
        quarters=_quarters(stored_periods, quarters),
        score_history=_history(
            snapshots.history_for_company(
                company.id, score_version=snapshot.score_version, limit=DEFAULT_HISTORY + 1
            ),
            as_of,
        ),
        filings=_filings(filings.list_for_company(company.id, until=as_of, limit=DEFAULT_FILINGS)),
        excerpts=_excerpts(excerpts.list_for_company(company.id, until=as_of)),
        enrichment=_enrichment(snapshot, metrics, settings),
        metrics=metrics,
    )


def _build(
    session: Session,
    settings: Settings,
    company: Company,
    snapshot: ScoreSnapshot,
    score: CompanyScore,
    selection: SelectionReason,
    quarters: int,
) -> ResearchBrief:
    """Assemble one brief, with every input bounded by the score's own date."""
    evidence = assemble_deterministic_evidence(
        session, settings, company, snapshot, score, quarters=quarters
    )

    return ResearchBrief(
        ticker=company.ticker,
        name=company.name,
        sector=company.sector,
        industry=company.industry,
        exchange=company.exchange,
        as_of=evidence.as_of,
        selection=selection,
        score=evidence.score,
        facts=evidence.facts,
        quarters=evidence.quarters,
        score_history=evidence.score_history,
        filings=evidence.filings,
        excerpts=evidence.excerpts,
        enrichment=evidence.enrichment,
    )


def _score_evidence(snapshot: ScoreSnapshot, score: CompanyScore) -> ScoreEvidence:
    """Rebuild the stored breakdown as read-only evidence."""
    return ScoreEvidence(
        score_version=snapshot.score_version,
        score_date=snapshot.score_date,
        scoring_status=ScoringStatus(snapshot.scoring_status),
        final_score=snapshot.final_score,
        raw_score=snapshot.raw_score,
        risk_penalty=snapshot.risk_penalty,
        risk_level=RiskLevel(snapshot.risk_level) if snapshot.risk_level else None,
        category=ScoreCategory(snapshot.score_category) if snapshot.score_category else None,
        data_coverage=snapshot.data_coverage,
        ranking_state=RankingState(snapshot.ranking_state),
        valuation_basis=_enum(
            ValuationBasis, snapshot.valuation_basis, ValuationBasis.NOT_AVAILABLE
        ),
        market_cap_source=_enum(
            MarketCapSource, snapshot.market_cap_source, MarketCapSource.UNKNOWN
        ),
        liquidity_basis=_enum(VolumeBasis, snapshot.volume_basis, VolumeBasis.UNKNOWN),
        items=_score_items(score),
        warnings=tuple(ScoreWarning(warning) for warning in score.warnings),
    )


def parse_score_breakdown(snapshot: ScoreSnapshot) -> CompanyScore | None:
    """Return the stored breakdown as a `CompanyScore`, or None when unreadable.

    An unreadable breakdown is a corrupt row rather than a missing one, and it
    stops this company's brief without stopping the run — the same treatment a
    provider failure gets everywhere else in the codebase.
    """
    if not isinstance(snapshot.breakdown, dict):
        log.warning("breakdown missing", company_id=snapshot.company_id)
        return None
    try:
        return CompanyScore.model_validate(snapshot.breakdown)
    except ValidationError:
        log.warning(
            "breakdown could not be read under the current models",
            company_id=snapshot.company_id,
            score_version=snapshot.score_version,
        )
        return None


def _score_items(score: CompanyScore) -> tuple[ScoreItem, ...]:
    """Flatten a score into addressable lines, unavailable ones included.

    A sub-score with no points is supplied rather than filtered out. The report
    should be able to say what the score could not judge, and it cannot do that
    if the gaps never reach it.
    """
    items: list[ScoreItem] = []

    for component in score.components:
        label = _COMPONENT_LABELS.get(component.name, component.name.capitalize())
        items.append(
            ScoreItem(
                id=f"S.{component.name}",
                label=f"{label} component",
                points=component.score,
                max_points=component.max_points,
            )
        )
        items.extend(
            ScoreItem(
                id=f"S.{component.name}.{sub.name}",
                label=f"{label} - {_humanise(sub.name)}",
                points=sub.points,
                max_points=sub.max_points,
                observed=sub.observed,
                unit=sub.unit,
                note=sub.note,
            )
            for sub in component.subscores
        )

    risk = score.risk
    if risk is not None:
        items.extend(
            (
                ScoreItem(
                    id="S.risk.dilution",
                    label="Risk - dilution",
                    points=risk.dilution_penalty,
                    observed=risk.share_count_growth_yoy,
                    unit=MetricUnit.PERCENT,
                ),
                ScoreItem(
                    id="S.risk.cash_runway",
                    label="Risk - cash runway",
                    points=risk.runway_penalty,
                    observed=risk.cash_runway_months,
                    unit=MetricUnit.MONTHS,
                ),
                ScoreItem(
                    id="S.risk.leverage",
                    label="Risk - leverage",
                    points=risk.balance_sheet_penalty,
                    observed=risk.net_debt_to_market_cap,
                    unit=MetricUnit.PERCENT,
                ),
            )
        )

    return tuple(items)


def _preserved(snapshot: ScoreSnapshot, score: CompanyScore) -> dict[str, float | None]:
    """Return the metric values the score itself recorded, by metric field.

    A value of None is preserved as deliberately as a number: it means the score
    could not use that metric, and a brief that filled the gap with a freshly
    calculated figure would be explaining a sub-score that earned nothing with a
    number that exists.
    """
    preserved: dict[str, float | None] = {}

    for component in score.components:
        for sub in component.subscores:
            field = _PRESERVED_SUBSCORES.get(f"{component.name}.{sub.name}")
            if field is not None:
                preserved[field] = sub.observed

    if score.risk is not None:
        preserved["share_count_growth_yoy"] = score.risk.share_count_growth_yoy

    for column, field in _PRESERVED_COLUMNS.items():
        preserved.setdefault(field, getattr(snapshot, column))

    return preserved


def _metric_facts(
    metrics: CompanyMetrics, preserved: Mapping[str, float | None]
) -> tuple[MetricFact, ...]:
    """Flatten the metric engine's output into citable facts.

    Every mapped metric appears, including the ones that are None. An absent
    fact and an unknown one look identical to a reader; only one of them is
    honest about a company whose data does not reach.
    """
    facts: list[MetricFact] = []
    for field, label, unit in _METRIC_FACTS:
        if field in preserved:
            value, source = preserved[field], SCORE_SOURCE
        else:
            value, source = getattr(metrics, field), DERIVED_SOURCE
        facts.append(
            MetricFact(
                id=f"M.{field}",
                label=label,
                value=float(value) if value is not None else None,
                unit=unit,
                source=source,
            )
        )
    return tuple(facts)


def _quarters(rows: Sequence[FinancialSnapshot], limit: int) -> tuple[ReportedPeriod, ...]:
    """Return the most recent reporting periods, oldest first.

    The rows arrive oldest first and already bounded by the score's date, so the
    newest are taken from the end and the order is preserved. Oldest-first is
    what makes a trend readable in the prompt without the model reversing it.
    """
    recent = list(rows)[-limit:] if limit > 0 else []
    return tuple(
        ReportedPeriod(
            id=f"F.{period.period_end.isoformat()}",
            period_end=period.period_end,
            revenue=period.revenue,
            gross_profit=period.gross_profit,
            operating_income=period.operating_income,
            free_cash_flow=period.free_cash_flow,
            cash=period.cash,
            total_debt=period.total_debt,
            shares_outstanding=period.shares_outstanding,
            gross_profit_basis=period.gross_profit_basis,
            source=period.source,
        )
        for period in (to_financial_period(row) for row in recent)
    )


def _history(rows: Sequence[ScoreSnapshot], as_of: date) -> tuple[ScorePoint, ...]:
    """Return earlier scores of the same version, oldest first.

    The repository has already filtered to one `score_version`. Anything on or
    after the day the brief describes is dropped here: today's number is not a
    movement, and a later one did not exist yet.
    """
    earlier = [row for row in rows if row.score_date < as_of]
    earlier.sort(key=lambda row: row.score_date)
    return tuple(
        ScorePoint(
            id=f"S.history.{row.score_date.isoformat()}",
            score_date=row.score_date,
            final_score=row.final_score,
        )
        for row in earlier[-DEFAULT_HISTORY:]
    )


def _filings(rows: Sequence[FilingRecord]) -> tuple[FilingReference, ...]:
    """Return stored filing index entries as citable references, newest first.

    Metadata only, by construction: what a filing *says* travels separately, as
    `FilingText` under an `X.` id, so a claim can never rest on the fact that a
    document exists while sounding like it rests on the document.
    """
    return tuple(
        FilingReference(
            id=f"D.{filing.accession}",
            form=filing.form,
            filed=filing.filed,
            period_end=filing.period_end,
            accession=filing.accession,
            url=filing.url,
        )
        for filing in (to_filing(row) for row in rows)
    )


def _excerpts(rows: Sequence[FilingExcerptRecord]) -> tuple[FilingText, ...]:
    """Return stored filing text as citable evidence, newest filing first.

    Ordered by what a section is before when it was filed: business, then MD&A,
    then risk factors, then 8-K items, each group newest first. A recent 8-K is
    still read — it simply cannot push out the description of the business.

    Three bounds apply, in this order: each excerpt is cut to
    `MAX_EXCERPT_CHARS`, no more than `MAX_EXCERPTS` are taken, and the running
    total stops at `MAX_EXCERPT_TOTAL_CHARS`.

    Reading stops at the first excerpt that would breach the total rather than
    skipping it for a shorter one further down: keeping the chosen order intact
    matters more than filling the budget exactly, and a rule that reorders
    evidence by length is a rule nobody can predict the output of.
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            _SECTION_RANK.get(row.section, _EIGHT_K_RANK),
            -row.filed.toordinal(),
            row.accession,
            row.section,
        ),
    )

    taken: list[FilingText] = []
    used = 0
    for row in ordered:
        if len(taken) >= MAX_EXCERPTS:
            break
        text = _clip(row.text[_excerpt_start(row) :], MAX_EXCERPT_CHARS)
        if used + len(text) > MAX_EXCERPT_TOTAL_CHARS:
            break
        taken.append(
            FilingText(
                id=f"X.{row.accession}.{row.section}",
                accession=row.accession,
                form=row.form,
                filed=row.filed,
                section=row.section,
                text=text,
                url=row.url,
            )
        )
        used += len(text)
    return tuple(taken)


def _excerpt_start(row: FilingExcerptRecord) -> int:
    """Return where this excerpt's window should begin.

    Zero for everything except an MD&A that names one of its internal headings:
    a business description a thousand characters into a section is invisible to a
    brief that always reads from the top.

    Args:
        row: The stored excerpt.

    Returns:
        An offset into the stored text, `0` when nothing better was found.
    """
    if row.section != "mda":
        return 0

    for anchor in _MDA_ANCHORS:
        found = anchor.search(row.text)
        if found is not None:
            # Group one is the heading itself; the match opens on the whitespace
            # in front of it, which would start the excerpt on a blank line.
            return found.start(1)
    return 0


def _clip(text: str, limit: int) -> str:
    """Cut text to a bound at the last sentence that fits, never mid-word."""
    if len(text) <= limit:
        return text

    window = text[:limit]
    stop = max(window.rfind(". "), window.rfind(".\n"))
    if stop > limit // 2:
        return window[: stop + 1].strip()
    return window.rsplit(" ", 1)[0].strip()


def _enrichment(
    snapshot: ScoreSnapshot, metrics: CompanyMetrics, settings: Settings
) -> EnrichmentFacts | None:
    """Return the vendor-supplied figures behind this score, when there are any.

    Enrichment is optional by design: the broad scan runs on free data and a
    metered quota is finite. Absent enrichment is normal, not a gap — it caps
    how confident a report may be and nothing else.
    """
    facts: list[MetricFact] = []

    if snapshot.market_cap_source == MarketCapSource.PROVIDER.value and snapshot.market_cap:
        facts.append(
            MetricFact(
                id="E.market_cap",
                label="Market capitalisation, vendor-supplied",
                value=snapshot.market_cap,
                unit=MetricUnit.MONEY,
                kind=EvidenceKind.ENRICHMENT,
                source=settings.fundamentals_provider,
            )
        )
    if snapshot.volume_basis == VolumeBasis.CONSOLIDATED.value:
        facts.append(
            MetricFact(
                id="E.average_dollar_volume_20d",
                label="Average daily dollar volume, consolidated",
                value=metrics.average_dollar_volume_20d,
                unit=MetricUnit.MONEY,
                kind=EvidenceKind.ENRICHMENT,
                source=settings.fundamentals_provider,
            )
        )

    if not facts:
        return None
    return EnrichmentFacts(
        provider=settings.fundamentals_provider,
        retrieved=snapshot.score_date,
        facts=tuple(facts),
    )


def _humanise(name: str) -> str:
    """Turn a machine-readable sub-score name into something a prompt can read."""
    return name.replace("_", " ")


def _enum[EnumT: StrEnum](kind: type[EnumT], value: str | None, default: EnumT) -> EnumT:
    """Return a stored string as its enum member, falling back when it is absent.

    A column written by an older revision, or left NULL before the provenance
    columns existed, reads back as the default rather than raising. The default
    is always the enum's own "unknown" member, so an absent provenance stays
    visibly absent instead of being guessed at.
    """
    if not value:
        return default
    try:
        return kind(value)
    except ValueError:
        log.warning("unrecognised provenance value", kind=kind.__name__, value=value)
        return default
