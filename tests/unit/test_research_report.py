"""What the inspection commands print.

`unit`: these are pure functions of a brief and a candidate list. The output is
what an operator reads before deciding whether a research run is worth paying
for, so the cases worth pinning are the empty ones — a blank table and a table of
zeros look identical at a glance and mean opposite things.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from domain import ScoringStatus
from research import (
    CURRENT_CONTRACT_VERSION,
    ConfidenceLevel,
    FilingReference,
    IssueCode,
    RankingState,
    ReportSections,
    ResearchBrief,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    ScoreEvidence,
    SelectionReason,
    ValidationIssue,
)
from stock_screener.research import (
    Candidate,
    ResearchOutcome,
    ResearchSkip,
    filing_evidence_ids,
    format_brief,
    format_candidates,
    format_research_run,
)

SCORE_DATE = date(2026, 6, 30)


def _brief(**overrides: object) -> ResearchBrief:
    fields: dict[str, object] = {
        "ticker": "XYZ",
        "name": "Example Corp",
        "industry": "Software",
        "exchange": "NASDAQ",
        "as_of": SCORE_DATE,
        "selection": SelectionReason.TOP_RANKED,
        "score": ScoreEvidence(
            score_version="COMPOUNDER_V1_1",
            score_date=SCORE_DATE,
            scoring_status=ScoringStatus.SCORED,
            final_score=80.0,
            data_coverage=0.9,
            ranking_state=RankingState.PRELIMINARY,
        ),
    }
    return ResearchBrief(**(fields | overrides))  # type: ignore[arg-type]


def _candidate(ticker: str = "XYZ", **overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "ticker": ticker,
        "name": "Example Corp",
        "selection": SelectionReason.TOP_RANKED,
        "final_score": 80.0,
        "data_coverage": 0.9,
        "ranking_state": "PRELIMINARY",
        "quarters": 20,
    }
    return Candidate(**(fields | overrides))  # type: ignore[arg-type]


@pytest.mark.unit
def test_an_empty_selection_says_why_rather_than_printing_a_blank_table() -> None:
    output = format_candidates([])

    assert "No research candidates" in output
    assert "score" in output


@pytest.mark.unit
def test_a_selection_is_tallied_by_reason() -> None:
    output = format_candidates(
        [_candidate("AAA"), _candidate("BBB", selection=SelectionReason.HIDDEN_GEM)]
    )

    assert "2 selected" in output
    assert "top_ranked=1" in output
    assert "hidden_gem=1" in output


@pytest.mark.unit
def test_a_candidate_without_a_score_change_shows_a_dash_not_a_zero() -> None:
    output = format_candidates([_candidate(score_change_30d=None)])

    assert " -" in output
    assert "+0.0" not in output


@pytest.mark.unit
def test_a_score_change_is_signed() -> None:
    output = format_candidates([_candidate(score_change_30d=12.5)])

    assert "+12.5" in output


@pytest.mark.unit
def test_a_brief_with_no_filings_says_how_to_get_some() -> None:
    output = format_brief(_brief())

    assert "filings      0" in output
    assert "research update-filings" in output


@pytest.mark.unit
def test_a_brief_tallies_its_filing_forms() -> None:
    brief = _brief(
        filings=(
            FilingReference(id="D.a-1", form="10-K", filed=date(2026, 2, 1), accession="a-1"),
            FilingReference(id="D.a-2", form="10-Q", filed=date(2026, 5, 1), accession="a-2"),
        )
    )

    output = format_brief(brief)

    assert "filings      2" in output
    assert "10-Kx1 10-Qx1" in output
    assert "newest 2026-05-01" in output


@pytest.mark.unit
def test_a_brief_with_no_quarters_shows_zero_rather_than_an_empty_range() -> None:
    output = format_brief(_brief())

    assert "quarters     0" in output


@pytest.mark.unit
def test_a_brief_reports_its_provenance() -> None:
    output = format_brief(_brief())

    assert "market cap UNKNOWN" in output
    assert "PRELIMINARY" in output


@pytest.mark.unit
def test_filing_ids_come_back_in_order() -> None:
    brief = _brief(
        filings=(
            FilingReference(id="D.a-2", form="10-Q", filed=date(2026, 5, 1), accession="a-2"),
            FilingReference(id="D.a-1", form="10-K", filed=date(2026, 2, 1), accession="a-1"),
        )
    )

    assert filing_evidence_ids(brief) == ("D.a-2", "D.a-1")


@pytest.mark.unit
def test_a_brief_without_filings_has_no_filing_ids() -> None:
    assert filing_evidence_ids(_brief()) == ()


# --- the run summary -------------------------------------------------------


def _report(
    status: ResearchStatus = ResearchStatus.COMPLETE,
    *,
    issues: tuple[ValidationIssue, ...] = (),
) -> ResearchReport:
    return ResearchReport(
        ticker="XYZ",
        score_version="COMPOUNDER_V1_1",
        score_date=SCORE_DATE,
        brief_fingerprint="f" * 64,
        prompt_version="RESEARCH_PROMPT_V1",
        contract_version=CURRENT_CONTRACT_VERSION,
        model_id="claude-sonnet-5",
        generated_at=datetime(2026, 8, 15, tzinfo=UTC),
        status=status,
        sections=ReportSections(),
        confidence=ResearchConfidence(
            level=ConfidenceLevel.LOW,
            claimed=ConfidenceLevel.LOW,
            ceiling=ConfidenceLevel.LOW,
            rationale="thin",
        ),
        issues=issues,
    )


def _outcome(ticker: str, **overrides: object) -> ResearchOutcome:
    fields: dict[str, object] = {"ticker": ticker, "report": _report()}
    fields.update(overrides)
    return ResearchOutcome(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_a_run_that_researched_nothing_says_so_rather_than_printing_a_header() -> None:
    assert "Nothing researched" in format_research_run([])


@pytest.mark.unit
def test_the_summary_separates_cache_hits_from_provider_calls() -> None:
    # A run that reused everything cost nothing and a run that generated
    # everything cost real money; the statuses alone cannot tell them apart.
    outcomes = [
        _outcome("AAA", cached=True),
        _outcome("BBB", input_tokens=8_000, output_tokens=4_000, cost_usd=0.084),
    ]

    printed = format_research_run(outcomes)

    assert "cache hits 1" in printed
    assert "provider calls 1" in printed


@pytest.mark.unit
def test_the_summary_reports_the_estimated_spend() -> None:
    outcomes = [_outcome("AAA", cost_usd=0.08), _outcome("BBB", cost_usd=0.10)]

    assert "~$0.18 estimated" in format_research_run(outcomes)


@pytest.mark.unit
def test_the_summary_counts_each_status() -> None:
    outcomes = [
        _outcome("AAA"),
        _outcome("BBB", report=_report(ResearchStatus.PARTIAL)),
        _outcome("CCC", report=_report(ResearchStatus.FAILED)),
        _outcome("DDD", report=None, skipped=ResearchSkip.BUDGET_EXHAUSTED),
    ]

    printed = format_research_run(outcomes)

    assert "COMPLETE 1" in printed
    assert "PARTIAL 1" in printed
    assert "FAILED 1" in printed
    assert "skipped by budget 1" in printed


@pytest.mark.unit
def test_the_summary_groups_validation_issues_by_code() -> None:
    issue = ValidationIssue(code=IssueCode.FABRICATED_NUMBER, detail="42")
    outcomes = [
        _outcome("AAA", report=_report(issues=(issue, issue))),
        _outcome(
            "BBB",
            report=_report(
                issues=(ValidationIssue(code=IssueCode.EMPTY_SECTION, detail="nothing survived"),)
            ),
        ),
    ]

    printed = format_research_run(outcomes)

    assert "EMPTY_SECTION 1" in printed
    assert "FABRICATED_NUMBER 2" in printed


@pytest.mark.unit
def test_a_run_with_no_validation_issues_says_none_rather_than_omitting_the_line() -> None:
    assert "validation issues: none" in format_research_run([_outcome("AAA")])


@pytest.mark.unit
def test_a_budget_stop_is_called_out_below_the_tally() -> None:
    # Buried in a status column this reads as a failure. It is not one, and the
    # operator needs to know the run was cut short rather than finished.
    outcomes = [
        _outcome("AAA"),
        _outcome("BBB", report=None, skipped=ResearchSkip.BUDGET_EXHAUSTED),
    ]

    printed = format_research_run(outcomes)

    assert "BUDGET_EXHAUSTED" in printed
    assert "RESEARCH_MAX_RUN_COST_USD" in printed


@pytest.mark.unit
def test_a_completed_run_does_not_mention_the_budget() -> None:
    assert "BUDGET_EXHAUSTED" not in format_research_run([_outcome("AAA")])
