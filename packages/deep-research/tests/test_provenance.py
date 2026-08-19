import pytest

from deep_research import (
    DeepBasis,
    DeepClaim,
    DeepResearchBrief,
    DeepSection,
    allowed_bases,
    allowed_kinds,
    allows_external,
    cites_filing_text,
    claim_problems,
    requires_evidence,
    supports,
    unsupported_citations,
)
from research import EvidenceKind


@pytest.mark.unit
@pytest.mark.parametrize(
    ("basis", "evidence_id", "expected"),
    [
        (DeepBasis.DETERMINISTIC, "S.growth", True),
        (DeepBasis.DETERMINISTIC, "M.fcf_margin", True),
        (DeepBasis.DETERMINISTIC, "F.2026-03-31", True),
        (DeepBasis.DETERMINISTIC, "E.market_cap", True),
        (DeepBasis.DETERMINISTIC, "X.0000320193-26-000073.mda", False),
        (DeepBasis.DETERMINISTIC, "W.news.abc123", False),
        (DeepBasis.EXTRACTED, "X.0000320193-26-000073.mda", True),
        (DeepBasis.EXTRACTED, "D.0000320193-26-000073", True),
        (DeepBasis.EXTRACTED, "M.fcf_margin", False),
        (DeepBasis.EXTRACTED, "W.news.abc123", False),
        (DeepBasis.EXTERNAL, "W.news.abc123", True),
        (DeepBasis.EXTERNAL, "S.growth", False),
        (DeepBasis.EXTERNAL, "X.0000320193-26-000073.mda", False),
        (DeepBasis.INTERPRETATION, "S.growth", True),
        (DeepBasis.INTERPRETATION, "W.news.abc123", True),
        (DeepBasis.INTERPRETATION, "X.0000320193-26-000073.mda", True),
        (DeepBasis.UNKNOWN, "S.growth", False),
        (DeepBasis.UNKNOWN, "W.news.abc123", False),
    ],
)
def test_basis_and_evidence_compatibility(
    basis: DeepBasis, evidence_id: str, *, expected: bool
) -> None:
    assert supports(basis, evidence_id) is expected


@pytest.mark.unit
def test_an_unrecognised_namespace_supports_nothing() -> None:
    assert supports(DeepBasis.INTERPRETATION, "Z.mystery") is False


@pytest.mark.unit
def test_deterministic_evidence_never_includes_the_external_namespace() -> None:
    """`W.` is absent from `research.EvidenceKind`, which is what makes this hold."""
    for basis in DeepBasis:
        assert EvidenceKind.EXCERPT not in allowed_kinds(basis) or basis in {
            DeepBasis.EXTRACTED,
            DeepBasis.INTERPRETATION,
        }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        (DeepBasis.EXTERNAL, True),
        (DeepBasis.INTERPRETATION, True),
        (DeepBasis.DETERMINISTIC, False),
        (DeepBasis.EXTRACTED, False),
        (DeepBasis.UNKNOWN, False),
    ],
)
def test_which_bases_may_rest_on_a_web_source(basis: DeepBasis, *, expected: bool) -> None:
    assert allows_external(basis) is expected


@pytest.mark.unit
def test_unknown_is_the_only_basis_needing_no_evidence() -> None:
    needing = {basis for basis in DeepBasis if requires_evidence(basis)}

    assert needing == set(DeepBasis) - {DeepBasis.UNKNOWN}


@pytest.mark.unit
def test_unknown_is_allowed_in_every_section() -> None:
    """A section that could not answer must always be able to say so."""
    assert all(DeepBasis.UNKNOWN in allowed_bases(section) for section in DeepSection)


@pytest.mark.unit
@pytest.mark.parametrize(
    "section", [DeepSection.CURRENT_SNAPSHOT, DeepSection.WHY_THE_ALGORITHM_LIKES_IT]
)
def test_the_score_explaining_sections_admit_no_web_evidence(section: DeepSection) -> None:
    """Web never changes the score, and never explains it either."""
    assert not any(allows_external(basis) for basis in allowed_bases(section))


