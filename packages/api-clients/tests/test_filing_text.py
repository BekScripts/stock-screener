"""Turning filed documents into quotable sections.

`unit`: pure text in, pure text out. The cases that matter are the ones where an
extractor is tempted to guess — a contents page that repeats every heading, a
cross-reference that looks like a section, a form nobody wrote a pattern for —
because a wrong excerpt is worse than a missing one and looks identical to a
reader who never opens the filing.
"""

from __future__ import annotations

import pytest

from api_clients import (
    MAX_SECTION_CHARS,
    MIN_SECTION_CHARS,
    clean_filing_text,
    extract_sections,
)

BODY = "The Company sells industrial widgets to municipal buyers. " * 12


def _sections(form: str, text: str) -> dict[str, str]:
    return {found.section: found.text for found in extract_sections(form, text)}


# --- cleaning --------------------------------------------------------------


@pytest.mark.unit
def test_strips_tags_and_keeps_the_words() -> None:
    html = "<html><body><p>Revenue <b>grew</b> sharply.</p></body></html>"

    assert clean_filing_text(html) == "Revenue grew sharply."


@pytest.mark.unit
def test_drops_script_and_style_content() -> None:
    html = "<div>Real text.</div><script>var hidden = 1;</script><style>.x{color:red}</style>"

    cleaned = clean_filing_text(html)

    assert "Real text." in cleaned
    assert "hidden" not in cleaned
    assert "color" not in cleaned


@pytest.mark.unit
def test_drops_inline_xbrl_scaffolding() -> None:
    html = "<ix:header><ix:hidden>0000320193</ix:hidden></ix:header><p>Filed text.</p>"

    cleaned = clean_filing_text(html)

    assert cleaned == "Filed text."


@pytest.mark.unit
def test_resolves_entities_and_non_breaking_spaces() -> None:
    html = "<p>Net&nbsp;income rose 5&#37; &amp; margins held.</p>"

    assert clean_filing_text(html) == "Net income rose 5% & margins held."


@pytest.mark.unit
def test_separates_blocks_so_headings_do_not_run_into_text() -> None:
    html = "<div>Item 1. Business</div><div>We make widgets.</div>"

    lines = clean_filing_text(html).splitlines()

    assert [line for line in lines if line] == ["Item 1. Business", "We make widgets."]


@pytest.mark.unit
def test_cleaning_is_deterministic() -> None:
    # The cache key and the stored excerpt both assume this: the same document
    # read twice must produce the same string, or re-extraction would churn rows
    # and change brief fingerprints for no reason.
    html = "<p>Alpha</p><table><tr><td>Beta</td></tr></table><p>Gamma  </p>"

    assert clean_filing_text(html) == clean_filing_text(html)


# --- 10-K ------------------------------------------------------------------


@pytest.mark.unit
def test_extracts_the_three_ten_k_sections() -> None:
    text = (
        f"Item 1. Business\n{BODY}\n"
        f"Item 1A. Risk Factors\n{'Widget demand is cyclical. ' * 20}\n"
        f"Item 7. Management's Discussion and Analysis\n{'Revenue rose. ' * 40}\n"
        "Item 8. Financial Statements\nSee the accompanying notes."
    )

    found = _sections("10-K", text)

    assert set(found) == {"business", "risk_factors", "mda"}
    assert found["business"].startswith("The Company sells industrial widgets")


@pytest.mark.unit
def test_prefers_the_section_over_the_table_of_contents() -> None:
    # Every filing names its items twice. The first match is the contents page,
    # and quoting it would hand a research brief a list of page numbers.
    text = (
        "Table of Contents\n"
        "Item 1. Business 3\nItem 1A. Risk Factors 12\n"
        f"Item 1. Business\n{BODY}\n"
        f"Item 1A. Risk Factors\n{'Demand is cyclical. ' * 25}"
    )

    found = _sections("10-K", text)

    assert "3\n" not in found["business"]
    assert found["business"].startswith("The Company sells")


