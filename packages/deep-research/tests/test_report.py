from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from deep_research import (
    MAX_CLAIM_CHARS,
    MAX_DRAFT_CLAIM_CHARS,
    DeepBasis,
    DeepClaim,
    DeepConfidence,
    DeepDraftClaim,
    DeepResearchDraft,
    DeepResearchReport,
    DeepSection,
    DeepSections,
)
from research import ConfidenceLevel, ResearchReport, Section

SPECIFIED_SECTIONS = (
    "company_overview",
    "current_snapshot",
    "why_the_algorithm_likes_it",
    "growth_quality",
    "financial_quality",
    "valuation",
    "latest_earnings",
    "recent_developments",
    "competitive_position",
    "catalysts",
    "major_risks",
    "bull_case",
    "bear_case",
    "thesis_breakers",
    "what_the_market_may_be_missing",
    "what_to_watch_next",
    "research_conclusion",
)

FORBIDDEN_TOKENS = frozenset(
    {
        "score",
        "rating",
        "recommendation",
        "target",
        "verdict",
        "signal",
        "allocation",
        "conviction",
        "sizing",
        "buy",
        "sell",
        "hold",
    }
)
"""Words that would turn a research note into an instruction.

Matched per underscore-separated word rather than as substrings, so
`competitive_position` — a section about a business, not about a trade — is not
mistaken for position sizing.
"""

FORBIDDEN_NAMES = frozenset({"price_target", "fair_value", "stop_loss", "position_size"})

ALLOWED_NAMES = frozenset({"score_version"})
"""Recording which formula produced a score is not carrying a score."""


def _is_forbidden(name: str) -> bool:
    """Whether a field name would let a report issue an instruction."""
    if name in ALLOWED_NAMES:
        return False
    return name in FORBIDDEN_NAMES or bool(set(name.lower().split("_")) & FORBIDDEN_TOKENS)


