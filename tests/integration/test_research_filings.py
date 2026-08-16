"""Filing references in a brief, and the score date that bounds every input.

Two things are checked here that nothing else can check. That a brief carries
citable filings without a provider being called while it is assembled — the whole
reason the index is stored rather than fetched. And that data arriving *after* a
score cannot change the brief that explains it, which is what keeps a
week-old 81 from being explained with this week's numbers.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from api_clients import MockFundamentals
from data_access import (
    CompanyRepository,
    FilingExcerptRepository,
    FilingRepository,
    PriceHistoryRepository,
)
from domain import (
    CURRENT_SCORE_VERSION,
    BenchmarkReturns,
    CompanyMetrics,
    CompanyProfile,
    Filing,
    FilingExcerpt,
    FinancialPeriod,
    PriceBar,
    score_company,
)
from research import SelectionReason
from stock_screener.research import SCORE_SOURCE, assemble_brief
from stock_screener.research.brief import (
    _PRESERVED_SUBSCORES,
    MAX_EXCERPT_CHARS,
    MAX_EXCERPT_TOTAL_CHARS,
    MAX_EXCERPTS,
)
from stock_screener.scanning import select_text_filings, update_filing_text, update_filings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from data_access import FilingRecord
    from stock_screener.config import Settings

SCORE_DATE = date(2026, 6, 30)


def _filing(accession: str, *, form: str = "10-Q", filed: date) -> Filing:
    return Filing(
        accession=accession,
        form=form,
        filed=filed,
        period_end=filed - timedelta(days=30),
        primary_document="doc.htm",
        url=f"https://www.sec.gov/Archives/{accession}",
        source="sec-edgar",
    )


@pytest.fixture
def with_filings(session: Session, scored_company: str) -> str:
    """Store three filings for the seeded company, all before the score date."""
    company = CompanyRepository(session).get_by_ticker(scored_company)
    assert company is not None
    FilingRepository(session).upsert_filings(
        company.id,
        [
            _filing("a-1", form="10-K", filed=SCORE_DATE - timedelta(days=120)),
            _filing("a-2", form="10-Q", filed=SCORE_DATE - timedelta(days=30)),
            _filing("a-3", form="8-K", filed=SCORE_DATE - timedelta(days=5)),
        ],
    )
    return scored_company


# --- filings in a brief ----------------------------------------------------


@pytest.mark.integration
def test_a_brief_carries_the_stored_filings(
    session: Session, settings: Settings, with_filings: str
) -> None:
    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert len(brief.filings) == 3
    assert {filing.form for filing in brief.filings} == {"10-K", "10-Q", "8-K"}


@pytest.mark.integration
def test_filing_references_are_citable(
    session: Session, settings: Settings, with_filings: str
) -> None:
    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert "D.a-3" in brief.evidence_ids


@pytest.mark.integration
def test_filings_arrive_newest_first(
    session: Session, settings: Settings, with_filings: str
) -> None:
    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    filed = [filing.filed for filing in brief.filings]
    assert filed == sorted(filed, reverse=True)


@pytest.mark.integration
def test_a_brief_without_extraction_quotes_nothing(
    session: Session, settings: Settings, with_filings: str
) -> None:
    # Filings indexed, none read. The brief cites that they exist and offers no
    # text, which is what makes the filing-dependent sections answer UNKNOWN.
    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert brief.filings
    assert brief.excerpts == ()
    assert brief.extractable_ids == frozenset()


@pytest.mark.integration
def test_assembling_a_brief_with_filings_still_calls_no_provider(
    session: Session, settings: Settings, with_filings: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("assembling a brief must not reach a provider")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert brief.filings


# --- which filings get read ------------------------------------------------


def _stored(session: Session, ticker: str, entries: list[Filing]) -> list[FilingRecord]:
    """Store an index for one company and return the rows back."""
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    FilingRepository(session).upsert_filings(company.id, entries)
    session.flush()
    return FilingRepository(session).list_for_company(company.id)


def _forms(rows: list[FilingRecord]) -> list[str]:
    return sorted(row.form for row in rows)


@pytest.mark.integration
def test_the_annual_report_is_read_before_newer_eight_ks(
    session: Session, scored_company: str
) -> None:
    # The failure this policy exists to fix: a busy month of 8-Ks fills every
    # slot and the report describing the business is never read.
    rows = _stored(
        session,
        scored_company,
        [
            *(
                _filing(f"k-{index}", form="8-K", filed=SCORE_DATE - timedelta(days=index))
                for index in range(1, 9)
            ),
            _filing("annual", form="10-K", filed=SCORE_DATE - timedelta(days=300)),
        ],
    )

    selected = select_text_filings(rows)

    assert "10-K" in _forms(selected)
    assert len(selected) == 6


@pytest.mark.integration
def test_two_quarterlies_are_read_before_eight_ks_fill_the_rest(
    session: Session, scored_company: str
) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            *(
                _filing(f"k-{index}", form="8-K", filed=SCORE_DATE - timedelta(days=index))
                for index in range(1, 9)
            ),
            _filing("q-1", form="10-Q", filed=SCORE_DATE - timedelta(days=100)),
            _filing("q-2", form="10-Q", filed=SCORE_DATE - timedelta(days=190)),
            _filing("q-3", form="10-Q", filed=SCORE_DATE - timedelta(days=280)),
        ],
    )

    selected = select_text_filings(rows)

    assert _forms(selected).count("10-Q") == 2
    assert {row.accession for row in selected} >= {"q-1", "q-2"}
    assert "q-3" not in {row.accession for row in selected}


@pytest.mark.integration
def test_a_company_with_no_annual_report_gets_more_eight_ks(
    session: Session, scored_company: str
) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            *(
                _filing(f"k-{index}", form="8-K", filed=SCORE_DATE - timedelta(days=index))
                for index in range(1, 9)
            ),
            _filing("q-1", form="10-Q", filed=SCORE_DATE - timedelta(days=40)),
        ],
    )

    selected = select_text_filings(rows)

    assert len(selected) == 6
    assert _forms(selected).count("8-K") == 5


@pytest.mark.integration
def test_a_company_with_only_periodic_reports_still_fills_its_slots(
    session: Session, scored_company: str
) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            _filing("annual", form="10-K", filed=SCORE_DATE - timedelta(days=300)),
            *(
                _filing(f"q-{index}", form="10-Q", filed=SCORE_DATE - timedelta(days=index * 90))
                for index in range(1, 5)
            ),
        ],
    )

    selected = select_text_filings(rows)

    # One 10-K, two reserved 10-Qs, then the backfill takes what is left.
    assert len(selected) == 5
    assert _forms(selected) == ["10-K", "10-Q", "10-Q", "10-Q", "10-Q"]


@pytest.mark.integration
def test_selection_never_repeats_an_accession(session: Session, scored_company: str) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            _filing("annual", form="10-K", filed=SCORE_DATE - timedelta(days=300)),
            _filing("q-1", form="10-Q", filed=SCORE_DATE - timedelta(days=40)),
            _filing("k-1", form="8-K", filed=SCORE_DATE - timedelta(days=2)),
        ],
    )

    selected = select_text_filings(rows)

    assert len({row.accession for row in selected}) == len(selected)


@pytest.mark.integration
def test_selection_never_exceeds_the_limit(session: Session, scored_company: str) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            _filing(f"k-{index}", form="8-K", filed=SCORE_DATE - timedelta(days=index))
            for index in range(1, 20)
        ],
    )

    assert len(select_text_filings(rows)) == 6
    assert len(select_text_filings(rows, limit=2)) == 2


@pytest.mark.integration
def test_a_form_the_extractor_cannot_read_is_never_fetched(
    session: Session, scored_company: str
) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            _filing("proxy", form="DEF 14A", filed=SCORE_DATE - timedelta(days=1)),
            _filing("ownership", form="4", filed=SCORE_DATE - timedelta(days=2)),
            _filing("k-1", form="8-K", filed=SCORE_DATE - timedelta(days=3)),
        ],
    )

    selected = select_text_filings(rows)

    assert [row.accession for row in selected] == ["k-1"]


@pytest.mark.integration
def test_an_amended_report_counts_as_the_form_it_amends(
    session: Session, scored_company: str
) -> None:
    rows = _stored(
        session,
        scored_company,
        [
            _filing("annual-a", form="10-K/A", filed=SCORE_DATE - timedelta(days=10)),
            *(
                _filing(f"k-{index}", form="8-K", filed=SCORE_DATE - timedelta(days=index))
                for index in range(1, 9)
            ),
        ],
    )

    assert "10-K/A" in _forms(select_text_filings(rows))


@pytest.mark.integration
def test_a_selected_filing_already_extracted_is_not_fetched_again(
    session: Session, scored_company: str
) -> None:
    # Cache semantics under the new policy: selection decides which six filings
    # matter, and only the ones without stored text cost a request.
    _stored(
        session,
        scored_company,
        [
            _filing("a-1", form="10-K", filed=SCORE_DATE - timedelta(days=120)),
            _filing("a-2", form="10-Q", filed=SCORE_DATE - timedelta(days=30)),
            _filing("a-3", form="8-K", filed=SCORE_DATE - timedelta(days=5)),
        ],
    )
    update_filing_text(session, _document_provider(), tickers=[scored_company])

    provider = _document_provider()
    report = update_filing_text(session, provider, tickers=[scored_company])

    assert report.rows_written == 0
    assert report.skipped == 1


# --- extracted text --------------------------------------------------------


@pytest.fixture
def with_text(session: Session, with_filings: str) -> str:
    """Extract text for the stored filings from a mock that serves documents."""
    company = CompanyRepository(session).get_by_ticker(with_filings)
    assert company is not None
    update_filing_text(session, _document_provider(), tickers=[with_filings])
    return with_filings


def _excerpt_fixtures() -> dict[str, FilingExcerpt]:
    """One quotable section per stored filing."""
    return {
        "a-1": FilingExcerpt(
            accession="a-1",
            form="10-K",
            section="business",
            text="The Company designs and sells municipal water meters. " * 8,
            filed=SCORE_DATE - timedelta(days=120),
            url="https://www.sec.gov/Archives/a-1",
            source="sec-edgar",
        ),
        "a-2": FilingExcerpt(
            accession="a-2",
            form="10-Q",
            section="mda",
            text="Revenue grew on higher meter volumes. " * 8,
            filed=SCORE_DATE - timedelta(days=30),
            url="https://www.sec.gov/Archives/a-2",
            source="sec-edgar",
        ),
        "a-3": FilingExcerpt(
            accession="a-3",
            form="8-K",
            section="item_1.01",
            text="The Company entered a supply agreement with a municipal buyer. " * 6,
            filed=SCORE_DATE - timedelta(days=5),
            url="https://www.sec.gov/Archives/a-3",
            source="sec-edgar",
        ),
    }


def _document_provider(**overrides: object) -> MockFundamentals:
    """A provider serving the fixture excerpts."""
    return MockFundamentals(excerpts={"XYZ": list(_excerpt_fixtures().values())}, **overrides)  # type: ignore[arg-type]


@pytest.mark.integration
def test_extraction_stores_one_row_per_section(session: Session, with_filings: str) -> None:
    report = update_filing_text(session, _document_provider(), tickers=[with_filings])

    assert report.rows_written == 3
    assert FilingExcerptRepository(session).count() == 3


@pytest.mark.integration
def test_a_second_extraction_pass_fetches_nothing(session: Session, with_text: str) -> None:
    # The reason extraction is stored at all: re-running a nightly pass must not
    # re-read every document the SEC already served.
    provider = _document_provider()

    report = update_filing_text(session, provider, tickers=[with_text])

    assert report.skipped == 1
    assert report.rows_written == 0
    assert FilingExcerptRepository(session).count() == 3


@pytest.mark.integration
def test_extraction_is_idempotent(session: Session, with_text: str) -> None:
    update_filing_text(session, _document_provider(), tickers=[with_text], force=True)

    assert FilingExcerptRepository(session).count() == 3


@pytest.mark.integration
def test_a_default_pass_over_read_filings_fetches_nothing(
    session: Session, with_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cached pass must not reach the network")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)

    report = update_filing_text(session, _document_provider(), tickers=[with_text])

    assert report.rows_written == 0


@pytest.mark.integration
def test_force_re_reads_filings_that_already_have_text(session: Session, with_text: str) -> None:
    # What makes an improved extractor applicable to filings already read. The
    # provider records every call, so "did it fetch" is answerable directly.
    provider = _document_provider()

    report = update_filing_text(session, provider, tickers=[with_text], force=True)

    assert report.rows_written == 3
    assert FilingExcerptRepository(session).count() == 3


@pytest.mark.integration
def test_forcing_twice_leaves_one_row_per_section(session: Session, with_text: str) -> None:
    update_filing_text(session, _document_provider(), tickers=[with_text], force=True)
    update_filing_text(session, _document_provider(), tickers=[with_text], force=True)

    rows = FilingExcerptRepository(session).list_for_company(_company_id(session, with_text))
    keys = [(row.accession, row.section) for row in rows]

    assert len(keys) == len(set(keys)) == 3


@pytest.mark.integration
def test_a_re_extraction_that_finds_a_new_section_adds_it(session: Session, with_text: str) -> None:
    # The point of forcing: a filing read under an older extractor gains the
    # section the improved one can now see, without losing what it had.
    company_id = _company_id(session, with_text)
    extra = FilingExcerpt(
        accession="a-1",
        form="10-K",
        section="risk_factors",
        text="Demand for meters depends on municipal budgets. " * 8,
        filed=SCORE_DATE - timedelta(days=120),
        url="https://www.sec.gov/Archives/a-1",
        source="sec-edgar",
    )
    provider = MockFundamentals(
        excerpts={"XYZ": [*_excerpt_fixtures().values(), extra]},
    )

    update_filing_text(session, provider, tickers=[with_text], force=True)

    rows = FilingExcerptRepository(session).list_for_company(company_id)
    assert {row.section for row in rows} == {"business", "mda", "risk_factors", "item_1.01"}
    assert len(rows) == 4


def _company_id(session: Session, ticker: str) -> int:
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    return company.id


@pytest.mark.integration
def test_a_brief_quotes_the_extracted_text(
    session: Session, settings: Settings, with_text: str
) -> None:
    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    assert len(brief.excerpts) == 3
    assert brief.excerpts[0].section == "business"


@pytest.mark.integration
def test_excerpt_ids_are_deterministic(
    session: Session, settings: Settings, with_text: str
) -> None:
    first = assemble_brief(session, settings, with_text)
    second = assemble_brief(session, settings, with_text)

    assert first is not None
    assert second is not None
    assert [excerpt.id for excerpt in first.excerpts] == [
        "X.a-1.business",
        "X.a-2.mda",
        "X.a-3.item_1.01",
    ]
    assert first.fingerprint() == second.fingerprint()


@pytest.mark.integration
def test_quoted_text_is_what_makes_a_filing_extractable(
    session: Session, settings: Settings, with_text: str
) -> None:
    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    assert brief.extractable_ids == {"X.a-1.business", "X.a-2.mda", "X.a-3.item_1.01"}
    assert not any(cited.startswith("D.") for cited in brief.extractable_ids)


@pytest.mark.integration
def test_assembling_a_brief_with_text_still_calls_no_provider(
    session: Session, settings: Settings, with_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("assembling a brief must not reach a provider")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)

    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    assert brief.excerpts


@pytest.mark.integration
def test_text_from_a_filing_after_the_score_is_not_quoted(
    session: Session, settings: Settings, with_text: str
) -> None:
    company = CompanyRepository(session).get_by_ticker(with_text)
    assert company is not None
    FilingExcerptRepository(session).upsert_excerpts(
        company.id,
        [
            FilingExcerpt(
                accession="a-9",
                form="8-K",
                section="item_8.01",
                text="A later announcement the score never saw. " * 10,
                filed=SCORE_DATE + timedelta(days=10),
                url="https://www.sec.gov/Archives/a-9",
                source="sec-edgar",
            )
        ],
    )

    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    assert "X.a-9.item_8.01" not in brief.evidence_ids


@pytest.mark.integration
def test_a_busy_month_of_eight_ks_cannot_push_out_the_business_description(
    session: Session, settings: Settings, with_text: str
) -> None:
    # The failure this ordering exists to fix: governance 8-Ks filed last week
    # crowding out the section that says what the company does.
    company_id = _company_id(session, with_text)
    FilingExcerptRepository(session).upsert_excerpts(
        company_id,
        [
            FilingExcerpt(
                accession=f"new-{index}",
                form="8-K",
                section="item_8.01",
                text="A bylaw was amended. " * 20,
                filed=SCORE_DATE - timedelta(days=index),
                url="",
                source="sec-edgar",
            )
            for index in range(1, 6)
        ],
    )

    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    sections = [excerpt.section for excerpt in brief.excerpts]
    assert sections[:2] == ["business", "mda"]
    assert sections.count("item_8.01") == 3


@pytest.mark.integration
def test_the_newest_eight_k_wins_among_eight_ks(
    session: Session, settings: Settings, with_text: str
) -> None:
    company_id = _company_id(session, with_text)
    FilingExcerptRepository(session).upsert_excerpts(
        company_id,
        [
            FilingExcerpt(
                accession=f"new-{index}",
                form="8-K",
                section="item_8.01",
                text="A bylaw was amended. " * 20,
                filed=SCORE_DATE - timedelta(days=index),
                url="",
                source="sec-edgar",
            )
            for index in (1, 40)
        ],
    )

    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    eight_ks = [e for e in brief.excerpts if e.section == "item_8.01"]
    assert eight_ks[0].accession == "new-1"


@pytest.mark.integration
def test_a_brief_caps_how_many_excerpts_it_carries(
    session: Session, settings: Settings, with_filings: str
) -> None:
    company = CompanyRepository(session).get_by_ticker(with_filings)
    assert company is not None
    FilingExcerptRepository(session).upsert_excerpts(
        company.id,
        [
            FilingExcerpt(
                accession=f"b-{index}",
                form="8-K",
                section="item_8.01",
                text="An announcement. " * 30,
                filed=SCORE_DATE - timedelta(days=index),
                url="",
                source="sec-edgar",
            )
            for index in range(1, MAX_EXCERPTS + 4)
        ],
    )

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert len(brief.excerpts) == MAX_EXCERPTS


def _store_mda(session: Session, ticker: str, text: str) -> None:
    """Store one MD&A excerpt for a company."""
    FilingExcerptRepository(session).upsert_excerpts(
        _company_id(session, ticker),
        [
            FilingExcerpt(
                accession="mda-1",
                form="10-Q",
                section="mda",
                text=text,
                filed=SCORE_DATE - timedelta(days=1),
                url="",
                source="sec-edgar",
            )
        ],
    )


PREAMBLE = (
    "This management's discussion and analysis should be read in conjunction with "
    "the audited Consolidated Financial Statements. The following discussion contains "
    "forward-looking statements subject to risks and uncertainties. " * 8
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "heading",
    ["Company Overview", "Business Overview", "Overview", "Results of Operations"],
)
def test_an_mda_excerpt_starts_at_its_first_useful_heading(
    session: Session, settings: Settings, with_filings: str, heading: str
) -> None:
    # The preamble is longer than the excerpt budget, so reading from the top
    # sends a page of throat-clearing and none of the business.
    body = "The Company is a leader in municipal water metering. " * 12
    _store_mda(session, with_filings, f"{PREAMBLE}{heading}\n{body}")

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    excerpt = next(e for e in brief.excerpts if e.section == "mda")
    assert excerpt.text.startswith(heading)
    assert "leader in municipal water metering" in excerpt.text


@pytest.mark.integration
def test_an_mda_prefers_the_company_overview_to_a_later_heading(
    session: Session, settings: Settings, with_filings: str
) -> None:
    _store_mda(
        session,
        with_filings,
        f"{PREAMBLE}Company Overview\n{'We make meters. ' * 20}"
        f"Results of Operations\n{'Revenue rose. ' * 20}",
    )

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    excerpt = next(e for e in brief.excerpts if e.section == "mda")
    assert excerpt.text.startswith("Company Overview")


@pytest.mark.integration
def test_an_mda_with_no_heading_reads_from_the_top(
    session: Session, settings: Settings, with_filings: str
) -> None:
    _store_mda(session, with_filings, f"{PREAMBLE}{'Revenue rose. ' * 30}")

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    excerpt = next(e for e in brief.excerpts if e.section == "mda")
    assert excerpt.text.startswith("This management's discussion")


@pytest.mark.integration
def test_the_anchored_excerpt_is_still_verbatim_and_bounded(
    session: Session, settings: Settings, with_filings: str
) -> None:
    body = "The Company is a leader in municipal water metering. " * 60
    stored = f"{PREAMBLE}Company Overview\n{body}"
    _store_mda(session, with_filings, stored)

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    excerpt = next(e for e in brief.excerpts if e.section == "mda")
    assert excerpt.text in stored
    assert len(excerpt.text) <= MAX_EXCERPT_CHARS


@pytest.mark.integration
def test_only_the_discussion_is_anchored(
    session: Session, settings: Settings, with_text: str
) -> None:
    # Other sections keep reading from the top: a risk-factor section that
    # happens to say "overview" must not start there.
    FilingExcerptRepository(session).upsert_excerpts(
        _company_id(session, with_text),
        [
            FilingExcerpt(
                accession="rf-1",
                form="10-K",
                section="risk_factors",
                text=f"Demand is cyclical. {PREAMBLE}Overview\n{'Risks follow. ' * 20}",
                filed=SCORE_DATE - timedelta(days=1),
                url="",
                source="sec-edgar",
            )
        ],
    )

    brief = assemble_brief(session, settings, with_text)

    assert brief is not None
    excerpt = next(e for e in brief.excerpts if e.section == "risk_factors")
    assert excerpt.text.startswith("Demand is cyclical.")


@pytest.mark.integration
def test_a_brief_caps_the_length_of_one_excerpt(
    session: Session, settings: Settings, with_filings: str
) -> None:
    company = CompanyRepository(session).get_by_ticker(with_filings)
    assert company is not None
    FilingExcerptRepository(session).upsert_excerpts(
        company.id,
        [
            FilingExcerpt(
                accession="b-long",
                form="10-K",
                section="risk_factors",
                text="Demand for meters is cyclical and may decline. " * 200,
                filed=SCORE_DATE - timedelta(days=1),
                url="",
                source="sec-edgar",
            )
        ],
    )

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert len(brief.excerpts[0].text) <= MAX_EXCERPT_CHARS


@pytest.mark.integration
def test_a_brief_caps_the_total_filing_text_it_sends(
    session: Session, settings: Settings, with_filings: str
) -> None:
    # The bound that keeps the prompt affordable. Five long sections would sail
    # past it, so the run stops taking them.
    company = CompanyRepository(session).get_by_ticker(with_filings)
    assert company is not None
    FilingExcerptRepository(session).upsert_excerpts(
        company.id,
        [
            FilingExcerpt(
                accession=f"c-{index}",
                form="10-K",
                section="risk_factors",
                text="Demand for meters is cyclical and may decline. " * 200,
                filed=SCORE_DATE - timedelta(days=index),
                url="",
                source="sec-edgar",
            )
            for index in range(1, MAX_EXCERPTS + 1)
        ],
    )

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert sum(len(excerpt.text) for excerpt in brief.excerpts) <= MAX_EXCERPT_TOTAL_CHARS


# --- the score date is the boundary ----------------------------------------


@pytest.mark.integration
def test_a_filing_submitted_after_the_score_is_not_cited(
    session: Session, settings: Settings, with_filings: str
) -> None:
    company = CompanyRepository(session).get_by_ticker(with_filings)
    assert company is not None
    FilingRepository(session).upsert_filings(
        company.id, [_filing("later", filed=SCORE_DATE + timedelta(days=1))]
    )

    brief = assemble_brief(session, settings, with_filings)

    assert brief is not None
    assert "D.later" not in brief.evidence_ids


@pytest.mark.integration
def test_a_newer_price_bar_does_not_change_the_brief(
    session: Session, settings: Settings, scored_company: str
) -> None:
    before = assemble_brief(session, settings, scored_company)
    company = CompanyRepository(session).get_by_ticker(scored_company)
    assert company is not None

    PriceHistoryRepository(session).upsert_bars(
        company.id,
        [
            PriceBar(
                date=SCORE_DATE + timedelta(days=offset),
                open=999.0,
                high=1000.0,
                low=998.0,
                close=999.0,
                volume=5_000_000.0,
            )
            for offset in range(1, 6)
        ],
    )
    session.flush()
    after = assemble_brief(session, settings, scored_company)

    assert before is not None
    assert after is not None
    assert after.fingerprint() == before.fingerprint()


@pytest.mark.integration
def test_a_newer_financial_period_does_not_change_the_brief(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed], scored_company: str
) -> None:
    from data_access import FinancialSnapshotRepository

    before = assemble_brief(session, settings, scored_company)
    company = CompanyRepository(session).get_by_ticker(scored_company)
    assert company is not None

    FinancialSnapshotRepository(session).upsert_periods(
        company.id,
        [
            FinancialPeriod(
                period_end=SCORE_DATE + timedelta(days=92),
                revenue=999_000_000.0,
                gross_profit=500_000_000.0,
                source="test",
            )
        ],
    )
    session.flush()
    after = assemble_brief(session, settings, scored_company)

    assert before is not None
    assert after is not None
    assert after.fingerprint() == before.fingerprint()
    assert all(period.period_end <= SCORE_DATE for period in after.quarters)


@pytest.mark.integration
def test_a_later_score_is_not_part_of_the_history(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "LATER")
    seed.score(session, company_id, "LATER", final=70.0, score_date=SCORE_DATE - timedelta(days=30))
    seed.score(session, company_id, "LATER", final=80.0)

    brief = assemble_brief(session, settings, "LATER", score_version=CURRENT_SCORE_VERSION)

    assert brief is not None
    assert [point.score_date for point in brief.score_history] == [SCORE_DATE - timedelta(days=30)]


@pytest.mark.integration
def test_an_older_score_is_explained_with_the_data_of_its_own_day(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # Two scoring days. Asking for the older one must produce a brief whose
    # evidence stops at that day, not at the newer one.
    company_id = seed.company(session, make, "TWICE")
    older = SCORE_DATE - timedelta(days=30)
    seed.score(session, company_id, "TWICE", final=70.0, score_date=older)
    seed.score(session, company_id, "TWICE", final=80.0)

    latest = assemble_brief(session, settings, "TWICE")

    assert latest is not None
    assert latest.as_of == SCORE_DATE
    assert all(filing.filed <= SCORE_DATE for filing in latest.filings)
    assert all(point.score_date < SCORE_DATE for point in latest.score_history)


# --- preserved score-time values -------------------------------------------


@pytest.mark.integration
def test_a_preserved_fact_matches_the_subscore_it_explains(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    facts = {fact.id: fact for fact in brief.all_facts}
    items = {item.id: item for item in brief.score.items}

    assert facts["M.gross_margin"].value == items["S.quality.gross_margin"].observed
    assert facts["M.gross_margin"].source == SCORE_SOURCE


@pytest.mark.integration
def test_a_metric_the_score_could_not_use_stays_unknown_in_the_brief(
    session: Session, settings: Settings, scored_company: str
) -> None:
    # The seeded score has an unavailable `*_secondary` sub-score. Any metric a
    # sub-score recorded as None must not be quietly refilled from a fresh
    # calculation — the brief would then explain a sub-score that earned nothing
    # with a number that exists.
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    for fact in brief.all_facts:
        if fact.source == SCORE_SOURCE and not fact.known:
            assert fact.id in brief.unknowns


@pytest.mark.integration
def test_an_unpreserved_metric_is_recalculated_and_says_so(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    facts = {fact.id: fact for fact in brief.all_facts}

    assert facts["M.ttm_revenue"].source != SCORE_SOURCE
    assert facts["M.ttm_revenue"].value is not None


@pytest.mark.unit
def test_every_preserved_subscore_still_exists_in_a_real_score() -> None:
    # The preserve map is keyed on sub-score names the scoring engine emits. A
    # renamed sub-score would silently stop preserving its metric and the brief
    # would drift back to recomputed values, which is exactly the bug this whole
    # mechanism exists to prevent.
    score = score_company(
        CompanyProfile(ticker="XYZ", name="Example Corp", market_cap=1_000_000_000.0),
        CompanyMetrics(
            ticker="XYZ",
            price=25.0,
            market_cap=1_000_000_000.0,
            revenue_growth_yoy=0.35,
            previous_revenue_growth_yoy=0.20,
            revenue_growth_acceleration=0.15,
            recent_revenue_growth_yoy=(0.35, 0.20, 0.18, 0.15),
            ttm_revenue=400_000_000.0,
            revenue_cagr_3y=0.25,
            gross_margin=0.55,
            gross_margin_change=0.02,
            gross_profit_growth_yoy=0.40,
            operating_margin=0.10,
            operating_margin_change=0.03,
            fcf_margin=0.12,
            ttm_free_cash_flow=48_000_000.0,
            cash=200_000_000.0,
            debt=50_000_000.0,
            net_cash=150_000_000.0,
            enterprise_value=850_000_000.0,
            share_count_growth_yoy=0.02,
            return_6m=0.30,
            return_12m=0.45,
            distance_from_52w_high=-0.05,
        ),
        BenchmarkReturns(symbol="SPY", return_6m=0.05, return_12m=0.10),
        eligible=True,
    )

    emitted = {
        f"{component.name}.{sub.name}"
        for component in score.components
        for sub in component.subscores
    }

    assert set(_PRESERVED_SUBSCORES) <= emitted


# --- ingestion -------------------------------------------------------------


@pytest.mark.integration
def test_ingestion_stores_what_the_provider_returns(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    seed.company(session, make, "XYZ")
    provider = MockFundamentals(
        filings={"XYZ": [_filing("a-1", filed=SCORE_DATE), _filing("a-2", filed=SCORE_DATE)]}
    )

    report = update_filings(session, provider, tickers=["XYZ"])

    assert report.succeeded == 1
    assert report.rows_written == 2
    assert FilingRepository(session).count() == 2


@pytest.mark.integration
def test_ingestion_skips_a_company_the_provider_has_no_filings_for(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    seed.company(session, make, "XYZ")

    report = update_filings(session, MockFundamentals(), tickers=["XYZ"])

    assert report.skipped == 1
    assert report.succeeded == 0
    assert FilingRepository(session).count() == 0


@pytest.mark.integration
def test_one_failing_company_does_not_stop_the_pass(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    seed.company(session, make, "AAA")
    seed.company(session, make, "BBB")
    provider = MockFundamentals(
        filings={"BBB": [_filing("b-1", filed=SCORE_DATE)]}, failing_tickers=["AAA"]
    )

    report = update_filings(session, provider)

    assert report.failed == 1
    assert report.succeeded == 1
    assert report.failures == ["AAA"]


@pytest.mark.integration
def test_re_running_ingestion_does_not_duplicate(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    seed.company(session, make, "XYZ")
    provider = MockFundamentals(filings={"XYZ": [_filing("a-1", filed=SCORE_DATE)]})

    update_filings(session, provider, tickers=["XYZ"])
    update_filings(session, provider, tickers=["XYZ"])

    assert FilingRepository(session).count() == 1


@pytest.mark.integration
def test_a_brief_lists_no_filings_when_none_were_ingested(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company, selection=SelectionReason.HIDDEN_GEM)

    assert brief is not None
    assert brief.filings == ()
