from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from research import (
    MAX_CLAIM_CHARS,
    NO_EVIDENCE_TEXT,
    Basis,
    Claim,
    ConfidenceLevel,
    DraftClaim,
    DraftReport,
    FilingText,
    IssueCode,
    RankingState,
    ReportSections,
    ResearchBrief,
    ResearchReport,
    ResearchStatus,
    Section,
    allowed_bases,
    confidence_ceiling,
    failed_report,
    find_advice,
    unsupported_figures,
    validate_report,
)

GENERATED_AT = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
ACCESSION = "0000320193-26-000073"
DraftFactory = Callable[..., DraftReport]


def _validate(draft: DraftReport, brief: ResearchBrief) -> ResearchReport:
    return validate_report(
        draft,
        brief,
        prompt_version="RESEARCH_PROMPT_V1",
        model_id="test-model",
        generated_at=GENERATED_AT,
    )


def _codes(report: ResearchReport) -> set[IssueCode]:
    return {issue.code for issue in report.issues}


def _codes_for(report: ResearchReport, section: Section) -> set[IssueCode]:
    return {issue.code for issue in report.issues if issue.section is section}


# --- section and basis -----------------------------------------------------


@pytest.mark.unit
def test_unknown_is_allowed_in_every_section() -> None:
    assert all(Basis.UNKNOWN in allowed_bases(section) for section in Section)


@pytest.mark.unit
def test_why_it_ranked_high_does_not_accept_interpretation() -> None:
    assert Basis.INTERPRETATION not in allowed_bases(Section.WHY_IT_RANKED_HIGH)


@pytest.mark.unit
def test_a_bull_case_does_not_accept_a_deterministic_claim() -> None:
    assert Basis.DETERMINISTIC not in allowed_bases(Section.BULL_CASE)


