"""Deep validation: what survives a draft, and what the report may then claim."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from deep_research import (
    CURRENT_DEEP_PROMPT_VERSION,
    DEEP_RESEARCH_PROMPT_V1,
    DEEP_RESEARCH_PROMPT_V2,
    NO_EVIDENCE_TEXT,
    NO_VALID_CLAIMS_TEXT,
    DeepBasis,
    DeepDraftClaim,
    DeepIssueCode,
    DeepResearchDraft,
    DeepSection,
    UnknownReason,
    allowed_bases,
    build_deep_system_prompt,
    find_forecast,
    unsupported_figures,
    validate_deep_report,
)
from research import ConfidenceLevel, ResearchStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from deep_research import DeepResearchBrief

GENERATED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _validate(draft: DeepResearchDraft, brief: DeepResearchBrief) -> object:
    return validate_deep_report(
        draft,
        brief,
        prompt_version="DEEP_RESEARCH_PROMPT_V1",
        model_id="test-model",
        generated_at=GENERATED_AT,
    )


@pytest.fixture
def draft_of() -> Callable[..., DeepResearchDraft]:
    """Build a one-claim draft in a chosen section."""

    def build(section: DeepSection, text: str, basis: DeepBasis, *evidence: str):
        return DeepResearchDraft(
            ticker="ACME",
            claims=(DeepDraftClaim(section=section, text=text, basis=basis, evidence=evidence),),
        )

    return build


@pytest.mark.unit
def test_a_well_grounded_deterministic_claim_survives(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        "Growth contributed 30.4 of a possible 35.0 points.",
        DeepBasis.DETERMINISTIC,
        "S.growth",
    )

    report = _validate(draft, researched)

    kept = report.sections.why_the_algorithm_likes_it
    assert [claim.basis for claim in kept] == [DeepBasis.DETERMINISTIC]
    assert not [
        issue for issue in report.issues if issue.section is DeepSection.WHY_THE_ALGORITHM_LIKES_IT
    ]


@pytest.mark.unit
def test_a_deterministic_claim_may_not_rest_on_a_web_source(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """The structural half of 'web never changes the score'."""
    article = researched.external[0]
    draft = draft_of(
        DeepSection.CURRENT_SNAPSHOT,
        "The company scored well.",
        DeepBasis.DETERMINISTIC,
        article.evidence_id,
    )

    report = _validate(draft, researched)

    assert report.status is ResearchStatus.PARTIAL
    assert "current_snapshot" in report.unknowns


@pytest.mark.unit
def test_an_external_claim_requires_a_w_citation(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.RECENT_DEVELOPMENTS,
        "The company announced something recently.",
        DeepBasis.EXTERNAL,
        "M.fcf_margin",
    )

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.MISSING_EXTERNAL_EVIDENCE in codes


@pytest.mark.unit
def test_an_extracted_claim_citing_only_an_accession_is_dropped(
    researched: DeepResearchBrief, accession: str, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """`D.` proves a filing exists; it proves nothing about what it says."""
    draft = draft_of(
        DeepSection.COMPANY_OVERVIEW,
        "The company described a new supply agreement.",
        DeepBasis.EXTRACTED,
        f"D.{accession}",
    )

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.MISSING_EXTRACTED_EVIDENCE in codes


@pytest.mark.unit
def test_an_invented_citation_is_dropped(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.BULL_CASE,
        "Growth may persist.",
        DeepBasis.INTERPRETATION,
        "M.invented",
    )

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.UNRESOLVED_EVIDENCE in codes


@pytest.mark.unit
def test_an_unknown_claim_survives_and_marks_the_section(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(DeepSection.VALUATION, "The brief does not answer this.", DeepBasis.UNKNOWN)

    report = _validate(draft, researched)

    assert "valuation" in report.unknowns


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Investors should buy the shares at this level.",
        "We recommend selling into the rally.",
        "Our price target is $120 per share.",
        "A position size of 3% is appropriate here.",
        "The stock implies 30% upside from here.",
        "We expect a total return of 25% over twelve months.",
        "Fair value of $95 suggests the shares are cheap.",
    ],
)
def test_instructions_and_forecasts_are_rejected(
    researched: DeepResearchBrief, text: str, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """A deep report describes evidence. It never tells anyone what to do."""
    draft = draft_of(DeepSection.RESEARCH_CONCLUSION, text, DeepBasis.INTERPRETATION, "S.growth")

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.INVESTMENT_ADVICE in codes


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "The shares look expensive against the peer group.",
        "Growth may prove less durable than the headline suggests.",
        "The evidence supports further research rather than conviction.",
    ],
)
def test_valuation_and_evidence_language_stays_legal(
    researched: DeepResearchBrief, text: str, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """The advice check must not swallow the interpretation the report exists for."""
    assert find_forecast(text) is None

    draft = draft_of(DeepSection.BEAR_CASE, text, DeepBasis.INTERPRETATION, "S.growth")
    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.INVESTMENT_ADVICE not in codes


@pytest.mark.unit
def test_a_figure_from_one_article_does_not_ground_a_claim_citing_another(
    brief: DeepResearchBrief, make_external
) -> None:
    """The laundering the W. namespace makes cheapest, and the rule that stops it."""
    reported = make_external(
        "https://example.test/one",
        excerpt="Acme said the new facility will employ 1,470 people once it is complete.",
    )
    unrelated = make_external(
        "https://example.test/two", excerpt="Acme opened a distribution centre in Ohio."
    )
    supplied = brief.model_copy(update={"external": (reported, unrelated)})

    cites_the_wrong_one = unsupported_figures(
        "The site will employ 1,470 people.", supplied, cited=(unrelated.evidence_id,)
    )
    cites_the_right_one = unsupported_figures(
        "The site will employ 1,470 people.", supplied, cited=(reported.evidence_id,)
    )

    assert cites_the_wrong_one
    assert cites_the_right_one == ()


@pytest.mark.unit
def test_a_figure_from_one_excerpt_does_not_ground_a_claim_citing_another(
    researched: DeepResearchBrief, accession: str
) -> None:
    """The same scoping, applied to filing text."""
    other = researched.excerpts[0].model_copy(
        update={
            "id": f"X.{accession}.risk_factors",
            "section": "risk_factors",
            "text": "The Company operates 37 distribution centres across the region.",
        }
    )
    supplied = researched.model_copy(update={"excerpts": (*researched.excerpts, other)})

    quoting_the_right_one = unsupported_figures(
        "The Company operates 37 distribution centres.", supplied, cited=(other.id,)
    )
    quoting_the_wrong_one = unsupported_figures(
        "The Company operates 37 distribution centres.",
        supplied,
        cited=(researched.excerpts[0].id,),
    )

    assert quoting_the_right_one == ()
    assert quoting_the_wrong_one


@pytest.mark.unit
def test_a_structured_metric_grounds_any_claim_that_cites_it(researched: DeepResearchBrief) -> None:
    """Structured figures are facts about the company, not quotations."""
    assert unsupported_figures("FCF margin was 12.1%.", researched, cited=("M.fcf_margin",)) == ()


@pytest.mark.unit
def test_new_arithmetic_is_rejected(researched: DeepResearchBrief) -> None:
    """A ratio the pipeline never calculated is fabricated, however correct."""
    assert unsupported_figures(
        "Revenue per share works out at 0.294.", researched, cited=("M.fcf_margin",)
    )


@pytest.mark.unit
def test_confidence_is_computed_not_taken_from_the_model(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        "Growth contributed 30.4 of a possible 35.0 points.",
        DeepBasis.DETERMINISTIC,
        "S.growth",
    ).model_copy(update={"confidence": ConfidenceLevel.HIGH})

    report = _validate(draft, researched)

    assert report.confidence.level is report.confidence.ceiling
    assert report.confidence.claimed is report.confidence.ceiling


@pytest.mark.unit
def test_a_company_with_no_external_evidence_can_still_reach_medium(
    brief: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """The ACTG case: good metrics and real filing text, no news at all."""
    assert brief.external == ()

    draft = draft_of(
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        "Growth contributed 30.4 of a possible 35.0 points.",
        DeepBasis.DETERMINISTIC,
        "S.growth",
    )
    report = _validate(draft, brief)

    assert report.confidence.level is ConfidenceLevel.MEDIUM


@pytest.mark.unit
def test_thin_metric_coverage_caps_confidence_at_low(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    thin = researched.model_copy(
        update={"score": researched.score.model_copy(update={"data_coverage": 0.3})}
    )
    draft = draft_of(
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        "Growth contributed 30.4 of a possible 35.0 points.",
        DeepBasis.DETERMINISTIC,
        "S.growth",
    )

    report = _validate(draft, thin)

    assert report.confidence.level is ConfidenceLevel.LOW


@pytest.mark.unit
def test_a_report_carries_only_the_external_sources_its_claims_cite(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        "Growth contributed 30.4 of a possible 35.0 points.",
        DeepBasis.DETERMINISTIC,
        "S.growth",
    )

    report = _validate(draft, researched)

    assert report.external_evidence == ()


@pytest.mark.unit
def test_an_overlong_claim_is_dropped_whole(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(DeepSection.BULL_CASE, "x" * 400, DeepBasis.INTERPRETATION, "S.growth")

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.CLAIM_TOO_LONG in codes


@pytest.mark.unit
def test_every_section_is_answered_even_when_the_model_said_nothing(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.BULL_CASE, "Growth may persist.", DeepBasis.INTERPRETATION, "S.growth"
    )

    report = _validate(draft, researched)

    for _, claims in report.sections.iter_sections():
        assert claims


@pytest.mark.unit
def test_the_prompt_states_the_bases_each_section_accepts() -> None:
    """Generated from `allowed_bases`, so the instruction cannot drift from the rule."""
    prompt = build_deep_system_prompt()

    for section in DeepSection:
        assert section.value in prompt
    assert "allowed: DETERMINISTIC/UNKNOWN" in prompt
    assert "allowed: EXTRACTED/EXTERNAL/UNKNOWN" in prompt


@pytest.mark.unit
def test_the_prompt_basis_lines_match_the_validator_policy() -> None:
    """The pairing that matters: what the model is told is what is enforced."""
    prompt = build_deep_system_prompt()

    for section in DeepSection:
        expected = "/".join(
            basis.value
            for basis in (
                DeepBasis.DETERMINISTIC,
                DeepBasis.EXTRACTED,
                DeepBasis.EXTERNAL,
                DeepBasis.INTERPRETATION,
                DeepBasis.UNKNOWN,
            )
            if basis in allowed_bases(section)
        )
        assert f"{section.value}\n      allowed: {expected}" in prompt


@pytest.mark.unit
def test_the_prompt_asks_for_an_evidence_assessment_not_an_action() -> None:
    prompt = build_deep_system_prompt()

    assert "research_conclusion must contain at least one INTERPRETATION claim" in prompt
    assert "as something to research" in prompt
    assert "Never an action, a target, a return or an imperative." in prompt


@pytest.mark.unit
def test_the_current_prompt_version_is_v2() -> None:
    assert CURRENT_DEEP_PROMPT_VERSION == DEEP_RESEARCH_PROMPT_V2
    assert DEEP_RESEARCH_PROMPT_V1 != DEEP_RESEARCH_PROMPT_V2


@pytest.mark.unit
def test_a_conclusion_assessing_the_evidence_survives(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """The shape V2 asks for must actually pass the validator it is paired with."""
    draft = draft_of(
        DeepSection.RESEARCH_CONCLUSION,
        "The deterministic evidence is strong but filing coverage is thin, so this "
        "warrants continued research rather than conviction.",
        DeepBasis.INTERPRETATION,
        "S.growth",
    )

    report = _validate(draft, researched)

    kept = report.sections.research_conclusion
    assert [claim.basis for claim in kept] == [DeepBasis.INTERPRETATION]


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "This is a strong buy at current levels.",
        "We rate the shares a hold pending further evidence.",
        "The evidence supports a price target of $140.",
        "Continued research is warranted, with expected return of 18% over a year.",
    ],
)
def test_an_advisory_conclusion_is_still_rejected(
    researched: DeepResearchBrief, text: str, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """V2 changed the prompt, not the check."""
    draft = draft_of(DeepSection.RESEARCH_CONCLUSION, text, DeepBasis.INTERPRETATION, "S.growth")

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.INVESTMENT_ADVICE in codes


@pytest.mark.unit
def test_a_section_the_model_never_addressed_reports_no_evidence(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """ACTG's catalysts case: nothing offered, nothing written, nothing rejected."""
    draft = draft_of(
        DeepSection.BULL_CASE, "Growth may persist.", DeepBasis.INTERPRETATION, "S.growth"
    )

    report = _validate(draft, researched)

    assert report.unknown_reasons["catalysts"] is UnknownReason.NO_EVIDENCE
    assert report.sections.catalysts[0].text == NO_EVIDENCE_TEXT


