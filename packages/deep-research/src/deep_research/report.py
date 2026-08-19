"""What a deep research model may return: seventeen sections, and nothing else.

The central design choice is inherited from Phase 3 and matters more here, not
less. There is **no score field anywhere in `DeepResearchReport`** — no rating,
no component number, no category, no price target, no recommendation. A deep
report reads more widely than a Phase 3 report and is therefore more tempting to
let conclude something; the contract answers that temptation structurally, by
providing nowhere to put a conclusion of that kind.

`research_conclusion` is the section a reader will look at first, and it is
defined as a statement about *evidence*: how much of the case rests on filings
and figures rather than inference, and how attractive the company looks as
something to research further. It is not a verdict on the security, and the
absence of BUY, SELL, HOLD and a target price from this module is the mechanism
rather than an oversight.

The draft/report split is Phase 3's, kept for the reason Phase 3 learned it: a
strict wire schema costs whole paid responses. `DeepDraftClaim` parses almost
anything a model sends, and `DeepClaim` is what survived checking. Only the
second is persisted, and only the (later) deep validator constructs it.
"""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 — pydantic needs the runtime symbols
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deep_research.evidence import ExternalEvidence, is_external
from research import ConfidenceLevel, ResearchStatus, confidence_rank

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_CLAIM_CHARS = 320
"""How long an **accepted** deep claim may be.

Wider than Phase 3's 240 because a deep claim often has to name its external
source inside the sentence — "according to X, reporting on date Y" — and the
attribution is not padding. Still a single assertion resting on a single set of
evidence; a paragraph that runs past this is several claims wearing a coat.
"""

MAX_DRAFT_CLAIM_CHARS = 4000
"""How long a claim may be *as the model writes it*.

Deliberately far above `MAX_CLAIM_CHARS`, for the reason Phase 3 records: a
single overlong sentence must cost one claim, not the entire response. The API
strips length constraints from the transmitted schema anyway, so a strict wire
bound buys nothing and risks everything. This exists only to stop a pathological
response consuming memory.
"""


class _Frozen(BaseModel):
    """Base for immutable value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DeepBasis(StrEnum):
    """Where one deep claim's authority comes from.

    Phase 3's four bases plus `EXTERNAL`, which is the whole reason this enum
    exists rather than reusing `research.Basis`. A `StrEnum` cannot be extended,
    and extending the Phase 3 one in place would edit a frozen contract — so the
    two are separate, and a reader of either always knows which world they are
    in.
    """

    DETERMINISTIC = "DETERMINISTIC"
    """Restates a figure this system calculated: a score line, a metric, a
    reported period, a vendor-supplied field. Authoritative, and not the model's
    opinion."""

    EXTRACTED = "EXTRACTED"
    """Taken from filing text supplied in the brief. The company's own words."""

    EXTERNAL = "EXTERNAL"
    """Taken from current external evidence — a third party's account, carrying
    that party's name and date. The claim is only ever as good as the source, and
    the source is always named."""

    INTERPRETATION = "INTERPRETATION"
    """The model's reasoning over the evidence. Where the value of a deep report
    lives, and the part to read most sceptically."""

    UNKNOWN = "UNKNOWN"
    """The brief does not answer this. A first-class answer, and always
    preferable to a plausible sentence."""


class DeepSection(StrEnum):
    """The seventeen sections a deep report answers, in reading order.

    Each value is the name of the matching field on `DeepSections`, so the two
    cannot drift apart silently.
    """

    COMPANY_OVERVIEW = "company_overview"
    CURRENT_SNAPSHOT = "current_snapshot"
    WHY_THE_ALGORITHM_LIKES_IT = "why_the_algorithm_likes_it"
    GROWTH_QUALITY = "growth_quality"
    FINANCIAL_QUALITY = "financial_quality"
    VALUATION = "valuation"
    LATEST_EARNINGS = "latest_earnings"
    RECENT_DEVELOPMENTS = "recent_developments"
    COMPETITIVE_POSITION = "competitive_position"
    CATALYSTS = "catalysts"
    MAJOR_RISKS = "major_risks"
    BULL_CASE = "bull_case"
    BEAR_CASE = "bear_case"
    THESIS_BREAKERS = "thesis_breakers"
    WHAT_THE_MARKET_MAY_BE_MISSING = "what_the_market_may_be_missing"
    WHAT_TO_WATCH_NEXT = "what_to_watch_next"
    RESEARCH_CONCLUSION = "research_conclusion"


