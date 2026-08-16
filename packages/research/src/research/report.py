"""What the model may return: claims, their basis, and what we accepted.

The central design choice is what this module does **not** contain. There is no
score field anywhere in `ResearchReport` — no rating, no component number, no
category, no target. The AI cannot override the CompounderScore because the
contract gives it nowhere to put one, which is a stronger guarantee than any
instruction in a prompt.

The second choice is the split between `DraftReport` and `ResearchReport`. A
draft is what the model said; a report is what we accepted after checking it.
Only `research.validation` produces the latter, so unvalidated output cannot be
persisted by accident — it is not the same type.

Every assertion is a `Claim` carrying its own `basis`, which is how a
deterministic restatement, a fact taken from a filing, an interpretation and an
admission of ignorance stay distinguishable after they have all been rendered
into the same paragraph.
"""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 — pydantic needs the runtime symbols
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_CLAIM_CHARS = 240
"""How long an **accepted** claim may be.

A claim is one assertion resting on one set of evidence. A paragraph that runs
past this is several claims wearing a coat, and the ones after the first tend to
be the unevidenced ones.

Enforced by `validate_report`, which drops the offending claim, and **not** on
the wire — see `MAX_DRAFT_CLAIM_CHARS`.
"""

MAX_DRAFT_CLAIM_CHARS = 4000
"""How long a claim may be *as the model writes it*.

Deliberately far above `MAX_CLAIM_CHARS`, and the reason is a live incident: a
DELL response arrived as HTTP 200, one claim of twenty-five ran a few characters
past 240, and pydantic rejected the **entire** draft. A paid, otherwise-good
report was lost to one long sentence, and the command died with a traceback.

The API cannot enforce this for us — the SDK strips length constraints out of the
transmitted schema and demotes them to a description hint — so a strict wire
bound buys nothing and costs whole reports. This bound exists only to stop a
pathological response consuming memory; the real rule is applied per claim during
validation, where overrunning costs one claim instead of thirteen sections.
"""


class _Frozen(BaseModel):
    """Base for immutable value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Basis(StrEnum):
    """Where one claim's authority comes from.

    The distinction the whole report exists to preserve. A reader deciding
    whether to act on a sentence needs to know whether it is a figure this system
    calculated, something the company wrote down, the model's reasoning, or an
    admission that nothing in the brief answers the question.
    """

    DETERMINISTIC = "DETERMINISTIC"
    """Restates a figure from Phase 1 or Phase 2. Authoritative, and not the
    model's opinion."""

    EXTRACTED = "EXTRACTED"
    """Taken from a filing supplied in the brief. The company's claim, not ours
    and not the model's."""

    INTERPRETATION = "INTERPRETATION"
    """The model's reasoning over the evidence. Where the value of the report
    lives, and the part to read most sceptically."""

    UNKNOWN = "UNKNOWN"
    """The brief does not answer this. A first-class answer, and always
    preferable to a plausible sentence."""


