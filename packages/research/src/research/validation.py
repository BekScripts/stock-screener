"""Checking a draft against its brief, and accepting only what survives.

A prompt is a request, not a guarantee. Everything here assumes the model tried
to comply and sometimes did not: a citation that points at nothing, a figure that
was computed rather than quoted, a recommendation nobody asked for. Each is
caught structurally rather than trusted away, because the failure mode of a
research tool is not a wrong number — it is a fluent, well-cited, wrong number.

Five rules, applied to every claim:

1. **The basis must fit the section.** An interpretation cannot be offered as an
   account of why the score is what it is.
2. **Citations must resolve.** An id absent from the brief is not weak evidence,
   it is invented evidence, and the claim resting on it is dropped.
3. **The right kind of citation.** A `DETERMINISTIC` claim cites a score line,
   metric or period. An `EXTRACTED` claim cites a filing whose **text was
   actually supplied** — a bare accession number proves a filing exists and
   proves nothing about its contents, so a remembered fact about the business
   cannot be legitimised by attaching a filing id to it. Until filing-text
   extraction exists no filing carries text, so the sections that would rest on
   one answer `UNKNOWN`. That is the rule working, not a gap to widen.
4. **Every number must already exist.** No new arithmetic — no ratio, growth
   rate, multiple, percentage or target the deterministic layers did not
   produce. Display rounding is the only transformation allowed.
5. **No advice.** The tool ranks candidates for research and has no view on a
   security.
6. **One claim, one sentence.** A claim past `MAX_CLAIM_CHARS` is dropped whole —
   never truncated, because an edited sentence is no longer the model's claim and
   its evidence may no longer match what is left of it. The wire model is
   deliberately permissive so that one long sentence costs one claim instead of
   the entire paid response.

A failed claim is dropped, not rewritten, and the drop is recorded. A section
emptied by dropping is answered `UNKNOWN`, which is the honest form of an
unanswerable question and reads very differently from a blank.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from research.brief import EvidenceKind, Quantity, RankingState, evidence_kind
from research.quantities import (
    LITERAL,
    find_advice,
    quoted_quantities,
)
from research.quantities import unsupported_figures as _unsupported_figures
from research.report import (
    MAX_CLAIM_CHARS,
    Basis,
    Claim,
    ConfidenceLevel,
    IssueCode,
    ReportSections,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    Section,
    ValidationIssue,
    confidence_rank,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from research.brief import ResearchBrief
    from research.report import DraftReport

MIN_COVERAGE_FOR_MEDIUM = 0.6
"""Below this share of scoring metrics, no report may claim better than `LOW`.

A company scored on less than 60% of its metrics is one the deterministic layers
barely know. A confident narrative about it would be confidence in the model, not
in the evidence.
"""

MIN_COVERAGE_FOR_HIGH = 0.8
"""Coverage required before `HIGH` is reachable at all."""

NO_EVIDENCE_TEXT = "The brief does not contain evidence for this section."
"""What a section says when nothing survived. Deliberately flat, and never
softened into a sentence that sounds like an answer."""

_ALLOWED_BASES: dict[Section, frozenset[Basis]] = {
    Section.COMPANY_SUMMARY: frozenset({Basis.EXTRACTED}),
    Section.WHY_IT_RANKED_HIGH: frozenset({Basis.DETERMINISTIC}),
    Section.GROWTH_DRIVERS: frozenset({Basis.DETERMINISTIC, Basis.EXTRACTED, Basis.INTERPRETATION}),
    Section.RECENT_DEVELOPMENTS: frozenset({Basis.EXTRACTED}),
    Section.CATALYSTS: frozenset({Basis.EXTRACTED, Basis.INTERPRETATION}),
    Section.FINANCIAL_QUALITY_INTERPRETATION: frozenset(
        {Basis.DETERMINISTIC, Basis.INTERPRETATION}
    ),
    Section.VALUATION_INTERPRETATION: frozenset({Basis.DETERMINISTIC, Basis.INTERPRETATION}),
    Section.MAJOR_RISKS: frozenset({Basis.DETERMINISTIC, Basis.INTERPRETATION}),
    Section.DILUTION_FINANCING_RISK: frozenset(
        {Basis.DETERMINISTIC, Basis.EXTRACTED, Basis.INTERPRETATION}
    ),
    Section.BULL_CASE: frozenset({Basis.INTERPRETATION}),
    Section.BEAR_CASE: frozenset({Basis.INTERPRETATION}),
    Section.THESIS_BREAKERS: frozenset({Basis.INTERPRETATION}),
    Section.WATCH_NEXT_QUARTER: frozenset({Basis.INTERPRETATION}),
}
"""Which bases each section accepts, before `UNKNOWN` is added to every one.

