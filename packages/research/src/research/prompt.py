"""What the model is told, and how a brief is rendered for it.

The prompt lives beside the validator on purpose. Every rule stated here has a
check in `research.validation` that enforces it, and every check there has a
sentence here that warns about it. Split across two packages they would drift,
and the failure would be silent: a prompt that stopped asking for something the
validator still rejects produces reports that are `PARTIAL` for no visible
reason.

The rendering is as load-bearing as the instructions. A missing value is written
as the literal `unknown` — never blank, never `0` — because a blank cell reads as
"nothing to say here" and a zero reads as a measurement. Every citable item is
written with its id in the left margin, so quoting an id is the path of least
resistance rather than a discipline the model has to remember.

Nothing here performs I/O and nothing here calls a model. This module turns a
`ResearchBrief` into two strings; who sends them is the application's business.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain import MetricUnit
from research.report import Basis, Section
from research.validation import allowed_bases

if TYPE_CHECKING:
    from research.brief import MetricFact, ResearchBrief, ScoreItem

RESEARCH_PROMPT_V1 = "RESEARCH_PROMPT_V1"
"""The first research prompt.

Stored on every report. Reports produced under different prompt versions are not
compared, for the same reason score snapshots are not compared across formula
versions: the change would be in the instructions, not in the company. Editing
the wording below means a new identifier here — never an edit to an existing one.
"""

CURRENT_PROMPT_VERSION = RESEARCH_PROMPT_V1
"""The version every new report is stamped with."""

UNKNOWN = "unknown"
"""How an absent value is written. Never blank, never zero."""

_SECTION_BRIEFS: dict[Section, str] = {
    Section.COMPANY_SUMMARY: "What the company actually does.",
    Section.WHY_IT_RANKED_HIGH: "Which components and sub-scores produced this score.",
    Section.GROWTH_DRIVERS: "What is driving revenue, and whether growth is accelerating.",
    Section.RECENT_DEVELOPMENTS: "What the company has filed recently.",
    Section.CATALYSTS: "What could move the business from here.",
    Section.FINANCIAL_QUALITY_INTERPRETATION: "What the margins, cash and balance sheet mean.",
    Section.VALUATION_INTERPRETATION: "What the valuation component is reacting to.",
    Section.MAJOR_RISKS: "The largest risks visible in this evidence.",
    Section.DILUTION_FINANCING_RISK: "Share count, financing, and what they imply.",
    Section.BULL_CASE: "The case for the company, argued from this evidence.",
    Section.BEAR_CASE: "The case against, argued from the same evidence.",
    Section.THESIS_BREAKERS: "What would falsify the bull case.",
    Section.WATCH_NEXT_QUARTER: "The specific things to check when the next quarter lands.",
}

_SYSTEM_PROMPT = """\
You are a research assistant for Compounder Radar, a personal stock-screening
tool. A deterministic scanner has already ranked the market. Your job is to
explain one company that the scanner surfaced, using only the evidence supplied
with this request.

You are not being asked whether the company is a good investment. You are being
asked what the evidence says, what it does not say, and how a careful reader
should interpret it.

# The brief is your entire world

Use only the supplied brief. Do not use anything you remember about this company,
its industry, its executives, its products or its history from your training. If
a fact is not in the brief, you do not have it — say so with an UNKNOWN claim
rather than supplying it from memory. Recalled facts are untraceable, and an
untraceable claim is indistinguishable from an invented one.

# The CompounderScore is given

The score, its four components, its sub-scores and its risk penalties were
calculated by the deterministic layers of this system. They are authoritative
and final. Do not recompute them, re-weight them, dispute the arithmetic, or
suggest what the score "should" be. If you think the score misreads the company,
say so in the bear case as an interpretation, in words, without a competing
number.

# Never do new arithmetic

Every number you write must already appear in the brief. Quote figures; do not
derive them. Do not calculate a ratio, a percentage, a growth rate, a margin, a
multiple, a price target or a return estimate, even from two numbers that are
both in the brief. If a figure you want is not supplied, that figure is not
available to you.

**When referring to a supplied numeric value, use the value as supplied. Do not
round, approximate, shorten, or replace it with a nearby figure.** Write
91,900,000 rather than "roughly 90 million", and 53.15% rather than "about 53%".
An approximated figure is treated as a figure the brief does not contain, and the
claim carrying it is discarded.

# Cite everything

Every citable item in the brief carries an id in its left margin — `S.` for score
lines, `M.` for metrics, `F.` for reported quarters, `D.` for filings, `X.` for
text quoted from a filing, `E.` for vendor-supplied figures. Cite the ids your
claim rests on. Cite only ids that appear in the brief; an id you invent will be
detected and the claim discarded.

# Mark what each claim is

Every claim carries a basis:

- DETERMINISTIC — restates a figure this system calculated. Cite `S.`, `M.`, `F.`
  or `E.` ids.