class ConfidenceLevel(StrEnum):
    """How much of the report rests on evidence rather than inference."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_CONFIDENCE_ORDER: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.LOW: 0,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.HIGH: 2,
}


def confidence_rank(level: ConfidenceLevel) -> int:
    """Return a level's position in the ordering, lowest first.

    Args:
        level: The level to rank.

    Returns:
        An integer suitable only for comparison with another rank.
    """
    return _CONFIDENCE_ORDER[level]


class ResearchStatus(StrEnum):
    """Whether a report is usable, and how completely."""

    COMPLETE = "COMPLETE"
    """Every claim survived validation unchanged."""

    PARTIAL = "PARTIAL"
    """The report stands, but claims were dropped or confidence was lowered. The
    reasons are in `issues`."""

    FAILED = "FAILED"
    """No usable output — the provider failed, or the response could not be
    parsed. Recorded rather than swallowed, and never a reason to fail the run."""


class Section(StrEnum):
    """The thirteen sections a report answers, in reading order.

    Each value is the name of the matching field on `ReportSections`, so the two
    cannot drift apart silently.
    """

    COMPANY_SUMMARY = "company_summary"
    WHY_IT_RANKED_HIGH = "why_it_ranked_high"
    GROWTH_DRIVERS = "growth_drivers"
    RECENT_DEVELOPMENTS = "recent_developments"
    CATALYSTS = "catalysts"
    FINANCIAL_QUALITY_INTERPRETATION = "financial_quality_interpretation"
    VALUATION_INTERPRETATION = "valuation_interpretation"
    MAJOR_RISKS = "major_risks"
    DILUTION_FINANCING_RISK = "dilution_financing_risk"
    BULL_CASE = "bull_case"
    BEAR_CASE = "bear_case"
    THESIS_BREAKERS = "thesis_breakers"
    WATCH_NEXT_QUARTER = "watch_next_quarter"


class IssueCode(StrEnum):
    """Why validation changed something the model returned."""

    UNRESOLVED_EVIDENCE = "UNRESOLVED_EVIDENCE"
    """A cited id is not in the brief. The citation was invented, so the claim
    carries no evidence at all."""

    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    """A claim that must rest on something cited nothing, or cited only the wrong
    kind of thing."""

    MISSING_EXTRACTED_EVIDENCE = "MISSING_EXTRACTED_EVIDENCE"
    """A claim about a filing's contents cited only filing metadata. The
    accession proves the filing exists; it proves nothing about what is in it,
    so the claim rests on the model's memory rather than on evidence."""

    BASIS_NOT_ALLOWED = "BASIS_NOT_ALLOWED"
    """The claim's basis is not permitted in that section — an interpretation
    offered as a summary of the score, for instance."""

    FABRICATED_NUMBER = "FABRICATED_NUMBER"
    """A figure appears that the brief does not contain. Either invented, or
    calculated — and this contract forbids both."""

    INVESTMENT_ADVICE = "INVESTMENT_ADVICE"
    """The claim recommends an action on the security. The tool ranks research
    candidates; it does not have a view."""

    EMPTY_SECTION = "EMPTY_SECTION"
    """Nothing survived in a section, so it was answered `UNKNOWN` rather than
    left blank."""

    CLAIM_TOO_LONG = "CLAIM_TOO_LONG"
    """The claim ran past the length an accepted claim may have. Dropped whole:
    validation never truncates or rewrites what the model said, because an
    edited sentence is no longer the model's claim and its evidence may no
    longer match it."""

    CONFIDENCE_LOWERED = "CONFIDENCE_LOWERED"
    """The model claimed more confidence than the evidence supports."""


class ValidationIssue(_Frozen):
    """One thing validation found and what it did about it.

    Attributes:
        code: The rule that fired.
        detail: What was wrong, specifically enough to debug a prompt from.
        section: The section it was found in, when it belongs to one.
        claim_index: Position of the offending claim within that section, as the
            model returned it.
    """

    code: IssueCode
    detail: str
    section: Section | None = None
    claim_index: int | None = None


class Claim(_Frozen):
    """One assertion, its basis, and what it rests on.

    Attributes:
        text: A single sentence. Numbers in it must already exist in the brief.
        basis: Where the claim's authority comes from.
        evidence: Ids from the brief. Empty only for an `UNKNOWN` claim.
    """

    text: str = Field(min_length=1, max_length=MAX_CLAIM_CHARS)
    basis: Basis
    evidence: tuple[str, ...] = ()


