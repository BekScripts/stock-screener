"""Checking a deep draft against its brief, and accepting only what survives.

The Phase 3 validator's rules, plus the two the external namespace makes
necessary. Everything here assumes the model tried to comply and sometimes did
not, because the failure mode of a research tool is not a wrong number — it is a
fluent, well-cited, wrong number, and a deep report reads widely enough to make
one easy to write.

Seven rules, applied to every claim:

1. **The basis must fit the section.** A news item cannot account for why the
   deterministic score is what it is.
2. **Citations must resolve.** An id absent from the brief is invented evidence,
   not weak evidence, and the claim resting on it is dropped.
3. **The right kind of citation.** `DETERMINISTIC` cites `S./M./F./E.`,
   `EXTRACTED` cites `X.`, `EXTERNAL` cites `W.`, `INTERPRETATION` cites
   anything resolved, `UNKNOWN` cites nothing. A bare `D.` accession proves a
   filing exists and nothing about its contents.
4. **Every number must already exist, in the evidence that claim cites.** This
   is stricter than "somewhere in the brief". A figure printed in one article is
   evidence for a sentence citing that article and for no other — otherwise any
   quoted number would legitimise any claim, which is the laundering the `W.`
   namespace makes cheapest.
5. **No new arithmetic.** No ratio, growth rate, multiple or percentage the
   deterministic layers did not produce. Display rounding is the only
   transformation allowed.
6. **No instruction and no prediction.** Phase 3 forbade advice; a deep report
   additionally forbids an expected return, because a number attached to a future
   is a forecast whatever the surrounding words say.
7. **One claim, one sentence.** A claim past `MAX_CLAIM_CHARS` is dropped whole,
   never truncated: an edited sentence is no longer the model's claim and its
   evidence may no longer match what is left of it.

A failed claim is dropped, not rewritten, and the drop is recorded. A section
emptied by dropping is answered `UNKNOWN`.

**Confidence is computed here, never taken from the model.** A model asked to
grade its own reliability is the least reliable participant in the exercise, and
fifteen thin news stories must not be able to buy a confident report.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from deep_research.evidence import SourceTier, is_external
from deep_research.provenance import claim_problems, supports
from deep_research.report import (
    MAX_CLAIM_CHARS,
    DeepBasis,
    DeepClaim,
    DeepConfidence,
    DeepIssueCode,
    DeepResearchReport,
    DeepSection,
    DeepSections,
    DeepValidationIssue,
    UnknownReason,
)
from research import (
    ConfidenceLevel,
    EvidenceKind,
    RankingState,
    ResearchStatus,
    evidence_kind,
    find_advice,
    quoted_quantities,
)
from research.quantities import LITERAL
from research.quantities import unsupported_figures as _unsupported_figures

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from deep_research.brief import DeepResearchBrief
    from deep_research.report import DeepResearchDraft
    from research import Quantity

__all__ = [
    "MIN_COVERAGE_FOR_HIGH",
    "MIN_COVERAGE_FOR_MEDIUM",
    "NO_EVIDENCE_TEXT",
    "NO_VALID_CLAIMS_TEXT",
    "deep_confidence",
    "find_forecast",
    "find_rating",
    "unsupported_figures",
    "validate_deep_report",
]

MIN_COVERAGE_FOR_MEDIUM = 0.6
"""Below this share of scoring metrics, no report may claim better than `LOW`."""

MIN_COVERAGE_FOR_HIGH = 0.8
"""Coverage required before `HIGH` is reachable at all."""

MIN_GROUNDED_FOR_HIGH = 0.5
"""Share of surviving claims that must rest on a filing or a named source for `HIGH`.

Interpretation is where a deep report earns its keep, but a report that is mostly
interpretation is mostly the model talking. Half is the point at which the
evidence, rather than the reasoning, is carrying the document.
"""

NO_EVIDENCE_TEXT = "The available evidence does not address this section."
"""What a section says when the brief genuinely offered nothing.

Deliberately flat, and never softened into a sentence that sounds like an answer.
"""

NO_VALID_CLAIMS_TEXT = "Evidence is available, but no statement about it passed validation."
"""What a section says when everything the model wrote for it was rejected.

The distinction this phase exists to draw. Telling a reader the evidence was
absent, when in fact it was present and the generated claims were unusable,
blames the data for a failure of the generation — and sends the reader looking
for information the system already had.

