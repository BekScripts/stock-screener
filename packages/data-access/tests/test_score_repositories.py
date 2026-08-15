"""Score snapshot and benchmark persistence, against a real SQLite database.

`integration`, like the other repository tests: the daily idempotency these
verify lives in the database's `ON CONFLICT` handling, so a mocked session would
prove nothing about whether re-running the scoring command duplicates a day.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    ScoreRecord,
    ScoreSnapshotRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
    to_price_bar,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyMetrics,
    CompanyProfile,
    CompanyScore,
    ComponentScore,
    ComponentStatus,
    PriceBar,
    RiskAssessment,
    RiskLevel,
    ScoreCategory,
    ScoringStatus,
    SubScore,
    ValuationBasis,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

TODAY = date(2026, 6, 30)


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as open_session:
        yield open_session
    engine.dispose()


def _company(session: Session, ticker: str = "XYZ", **fields: Any) -> int:
    """Insert a company and return its primary key."""
    repo = CompanyRepository(session)
    repo.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc", **fields))
    session.flush()
    stored = repo.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _score(
    ticker: str = "XYZ",
    *,
    final: float | None = 80.0,
    status: ScoringStatus = ScoringStatus.SCORED,
    version: str = CURRENT_SCORE_VERSION,
    growth: float = 28.0,
    quality: float = 20.0,
    valuation: float = 18.0,
) -> CompanyScore:
    """Build a score with the columns the ranking queries read."""
    if status is not ScoringStatus.SCORED:
        return CompanyScore(ticker=ticker, status=status, score_version=version)

    def component(name: str, score: float, maximum: float) -> ComponentScore:
        return ComponentScore(
            name=name,
            status=ComponentStatus.SCORED,
            score=score,
            max_points=maximum,
            coverage=1.0,
            subscores=(SubScore(name=f"{name}_metric", points=score, max_points=maximum),),
        )

    momentum = 12.0
    raw = growth + quality + valuation + momentum
    return CompanyScore(
        ticker=ticker,
        score_version=version,
        status=status,
        growth=component("growth", growth, 35.0),
        quality=component("quality", quality, 25.0),
        valuation=component("valuation", valuation, 25.0),
        momentum=component("momentum", momentum, 15.0),
        raw_score=raw,
        risk=RiskAssessment(total_penalty=round((final or 0.0) - raw, 2), level=RiskLevel.LOW),
        final_score=final,
        category=ScoreCategory.STRONG_RESEARCH_CANDIDATE,
        data_coverage=1.0,
        valuation_basis=ValuationBasis.EV_TO_REVENUE,
    )


def _metrics(ticker: str = "XYZ", **fields: Any) -> CompanyMetrics:
    """Build the metrics whose display values are copied onto a snapshot."""
    defaults: dict[str, Any] = {
        "market_cap": 2_000_000_000.0,
        "revenue_growth_yoy": 0.35,
        "revenue_growth_acceleration": 0.10,
        "enterprise_value": 1_800_000_000.0,
    }
    return CompanyMetrics(ticker=ticker, **{**defaults, **fields})


@pytest.mark.integration
def test_a_score_is_stored_with_its_components_and_breakdown(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)

    repository.upsert_scores([ScoreRecord(company_id, _score(), _metrics())], TODAY)
    session.flush()

    stored = repository.latest_for_company(company_id, score_version=CURRENT_SCORE_VERSION)
    assert stored is not None
    assert stored.final_score == 80.0
    assert stored.growth_score == 28.0
    assert stored.scoring_status == "SCORED"
    assert stored.market_cap == 2_000_000_000.0
    assert isinstance(stored.breakdown, dict)
    assert stored.breakdown["growth"]["score"] == 28.0


@pytest.mark.integration
def test_scoring_the_same_day_twice_replaces_rather_than_duplicates(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)

    repository.upsert_scores([ScoreRecord(company_id, _score(final=70.0), _metrics())], TODAY)
    session.flush()
    repository.upsert_scores([ScoreRecord(company_id, _score(final=85.0), _metrics())], TODAY)
    session.flush()

    assert repository.count() == 1
    stored = repository.latest_for_company(company_id, score_version=CURRENT_SCORE_VERSION)
    assert stored is not None
    assert stored.final_score == 85.0


@pytest.mark.integration
def test_a_second_version_does_not_overwrite_the_first(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)

    repository.upsert_scores([ScoreRecord(company_id, _score(final=70.0), _metrics())], TODAY)
    repository.upsert_scores(
        [ScoreRecord(company_id, _score(final=90.0, version="COMPOUNDER_V2"), _metrics())], TODAY
    )
    session.flush()

    assert repository.count() == 2
    original = repository.latest_for_company(company_id, score_version=CURRENT_SCORE_VERSION)
    assert original is not None
    assert original.final_score == 70.0


@pytest.mark.integration
def test_a_company_that_could_not_be_scored_still_gets_a_row(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)

    repository.upsert_scores(
        [ScoreRecord(company_id, _score(status=ScoringStatus.UNSUPPORTED_SECTOR), None)], TODAY
    )
    session.flush()

    stored = repository.latest_for_company(company_id, score_version=CURRENT_SCORE_VERSION)
    assert stored is not None
    assert stored.scoring_status == "UNSUPPORTED_SECTOR"
    assert stored.final_score is None


@pytest.mark.integration
def test_rankings_come_back_best_first(session: Session) -> None:
    repository = ScoreSnapshotRepository(session)
    for ticker, final in (("AAA", 60.0), ("BBB", 90.0), ("CCC", 75.0)):
        repository.upsert_scores(
            [ScoreRecord(_company(session, ticker), _score(ticker, final=final), _metrics())], TODAY
        )
    session.flush()

    ranked = repository.list_scored(score_version=CURRENT_SCORE_VERSION)

    assert [company.ticker for _, company in ranked] == ["BBB", "CCC", "AAA"]


@pytest.mark.integration
def test_rankings_exclude_companies_without_a_score(session: Session) -> None:
    repository = ScoreSnapshotRepository(session)
    repository.upsert_scores(
        [ScoreRecord(_company(session, "AAA"), _score("AAA", final=60.0), _metrics())], TODAY
    )
    repository.upsert_scores(
        [
            ScoreRecord(
                _company(session, "BANK"),
                _score("BANK", status=ScoringStatus.UNSUPPORTED_SECTOR),
                None,
            )
        ],
        TODAY,
    )
    session.flush()

    ranked = repository.list_scored(score_version=CURRENT_SCORE_VERSION)

    assert [company.ticker for _, company in ranked] == ["AAA"]


@pytest.mark.integration
def test_rankings_apply_their_filters(session: Session) -> None:
    repository = ScoreSnapshotRepository(session)
    repository.upsert_scores(
        [
            ScoreRecord(
                _company(session, "BIG"),
                _score("BIG", final=88.0),
                _metrics(market_cap=50_000_000_000.0),
            ),
            ScoreRecord(
                _company(session, "SMALL"),
                _score("SMALL", final=72.0),
                _metrics(market_cap=1_000_000_000.0),
            ),
        ],
        TODAY,
    )
    session.flush()

    ranked = repository.list_scored(
        score_version=CURRENT_SCORE_VERSION, max_market_cap=5_000_000_000.0, min_final_score=70.0
    )

    assert [company.ticker for _, company in ranked] == ["SMALL"]


@pytest.mark.integration
def test_rankings_default_to_the_most_recent_scored_day(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)
    repository.upsert_scores(
        [ScoreRecord(company_id, _score(final=60.0), _metrics())], TODAY - timedelta(days=30)
    )
    repository.upsert_scores([ScoreRecord(company_id, _score(final=80.0), _metrics())], TODAY)
    session.flush()

    ranked = repository.list_scored(score_version=CURRENT_SCORE_VERSION)

    assert [snapshot.final_score for snapshot, _ in ranked] == [80.0]


@pytest.mark.integration
def test_an_empty_table_ranks_nothing(session: Session) -> None:
    assert ScoreSnapshotRepository(session).list_scored(score_version=CURRENT_SCORE_VERSION) == []


@pytest.mark.integration
def test_the_nearest_earlier_snapshot_is_found_without_an_exact_date(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)
    repository.upsert_scores(
        [ScoreRecord(company_id, _score(final=64.0), _metrics())], TODAY - timedelta(days=33)
    )
    repository.upsert_scores(
        [ScoreRecord(company_id, _score(final=70.0), _metrics())], TODAY - timedelta(days=31)
    )
    repository.upsert_scores([ScoreRecord(company_id, _score(final=82.0), _metrics())], TODAY)
    session.flush()

    earlier = repository.snapshots_on_or_before(
        [company_id], TODAY - timedelta(days=30), score_version=CURRENT_SCORE_VERSION
    )

    assert earlier[company_id].final_score == 70.0


@pytest.mark.integration
def test_no_earlier_snapshot_returns_nothing_rather_than_a_zero(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)
    repository.upsert_scores([ScoreRecord(company_id, _score(), _metrics())], TODAY)
    session.flush()

    earlier = repository.snapshots_on_or_before(
        [company_id], TODAY - timedelta(days=30), score_version=CURRENT_SCORE_VERSION
    )

    assert earlier == {}


@pytest.mark.integration
def test_a_prior_snapshot_from_another_version_is_not_returned(session: Session) -> None:
    company_id = _company(session)
    repository = ScoreSnapshotRepository(session)
    repository.upsert_scores(
        [ScoreRecord(company_id, _score(final=50.0, version="COMPOUNDER_V0"), _metrics())],
        TODAY - timedelta(days=31),
    )
    session.flush()

    earlier = repository.snapshots_on_or_before(
        [company_id], TODAY - timedelta(days=30), score_version=CURRENT_SCORE_VERSION
    )

    assert earlier == {}


@pytest.mark.integration
def test_looking_up_no_companies_makes_no_query(session: Session) -> None:
    assert (
        ScoreSnapshotRepository(session).snapshots_on_or_before(
            [], TODAY, score_version=CURRENT_SCORE_VERSION
        )
        == {}
    )


@pytest.mark.integration
def test_benchmark_bars_are_stored_and_read_back(session: Session) -> None:
    repository = BenchmarkPriceRepository(session)
    bars = [
        PriceBar(
            date=TODAY - timedelta(days=offset), open=5.0, high=6.0, low=4.0, close=5.5, volume=10.0
        )
        for offset in range(3)
    ]

    written = repository.upsert_bars("spy", bars)
    session.flush()

    assert written == 3
    assert repository.latest_date("SPY") == TODAY
    assert [to_price_bar(row).close for row in repository.list_bars("SPY")] == [5.5, 5.5, 5.5]


@pytest.mark.integration
def test_re_fetching_benchmark_bars_updates_rather_than_duplicates(session: Session) -> None:
    repository = BenchmarkPriceRepository(session)
    original = PriceBar(date=TODAY, open=5.0, high=6.0, low=4.0, close=5.5, volume=10.0)
    corrected = PriceBar(date=TODAY, open=5.0, high=6.0, low=4.0, close=5.9, volume=12.0)

    repository.upsert_bars("SPY", [original])
    session.flush()
    repository.upsert_bars("SPY", [corrected])
    session.flush()

    assert repository.count() == 1
    assert repository.list_bars("SPY")[0].close == 5.9


@pytest.mark.integration
def test_an_unfetched_benchmark_has_no_latest_date(session: Session) -> None:
    assert BenchmarkPriceRepository(session).latest_date("SPY") is None


@pytest.mark.integration
def test_a_risk_level_can_be_excluded_from_a_ranking(session: Session) -> None:
    repository = ScoreSnapshotRepository(session)
    risky = _score("RISK", final=80.0)
    risky = risky.model_copy(
        update={"risk": RiskAssessment(total_penalty=-20.0, level=RiskLevel.VERY_HIGH)}
    )
    repository.upsert_scores(
        [
            ScoreRecord(_company(session, "SAFE"), _score("SAFE", final=75.0), _metrics()),
            ScoreRecord(_company(session, "RISK"), risky, _metrics()),
        ],
        TODAY,
    )
    session.flush()

    ranked = repository.list_scored(
        score_version=CURRENT_SCORE_VERSION, exclude_risk_levels=[RiskLevel.VERY_HIGH.value]
    )

    assert [company.ticker for _, company in ranked] == ["SAFE"]


@pytest.mark.integration
def test_a_market_wide_write_is_chunked_rather_than_one_statement(session: Session) -> None:
    # Every column of every row is a bind parameter, and PostgreSQL caps a
    # statement at 65,535 of them. A universe of a few thousand companies would
    # exceed that in a single INSERT, so the write is chunked.
    repository = ScoreSnapshotRepository(session)
    records = [
        ScoreRecord(_company(session, f"T{index:04d}"), _score(f"T{index:04d}"), _metrics())
        for index in range(1200)
    ]

    written = repository.upsert_scores(records, TODAY)
    session.flush()

    assert written == 1200
    assert repository.count() == 1200


@pytest.mark.integration
def test_a_market_wide_prior_lookup_is_chunked(session: Session) -> None:
    repository = ScoreSnapshotRepository(session)
    identifiers = [_company(session, f"T{index:04d}") for index in range(1200)]
    repository.upsert_scores(
        [ScoreRecord(company_id, _score(), _metrics()) for company_id in identifiers],
        TODAY - timedelta(days=30),
    )
    session.flush()

    earlier = repository.snapshots_on_or_before(
        identifiers, TODAY, score_version=CURRENT_SCORE_VERSION
    )

    assert len(earlier) == 1200