`UNKNOWN` is universally allowed and deliberately absent from this table: it is
the answer a section falls back to, so forbidding it anywhere would make an
unanswerable section unrepresentable.
"""

_DETERMINISTIC_KINDS = frozenset(
    {EvidenceKind.SCORE, EvidenceKind.METRIC, EvidenceKind.STATEMENT, EvidenceKind.ENRICHMENT}
)


def allowed_bases(section: Section) -> frozenset[Basis]:
    """Return every basis a section accepts.

    Args:
        section: The section to look up.

    Returns:
        The permitted bases, always including `UNKNOWN`.
    """
    return _ALLOWED_BASES[section] | {Basis.UNKNOWN}


def unsupported_figures(
    text: str, brief: ResearchBrief, *, cited: Sequence[str] = ()
) -> tuple[str, ...]:
    """Return every number in a claim that its evidence does not contain.

    A thin scoping layer over `research.quantities.unsupported_figures`, which
    holds the matching rule itself. What this decides is *which* values the claim
    may draw on, and there are two pools with a deliberate difference between
    them.

    The brief's structured quantities are available to every claim: they are
    facts about the company, addressable by id, and any claim may restate one.
    Numbers inside filing text are available **only to a claim that cites the
    excerpt they appear in** — a figure in one 8-K says nothing about a sentence
    describing another, and pooling them would let any quoted number legitimise
    any claim.

    Bare numbers are also matched against every digit sequence in the brief's
    structured fields — dates, share counts, form periods — because a quarter
    count or a year is not an arithmetic claim, and demoting reports over them
    would train the reader to ignore the flag. Filing text is deliberately
    excluded from that fallback: it is prose, full of incidental integers, and
    letting it through brief-wide would undo the scoping above.

    Args:
        text: The claim's text.
        brief: The evidence the claim must rest on.
        cited: The evidence ids the claim cites. Only the `X.` ones matter here,
            and only theirs contribute numbers.

    Returns:
        The unsupported figures as written, in the order they appear.
    """
    wanted = {
        identifier for identifier in cited if evidence_kind(identifier) is EvidenceKind.EXCERPT
    }
    quoted: list[Quantity] = []
    for excerpt in brief.excerpts:
        if excerpt.id in wanted:
            quoted.extend(quoted_quantities(excerpt.text))

    structured = brief.model_copy(update={"excerpts": ()}).model_dump_json()
    scores = [
        value for value in (brief.score.final_score, brief.score.raw_score) if value is not None
    ]

    return _unsupported_figures(
        text,
        quantities=(*brief.quantities, *quoted),
        literals={float(token) for token in LITERAL.findall(structured)},
        scale_values=scores,
    )


def confidence_ceiling(brief: ResearchBrief, *, cites_filing: bool) -> ConfidenceLevel:
    """Return the most confidence the evidence behind a brief can support.

    Computed from the brief, never from the report, so a model cannot argue its
    way past thin data.

    Args:
        brief: The evidence the report was written from.
        cites_filing: Whether any surviving claim cites a filing.

    Returns:
        `HIGH` only for a well-covered, enriched company with at least one filing
        cited; `LOW` when the score itself rests on little.
    """
    coverage = brief.score.data_coverage
    if coverage is None or coverage < MIN_COVERAGE_FOR_MEDIUM:
        return ConfidenceLevel.LOW
    if (
        coverage >= MIN_COVERAGE_FOR_HIGH
        and brief.score.ranking_state is RankingState.FINAL
        and cites_filing
    ):
        return ConfidenceLevel.HIGH
    return ConfidenceLevel.MEDIUM


def _check_claim(
    claim: Claim, section: Section, brief: ResearchBrief
) -> tuple[ValidationIssue, ...]:
    """Return everything wrong with one claim. Empty means it survives."""
    found: list[ValidationIssue] = []

    def issue(code: IssueCode, detail: str) -> None:
        found.append(ValidationIssue(code=code, detail=detail, section=section))

    if claim.basis not in allowed_bases(section):
        issue(
            IssueCode.BASIS_NOT_ALLOWED,
            f"{claim.basis.value} is not permitted in {section.value}",
        )

    citable = brief.evidence_ids
    unresolved = tuple(cited for cited in claim.evidence if cited not in citable)
    if unresolved:
        issue(
            IssueCode.UNRESOLVED_EVIDENCE,
            f"cited ids are not in the brief: {', '.join(unresolved)}",
        )

    resolved = tuple(cited for cited in claim.evidence if cited in citable)
    kinds = {evidence_kind(cited) for cited in resolved}

    if claim.basis is Basis.EXTRACTED:
        if not (kinds & {EvidenceKind.EXCERPT, EvidenceKind.FILING}):
            issue(IssueCode.MISSING_EVIDENCE, "an extracted claim must cite filing text")
        elif not (set(resolved) & brief.extractable_ids):
            # A `D.` id resolves — the filing is real — and proves only that the
            # company filed something. Accepting it here would let any accession
            # legitimise any claim about filing content, which is precisely the
            # laundering this rule exists to stop.
            issue(
                IssueCode.MISSING_EXTRACTED_EVIDENCE,
                "the cited filings carry metadata only; an accession proves a filing "
                "exists, not what it says — cite an X. excerpt",
            )
    if claim.basis is Basis.DETERMINISTIC and not (kinds & _DETERMINISTIC_KINDS):
        issue(
            IssueCode.MISSING_EVIDENCE,
            "a deterministic claim must cite a score line, metric or period",
        )
    if claim.basis is Basis.INTERPRETATION and not resolved:
        issue(IssueCode.MISSING_EVIDENCE, "an interpretation must cite what it reasons from")
    if (
        section is Section.WHY_IT_RANKED_HIGH
        and claim.basis is not Basis.UNKNOWN
        and EvidenceKind.SCORE not in kinds
    ):
        issue(IssueCode.MISSING_EVIDENCE, "this section must cite the score breakdown")

    fabricated = unsupported_figures(claim.text, brief, cited=resolved)
    if fabricated:
        issue(
            IssueCode.FABRICATED_NUMBER,
            f"figures not present in the brief: {', '.join(fabricated)}",
        )

    advice = find_advice(claim.text)
    if advice is not None:
        issue(IssueCode.INVESTMENT_ADVICE, f"recommendation language: {advice!r}")

    return tuple(found)


def _filler() -> Claim:
    """Return the claim a section carries when nothing survived."""
    return Claim(text=NO_EVIDENCE_TEXT, basis=Basis.UNKNOWN)


def validate_report(
    draft: DraftReport,
    brief: ResearchBrief,
    *,
    prompt_version: str,
    model_id: str,
    generated_at: datetime,
) -> ResearchReport:
    """Check a draft against its brief and return what may be kept.

    Never raises on bad model output: a draft full of invented citations produces
    a `PARTIAL` report whose sections say they have nothing to offer, which is
    the outcome a reader can act on. Only a malformed response — one that could
    not be parsed into a `DraftReport` at all — is a failure, and that is
    recorded with `failed_report`.

    Args:
        draft: The model's output, parsed but unchecked.
        brief: The evidence it was given.
        prompt_version: The prompt that produced the draft.
        model_id: The model that produced it.
        generated_at: When it was produced, as an aware datetime.

    Returns:
        A validated report. `COMPLETE` when every claim survived untouched,
        `PARTIAL` otherwise, with each correction recorded in `issues`.
    """
    issues: list[ValidationIssue] = []
    kept: dict[Section, tuple[Claim, ...]] = {}

    for section, drafted in draft.by_section().items():
        survivors: list[Claim] = []
        for index, written in enumerate(drafted):
            if len(written.text) > MAX_CLAIM_CHARS:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.CLAIM_TOO_LONG,
                        detail=(
                            f"{len(written.text)} characters, limit {MAX_CLAIM_CHARS}; "
                            "dropped rather than truncated"
                        ),
                        section=section,
                        claim_index=index,
                    )
                )
                continue

            claim = written.to_claim()
            problems = _check_claim(claim, section, brief)
            if problems:
                issues.extend(
                    problem.model_copy(update={"claim_index": index}) for problem in problems
                )
                continue
            survivors.append(claim)

        if not survivors:
            issues.append(
                ValidationIssue(
                    code=IssueCode.EMPTY_SECTION,
                    detail="no claim survived validation; answered UNKNOWN",
                    section=section,
                )
            )
            survivors.append(_filler())
        kept[section] = tuple(survivors)

    sections = ReportSections.from_mapping(kept)
    confidence = _confidence(draft, brief, kept, issues)
    unknowns = tuple(
        section.value
        for section, claims in sections.iter_sections()
        if all(claim.basis is Basis.UNKNOWN for claim in claims)
    )

    return ResearchReport(
        contract_version=brief.contract_version,
        prompt_version=prompt_version,
        model_id=model_id,
        generated_at=generated_at,
        ticker=brief.ticker,
        score_version=brief.score.score_version,
        score_date=brief.score.score_date,
        brief_fingerprint=brief.fingerprint(),
        status=ResearchStatus.PARTIAL if issues else ResearchStatus.COMPLETE,
        sections=sections,
        unknowns=unknowns,
        confidence=confidence,
        issues=tuple(issues),
    )


def _confidence(
    draft: DraftReport,
    brief: ResearchBrief,
    kept: dict[Section, tuple[Claim, ...]],
    issues: list[ValidationIssue],
) -> ResearchConfidence:
    """Return the confidence the surviving evidence supports, lowering as needed."""
    survivors = [claim for claims in kept.values() for claim in claims]
    with_filing = [
        claim
        for claim in survivors
        if any(
            evidence_kind(cited) in {EvidenceKind.FILING, EvidenceKind.EXCERPT}
            for cited in claim.evidence
        )
    ]
    filing_coverage = len(with_filing) / len(survivors) if survivors else 0.0

    ceiling = confidence_ceiling(brief, cites_filing=bool(with_filing))
    level = draft.confidence
    if confidence_rank(level) > confidence_rank(ceiling):
        issues.append(
            ValidationIssue(
                code=IssueCode.CONFIDENCE_LOWERED,
                detail=f"claimed {level.value}, evidence supports at most {ceiling.value}",
            )
        )
        level = ceiling

    return ResearchConfidence(
        level=level,
        ceiling=ceiling,
        claimed=draft.confidence,
        rationale=_rationale(level, ceiling, draft.confidence, brief, filing_coverage, issues),
        metric_coverage=brief.score.data_coverage,
        filing_coverage=filing_coverage,
    )


def _rationale(
    level: ConfidenceLevel,
    ceiling: ConfidenceLevel,
    claimed: ConfidenceLevel,
    brief: ResearchBrief,
    filing_coverage: float,
    issues: list[ValidationIssue],
) -> str:
    """State why a report carries the confidence it does, from facts alone.

    Every clause is something the pipeline already knows: what the model asked
    for, what the evidence allows, how complete the metrics are, whether the
    ranking is provisional, whether any claim rests on a filing, and how much
    validation had to drop. Asking the model to write this instead would be
    asking the least reliable participant to grade itself — and a sentence about
    reliability that the model composed is not evidence about reliability.
    """
    if claimed is not level:
        opening = (
            f"lowered from {claimed.value} to {level.value}; "
            f"the evidence supports {ceiling.value} at most"
        )
    elif confidence_rank(level) < confidence_rank(ceiling):
        # The model asked for less than it was entitled to. Saying this "matches
        # the ceiling" would credit validation with a correction it never made.
        opening = f"{level.value} as claimed, below the {ceiling.value} the evidence would allow"
    else:
        opening = f"{level.value}, matching the {ceiling.value} ceiling the evidence supports"

    coverage = brief.score.data_coverage
    clauses = [
        opening,
        "metric coverage unknown" if coverage is None else f"metric coverage {coverage:.0%}",
        f"ranking {brief.score.ranking_state.value}",
        (
            f"{filing_coverage:.0%} of kept claims cite a filing"
            if brief.extractable_ids
            else "no filing text supplied"
        ),
        "no validation issues" if not issues else f"{len(issues)} validation issue(s)",
    ]
    return "; ".join(clauses) + "."


def failed_report(
    brief: ResearchBrief,
    *,
    prompt_version: str,
    model_id: str,
    generated_at: datetime,
    detail: str,
) -> ResearchReport:
    """Return a report recording that no usable output was produced.

    A provider outage, a quota, a response that would not parse. Stored rather
    than swallowed so the run reports what it could not do — and so the next run
    knows to try again — without any of it stopping the scan, the scoring or the
    ranking.

    Args:
        brief: The evidence that was to be used.
        prompt_version: The prompt that was to produce the report.
        model_id: The model that was called.
        generated_at: When the attempt was made, as an aware datetime.
        detail: What went wrong.

    Returns:
        A `FAILED` report with no sections and `LOW` confidence.
    """
    return ResearchReport(
        contract_version=brief.contract_version,
        prompt_version=prompt_version,
        model_id=model_id,
        generated_at=generated_at,
        ticker=brief.ticker,
        score_version=brief.score.score_version,
        score_date=brief.score.score_date,
        brief_fingerprint=brief.fingerprint(),
        status=ResearchStatus.FAILED,
        sections=ReportSections(),
        confidence=ResearchConfidence(
            level=ConfidenceLevel.LOW,
            ceiling=ConfidenceLevel.LOW,
            claimed=ConfidenceLevel.LOW,
            rationale=detail,
            metric_coverage=brief.score.data_coverage,
        ),
        issues=(ValidationIssue(code=IssueCode.EMPTY_SECTION, detail=detail),),
    )