class ReportSections(_Frozen):
    """The thirteen sections, each a list of claims.

    Empty tuples are legal on a draft and on a failed report. A validated report
    never has one: a section with nothing to say carries a single `UNKNOWN`
    claim, because a blank section and an unanswerable question look identical
    to a reader and mean completely different things.
    """

    company_summary: tuple[Claim, ...] = ()
    why_it_ranked_high: tuple[Claim, ...] = ()
    growth_drivers: tuple[Claim, ...] = ()
    recent_developments: tuple[Claim, ...] = ()
    catalysts: tuple[Claim, ...] = ()
    financial_quality_interpretation: tuple[Claim, ...] = ()
    valuation_interpretation: tuple[Claim, ...] = ()
    major_risks: tuple[Claim, ...] = ()
    dilution_financing_risk: tuple[Claim, ...] = ()
    bull_case: tuple[Claim, ...] = ()
    bear_case: tuple[Claim, ...] = ()
    thesis_breakers: tuple[Claim, ...] = ()
    watch_next_quarter: tuple[Claim, ...] = ()

    def iter_sections(self) -> tuple[tuple[Section, tuple[Claim, ...]], ...]:
        """Return every section paired with its claims, in reading order.

        Written out rather than reflected over the fields: an explicit list is
        what makes a section added to the enum but not to this model a type
        error instead of a section that silently skips validation.

        Returns:
            Thirteen pairs, in `Section` order.
        """
        return (
            (Section.COMPANY_SUMMARY, self.company_summary),
            (Section.WHY_IT_RANKED_HIGH, self.why_it_ranked_high),
            (Section.GROWTH_DRIVERS, self.growth_drivers),
            (Section.RECENT_DEVELOPMENTS, self.recent_developments),
            (Section.CATALYSTS, self.catalysts),
            (Section.FINANCIAL_QUALITY_INTERPRETATION, self.financial_quality_interpretation),
            (Section.VALUATION_INTERPRETATION, self.valuation_interpretation),
            (Section.MAJOR_RISKS, self.major_risks),
            (Section.DILUTION_FINANCING_RISK, self.dilution_financing_risk),
            (Section.BULL_CASE, self.bull_case),
            (Section.BEAR_CASE, self.bear_case),
            (Section.THESIS_BREAKERS, self.thesis_breakers),
            (Section.WATCH_NEXT_QUARTER, self.watch_next_quarter),
        )

    @classmethod
    def from_mapping(cls, claims: Mapping[Section, Sequence[Claim]]) -> ReportSections:
        """Build sections from a section-keyed mapping.

        Args:
            claims: Claims per section. Missing sections default to empty.

        Returns:
            The assembled sections.
        """
        return cls(**{section.value: tuple(found) for section, found in claims.items()})


class DraftClaim(_Frozen):
    """One assertion as the model returns it, tagged with where it belongs.

    The wire form of a claim, and the reason it exists: thirteen parallel arrays
    of objects is a structure the API rejects outright — a live request returned
    `400 invalid_request_error: "Schema is too complex."`. One array of claims,
    each naming its own section, describes exactly the same thing in a fraction
    of the schema.

    The flattening stops at the wire. `ReportSections` is unchanged, every
    validation rule still runs per section, and a reader of a stored report sees
    the same thirteen fields as before.

    Attributes:
        section: Which of the thirteen sections this claim belongs to.
        text: A single sentence. Numbers in it must already exist in the brief.
            Bounded only against a pathological response — a claim longer than
            `MAX_CLAIM_CHARS` parses here and is dropped by validation, so one
            long sentence costs one claim rather than the whole report.
        basis: Where the claim's authority comes from.
        evidence: Ids from the brief. Empty only for an `UNKNOWN` claim.
    """

    section: Section
    text: str = Field(min_length=1, max_length=MAX_DRAFT_CLAIM_CHARS)
    basis: Basis
    evidence: tuple[str, ...] = ()

    def to_claim(self) -> Claim:
        """Return this claim without its section tag, ready to be grouped."""
        return Claim(text=self.text, basis=self.basis, evidence=self.evidence)


class DraftReport(_Frozen):
    """Unvalidated output, exactly as the model returned it.

    Deliberately a different type from `ResearchReport`. Nothing persists a
    draft, and nothing renders one to a reader — it is the input to validation
    and nothing else.

    Attributes:
        ticker: The company the model was asked about.
        claims: Every claim, flat, each naming its section. A section the model
            omitted simply has no claims here, which `grouped` turns into an
            empty section and validation turns into `UNKNOWN` plus an issue —
            the same outcome as before the flattening.
        confidence: How confident the model says it is. Validation may lower
            this and can never raise it.
        confidence_rationale: The model's reason, kept whether or not the level
            survives.
    """

    # This docstring is transmitted as the schema's description, so every
    # sentence in it is prompt. Two notes that therefore stay out of it:
    #
    # `confidence_rationale` is diagnostic only. Removing it (step 15) made the
    # model answer with a minimal object and no claims in four runs out of four,
    # so the field earns its place in generation — but nothing reads its text.
    # `_rationale` composes what a stored report carries, from coverage, ranking
    # state, filing evidence and the validation issues.
    #
    # `claims` carries no default, which is what puts it in the schema's
    # `required` list. An answer that omits it is then structurally invalid
    # rather than merely disappointing.
    ticker: str
    claims: tuple[DraftClaim, ...]
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_rationale: str = ""

    def by_section(self) -> dict[Section, tuple[DraftClaim, ...]]:
        """Return the flat claims grouped by section, still unconverted.

        Grouping stops short of building `Claim` objects on purpose: a claim
        that overruns `MAX_CLAIM_CHARS` cannot become one, and converting here
        would raise and lose the whole draft — the failure this design exists to
        prevent. Validation decides what becomes a `Claim`.

        Order within a section is the order the model produced, so a
        `claim_index` in an issue still points at what the model wrote. Every
        section appears, empty when the model said nothing about it — the
        emptiness is the signal validation acts on.

        Returns:
            Every section, in reading order, with the claims the model assigned.
        """
        grouped: dict[Section, list[DraftClaim]] = {section: [] for section in Section}
        for claim in self.claims:
            grouped[claim.section].append(claim)
        return {section: tuple(claims) for section, claims in grouped.items()}


