"""Scoring and ranking end to end: stored rows in, a shortlist out.

Real SQLite, the real metric engine, the real formula. The companies are
synthetic but complete — twelve quarters of statements and four hundred sessions
of prices each — because the behaviour worth protecting is what the ranking does
with a whole market, not what one function returns.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ScoreSnapshotRepository,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyProfile,
    FinancialPeriod,
    PriceBar,
    ScoringStatus,
)
from stock_screener.config import Settings
from stock_screener.scoring import (
    great_company_wrong_price,
    hidden_gems,
    improving_fast,
    latest_score,
    load_benchmark_returns,
    score_market,
    top_opportunities,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SETTINGS = Settings(environment="test")
TODAY = date(2026, 6, 30)
QUARTER_DAYS = 91
PRICE_DAYS = 400


def _quarters(
    *,
    growth: float,
    base_revenue: float = 100_000_000.0,
    gross_margin: float = 0.60,
    operating_margin: float = 0.08,
    fcf_margin: float = 0.12,
    margin_trend: float = 0.0,
    cash: float = 300_000_000.0,
    debt: float = 50_000_000.0,
    dilution: float = 0.01,
    count: int = 16,
    age_days: int = 0,
) -> list[FinancialPeriod]:
    """Build a history whose year-over-year revenue growth is exactly `growth`.

    `margin_trend` widens the gross and operating margins by that many decimal
    percentage points a year, so a test can ask for a company whose margins are
    improving without hand-writing sixteen quarters.

    `age_days` shifts the whole history back, which is how a test asks for a
    company that has stopped reporting.
    """
    periods = []
    for index in range(count):
        years = index / 4
        revenue = base_revenue * (1 + growth) ** years
        periods.append(
            FinancialPeriod(
                period_end=TODAY - timedelta(days=QUARTER_DAYS * (count - 1 - index) + age_days),
                revenue=revenue,
                gross_profit=revenue * (gross_margin + margin_trend * years),
                operating_income=revenue * (operating_margin + margin_trend * years),
                free_cash_flow=revenue * fcf_margin,
                cash=cash,
                total_debt=debt,
                shares_outstanding=100_000_000.0 * (1 + dilution) ** years,
                source="test",
            )
        )
    return periods


def _bars(*, annual_return: float, end_price: float = 40.0) -> list[PriceBar]:
    """Build daily bars compounding to exactly `annual_return` over a year."""
    daily = (1 + annual_return) ** (1 / 365)
    return [
        PriceBar(
            date=TODAY - timedelta(days=offset),
            open=(price := end_price / daily**offset),
            high=price * 1.001,
            low=price * 0.999,
            close=price,
            volume=1_000_000.0,
        )
        for offset in reversed(range(PRICE_DAYS))
    ]


def _store(
    session: Session,
    ticker: str,
    *,
    market_cap: float = 2_000_000_000.0,
    growth: float = 0.30,
    annual_return: float = 0.25,
    profile_fields: dict[str, Any] | None = None,
    quarter_fields: dict[str, Any] | None = None,
    bars: list[PriceBar] | None = None,
) -> int:
    """Store one complete company and return its primary key."""
    companies = CompanyRepository(session)
    fields: dict[str, Any] = {
        "ticker": ticker,
        "name": f"{ticker} Corp",
        "exchange": "NASDAQ",
        "sector": "Technology",
        "industry": "Software - Application",
        "market_cap": market_cap,
        "average_volume": 1_000_000.0,
        **(profile_fields or {}),
    }
    companies.upsert_profile(CompanyProfile(**fields))
    session.flush()

    stored = companies.get_by_ticker(ticker)
    assert stored is not None

    FinancialSnapshotRepository(session).upsert_periods(
        stored.id, _quarters(growth=growth, **(quarter_fields or {}))
    )
    PriceHistoryRepository(session).upsert_bars(
        stored.id, bars if bars is not None else _bars(annual_return=annual_return)
    )
    session.flush()
    return stored.id


def _store_benchmark(session: Session, *, annual_return: float = 0.10) -> None:
    """Store the benchmark series scoring measures relative strength against."""
    BenchmarkPriceRepository(session).upsert_bars(
        SETTINGS.benchmark_symbol, _bars(annual_return=annual_return, end_price=500.0)
    )
    session.flush()


@pytest.mark.integration
def test_a_complete_company_is_scored_and_persisted(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)

    run = score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert run.persisted == 1
    assert [row.score.status for row in run.rows] == [ScoringStatus.SCORED]
    stored = ScoreSnapshotRepository(session).list_scored(
        score_version=run.rows[0].score.score_version
    )
    assert len(stored) == 1
    assert stored[0][0].final_score == run.rows[0].score.final_score


@pytest.mark.integration
def test_scoring_twice_in_one_day_leaves_one_snapshot(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert ScoreSnapshotRepository(session).count() == 1


@pytest.mark.integration
def test_a_dry_run_writes_nothing(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)

    run = score_market(session, SETTINGS, score_date=TODAY, persist=False)
    session.flush()

    assert run.rows[0].score.final_score is not None
    assert run.persisted == 0
    assert ScoreSnapshotRepository(session).count() == 0


@pytest.mark.integration
def test_a_bank_stays_in_the_database_and_out_of_the_ranking(session: Session) -> None:
    _store(session, "XYZ")
    _store(
        session,
        "BNK",
        profile_fields={"sector": "Financial Services", "industry": "Banks - Regional"},
    )
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    statuses = {
        company.ticker: snapshot.scoring_status for snapshot, company in _all_snapshots(session)
    }
    assert statuses["BNK"] == ScoringStatus.UNSUPPORTED_SECTOR.value
    assert [row.ticker for row in top_opportunities(session)] == ["XYZ"]
    assert CompanyRepository(session).get_by_ticker("BNK") is not None


@pytest.mark.integration
def test_an_ineligible_company_is_recorded_but_not_ranked(session: Session) -> None:
    _store(session, "XYZ")
    _store(session, "TINY", market_cap=10_000_000.0)
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    statuses = {
        company.ticker: snapshot.scoring_status for snapshot, company in _all_snapshots(session)
    }
    assert statuses["TINY"] == ScoringStatus.NOT_ELIGIBLE.value
    assert [row.ticker for row in top_opportunities(session)] == ["XYZ"]


@pytest.mark.integration
def test_without_benchmark_history_nothing_can_be_scored(session: Session) -> None:
    _store(session, "XYZ")

    run = score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert run.rows[0].score.status is ScoringStatus.INSUFFICIENT_DATA
    assert top_opportunities(session) == []


@pytest.mark.integration
def test_benchmark_returns_are_read_from_the_stored_series(session: Session) -> None:
    _store_benchmark(session, annual_return=0.12)

    benchmark = load_benchmark_returns(session, SETTINGS.benchmark_symbol)

    assert benchmark.symbol == "SPY"
    assert benchmark.return_12m == pytest.approx(0.12, abs=0.005)
    assert benchmark.return_6m is not None


@pytest.mark.integration
def test_the_ranking_orders_companies_the_way_the_score_does(session: Session) -> None:
    # A: strong everywhere. B: fast but expensive. C: mediocre.
    # D: B's growth with heavy dilution and a burn.
    _store(session, "AAA", market_cap=1_000_000_000.0, growth=0.55, annual_return=0.50)
    _store(session, "BBB", market_cap=30_000_000_000.0, growth=0.55, annual_return=0.20)
    _store(session, "CCC", market_cap=6_000_000_000.0, growth=0.05, annual_return=0.02)
    _store(
        session,
        "DDD",
        market_cap=1_000_000_000.0,
        growth=0.55,
        annual_return=0.50,
        quarter_fields={"dilution": 0.45, "fcf_margin": -0.90, "cash": 40_000_000.0},
    )
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    ranked = {row.ticker: row for row in top_opportunities(session)}
    final = {ticker: row.final_score or 0.0 for ticker, row in ranked.items()}

    assert final["AAA"] > final["BBB"] > final["CCC"]
    # Same growth as AAA, a much lower final score, and the growth score intact.
    assert ranked["DDD"].growth_score == ranked["AAA"].growth_score
    assert final["DDD"] < final["AAA"] - 10
    assert ranked["DDD"].risk_level in {"HIGH", "VERY_HIGH"}


@pytest.mark.integration
def test_hidden_gems_leaves_out_the_large_companies(session: Session) -> None:
    _store(session, "SMALL", market_cap=1_000_000_000.0, growth=0.55, annual_return=0.45)
    _store(session, "MEGA", market_cap=80_000_000_000.0, growth=0.55, annual_return=0.45)
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert [row.ticker for row in hidden_gems(session)] == ["SMALL"]


@pytest.mark.integration
def test_hidden_gems_leaves_out_slow_growers(session: Session) -> None:
    _store(session, "SLOW", market_cap=1_000_000_000.0, growth=0.05, annual_return=0.45)
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert hidden_gems(session) == []


@pytest.mark.integration
def test_great_company_wrong_price_finds_the_expensive_strong_company(session: Session) -> None:
    # Identical businesses — fast, improving margins, cash generative — priced
    # seventy-five times apart.
    excellent = {"gross_margin": 0.69, "fcf_margin": 0.30, "margin_trend": 0.06}
    _store(
        session,
        "RICH",
        market_cap=60_000_000_000.0,
        growth=0.60,
        annual_return=0.60,
        quarter_fields=excellent,
    )
    _store(
        session,
        "CHEAP",
        market_cap=800_000_000.0,
        growth=0.60,
        annual_return=0.60,
        quarter_fields=excellent,
    )
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert [row.ticker for row in great_company_wrong_price(session)] == ["RICH"]


@pytest.mark.integration
def test_score_changes_are_measured_against_the_nearest_earlier_snapshot(
    session: Session,
) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY - timedelta(days=31))
    session.flush()
    _lower_stored_score(session, TODAY - timedelta(days=31), by=12.0)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    row = top_opportunities(session)[0]

    assert row.score_change_30d == pytest.approx(12.0, abs=0.01)
    # The only earlier snapshot is 31 days old, so the seven-day window falls
    # back to it rather than reporting no change: the comparison is always
    # against the nearest snapshot at or before the target date.
    assert row.score_change_7d == pytest.approx(12.0, abs=0.01)


@pytest.mark.integration
def test_improving_fast_ranks_by_the_size_of_the_improvement(session: Session) -> None:
    _store(session, "AAA")
    _store(session, "BBB", market_cap=3_000_000_000.0)
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY - timedelta(days=30))
    session.flush()
    _lower_stored_score(session, TODAY - timedelta(days=30), by=5.0, ticker="AAA")
    _lower_stored_score(session, TODAY - timedelta(days=30), by=15.0, ticker="BBB")
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    improving = improving_fast(session)

    assert [row.ticker for row in improving] == ["BBB", "AAA"]
    assert [row.rank for row in improving] == [1, 2]


@pytest.mark.integration
def test_improving_fast_omits_companies_with_no_history(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert improving_fast(session) == []


@pytest.mark.integration
def test_one_company_can_be_explained_from_its_stored_breakdown(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    detail = latest_score(session, "xyz")

    assert detail is not None
    assert detail.scoring_status == ScoringStatus.SCORED.value
    assert detail.breakdown["growth"]["subscores"][0]["name"] == "revenue_growth"


@pytest.mark.integration
def test_an_unknown_company_has_no_score_detail(session: Session) -> None:
    assert latest_score(session, "NOPE") is None


@pytest.mark.integration
def test_scoring_can_be_limited_to_named_companies(session: Session) -> None:
    _store(session, "AAA")
    _store(session, "BBB")
    _store_benchmark(session)

    run = score_market(session, SETTINGS, tickers=["AAA"], score_date=TODAY)

    assert [row.ticker for row in run.rows] == ["AAA"]


def _all_snapshots(session: Session) -> list[tuple[Any, Any]]:
    """Return every stored snapshot with its company, whatever the status."""
    repository = ScoreSnapshotRepository(session)
    rows: list[tuple[Any, Any]] = []
    for status in ScoringStatus:
        rows.extend(
            repository.list_scored(score_version=CURRENT_SCORE_VERSION, scoring_status=status.value)
        )
    return rows


def _lower_stored_score(
    session: Session, score_date: date, *, by: float, ticker: str | None = None
) -> None:
    """Push an earlier day's stored score down, to create a known improvement."""
    for snapshot, company in _all_snapshots(session):
        if snapshot.score_date != score_date or snapshot.final_score is None:
            continue
        if ticker is not None and company.ticker != ticker:
            continue
        snapshot.final_score = round(snapshot.final_score - by, 2)
    session.flush()