class UnknownReason(StrEnum):
    """Why a section ended up answered `UNKNOWN`.

    Two very different situations wearing the same word, and conflating them
    misleads a reader in the one direction that matters. "We have nothing on
    this" invites them to go and look elsewhere. "We had plenty and could not
    turn it into something we would stand behind" is a statement about this
    tool, not about the company — and a reader told the first when the second is
    true will draw the wrong conclusion about the company.

    Determined in code from what the model produced and what validation did with
    it. The model is never asked which case it is in; it is the least reliable
    witness to its own failure.
    """

    NO_EVIDENCE = "NO_EVIDENCE"
    """The brief offered nothing for this section, and the model wrote nothing.

    The honest, common case: a company with no recent filings has no recent
    developments, and a company nobody has written about has no external
    context."""

    NO_VALID_CLAIMS = "NO_VALID_CLAIMS"
    """The model wrote claims for this section and validation rejected all of them.

    The evidence was there. What came back was an investment instruction, an
    invented figure, a return forecast or an overlong sentence, and none of it
    could be shown to a reader. Saying "no evidence" here would blame the data
    for a failure of the generation."""


class DeepIssueCode(StrEnum):
    """Why deep validation changed something the model returned.

    Its own enum rather than `research.IssueCode`, because the external
    namespace brings failures Phase 3 cannot have — a claim resting on a source
    nobody supplied, or on a tier too weak for what it asserts.
    """

    UNRESOLVED_EVIDENCE = "UNRESOLVED_EVIDENCE"
    """A cited id is not in the brief. The citation was invented, so the claim
    carries no evidence at all."""

    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    """A claim that must rest on something cited nothing, or cited only the
    wrong kind of thing."""

    MISSING_EXTRACTED_EVIDENCE = "MISSING_EXTRACTED_EVIDENCE"
    """A claim about a filing's contents cited only filing metadata. An
    accession proves the filing exists; it proves nothing about what is in it."""

    MISSING_EXTERNAL_EVIDENCE = "MISSING_EXTERNAL_EVIDENCE"
    """A claim about current events cited no external source. The deep-research
    counterpart of the rule above: without a `W.` citation the sentence rests on
    the model's recollection of the news, which is exactly what this layer was
    built to replace."""

    BASIS_NOT_ALLOWED = "BASIS_NOT_ALLOWED"
    """The claim's basis is not permitted in that section — a news item offered
    as an account of why the score is what it is, for instance."""

    UNKNOWN_WITH_EVIDENCE = "UNKNOWN_WITH_EVIDENCE"
    """A claim declared `UNKNOWN` while citing evidence. Either it is not
    unknown, or the citation is decorative; both readings are wrong."""

    FABRICATED_NUMBER = "FABRICATED_NUMBER"
    """A figure appears that the evidence does not contain. Either invented, or
    calculated — and this contract forbids both."""

    INVESTMENT_ADVICE = "INVESTMENT_ADVICE"
    """The claim recommends an action on the security, or states a target price.
    The tool produces research; it does not have a view."""

    EMPTY_SECTION = "EMPTY_SECTION"
    """Nothing survived in a section, so it was answered `UNKNOWN` rather than
    left blank."""

    CLAIM_TOO_LONG = "CLAIM_TOO_LONG"
    """The claim ran past the length an accepted claim may have. Dropped whole:
    validation never truncates or rewrites what the model said."""

    STALE_EVIDENCE = "STALE_EVIDENCE"
    """A claim presented as current rests on a source older than the brief's
    currency window."""

    CONFIDENCE_LOWERED = "CONFIDENCE_LOWERED"
    """The model claimed more confidence than the evidence supports."""


class DeepValidationIssue(_Frozen):
    """One thing deep validation found, and what it did about it.

    Attributes:
        code: The rule that fired.
        detail: What was wrong, specifically enough to debug a prompt from.
        section: The section it was found in, when it belongs to one.
        claim_index: Position of the offending claim within that section, as the
            model returned it.
    """

    code: DeepIssueCode
    detail: str
    section: DeepSection | None = None
    claim_index: int | None = None


