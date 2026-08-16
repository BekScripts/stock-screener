import re
from datetime import date

import pytest
from pydantic import ValidationError

from domain import MetricUnit, ScoringStatus
from research import (
    CURRENT_CONTRACT_VERSION,
    EvidenceKind,
    FilingReference,
    MetricFact,
    Quantity,
    RankingState,
    ReportedPeriod,
    ResearchBrief,
    ScoreEvidence,
    ScoreItem,
    ScorePoint,
    SelectionReason,
    evidence_kind,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("evidence_id", "expected"),
    [
        ("S.growth", EvidenceKind.SCORE),
        ("M.fcf_margin", EvidenceKind.METRIC),
        ("F.2026-03-31", EvidenceKind.STATEMENT),
        ("D.0000320193-26-000073", EvidenceKind.FILING),
        ("E.market_cap", EvidenceKind.ENRICHMENT),
        ("X.0000320193-26-000073.mda", EvidenceKind.EXCERPT),
        ("Z.nonsense", None),
        ("", None),
    ],
)
def test_evidence_kind_reads_the_id_prefix(evidence_id: str, expected: EvidenceKind | None) -> None:
    assert evidence_kind(evidence_id) is expected


@pytest.mark.unit
def test_rejects_a_metric_fact_whose_prefix_disagrees_with_its_kind() -> None:
    with pytest.raises(ValidationError, match=re.escape("METRIC id must start with 'M.'")):
        MetricFact(id="S.growth", label="Growth", value=1.0)


@pytest.mark.unit
def test_rejects_an_id_that_is_only_a_prefix() -> None:
    with pytest.raises(ValidationError, match=re.escape("needs something after 'M.'")):
        MetricFact(id="M.", label="Nothing", value=1.0)


@pytest.mark.unit
def test_rejects_a_period_id_that_does_not_match_its_period_end() -> None:
    with pytest.raises(ValidationError, match=re.escape("period id must be 'F.2026-03-31'")):
        ReportedPeriod(id="F.2026-06-30", period_end=date(2026, 3, 31))


@pytest.mark.unit
def test_rejects_a_filing_id_that_does_not_match_its_accession() -> None:
    with pytest.raises(ValidationError, match=re.escape("filing id must be 'D.0001-24-000001'")):
        FilingReference(
            id="D.wrong", form="10-K", filed=date(2026, 2, 1), accession="0001-24-000001"
        )


@pytest.mark.unit
def test_rejects_a_score_point_id_that_does_not_match_its_date() -> None:
    expected = re.escape("score point id must be 'S.history.2026-07-15'")

    with pytest.raises(ValidationError, match=expected):
        ScorePoint(id="S.history.2026-07-16", score_date=date(2026, 7, 15))


@pytest.mark.unit
def test_normalises_the_ticker(brief: ResearchBrief) -> None:
    scruffy = brief.model_copy(update={"ticker": "  acme "})

    assert ResearchBrief.model_validate(scruffy.model_dump()).ticker == "ACME"


@pytest.mark.unit
def test_evidence_ids_cover_every_citable_thing(brief: ResearchBrief) -> None:
    ids = brief.evidence_ids

    assert "S.growth.revenue_growth" in ids
    assert "M.fcf_margin" in ids
    assert "F.2026-03-31" in ids
    assert "D.0000320193-26-000073" in ids
    assert "E.market_cap" in ids
    assert "S.history.2026-07-15" in ids


@pytest.mark.unit
def test_an_unavailable_subscore_is_still_citable(brief: ResearchBrief) -> None:
    assert "S.growth.cagr_3y" in brief.evidence_ids


@pytest.mark.unit
def test_unknowns_lists_unavailable_facts_and_subscores(brief: ResearchBrief) -> None:
    assert brief.unknowns == ("M.revenue_cagr_3y", "S.growth.cagr_3y")


@pytest.mark.unit
def test_a_zero_metric_is_not_an_unknown(brief: ResearchBrief) -> None:
    reported_zero = MetricFact(id="M.operating_margin", label="Operating margin", value=0.0)

    updated = brief.model_copy(update={"facts": (*brief.facts, reported_zero)})

    assert "M.operating_margin" not in updated.unknowns


@pytest.mark.unit
def test_quantities_carry_the_unit_each_number_should_be_read_in(brief: ResearchBrief) -> None:
    quantities = brief.quantities

    assert Quantity(value=0.382, unit=MetricUnit.PERCENT) in quantities
    assert Quantity(value=0.17, unit=MetricUnit.POINTS) in quantities
    assert Quantity(value=3.2, unit=MetricUnit.MULTIPLE) in quantities
    assert Quantity(value=412_000_000.0, unit=MetricUnit.MONEY) in quantities
    assert Quantity(value=77.4) in quantities


@pytest.mark.unit
def test_quantities_include_how_much_evidence_was_supplied(brief: ResearchBrief) -> None:
    assert Quantity(value=4.0, unit=MetricUnit.COUNT) in brief.quantities
    assert Quantity(value=1.0, unit=MetricUnit.COUNT) in brief.quantities


@pytest.mark.unit
def test_quantities_omit_a_metric_that_is_unknown(brief: ResearchBrief) -> None:
    values = {quantity.value for quantity in brief.quantities}

    assert not any(quantity.unit is None and quantity.value == 0.0 for quantity in brief.quantities)
    assert 0.382 in values


@pytest.mark.unit
def test_enrichment_facts_must_be_in_the_enrichment_namespace() -> None:
    from research import EnrichmentFacts

    with pytest.raises(ValidationError, match="must have kind ENRICHMENT"):
        EnrichmentFacts(
            provider="fmp",
            retrieved=date(2026, 8, 14),
            facts=(MetricFact(id="M.market_cap", label="Market cap", value=1.0),),
        )


@pytest.mark.unit
def test_fingerprint_is_stable_across_identical_briefs(brief: ResearchBrief) -> None:
    same = ResearchBrief.model_validate(brief.model_dump())

    assert same.fingerprint() == brief.fingerprint()


@pytest.mark.unit
def test_fingerprint_changes_when_any_evidence_changes(brief: ResearchBrief) -> None:
    moved = brief.model_copy(update={"score": brief.score.model_copy(update={"final_score": 77.5})})

    assert moved.fingerprint() != brief.fingerprint()


@pytest.mark.unit
def test_a_brief_defaults_to_the_current_contract_version() -> None:
    minimal = ResearchBrief(
        ticker="ACME",
        name="Acme",
        as_of=date(2026, 8, 14),
        selection=SelectionReason.HIDDEN_GEM,
        score=ScoreEvidence(
            score_version="COMPOUNDER_V1_1",
            score_date=date(2026, 8, 14),
            scoring_status=ScoringStatus.SCORED,
        ),
    )

    assert minimal.contract_version == CURRENT_CONTRACT_VERSION
    assert minimal.score.ranking_state is RankingState.PRELIMINARY


@pytest.mark.unit
def test_a_brief_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScoreItem(id="S.growth", label="Growth", points=1.0, verdict="strong")
