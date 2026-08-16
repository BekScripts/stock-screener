"""Rendering candidates and briefs for a person at a terminal.

Inspection only. These are the views that answer "what would a research run pick
tonight, and what would it actually send?" — which is the question worth being
able to ask before any of it costs a model call.

Nothing here is a report *about* a company. It is a report about the evidence,
which is why it counts facts and lists what is unknown rather than summarising
what is known.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from research import EvidenceKind, ResearchStatus, Section, evidence_kind
from stock_screener.research.brief import SCORE_SOURCE
from stock_screener.research.runner import ResearchSkip

if TYPE_CHECKING:
    from collections.abc import Sequence

    from research import ResearchBrief
    from stock_screener.research.candidates import Candidate
    from stock_screener.research.runner import ResearchOutcome

_CANDIDATE_HEADER = (
    f"{'#':>3}  {'TICKER':<8}{'SCORE':>7}{'COVER':>7}{'QTRS':>6}{'30D':>7}  "
    f"{'REASON':<13}{'STATE':<12}NAME"
)


def format_candidates(candidates: Sequence[Candidate]) -> str:
    """Render the selected research candidates as a table.

    Args:
        candidates: The selection, in precedence order.

    Returns:
        A table, or a line explaining why there is nothing to show.
    """
    if not candidates:
        return "No research candidates. Run `score` first, or check the qualification floors."

    lines = [_CANDIDATE_HEADER, "-" * len(_CANDIDATE_HEADER)]
    for position, candidate in enumerate(candidates, start=1):
        change = "-" if candidate.score_change_30d is None else f"{candidate.score_change_30d:+.1f}"
        lines.append(
            f"{position:>3}  {candidate.ticker:<8}"
            f"{_number(candidate.final_score):>7}"
            f"{_number(candidate.data_coverage, places=2):>7}"
            f"{candidate.quarters:>6}{change:>7}  "
            f"{candidate.selection.value:<13}{candidate.ranking_state:<12}{candidate.name[:40]}"
        )

    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.selection.value] = counts.get(candidate.selection.value, 0) + 1
    tally = " ".join(f"{reason.lower()}={count}" for reason, count in sorted(counts.items()))
    lines.append("")
    lines.append(f"{len(candidates)} selected: {tally}")
    return "\n".join(lines)


def format_brief(brief: ResearchBrief) -> str:
    """Render one brief as a summary of the evidence it carries.

    Shows what a model would be given and — as importantly — what it would not:
    the unknown list, the provenance of the score, and how many facts were
    preserved from the score rather than recalculated.

    Args:
        brief: The assembled brief.

    Returns:
        A multi-line summary.
    """
    score = brief.score
    known = sum(1 for fact in brief.all_facts if fact.known)
    preserved = sum(1 for fact in brief.all_facts if fact.source == SCORE_SOURCE)
    forms = _filing_forms(brief)

    lines = [
        f"{brief.ticker} — {brief.name}",
        f"  industry     {brief.industry or 'unknown'} ({brief.exchange or 'unknown'})",
        f"  selected as  {brief.selection.value}",
        f"  contract     {brief.contract_version}",
        "",
        f"  score        {_number(score.final_score)} final"
        f"  /  {_number(score.raw_score)} raw"
        f"  /  {_number(score.risk_penalty)} risk"
        f"  /  {score.risk_level.value if score.risk_level else 'unknown'}",
        f"  version      {score.score_version} as of {score.score_date}",
        f"  status       {score.scoring_status.value}"
        f"  /  {score.category.value if score.category else 'no category'}",
        f"  coverage     {_number(score.data_coverage, places=2)}",
        f"  provenance   market cap {score.market_cap_source.value}"
        f"  /  valuation {score.valuation_basis.value}"
        f"  /  liquidity {score.liquidity_basis.value}"
        f"  /  {score.ranking_state.value}",
        "",
        f"  facts        {len(brief.all_facts)} "
        f"({known} known, {preserved} preserved from the score)",
        f"  score items  {len(score.items)}",
        f"  quarters     {_period_range(brief)}",
        f"  filings      {len(brief.filings)}{forms}",
        f"  history      {len(brief.score_history)} earlier score point(s)",
        f"  enrichment   {brief.enrichment.provider if brief.enrichment else 'none'}",
        f"  warnings     {', '.join(w.value for w in score.warnings) or 'none'}",
        "",
        f"  fingerprint  {brief.fingerprint()}",
        f"  citable ids  {len(brief.evidence_ids)}",
        f"  unknown      {len(brief.unknowns)}",
    ]
    lines.extend(f"    {unknown}" for unknown in brief.unknowns)
    return "\n".join(lines)


def _filing_forms(brief: ResearchBrief) -> str:
    """Return a parenthesised tally of filing forms, or nothing when there are none."""
    if not brief.filings:
        return "  (none stored — run `research update-filings`)"

    counts: dict[str, int] = {}
    for filing in brief.filings:
        counts[filing.form] = counts.get(filing.form, 0) + 1
    tally = " ".join(f"{form}x{count}" for form, count in sorted(counts.items()))
    newest = max(filing.filed for filing in brief.filings)
    return f"  ({tally}, newest {newest})"


def _period_range(brief: ResearchBrief) -> str:
    """Return the span the supplied reporting periods cover."""
    if not brief.quarters:
        return "0"
    span = f"{brief.quarters[0].period_end} → {brief.quarters[-1].period_end}"
    return f"{len(brief.quarters)} ({span})"


def _number(value: float | None, *, places: int = 1) -> str:
    """Render a figure, or a dash when it is unknown. Never a zero."""
    return "-" if value is None else f"{value:.{places}f}"


def filing_evidence_ids(brief: ResearchBrief) -> tuple[str, ...]:
    """Return the brief's filing citation handles, newest first.

    Exposed for the CLI and for tests: "which filings could a claim cite" is the
    question the whole filing index exists to answer.

    Args:
        brief: The assembled brief.

    Returns:
        The `D.*` ids, in the order the brief carries them.
    """
    return tuple(
        identifier
        for identifier in (filing.id for filing in brief.filings)
        if evidence_kind(identifier) is EvidenceKind.FILING
    )


def format_research_run(outcomes: Sequence[ResearchOutcome]) -> str:
    """Render what a research run did, company by company, and what it cost.

    Reports the split between generated, reused and skipped explicitly: a run
    that reused everything cost nothing, a run that generated everything cost
    real money, and a run the budget stopped early did neither — and all three
    look identical if only the statuses are shown.

    Args:
        outcomes: One outcome per company, in run order.

    Returns:
        A table, a tally, and the validation issues grouped by code.
    """
    if not outcomes:
        return "Nothing researched. No candidates qualified, or none could be briefed."

    header = (
        f"{'TICKER':<8}{'STATUS':<18}{'SOURCE':<11}{'CONF':<8}{'CLAIMS':>7}{'ISSUES':>7}"
        f"{'IN':>8}{'OUT':>7}{'USD':>8}  UNKNOWN SECTIONS"
    )
    lines = [header, "-" * len(header)]
    for outcome in outcomes:
        report = outcome.report
        confidence = report.confidence.level.value if report is not None else "-"
        claims = _claim_count(outcome)
        issues = len(report.issues) if report is not None else 0
        unknowns = len(report.unknowns) if report is not None else 0
        source = "skipped" if report is None else "reused" if outcome.cached else "generated"
        lines.append(
            f"{outcome.ticker:<8}{outcome.status:<18}{source:<11}{confidence:<8}"
            f"{claims:>7}{issues:>7}{outcome.input_tokens:>8}{outcome.output_tokens:>7}"
            f"{outcome.cost_usd:>8.3f}  {unknowns} of {len(Section)}"
        )

    lines.append("")
    lines.extend(_run_tally(outcomes))
    return "\n".join(lines)


def _claim_count(outcome: ResearchOutcome) -> int:
    """Return how many claims a company's report kept."""
    if outcome.report is None:
        return 0
    return sum(len(claims) for _, claims in outcome.report.sections.iter_sections())