class DeepClaim(_Frozen):
    """One accepted assertion, its basis, and what it rests on.

    Attributes:
        text: A single sentence. Numbers in it must already exist in the
            evidence.
        basis: Where the claim's authority comes from.
        evidence: Ids from the brief. Empty only for an `UNKNOWN` claim.
        unknown_reason: On a fallback `UNKNOWN` claim, why the section is empty.
            None on every claim the model actually wrote, including its own
            `UNKNOWN` answers — those carry the model's words, and overwriting
            their reason would misreport who said what.
    """

    text: str = Field(min_length=1, max_length=MAX_CLAIM_CHARS)
    basis: DeepBasis
    evidence: tuple[str, ...] = ()
    unknown_reason: UnknownReason | None = None


class DeepDraftClaim(_Frozen):
    """One assertion as the model returns it, tagged with where it belongs.

    The wire form, flat rather than nested, because seventeen parallel arrays of
    objects is a schema the API rejects outright — Phase 3 hit exactly that with
    thirteen. One array of claims, each naming its own section, describes the
    same thing in a fraction of the schema, and the flattening stops at the wire.

    Deliberately permissive. A claim with an impossible basis, an invented
    citation or an overlong sentence parses here and is dropped by the deep
    validator, so one bad claim costs one claim rather than the whole response.

    Attributes:
        section: Which of the seventeen sections this claim belongs to.
        text: A single sentence, bounded only against a pathological response.
        basis: Where the claim's authority comes from.
        evidence: Ids from the brief. Empty only for an `UNKNOWN` claim.
    """

    section: DeepSection
    text: str = Field(min_length=1, max_length=MAX_DRAFT_CLAIM_CHARS)
    basis: DeepBasis
    evidence: tuple[str, ...] = ()

    def to_claim(self) -> DeepClaim:
        """Return this claim without its section tag, ready to be grouped."""
        return DeepClaim(text=self.text, basis=self.basis, evidence=self.evidence)


class DeepSections(_Frozen):
    """The seventeen sections, each a list of accepted claims.

    Empty tuples are legal on a draft and on a failed report. A validated report
    never has one: a section with nothing to say carries a single `UNKNOWN`
    claim, because a blank section and an unanswerable question look identical to
    a reader and mean completely different things.
    """

    company_overview: tuple[DeepClaim, ...] = ()
    current_snapshot: tuple[DeepClaim, ...] = ()
    why_the_algorithm_likes_it: tuple[DeepClaim, ...] = ()
    growth_quality: tuple[DeepClaim, ...] = ()
    financial_quality: tuple[DeepClaim, ...] = ()
    valuation: tuple[DeepClaim, ...] = ()
    latest_earnings: tuple[DeepClaim, ...] = ()
    recent_developments: tuple[DeepClaim, ...] = ()
    competitive_position: tuple[DeepClaim, ...] = ()
    catalysts: tuple[DeepClaim, ...] = ()
    major_risks: tuple[DeepClaim, ...] = ()
    bull_case: tuple[DeepClaim, ...] = ()
    bear_case: tuple[DeepClaim, ...] = ()
    thesis_breakers: tuple[DeepClaim, ...] = ()
    what_the_market_may_be_missing: tuple[DeepClaim, ...] = ()
    what_to_watch_next: tuple[DeepClaim, ...] = ()
    research_conclusion: tuple[DeepClaim, ...] = ()

    def iter_sections(self) -> tuple[tuple[DeepSection, tuple[DeepClaim, ...]], ...]:
        """Return every section paired with its claims, in reading order.

        Written out rather than reflected over the fields: an explicit list is
        what makes a section added to the enum but not to this model a type error
        instead of a section that silently skips validation.

        Returns:
            Seventeen pairs, in `DeepSection` order.
        """
        return (
            (DeepSection.COMPANY_OVERVIEW, self.company_overview),
            (DeepSection.CURRENT_SNAPSHOT, self.current_snapshot),
            (DeepSection.WHY_THE_ALGORITHM_LIKES_IT, self.why_the_algorithm_likes_it),
            (DeepSection.GROWTH_QUALITY, self.growth_quality),
            (DeepSection.FINANCIAL_QUALITY, self.financial_quality),
            (DeepSection.VALUATION, self.valuation),
            (DeepSection.LATEST_EARNINGS, self.latest_earnings),
            (DeepSection.RECENT_DEVELOPMENTS, self.recent_developments),
            (DeepSection.COMPETITIVE_POSITION, self.competitive_position),
            (DeepSection.CATALYSTS, self.catalysts),
            (DeepSection.MAJOR_RISKS, self.major_risks),
            (DeepSection.BULL_CASE, self.bull_case),
            (DeepSection.BEAR_CASE, self.bear_case),
            (DeepSection.THESIS_BREAKERS, self.thesis_breakers),
            (DeepSection.WHAT_THE_MARKET_MAY_BE_MISSING, self.what_the_market_may_be_missing),
            (DeepSection.WHAT_TO_WATCH_NEXT, self.what_to_watch_next),
            (DeepSection.RESEARCH_CONCLUSION, self.research_conclusion),
        )

    @classmethod
    def from_mapping(cls, claims: Mapping[DeepSection, Sequence[DeepClaim]]) -> DeepSections:
        """Build sections from a section-keyed mapping.

        Args:
            claims: Claims per section. Missing sections default to empty.

        Returns:
            The assembled sections.
        """
        return cls(**{section.value: tuple(found) for section, found in claims.items()})


