"""The instructions, and how a brief is rendered for a model to read.

Every rule stated here has a check in `deep_research.validation` that enforces
it. That pairing is deliberate and worth preserving: a prompt asking for
something nothing verifies is a wish, and a check for something the prompt never
asked for is a trap. When one changes, the other changes with it, and the version
identifier moves.

The rendering is as important as the wording. Evidence arrives with its id
attached, so a claim can cite it; missing values render as the literal `unknown`
rather than being omitted, so the model is told what it lacks instead of
inferring it from a gap; and every dated item shows its date, so "recently" has
something to mean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deep_research.provenance import allowed_bases
from deep_research.report import DeepBasis, DeepSection

if TYPE_CHECKING:
    from deep_research.brief import DeepResearchBrief

DEEP_RESEARCH_PROMPT_V1 = "DEEP_RESEARCH_PROMPT_V1"
"""The first deep research prompt.

Reports are not compared across prompt versions any more than scores are across
formula versions: the difference would be in the instructions, not in the
company. Changing the wording means a new identifier here, which also makes every
cached report under the old one a miss.
"""

DEEP_RESEARCH_PROMPT_V2 = "DEEP_RESEARCH_PROMPT_V2"
"""The second deep research prompt.

V1 produced sound claims filed under bases its section would not accept —
`BASIS_NOT_ALLOWED` was the largest single cause of dropped claims in both live
runs — and left `research_conclusion` empty because every conclusion it wrote
read as advice. V2 changes what the model is told, and nothing about what is
enforced: the per-section table below is generated from `allowed_bases`, so the
instructions cannot drift from the policy, and the conclusion guidance shows the
model what an evidence assessment sounds like rather than relaxing the check that
rejected the alternative.
"""

CURRENT_DEEP_PROMPT_VERSION = DEEP_RESEARCH_PROMPT_V2
"""The version every new deep report is stamped with."""

_SECTION_GUIDE: dict[DeepSection, str] = {
    DeepSection.COMPANY_OVERVIEW: "What the business does, from filings or named sources.",
    DeepSection.CURRENT_SNAPSHOT: "The stored figures as they are. Deterministic only.",
    DeepSection.WHY_THE_ALGORITHM_LIKES_IT: (
        "Account for the CompounderScore from its own breakdown. Deterministic only."
    ),
    DeepSection.GROWTH_QUALITY: "Whether growth looks durable, and what drives it.",
    DeepSection.FINANCIAL_QUALITY: "Margins, cash generation, balance sheet.",
    DeepSection.VALUATION: "What the multiple reflects. No target, no fair value.",
    DeepSection.LATEST_EARNINGS: "The most recent reported period and any guidance.",
    DeepSection.RECENT_DEVELOPMENTS: "What has happened lately. Filings or named sources.",
    DeepSection.COMPETITIVE_POSITION: "Position against rivals, where evidence supports it.",
    DeepSection.CATALYSTS: "Identifiable upcoming events, not hopes.",
    DeepSection.MAJOR_RISKS: "What could go wrong, grounded in evidence.",
    DeepSection.BULL_CASE: "The strongest reading of the evidence.",
    DeepSection.BEAR_CASE: "The strongest reading against.",
    DeepSection.THESIS_BREAKERS: "What would falsify the bull case.",
    DeepSection.WHAT_THE_MARKET_MAY_BE_MISSING: "Only where evidence supports a gap.",
    DeepSection.WHAT_TO_WATCH_NEXT: "Specific, checkable things.",
    DeepSection.RESEARCH_CONCLUSION: (
        "How strong the evidence is and how interesting this is to research "
        "further. Never what to do about the security."
    ),
}


_BASIS_ORDER = (
    DeepBasis.DETERMINISTIC,
    DeepBasis.EXTRACTED,
    DeepBasis.EXTERNAL,
    DeepBasis.INTERPRETATION,
    DeepBasis.UNKNOWN,
)

_CONCLUSION_EXAMPLES = (
    "a compelling research candidate with material unresolved risks",
    "promising, but the evidence is mixed",
    "strong operating evidence against a demanding valuation",
    "headline growth that appears less durable once the filings are read",
    "worth continued research, though the evidence supports limited conviction",
)


def _permitted(section: DeepSection) -> str:
    """Render the bases a section accepts, shortest useful form.

    Generated from `allowed_bases` rather than written out, so the instruction
    and the rule it describes cannot disagree. A section added to the policy
    appears here automatically; one whose policy changes says so on the next run.
    """
    allowed = allowed_bases(section)
    return "/".join(basis.value for basis in _BASIS_ORDER if basis in allowed)


def build_deep_system_prompt() -> str:
    """Return the instructions for a deep research generation.

    Returns:
        The system prompt. Deterministic — no dates, no company, nothing that
        would make two runs differ.
    """
    sections = "\n".join(
        f"{index:>2}. {section.value}\n"
        f"      allowed: {_permitted(section)}\n"
        f"      {_SECTION_GUIDE[section]}"
        for index, section in enumerate(DeepSection, start=1)
    )
    conclusions = "\n".join(f"      - {example}" for example in _CONCLUSION_EXAMPLES)
    return f"""You are a securities research analyst writing a grounded research note.