- EXTRACTED — a fact read from filing text supplied in the brief. Cite the `X.`
  ids whose text you read; an EXTRACTED claim citing only `D.` ids will be
  discarded. A `D.` entry is metadata: it proves the filing exists and proves
  nothing about what is in it, and attaching an accession number to a fact you
  remember does not make it evidence.
  An excerpt says what it says and no more. Do not continue a sentence it cuts
  off, do not generalise from one quoted section to the company as a whole, and
  do not treat a topic the excerpt mentions as a topic it explains.
- INTERPRETATION — your reasoning over the evidence. Cite what you reasoned from.
- UNKNOWN — the brief does not answer this. Evidence is optional.

# UNKNOWN is a real answer

When the evidence does not support a section, write one UNKNOWN claim saying so
plainly. Do not fill the space with a plausible sentence, a generic industry
observation, or a hedge. A section that honestly says it has nothing is more
useful than one that sounds informative and is not.

When no filing text is supplied — which is the normal case today — the sections
that describe the business, its recent developments and its catalysts have no
evidence behind them, and UNKNOWN is the correct answer for all three. A report
that answers those three with UNKNOWN and the other ten from the score and the
metrics is a complete, correct report. Do not pad it.

# Never give investment advice

No buy, sell or hold. No price targets, no fair-value estimates, no position
sizing, no return predictions, no recommendations to act. This tool ranks
companies for further research; it has no view on any security.

# Shape of the answer

Return one flat list of claims. Every claim names the section it belongs to.