class ResearchConfidence(_Frozen):
    """The confidence a report is allowed to carry, and the evidence for it.

    `level` is what the report claims after correction; `ceiling` is the most the
    evidence supports. The model may be less confident than the ceiling and never
    more, so a thinly-evidenced report cannot present itself as a well-researched
    one.

    Attributes:
        level: The corrected level.
        ceiling: The most the evidence permits, computed from coverage.
        claimed: What the model asked for, before correction.
        rationale: Why the level is what it is, composed by validation from the
            coverage, ranking state and issues that decided it. Never the
            model's own words.
        metric_coverage: The score's `data_coverage`, carried through.
        filing_coverage: Share of surviving claims that cite a filing, 0-1. Near
            zero throughout this contract version, since filing text is not yet
            extracted.
    """

    level: ConfidenceLevel
    ceiling: ConfidenceLevel
    claimed: ConfidenceLevel
    rationale: str = ""
    metric_coverage: float | None = Field(default=None, ge=0, le=1)
    filing_coverage: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _check_ceiling(self) -> Self:
        """Reject a level above its own ceiling.

        The correction belongs to validation; by the time a `ResearchConfidence`
        exists the two must already agree, so a bug that skipped the correction
        fails here rather than shipping an overconfident report.
        """
        if confidence_rank(self.level) > confidence_rank(self.ceiling):
            raise ValueError(f"confidence {self.level.value} exceeds ceiling {self.ceiling.value}")
        return self


class ResearchReport(_Frozen):
    """One validated research report, ready to store and to read.

    There is no score, rating or category field here, and adding one would defeat
    the contract: the CompounderScore is computed by `domain` and explained by
    this report, never revised by it.

    Attributes:
        contract_version: The research contract this was produced under.
        prompt_version: The prompt that produced it. Reports are not compared
            across prompt versions any more than scores are across formula
            versions.
        model_id: Which model wrote it.
        generated_at: When, as an aware UTC datetime.
        ticker: The company.
        score_version: The formula version of the score the report explains,
            echoed from the brief so the pairing is provable afterwards.
        score_date: The score date the report describes.
        brief_fingerprint: Hash of the evidence the report was written from. The
            cache key, and the way to tell whether a report is stale.
        status: Whether the report is complete, partial or failed.
        sections: The thirteen sections after correction.
        unknowns: Sections that ended up answered `UNKNOWN`. Computed, not
            model-supplied.
        confidence: The corrected confidence and the evidence behind it.
        issues: Everything validation found. Empty on a `COMPLETE` report.
    """

    contract_version: str
    prompt_version: str
    model_id: str
    generated_at: datetime
    ticker: str
    score_version: str
    score_date: date
    brief_fingerprint: str
    status: ResearchStatus
    confidence: ResearchConfidence
    sections: ReportSections = ReportSections()
    unknowns: tuple[str, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def _check_generated_at(self) -> Self:
        """Reject a naive timestamp.

        A report's age decides whether it is refreshed. An offset-naive datetime
        compares against an aware one by raising, which would surface as a broken
        daily run rather than as the storage bug it is.
        """
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return self
