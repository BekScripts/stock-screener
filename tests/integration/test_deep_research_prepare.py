"""Single-stock deep research preparation, against a real database.

`integration`: the workflow's whole point is that it drives the existing
ingestion and scoring passes over stored rows, so a test with a mocked session
would prove nothing about whether the score it produced is the one the scanner
would have produced.

Providers are fakes. Nothing here reaches a network, and there is no research
provider anywhere in these tests because this path must not have one.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from api_clients import MockFundamentals, MockMarketData, ProviderAuthError
from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    FinancialSnapshotRepository,
    ScoreSnapshotRepository,
)
from domain import (
    CURRENT_SCORE_VERSION,
    CompanyProfile,
    Filing,
    FilingExcerpt,
    PriceBar,
    ScoringStatus,
)
from stock_screener.deep_research import (
    PreparationError,
    Stage,
    StageState,
    assemble_deep_brief,
    prepare_company,
)
from stock_screener.scoring import score_market

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from stock_screener.config import Settings

TICKER = "ACME"
LATEST = date(2026, 6, 30)
ACCESSION = "0000320193-26-000073"


def _profile(ticker: str = TICKER) -> CompanyProfile:
    """A listing that clears every eligibility threshold."""
    return CompanyProfile(
        ticker=ticker,
        name=f"{ticker} Corporation",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        market_cap=1_200_000_000.0,
    )


def _filing() -> Filing:
    """One periodic filing worth reading."""
    return Filing(
        accession=ACCESSION,
        form="10-Q",
        filed=date(2026, 5, 2),
        period_end=date(2026, 3, 31),
        url="https://www.sec.gov/Archives/edgar/data/1/x.htm",
    )


def _excerpt() -> FilingExcerpt:
    """Quotable text from that filing."""
    return FilingExcerpt(
        accession=ACCESSION,
        form="10-Q",
        filed=date(2026, 5, 2),
        section="mda",
        text="Revenue grew on continued demand for the Company's platform products.",
        url="https://www.sec.gov/Archives/edgar/data/1/x.htm",
        source="sec-edgar",
    )


class RecordingFundamentals(MockFundamentals):
    """A fundamentals fake that remembers the order it was called in."""

    def __init__(self, calls: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = calls

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        self.calls.append("profile")
        return super().get_company_profile(ticker)

    def get_financial_statements(self, ticker: str, limit: int = 20) -> Any:
        self.calls.append("fundamentals")
        return super().get_financial_statements(ticker, limit)

    def get_filings(self, ticker: str, limit: int = 8) -> Any:
        self.calls.append("filings")
        return super().get_filings(ticker, limit)

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> Any:
        self.calls.append("filing_text")
        return super().get_filing_excerpts(ticker, filing)


class RecordingMarketData(MockMarketData):
    """A market-data fake that remembers the order it was called in."""

    def __init__(self, calls: list[str], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = calls

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        self.calls.append("market_data")
        return super().get_daily_prices(ticker, start, end)

    def get_daily_prices_batch(
        self, tickers: Sequence[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        self.calls.append("market_data")
        return super().get_daily_prices_batch(tickers, start, end)


@pytest.fixture
def calls() -> list[str]:
    """The shared call log every fake appends to."""
    return []


@pytest.fixture
def fakes(calls: list[str], make: type[Make]) -> tuple[RecordingMarketData, RecordingFundamentals]:
    """Providers serving one fully-covered company, plus a benchmark series."""
    profile = _profile()
    bars = make.bars(sessions=400)
    market = RecordingMarketData(
        calls,
        [profile],
        {TICKER: bars, "SPY": bars},
    )
    fundamentals = RecordingFundamentals(
        calls,
        profiles={TICKER: profile},
        statements={TICKER: make.quarters(count=8)},
        filings={TICKER: [_filing()]},
        excerpts={TICKER: [_excerpt()]},
    )
    return market, fundamentals


def _prepare(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
    ticker: str = TICKER,
) -> Any:
    """Run preparation with the fake providers, pinned to a fixed date."""
    market, fundamentals = fakes
    return prepare_company(session, settings, market, fundamentals, ticker, today=LATEST)


@pytest.mark.integration
def test_prepares_only_the_requested_company(
    session: Session,
    settings: Settings,
    make: type[Make],
    seed: type[Seed],
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """A single-stock run must not touch the rest of the universe."""
    seed.company(session, make, "OTHR")
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    _prepare(session, settings, fakes)

    other = CompanyRepository(session).get_by_ticker("OTHR")
    assert other is not None
    assert (
        ScoreSnapshotRepository(session).latest_for_company(
            other.id, score_version=CURRENT_SCORE_VERSION
        )
        is None
    )


@pytest.mark.integration
def test_normalises_the_requested_ticker(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes, ticker="  acme  ")

    assert result.ticker == TICKER


@pytest.mark.integration
def test_market_data_and_fundamentals_are_refreshed_before_scoring(
    session: Session,
    settings: Settings,
    calls: list[str],
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics are computed from stored prices and statements, so both must land first."""

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("score")
        return score_market(*args, **kwargs)

    monkeypatch.setattr("stock_screener.deep_research.preparation.score_market", spy)
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    _prepare(session, settings, fakes)

    assert calls.index("market_data") < calls.index("score")
    assert calls.index("fundamentals") < calls.index("score")