def _run_tally(outcomes: Sequence[ResearchOutcome]) -> list[str]:
    """Summarise a run: where the reports came from, what they cost, what broke.

    Counts calls rather than companies where the two differ, because a cache hit
    and a generated report are indistinguishable in the table above and cost
    two very different amounts.
    """
    statuses = Counter(outcome.report.status for outcome in outcomes if outcome.report is not None)
    skips = Counter(outcome.skipped for outcome in outcomes if outcome.report is None)
    issues = Counter(
        issue.code.value
        for outcome in outcomes
        if outcome.report is not None
        for issue in outcome.report.issues
    )
    reused = sum(1 for outcome in outcomes if outcome.cached)
    calls = sum(1 for outcome in outcomes if outcome.report is not None and not outcome.cached)
    budget_stopped = skips[ResearchSkip.BUDGET_EXHAUSTED]

    lines = [
        f"candidates {len(outcomes)}  /  cache hits {reused}  /  provider calls {calls}",
        f"COMPLETE {statuses[ResearchStatus.COMPLETE]}  "
        f"PARTIAL {statuses[ResearchStatus.PARTIAL]}  "
        f"FAILED {statuses[ResearchStatus.FAILED]}  "
        f"no brief {skips[ResearchSkip.NO_BRIEF]}  "
        f"skipped by budget {budget_stopped}",
        f"claims {sum(_claim_count(outcome) for outcome in outcomes)}  /  "
        f"{sum(outcome.input_tokens for outcome in outcomes)} in + "
        f"{sum(outcome.output_tokens for outcome in outcomes)} out tokens  /  "
        f"~${sum(outcome.cost_usd for outcome in outcomes):.2f} estimated",
    ]
    if issues:
        listed = ", ".join(f"{code} {count}" for code, count in sorted(issues.items()))
        lines.append(f"validation issues: {listed}")
    else:
        lines.append("validation issues: none")
    if budget_stopped:
        lines.append(
            f"BUDGET_EXHAUSTED — stopped before {budget_stopped} company(s). "
            "Everything already generated is stored; raise RESEARCH_MAX_RUN_COST_USD "
            "and re-run to continue, and the finished reports will be reused."
        )
    return lines
