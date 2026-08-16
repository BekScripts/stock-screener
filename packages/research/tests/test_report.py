from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from research import (
    MAX_CLAIM_CHARS,
    Basis,
    Claim,
    ConfidenceLevel,
    ReportSections,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    Section,
    confidence_rank,
)

# Names that would let a report restate the CompounderScore as its own number.
# The contract's central guarantee is that none of them exist.
FORBIDDEN_FIELDS = frozenset(
    {
        "score",
        "final_score",
        "raw_score",
        "adjusted_score",
        "growth_score",
        "quality_score",
        "valuation_score",
        "momentum_score",
        "risk_penalty",
        "rating",
        "category",
        "score_category",
        "price_target",
        "target_price",
        "recommendation",
    }
)


def _confidence() -> ResearchConfidence:
    return ResearchConfidence(
        level=ConfidenceLevel.LOW,
        ceiling=ConfidenceLevel.LOW,
        claimed=ConfidenceLevel.LOW,
    )


def _report(generated_at: datetime = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)) -> ResearchReport:
    return ResearchReport(
        contract_version="RESEARCH_V1",
        prompt_version="P1",
        model_id="test-model",
        generated_at=generated_at,
        ticker="ACME",
        score_version="COMPOUNDER_V1_1",
        score_date=date(2026, 8, 14),
        brief_fingerprint="abc",
        status=ResearchStatus.COMPLETE,
        confidence=_confidence(),
    )


@pytest.mark.unit
def test_a_report_has_nowhere_to_put_a_score() -> None:
    assert not FORBIDDEN_FIELDS & set(ResearchReport.model_fields)


@pytest.mark.unit
def test_a_claim_has_nowhere_to_put_a_score() -> None:
    assert not FORBIDDEN_FIELDS & set(Claim.model_fields)


@pytest.mark.unit
def test_every_section_names_a_field_on_report_sections() -> None:
    assert {section.value for section in Section} == set(ReportSections.model_fields)


@pytest.mark.unit
def test_iter_sections_yields_all_thirteen_in_order() -> None:
    yielded = [section for section, _ in ReportSections().iter_sections()]

    assert yielded == list(Section)


@pytest.mark.unit
def test_from_mapping_leaves_unmentioned_sections_empty() -> None:
    claim = Claim(text="Something.", basis=Basis.UNKNOWN)

    sections = ReportSections.from_mapping({Section.BULL_CASE: (claim,)})

    assert sections.bull_case == (claim,)
    assert sections.bear_case == ()


@pytest.mark.unit
def test_rejects_a_naive_generated_at() -> None:
    with pytest.raises(ValidationError, match="generated_at must be timezone-aware"):
        _report(generated_at=datetime(2026, 8, 15, 9, 0))  # noqa: DTZ001 — the point of the test


@pytest.mark.unit
def test_rejects_confidence_above_its_own_ceiling() -> None:
    with pytest.raises(ValidationError, match="confidence HIGH exceeds ceiling MEDIUM"):
        ResearchConfidence(
            level=ConfidenceLevel.HIGH,
            ceiling=ConfidenceLevel.MEDIUM,
            claimed=ConfidenceLevel.HIGH,
        )


@pytest.mark.unit
def test_accepts_confidence_below_its_ceiling() -> None:
    confidence = ResearchConfidence(
        level=ConfidenceLevel.LOW,
        ceiling=ConfidenceLevel.HIGH,
        claimed=ConfidenceLevel.LOW,
    )

    assert confidence.level is ConfidenceLevel.LOW


@pytest.mark.unit
def test_confidence_levels_are_ordered_lowest_first() -> None:
    ranks = [confidence_rank(level) for level in ConfidenceLevel]

    assert ranks == sorted(ranks)


@pytest.mark.unit
def test_rejects_a_claim_longer_than_the_limit() -> None:
    with pytest.raises(ValidationError, match="at most 240 characters"):
        Claim(text="x" * (MAX_CLAIM_CHARS + 1), basis=Basis.INTERPRETATION)


@pytest.mark.unit
def test_rejects_an_empty_claim() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        Claim(text="", basis=Basis.UNKNOWN)


@pytest.mark.unit
def test_a_report_is_immutable() -> None:
    report = _report()

    with pytest.raises(ValidationError, match="frozen"):
        report.status = ResearchStatus.FAILED  # type: ignore[misc]
