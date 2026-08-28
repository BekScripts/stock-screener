from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from deep_research import (
    DataFreshness,
    DeepBasis,
    DeepClaim,
    DeepConfidence,
    DeepResearchBrief,
    DeepResearchReport,
    DeepSections,
    ExternalEvidence,
    ExternalSourceType,
    MarketRanking,
    SourceTier,
    external_evidence_id,
)
from domain import MetricUnit, RiskLevel, ScoreCategory, ScoreWarning, ScoringStatus
from research import (
    ConfidenceLevel,
    EnrichmentFacts,
    EvidenceKind,
    FilingReference,
    FilingText,
    MetricFact,
    RankingState,
    ReportedPeriod,
    ResearchStatus,
    ScoreEvidence,
    ScoreItem,
    ScorePoint,
)

SCORE_DATE = date(2026, 8, 14)
ASSEMBLED_AT = datetime(2026, 8, 14, 18, 30, tzinfo=UTC)
ACCESSION = "0000320193-26-000073"
ARTICLE_URL = "https://example-news.test/acme-signs-supply-deal"


@pytest.fixture
def score_date() -> date:
    """The score date every fixture is built around."""
    return SCORE_DATE


@pytest.fixture
def assembled_at() -> datetime:
    """When the brief fixtures were assembled."""
    return ASSEMBLED_AT


@pytest.fixture
def accession() -> str:
    """The accession the filing fixtures share."""
    return ACCESSION


@pytest.fixture
def score() -> ScoreEvidence:
    """A well-covered, enriched score with one unscored sub-score."""
    return ScoreEvidence(
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
                id="S.growth.cagr_3y", label="Growth - 3-year CAGR", points=None, max_points=6.0
            ),
            ScoreItem(id="S.quality", label="Financial quality", points=20.5, max_points=25.0),
            ScoreItem(id="S.valuation", label="Valuation", points=18.0, max_points=25.0),
        ),
        warnings=(ScoreWarning.WEIGHT_REDISTRIBUTED,),
    )


@pytest.fixture
def article() -> ExternalEvidence:
    """One reputable news item about the company."""
    return ExternalEvidence(
        evidence_id=external_evidence_id(ExternalSourceType.NEWS, ARTICLE_URL),
        source_type=ExternalSourceType.NEWS,
        tier=SourceTier.TIER_2_REPUTABLE,
        title="Acme signs multi-year supply agreement",
        publisher="Example Newswire",
        url=ARTICLE_URL,
        excerpt="Acme Compounders said on Tuesday it had signed a multi-year supply agreement.",
        retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        published_at=date(2026, 8, 12),
        ticker="ACME",
    )


@pytest.fixture
def brief(score: ScoreEvidence) -> DeepResearchBrief:
    """A refreshed company with filings, extracted text and no external evidence.

    Deliberately without `external`: collection has not been built, an empty set
    is a legal brief, and every test that adds an item then measures a real
    difference rather than a difference from another article.
    """
    return DeepResearchBrief(
        ticker="ACME",
        name="Acme Compounders Inc.",
        sector="Technology",
        industry="Application Software",
        exchange="NASDAQ",
        as_of=SCORE_DATE,
        assembled_at=ASSEMBLED_AT,
        score=score,
        facts=(
            MetricFact(id="M.revenue_growth_yoy", label="Revenue growth YoY", value=0.382),
            MetricFact(id="M.fcf_margin", label="FCF margin", value=0.121),
            MetricFact(id="M.revenue_cagr_3y", label="3-year revenue CAGR", value=None),
        ),
        quarters=(
            ReportedPeriod(
                id="F.2026-03-31",
                period_end=date(2026, 3, 31),
                revenue=412_000_000.0,
                free_cash_flow=50_000_000.0,
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
        excerpts=(
            FilingText(
                id=f"X.{ACCESSION}.mda",
                accession=ACCESSION,
                form="10-Q",
                filed=date(2026, 5, 2),
                section="mda",
                text="Revenue grew on continued demand for the Company's platform products.",
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
        ranking=MarketRanking(
            rank=12, universe_size=4300, percentile=0.997, ranking_state=RankingState.FINAL
        ),
        freshness=DataFreshness(
            refreshed_at=datetime(2026, 8, 14, 18, 0, tzinfo=UTC),
            price_as_of=SCORE_DATE,
            fundamentals_through=date(2026, 3, 31),
        ),
    )


@pytest.fixture
def researched(brief: DeepResearchBrief, article: ExternalEvidence) -> DeepResearchBrief:
    """The same brief with one external source collected."""
    return brief.model_copy(update={"external": (article,)})


@pytest.fixture
def make_external() -> Callable[..., ExternalEvidence]:
    """Build an external item, varying only what a test cares about."""

    def build(
        url: str = ARTICLE_URL,
        *,
        source_type: ExternalSourceType = ExternalSourceType.NEWS,
        tier: SourceTier = SourceTier.TIER_2_REPUTABLE,
        excerpt: str = "An excerpt.",
        retrieved_at: datetime = datetime(2026, 8, 14, 17, 0, tzinfo=UTC),
        published_at: date | None = date(2026, 8, 12),
        occurred_at: date | None = None,
    ) -> ExternalEvidence:
        return ExternalEvidence(
            evidence_id=external_evidence_id(source_type, url),
            source_type=source_type,
            tier=tier,
            title="A headline",
            publisher="Example Newswire",
            url=url,
            excerpt=excerpt,
            retrieved_at=retrieved_at,
            published_at=published_at,
            occurred_at=occurred_at,
        )

    return build


@pytest.fixture
def researched_report(researched: DeepResearchBrief) -> DeepResearchReport:
    """A validated report over the researched brief, carrying its cited source.

    Hand-built rather than produced by a validator, because the validator is a
    later step. What it demonstrates is the shape a stored report has: claims on
    three different bases, and the external item its `W.` citation names.
    """
    article = researched.external[0]
    return DeepResearchReport(
        contract_version=researched.contract_version,
        prompt_version="DEEP_PROMPT_V1",
        model_id="test-model",
        generated_at=datetime(2026, 8, 14, 19, 0, tzinfo=UTC),
        ticker=researched.ticker,
        as_of=researched.as_of,
        score_version=researched.score.score_version,
        deterministic_fingerprint=researched.deterministic_fingerprint(),
        evidence_fingerprint=researched.evidence_fingerprint(),
        status=ResearchStatus.COMPLETE,
        confidence=DeepConfidence(
            level=ConfidenceLevel.MEDIUM,
            ceiling=ConfidenceLevel.MEDIUM,
            claimed=ConfidenceLevel.MEDIUM,
            rationale="metric coverage 85%; ranking FINAL.",
            metric_coverage=0.85,
            filing_coverage=0.33,
            external_coverage=0.33,
        ),
        sections=DeepSections(
            why_the_algorithm_likes_it=(
                DeepClaim(
                    text="Growth contributed 30.4 of a possible 35.0 points.",
                    basis=DeepBasis.DETERMINISTIC,
                    evidence=("S.growth",),
                ),
            ),
            recent_developments=(
                DeepClaim(
                    text="Acme said it signed a multi-year supply agreement.",
                    basis=DeepBasis.EXTERNAL,
                    evidence=(article.evidence_id,),
                ),
            ),
            bull_case=(
                DeepClaim(
                    text="Demand for the platform may keep revenue growing.",
                    basis=DeepBasis.INTERPRETATION,
                    evidence=(f"X.{ACCESSION}.mda",),
                ),
            ),
        ),
        external_evidence=(article,),
        unknowns=("valuation",),
    )