@pytest.mark.unit
def test_a_section_shorter_than_the_floor_is_skipped() -> None:
    text = f"Item 1. Business\nSee Item 1A.\nItem 1A. Risk Factors\n{'Cyclical. ' * 60}"

    found = _sections("10-K", text)

    assert "business" not in found
    assert len(found["risk_factors"]) >= MIN_SECTION_CHARS


@pytest.mark.unit
def test_a_cross_reference_inside_a_sentence_does_not_end_a_section() -> None:
    # ACTG's MD&A refers to "Item 1A. Risk Factors" four hundred characters in.
    # Treating that as the start of the next item ended the section on its own
    # first paragraph and threw away everything it was extracted for.
    body = (
        "The following discussion should be read with our statements. Our results "
        'could differ because of the risks in "Item 1A. Risk Factors" to our Annual '
        "Report. "
    )
    text = f"Item 2. Management's Discussion\n{body}{'We license patents. ' * 60}"

    assert "We license patents." in _sections("10-Q", text)["mda"]


@pytest.mark.unit
def test_the_real_section_wins_over_a_cross_reference_that_precedes_it() -> None:
    # A filing names its MD&A in the forward-looking preamble before the section
    # itself. Both bodies exceed what is kept, so comparing them after the cut
    # made every candidate the same length and handed it to the earlier one.
    preamble = f"Item 2. Management's Discussion of results\n{'Boilerplate follows. ' * 300}"
    real = f"Item 2. Management's Discussion\n{'Revenue rose on licensing. ' * 3000}"

    mda = _sections("10-Q", f"{preamble}\n{real}")["mda"]

    assert "Revenue rose on licensing." in mda
    assert not mda.startswith("Boilerplate")


_SENTENCE = "The Company makes widgets. "
#: A 10-K whose business section is longer than what is kept, so the two tests
#: below exercise the cut rather than a section that fitted all along. Sized
#: from the bound so raising it cannot quietly turn them into no-ops.
OVERLONG_10K = f"Item 1. Business\n{_SENTENCE * (MAX_SECTION_CHARS // len(_SENTENCE) + 200)}"


@pytest.mark.unit
def test_a_long_section_is_cut_to_the_stored_bound() -> None:
    found = _sections("10-K", OVERLONG_10K)

    assert len(found["business"]) <= MAX_SECTION_CHARS


@pytest.mark.unit
def test_a_cut_section_ends_on_a_sentence() -> None:
    assert _sections("10-K", OVERLONG_10K)["business"].endswith(".")


# --- heading punctuation ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "heading",
    [
        "Item 2. Management's Discussion and Analysis",
        "Item 2. Management\u2019s Discussion and Analysis",
        "Item 2. Management\u2018s Discussion and Analysis",
        "ITEM 2 \u2014 MANAGEMENT\u2019S DISCUSSION AND ANALYSIS",
        "ITEM 2 \u2013 MANAGEMENT\u2019S DISCUSSION AND ANALYSIS",
        "Item 2 - Management's Discussion and Analysis",
        "Item 2: Management's Discussion and Analysis",
        "Item    2 .  Management\u2019s   Discussion  and Analysis",
        "Item 2 Managements Discussion and Analysis",
        "Item 2. Management Discussion and Analysis",
    ],
)
def test_finds_the_discussion_however_the_filer_punctuates_it(heading: str) -> None:
    # Dell files the em-dash-and-curly-apostrophe form, and until this folding
    # existed the MD&A pattern matched no filing from any company at all.
    text = f"{heading}\n{'Revenue rose on higher volumes. ' * 30}"

    assert _sections("10-Q", text)["mda"].startswith("Revenue rose on higher volumes.")


@pytest.mark.unit
def test_a_typographic_heading_is_quoted_exactly_as_filed() -> None:
    # Folding is for finding the heading. What gets stored is the filer's own
    # characters, curly apostrophes and all.
    body = "The Company\u2019s revenue rose on higher volumes. " * 20
    text = f"ITEM 2 \u2014 MANAGEMENT\u2019S DISCUSSION AND ANALYSIS\n{body}"

    extracted = _sections("10-Q", text)["mda"]

    assert "\u2019" in extracted
    assert "'" not in extracted


