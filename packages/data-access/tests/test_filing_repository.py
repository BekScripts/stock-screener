"""Filing index persistence, against a real SQLite database.

`integration`, like the other repository tests: what these check is the upsert
and the date bound, both of which live in the database rather than in the code
calling it.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import (
    CompanyRepository,
    FilingRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
    to_filing,
)
from domain import CompanyProfile, Filing

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as open_session:
        yield open_session
    engine.dispose()


@pytest.fixture
def company_id(session: Session) -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker="XYZ", name="Example Corp"))
    stored = companies.get_by_ticker("XYZ")
    assert stored is not None
    return stored.id


def _filing(accession: str, *, form: str = "10-Q", filed: date = date(2026, 5, 2)) -> Filing:
    return Filing(
        accession=accession,
        form=form,
        filed=filed,
        period_end=date(2026, 3, 31),
        primary_document="doc.htm",
        url=f"https://www.sec.gov/Archives/{accession}",
        source="sec-edgar",
    )


@pytest.mark.integration
def test_stores_filings(session: Session, company_id: int) -> None:
    written = FilingRepository(session).upsert_filings(company_id, [_filing("a-1"), _filing("a-2")])

    assert written == 2
    assert FilingRepository(session).count() == 2


@pytest.mark.integration
def test_re_running_replaces_rather_than_duplicates(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    filings.upsert_filings(company_id, [_filing("a-1")])
    filings.upsert_filings(company_id, [_filing("a-1")])

    assert filings.count() == 1


@pytest.mark.integration
def test_a_corrected_filing_overwrites_the_stored_one(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    filings.upsert_filings(company_id, [_filing("a-1", form="8-K")])
    filings.upsert_filings(company_id, [_filing("a-1", form="10-Q")])

    stored = filings.list_for_company(company_id)

    assert len(stored) == 1
    assert stored[0].form == "10-Q"


@pytest.mark.integration
def test_storing_nothing_writes_nothing(session: Session, company_id: int) -> None:
    assert FilingRepository(session).upsert_filings(company_id, []) == 0


@pytest.mark.integration
def test_lists_filings_newest_first(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    filings.upsert_filings(
        company_id,
        [
            _filing("a-1", filed=date(2026, 1, 5)),
            _filing("a-3", filed=date(2026, 7, 5)),
            _filing("a-2", filed=date(2026, 4, 5)),
        ],
    )

    filed = [row.filed for row in filings.list_for_company(company_id)]

    assert filed == sorted(filed, reverse=True)


@pytest.mark.integration
def test_excludes_filings_after_the_boundary_date(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    filings.upsert_filings(
        company_id,
        [
            _filing("before", filed=date(2026, 6, 1)),
            _filing("on", filed=date(2026, 6, 30)),
            _filing("after", filed=date(2026, 7, 1)),
        ],
    )

    accessions = [
        row.accession for row in filings.list_for_company(company_id, until=date(2026, 6, 30))
    ]

    assert accessions == ["on", "before"]


@pytest.mark.integration
def test_caps_the_number_returned(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    filings.upsert_filings(
        company_id, [_filing(f"a-{index}", filed=date(2026, 6, index + 1)) for index in range(5)]
    )

    assert len(filings.list_for_company(company_id, limit=2)) == 2


@pytest.mark.integration
def test_reports_the_newest_filing_date(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    filings.upsert_filings(
        company_id,
        [_filing("a-1", filed=date(2026, 1, 5)), _filing("a-2", filed=date(2026, 7, 5))],
    )

    assert filings.latest_filed(company_id) == date(2026, 7, 5)


@pytest.mark.integration
def test_a_company_with_no_filings_has_no_newest_date(session: Session, company_id: int) -> None:
    assert FilingRepository(session).latest_filed(company_id) is None


@pytest.mark.integration
def test_a_stored_row_converts_back_to_a_domain_filing(session: Session, company_id: int) -> None:
    filings = FilingRepository(session)
    original = _filing("a-1")
    filings.upsert_filings(company_id, [original])

    restored = to_filing(filings.list_for_company(company_id)[0])

    assert restored == original