# -- V1.2: stale fundamentals leave the ranking, not the database -----------


@pytest.mark.integration
def test_a_current_company_scores_exactly_what_v1_1_scored(session: Session) -> None:
    # The whole claim of V1.2, pinned to a number. Every formula, curve, weight,
    # cap and threshold is V1.1's, so a company whose fundamentals are current
    # must produce the identical figure — not merely a similar one.
    #
    # These values were not copied from the current implementation. They were
    # measured by running this exact company through the last V1.1 commit and
    # through V1.2 and comparing: both produce 75.75, component for component.
    # If a later change moves any of them, it is a scoring change and needs its
    # own version.
    _store(session, "CURR")
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    row = top_opportunities(session)[0]
    assert row.ticker == "CURR"
    assert row.final_score == pytest.approx(75.75, abs=0.01)
    assert row.raw_score == pytest.approx(75.75, abs=0.01)
    assert row.growth_score == pytest.approx(23.50, abs=0.01)
    assert row.quality_score == pytest.approx(17.65, abs=0.01)
    assert row.valuation_score == pytest.approx(22.68, abs=0.01)
    assert row.momentum_score == pytest.approx(11.92, abs=0.01)
    assert row.risk_penalty == pytest.approx(0.0, abs=0.01)
    assert row.data_coverage == pytest.approx(1.0, abs=0.001)