@pytest.mark.unit
def test_prose_about_management_discussion_is_not_a_section() -> None:
    # The item number is what makes it a heading. Without one this is a sentence.
    text = f"Our management discussion and analysis follows.\n{'Revenue rose. ' * 40}"

    assert _sections("10-Q", text) == {}


@pytest.mark.unit
def test_an_annual_report_discussion_is_item_seven() -> None:
    text = (
        f"Item 7 \u2014 Management\u2019s Discussion and Analysis\n{'Revenue rose. ' * 40}\n"
        "Item 8. Financial Statements\nSee notes."
    )

    assert "mda" in _sections("10-K", text)


@pytest.mark.unit
def test_typographic_dashes_still_bound_a_section() -> None:
    # The boundary search reads the same folded copy, so an em-dashed heading
    # ends the previous section rather than being swallowed by it.
    text = f"Item 1. Business\n{BODY}\nITEM 1A \u2014 RISK FACTORS\n{'Demand is cyclical. ' * 30}"

    found = _sections("10-K", text)

    assert "RISK FACTORS" not in found["business"]
    assert "risk_factors" in found


# --- 10-Q ------------------------------------------------------------------


@pytest.mark.unit
def test_extracts_the_ten_q_discussion_and_risks() -> None:
    text = (
        "Item 1. Financial Statements\nSee notes.\n"
        f"Item 2. Management's Discussion and Analysis\n{'Revenue rose 12%. ' * 40}\n"
        f"Item 1A. Risk Factors\n{'Supply remains tight. ' * 30}\n"
        "Item 6. Exhibits\nNone."
    )

    found = _sections("10-Q", text)

    assert set(found) == {"mda", "risk_factors"}
    assert found["mda"].startswith("Revenue rose 12%.")


@pytest.mark.unit
def test_a_ten_q_without_risk_factors_yields_only_what_it_has() -> None:
    text = f"Item 2. Management's Discussion and Analysis\n{'Revenue rose. ' * 50}"

    assert set(_sections("10-Q", text)) == {"mda"}


# --- 8-K -------------------------------------------------------------------


@pytest.mark.unit
def test_extracts_eight_k_items() -> None:
    text = (
        f"Item 2.02 Results of Operations and Financial Condition\n{'Revenue was 12. ' * 40}\n"
        f"Item 7.01 Regulation FD Disclosure\n{'A presentation was furnished. ' * 25}"
    )

    found = _sections("8-K", text)

    assert set(found) == {"item_2.02", "item_7.01"}


@pytest.mark.unit
def test_eight_k_items_come_back_in_research_priority_order() -> None:
    text = (
        f"Item 8.01 Other Events\n{'A dividend was declared. ' * 30}\n"
        f"Item 1.01 Entry into a Material Definitive Agreement\n{'A contract was signed. ' * 30}"
    )

    order = [found.section for found in extract_sections("8-K", text)]

    assert order == ["item_1.01", "item_8.01"]


@pytest.mark.unit
def test_an_eight_k_item_with_no_substance_is_skipped() -> None:
    text = (
        "Item 9.01 Financial Statements and Exhibits\n99.1 Press release.\n"
        f"Item 1.01 Entry\n{'Signed. ' * 80}"
    )

    assert "item_9.01" not in _sections("8-K", text)


# --- forms and malformed input --------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("form", ["S-1", "DEF 14A", "4", "", "13F-HR"])
def test_an_unread_form_yields_nothing(form: str) -> None:
    # There is no generic fallback on purpose: guessing at an unfamiliar layout
    # is how a cover page becomes a business description.
    assert extract_sections(form, f"Item 1. Business\n{BODY}") == ()