@pytest.mark.unit
def test_recent_developments_admits_external_evidence() -> None:
    assert DeepBasis.EXTERNAL in allowed_bases(DeepSection.RECENT_DEVELOPMENTS)


@pytest.mark.unit
def test_the_case_sections_are_interpretation_only() -> None:
    for section in (
        DeepSection.BULL_CASE,
        DeepSection.BEAR_CASE,
        DeepSection.THESIS_BREAKERS,
        DeepSection.RESEARCH_CONCLUSION,
    ):
        assert allowed_bases(section) == frozenset({DeepBasis.INTERPRETATION, DeepBasis.UNKNOWN})


@pytest.mark.unit
def test_every_section_has_a_basis_table_entry() -> None:
    """A section added to the enum but not to the table would raise at lookup."""
    for section in DeepSection:
        assert allowed_bases(section)


@pytest.mark.unit
def test_a_citation_absent_from_the_brief_is_unsupported(
    researched: DeepResearchBrief,
) -> None:
    claim = DeepClaim(
        text="Revenue grew.", basis=DeepBasis.DETERMINISTIC, evidence=("M.invented_metric",)
    )

    assert unsupported_citations(claim, researched) == ("M.invented_metric",)


@pytest.mark.unit
def test_a_resolvable_citation_of_the_wrong_kind_is_unsupported(
    researched: DeepResearchBrief,
) -> None:
    """An id can be real and still be the wrong sort of thing to rest on."""
    claim = DeepClaim(
        text="A news story says revenue grew.",
        basis=DeepBasis.DETERMINISTIC,
        evidence=(researched.external[0].evidence_id,),
    )

    assert unsupported_citations(claim, researched) == (researched.external[0].evidence_id,)


@pytest.mark.unit
def test_a_well_grounded_claim_has_no_unsupported_citations(
    researched: DeepResearchBrief,
) -> None:
    claim = DeepClaim(
        text="Growth contributed 30.4 points.",
        basis=DeepBasis.DETERMINISTIC,
        evidence=("S.growth",),
    )

    assert unsupported_citations(claim, researched) == ()


@pytest.mark.unit
def test_filing_metadata_alone_does_not_quote_a_filing(
    researched: DeepResearchBrief, accession: str
) -> None:
    assert cites_filing_text((f"D.{accession}",), researched) is False


@pytest.mark.unit
def test_an_excerpt_does_quote_a_filing(researched: DeepResearchBrief, accession: str) -> None:
    assert cites_filing_text((f"X.{accession}.mda",), researched) is True


@pytest.mark.unit
def test_an_extracted_claim_citing_only_an_accession_is_rejected(
    researched: DeepResearchBrief, accession: str
) -> None:
    """Phase 3's rule, carried forward: an accession proves existence, not content."""
    claim = DeepClaim(
        text="The company described a new supply agreement.",
        basis=DeepBasis.EXTRACTED,
        evidence=(f"D.{accession}",),
    )

    problems = claim_problems(claim, DeepSection.COMPANY_OVERVIEW, researched)

    assert any("cite an X. excerpt" in problem for problem in problems)


@pytest.mark.unit
def test_an_extracted_claim_citing_an_excerpt_is_accepted(
    researched: DeepResearchBrief, accession: str
) -> None:
    claim = DeepClaim(
        text="The company attributed growth to platform demand.",
        basis=DeepBasis.EXTRACTED,
        evidence=(f"X.{accession}.mda",),
    )

    assert claim_problems(claim, DeepSection.COMPANY_OVERVIEW, researched) == ()


@pytest.mark.unit
def test_an_external_claim_grounded_in_a_collected_source_is_accepted(
    researched: DeepResearchBrief,
) -> None:
    claim = DeepClaim(
        text="Acme said it signed a multi-year supply agreement.",
        basis=DeepBasis.EXTERNAL,
        evidence=(researched.external[0].evidence_id,),
    )

    assert claim_problems(claim, DeepSection.RECENT_DEVELOPMENTS, researched) == ()