THE BRIEF IS YOUR ENTIRE FACTUAL WORLD.
Everything you may state as fact is in the brief below. You have no other
knowledge of this company. If a product, executive, customer, competitor, deal,
lawsuit, market narrative or industry fact is not in the brief, it does not
exist for this report — however confident you are about it. Answer UNKNOWN
instead. UNKNOWN is a correct, valued answer; a plausible sentence is not.

THE COMPOUNDERSCORE IS FIXED.
It was computed from deterministic data before any news was read. Explain it;
never dispute, recompute or adjust it. No external source can change it.

EVERY CLAIM CARRIES A BASIS AND ITS EVIDENCE IDS.
  DETERMINISTIC  a figure this system calculated. Cite S. M. F. or E.
  EXTRACTED      what a filing says. Cite an X. excerpt.
  EXTERNAL       what a named source published. Cite a W. item.
  INTERPRETATION your reasoning. Cite the evidence you reason from.
  UNKNOWN        the brief does not answer this. Cite nothing.

D. ids are filing metadata only. They prove a filing exists and say nothing
about its contents, so they cannot support a factual claim about what it says.

NUMBERS.
Every figure you write must already appear in the evidence that claim cites. Do
no arithmetic: no ratios, growth rates, multiples, percentages or totals the
brief does not already state. Rounding for display is the only change allowed. A
number printed in one source does not become available to a claim citing a
different source.

EVIDENCE QUALITY.
Dates matter — never describe an older source as if it happened today; each item
shows its date. Primary evidence (filings, company releases) outranks reporting
about them. Several outlets covering one event is one fact, not several: do not
treat repetition as independent corroboration.

NEVER WRITE.
No buy, sell or hold. No price target or fair value. No position sizing. No
expected return, upside or downside percentage. You describe evidence; you do
not tell anyone what to do or what a share will be worth.

THE SEVENTEEN SECTIONS.
Each lists the bases it accepts. A claim filed under a basis its section does not
accept is discarded however true it is, so check the line before you write. If
what you want to say does not fit the section's bases, it belongs in a different
section — or it is an UNKNOWN.

{sections}

THE CONCLUSION.
research_conclusion must contain at least one INTERPRETATION claim citing the
evidence behind it. Assess the company **as something to research**, not as
something to trade. Useful shapes:
{conclusions}
Say how strong the evidence is, where it is thin, and what would settle the
question. Never an action, a target, a return or an imperative.