class DeepResearchDraft(_Frozen):
    """Unvalidated deep output, exactly as the model returned it.

    Deliberately a different type from `DeepResearchReport`. Nothing persists a
    draft and nothing renders one to a reader — it is the input to validation and
    nothing else.

    Attributes:
        ticker: The company the model was asked about.
        claims: Every claim, flat, each naming its section. A section the model
            omitted simply has no claims here, which `by_section` turns into an
            empty section and validation turns into `UNKNOWN` plus an issue.
        confidence: How confident the model says it is. Validation may lower this
            and can never raise it.
        confidence_rationale: The model's reason, kept whether or not the level
            survives. Diagnostic only — nothing reads its text into a report.
    """

    ticker: str
    claims: tuple[DeepDraftClaim, ...]
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_rationale: str = ""

    def by_section(self) -> dict[DeepSection, tuple[DeepDraftClaim, ...]]:
        """Return the flat claims grouped by section, still unconverted.

        Grouping stops short of building `DeepClaim` objects on purpose: a claim
        that overruns `MAX_CLAIM_CHARS` cannot become one, and converting here
        would raise and lose the whole draft. Validation decides what becomes a
        `DeepClaim`.

        Order within a section is the order the model produced, so a
        `claim_index` in an issue still points at what the model wrote. Every
        section appears, empty when the model said nothing about it — the
        emptiness is the signal validation acts on.

        Returns:
            Every section, in reading order, with the claims the model assigned.
        """
        grouped: dict[DeepSection, list[DeepDraftClaim]] = {section: [] for section in DeepSection}
        for claim in self.claims:
            grouped[claim.section].append(claim)
        return {section: tuple(claims) for section, claims in grouped.items()}


class DeepConfidence(_Frozen):
    """The confidence a deep report may carry, and the evidence for it.

    `level` is what the report claims after correction; `ceiling` is the most the
    evidence supports. The model may be less confident than the ceiling and never
    more, so a thinly-evidenced report cannot present itself as a well-researched
    one.

    Attributes:
        level: The corrected level.
        ceiling: The most the evidence permits.
        claimed: What the model asked for, before correction.
        rationale: Why the level is what it is, composed by validation from the
            coverage, freshness and issues that decided it. Never the model's own
            words — asking the least reliable participant to grade itself is not
            evidence about reliability.
        metric_coverage: The score's `data_coverage`, carried through.
        filing_coverage: Share of surviving claims citing filing text, 0-1.
        external_coverage: Share of surviving claims citing an external source,
            0-1. Recorded separately from `filing_coverage` because a report
            grounded entirely in news and one grounded entirely in filings are
            differently trustworthy, and averaging them would hide that.
    """

    level: ConfidenceLevel
    ceiling: ConfidenceLevel
    claimed: ConfidenceLevel
    rationale: str = ""
    metric_coverage: float | None = Field(default=None, ge=0, le=1)
    filing_coverage: float = Field(default=0.0, ge=0, le=1)
    external_coverage: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _check_ceiling(self) -> Self:
        """Reject a level above its own ceiling.

        The correction belongs to validation; by the time a `DeepConfidence`
        exists the two must already agree, so a bug that skipped the correction
        fails here rather than shipping an overconfident report.
        """
        if confidence_rank(self.level) > confidence_rank(self.ceiling):
            raise ValueError(f"confidence {self.level.value} exceeds ceiling {self.ceiling.value}")
        return self