Deliberately says nothing about *what* was rejected. The dropped text was
rejected because it was an instruction, an invented figure or a forecast, and
surfacing it would publish exactly what validation refused to publish. The codes
are in `issues` for whoever is debugging the prompt.
"""

_FORECAST = re.compile(
    r"(?<![\w-])("
    r"expected return|total return of|will return|should return|"
    r"upside of|downside of|implies? (?:a )?(?:\d|upside|downside)|"
    r"could (?:reach|hit|double|triple)|"
    r"we (?:expect|forecast|project) .{0,30}(?:\d|%)|"
    r"fair value of|worth \$|valued at \$\d+.{0,12}(?:per share|a share)|"
    r"cagr of \d|compound(?:ed)? at \d"
    r")",
    re.IGNORECASE,
)
"""Phrases that turn an interpretation into a prediction with a number on it.

Narrower than it looks. "Growth may prove durable" is interpretation and stays
legal; "implies 30% upside" is a forecast and does not. The distinction is
whether a number is attached to a future, because that is the sentence a reader
will act on.
"""


_RATING = re.compile(
    r"(?<![\w-])("
    r"(?:rate[sd]?|rating)\s+(?:the\s+)?(?:shares?|stock|it)?\s*(?:a|as|an)?\s*"
    r"(?:strong\s+)?(?:buy|sell|hold)|"
    r"strong\s+buy|"
    r"(?:a|our|the)\s+(?:buy|sell|hold)\s+rating|"
    r"rated\s+(?:a\s+)?(?:buy|sell|hold)"
    r")",
    re.IGNORECASE,
)
"""Rating language Phase 3's advice check does not reach.