@pytest.mark.unit
def test_a_section_whose_claims_were_all_rejected_reports_no_valid_claims(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """MU's research_conclusion case: evidence was there, the output was not usable."""
    draft = draft_of(
        DeepSection.RESEARCH_CONCLUSION,
        "The evidence supports an expected return of 20% over the next year.",
        DeepBasis.INTERPRETATION,
        "S.growth",
    )

    report = _validate(draft, researched)

    assert report.unknown_reasons["research_conclusion"] is UnknownReason.NO_VALID_CLAIMS
    assert report.sections.research_conclusion[0].text == NO_VALID_CLAIMS_TEXT


@pytest.mark.unit
def test_the_rejected_text_is_never_exposed(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """Publishing what validation refused to publish would defeat the refusal."""
    rejected = "Investors should buy the shares, price target $140."
    draft = draft_of(
        DeepSection.RESEARCH_CONCLUSION, rejected, DeepBasis.INTERPRETATION, "S.growth"
    )

    report = _validate(draft, researched)

    document = report.model_dump_json()
    assert rejected not in document
    assert "$140" not in document
    assert "buy the shares" not in document


@pytest.mark.unit
def test_an_unsafe_conclusion_is_still_rejected_after_the_change(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """The fix is the fallback explanation, not the validator."""
    draft = draft_of(
        DeepSection.RESEARCH_CONCLUSION,
        "We expect a total return of 25% and rate the shares a buy.",
        DeepBasis.INTERPRETATION,
        "S.growth",
    )

    report = _validate(draft, researched)

    codes = {issue.code for issue in report.issues}
    assert DeepIssueCode.INVESTMENT_ADVICE in codes
    assert "research_conclusion" in report.unknowns


@pytest.mark.unit
def test_the_models_own_unknown_answer_is_preserved_verbatim(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """An honest UNKNOWN the model wrote is its words, and stays its words."""
    written = "No named competitors appear anywhere in the supplied evidence."
    draft = draft_of(DeepSection.COMPETITIVE_POSITION, written, DeepBasis.UNKNOWN)

    report = _validate(draft, researched)

    kept = report.sections.competitive_position[0]
    assert kept.text == written
    assert kept.unknown_reason is None
    assert report.unknown_reasons["competitive_position"] is UnknownReason.NO_EVIDENCE


@pytest.mark.unit
def test_a_section_with_surviving_claims_has_no_unknown_reason(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        "Growth contributed 30.4 of a possible 35.0 points.",
        DeepBasis.DETERMINISTIC,
        "S.growth",
    )

    report = _validate(draft, researched)

    assert "why_the_algorithm_likes_it" not in report.unknown_reasons


@pytest.mark.unit
def test_every_unknown_section_carries_a_reason(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    """A UI must never have to infer this from an issue string."""
    draft = draft_of(
        DeepSection.BULL_CASE, "Growth may persist.", DeepBasis.INTERPRETATION, "S.growth"
    )

    report = _validate(draft, researched)

    assert set(report.unknowns) == set(report.unknown_reasons)
    assert all(isinstance(reason, UnknownReason) for reason in report.unknown_reasons.values())


@pytest.mark.unit
def test_dropping_every_claim_still_yields_a_partial_report_not_a_failure(
    researched: DeepResearchBrief, draft_of: Callable[..., DeepResearchDraft]
) -> None:
    draft = draft_of(
        DeepSection.RESEARCH_CONCLUSION,
        "Investors should buy.",
        DeepBasis.INTERPRETATION,
        "S.growth",
    )

    report = _validate(draft, researched)

    assert report.status is ResearchStatus.PARTIAL
    assert len(report.unknowns) == len(DeepSection)


@pytest.mark.unit
def test_a_fully_answered_report_is_complete_and_has_no_unknown_reasons(
    researched: DeepResearchBrief, accession: str
) -> None:
    """COMPLETE semantics are unchanged: every section answered, nothing corrected."""
    article = researched.external[0]
    supported: dict[DeepSection, tuple[str, DeepBasis, tuple[str, ...]]] = {
        DeepSection.COMPANY_OVERVIEW: (
            "The company describes continued platform demand.",
            DeepBasis.EXTRACTED,
            (f"X.{accession}.mda",),
        ),
        DeepSection.CURRENT_SNAPSHOT: (
            "Growth contributed 30.4 points.",
            DeepBasis.DETERMINISTIC,
            ("S.growth",),
        ),
        DeepSection.WHY_THE_ALGORITHM_LIKES_IT: (
            "Growth contributed 30.4 of a possible 35.0 points.",
            DeepBasis.DETERMINISTIC,
            ("S.growth",),
        ),
        DeepSection.RECENT_DEVELOPMENTS: (
            "A supply agreement was reported.",
            DeepBasis.EXTERNAL,
            (article.evidence_id,),
        ),
        DeepSection.LATEST_EARNINGS: (
            "Growth contributed 30.4 points.",
            DeepBasis.DETERMINISTIC,
            ("S.growth",),
        ),
    }
    claims = [
        DeepDraftClaim(section=section, text=text, basis=basis, evidence=evidence)
        for section, (text, basis, evidence) in supported.items()
    ]
    claims += [
        DeepDraftClaim(
            section=section,
            text="The supplied evidence does not answer this.",
            basis=DeepBasis.UNKNOWN,
        )
        for section in DeepSection
        if section not in supported
    ]

    report = _validate(DeepResearchDraft(ticker="ACME", claims=tuple(claims)), researched)

    assert report.status is ResearchStatus.COMPLETE
    assert report.issues == ()
    assert all(reason is UnknownReason.NO_EVIDENCE for reason in report.unknown_reasons.values())
