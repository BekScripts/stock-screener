"""The deep research runner: what it spends, and what it refuses to spend it on.

`integration`: the cache lives in the database, so the property that matters
most — identical evidence costs nothing — can only be shown against real rows.
Every provider here is a fake. Nothing in this file reaches a network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from api_clients import MockDeepResearch, MockExternalResearch
from data_access import (
    EXTERNAL_FRESH,
    EXTERNAL_REUSED,
    DeepResearchReportRepository,
    FilingExcerptRepository,
)
from deep_research import DeepBasis, DeepDraftClaim, DeepResearchDraft, DeepSection
from domain import ExternalSearchResult, FilingExcerpt
from stock_screener.deep_research import DeepResearchRun, RunStatus, run_deep_research

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from stock_screener.config import Settings

TICKER = "ACME"
ACCESSION = "0000320193-26-000073"


def _draft(*claims: DeepDraftClaim) -> DeepResearchDraft:
    return DeepResearchDraft(ticker=TICKER, claims=claims)


def _good_claim() -> DeepDraftClaim:
    return DeepDraftClaim(
        section=DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        text="Growth contributed 30.0 of a possible 35.0 points.",
        basis=DeepBasis.DETERMINISTIC,
        evidence=("S.growth",),
    )


@pytest.fixture
def scored(session: Session, make: type[Make], seed: type[Seed]) -> int:
    """A scored company with one filing excerpt, ready to research."""
    company_id = seed.company(session, make, TICKER)
    seed.score(session, company_id, TICKER)
    FilingExcerptRepository(session).upsert_excerpts(
        company_id,
        [
            FilingExcerpt(
                accession=ACCESSION,
                form="10-Q",
                section="mda",
                text="Revenue grew on continued demand for the Company's platform.",
                filed=date(2026, 5, 2),
                url="https://sec.gov/x",
                source="sec-edgar",
            )
        ],
    )
    session.flush()
    return company_id


def _run(
    session: Session,
    settings: Settings,
    *,
    synthesis: MockDeepResearch | None = None,
    external: MockExternalResearch | None = None,
) -> DeepResearchRun:
    """Run deep research with fakes, reusing stored deterministic state."""
    return run_deep_research(
        session,
        settings,
        synthesis=synthesis or MockDeepResearch(_draft(_good_claim())),
        external=external,
        ticker=TICKER,
        refresh=False,
    )


@pytest.mark.integration
def test_a_clean_draft_is_validated_and_persisted(
    session: Session, settings: Settings, scored: int
) -> None:
    run = _run(session, settings)

    assert run.status is RunStatus.VALIDATION_PARTIAL
    assert run.report is not None
    assert run.stored_id is not None
    assert DeepResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_no_raw_draft_is_ever_persisted(session: Session, settings: Settings, scored: int) -> None:
    """Only validation constructs the stored type; a draft has no path to the table."""
    run = _run(session, settings)

    stored = DeepResearchReportRepository(session).latest_for_company(scored)
    assert stored is not None
    assert "claims" not in stored.validated_report_json
    assert "sections" in stored.validated_report_json
    assert run.report is not None


@pytest.mark.integration
def test_identical_evidence_costs_nothing_the_second_time(
    session: Session, settings: Settings, scored: int
) -> None:
    """The cache is consulted before the provider, which is the whole point."""
    first_provider = MockDeepResearch(_draft(_good_claim()))
    _run(session, settings, synthesis=first_provider)
    session.flush()

    second_provider = MockDeepResearch(_draft(_good_claim()))
    again = _run(session, settings, synthesis=second_provider)

    assert again.status is RunStatus.CACHE_HIT
    assert again.provider_calls == 0
    assert again.estimated_cost_usd == 0.0
    assert second_provider.calls == 0
    assert second_provider.counts == 0
    assert DeepResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_a_cache_hit_still_returns_the_report(
    session: Session, settings: Settings, scored: int
) -> None:
    _run(session, settings)
    session.flush()

    again = _run(session, settings)

    assert again.report is not None
    assert again.succeeded


@pytest.mark.integration
def test_changed_external_evidence_produces_a_second_report(
    session: Session, settings: Settings, scored: int
) -> None:
    """Old reports are retained; a new evidence set is a new reading."""
    _run(session, settings)
    session.flush()

    article = ExternalSearchResult(
        title="Acme signs a multi-year supply agreement with Vertex",
        url="https://reuters.com/business/acme-vertex",
        snippet=(
            "Acme Corporation said it had signed a multi-year supply agreement with "
            "Vertex covering platform components through 2030."
        ),
        published_at=date(2026, 6, 28),
    )
    with_news = _run(session, settings, external=MockExternalResearch({"": [article]}))
    session.flush()

    assert with_news.status is not RunStatus.CACHE_HIT
    assert with_news.provider_calls == 1
    assert DeepResearchReportRepository(session).count() == 2


@pytest.mark.integration
def test_a_changed_prompt_version_invalidates_the_cache(
    session: Session, settings: Settings, scored: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(session, settings)
    session.flush()

    monkeypatch.setattr(
        "stock_screener.deep_research.runner.CURRENT_DEEP_PROMPT_VERSION",
        "DEEP_RESEARCH_PROMPT_V99",
    )
    provider = MockDeepResearch(_draft(_good_claim()))
    again = _run(session, settings, synthesis=provider)

    assert again.status is not RunStatus.CACHE_HIT
    assert provider.calls == 1


@pytest.mark.integration
def test_a_changed_model_invalidates_the_cache(
    session: Session, settings: Settings, scored: int
) -> None:
    """A different model answering the same question is a different reading."""
    _run(session, settings)
    session.flush()

    provider = MockDeepResearch(_draft(_good_claim()))
    again = _run(
        session,
        settings.model_copy(update={"deep_research_model": "claude-opus-5"}),
        synthesis=provider,
    )

    assert again.status is not RunStatus.CACHE_HIT
    assert provider.calls == 1


@pytest.mark.integration
def test_a_prompt_above_the_ceiling_is_refused_before_the_call(
    session: Session, settings: Settings, scored: int
) -> None:
    """No truncation, no spend — and the measured size is reported."""
    provider = MockDeepResearch(_draft(_good_claim()), input_tokens=30_001)

    run = _run(session, settings, synthesis=provider)

    assert run.status is RunStatus.INPUT_TOO_LARGE
    assert run.input_tokens == 30_001
    assert provider.calls == 0
    assert run.estimated_cost_usd == 0.0


@pytest.mark.integration
def test_a_refused_prompt_persists_no_report(
    session: Session, settings: Settings, scored: int
) -> None:
    _run(session, settings, synthesis=MockDeepResearch(_draft(), input_tokens=30_001))

    assert DeepResearchReportRepository(session).count() == 0


@pytest.mark.integration
def test_a_prompt_at_the_ceiling_proceeds(
    session: Session, settings: Settings, scored: int
) -> None:
    provider = MockDeepResearch(_draft(_good_claim()), input_tokens=30_000)

    run = _run(session, settings, synthesis=provider)

    assert run.status is not RunStatus.INPUT_TOO_LARGE
    assert provider.calls == 1


@pytest.mark.integration
def test_a_run_that_would_cross_the_cost_ceiling_is_refused(
    session: Session, settings: Settings, scored: int
) -> None:
    provider = MockDeepResearch(_draft(_good_claim()), input_tokens=25_000)

    run = _run(
        session,
        settings.model_copy(update={"deep_research_max_cost_usd": 0.001}),
        synthesis=provider,
    )

    assert run.status is RunStatus.COST_REFUSED
    assert provider.calls == 0


@pytest.mark.integration
def test_a_provider_failure_persists_nothing(
    session: Session, settings: Settings, scored: int
) -> None:
    """A fabricated report is worse than a missing one."""
    run = _run(session, settings, synthesis=MockDeepResearch(failing="the model is down"))

    assert run.status is RunStatus.PROVIDER_FAILURE
    assert run.report is None
    assert DeepResearchReportRepository(session).count() == 0


@pytest.mark.integration
def test_an_external_search_outage_still_produces_a_report(
    session: Session, settings: Settings, scored: int
) -> None:
    """A Tavily outage costs the external half of the evidence and nothing else."""
    run = _run(
        session,
        settings,
        external=MockExternalResearch(failing="search unavailable"),
    )

    assert run.report is not None
    assert run.degraded
    assert any("external search" in note for note in run.degraded)


@pytest.mark.integration
def test_no_external_makes_no_search_call(
    session: Session, settings: Settings, scored: int
) -> None:
    provider = MockExternalResearch({"": []})

    run = _run(session, settings, external=None)

    assert provider.queries == []
    assert run.report is not None
    assert run.report.external_evidence == ()


@pytest.mark.integration
def test_an_unknown_company_aborts_before_any_spend(session: Session, settings: Settings) -> None:
    provider = MockDeepResearch(_draft(_good_claim()))

    run = run_deep_research(session, settings, synthesis=provider, ticker="GHOST", refresh=False)

    assert run.status is RunStatus.PREPARATION_FAILURE
    assert provider.calls == 0
    assert provider.counts == 0


@pytest.mark.integration
def test_history_is_retained_across_runs(
    session: Session, settings: Settings, scored: int, make: type[Make], seed: type[Seed]
) -> None:
    _run(session, settings)
    session.flush()

    seed.score(session, scored, TICKER, final=91.0, score_date=date(2026, 7, 1))
    session.flush()
    _run(session, settings)
    session.flush()

    repository = DeepResearchReportRepository(session)
    assert repository.count() == 2
    assert len(repository.history_for_company(scored)) == 2


@pytest.mark.integration
def test_the_run_reports_what_it_spent(session: Session, settings: Settings, scored: int) -> None:
    run = _run(
        session,
        settings,
        synthesis=MockDeepResearch(_draft(_good_claim()), input_tokens=1200, output_tokens=800),
    )

    assert run.input_tokens == 1200
    assert run.output_tokens == 800
    assert run.prompt_chars > 0
    assert run.estimated_cost_usd > 0


@pytest.mark.integration
def test_bad_claims_are_dropped_and_the_report_still_persists(
    session: Session, settings: Settings, scored: int
) -> None:
    """VALIDATION_PARTIAL is a useful outcome, not a failure."""
    bad = DeepDraftClaim(
        section=DeepSection.BULL_CASE,
        text="Investors should buy the shares.",
        basis=DeepBasis.INTERPRETATION,
        evidence=("S.growth",),
    )

    run = _run(session, settings, synthesis=MockDeepResearch(_draft(_good_claim(), bad)))

    assert run.status is RunStatus.VALIDATION_PARTIAL
    assert run.report is not None
    assert DeepResearchReportRepository(session).count() == 1


def _article(url: str = "https://reuters.com/business/acme-vertex") -> ExternalSearchResult:
    """One well-formed recent article a collection would accept."""
    return ExternalSearchResult(
        title="Acme signs a multi-year supply agreement with Vertex",
        url=url,
        snippet=(
            "Acme Corporation said it had signed a multi-year supply agreement with "
            "Vertex covering platform components through 2030."
        ),
        published_at=date(2026, 6, 28),
    )


@pytest.mark.integration
def test_external_evidence_is_reused_inside_the_window(
    session: Session, settings: Settings, scored: int
) -> None:
    """A search engine drifting between calls must not buy a new paid report."""
    first = MockExternalResearch({"": [_article()]})
    _run(session, settings, external=first)
    session.flush()

    second = MockExternalResearch({"": [_article("https://reuters.com/business/other")]})
    again = _run(session, settings, external=second)

    assert again.external_state == EXTERNAL_REUSED
    assert second.queries == []
    assert again.status is RunStatus.CACHE_HIT
    assert again.provider_calls == 0
    assert again.estimated_cost_usd == 0.0


@pytest.mark.integration
def test_a_reused_collection_is_not_searched_for_again(
    session: Session, settings: Settings, scored: int
) -> None:
    _run(session, settings, external=MockExternalResearch({"": [_article()]}))
    session.flush()
    provider = MockExternalResearch({"": [_article()]})

    _run(session, settings, external=provider)

    assert provider.queries == []


@pytest.mark.integration
def test_an_expired_window_collects_again(
    session: Session, settings: Settings, scored: int
) -> None:
    _run(session, settings, external=MockExternalResearch({"": [_article()]}))
    session.flush()

    # Age the stored collection past the window. A tiny window would not do it:
    # the row was written microseconds ago and is inside any positive interval.
    stored = DeepResearchReportRepository(session).latest_for_company(scored)
    assert stored is not None
    stored.external_collected_at = datetime.now(UTC) - timedelta(hours=9)
    session.flush()

    provider = MockExternalResearch({"": [_article()]})
    again = _run(session, settings, external=provider)

    assert provider.queries
    assert again.external_state == EXTERNAL_FRESH


@pytest.mark.integration
def test_changed_deterministic_evidence_bypasses_reuse(
    session: Session, settings: Settings, scored: int, make: type[Make], seed: type[Seed]
) -> None:
    """Yesterday's news beside today's rescored fundamentals is a brief that never existed."""
    _run(session, settings, external=MockExternalResearch({"": [_article()]}))
    session.flush()

    seed.score(session, scored, TICKER, final=91.0, score_date=date(2026, 7, 1))
    session.flush()
    provider = MockExternalResearch({"": [_article()]})
    again = _run(session, settings, external=provider)

    assert provider.queries
    assert again.external_state == EXTERNAL_FRESH


@pytest.mark.integration
def test_a_degraded_collection_is_never_reused(
    session: Session, settings: Settings, scored: int
) -> None:
    """A thin set caused by failed searches must not be frozen in for the window."""
    _run(session, settings, external=MockExternalResearch(failing="search down"))
    session.flush()

    provider = MockExternalResearch({"": [_article()]})
    again = _run(session, settings, external=provider)

    assert provider.queries
    assert again.external_state == EXTERNAL_FRESH


@pytest.mark.integration
def test_refresh_external_bypasses_the_reuse_window(
    session: Session, settings: Settings, scored: int
) -> None:
    _run(session, settings, external=MockExternalResearch({"": [_article()]}))
    session.flush()
    provider = MockExternalResearch({"": [_article()]})

    again = run_deep_research(
        session,
        settings,
        synthesis=MockDeepResearch(_draft(_good_claim())),
        external=provider,
        ticker=TICKER,
        refresh=False,
        refresh_external=True,
    )

    assert provider.queries
    assert again.external_state == EXTERNAL_FRESH


@pytest.mark.integration
def test_the_whole_accepted_set_is_stored_not_just_cited_sources(
    session: Session, settings: Settings, scored: int
) -> None:
    """A subset would fingerprint differently and miss the cache it was rebuilding for."""
    run = _run(session, settings, external=MockExternalResearch({"": [_article()]}))
    session.flush()

    stored = DeepResearchReportRepository(session).latest_for_company(scored)
    assert stored is not None
    assert stored.collected_external_json
    assert run.report is not None
    assert len(stored.collected_external_json) >= len(run.report.external_evidence)