Return claims for every section you can support. One assertion per claim, one
sentence, under 320 characters. A section you cannot support from the brief gets
a single UNKNOWN claim saying so."""


def _line(label: str, value: object) -> str:
    """Render one labelled value, showing absence as `unknown`."""
    return f"  {label}: {'unknown' if value is None else value}"


def render_deep_brief(brief: DeepResearchBrief) -> str:
    """Render a brief as the text a model reads.

    Every citable item leads with its id, because a claim's evidence is an id and
    a model cannot cite what it was not shown by name. Absent values render as
    `unknown` rather than being dropped, so a gap is visible as a gap.

    Args:
        brief: The evidence to render.

    Returns:
        The rendered brief.
    """
    score = brief.score
    parts: list[str] = [
        f"COMPANY: {brief.name} ({brief.ticker})",
        _line("sector", brief.sector),
        _line("industry", brief.industry),
        _line("exchange", brief.exchange),
        f"  research as-of: {brief.as_of.isoformat()}",
        "",
        "DATA FRESHNESS",
        _line("prices through", brief.freshness.price_as_of),
        _line("fundamentals through", brief.freshness.fundamentals_through),
        _line("filings through", brief.freshness.filings_through),
        _line("filing text through", brief.freshness.excerpts_through),
    ]
    if brief.freshness.stale:
        parts.extend(f"  could not refresh: {note}" for note in brief.freshness.stale)

    parts += [
        "",
        f"COMPOUNDERSCORE ({score.score_version}, {score.score_date.isoformat()}) — FIXED",
        _line("status", score.scoring_status.value),
        _line("final score", score.final_score),
        _line("raw score", score.raw_score),
        _line("risk penalty", score.risk_penalty),
        _line("risk level", score.risk_level.value if score.risk_level else None),
        _line("category", score.category.value if score.category else None),
        _line("data coverage", score.data_coverage),
        _line("ranking state", score.ranking_state.value),
        _line("valuation basis", score.valuation_basis.value),
        _line("market cap source", score.market_cap_source.value),
        _line("liquidity basis", score.liquidity_basis.value),
    ]
    if brief.ranking is not None:
        parts.append(
            f"  rank: {brief.ranking.rank} of {brief.ranking.universe_size} scored companies"
        )
    if score.warnings:
        parts.append(f"  warnings: {', '.join(warning.value for warning in score.warnings)}")

    parts += ["", "SCORE BREAKDOWN (S.)"]
    parts.extend(
        f"  {item.id} | {item.label} | points {item.points} of {item.max_points}"
        f" | observed {item.observed if item.observed is not None else 'unknown'}"
        + (f" | {item.note}" if item.note else "")
        for item in score.items
    )

    parts += ["", "DERIVED METRICS (M. / E.)"]
    parts.extend(
        f"  {fact.id} | {fact.label} | "
        f"{fact.value if fact.value is not None else 'unknown'} ({fact.unit.value})"
        for fact in brief.all_facts
    )

    parts += ["", "REPORTED PERIODS (F.), oldest first"]
    parts.extend(
        f"  {period.id} | revenue {period.revenue} | gross profit {period.gross_profit}"
        f" | operating income {period.operating_income} | FCF {period.free_cash_flow}"
        f" | cash {period.cash} | debt {period.total_debt}"
        f" | diluted shares {period.shares_outstanding}"
        for period in brief.quarters
    )

    if brief.score_history:
        parts += ["", "EARLIER SCORES (S.history)"]
        parts.extend(
            f"  {point.id} | {point.score_date.isoformat()} | {point.final_score}"
            for point in brief.score_history
        )

    parts += ["", "FILINGS (D.) — metadata only, cannot support a factual claim"]
    if brief.filings:
        parts.extend(
            f"  {filing.id} | {filing.form} | filed {filing.filed.isoformat()}"
            for filing in brief.filings
        )
    else:
        parts.append("  none")

    parts += ["", "FILING TEXT (X.) — what the filings say"]
    if brief.excerpts:
        for excerpt in brief.excerpts:
            parts.append(
                f"  {excerpt.id} | {excerpt.form} | filed {excerpt.filed.isoformat()}"
                f" | section {excerpt.section}"
            )
            parts.append(f"    {excerpt.text}")
    else:
        parts.append("  none — sections needing filing text must answer UNKNOWN")

    parts += ["", "CURRENT EXTERNAL EVIDENCE (W.)"]
    if brief.external:
        for item in brief.external:
            published = item.published_at.isoformat() if item.published_at else "undated"
            age = item.age_in_days(brief.as_of)
            parts.append(
                f"  {item.evidence_id} | {item.tier.value} | {item.source_type.value}"
                f" | {item.publisher} | published {published}"
                + (f" ({age} days before as-of)" if age is not None else "")
            )
            parts.append(f"    {item.title}")
            parts.append(f"    {item.excerpt}")
    else:
        parts.append(
            "  none — no current external evidence was collected. Sections needing "
            "it must answer UNKNOWN. Do not supply news from memory."
        )

    if brief.unknowns:
        parts += ["", "KNOWN GAPS — these ids have no value"]
        parts.append(f"  {', '.join(brief.unknowns)}")

    return "\n".join(parts)
