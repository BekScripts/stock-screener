"""The second pass: verify the top candidates, then re-score them.

What these protect is the shape of the workflow rather than any number. A broad
scan on free data must rank the market; a metered provider must be able to fail,
run out of quota, or be absent entirely without taking the ranking with it; and
what enrichment changes must be the *inputs*, never the formula.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from api_clients import ProviderDataError, ProviderRateLimitError
from data_access import (
    FINAL,
    PRELIMINARY,
    BenchmarkPriceRepository,
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ScoreSnapshotRepository,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyProfile,
    Filing,
    FilingExcerpt,
    FinancialPeriod,
    MarketCapSource,
    PriceBar,
    ScoringStatus,
)
from stock_screener.config import Settings
from stock_screener.scoring import (
    EnrichmentStatus,
    enrich_candidates,
    score_market,
    top_opportunities,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SETTINGS = Settings(environment="test")
TODAY = date(2026, 6, 30)
QUARTERS = 16
PRICE_DAYS = 400


class FakeProfiles:
    """A profile source with a request budget, like a metered plan.

    Args:
        profiles: What to return per ticker. A ticker absent from the mapping is
            a coverage gap, which is not a failure.
        quota: Requests served before the account starts refusing.
        failing: Tickers that raise a non-quota error.
    """

    def __init__(
        self,
        profiles: dict[str, CompanyProfile] | None = None,
        *,
        quota: int = 1_000,
        failing: tuple[str, ...] = (),
    ) -> None:
        self._profiles = profiles or {}
        self._quota = quota
        self._failing = set(failing)
        self.requests: list[str] = []

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return a profile, refusing once the budget is spent."""
        self.requests.append(ticker)
        if len(self.requests) > self._quota:
            raise ProviderRateLimitError("fmp rate limit reached (HTTP 429)")
        if ticker in self._failing:
            raise ProviderDataError(f"provider failed for {ticker}")
        return self._profiles.get(ticker)

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Never called: enrichment buys profiles, not statements."""
        raise AssertionError("enrichment must not re-fetch statements")

    def get_filings(self, ticker: str, limit: int = 8) -> list[Filing]:
        """Never called: enrichment buys profiles, not filings."""
        raise AssertionError("enrichment must not fetch filings")

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> list[FilingExcerpt]:
        """Never called: enrichment buys profiles, not documents."""
        raise AssertionError("enrichment must not fetch filing documents")


def _bars(*, annual_return: float, end_price: float = 40.0) -> list[PriceBar]:
    """Daily bars compounding to `annual_return` over a year."""
    daily = (1 + annual_return) ** (1 / 365)
    return [
        PriceBar(
            date=TODAY - timedelta(days=offset),
            open=(price := end_price / daily**offset),
            high=price * 1.001,
            low=price * 0.999,
            close=price,
            volume=volume,
        )
        for offset, volume in ((offset, 200_000.0) for offset in reversed(range(PRICE_DAYS)))
    ]


def _quarters(
    *, growth: float, shares: float, reported_currency: str | None = None
) -> list[FinancialPeriod]:
    """Sixteen quarters with a cover-page share count on every one.

    `reported_currency` is the filing's own unit. It lives here rather than on
    the profile because that is where the metric engine reads it from — the
    money a company reports in is a property of its statements, not of the
    vendor record describing its listing.
    """
    return [
        FinancialPeriod(
            period_end=TODAY - timedelta(days=91 * (QUARTERS - 1 - index)),
            revenue=(revenue := 100_000_000.0 * (1 + growth) ** (index / 4)),
            gross_profit=revenue * 0.62,
            operating_income=revenue * 0.10,
            free_cash_flow=revenue * 0.15,
            cash=300_000_000.0,
            total_debt=40_000_000.0,
            shares_outstanding=shares * 0.98,
            common_shares_outstanding=shares,
            reported_currency=reported_currency,
            source="test",
        )
        for index in range(QUARTERS)
    ]


def _store(
    session: Session,
    ticker: str,
    *,
    growth: float = 0.45,
    shares: float = 50_000_000.0,
    annual_return: float = 0.40,
    end_price: float = 40.0,
    profile_fields: dict[str, Any] | None = None,
    reported_currency: str | None = None,
) -> int:
    """Store one company with no provider market cap, as the broad scan sees it."""
    companies = CompanyRepository(session)
    fields: dict[str, Any] = {
        "ticker": ticker,
        "name": f"{ticker} Corp",
        "exchange": "NASDAQ",
        "industry": "Services-Prepackaged Software",
        **(profile_fields or {}),
    }
    companies.upsert_profile(CompanyProfile(**fields))
    session.flush()
    stored = companies.get_by_ticker(ticker)
    assert stored is not None

    FinancialSnapshotRepository(session).upsert_periods(
        stored.id,
        _quarters(growth=growth, shares=shares, reported_currency=reported_currency),
    )
    PriceHistoryRepository(session).upsert_bars(
        stored.id, _bars(annual_return=annual_return, end_price=end_price)
    )
    session.flush()
    return stored.id


def _store_benchmark(session: Session) -> None:
    """Store the benchmark series relative strength is measured against."""
    BenchmarkPriceRepository(session).upsert_bars(
        SETTINGS.benchmark_symbol, _bars(annual_return=0.10, end_price=500.0)
    )
    session.flush()


@pytest.mark.integration
def test_a_company_no_provider_covers_is_ranked_on_filings_alone(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    rows = top_opportunities(session)
    assert [row.ticker for row in rows] == ["XYZ"]
    assert rows[0].market_cap_source == MarketCapSource.CALCULATED.value
    assert rows[0].ranking_state == PRELIMINARY
    # 50M shares at $40 — the price the bars end on.
    assert rows[0].market_cap == pytest.approx(2_000_000_000.0, rel=1e-6)


@pytest.mark.integration
def test_partial_volume_does_not_exclude_a_company_from_the_broad_scan(
    session: Session,
) -> None:
    # 200k shares a day at $40 is $8M of single-exchange volume, which says
    # nothing about consolidated volume — so the threshold is not applied.
    _store(session, "THIN", end_price=2.5)
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    rows = top_opportunities(session)
    assert [row.ticker for row in rows] == ["THIN"]
    assert "LIQUIDITY_UNVERIFIED" in rows[0].warnings


@pytest.mark.integration
def test_enrichment_replaces_inputs_and_marks_the_row_final(session: Session) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    provider = FakeProfiles(
        {
            "XYZ": CompanyProfile(
                ticker="XYZ",
                name="XYZ Corp",
                sector="Technology",
                industry="Software - Application",
                market_cap=2_100_000_000.0,
                average_volume=1_000_000.0,
            )
        }
    )
    report = enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()

    assert report.status == EnrichmentStatus.COMPLETE
    assert report.succeeded == 1
    assert report.rescored == 1

    row = top_opportunities(session)[0]
    assert row.ranking_state == FINAL
    assert row.market_cap_source == MarketCapSource.PROVIDER.value
    assert row.market_cap == 2_100_000_000.0
    assert row.volume_basis == "CONSOLIDATED"


@pytest.mark.integration
def test_verified_liquidity_can_drop_a_company_from_the_final_ranking(
    session: Session,
) -> None:
    # The case the two-pass design exists for: the company looked liquid enough
    # on partial volume, and consolidated volume says otherwise.
    _store(session, "THIN")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()
    assert [row.ticker for row in top_opportunities(session)] == ["THIN"]

    provider = FakeProfiles(
        {
            "THIN": CompanyProfile(
                ticker="THIN",
                name="THIN Corp",
                market_cap=2_000_000_000.0,
                # $320k a day across every venue, against a $1M threshold.
                average_volume=8_000.0,
            )
        }
    )
    enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()

    assert top_opportunities(session) == []
    company = CompanyRepository(session).get_by_ticker("THIN")
    assert company is not None
    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        company.id, score_version=CURRENT_SCORE_VERSION
    )
    assert snapshot is not None
    assert snapshot.scoring_status == ScoringStatus.NOT_ELIGIBLE.value


@pytest.mark.integration
def test_quota_exhaustion_stops_the_pass_without_losing_what_it_did(
    session: Session,
) -> None:
    for ticker, growth in (("AAA", 0.60), ("BBB", 0.50), ("CCC", 0.40)):
        _store(session, ticker, growth=growth)
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    profiles = {
        ticker: CompanyProfile(
            ticker=ticker,
            name=f"{ticker} Corp",
            market_cap=2_000_000_000.0,
            average_volume=900_000.0,
        )
        for ticker in ("AAA", "BBB", "CCC")
    }
    provider = FakeProfiles(profiles, quota=1)

    report = enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()

    assert report.status == EnrichmentStatus.PARTIAL
    assert report.succeeded == 1
    assert report.rate_limited == 1
    assert report.skipped == 1
    # Stopped rather than working through the rest into the same wall.
    assert len(provider.requests) == 2

    states = {row.ticker: row.ranking_state for row in top_opportunities(session)}
    assert states["AAA"] == FINAL
    assert states["BBB"] == PRELIMINARY
    assert states["CCC"] == PRELIMINARY


@pytest.mark.integration
def test_a_provider_failure_costs_one_company_not_the_pass(session: Session) -> None:
    for ticker, growth in (("AAA", 0.60), ("BBB", 0.50)):
        _store(session, ticker, growth=growth)
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    provider = FakeProfiles(
        {
            "BBB": CompanyProfile(
                ticker="BBB", name="BBB Corp", market_cap=1_500_000_000.0, average_volume=900_000.0
            )
        },
        failing=("AAA",),
    )
    report = enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()

    assert report.failed == 1
    assert report.succeeded == 1
    assert report.status == EnrichmentStatus.PARTIAL


@pytest.mark.integration
def test_a_ticker_the_provider_does_not_cover_is_a_gap_not_a_failure(
    session: Session,
) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    report = enrich_candidates(session, FakeProfiles({}), SETTINGS, score_date=TODAY)

    assert report.uncovered == 1
    assert report.failed == 0
    assert report.rescored == 0


@pytest.mark.integration
def test_enrichment_spends_no_more_requests_than_its_limit(session: Session) -> None:
    for index in range(5):
        _store(session, f"T{index}", growth=0.60 - index * 0.05)
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    provider = FakeProfiles({})
    enrich_candidates(session, provider, SETTINGS, score_date=TODAY, limit=2)

    assert len(provider.requests) == 2


@pytest.mark.integration
def test_enrichment_before_any_scoring_does_nothing(session: Session) -> None:
    provider = FakeProfiles({})

    report = enrich_candidates(session, provider, SETTINGS)

    assert report.status == EnrichmentStatus.NOT_RUN
    assert provider.requests == []


@pytest.mark.integration
def test_the_re_score_uses_the_same_formula(session: Session) -> None:
    # Enrichment changes inputs, never the rules. With a provider market cap
    # equal to the calculated one and consolidated volume above the threshold,
    # the score must come back identical.
    _store(session, "XYZ")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()
    before = top_opportunities(session)[0]

    provider = FakeProfiles(
        {
            "XYZ": CompanyProfile(
                ticker="XYZ",
                name="XYZ Corp",
                industry="Services-Prepackaged Software",
                market_cap=before.market_cap,
                average_volume=1_000_000.0,
            )
        }
    )
    enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()
    after = top_opportunities(session)[0]

    assert after.final_score == before.final_score
    assert after.growth_score == before.growth_score
    assert after.valuation_score == before.valuation_score
    assert after.ranking_state == FINAL


@pytest.mark.integration
def test_a_market_cap_disagreement_is_recorded_rather_than_reconciled(
    session: Session,
) -> None:
    _store(session, "XYZ")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    provider = FakeProfiles(
        {
            "XYZ": CompanyProfile(
                ticker="XYZ",
                name="XYZ Corp",
                # Double the calculated figure: a second share class, or a
                # count that predates an issuance.
                market_cap=4_000_000_000.0,
                average_volume=1_000_000.0,
            )
        }
    )
    report = enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()

    assert report.discrepancies == ["XYZ"]
    row = top_opportunities(session)[0]
    # The provider's figure is used, not an average of the two.
    assert row.market_cap == 4_000_000_000.0
    assert row.market_cap_source == MarketCapSource.PROVIDER.value


@pytest.mark.integration
def test_a_bank_is_kept_out_of_the_ranking_without_a_commercial_sector_feed(
    session: Session,
) -> None:
    # The SIC description EDGAR supplies is what makes this work when no vendor
    # sector is available.
    _store(session, "XYZ")
    _store(session, "BNK", profile_fields={"industry": "State Commercial Banks"})
    _store_benchmark(session)

    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    assert [row.ticker for row in top_opportunities(session)] == ["XYZ"]


@pytest.mark.integration
def test_a_profile_with_nothing_to_verify_does_not_mark_a_row_final(
    session: Session,
) -> None:
    # EDGAR's profile carries identity only. Treating that as enrichment would
    # make FINAL mean "we asked", not "we checked".
    _store(session, "XYZ")
    _store_benchmark(session)
    score_market(session, SETTINGS, score_date=TODAY)
    session.flush()

    provider = FakeProfiles({"XYZ": CompanyProfile(ticker="XYZ", name="XYZ Corp")})
    report = enrich_candidates(session, provider, SETTINGS, score_date=TODAY)
    session.flush()

    assert report.succeeded == 0
    assert report.uncovered == 1
    assert top_opportunities(session)[0].ranking_state == PRELIMINARY


@pytest.mark.integration
def test_enrichment_keeps_a_foreign_issuers_valuation_intact(session: Session) -> None:
    # The regression this exists for: enrichment re-scores through the same
    # `build_scores` the market-wide pass uses, but built its own call without an
    # FX resolver. A company filing in euros would come out of the broad pass with
    # a complete valuation and go into the database as FINAL with none of it —
    # because `market_cap_for_ratios` is None with no rate, and FINAL overwrites
    # PRELIMINARY. Verification made the score worse for exactly the companies it
    # was verifying.
    fx_settings = Settings(environment="test", fx_provider="mock")
    _store(
        session,
        "EURO",
        profile_fields={"reporting_currency": "EUR", "quote_currency": "USD"},
        reported_currency="EUR",
    )
    _store_benchmark(session)
    score_market(session, fx_settings, score_date=TODAY)
    session.flush()

    before = top_opportunities(session)[0]
    assert before.ranking_state == PRELIMINARY
    assert before.enterprise_value is not None
    assert before.valuation_score is not None

    provider = FakeProfiles(
        {
            "EURO": CompanyProfile(
                ticker="EURO",
                name="EURO Corp",
                industry="Services-Prepackaged Software",
                market_cap=2_100_000_000.0,
                average_volume=1_000_000.0,
                quote_currency="USD",
            )
        }
    )
    enrich_candidates(session, provider, fx_settings, score_date=TODAY)
    session.flush()

    # Without a resolver this list is empty: valuation loses its required
    # multiple, the company scores INSUFFICIENT_DATA, and verifying it has
    # dropped it out of the ranking altogether.
    ranked = top_opportunities(session)
    assert [row.ticker for row in ranked] == ["EURO"]

    after = ranked[0]
    assert after.ranking_state == FINAL
    assert after.market_cap_source == MarketCapSource.PROVIDER.value
    # The point of the test: still valued, and valued on the balance sheet.
    assert after.enterprise_value is not None
    assert after.valuation_basis == "EV_TO_REVENUE"
    assert after.data_coverage == before.data_coverage