@pytest.mark.unit
def test_an_amended_form_is_read_like_the_form_it_amends() -> None:
    assert "business" in _sections("10-K/A", f"Item 1. Business\n{BODY}")


@pytest.mark.unit
def test_a_document_with_no_headings_yields_nothing() -> None:
    assert extract_sections("10-K", "A letter to shareholders. " * 100) == ()


@pytest.mark.unit
def test_an_empty_document_yields_nothing() -> None:
    assert extract_sections("10-K", "") == ()
    assert clean_filing_text("") == ""


@pytest.mark.unit
def test_unclosed_tags_do_not_stop_extraction() -> None:
    html = f"<html><body><p>Item 1. Business<p>{BODY}"

    assert "business" in _sections("10-K", clean_filing_text(html))


@pytest.mark.unit
def test_extraction_is_idempotent() -> None:
    text = f"Item 1. Business\n{BODY}"

    assert extract_sections("10-K", text) == extract_sections("10-K", text)


# --- 20-F: the same sections under different item numbers ------------------


TWENTY_F = (
    "Item 3. Key Information\n"
    "Risk Factors\n"
    f"{BODY}\n"
    "Item 4. Information on the Company\n"
    f"{BODY}\n"
    "Item 4A. Unresolved Staff Comments\n"
    "None.\n"
    "Item 5. Operating and Financial Review and Prospects\n"
    f"{BODY}\n"
    "Item 6. Directors, Senior Management and Employees\n"
)


@pytest.mark.unit
def test_extracts_the_three_twenty_f_sections() -> None:
    # A foreign private issuer's annual report is not a 10-K with a different
    # cover: business is Item 4 and the discussion is Item 5.
    found = _sections("20-F", TWENTY_F)

    assert set(found) == {"business", "risk_factors", "mda"}


@pytest.mark.unit
def test_ten_k_patterns_find_nothing_in_a_twenty_f() -> None:
    # Why this went unnoticed rather than producing wrong text: the 10-K item
    # numbers simply never matched.
    assert extract_sections("10-K", TWENTY_F) == ()


@pytest.mark.unit
def test_twenty_f_risk_factors_are_found_without_an_item_number() -> None:
    # A 20-F files them as a titled subsection of Item 3 rather than an item of
    # their own, so the heading is matched on its own line.
    found = _sections("20-F", TWENTY_F)

    assert found["risk_factors"].startswith("The Company sells industrial widgets")


@pytest.mark.unit
def test_a_cross_reference_is_not_mistaken_for_the_risk_factors_section() -> None:
    # "Risk Factors" appears throughout a 20-F's prose as a pointer. Quoting one
    # of those would return a sentence about the section instead of the section.
    text = (
        "Item 3. Key Information\n"
        "Risk Factors\n"
        f"{BODY}\n"
        "Item 4. Information on the Company\n"
        "Please see Item 3. Key Information - Risk Factors for a discussion of "
        f"the factors that may cause actual results to differ. {BODY}\n"
        "Item 5. Operating and Financial Review and Prospects\n"
    )

    found = _sections("20-F", text)

    assert found["risk_factors"].startswith("The Company sells industrial widgets")
    assert "Please see" not in found["risk_factors"]


@pytest.mark.unit
def test_an_amended_twenty_f_is_read_like_the_original() -> None:
    assert set(_sections("20-F/A", TWENTY_F)) == {"business", "risk_factors", "mda"}


@pytest.mark.unit
def test_a_six_k_yields_nothing() -> None:
    # A 6-K has no item structure at all. It carries monthly revenue releases,
    # board changes, AGM notices and half-year statements indistinguishably, so
    # there is nothing to locate deterministically and guessing is worse than
    # silence.
    assert extract_sections("6-K", TWENTY_F) == ()


@pytest.mark.unit
def test_a_forty_f_yields_nothing() -> None:
    # A 40-F incorporates a Canadian annual information form by reference, so
    # its item numbers name exhibits rather than the prose inside them.
    assert extract_sections("40-F", TWENTY_F) == ()