def _field_names(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set[str]:
    """Every field name on a model and on every model nested inside it."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)

    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        for nested in _nested_models(field.annotation):
            names |= _field_names(nested, seen)
    return names


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Pull every pydantic model out of a possibly-generic annotation."""
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for argument in getattr(annotation, "__args__", ()):
        found.extend(_nested_models(argument))
    return found


@pytest.mark.unit
def test_the_report_defines_the_seventeen_specified_sections() -> None:
    assert tuple(section.value for section in DeepSection) == SPECIFIED_SECTIONS


@pytest.mark.unit
def test_every_section_has_a_matching_field() -> None:
    """The enum and the model cannot drift apart silently."""
    assert tuple(DeepSections.model_fields) == SPECIFIED_SECTIONS


@pytest.mark.unit
def test_iter_sections_covers_every_section_once() -> None:
    covered = tuple(section for section, _ in DeepSections().iter_sections())

    assert covered == tuple(DeepSection)


@pytest.mark.unit
def test_the_report_carries_no_field_that_could_replace_the_compounder_score() -> None:
    """The contract's central guarantee, enforced structurally rather than by prompt.

    Walks nested models too, so a score field added to `DeepConfidence` or to a
    claim fails here as loudly as one added to the report itself.
    """
    offending = {name for name in _field_names(DeepResearchReport) if _is_forbidden(name)}

    assert offending == set()


@pytest.mark.unit
def test_the_draft_carries_no_score_or_advice_field_either() -> None:
    """A model cannot return a rating even before validation runs."""
    offending = {name for name in _field_names(DeepResearchDraft) if _is_forbidden(name)}

    assert offending == set()


@pytest.mark.unit
def test_the_report_records_the_score_version_it_explains() -> None:
    """Carrying the version is not the same as carrying a score."""
    assert "score_version" in DeepResearchReport.model_fields
    assert "final_score" not in DeepResearchReport.model_fields


@pytest.mark.unit
def test_a_deep_report_is_not_a_research_report() -> None:
    """Phase 3 and Phase 6 are unrelated types, not one wrapping the other."""
    assert not issubclass(DeepResearchReport, ResearchReport)
    assert not issubclass(ResearchReport, DeepResearchReport)


@pytest.mark.unit
def test_the_deep_sections_are_not_the_phase_three_sections() -> None:
    assert {section.value for section in DeepSection} != {section.value for section in Section}


@pytest.mark.unit
def test_the_basis_set_adds_external_to_the_phase_three_four() -> None:
    assert {basis.value for basis in DeepBasis} == {
        "DETERMINISTIC",
        "EXTRACTED",
        "EXTERNAL",
        "INTERPRETATION",
        "UNKNOWN",
    }


@pytest.mark.unit
def test_an_accepted_claim_is_bounded() -> None:
    with pytest.raises(ValidationError, match="text"):
        DeepClaim(
            text="x" * (MAX_CLAIM_CHARS + 1),
            basis=DeepBasis.INTERPRETATION,
            evidence=("M.fcf_margin",),
        )


@pytest.mark.unit
def test_a_draft_claim_far_longer_than_an_accepted_one_still_parses() -> None:
    """One overlong sentence must cost one claim, never the whole paid response."""
    claim = DeepDraftClaim(
        section=DeepSection.BULL_CASE,
        text="x" * (MAX_CLAIM_CHARS + 1),
        basis=DeepBasis.INTERPRETATION,
    )

    assert len(claim.text) > MAX_CLAIM_CHARS


@pytest.mark.unit
def test_a_draft_claim_beyond_the_wire_bound_is_rejected() -> None:
    with pytest.raises(ValidationError, match="text"):
        DeepDraftClaim(
            section=DeepSection.BULL_CASE,
            text="x" * (MAX_DRAFT_CLAIM_CHARS + 1),
            basis=DeepBasis.INTERPRETATION,
        )


@pytest.mark.unit
def test_a_draft_claim_with_impossible_provenance_still_parses() -> None:
    """The draft is permissive on purpose; the validator drops, the schema does not."""
    claim = DeepDraftClaim(
        section=DeepSection.WHY_THE_ALGORITHM_LIKES_IT,
        text="A news story explains the score.",
        basis=DeepBasis.EXTERNAL,
        evidence=("W.news.abc123",),
    )

    assert claim.basis is DeepBasis.EXTERNAL


@pytest.mark.unit
def test_grouping_a_draft_yields_every_section_even_when_unanswered() -> None:
    draft = DeepResearchDraft(
        ticker="ACME",
        claims=(
            DeepDraftClaim(
                section=DeepSection.BULL_CASE,
                text="Growth may persist.",
                basis=DeepBasis.INTERPRETATION,
                evidence=("M.revenue_growth_yoy",),
            ),
        ),
    )

    grouped = draft.by_section()

    assert set(grouped) == set(DeepSection)
    assert grouped[DeepSection.BEAR_CASE] == ()
    assert len(grouped[DeepSection.BULL_CASE]) == 1


@pytest.mark.unit
def test_an_unknown_claim_may_carry_no_evidence() -> None:
    claim = DeepClaim(text="The brief does not answer this.", basis=DeepBasis.UNKNOWN)

    assert claim.evidence == ()


@pytest.mark.unit
def test_confidence_may_not_exceed_its_own_ceiling() -> None:
    with pytest.raises(ValidationError, match="exceeds ceiling"):
        DeepConfidence(
            level=ConfidenceLevel.HIGH,
            ceiling=ConfidenceLevel.MEDIUM,
            claimed=ConfidenceLevel.HIGH,
        )


@pytest.mark.unit
def test_confidence_below_its_ceiling_is_allowed() -> None:
    confidence = DeepConfidence(
        level=ConfidenceLevel.LOW,
        ceiling=ConfidenceLevel.HIGH,
        claimed=ConfidenceLevel.LOW,
    )

    assert confidence.level is ConfidenceLevel.LOW


@pytest.mark.unit
def test_confidence_records_filing_and_external_coverage_separately() -> None:
    """A report grounded in news and one grounded in filings are differently trustworthy."""
    confidence = DeepConfidence(
        level=ConfidenceLevel.MEDIUM,
        ceiling=ConfidenceLevel.MEDIUM,
        claimed=ConfidenceLevel.MEDIUM,
        filing_coverage=0.2,
        external_coverage=0.7,
    )

    assert confidence.filing_coverage != confidence.external_coverage


@pytest.mark.unit
def test_rejects_a_naive_generation_timestamp(researched_report: DeepResearchReport) -> None:
    payload = researched_report.model_dump() | {
        "generated_at": datetime(2026, 8, 14, 19, 0)  # noqa: DTZ001 — the case under test
    }

    with pytest.raises(ValidationError, match="generated_at must be timezone-aware"):
        DeepResearchReport.model_validate(payload)


@pytest.mark.unit
def test_a_stored_report_resolves_its_own_external_citations(
    researched_report: DeepResearchReport,
) -> None:
    """A `W.` id in a persisted claim must not become a dangling handle."""
    carried = {item.evidence_id for item in researched_report.external_evidence}

    assert researched_report.cited_external_ids
    assert researched_report.cited_external_ids <= carried


@pytest.mark.unit
def test_a_report_round_trips_through_json(researched_report: DeepResearchReport) -> None:
    restored = DeepResearchReport.model_validate(researched_report.model_dump(mode="json"))

    assert restored == researched_report


@pytest.mark.unit
def test_the_report_rejects_an_unexpected_field(researched_report: DeepResearchReport) -> None:
    """`extra="forbid"` is what stops a rating being smuggled in as loose JSON."""
    payload = researched_report.model_dump(mode="json") | {"price_target": 120.0}

    with pytest.raises(ValidationError, match="price_target"):
        DeepResearchReport.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "forbidden"),
    [
        ("final_score", True),
        ("compounder_score", True),
        ("price_target", True),
        ("target_price", True),
        ("analyst_rating", True),
        ("position_size", True),
        ("recommendation", True),
        ("score_version", False),
        ("competitive_position", False),
        ("current_snapshot", False),
        ("research_conclusion", False),
    ],
)
def test_the_forbidden_field_guard_catches_what_it_claims_to(name: str, *, forbidden: bool) -> None:
    """A guard nobody tests is a guard that quietly stops guarding."""
    assert _is_forbidden(name) is forbidden


@pytest.mark.unit
def test_a_draft_claim_converts_to_an_accepted_claim() -> None:
    """What the validator calls once a claim has survived its checks."""
    drafted = DeepDraftClaim(
        section=DeepSection.BULL_CASE,
        text="Growth may persist.",
        basis=DeepBasis.INTERPRETATION,
        evidence=("M.revenue_growth_yoy",),
    )

    claim = drafted.to_claim()

    assert claim == DeepClaim(
        text="Growth may persist.",
        basis=DeepBasis.INTERPRETATION,
        evidence=("M.revenue_growth_yoy",),
    )


@pytest.mark.unit
def test_sections_can_be_built_from_a_section_keyed_mapping() -> None:
    claim = DeepClaim(
        text="Growth may persist.", basis=DeepBasis.INTERPRETATION, evidence=("S.growth",)
    )

    sections = DeepSections.from_mapping({DeepSection.BULL_CASE: [claim]})

    assert sections.bull_case == (claim,)
    assert sections.bear_case == ()