@pytest.mark.unit
def test_an_external_claim_in_a_deterministic_section_is_rejected(
    researched: DeepResearchBrief,
) -> None:
    """The structural half of 'web never changes the score'."""
    claim = DeepClaim(
        text="A news story explains the ranking.",
        basis=DeepBasis.EXTERNAL,
        evidence=(researched.external[0].evidence_id,),
    )

    problems = claim_problems(claim, DeepSection.WHY_THE_ALGORITHM_LIKES_IT, researched)

    assert any("not permitted in why_the_algorithm_likes_it" in problem for problem in problems)


@pytest.mark.unit
def test_an_unknown_claim_citing_nothing_is_accepted(researched: DeepResearchBrief) -> None:
    claim = DeepClaim(
        text="The brief does not contain evidence for this section.", basis=DeepBasis.UNKNOWN
    )

    assert claim_problems(claim, DeepSection.VALUATION, researched) == ()


@pytest.mark.unit
def test_an_unknown_claim_that_cites_evidence_is_rejected(
    researched: DeepResearchBrief,
) -> None:
    """Either it is not unknown, or the citation is decorative."""
    claim = DeepClaim(
        text="The brief does not answer this.",
        basis=DeepBasis.UNKNOWN,
        evidence=("M.fcf_margin",),
    )

    problems = claim_problems(claim, DeepSection.VALUATION, researched)

    assert any("must cite nothing" in problem for problem in problems)


@pytest.mark.unit
def test_an_unknown_claim_is_accepted_in_every_section(researched: DeepResearchBrief) -> None:
    claim = DeepClaim(text="Nothing here answers this.", basis=DeepBasis.UNKNOWN)

    for section in DeepSection:
        assert claim_problems(claim, section, researched) == ()


@pytest.mark.unit
def test_an_interpretation_citing_nothing_is_rejected(researched: DeepResearchBrief) -> None:
    claim = DeepClaim(text="The company looks well positioned.", basis=DeepBasis.INTERPRETATION)

    problems = claim_problems(claim, DeepSection.BULL_CASE, researched)

    assert any("must cite what it rests on" in problem for problem in problems)


@pytest.mark.unit
def test_the_score_section_must_cite_the_score_breakdown(
    researched: DeepResearchBrief,
) -> None:
    claim = DeepClaim(
        text="Free cash flow margin was 12.1%.",
        basis=DeepBasis.DETERMINISTIC,
        evidence=("M.fcf_margin",),
    )

    problems = claim_problems(claim, DeepSection.WHY_THE_ALGORITHM_LIKES_IT, researched)

    assert any("must cite the score breakdown" in problem for problem in problems)


@pytest.mark.unit
def test_the_score_section_accepts_a_claim_citing_a_score_line(
    researched: DeepResearchBrief,
) -> None:
    claim = DeepClaim(
        text="Growth contributed 30.4 of a possible 35.0 points.",
        basis=DeepBasis.DETERMINISTIC,
        evidence=("S.growth",),
    )

    assert claim_problems(claim, DeepSection.WHY_THE_ALGORITHM_LIKES_IT, researched) == ()


@pytest.mark.unit
def test_an_invented_citation_is_reported_as_unresolved(
    researched: DeepResearchBrief,
) -> None:
    claim = DeepClaim(
        text="Revenue tripled.", basis=DeepBasis.DETERMINISTIC, evidence=("S.made_up",)
    )

    problems = claim_problems(claim, DeepSection.CURRENT_SNAPSHOT, researched)

    assert any("not in the brief" in problem for problem in problems)


@pytest.mark.unit
def test_a_brief_with_no_external_evidence_cannot_ground_an_external_claim(
    brief: DeepResearchBrief,
) -> None:
    """Until collection runs, a section resting on the news answers UNKNOWN."""
    claim = DeepClaim(
        text="Reports say the company won a contract.",
        basis=DeepBasis.EXTERNAL,
        evidence=("W.news.abc123",),
    )

    problems = claim_problems(claim, DeepSection.RECENT_DEVELOPMENTS, brief)

    assert any("not in the brief" in problem for problem in problems)