@pytest.mark.integration
def test_sec_evidence_is_prepared_after_scoring_and_before_assembly(
    session: Session,
    settings: Settings,
    calls: list[str],
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("score")
        return score_market(*args, **kwargs)

    monkeypatch.setattr("stock_screener.deep_research.preparation.score_market", spy)
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)
    before_assembly = list(calls)
    assemble_deep_brief(session, settings, result.ticker, preparation=result)

    assert "filings" in before_assembly
    assert "filing_text" in before_assembly
    assert calls == before_assembly


@pytest.mark.integration
def test_the_assembler_makes_no_provider_call(
    session: Session,
    settings: Settings,
    calls: list[str],
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """Assembly is a pure read. A fetch here would make a brief depend on when it ran."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    result = _prepare(session, settings, fakes)
    calls.clear()

    assemble_deep_brief(session, settings, result.ticker, preparation=result)

    assert calls == []


@pytest.mark.integration
def test_the_stages_run_in_dependency_order(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)

    assert [outcome.stage for outcome in result.outcomes] == [
        Stage.PROFILE,
        Stage.MARKET_DATA,
        Stage.FUNDAMENTALS,
        Stage.BENCHMARK,
        Stage.SCORE,
        Stage.FILINGS,
        Stage.FILING_TEXT,
    ]


@pytest.mark.integration
def test_uses_the_existing_compounder_score_rather_than_a_second_one(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """The snapshot must be an ordinary one, indistinguishable from a nightly run's."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)

    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        result.company_id, score_version=CURRENT_SCORE_VERSION
    )
    assert snapshot is not None
    assert snapshot.score_version == CURRENT_SCORE_VERSION
    assert snapshot.score_date == LATEST


@pytest.mark.integration
def test_a_ticker_the_provider_does_not_know_is_fatal(
    session: Session, settings: Settings, calls: list[str]
) -> None:
    market = RecordingMarketData(calls, [], {})
    fundamentals = RecordingFundamentals(calls)

    with pytest.raises(PreparationError, match="does not recognise it"):
        prepare_company(session, settings, market, fundamentals, "NOPE", today=LATEST)


