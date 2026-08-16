"""The research pipeline end to end, with a mock model.

The order under test is the contract's central claim:

    brief → draft → validate_report → persist

Every test here asks one of two questions. Does bad model output reach the
database? (It must not — it is dropped, and the drop is recorded.) And does a
provider failure reach the scan or the ranking? (It must not — it becomes a
`FAILED` report and nothing else changes.)
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from api_clients import (
    MockResearch,
    ProviderDataError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderRequestError,
    estimate_cost_usd,
)
from data_access import CompanyRepository, FilingRepository, ResearchReportRepository
from domain import CURRENT_SCORE_VERSION, Filing
from research import (
    NO_EVIDENCE_TEXT,
    Basis,
    Claim,
    DraftClaim,
    DraftReport,
    IssueCode,
    ReportSections,
    ResearchStatus,
    Section,
    SelectionReason,
    build_system_prompt,
    render_brief,
)
from stock_screener.research import (
    RESERVED_PROMPT_TOKENS,
    ResearchOutcome,
    ResearchSkip,
    assemble_brief,
    find_cached_report,
    research_candidates,
    research_company,
    select_candidates,
)
from stock_screener.scanning import scan_market
from stock_screener.scoring import top_opportunities

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from research import ResearchBrief
    from stock_screener.config import Settings

PROMPT = "RESEARCH_PROMPT_V1"

#: One valid claim per section, each citing evidence the seeded brief contains.
#: `0.35` is the observed value on every seeded sub-score, so `35%` is a figure
#: the numeric check accepts.
_VALID: dict[Section, tuple[str, Basis, tuple[str, ...]]] = {
    Section.COMPANY_SUMMARY: (
        "The brief supplies no filing text, so the business is not described here.",
        Basis.UNKNOWN,
        (),
    ),
    Section.WHY_IT_RANKED_HIGH: (
        "Growth contributed 30 of 35 points.",
        Basis.DETERMINISTIC,
        ("S.growth",),
    ),
    Section.GROWTH_DRIVERS: (
        "Revenue grew 35% year over year.",
        Basis.DETERMINISTIC,
        ("M.revenue_growth_yoy",),
    ),
    Section.RECENT_DEVELOPMENTS: (
        "No filing metadata is supplied for this company.",
        Basis.UNKNOWN,
        (),
    ),
    Section.CATALYSTS: (
        "Continued acceleration would be one.",
        Basis.INTERPRETATION,
        ("M.revenue_growth_acceleration",),
    ),
    Section.FINANCIAL_QUALITY_INTERPRETATION: (
        "Gross margin of 35% is the strongest quality signal available.",
        Basis.DETERMINISTIC,
        ("M.gross_margin",),
    ),
    Section.VALUATION_INTERPRETATION: (
        "Valuation earned 18 of 25 points.",
        Basis.DETERMINISTIC,
        ("S.valuation",),
    ),
    Section.MAJOR_RISKS: (
        "The unscored sub-scores are the largest gap in what is known.",
        Basis.INTERPRETATION,
        ("S.growth.growth_secondary",),
    ),
    Section.DILUTION_FINANCING_RISK: (
        "Dilution carried no penalty.",
        Basis.DETERMINISTIC,
        ("S.risk.dilution",),
    ),
    Section.BULL_CASE: (
        "Growth at this level may persist.",
        Basis.INTERPRETATION,
        ("M.revenue_growth_yoy",),
    ),
    Section.BEAR_CASE: (
        "The score rests on partial coverage.",
        Basis.INTERPRETATION,
        ("S.quality",),
    ),
    Section.THESIS_BREAKERS: (
        "Deceleration would end the case.",
        Basis.INTERPRETATION,
        ("M.revenue_growth_acceleration",),
    ),
    Section.WATCH_NEXT_QUARTER: (
        "Whether margins hold.",
        Basis.INTERPRETATION,
        ("F.2026-06-30",),
    ),
}


def _draft(overrides: dict[Section, tuple[Claim, ...]] | None = None) -> DraftReport:
    """A complete, valid draft, with named sections replaced.

    Built section-by-section for readability, then flattened — which is the wire
    shape the model actually returns.
    """
    claims: dict[Section, tuple[Claim, ...]] = {
        section: (Claim(text=text, basis=basis, evidence=evidence),)
        for section, (text, basis, evidence) in _VALID.items()
    }
    claims.update(overrides or {})
    return DraftReport(
        ticker="XYZ",
        claims=tuple(
            DraftClaim(section=section, text=claim.text, basis=claim.basis, evidence=claim.evidence)
            for section, section_claims in claims.items()
            for claim in section_claims
        ),
    )


def _run(
    session: Session,
    settings: Settings,
    provider: MockResearch,
    *,
    ticker: str = "XYZ",
    force: bool = False,
) -> ResearchOutcome:
    return research_company(session, settings, provider, ticker, prompt_version=PROMPT, force=force)


# --- a valid response ------------------------------------------------------


@pytest.mark.integration
def test_a_valid_response_is_stored_intact(
    session: Session, settings: Settings, scored_company: str
) -> None:
    provider = MockResearch([_draft()], input_tokens=1000, output_tokens=500)

    outcome = _run(session, settings, provider)

    assert outcome.report is not None
    assert outcome.report.status is ResearchStatus.COMPLETE
    assert outcome.report.issues == ()
    assert outcome.input_tokens == 1000
    assert ResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_the_provider_receives_the_prompt_and_the_rendered_brief(
    session: Session, settings: Settings, scored_company: str
) -> None:
    provider = MockResearch([_draft()])

    _run(session, settings, provider)

    system, brief_text = provider.calls[0]
    assert system == build_system_prompt()
    brief = assemble_brief(session, settings, "XYZ")
    assert brief is not None
    assert brief_text == render_brief(brief)


@pytest.mark.integration
def test_the_stored_report_records_the_prompt_and_the_model(
    session: Session, settings: Settings, scored_company: str
) -> None:
    outcome = _run(session, settings, MockResearch([_draft()]))

    assert outcome.report is not None
    assert outcome.report.prompt_version == PROMPT
    assert outcome.report.model_id == "mock-research"
    assert outcome.report.score_version == CURRENT_SCORE_VERSION


# --- bad output never reaches the database intact --------------------------


@pytest.mark.integration
def test_an_unresolved_citation_drops_the_claim(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = _draft(
        {
            Section.BULL_CASE: (
                Claim(
                    text="Growth may persist.",
                    basis=Basis.INTERPRETATION,
                    evidence=("M.invented_metric",),
                ),
            )
        }
    )

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert IssueCode.UNRESOLVED_EVIDENCE in {issue.code for issue in outcome.report.issues}
    assert outcome.report.sections.bull_case[0].text == NO_EVIDENCE_TEXT


@pytest.mark.integration
def test_a_fabricated_number_drops_the_claim(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = _draft(
        {
            Section.GROWTH_DRIVERS: (
                Claim(
                    text="Revenue grew 61.4% year over year.",
                    basis=Basis.DETERMINISTIC,
                    evidence=("M.revenue_growth_yoy",),
                ),
            )
        }
    )

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert IssueCode.FABRICATED_NUMBER in {issue.code for issue in outcome.report.issues}
    stored = find_cached_report(session, _brief(session, settings), prompt_version=PROMPT)
    assert stored is not None
    assert all("61.4" not in claim.text for claim in stored.sections.growth_drivers)


@pytest.mark.integration
def test_investment_advice_drops_the_claim(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = _draft(
        {
            Section.BULL_CASE: (
                Claim(
                    text="Growth is durable, so this is a buy.",
                    basis=Basis.INTERPRETATION,
                    evidence=("M.revenue_growth_yoy",),
                ),
            )
        }
    )

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert IssueCode.INVESTMENT_ADVICE in {issue.code for issue in outcome.report.issues}
    assert all("buy" not in claim.text for claim in outcome.report.sections.bull_case)


@pytest.mark.integration
def test_an_omitted_section_is_answered_unknown(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = _draft({Section.CATALYSTS: ()})

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert IssueCode.EMPTY_SECTION in {issue.code for issue in outcome.report.issues}
    assert outcome.report.sections.catalysts[0].basis is Basis.UNKNOWN
    assert Section.CATALYSTS.value in outcome.report.unknowns


@pytest.mark.integration
def test_an_explicit_unknown_section_survives_untouched(
    session: Session, settings: Settings, scored_company: str
) -> None:
    outcome = _run(session, settings, MockResearch([_draft()]))

    assert outcome.report is not None
    summary = outcome.report.sections.company_summary
    assert summary[0].basis is Basis.UNKNOWN
    assert summary[0].text != NO_EVIDENCE_TEXT
    assert Section.COMPANY_SUMMARY.value in outcome.report.unknowns


@pytest.mark.integration
def test_a_partial_report_is_still_stored_with_its_issues(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = _draft(
        {
            Section.BEAR_CASE: (
                Claim(text="Sell it.", basis=Basis.INTERPRETATION, evidence=("S.quality",)),
            )
        }
    )

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert outcome.report.status is ResearchStatus.PARTIAL
    row = ResearchReportRepository(session).find(
        _company_id(session),
        score_version=CURRENT_SCORE_VERSION,
        brief_fingerprint=outcome.report.brief_fingerprint,
        prompt_version=PROMPT,
    )
    assert row is not None
    assert row.issues


# --- provider failures -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "error",
    [
        ProviderRateLimitError("429"),
        ProviderRequestError("timed out"),
        ProviderDataError("malformed draft"),
    ],
)
def test_a_provider_failure_becomes_a_failed_report(
    error: Exception, session: Session, settings: Settings, scored_company: str
) -> None:
    outcome = _run(session, settings, MockResearch(error=error))

    assert outcome.report is not None
    assert outcome.report.status is ResearchStatus.FAILED
    assert outcome.report.sections == ReportSections()
    assert ResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_a_failed_report_records_which_failure_it_was(
    session: Session, settings: Settings, scored_company: str
) -> None:
    outcome = _run(session, settings, MockResearch(error=ProviderRateLimitError("quota spent")))

    assert outcome.report is not None
    assert "ProviderRateLimitError" in outcome.report.issues[0].detail


@pytest.mark.integration
def test_a_failed_provider_does_not_affect_scanning_or_ranking(
    session: Session, settings: Settings, scored_company: str
) -> None:
    ranking_before = [row.ticker for row in top_opportunities(session)]
    scan_before = len(scan_market(session, settings.eligibility_thresholds).rows)

    _run(session, settings, MockResearch(error=ProviderRateLimitError("429")))

    assert [row.ticker for row in top_opportunities(session)] == ranking_before
    assert len(scan_market(session, settings.eligibility_thresholds).rows) == scan_before
    assert select_candidates(session)[0].ticker == "XYZ"


@pytest.mark.integration
def test_a_failed_report_is_retried_rather_than_reused(
    session: Session, settings: Settings, scored_company: str
) -> None:
    _run(session, settings, MockResearch(error=ProviderRateLimitError("429")))

    provider = MockResearch([_draft()])
    outcome = _run(session, settings, provider)

    assert provider.calls, "a failed report must not satisfy the cache"
    assert outcome.report is not None
    assert outcome.report.status is ResearchStatus.COMPLETE


# --- the cache -------------------------------------------------------------


@pytest.mark.integration
def test_a_second_run_reuses_the_report_without_calling_the_provider(
    session: Session, settings: Settings, scored_company: str
) -> None:
    _run(session, settings, MockResearch([_draft()]))

    second = MockResearch([_draft()])
    outcome = _run(session, settings, second)

    assert second.calls == []
    assert outcome.cached is True
    assert ResearchReportRepository(session).count() == 1


@pytest.mark.integration
def test_force_regenerates_despite_a_stored_report(
    session: Session, settings: Settings, scored_company: str
) -> None:
    _run(session, settings, MockResearch([_draft()]))

    second = MockResearch([_draft()])
    outcome = _run(session, settings, second, force=True)

    assert second.calls
    assert outcome.cached is False


@pytest.mark.integration
def test_changed_evidence_is_a_cache_miss(
    session: Session,
    settings: Settings,
    make: type[Make],
    seed: type[Seed],
    scored_company: str,
) -> None:
    _run(session, settings, MockResearch([_draft()]))

    seed.company(session, make, "XYZ", quarters=8, revenue=444_000_000.0)
    second = MockResearch([_draft()])
    _run(session, settings, second)

    assert second.calls, "restated evidence must be re-read, not reused"


@pytest.mark.integration
def test_a_different_prompt_version_is_a_cache_miss(
    session: Session, settings: Settings, scored_company: str
) -> None:
    _run(session, settings, MockResearch([_draft()]))

    second = MockResearch([_draft()])
    research_company(session, settings, second, "XYZ", prompt_version="RESEARCH_PROMPT_V2")

    assert second.calls


# --- nothing to research ---------------------------------------------------


@pytest.mark.integration
def test_an_unknown_company_calls_no_provider_and_stores_nothing(
    session: Session, settings: Settings
) -> None:
    provider = MockResearch([_draft()])

    outcome = _run(session, settings, provider, ticker="NOPE")

    assert outcome.report is None
    assert outcome.status == "NO_BRIEF"
    assert provider.calls == []
    assert ResearchReportRepository(session).count() == 0


@pytest.mark.integration
def test_a_dry_run_generates_without_storing(
    session: Session, settings: Settings, scored_company: str
) -> None:
    outcome = research_company(
        session,
        settings,
        MockResearch([_draft()]),
        "XYZ",
        prompt_version=PROMPT,
        persist=False,
    )

    assert outcome.report is not None
    assert ResearchReportRepository(session).count() == 0


def _brief(session: Session, settings: Settings) -> ResearchBrief:
    brief = assemble_brief(session, settings, "XYZ")
    assert brief is not None
    return brief


def _company_id(session: Session) -> int:
    from data_access import CompanyRepository

    company = CompanyRepository(session).get_by_ticker("XYZ")
    assert company is not None
    return company.id


# --- grounding: metadata is not content ------------------------------------


@pytest.mark.integration
def test_a_remembered_business_fact_citing_a_filing_never_reaches_storage(
    session: Session,
    settings: Settings,
    make: type[Make],
    seed: type[Seed],
    scored_company: str,
) -> None:
    # The whole point of the rule, exercised through the pipeline: the accession
    # resolves, the filing exists, and the sentence is still the model's memory.
    company = CompanyRepository(session).get_by_ticker("XYZ")
    assert company is not None
    FilingRepository(session).upsert_filings(
        company.id,
        [
            Filing(
                accession="a-1",
                form="10-K",
                filed=date(2026, 2, 1),
                url="https://www.sec.gov/Archives/a-1",
                source="sec-edgar",
            )
        ],
    )
    session.flush()

    draft = _draft(
        {
            Section.RECENT_DEVELOPMENTS: (
                Claim(
                    text="The company announced a partnership with a major cloud vendor.",
                    basis=Basis.EXTRACTED,
                    evidence=("D.a-1",),
                ),
            )
        }
    )

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert IssueCode.MISSING_EXTRACTED_EVIDENCE in {issue.code for issue in outcome.report.issues}
    stored = find_cached_report(session, _brief(session, settings), prompt_version=PROMPT)
    assert stored is not None
    assert all("cloud vendor" not in claim.text for claim in stored.sections.recent_developments)


@pytest.mark.integration
def test_the_rendered_brief_marks_filings_as_metadata_only(
    session: Session,
    settings: Settings,
    make: type[Make],
    seed: type[Seed],
    scored_company: str,
) -> None:
    company = CompanyRepository(session).get_by_ticker("XYZ")
    assert company is not None
    FilingRepository(session).upsert_filings(
        company.id,
        [
            Filing(
                accession="a-1",
                form="10-Q",
                filed=date(2026, 5, 1),
                url="https://www.sec.gov/Archives/a-1",
                source="sec-edgar",
            )
        ],
    )
    session.flush()

    rendered = render_brief(_brief(session, settings))

    assert "[metadata only]" in rendered
    assert "No filing text is supplied" in rendered
    assert "must answer UNKNOWN" in rendered


# --- the multi-company loop ------------------------------------------------


@pytest.mark.integration
def test_a_run_over_several_companies_returns_one_outcome_each(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    for ticker in ("AAA", "BBB"):
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker)

    outcomes = research_candidates(
        session,
        settings,
        MockResearch([_draft()]),
        [("AAA", SelectionReason.TOP_RANKED), ("BBB", SelectionReason.HIDDEN_GEM)],
        prompt_version=PROMPT,
    )

    assert [outcome.ticker for outcome in outcomes] == ["AAA", "BBB"]
    assert ResearchReportRepository(session).count() == 2


@pytest.mark.integration
def test_one_company_failing_does_not_stop_the_others(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # A quota spent halfway through a run must cost the remaining companies
    # nothing. Every one of them gets an outcome; the failures are recorded.
    for ticker in ("AAA", "BBB"):
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker)

    outcomes = research_candidates(
        session,
        settings,
        MockResearch(error=ProviderRateLimitError("429")),
        [("AAA", SelectionReason.TOP_RANKED), ("BBB", SelectionReason.TOP_RANKED)],
        prompt_version=PROMPT,
    )

    assert len(outcomes) == 2
    assert all(o.report is not None and o.report.status is ResearchStatus.FAILED for o in outcomes)


# --- the run budget --------------------------------------------------------


MOCK_MODEL = "claude-sonnet-5"


def _seed_three(session: Session, make: type[Make], seed: type[Seed]) -> list[str]:
    tickers = ["AAA", "BBB", "CCC"]
    for ticker in tickers:
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker)
    return tickers


def _candidates(tickers: Sequence[str]) -> list[tuple[str, SelectionReason]]:
    return [(ticker, SelectionReason.TOP_RANKED) for ticker in tickers]


@pytest.mark.integration
def test_a_run_without_a_budget_researches_every_candidate(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    tickers = _seed_three(session, make, seed)
    provider = MockResearch([_draft()], input_tokens=8_000, output_tokens=4_000)

    outcomes = research_candidates(
        session, settings, provider, _candidates(tickers), prompt_version=PROMPT
    )

    assert len(provider.calls) == 3
    assert all(outcome.skipped is None for outcome in outcomes)


@pytest.mark.integration
def test_a_budget_too_small_for_one_request_calls_nobody(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # The check runs before the request, not after it, so a budget that cannot
    # cover a single call spends nothing at all.
    tickers = _seed_three(session, make, seed)
    provider = MockResearch([_draft()], input_tokens=8_000, output_tokens=4_000)

    outcomes = research_candidates(
        session, settings, provider, _candidates(tickers), prompt_version=PROMPT, budget_usd=0.001
    )

    assert provider.calls == []
    assert [outcome.ticker for outcome in outcomes] == tickers
    assert all(outcome.skipped is ResearchSkip.BUDGET_EXHAUSTED for outcome in outcomes)


@pytest.mark.integration
def test_a_budget_stop_is_not_a_failure(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    tickers = _seed_three(session, make, seed)
    provider = MockResearch([_draft()], input_tokens=8_000, output_tokens=4_000)

    outcomes = research_candidates(
        session, settings, provider, _candidates(tickers), prompt_version=PROMPT, budget_usd=0.001
    )

    assert all(outcome.report is None for outcome in outcomes)
    assert all(outcome.status == "BUDGET_EXHAUSTED" for outcome in outcomes)
    assert ResearchReportRepository(session).count() == 0


@pytest.mark.integration
def test_a_budget_stops_the_run_partway_and_keeps_what_it_bought(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # Two calls fit inside the budget, the third does not. The two reports that
    # were paid for stay stored — the guard stops spending, not persistence.
    tickers = _seed_three(session, make, seed)
    provider = MockResearch(
        [_draft()], input_tokens=100_000, output_tokens=10_000, model_id=MOCK_MODEL
    )
    budget = 2 * estimate_cost_usd(MOCK_MODEL, 100_000, 10_000)

    outcomes = research_candidates(
        session, settings, provider, _candidates(tickers), prompt_version=PROMPT, budget_usd=budget
    )

    assert len(provider.calls) == 2
    assert all(outcome.report is not None for outcome in outcomes[:2])
    assert outcomes[2].skipped is ResearchSkip.BUDGET_EXHAUSTED
    assert ResearchReportRepository(session).count() == 2


@pytest.mark.integration
def test_a_stored_report_costs_nothing_against_the_budget(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # The whole point of the cache: a second pass over companies already
    # researched must not consume the allowance a new company needs.
    tickers = _seed_three(session, make, seed)
    provider = MockResearch([_draft()], input_tokens=100_000, output_tokens=10_000)
    research_candidates(session, settings, provider, _candidates(tickers), prompt_version=PROMPT)

    again = research_candidates(
        session, settings, provider, _candidates(tickers), prompt_version=PROMPT, budget_usd=0.5
    )

    assert len(provider.calls) == 3
    assert all(outcome.cached for outcome in again)
    assert sum(outcome.cost_usd for outcome in again) == 0.0


@pytest.mark.integration
def test_a_generated_report_records_what_it_cost(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "AAA")
    seed.score(session, company_id, "AAA")
    provider = MockResearch(
        [_draft()], input_tokens=8_000, output_tokens=4_000, model_id=MOCK_MODEL
    )

    outcome = research_company(session, settings, provider, "AAA", prompt_version=PROMPT)

    assert outcome.cost_usd == pytest.approx(estimate_cost_usd(MOCK_MODEL, 8_000, 4_000))


@pytest.mark.integration
def test_the_budget_check_prices_a_request_before_making_it(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # The reserve is what a company is assumed to cost before its brief exists,
    # and it must be pessimistic: a run may stop early, but never overshoot.
    tickers = _seed_three(session, make, seed)
    provider = MockResearch([_draft()], input_tokens=8_000, output_tokens=4_000)
    reserve = estimate_cost_usd(
        settings.research_model, RESERVED_PROMPT_TOKENS, settings.research_max_output_tokens
    )

    outcomes = research_candidates(
        session,
        settings,
        provider,
        _candidates(tickers),
        prompt_version=PROMPT,
        budget_usd=reserve * 0.999,
    )

    assert provider.calls == []
    assert all(outcome.skipped is ResearchSkip.BUDGET_EXHAUSTED for outcome in outcomes)


@pytest.mark.integration
def test_companies_are_researched_one_at_a_time_in_order(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    tickers = _seed_three(session, make, seed)
    provider = MockResearch([_draft()], input_tokens=8_000, output_tokens=4_000)

    outcomes = research_candidates(
        session, settings, provider, _candidates(tickers), prompt_version=PROMPT, budget_usd=5.0
    )

    assert [outcome.ticker for outcome in outcomes] == tickers
    assert all(ticker in brief for ticker, (_, brief) in zip(tickers, provider.calls, strict=True))


# --- the flat wire shape ---------------------------------------------------


@pytest.mark.integration
def test_a_flat_draft_normalises_into_all_thirteen_sections(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = _draft()

    outcome = _run(session, settings, MockResearch([draft]))

    assert len(draft.claims) == len(Section)
    assert outcome.report is not None
    grouped = outcome.report.sections
    assert all(claims for _, claims in grouped.iter_sections())


@pytest.mark.integration
def test_grouping_preserves_the_order_the_model_produced(
    session: Session, settings: Settings, scored_company: str
) -> None:
    draft = DraftReport(
        ticker="XYZ",
        claims=(
            DraftClaim(
                section=Section.BULL_CASE,
                text="First point.",
                basis=Basis.INTERPRETATION,
                evidence=("M.revenue_growth_yoy",),
            ),
            DraftClaim(
                section=Section.BULL_CASE,
                text="Second point.",
                basis=Basis.INTERPRETATION,
                evidence=("M.revenue_growth_yoy",),
            ),
        ),
    )

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert [claim.text for claim in outcome.report.sections.bull_case] == [
        "First point.",
        "Second point.",
    ]


@pytest.mark.integration
def test_a_section_absent_from_the_flat_list_is_still_detected(
    session: Session, settings: Settings, scored_company: str
) -> None:
    # The flattening must not turn "the model said nothing about catalysts" into
    # a silently empty section. It is still an issue and still forces PARTIAL.
    draft = _draft({Section.CATALYSTS: ()})

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert not any(claim.section is Section.CATALYSTS for claim in draft.claims)
    assert IssueCode.EMPTY_SECTION in {issue.code for issue in outcome.report.issues}
    assert outcome.report.status is ResearchStatus.PARTIAL
    assert Section.CATALYSTS.value in outcome.report.unknowns


@pytest.mark.integration
def test_an_unknown_claim_survives_normalisation(
    session: Session, settings: Settings, scored_company: str
) -> None:
    outcome = _run(session, settings, MockResearch([_draft()]))

    assert outcome.report is not None
    summary = outcome.report.sections.company_summary
    assert summary[0].basis is Basis.UNKNOWN
    assert summary[0].text != NO_EVIDENCE_TEXT


@pytest.mark.integration
def test_an_overlong_claim_costs_only_itself_end_to_end(
    session: Session, settings: Settings, scored_company: str
) -> None:
    # DELL returned one claim past the accepted length and the whole paid draft
    # was lost to a traceback. The rest of the report must now reach storage.
    draft = _draft()
    long_claim = draft.claims[0].model_copy(
        update={"text": draft.claims[0].text + " " + "The company kept compounding. " * 12}
    )
    draft = draft.model_copy(update={"claims": (long_claim, *draft.claims[1:])})

    outcome = _run(session, settings, MockResearch([draft]))

    assert outcome.report is not None
    assert outcome.report.status is ResearchStatus.PARTIAL
    assert IssueCode.CLAIM_TOO_LONG in {issue.code for issue in outcome.report.issues}
    assert ResearchReportRepository(session).count() == 1


# --- permanent versus transient failure ------------------------------------


@pytest.mark.integration
def test_a_rejected_request_aborts_instead_of_writing_a_failed_row(
    session: Session, settings: Settings, scored_company: str
) -> None:
    provider = MockResearch(error=ProviderInvalidRequestError("400: Schema is too complex."))

    with pytest.raises(ProviderInvalidRequestError, match="too complex"):
        _run(session, settings, provider)

    assert ResearchReportRepository(session).count() == 0


@pytest.mark.integration
def test_a_rejected_request_stops_the_batch_at_the_first_company(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # The failure this exists to prevent: nineteen calls and nineteen FAILED
    # rows for one defect in the request we built.
    for ticker in ("AAA", "BBB", "CCC"):
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker)
    provider = MockResearch(error=ProviderInvalidRequestError("400: Schema is too complex."))

    with pytest.raises(ProviderInvalidRequestError):
        research_candidates(
            session,
            settings,
            provider,
            [(t, SelectionReason.TOP_RANKED) for t in ("AAA", "BBB", "CCC")],
            prompt_version=PROMPT,
        )

    assert len(provider.calls) == 1
    assert ResearchReportRepository(session).count() == 0


@pytest.mark.integration
def test_a_rate_limit_still_records_and_continues(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # The other side of the split, unchanged: transient failures are per-company
    # data, and the run carries on.
    for ticker in ("AAA", "BBB"):
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker)
    provider = MockResearch(error=ProviderRateLimitError("429"))

    outcomes = research_candidates(
        session,
        settings,
        provider,
        [(t, SelectionReason.TOP_RANKED) for t in ("AAA", "BBB")],
        prompt_version=PROMPT,
    )

    assert len(provider.calls) == 2
    assert all(o.report is not None and o.report.status is ResearchStatus.FAILED for o in outcomes)
    assert ResearchReportRepository(session).count() == 2