@pytest.mark.integration
def test_a_stale_company_keeps_its_score_and_leaves_the_ranking(session: Session) -> None:
    # Centerra Gold, in miniature: a real score of the company as it last
    # reported, ranked against a market capitalisation two years newer. The
    # number is not wrong — it is not an answer to what looks interesting now.
    _store(session, "FRESH")
    _store(session, "STALE", quarter_fields={"age_days": 900})
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert [row.ticker for row in top_opportunities(session)] == ["FRESH"]

    # The score itself survives in full, which is what the stock page reads.
    detail = latest_score(session, "STALE")
    assert detail is not None
    assert detail.scoring_status == ScoringStatus.SCORED.value
    assert detail.final_score is not None
    assert detail.freshness == "STALE"
    assert detail.rank_eligible is False

    current = latest_score(session, "FRESH")
    assert current is not None
    assert current.freshness == "CURRENT"
    assert current.rank_eligible is True


@pytest.mark.integration
def test_every_current_ranking_view_excludes_a_stale_score(session: Session) -> None:
    # One filter in one place, four views. A view that built its own query would
    # be the one that quietly readmitted them.
    _store(
        session, "STALE", growth=0.60, market_cap=300_000_000.0, quarter_fields={"age_days": 900}
    )
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert top_opportunities(session) == []
    assert hidden_gems(session) == []
    assert great_company_wrong_price(session) == []
    assert improving_fast(session) == []


@pytest.mark.integration
def test_a_stale_row_is_still_stored_and_countable(session: Session) -> None:
    # Excluded from a ranking is not deleted. The row, its status and its number
    # are all still there for history and for the stock page.
    _store(session, "STALE", quarter_fields={"age_days": 900})
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    stored = ScoreSnapshotRepository(session).list_scored(
        score_version=CURRENT_SCORE_VERSION, rank_eligible_only=False
    )
    assert [company.ticker for _, company in stored] == ["STALE"]
    assert stored[0][0].final_score is not None
    assert stored[0][0].freshness == "STALE"


@pytest.mark.integration
def test_a_snapshot_predating_freshness_still_ranks(session: Session) -> None:
    # Every V1.1 row has NULL here, and was ranked under rules where freshness
    # did not bear on ranking. Reading NULL as stale would empty the history.
    company_id = _store(session, "OLD")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    session.execute(
        text("UPDATE score_snapshots SET freshness = NULL WHERE company_id = :id"),
        {"id": company_id},
    )
    session.flush()

    assert [row.ticker for row in top_opportunities(session)] == ["OLD"]
    detail = latest_score(session, "OLD")
    assert detail is not None
    assert detail.freshness == "CURRENT"
    assert detail.rank_eligible is True