class DeepResearchReport(_Frozen):
    """One validated deep research report, ready to store and to read.

    There is no score, rating, category, recommendation or target field here, and
    adding one would defeat the contract. The CompounderScore is computed by
    `domain`, explained by this report, and revised by neither this report nor
    anything in the `W.` namespace that fed it.

    Attributes:
        contract_version: The deep contract this was produced under.
        prompt_version: The prompt that produced it. Reports are not compared
            across prompt versions any more than scores are across formula
            versions.
        model_id: Which model wrote it.
        generated_at: When, as an aware UTC datetime.
        ticker: The company.
        as_of: The score date the report describes.
        score_version: The formula version of the score the report explains,
            echoed from the brief so the pairing is provable afterwards.
        deterministic_fingerprint: Hash of the calculated and filed evidence.
        evidence_fingerprint: Hash of that plus the external evidence. The cache
            key: a stored report may be reused when and only when this matches.
        status: Whether the report is complete, partial or failed. Reused from
            the Phase 3 contract, which already means exactly this.
        confidence: The corrected confidence and the evidence behind it.
        sections: The seventeen sections after correction.
        external_evidence: The external items the surviving claims actually
            cite, carried so a stored report resolves its own `W.` citations. A
            reader six weeks later needs the title, publisher, URL and date, and
            a bare id in a JSON column would give them none of it. Items nothing
            cites are dropped, which is what bounds the row.
        unknowns: Sections that ended up answered `UNKNOWN`. Computed, not
            model-supplied.
        issues: Everything validation found. Empty on a `COMPLETE` report.
    """

    contract_version: str
    prompt_version: str
    model_id: str
    generated_at: datetime
    ticker: str
    as_of: date
    score_version: str
    deterministic_fingerprint: str
    evidence_fingerprint: str
    status: ResearchStatus
    confidence: DeepConfidence
    sections: DeepSections = DeepSections()
    external_evidence: tuple[ExternalEvidence, ...] = ()
    unknowns: tuple[str, ...] = ()
    issues: tuple[DeepValidationIssue, ...] = ()

    @model_validator(mode="after")
    def _check_generated_at(self) -> Self:
        """Reject a naive timestamp.

        A report's age decides whether it is refreshed. An offset-naive datetime
        compares against an aware one by raising, which would surface as a broken
        run rather than as the storage bug it is.
        """
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return self

    @property
    def unknown_reasons(self) -> dict[str, UnknownReason]:
        """Why each `UNKNOWN` section is empty, keyed by section name.

        What a reading surface should render from, rather than parsing issue
        strings or the fallback sentence. A section answered `UNKNOWN` in the
        model's own words reports `NO_EVIDENCE`: an `UNKNOWN` claim asserts
        absence by definition, whoever phrased it.

        Returns:
            One entry per section in `unknowns`.
        """
        found: dict[str, UnknownReason] = {}
        for section, claims in self.sections.iter_sections():
            if not claims or any(claim.basis is not DeepBasis.UNKNOWN for claim in claims):
                continue
            reasons = {claim.unknown_reason for claim in claims if claim.unknown_reason}
            found[section.value] = reasons.pop() if len(reasons) == 1 else UnknownReason.NO_EVIDENCE
        return found

    @property
    def cited_external_ids(self) -> frozenset[str]:
        """Every external id the surviving claims cite.

        Returns:
            The `W.` ids appearing in accepted claims. Should always be a subset
            of the ids in `external_evidence`; a difference means a report was
            assembled without carrying the sources it rests on.
        """
        return frozenset(
            cited
            for _, claims in self.sections.iter_sections()
            for claim in claims
            for cited in claim.evidence
            if is_external(cited)
        )
