from collections.abc import Callable
from datetime import date

import pytest

from domain import MetricUnit, RiskLevel, ScoreCategory, ScoreWarning, ScoringStatus
from research import (
    Claim,
    ConfidenceLevel,
    DraftClaim,
    DraftReport,
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
    Section,
    SelectionReason,
)

SCORE_DATE = date(2026, 8, 14)
ACCESSION = "0000320193-26-000073"


@pytest.fixture
def brief() -> ResearchBrief:
    """A well-covered, enriched company with one filing and four quarters."""
    return ResearchBrief(
        ticker="ACME",
        name="Acme Compounders Inc.",
        sector="Technology",
        industry="Application Software",
        exchange="NASDAQ",
        as_of=SCORE_DATE,
        selection=SelectionReason.TOP_RANKED,
        score=ScoreEvidence(
            score_version="COMPOUNDER_V1_1",
            score_date=SCORE_DATE,
            scoring_status=ScoringStatus.SCORED,
            final_score=77.4,
            raw_score=82.0,
            risk_penalty=-4.6,
            risk_level=RiskLevel.MEDIUM,
            category=ScoreCategory.STRONG_RESEARCH_CANDIDATE,
            data_coverage=0.85,
            ranking_state=RankingState.FINAL,
            items=(
                ScoreItem(id="S.growth", label="Growth", points=30.4, max_points=35.0),
                ScoreItem(
                    id="S.growth.revenue_growth",
                    label="Growth - revenue growth",
                    points=9.6,
                    max_points=12.0,
                    observed=0.382,
                    unit=MetricUnit.PERCENT,
                ),
                ScoreItem(
                    id="S.growth.cagr_3y",
                    label="Growth - 3-year CAGR",
                    points=None,
                    max_points=6.0,
                ),
                ScoreItem(id="S.quality", label="Financial quality", points=20.5, max_points=25.0),
                ScoreItem(id="S.valuation", label="Valuation", points=18.0, max_points=25.0),
                ScoreItem(
                    id="S.momentum", label="Market confirmation", points=13.1, max_points=15.0
                ),
                ScoreItem(
                    id="S.risk.dilution",
                    label="Risk - dilution",
                    points=-4.6,
                    observed=0.11,
                    unit=MetricUnit.PERCENT,
                ),
            ),
            warnings=(ScoreWarning.WEIGHT_REDISTRIBUTED,),
        ),
        facts=(
            MetricFact(id="M.revenue_growth_yoy", label="Revenue growth YoY", value=0.382),
            MetricFact(
                id="M.revenue_growth_acceleration",
                label="Growth acceleration",
                value=0.17,
                unit=MetricUnit.POINTS,
            ),
            MetricFact(id="M.fcf_margin", label="FCF margin", value=0.121),
            MetricFact(
                id="M.ev_to_revenue",
                label="EV / Revenue",
                value=3.2,
                unit=MetricUnit.MULTIPLE,
            ),
            MetricFact(id="M.revenue_cagr_3y", label="3-year revenue CAGR", value=None),
        ),
        quarters=(
            ReportedPeriod(
                id="F.2025-06-30",
                period_end=date(2025, 6, 30),
                revenue=298_000_000.0,
                source="sec-edgar",
            ),
            ReportedPeriod(
                id="F.2025-09-30",
                period_end=date(2025, 9, 30),
                revenue=331_000_000.0,
                source="sec-edgar",
            ),
            ReportedPeriod(
                id="F.2025-12-31",
                period_end=date(2025, 12, 31),
                revenue=376_000_000.0,
                source="sec-edgar",
            ),
            ReportedPeriod(
                id="F.2026-03-31",
                period_end=date(2026, 3, 31),
                revenue=412_000_000.0,
                gross_profit=289_000_000.0,
                free_cash_flow=50_000_000.0,
                shares_outstanding=1_400_000_000.0,
                source="sec-edgar",
            ),
        ),
        score_history=(
            ScorePoint(id="S.history.2026-07-15", score_date=date(2026, 7, 15), final_score=65.1),
        ),
        filings=(
            FilingReference(
                id=f"D.{ACCESSION}",
                form="10-Q",
                filed=date(2026, 5, 2),
                period_end=date(2026, 3, 31),
                accession=ACCESSION,
                url="https://www.sec.gov/Archives/edgar/data/1/000032019326000073.htm",
            ),
        ),
        enrichment=EnrichmentFacts(
            provider="fmp",
            retrieved=SCORE_DATE,
            facts=(
                MetricFact(
                    id="E.market_cap",
                    label="Market capitalisation",
                    value=4_100_000_000.0,
                    unit=MetricUnit.MONEY,
                    kind=EvidenceKind.ENRICHMENT,
                    source="fmp",
                ),
            ),
        ),
    )


@pytest.fixture
def excerpted_brief(brief: ResearchBrief) -> ResearchBrief:
    """The same brief, with one filing's text actually extracted.

    The metadata-only `brief` fixture is a company extraction has not reached;
    this is one it has. Both are realistic, and the pair is what keeps the
    difference between citing a filing and quoting one under test.
    """
    return brief.model_copy(
        update={
            "excerpts": (
                FilingText(
                    id=f"X.{ACCESSION}.item_1.01",
                    accession=ACCESSION,
                    form="8-K",
                    filed=date(2026, 5, 2),
                    section="item_1.01",
                    text="The Company entered a new supply agreement in the quarter.",
                    url="https://www.sec.gov/Archives/edgar/data/1/000032019326000073.htm",
                ),
            )
        }
    )


@pytest.fixture
def make_draft() -> Callable[..., DraftReport]:
    """Build a draft carrying claims in one section, leaving the rest empty."""

    def build(
        section: Section,
        *claims: Claim,
        confidence: ConfidenceLevel = ConfidenceLevel.LOW,
    ) -> DraftReport:
        return DraftReport(
            ticker="ACME",
            claims=tuple(
                DraftClaim(
                    section=section,
                    text=claim.text,
                    basis=claim.basis,
                    evidence=claim.evidence,
                )
                for claim in claims
            ),
            confidence=confidence,
        )

    return build