**All thirteen sections must appear in that list.** A section with no claim is
treated as unanswered, not as "nothing to say" — if the evidence does not support
a section, say so with an UNKNOWN claim naming that section. Two to four claims
per section is right; each claim is one sentence stating one thing, and **no claim may exceed
240 characters** — a longer one is dropped, so split it into two instead.
"""


def build_system_prompt() -> str:
    """Return the system prompt, including the per-section basis rules.

    The allowed bases are read from the validator rather than restated, so a
    change to what is permitted cannot leave the prompt asking for something that
    will be discarded.

    Returns:
        The complete system prompt.
    """
    lines = [_SYSTEM_PROMPT, "", "# The thirteen sections", ""]
    for section in Section:
        allowed = ", ".join(basis.value for basis in Basis if basis in allowed_bases(section))
        lines.append(f"- `{section.value}` — {_SECTION_BRIEFS[section]}")
        lines.append(f"  Allowed bases: {allowed}")
    return "\n".join(lines)


def render_brief(brief: ResearchBrief) -> str:
    """Render one brief as the text a model receives.

    Args:
        brief: The evidence to render.

    Returns:
        A plain-text rendering with an id in the left margin of every citable
        line, and `unknown` wherever a value is absent.
    """
    blocks = [
        _identity(brief),
        _score(brief),
        _metrics(brief),
        _quarters(brief),
        _history(brief),
        _filings(brief),
        _enrichment(brief),
        _unknowns(brief),
    ]
    return "\n\n".join(block for block in blocks if block)


def _identity(brief: ResearchBrief) -> str:
    """Render who the company is and why it is being researched."""
    return "\n".join(
        [
            "# Company",
            f"ticker        {brief.ticker}",
            f"name          {brief.name}",
            f"industry      {brief.industry or UNKNOWN}",
            f"exchange      {brief.exchange or UNKNOWN}",
            f"selected as   {brief.selection.value}",
            f"evidence as of {brief.as_of}",
        ]
    )


def _score(brief: ResearchBrief) -> str:
    """Render the stored breakdown, every line addressable."""
    score = brief.score
    lines = [
        "# CompounderScore (given — do not recompute)",
        f"version       {score.score_version} as of {score.score_date}",
        f"status        {score.scoring_status.value}",
        f"final         {_number(score.final_score)}",
        f"raw           {_number(score.raw_score)}",
        f"risk penalty  {_number(score.risk_penalty)}",
        f"risk level    {score.risk_level.value if score.risk_level else UNKNOWN}",
        f"category      {score.category.value if score.category else UNKNOWN}",
        f"data coverage {_number(score.data_coverage)}",
        f"ranking state {score.ranking_state.value}",
        f"valuation on  {score.valuation_basis.value}",
        f"market cap is {score.market_cap_source.value}",
        f"liquidity is  {score.liquidity_basis.value}",
    ]
    if score.warnings:
        lines.append(f"warnings      {', '.join(w.value for w in score.warnings)}")

    lines.extend(["", "## Score lines"])
    lines.extend(_score_line(item) for item in score.items)
    return "\n".join(lines)


def _score_line(item: ScoreItem) -> str:
    """Render one score line with its id, points and observed figure."""
    points = _number(item.points)
    if item.max_points is not None:
        points = f"{points} of {_number(item.max_points)}"
    observed = (
        f"  observed {_quantity(item.observed, item.unit)}" if item.observed is not None else ""
    )
    note = f"  ({item.note})" if item.note else ""
    return f"{item.id:<40}{item.label:<46}{points}{observed}{note}"


def _metrics(brief: ResearchBrief) -> str:
    """Render the derived metrics, unknown ones included."""
    lines = [
        "# Metrics",
        "Every metric this system calculated. `unknown` means the data",
        "could not support it — it does not mean zero.",
        "",
    ]
    lines.extend(_metric_line(fact) for fact in brief.facts)
    return "\n".join(lines)


def _metric_line(fact: MetricFact) -> str:
    """Render one metric with its id, label, value and provenance."""
    return f"{fact.id:<40}{fact.label:<46}{_quantity(fact.value, fact.unit)}  [{fact.source}]"


def _quarters(brief: ResearchBrief) -> str:
    """Render the reported quarters, oldest first."""
    if not brief.quarters:
        return ""

    lines = ["# Reported quarters (oldest first)"]
    for period in brief.quarters:
        lines.append(f"{period.id}  period ending {period.period_end}  [{period.source}]")
        for label, value in (
            ("revenue", period.revenue),
            ("gross profit", period.gross_profit),
            ("operating income", period.operating_income),
            ("free cash flow", period.free_cash_flow),
            ("cash", period.cash),
            ("total debt", period.total_debt),
            ("diluted shares", period.shares_outstanding),
        ):
            lines.append(f"    {label:<20}{_quantity(value, MetricUnit.MONEY)}")
        if period.gross_profit_basis:
            lines.append(f"    {'gross profit from':<20}{period.gross_profit_basis}")
    return "\n".join(lines)


def _history(brief: ResearchBrief) -> str:
    """Render earlier scores of the same version."""
    if not brief.score_history:
        return (
            "# Score history\nNo earlier score under this version, so the score has no "
            "movement to explain."
        )

    lines = ["# Score history (same formula version, oldest first)"]
    lines.extend(
        f"{point.id:<40}{point.score_date}  final {_number(point.final_score)}"
        for point in brief.score_history
    )
    return "\n".join(lines)


def _filings(brief: ResearchBrief) -> str:
    """Render filing references, metadata only."""
    if not brief.filings:
        return (
            "# Filings\nNo filing metadata is available for this company. Any section that "
            "would\nrest on a filing must answer UNKNOWN."
        )

    quoted = {excerpt.accession for excerpt in brief.excerpts}
    lines = [
        "# Filings",
        "A D. entry is METADATA: it proves the filing exists and says nothing about",
        "its contents. An EXTRACTED claim citing only D. ids will be discarded.",
        "",
    ]
    lines.extend(
        f"{filing.id:<40}{filing.form:<6}filed {filing.filed}  "
        f"period {filing.period_end or UNKNOWN}"
        f"{'  [text quoted below]' if filing.accession in quoted else '  [metadata only]'}"
        for filing in brief.filings
    )
    lines.append(_filing_text(brief))
    return "\n".join(lines)


def _filing_text(brief: ResearchBrief) -> str:
    """Render the extracted filing text, or say plainly that there is none."""
    if not brief.excerpts:
        return (
            "\nNo filing text is supplied. Sections that would rest on one —\n"
            "company_summary, recent_developments, catalysts — must answer UNKNOWN."
        )

    lines = [
        "",
        "## Filing text",
        "Verbatim extracts, each with its own id. These are the only evidence here",
        "of what a filing says. An extract is a fragment of a section, not the whole",
        "of it, and not the whole company.",
    ]
    for excerpt in brief.excerpts:
        lines.extend(
            [
                "",
                f"{excerpt.id}",
                f"  {excerpt.form}, filed {excerpt.filed}, section {excerpt.section}",
                f"  {excerpt.url}",
                "",
                excerpt.text,
            ]
        )
    return "\n".join(lines)


def _enrichment(brief: ResearchBrief) -> str:
    """Render vendor-supplied figures, when any exist."""
    if brief.enrichment is None:
        return ""

    lines = [
        f"# Vendor-supplied figures (from {brief.enrichment.provider}, "
        f"retrieved {brief.enrichment.retrieved})"
    ]
    lines.extend(_metric_line(fact) for fact in brief.enrichment.facts)
    return "\n".join(lines)


def _unknowns(brief: ResearchBrief) -> str:
    """Render what the brief could not supply, as an explicit list."""
    if not brief.unknowns:
        return ""

    lines = [
        "# What this brief does NOT contain",
        "These are unavailable, not zero. Do not supply them from memory.",
        "",
    ]
    lines.extend(f"  {unknown}" for unknown in brief.unknowns)
    return "\n".join(lines)


def _number(value: float | None) -> str:
    """Render a plain figure, or `unknown`."""
    return UNKNOWN if value is None else f"{value:g}"


def _quantity(value: float | None, unit: MetricUnit | None) -> str:
    """Render a figure in its unit, or `unknown`.

    Percentages and points are written out as percentages, because that is how a
    reader states them — and the numeric check accepts either form, so the model
    is never forced to convert.
    """
    if value is None:
        return UNKNOWN
    if unit is MetricUnit.PERCENT:
        return f"{value * 100:.2f}%"
    if unit is MetricUnit.POINTS:
        return f"{value * 100:+.2f}pp"
    if unit is MetricUnit.MULTIPLE:
        return f"{value:.2f}x"
    if unit is MetricUnit.MONEY:
        return f"{value:,.0f}"
    if unit is MetricUnit.MONTHS:
        return f"{value:.1f} months"
    return f"{value:g}"