@pytest.mark.unit
def test_drops_a_claim_whose_basis_the_section_forbids(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="The market has not yet noticed this company.",
            basis=Basis.INTERPRETATION,
            evidence=("S.growth",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.BASIS_NOT_ALLOWED in _codes(report)
    assert report.sections.why_it_ranked_high[0].text == NO_EVIDENCE_TEXT


# --- evidence resolution ---------------------------------------------------


@pytest.mark.unit
def test_drops_a_claim_citing_an_id_that_is_not_in_the_brief(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="Growth carried the score.",
            basis=Basis.DETERMINISTIC,
            evidence=("S.growth", "M.invented_metric"),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.UNRESOLVED_EVIDENCE in _codes(report)


@pytest.mark.unit
def test_keeps_a_claim_whose_citations_all_resolve(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="Growth contributed 30.4 of 35 points.",
            basis=Basis.DETERMINISTIC,
            evidence=("S.growth",),
        ),
    )

    report = _validate(draft, brief)

    assert [c.text for c in report.sections.why_it_ranked_high] == [c.text for c in draft.claims]
    assert _codes_for(report, Section.WHY_IT_RANKED_HIGH) == set()


@pytest.mark.unit
def test_an_extracted_claim_must_cite_a_filing(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.RECENT_DEVELOPMENTS,
        Claim(
            text="The company opened a second facility.",
            basis=Basis.EXTRACTED,
            evidence=("M.fcf_margin",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.MISSING_EVIDENCE in _codes(report)


@pytest.mark.unit
def test_an_extracted_claim_citing_a_metadata_only_filing_is_dropped(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    # The accession resolves — the filing genuinely exists — but no text was
    # supplied, so the claim rests on the model's memory of the company rather
    # than on anything in the brief.
    draft = make_draft(
        Section.RECENT_DEVELOPMENTS,
        Claim(
            text="The company opened a second manufacturing facility.",
            basis=Basis.EXTRACTED,
            evidence=("D.0000320193-26-000073",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.MISSING_EXTRACTED_EVIDENCE in _codes(report)
    assert report.sections.recent_developments[0].text == NO_EVIDENCE_TEXT


@pytest.mark.unit
def test_an_extracted_claim_is_kept_when_the_filing_text_was_supplied(
    excerpted_brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.RECENT_DEVELOPMENTS,
        Claim(
            text="The company entered a new supply agreement.",
            basis=Basis.EXTRACTED,
            evidence=("X.0000320193-26-000073.item_1.01",),
        ),
    )

    report = _validate(draft, excerpted_brief)

    assert _codes_for(report, Section.RECENT_DEVELOPMENTS) == set()
    assert [c.text for c in report.sections.recent_developments] == [c.text for c in draft.claims]


@pytest.mark.unit
def test_a_remembered_business_fact_cannot_be_laundered_through_a_filing_id(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    # The failure this rule exists to stop: a plausible sentence the model knows
    # from training, given the appearance of evidence by attaching an accession
    # that happens to resolve.
    draft = make_draft(
        Section.COMPANY_SUMMARY,
        Claim(
            text="Acme designs and licenses semiconductor intellectual property.",
            basis=Basis.EXTRACTED,
            evidence=("D.0000320193-26-000073",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.MISSING_EXTRACTED_EVIDENCE in _codes(report)
    assert all("semiconductor" not in claim.text for claim in report.sections.company_summary)


@pytest.mark.unit
def test_an_explicit_unknown_stays_valid_where_extraction_is_impossible(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.COMPANY_SUMMARY,
        Claim(text="The brief supplies no filing text.", basis=Basis.UNKNOWN),
    )

    report = _validate(draft, brief)

    assert [c.text for c in report.sections.company_summary] == [c.text for c in draft.claims]
    assert _codes_for(report, Section.COMPANY_SUMMARY) == set()


@pytest.mark.unit
def test_a_metadata_only_brief_has_nothing_extractable(brief: ResearchBrief) -> None:
    assert brief.filings
    assert brief.extractable_ids == frozenset()


@pytest.mark.unit
def test_extracted_text_is_what_makes_a_filing_quotable(
    excerpted_brief: ResearchBrief,
) -> None:
    assert excerpted_brief.extractable_ids == {"X.0000320193-26-000073.item_1.01"}
    assert "D.0000320193-26-000073" in excerpted_brief.evidence_ids
    assert "D.0000320193-26-000073" not in excerpted_brief.extractable_ids


@pytest.mark.unit
def test_a_deterministic_claim_may_not_rest_on_a_filing_alone(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.FINANCIAL_QUALITY_INTERPRETATION,
        Claim(
            text="Free cash flow is positive.",
            basis=Basis.DETERMINISTIC,
            evidence=("D.0000320193-26-000073",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.MISSING_EVIDENCE in _codes(report)


@pytest.mark.unit
def test_an_interpretation_must_cite_something(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.BULL_CASE,
        Claim(text="The business could compound for a decade.", basis=Basis.INTERPRETATION),
    )

    report = _validate(draft, brief)

    assert IssueCode.MISSING_EVIDENCE in _codes(report)


@pytest.mark.unit
def test_why_it_ranked_high_must_cite_the_score_breakdown(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="Revenue growth was the reason.",
            basis=Basis.DETERMINISTIC,
            evidence=("M.revenue_growth_yoy",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.MISSING_EVIDENCE in _codes(report)


@pytest.mark.unit
def test_an_unknown_claim_needs_no_evidence(brief: ResearchBrief, make_draft: DraftFactory) -> None:
    draft = make_draft(
        Section.CATALYSTS,
        Claim(text="The brief contains no filing text.", basis=Basis.UNKNOWN),
    )

    report = _validate(draft, brief)

    assert [c.text for c in report.sections.catalysts] == [c.text for c in draft.claims]
    assert _codes_for(report, Section.CATALYSTS) == set()


# --- the numeric whitelist -------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Revenue grew 38.2% year over year.",
        "Revenue grew 38% year over year.",
        "Growth accelerated by 17pp.",
        "The shares trade at 3.2x revenue.",
        "Quarterly revenue reached $412 million.",
        "Free cash flow margin is 12.1%.",
        "Growth contributed 30.4 of 35 points.",
        "The score rose from 65.1 to 77.4.",
        "Scoring used 85% of the available metrics.",
        "Four quarters were supplied, and 4 were used.",
        "The latest 10-Q was filed on 2026-05-02.",
    ],
)
def test_accepts_a_figure_the_brief_supplies(text: str, brief: ResearchBrief) -> None:
    assert unsupported_figures(text, brief) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Revenue grew 41.0% year over year.",
        "The shares trade at 12.0x revenue.",
        "Quarterly revenue reached $999 million.",
        "Gross margin is 70.1%.",
        "Growth accelerated by 25pp.",
    ],
)
def test_rejects_a_figure_the_brief_does_not_supply(text: str, brief: ResearchBrief) -> None:
    assert unsupported_figures(text, brief) != ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "The final CompounderScore is 77.4 out of 100.",
        "CompounderScore: 77.4/100.",
        "A final score of 77.4 of 100 places it in the strong band.",
        "The raw score of 82 out of 100 fell to 77.4 after the risk penalty.",
    ],
)
def test_accepts_the_score_scale_under_a_score_the_brief_recorded(
    text: str, brief: ResearchBrief
) -> None:
    # 100 is a property of the formula, not of the company, so no brief will
    # ever supply it. Under a real score it is a denominator; anywhere else it
    # is a number about a business and stays subject to the ordinary rule.
    assert unsupported_figures(text, brief) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Revenue reached 100 million in the quarter.",
        "The company issued 100 million shares.",
        "Revenue growth was 100%.",
        "The company serves 100 customers.",
        "Market capitalisation is 100.",
        "The stock trades at a 100x multiple.",
        "Free cash flow of 77.4 out of 100 million was returned to shareholders.",
    ],
)
def test_rejects_a_hundred_that_is_not_the_score_scale(text: str, brief: ResearchBrief) -> None:
    assert "100" in "".join(unsupported_figures(text, brief))


@pytest.mark.unit
def test_rejects_a_score_the_brief_never_recorded_even_written_over_100(
    brief: ResearchBrief,
) -> None:
    # The denominator only earns its exemption by standing under a real score.
    figures = unsupported_figures("The final CompounderScore is 88.8 out of 100.", brief)

    assert "88.8" in "".join(figures)


@pytest.mark.unit
def test_drops_a_claim_whose_score_is_invented(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="The final CompounderScore is 88.8 out of 100.",
            basis=Basis.DETERMINISTIC,
            evidence=("S.growth",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.FABRICATED_NUMBER in _codes(report)


@pytest.mark.unit
def test_keeps_a_claim_stating_the_real_score_over_its_scale(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="The final CompounderScore is 77.4 out of 100.",
            basis=Basis.DETERMINISTIC,
            evidence=("S.growth",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.FABRICATED_NUMBER not in _codes(report)
    assert report.sections.why_it_ranked_high[0].basis is Basis.DETERMINISTIC


@pytest.mark.unit
def test_the_score_scale_exemption_does_not_reach_other_denominators(
    brief: ResearchBrief,
) -> None:
    # 35 is a real max_points the brief supplies, so it needs no exemption; 50
    # is neither supplied nor a scale, and must not inherit one.
    assert unsupported_figures("Growth contributed 30.4 of 35 points.", brief) == ()
    assert "50" in "".join(unsupported_figures("Growth contributed 30.4 of 50 points.", brief))


# --- numbers quoted from filing text ---------------------------------------

BUSINESS = "X.0000320193-26-000073.business"
NOTES = "X.0000320193-26-000073.item_1.01"


@pytest.fixture
def quoting_brief(brief: ResearchBrief) -> ResearchBrief:
    """A brief carrying two excerpts, each with numbers of its own."""
    return brief.model_copy(
        update={
            "excerpts": (
                FilingText(
                    id=BUSINESS,
                    accession=ACCESSION,
                    form="10-K",
                    filed=date(2026, 5, 2),
                    section="business",
                    text=(
                        "As of April 28, 2026, there were 96,589,132 shares of common stock "
                        "issued and outstanding. Of those, 86,674,201 were voted at the "
                        "annual meeting."
                    ),
                ),
                FilingText(
                    id=NOTES,
                    accession=ACCESSION,
                    form="8-K",
                    filed=date(2026, 5, 2),
                    section="item_1.01",
                    text=(
                        "The Issuers completed an offering of $1,000,000,000 aggregate "
                        "principal amount of 4.750% Senior Notes due 2031, at a leverage "
                        "ratio of 1.7x."
                    ),
                ),
            )
        }
    )


@pytest.mark.unit
def test_accepts_an_integer_quoted_from_a_cited_excerpt(quoting_brief: ResearchBrief) -> None:
    text = "There were 96,589,132 shares issued and outstanding."

    assert unsupported_figures(text, quoting_brief, cited=(BUSINESS,)) == ()


@pytest.mark.unit
def test_accepts_a_dollar_amount_quoted_from_a_cited_excerpt(
    quoting_brief: ResearchBrief,
) -> None:
    text = "The subsidiaries issued $1,000,000,000 of senior notes."

    assert unsupported_figures(text, quoting_brief, cited=(NOTES,)) == ()


@pytest.mark.unit
def test_accepts_a_percentage_quoted_from_a_cited_excerpt(quoting_brief: ResearchBrief) -> None:
    text = "The notes carry a 4.750% coupon."

    assert unsupported_figures(text, quoting_brief, cited=(NOTES,)) == ()


@pytest.mark.unit
def test_accepts_a_year_quoted_from_a_cited_excerpt(quoting_brief: ResearchBrief) -> None:
    assert unsupported_figures("The notes mature in 2031.", quoting_brief, cited=(NOTES,)) == ()


@pytest.mark.unit
def test_accepts_a_multiple_quoted_from_a_cited_excerpt(quoting_brief: ResearchBrief) -> None:
    text = "Leverage stood at 1.7x."

    assert unsupported_figures(text, quoting_brief, cited=(NOTES,)) == ()


@pytest.mark.unit
def test_rejects_a_number_from_an_excerpt_the_claim_does_not_cite(
    quoting_brief: ResearchBrief,
) -> None:
    # The whole point of the scoping: a figure in one filing says nothing about
    # a sentence describing another.
    text = "There were 96,589,132 shares issued and outstanding."

    assert unsupported_figures(text, quoting_brief, cited=(NOTES,)) != ()


@pytest.mark.unit
def test_rejects_a_quoted_number_when_the_claim_cites_no_excerpt(
    quoting_brief: ResearchBrief,
) -> None:
    text = "There were 96,589,132 shares issued and outstanding."

    assert unsupported_figures(text, quoting_brief) != ()
    assert unsupported_figures(text, quoting_brief, cited=("M.fcf_margin",)) != ()


@pytest.mark.unit
def test_filing_metadata_supplies_no_quotable_numbers(quoting_brief: ResearchBrief) -> None:
    # A `D.` id proves a filing exists. It carries no text, so it carries no
    # figures, and citing one may not license a number from anywhere.
    text = "There were 96,589,132 shares issued and outstanding."

    assert unsupported_figures(text, quoting_brief, cited=(f"D.{ACCESSION}",)) != ()


@pytest.mark.unit
def test_a_claim_may_quote_from_every_excerpt_it_cites(quoting_brief: ResearchBrief) -> None:
    text = "96,589,132 shares were outstanding when $1,000,000,000 of notes were issued."

    assert unsupported_figures(text, quoting_brief, cited=(BUSINESS, NOTES)) == ()


@pytest.mark.unit
@pytest.mark.parametrize("text", ["96,589,133 shares", "96,589,000 shares", "4.751% coupon"])
def test_rejects_a_number_close_to_one_in_a_cited_excerpt(
    text: str, quoting_brief: ResearchBrief
) -> None:
    # Tolerance is unchanged: near is not the same as quoted.
    assert unsupported_figures(text, quoting_brief, cited=(BUSINESS, NOTES)) != ()


@pytest.mark.unit
def test_a_quoted_number_may_not_change_unit(quoting_brief: ResearchBrief) -> None:
    # 96,589,132 shares is a count. Written as a percentage it is a different
    # claim, and the excerpt does not support it.
    assert unsupported_figures("Growth was 96,589,132%.", quoting_brief, cited=(BUSINESS,)) != ()


@pytest.mark.unit
def test_structured_evidence_still_works_without_any_excerpt(
    quoting_brief: ResearchBrief,
) -> None:
    assert unsupported_figures("Revenue grew 38.2%.", quoting_brief) == ()
    assert unsupported_figures("Revenue grew 38.2%.", quoting_brief, cited=(BUSINESS,)) == ()


@pytest.mark.unit
def test_an_excerpt_number_is_not_pooled_across_claims(
    quoting_brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    # End to end: the claim citing the right excerpt survives, the one citing
    # the other is dropped, in the same report.
    quoted = "There were 96,589,132 shares issued and outstanding."
    draft = make_draft(
        Section.RECENT_DEVELOPMENTS,
        Claim(text=quoted, basis=Basis.EXTRACTED, evidence=(BUSINESS,)),
        Claim(text=quoted, basis=Basis.EXTRACTED, evidence=(NOTES,)),
    )

    report = _validate(draft, quoting_brief)

    assert [claim.text for claim in report.sections.recent_developments] == [quoted]
    assert IssueCode.FABRICATED_NUMBER in _codes(report)


@pytest.mark.unit
def test_accepts_a_percentage_written_without_its_sign(brief: ResearchBrief) -> None:
    assert unsupported_figures("Revenue growth of 38.2 in the quarter.", brief) == ()


@pytest.mark.unit
def test_rejects_a_figure_in_a_unit_the_brief_does_not_speak(brief: ResearchBrief) -> None:
    # Basis points are a rescaling of a percentage-point figure, so accepting
    # them would be accepting arithmetic the brief did not do.
    assert unsupported_figures("Margins improved 250bps.", brief) == ("250 (unsupported unit)",)


@pytest.mark.unit
def test_accepts_a_share_count_written_in_billions(brief: ResearchBrief) -> None:
    assert unsupported_figures("There were 1.4 billion diluted shares.", brief) == ()


@pytest.mark.unit
def test_rejects_a_ratio_computed_from_two_supplied_figures(brief: ResearchBrief) -> None:
    # Gross profit 289M over revenue 412M is a 70.1% margin. Both inputs are in
    # the brief; the quotient is not, and this contract does not let the model
    # do the division.
    assert unsupported_figures("Gross margin reached 70.1%.", brief) == ("70.1%",)


@pytest.mark.unit
def test_rejects_a_percentage_borrowed_from_a_number_of_another_unit(
    brief: ResearchBrief,
) -> None:
    # 3.2 is in the brief as an EV/Revenue multiple. Written as a percentage it
    # is a different claim, and a bare integer must not be dressed up in a unit.
    assert unsupported_figures("Margins expanded 3.2%.", brief) == ("3.2%",)


@pytest.mark.unit
def test_rejects_a_multiple_borrowed_from_a_percentage(brief: ResearchBrief) -> None:
    assert unsupported_figures("The shares trade at 0.382x revenue.", brief) == ("0.382x",)


@pytest.mark.unit
def test_ignores_the_digits_in_a_filing_form_type(brief: ResearchBrief) -> None:
    assert unsupported_figures("The 10-K and the 8-K both mention it.", brief) == ()


@pytest.mark.unit
def test_drops_a_claim_containing_a_fabricated_figure(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.VALUATION_INTERPRETATION,
        Claim(
            text="It trades at 12.0x revenue, which is demanding.",
            basis=Basis.DETERMINISTIC,
            evidence=("M.ev_to_revenue",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.FABRICATED_NUMBER in _codes(report)


# --- investment advice -----------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "This is a buy at current levels.",
        "Investors should sell into strength.",
        "A price target of $50 looks reasonable.",
        "We would accumulate below book value.",
        "Consider position sizing carefully.",
    ],
)
def test_detects_recommendation_language(text: str) -> None:
    assert find_advice(text) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Share buybacks reduced the count.",
        "Sell-through improved in the quarter.",
        "Cross-sell into the enterprise base is the driver.",
        "The shares look expensive against peers.",
        "Selling costs fell as a share of revenue.",
        "Buy-side interest is not something the brief covers.",
    ],
)
def test_does_not_mistake_business_language_for_advice(text: str) -> None:
    assert find_advice(text) is None


@pytest.mark.unit
def test_drops_a_claim_that_recommends_an_action(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.BULL_CASE,
        Claim(
            text="Growth is durable, so this is a buy.",
            basis=Basis.INTERPRETATION,
            evidence=("M.revenue_growth_yoy",),
        ),
    )

    report = _validate(draft, brief)

    assert IssueCode.INVESTMENT_ADVICE in _codes(report)


# --- the confidence rationale ----------------------------------------------


@pytest.mark.unit
def test_what_the_model_writes_about_its_own_confidence_is_not_stored(
    brief: ResearchBrief,
) -> None:
    # The field exists on the wire because removing it stopped the model
    # answering at all. Its contents are diagnostic: a sentence about how
    # reliable the output is, composed by the thing being judged, is not
    # evidence, and never reaches a stored report.
    draft = DraftReport(ticker="ACME", claims=(), confidence_rationale="I did great work.")

    report = _validate(draft, brief)

    assert "I did great work." not in report.confidence.rationale


@pytest.mark.unit
def test_the_rationale_reports_coverage_and_ranking_state(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    report = _validate(make_draft(Section.CATALYSTS), brief)

    assert "metric coverage 85%" in report.confidence.rationale
    assert f"ranking {brief.score.ranking_state.value}" in report.confidence.rationale


@pytest.mark.unit
def test_the_rationale_says_when_no_filing_text_was_supplied(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    report = _validate(make_draft(Section.CATALYSTS), brief)

    assert "no filing text supplied" in report.confidence.rationale


@pytest.mark.unit
def test_the_rationale_reports_filing_coverage_once_text_exists(
    excerpted_brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    report = _validate(make_draft(Section.CATALYSTS), excerpted_brief)

    assert "cite a filing" in report.confidence.rationale


@pytest.mark.unit
def test_the_rationale_counts_what_validation_dropped(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    report = _validate(make_draft(Section.CATALYSTS), brief)

    assert f"{len(report.issues)} validation issue(s)" in report.confidence.rationale


@pytest.mark.unit
def test_the_rationale_explains_a_lowered_level(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(Section.CATALYSTS, confidence=ConfidenceLevel.HIGH)

    report = _validate(draft, brief)

    assert "lowered from HIGH" in report.confidence.rationale
    assert report.confidence.ceiling.value in report.confidence.rationale


@pytest.mark.unit
def test_a_voluntarily_low_level_is_not_described_as_a_ceiling(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    # The model may claim less than the evidence allows. Reporting that as
    # "matching the ceiling" credits validation with a correction it never made.
    draft = make_draft(Section.CATALYSTS, confidence=ConfidenceLevel.LOW)

    report = _validate(draft, brief)

    assert report.confidence.ceiling is ConfidenceLevel.MEDIUM
    assert "LOW as claimed, below the MEDIUM" in report.confidence.rationale


@pytest.mark.unit
def test_an_uncorrected_level_still_earns_a_rationale(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    # A report that was never corrected still has to say what its confidence
    # rests on, or the field means "nothing went wrong" rather than anything
    # about the evidence.
    report = _validate(make_draft(Section.CATALYSTS, confidence=ConfidenceLevel.LOW), brief)

    assert report.confidence.rationale.startswith("LOW ")


# --- claim length ----------------------------------------------------------


def _overlong(section: Section) -> DraftClaim:
    """A drafted claim that says one supported thing, at length.

    Built as a `DraftClaim` because a `Claim` cannot hold it — that asymmetry is
    the point of the fix: the wire model accepts what the accepted model refuses.
    """
    padding = "The company continued to compound through the period. " * 5
    text = f"Growth contributed 30.4 of 35 points. {padding}".strip()
    assert len(text) > MAX_CLAIM_CHARS
    return DraftClaim(
        section=section,
        text=text,
        basis=Basis.DETERMINISTIC,
        evidence=("S.growth",),
    )


@pytest.mark.unit
def test_drops_a_claim_that_runs_past_the_length_limit(brief: ResearchBrief) -> None:
    draft = DraftReport(ticker="ACME", claims=(_overlong(Section.WHY_IT_RANKED_HIGH),))

    report = _validate(draft, brief)

    assert IssueCode.CLAIM_TOO_LONG in _codes(report)
    assert report.sections.why_it_ranked_high[0].basis is Basis.UNKNOWN


@pytest.mark.unit
def test_an_overlong_claim_is_dropped_whole_never_truncated(brief: ResearchBrief) -> None:
    written = _overlong(Section.WHY_IT_RANKED_HIGH)
    draft = DraftReport(ticker="ACME", claims=(written,))

    report = _validate(draft, brief)

    kept = [claim.text for claim in report.sections.why_it_ranked_high]
    assert not any(written.text.startswith(text) for text in kept)


@pytest.mark.unit
def test_the_siblings_of_an_overlong_claim_survive(brief: ResearchBrief) -> None:
    # The DELL failure in miniature: one long sentence used to cost the whole
    # paid response. It may now cost only itself.
    good = DraftClaim(
        section=Section.WHY_IT_RANKED_HIGH,
        text="Valuation contributed 18.0 of 25 points.",
        basis=Basis.DETERMINISTIC,
        evidence=("S.valuation",),
    )
    draft = DraftReport(ticker="ACME", claims=(_overlong(Section.WHY_IT_RANKED_HIGH), good))

    report = _validate(draft, brief)

    assert [claim.text for claim in report.sections.why_it_ranked_high] == [good.text]


@pytest.mark.unit
def test_a_claim_exactly_at_the_limit_is_kept(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    head = "Growth contributed 30.4 of 35 points. "
    text = head + "x" * (MAX_CLAIM_CHARS - len(head))
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(text=text, basis=Basis.DETERMINISTIC, evidence=("S.growth",)),
    )

    report = _validate(draft, brief)

    assert len(report.sections.why_it_ranked_high[0].text) == MAX_CLAIM_CHARS
    assert IssueCode.CLAIM_TOO_LONG not in _codes(report)


@pytest.mark.unit
def test_the_length_issue_names_the_section_and_the_claim(brief: ResearchBrief) -> None:
    draft = DraftReport(ticker="ACME", claims=(_overlong(Section.WHY_IT_RANKED_HIGH),))

    report = _validate(draft, brief)

    issue = next(i for i in report.issues if i.code is IssueCode.CLAIM_TOO_LONG)
    assert issue.section is Section.WHY_IT_RANKED_HIGH
    assert issue.claim_index == 0
    assert str(MAX_CLAIM_CHARS) in issue.detail


@pytest.mark.unit
def test_a_section_emptied_by_length_alone_answers_unknown(brief: ResearchBrief) -> None:
    draft = DraftReport(ticker="ACME", claims=(_overlong(Section.CATALYSTS),))

    report = _validate(draft, brief)

    assert report.sections.catalysts[0].text == NO_EVIDENCE_TEXT
    assert Section.CATALYSTS.value in report.unknowns


# --- empty sections and status ---------------------------------------------


@pytest.mark.unit
def test_answers_an_emptied_section_unknown_rather_than_leaving_it_blank(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(Section.CATALYSTS)

    report = _validate(draft, brief)

    assert report.sections.catalysts[0].basis is Basis.UNKNOWN
    assert report.sections.catalysts[0].text == NO_EVIDENCE_TEXT
    assert IssueCode.EMPTY_SECTION in _codes(report)


@pytest.mark.unit
def test_lists_every_section_that_ended_up_unanswered(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.WHY_IT_RANKED_HIGH,
        Claim(
            text="Growth contributed 30.4 of 35 points.",
            basis=Basis.DETERMINISTIC,
            evidence=("S.growth",),
        ),
    )

    report = _validate(draft, brief)

    assert Section.WHY_IT_RANKED_HIGH.value not in report.unknowns
    assert Section.BULL_CASE.value in report.unknowns


@pytest.mark.unit
def test_an_empty_draft_produces_a_partial_report_not_an_exception(brief: ResearchBrief) -> None:
    report = _validate(DraftReport(ticker="ACME", claims=()), brief)

    assert report.status is ResearchStatus.PARTIAL
    assert len(report.unknowns) == len(Section)


@pytest.mark.unit
def test_a_fully_evidenced_draft_survives_intact(excerpted_brief: ResearchBrief) -> None:
    brief = excerpted_brief
    filing = "X.0000320193-26-000073.item_1.01"
    supplied: dict[Section, tuple[str, Basis, tuple[str, ...]]] = {
        Section.COMPANY_SUMMARY: ("A software company.", Basis.EXTRACTED, (filing,)),
        Section.WHY_IT_RANKED_HIGH: (
            "Growth contributed 30.4 of 35 points.",
            Basis.DETERMINISTIC,
            ("S.growth",),
        ),
        Section.GROWTH_DRIVERS: (
            "Revenue grew 38.2%.",
            Basis.DETERMINISTIC,
            ("M.revenue_growth_yoy",),
        ),
        Section.RECENT_DEVELOPMENTS: (
            "A quarterly report was filed.",
            Basis.EXTRACTED,
            (filing,),
        ),
        Section.CATALYSTS: (
            "Continued acceleration would be one.",
            Basis.INTERPRETATION,
            ("M.revenue_growth_acceleration",),
        ),
        Section.FINANCIAL_QUALITY_INTERPRETATION: (
            "Free cash flow margin is 12.1%.",
            Basis.DETERMINISTIC,
            ("M.fcf_margin",),
        ),
        Section.VALUATION_INTERPRETATION: (
            "It trades at 3.2x revenue.",
            Basis.DETERMINISTIC,
            ("M.ev_to_revenue",),
        ),
        Section.MAJOR_RISKS: (
            "Dilution is the largest recorded penalty.",
            Basis.INTERPRETATION,
            ("S.risk.dilution",),
        ),
        Section.DILUTION_FINANCING_RISK: (
            "Share count grew 11%.",
            Basis.DETERMINISTIC,
            ("S.risk.dilution",),
        ),
        Section.BULL_CASE: (
            "Growth may persist.",
            Basis.INTERPRETATION,
            ("M.revenue_growth_yoy",),
        ),
        Section.BEAR_CASE: (
            "Dilution may continue.",
            Basis.INTERPRETATION,
            ("S.risk.dilution",),
        ),
        Section.THESIS_BREAKERS: (
            "Deceleration would end the case.",
            Basis.INTERPRETATION,
            ("M.revenue_growth_acceleration",),
        ),
        Section.WATCH_NEXT_QUARTER: (
            "Whether margins hold.",
            Basis.INTERPRETATION,
            ("F.2026-03-31",),
        ),
    }
    draft = DraftReport(
        ticker="ACME",
        claims=tuple(
            DraftClaim(section=section, text=text, basis=basis, evidence=evidence)
            for section, (text, basis, evidence) in supplied.items()
        ),
        confidence=ConfidenceLevel.HIGH,
    )

    report = _validate(draft, brief)

    assert report.status is ResearchStatus.COMPLETE
    assert report.issues == ()
    assert report.unknowns == ()
    assert report.confidence.level is ConfidenceLevel.HIGH


@pytest.mark.unit
def test_records_the_brief_fingerprint_and_score_version(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    report = _validate(make_draft(Section.BULL_CASE), brief)

    assert report.brief_fingerprint == brief.fingerprint()
    assert report.score_version == "COMPOUNDER_V1_1"
    assert report.score_date == brief.score.score_date


# --- confidence ------------------------------------------------------------


@pytest.mark.unit
def test_high_confidence_needs_coverage_enrichment_and_a_filing(brief: ResearchBrief) -> None:
    assert confidence_ceiling(brief, cites_filing=True) is ConfidenceLevel.HIGH


@pytest.mark.unit
def test_no_cited_filing_caps_confidence_at_medium(brief: ResearchBrief) -> None:
    assert confidence_ceiling(brief, cites_filing=False) is ConfidenceLevel.MEDIUM


@pytest.mark.unit
def test_a_preliminary_ranking_caps_confidence_at_medium(brief: ResearchBrief) -> None:
    preliminary = brief.model_copy(
        update={"score": brief.score.model_copy(update={"ranking_state": RankingState.PRELIMINARY})}
    )

    assert confidence_ceiling(preliminary, cites_filing=True) is ConfidenceLevel.MEDIUM


@pytest.mark.unit
@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (None, ConfidenceLevel.LOW),
        (0.2, ConfidenceLevel.LOW),
        (0.59, ConfidenceLevel.LOW),
        (0.6, ConfidenceLevel.MEDIUM),
        (0.79, ConfidenceLevel.MEDIUM),
        (0.8, ConfidenceLevel.HIGH),
    ],
)
def test_coverage_decides_the_confidence_ceiling(
    coverage: float | None, expected: ConfidenceLevel, brief: ResearchBrief
) -> None:
    adjusted = brief.model_copy(
        update={"score": brief.score.model_copy(update={"data_coverage": coverage})}
    )

    assert confidence_ceiling(adjusted, cites_filing=True) is expected


@pytest.mark.unit
def test_lowers_confidence_the_evidence_does_not_support(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    thin = brief.model_copy(update={"score": brief.score.model_copy(update={"data_coverage": 0.3})})
    draft = make_draft(
        Section.BULL_CASE,
        Claim(
            text="Growth should persist.",
            basis=Basis.INTERPRETATION,
            evidence=("M.revenue_growth_yoy",),
        ),
        confidence=ConfidenceLevel.HIGH,
    )

    report = _validate(draft, thin)

    assert report.confidence.level is ConfidenceLevel.LOW
    assert report.confidence.claimed is ConfidenceLevel.HIGH
    assert IssueCode.CONFIDENCE_LOWERED in _codes(report)


@pytest.mark.unit
def test_never_raises_confidence_above_what_the_model_claimed(
    brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    draft = make_draft(
        Section.BULL_CASE,
        Claim(
            text="Growth should persist.",
            basis=Basis.INTERPRETATION,
            evidence=("M.revenue_growth_yoy",),
        ),
        confidence=ConfidenceLevel.LOW,
    )

    report = _validate(draft, brief)

    assert report.confidence.level is ConfidenceLevel.LOW


@pytest.mark.unit
def test_filing_coverage_counts_claims_resting_on_a_filing(
    excerpted_brief: ResearchBrief, make_draft: DraftFactory
) -> None:
    brief = excerpted_brief
    draft = make_draft(
        Section.RECENT_DEVELOPMENTS,
        Claim(
            text="A quarterly report was filed.",
            basis=Basis.EXTRACTED,
            evidence=("X.0000320193-26-000073.item_1.01",),
        ),
    )

    report = _validate(draft, brief)

    assert report.confidence.filing_coverage > 0
    assert report.confidence.metric_coverage == 0.85


# --- failure ---------------------------------------------------------------


@pytest.mark.unit
def test_a_failed_report_records_why_and_carries_no_sections(brief: ResearchBrief) -> None:
    report = failed_report(
        brief,
        prompt_version="RESEARCH_PROMPT_V1",
        model_id="test-model",
        generated_at=GENERATED_AT,
        detail="provider returned 429",
    )

    assert report.status is ResearchStatus.FAILED
    assert report.sections == ReportSections()
    assert report.confidence.level is ConfidenceLevel.LOW
    assert report.issues[0].detail == "provider returned 429"
    assert report.brief_fingerprint == brief.fingerprint()