@pytest.mark.integration
def test_an_unstored_ticker_the_provider_knows_is_added_without_a_universe_refresh(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """The one place a per-stock run reaches past the scanner's passes."""
    assert CompanyRepository(session).get_by_ticker(TICKER) is None

    result = _prepare(session, settings, fakes)

    stored = CompanyRepository(session).get_by_ticker(TICKER)
    assert stored is not None
    profile_stage = result.outcome(Stage.PROFILE)
    assert profile_stage is not None
    assert profile_stage.state is StageState.REFRESHED
    assert CompanyRepository(session).count() == 1


@pytest.mark.integration
def test_an_optional_provider_failure_degrades_rather_than_stops(
    session: Session, settings: Settings, calls: list[str], make: type[Make]
) -> None:
    """A quota-exhausted vendor must not cost a well-covered company its brief."""
    profile = _profile()
    bars = make.bars(sessions=400)
    market = RecordingMarketData(calls, [profile], {TICKER: bars, "SPY": bars})
    fundamentals = RecordingFundamentals(
        calls,
        profiles={TICKER: profile},
        statements={TICKER: make.quarters(count=8)},
        failing_tickers=[TICKER],
    )
    CompanyRepository(session).upsert_profile(profile)
    session.flush()

    result = prepare_company(session, settings, market, fundamentals, TICKER, today=LATEST)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert brief is not None
    assert result.degraded
    assert brief.freshness.stale


@pytest.mark.integration
def test_a_company_without_enough_data_still_produces_a_brief(
    session: Session, settings: Settings, calls: list[str], make: type[Make]
) -> None:
    """No fabricated score. The brief explains the status instead."""
    profile = _profile()
    market = RecordingMarketData(calls, [profile], {TICKER: make.bars(sessions=3)})
    fundamentals = RecordingFundamentals(
        calls, profiles={TICKER: profile}, statements={TICKER: make.quarters(count=1)}
    )
    CompanyRepository(session).upsert_profile(profile)
    session.flush()

    result = prepare_company(session, settings, market, fundamentals, TICKER, today=LATEST)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert brief is not None
    assert brief.score.scoring_status is not ScoringStatus.SCORED
    assert brief.score.final_score is None


@pytest.mark.integration
def test_the_brief_carries_the_sec_evidence_preparation_stored(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert brief is not None
    assert [reference.id for reference in brief.filings] == [f"D.{ACCESSION}"]
    assert [text.id for text in brief.excerpts] == [f"X.{ACCESSION}.mda"]


@pytest.mark.integration
def test_the_brief_has_no_external_evidence_in_phase_6b(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """Collection does not exist yet, and the brief must not pretend otherwise."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert brief is not None
    assert brief.external == ()
    assert brief.external_ids == frozenset()


@pytest.mark.integration
def test_freshness_reports_the_dates_the_stored_evidence_reaches(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert brief is not None
    freshness = brief.freshness
    assert freshness.price_as_of == LATEST
    assert freshness.fundamentals_through is not None
    assert freshness.filings_through == date(2026, 5, 2)
    assert freshness.excerpts_through == date(2026, 5, 2)
    assert freshness.refreshed_at is not None


@pytest.mark.integration
def test_freshness_separates_refreshed_from_reused(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """Preparing twice: the second run finds everything current and says so."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    _prepare(session, settings, fakes)

    second = _prepare(session, settings, fakes)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=second)

    assert brief is not None
    assert "fundamentals" in brief.freshness.reused
    assert "score" in brief.freshness.refreshed


@pytest.mark.integration
def test_the_benchmark_is_reused_when_it_is_already_current(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """One shared series; re-fetching it per single-stock run would buy nothing."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    _prepare(session, settings, fakes)

    second = _prepare(session, settings, fakes)

    outcome = second.outcome(Stage.BENCHMARK)
    assert outcome is not None
    assert outcome.state is StageState.SKIPPED


@pytest.mark.integration
def test_a_stale_benchmark_is_refreshed(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()

    result = _prepare(session, settings, fakes)

    outcome = result.outcome(Stage.BENCHMARK)
    assert outcome is not None
    assert outcome.state is StageState.REFRESHED
    assert BenchmarkPriceRepository(session).latest_date("SPY") is not None


@pytest.mark.integration
def test_the_deterministic_fingerprint_is_stable_when_nothing_changed(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """Assembling twice over unchanged data is the cache hit the design exists for."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    result = _prepare(session, settings, fakes)

    first = assemble_deep_brief(session, settings, TICKER, preparation=result)
    second = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert first is not None
    assert second is not None
    assert first.deterministic_fingerprint() == second.deterministic_fingerprint()
    assert first.evidence_fingerprint() == second.evidence_fingerprint()


@pytest.mark.integration
def test_the_deterministic_fingerprint_moves_when_the_evidence_moves(
    session: Session,
    settings: Settings,
    make: type[Make],
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    result = _prepare(session, settings, fakes)
    before = assemble_deep_brief(session, settings, TICKER, preparation=result)

    stored = CompanyRepository(session).get_by_ticker(TICKER)
    assert stored is not None
    FinancialSnapshotRepository(session).upsert_periods(
        stored.id, make.quarters(count=8, revenue=250_000_000.0)
    )
    session.flush()
    after = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert before is not None
    assert after is not None
    assert before.deterministic_fingerprint() != after.deterministic_fingerprint()
    assert before.evidence_fingerprint() != after.evidence_fingerprint()


@pytest.mark.integration
def test_the_external_fingerprint_is_unchanged_by_a_deterministic_refresh(
    session: Session,
    settings: Settings,
    make: type[Make],
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """Empty stays empty, and hashes the same, however much the company moves."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    result = _prepare(session, settings, fakes)
    before = assemble_deep_brief(session, settings, TICKER, preparation=result)

    stored = CompanyRepository(session).get_by_ticker(TICKER)
    assert stored is not None
    FinancialSnapshotRepository(session).upsert_periods(
        stored.id, make.quarters(count=8, revenue=250_000_000.0)
    )
    session.flush()
    after = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert before is not None
    assert after is not None
    assert before.external_fingerprint() == after.external_fingerprint()


@pytest.mark.integration
def test_the_brief_records_where_the_company_ranks(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    result = _prepare(session, settings, fakes)

    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    assert brief is not None
    assert brief.ranking is not None
    assert brief.ranking.universe_size == 1


@pytest.mark.integration
def test_assembling_without_a_preparation_record_still_reports_dates(
    session: Session,
    settings: Settings,
    fakes: tuple[RecordingMarketData, RecordingFundamentals],
) -> None:
    """Reading a previously prepared company claims nothing about a run."""
    CompanyRepository(session).upsert_profile(_profile())
    session.flush()
    _prepare(session, settings, fakes)

    brief = assemble_deep_brief(session, settings, TICKER)

    assert brief is not None
    assert brief.freshness.price_as_of == LATEST
    assert brief.freshness.refreshed_at is None
    assert brief.freshness.refreshed == ()


@pytest.mark.integration
def test_an_unknown_company_assembles_no_brief(session: Session, settings: Settings) -> None:
    assert assemble_deep_brief(session, settings, "GHOST") is None


@pytest.mark.integration
def test_a_stored_but_unscored_company_assembles_no_brief(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    seed.company(session, make, "UNSC")

    assert assemble_deep_brief(session, settings, "UNSC") is None


@pytest.mark.integration
def test_no_research_provider_is_reachable_from_this_path() -> None:
    """Phase 6B must not be able to call a model, even by accident."""
    import stock_screener.deep_research.brief as brief_module
    import stock_screener.deep_research.preparation as preparation_module

    for module in (preparation_module, brief_module):
        source = module.__doc__ or ""
        assert "ResearchProvider" not in source
        assert not hasattr(module, "build_research_provider")
        assert not hasattr(module, "research_company")


class RejectingMarketData(RecordingMarketData):
    """A market-data fake whose credentials are refused."""

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        self.calls.append("market_data")
        raise ProviderAuthError("credentials rejected")

    def get_daily_prices_batch(
        self, tickers: Sequence[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        self.calls.append("market_data")
        raise ProviderAuthError("credentials rejected")


class RejectingFundamentals(RecordingFundamentals):
    """A fundamentals fake whose credentials are refused after the profile lookup.

    The profile still answers, so the company resolves and the run reaches the
    stages under test rather than failing at the first one.
    """

    def get_financial_statements(self, ticker: str, limit: int = 20) -> Any:
        self.calls.append("fundamentals")
        raise ProviderAuthError("credentials rejected")

    def get_filings(self, ticker: str, limit: int = 8) -> Any:
        self.calls.append("filings")
        raise ProviderAuthError("credentials rejected")

    def get_filing_excerpts(self, ticker: str, filing: Filing) -> Any:
        self.calls.append("filing_text")
        raise ProviderAuthError("credentials rejected")


@pytest.mark.integration
def test_a_provider_rejecting_credentials_degrades_every_stage_but_still_yields_a_brief(
    session: Session, settings: Settings, calls: list[str], make: type[Make], seed: type[Seed]
) -> None:
    """A misconfigured provider must not cost a stored company its brief.

    The underlying passes re-raise an auth error rather than counting it, because
    market-wide it fails identically for every company. For one stock the honest
    answer is the stored evidence plus a note saying what could not be checked.
    """
    company_id = seed.company(session, make, TICKER)
    seed.score(session, company_id, TICKER)
    session.flush()

    market = RejectingMarketData(calls, [_profile()], {})
    fundamentals = RejectingFundamentals(calls, profiles={TICKER: _profile()})

    result = prepare_company(session, settings, market, fundamentals, TICKER, today=LATEST)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    degraded = {outcome.stage for outcome in result.degraded}
    assert Stage.MARKET_DATA in degraded
    assert Stage.FUNDAMENTALS in degraded
    assert Stage.FILINGS in degraded
    assert brief is not None
    assert len(brief.freshness.stale) == len(result.degraded)


@pytest.mark.integration
def test_a_benchmark_provider_failure_degrades_rather_than_stops(
    session: Session, settings: Settings, calls: list[str], make: type[Make]
) -> None:
    """Without a benchmark the company scores INSUFFICIENT_DATA — still a brief."""
    profile = _profile()
    market = RejectingMarketData(calls, [profile], {})
    fundamentals = RecordingFundamentals(
        calls, profiles={TICKER: profile}, statements={TICKER: make.quarters(count=8)}
    )
    CompanyRepository(session).upsert_profile(profile)
    session.flush()

    result = prepare_company(session, settings, market, fundamentals, TICKER, today=LATEST)

    outcome = result.outcome(Stage.BENCHMARK)
    assert outcome is not None
    assert outcome.state is StageState.DEGRADED


@pytest.mark.integration
def test_a_provider_that_cannot_be_reached_to_resolve_a_ticker_is_fatal(
    session: Session, settings: Settings, calls: list[str]
) -> None:
    """An unknown ticker plus an unreachable provider leaves nothing to prepare."""
    market = RecordingMarketData(calls, [], {})
    fundamentals = RecordingFundamentals(calls, failing_tickers=["NOPE"])

    with pytest.raises(PreparationError, match="could not be reached to resolve it"):
        prepare_company(session, settings, market, fundamentals, "NOPE", today=LATEST)


@pytest.mark.integration
def test_filing_text_failing_leaves_the_brief_without_excerpts_rather_than_failing(
    session: Session, settings: Settings, calls: list[str], make: type[Make]
) -> None:
    """No readable filing is degraded, not fatal — those sections answer UNKNOWN."""
    profile = _profile()
    bars = make.bars(sessions=400)
    market = RecordingMarketData(calls, [profile], {TICKER: bars, "SPY": bars})

    class NoText(RecordingFundamentals):
        def get_filing_excerpts(self, ticker: str, filing: Filing) -> Any:
            self.calls.append("filing_text")
            raise ProviderAuthError("credentials rejected")

    fundamentals = NoText(
        calls,
        profiles={TICKER: profile},
        statements={TICKER: make.quarters(count=8)},
        filings={TICKER: [_filing()]},
    )
    CompanyRepository(session).upsert_profile(profile)
    session.flush()

    result = prepare_company(session, settings, market, fundamentals, TICKER, today=LATEST)
    brief = assemble_deep_brief(session, settings, TICKER, preparation=result)

    outcome = result.outcome(Stage.FILING_TEXT)
    assert outcome is not None
    assert outcome.state is StageState.DEGRADED
    assert brief is not None
    assert brief.excerpts == ()
    assert brief.filings