Phase 3 forbids "buy" and "sell" outright, which covers most of this, but a
research note can issue a rating without either word — "we rate the shares a
hold" is an instruction wearing analyst clothing. Added here rather than in
`research.find_advice` because Phase 3 is frozen and because a bare "hold" is
innocent in ordinary prose ("holding company", "shareholders hold"), so the
pattern only fires in an explicit rating construction.
"""


def find_rating(text: str) -> str | None:
    """Return the first analyst-rating phrase in a claim, if any.

    Args:
        text: The claim's text.

    Returns:
        The matched phrase, or None when the claim rates nothing.
    """
    found = _RATING.search(text)
    return found.group(0) if found is not None else None


def find_forecast(text: str) -> str | None:
    """Return the first return-forecast phrase in a claim, if any.

    Args:
        text: The claim's text.

    Returns:
        The matched phrase, or None when the claim predicts no return.
    """
    found = _FORECAST.search(text)
    return found.group(0) if found is not None else None


def unsupported_figures(
    text: str, brief: DeepResearchBrief, *, cited: Sequence[str] = ()
) -> tuple[str, ...]:
    """Return every number in a claim that the evidence *it cites* does not contain.

    Three pools, and the difference between them is the whole rule.

    The brief's structured quantities are available to every claim: they are
    facts about the company, addressable by id, and any claim may restate one.

    Numbers inside filing text and inside external excerpts are available **only
    to a claim citing the item they appear in**. A revenue figure printed in one
    news article is not evidence for a sentence citing a different article, and
    a figure in one 8-K says nothing about a sentence describing another. Pooling
    either would let any quoted number legitimise any claim — the cheapest way
    for a well-cited report to become a wrong one.

    Bare integers are additionally matched against every digit sequence in the
    brief's structured fields, because a year or a quarter count is not an
    arithmetic claim. Prose is excluded from that fallback: it is full of
    incidental integers, and admitting it brief-wide would undo the scoping.

    Args:
        text: The claim's text.
        brief: The evidence.
        cited: The ids this claim cites. Only the `X.` and `W.` ones contribute
            quoted numbers, and only theirs.

    Returns:
        The unsupported figures as written, in the order they appear.
    """
    quoted: list[Quantity] = []
    for identifier in cited:
        if evidence_kind(identifier) is EvidenceKind.EXCERPT or is_external(identifier):
            passage = brief.excerpt_text(identifier)
            if passage:
                quoted.extend(quoted_quantities(passage))

    structured = brief.model_copy(update={"excerpts": (), "external": ()}).model_dump_json()

    return _unsupported_figures(
        text,
        quantities=(*brief.quantities, *quoted),
        literals={float(token) for token in LITERAL.findall(structured)},
        scale_values=brief.scale_values,
    )


def _check_claim(
    claim: DeepClaim, section: DeepSection, brief: DeepResearchBrief
) -> tuple[DeepValidationIssue, ...]:
    """Return everything wrong with one claim. Empty means it survives."""
    found: list[DeepValidationIssue] = []

    def issue(code: DeepIssueCode, detail: str) -> None:
        found.append(DeepValidationIssue(code=code, detail=detail, section=section))

    for problem in claim_problems(claim, section, brief):
        if "is not permitted in" in problem:
            issue(DeepIssueCode.BASIS_NOT_ALLOWED, problem)
        elif "not in the brief" in problem:
            issue(DeepIssueCode.UNRESOLVED_EVIDENCE, problem)
        elif "cite an X. excerpt" in problem:
            issue(DeepIssueCode.MISSING_EXTRACTED_EVIDENCE, problem)
        elif "must cite nothing" in problem:
            issue(DeepIssueCode.UNKNOWN_WITH_EVIDENCE, problem)
        elif claim.basis is DeepBasis.EXTERNAL:
            issue(DeepIssueCode.MISSING_EXTERNAL_EVIDENCE, problem)
        else:
            issue(DeepIssueCode.MISSING_EVIDENCE, problem)

    resolved = tuple(cited for cited in claim.evidence if cited in brief.evidence_ids)
    grounded = tuple(cited for cited in resolved if supports(claim.basis, cited))

    fabricated = unsupported_figures(claim.text, brief, cited=grounded)
    if fabricated:
        issue(
            DeepIssueCode.FABRICATED_NUMBER,
            f"figures not present in the cited evidence: {', '.join(fabricated)}",
        )

    advice = find_advice(claim.text)
    if advice is not None:
        issue(DeepIssueCode.INVESTMENT_ADVICE, f"recommendation language: {advice!r}")

    rating = find_rating(claim.text)
    if rating is not None:
        issue(DeepIssueCode.INVESTMENT_ADVICE, f"analyst rating: {rating!r}")

    forecast = find_forecast(claim.text)
    if forecast is not None:
        issue(DeepIssueCode.INVESTMENT_ADVICE, f"return forecast: {forecast!r}")

    return tuple(found)


def _filler(reason: UnknownReason) -> DeepClaim:
    """Return the claim a section carries when nothing survived.

    Args:
        reason: Why the section is empty, decided by the caller from what the
            model produced and what validation did with it.

    Returns:
        An `UNKNOWN` claim carrying both the reason and text that matches it.
    """
    text = NO_VALID_CLAIMS_TEXT if reason is UnknownReason.NO_VALID_CLAIMS else NO_EVIDENCE_TEXT
    return DeepClaim(text=text, basis=DeepBasis.UNKNOWN, unknown_reason=reason)


def validate_deep_report(
    draft: DeepResearchDraft,
    brief: DeepResearchBrief,
    *,
    prompt_version: str,
    model_id: str,
    generated_at: datetime,
) -> DeepResearchReport:
    """Check a deep draft against its brief and return what may be kept.

    Never raises on bad model output: a draft full of invented citations produces
    a `PARTIAL` report whose sections say they have nothing to offer, which is an
    outcome a reader can act on. A provider that failed to answer at all is a
    different matter and is not this function's business — the runner records the
    failure and persists nothing, because a fabricated report is worse than none.

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
    issues: list[DeepValidationIssue] = []
    kept: dict[DeepSection, tuple[DeepClaim, ...]] = {}

    for section, drafted in draft.by_section().items():
        survivors: list[DeepClaim] = []
        for index, written in enumerate(drafted):
            if len(written.text) > MAX_CLAIM_CHARS:
                issues.append(
                    DeepValidationIssue(
                        code=DeepIssueCode.CLAIM_TOO_LONG,
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
            # The distinction the reader needs: did we have nothing, or did we
            # have something and reject all of it? Decided from what the model
            # actually submitted, never asked of the model.
            reason = UnknownReason.NO_VALID_CLAIMS if drafted else UnknownReason.NO_EVIDENCE
            issues.append(
                DeepValidationIssue(
                    code=DeepIssueCode.EMPTY_SECTION,
                    detail=(f"no claim survived validation; answered UNKNOWN ({reason.value})"),
                    section=section,
                )
            )
            survivors.append(_filler(reason))
        kept[section] = tuple(survivors)

    sections = DeepSections.from_mapping(kept)
    survivors = [claim for claims in kept.values() for claim in claims]
    confidence = deep_confidence(brief, survivors, issues)
    unknowns = tuple(
        section.value
        for section, claims in sections.iter_sections()
        if all(claim.basis is DeepBasis.UNKNOWN for claim in claims)
    )
    cited = {
        identifier
        for claim in survivors
        for identifier in claim.evidence
        if is_external(identifier)
    }

    return DeepResearchReport(
        contract_version=brief.contract_version,
        prompt_version=prompt_version,
        model_id=model_id,
        generated_at=generated_at,
        ticker=brief.ticker,
        as_of=brief.as_of,
        score_version=brief.score.score_version,
        deterministic_fingerprint=brief.deterministic_fingerprint(),
        evidence_fingerprint=brief.evidence_fingerprint(),
        status=ResearchStatus.PARTIAL if issues else ResearchStatus.COMPLETE,
        confidence=confidence,
        sections=sections,
        external_evidence=tuple(item for item in brief.external if item.evidence_id in cited),
        unknowns=unknowns,
        issues=tuple(issues),
    )


def deep_confidence(
    brief: DeepResearchBrief,
    survivors: Sequence[DeepClaim],
    issues: Sequence[DeepValidationIssue],
) -> DeepConfidence:
    """Return the confidence the evidence supports, computed and never asked for.

    Six inputs, all of them facts the pipeline already knows: how much of the
    score's metric set was available, whether a vendor verified the inputs, how
    much of the report rests on a filing or a named source rather than on
    reasoning, how many sections came back `UNKNOWN`, and how much validation had
    to drop.

    The model's own view is recorded as `claimed` and used for nothing. Asking
    the least reliable participant to grade itself produces a sentence about
    reliability, not evidence about it.

    A deliberate consequence: a company with no external evidence at all can
    still reach `MEDIUM` on good metric coverage and real filing text, and a
    company with fifteen thin news stories cannot reach `HIGH` on volume alone —
    the ceiling reads tier and coverage, not count.

    Args:
        brief: The evidence the report was written from.
        survivors: The claims that were kept.
        issues: Everything validation found.

    Returns:
        The corrected confidence, its ceiling, and the evidence behind both.
    """
    real = [claim for claim in survivors if claim.basis is not DeepBasis.UNKNOWN]
    total = len(real) or 1

    with_filing = sum(
        1
        for claim in real
        if any(evidence_kind(cited) is EvidenceKind.EXCERPT for cited in claim.evidence)
    )
    with_external = sum(1 for claim in real if any(is_external(cited) for cited in claim.evidence))
    filing_coverage = with_filing / total
    external_coverage = with_external / total
    grounded = (with_filing + with_external) / total

    coverage = brief.score.data_coverage
    best_tier = min(
        (item.tier for item in brief.external),
        key=lambda tier: {
            SourceTier.TIER_1_PRIMARY: 0,
            SourceTier.TIER_2_REPUTABLE: 1,
            SourceTier.TIER_3_SUPPORTING: 2,
        }[tier],
        default=None,
    )

    ceiling = ConfidenceLevel.LOW
    if (
        coverage is not None
        and coverage >= MIN_COVERAGE_FOR_MEDIUM
        and (brief.excerpts or brief.external)
    ):
        ceiling = ConfidenceLevel.MEDIUM
    if (
        coverage is not None
        and coverage >= MIN_COVERAGE_FOR_HIGH
        and brief.score.ranking_state is RankingState.FINAL
        and bool(brief.excerpts)
        and grounded >= MIN_GROUNDED_FOR_HIGH
        and best_tier in {SourceTier.TIER_1_PRIMARY, SourceTier.TIER_2_REPUTABLE}
    ):
        ceiling = ConfidenceLevel.HIGH

    return DeepConfidence(
        level=ceiling,
        ceiling=ceiling,
        claimed=ceiling,
        rationale=_rationale(brief, coverage, filing_coverage, external_coverage, issues),
        metric_coverage=coverage,
        filing_coverage=min(1.0, filing_coverage),
        external_coverage=min(1.0, external_coverage),
    )


def _rationale(
    brief: DeepResearchBrief,
    coverage: float | None,
    filing_coverage: float,
    external_coverage: float,
    issues: Sequence[DeepValidationIssue],
) -> str:
    """State why a report carries the confidence it does, from facts alone."""
    clauses = [
        "metric coverage unknown" if coverage is None else f"metric coverage {coverage:.0%}",
        f"ranking {brief.score.ranking_state.value}",
        (
            f"{filing_coverage:.0%} of claims cite filing text"
            if brief.excerpts
            else "no filing text supplied"
        ),
        (
            f"{external_coverage:.0%} of claims cite an external source "
            f"({len(brief.external)} supplied)"
            if brief.external
            else "no external evidence supplied"
        ),
        "no validation issues" if not issues else f"{len(issues)} validation issue(s)",
    ]
    return "; ".join(clauses) + "."
