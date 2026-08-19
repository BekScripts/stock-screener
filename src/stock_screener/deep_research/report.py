"""Render a preparation run and the brief it produced, for a person at a terminal.

Display only. Nothing here computes a figure the brief does not already carry —
the point of the summary is to show what preparation actually did and what the
brief actually contains, so a number invented during rendering would defeat it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deep_research import SourceTier
from stock_screener.deep_research.sources import registrable_domain

if TYPE_CHECKING:
    from deep_research import DeepResearchBrief
    from stock_screener.deep_research.collection import CollectionReport
    from stock_screener.deep_research.preparation import PreparationResult

_UNSET = "—"


def _date(value: object) -> str:
    """Render a date, or an em dash when there is none."""
    return _UNSET if value is None else str(value)


def _known(brief: DeepResearchBrief) -> str:
    """Render metric coverage as a share of facts that have a value."""
    facts = brief.all_facts
    if not facts:
        return _UNSET
    known = sum(1 for fact in facts if fact.known)
    return f"{known}/{len(facts)} ({known / len(facts):.0%})"


def format_preparation(result: PreparationResult, brief: DeepResearchBrief) -> str:
    """Return a concise account of one preparation run and its brief.

    Args:
        result: What each stage did.
        brief: The brief assembled afterwards.

    Returns:
        A multi-line summary: what ran, what the company scored, what evidence
        the brief carries, how current it is, and the three fingerprints.
    """
    score = brief.score
    ranking = brief.ranking
    freshness = brief.freshness

    lines = [
        f"{brief.ticker} — {brief.name}",
        "",
        "Preparation",
    ]
    lines.extend(f"  {outcome.line()}" for outcome in result.outcomes)

    placing = _UNSET
    if ranking is not None and ranking.rank is not None:
        placing = f"{ranking.rank} of {ranking.universe_size}"
    elif ranking is not None:
        placing = f"unranked, of {ranking.universe_size} scored"

    lines += [
        "",
        "Score",
        f"  status         {score.scoring_status.value}",
        f"  final          {_UNSET if score.final_score is None else f'{score.final_score:.2f}'}",
        f"  category       {_UNSET if score.category is None else score.category.value}",
        f"  version        {score.score_version} on {score.score_date.isoformat()}",
        f"  ranking state  {score.ranking_state.value}",
        f"  rank           {placing}",
        f"  data coverage  "
        f"{_UNSET if score.data_coverage is None else f'{score.data_coverage:.0%}'}",
        "",
        "Evidence",
        f"  metrics known  {_known(brief)}",
        f"  periods        {len(brief.quarters)}",
        f"  score history  {len(brief.score_history)}",
        f"  filings (D.)   {len(brief.filings)}",
        f"  excerpts (X.)  {len(brief.excerpts)}",
        f"  external (W.)  {len(brief.external)}",
        f"  unknowns       {len(brief.unknowns)}",
        "",
        "Freshness",
        f"  prices         {_date(freshness.price_as_of)}",
        f"  fundamentals   {_date(freshness.fundamentals_through)}",
        f"  filings        {_date(freshness.filings_through)}",
        f"  excerpts       {_date(freshness.excerpts_through)}",
        f"  refreshed      {', '.join(freshness.refreshed) or _UNSET}",
        f"  reused         {', '.join(freshness.reused) or _UNSET}",
    ]
    if freshness.stale:
        lines.append("  stale")
        lines.extend(f"    {note}" for note in freshness.stale)
    else:
        lines.append(f"  stale          {_UNSET}")

    lines += [
        "",
        "Fingerprints",
        f"  deterministic  {brief.deterministic_fingerprint()}",
        f"  external       {brief.external_fingerprint()}",
        f"  evidence       {brief.evidence_fingerprint()}",
    ]
    return "\n".join(lines)


def _domain(url: str) -> str:
    """Render a URL as its host, which is what a reader scans for."""
    return registrable_domain(url) or url


def format_collection(report: CollectionReport, brief: DeepResearchBrief) -> str:
    """Return a concise account of one external collection run.

    One line per accepted source plus the counts that say whether the collector
    is working. Excerpts are shown as a length rather than a body: a terminal
    dump of fifteen articles is unreadable, and the text is in the brief for
    anything that needs it.

    Args:
        report: What was searched, kept and refused.
        brief: The brief the evidence was attached to, for the fingerprints.

    Returns:
        A multi-line summary.
    """
    tiers = report.by_tier()
    lines = [
        f"{report.ticker} — external evidence",
        "",
        f"Queries ({len(report.queries)})",
    ]
    lines.extend(f"  {query}" for query in report.queries)

    lines += ["", f"Accepted ({len(report.evidence)})"]
    if not report.evidence:
        lines.append("  none")
    for item in report.evidence:
        published = item.published_at.isoformat() if item.published_at else "undated"
        lines += [
            f"  {item.evidence_id}",
            f"    tier       {item.tier.value}",
            f"    type       {item.source_type.value}",
            f"    publisher  {item.publisher}",
            f"    published  {published}",
            f"    title      {item.title[:90]}",
            f"    domain     {_domain(item.url)}",
            f"    excerpt    {len(item.excerpt)} chars",
        ]

    counts = report.counts()
    lines += [
        "",
        "Totals",
        f"  raw hits       {report.raw}",
        f"  unique hits    {report.unique}",
        f"  deduplicated   {report.duplicates}",
        f"  accepted       {len(report.evidence)}",
        f"  rejected       {len(report.rejected)}",
        f"  tier 1         {tiers[SourceTier.TIER_1_PRIMARY]}",
        f"  tier 2         {tiers[SourceTier.TIER_2_REPUTABLE]}",
        f"  tier 3         {tiers[SourceTier.TIER_3_SUPPORTING]}",
    ]
    if counts:
        lines.append("  rejected by reason")
        lines.extend(
            f"    {reason.lower():<18} {count}" for reason, count in sorted(counts.items())
        )
    if report.failures:
        lines.append("  search failures")
        lines.extend(f"    {failure[:110]}" for failure in report.failures)

    lines += [
        "",
        "Fingerprints",
        f"  deterministic  {brief.deterministic_fingerprint()}",
        f"  external       {brief.external_fingerprint()}",
        f"  evidence       {brief.evidence_fingerprint()}",
    ]
    return "\n".join(lines)
