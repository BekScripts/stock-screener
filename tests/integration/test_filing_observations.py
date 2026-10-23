"""The append-only filing record, and the restatement it exists to survive.

The worked example from the Phase 8B brief is `test_a_restatement_appends_...`:
one period reported twice, at two figures, on two dates. Both must survive, and
a decision dated between them must see the first.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import CompanyRepository, FundamentalObservationRepository
from domain import CompanyProfile, FilingObservation
from stock_screener.pit import ingest_filing_observations

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed

PERIOD = date(2025, 12, 31)


class FakeFilingSource:
    """A fundamentals provider that reports individual facts, like EDGAR does."""

    def __init__(self, observations: dict[str, list[FilingObservation]]) -> None:
        self._observations = observations
        self.calls: list[str] = []

    def get_filing_observations(self, ticker: str) -> list[FilingObservation]:
        """Return the facts for one ticker, recording that it was asked."""
        self.calls.append(ticker)
        return list(self._observations.get(ticker, ()))


class PlainProvider:
    """A provider with no filing dates at all, like a normalised-statement vendor."""

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[object]:
        """Return nothing; this fake exists only to lack the observation method."""
        return []


def _observation(value: float, filed: date | None, **overrides: object) -> FilingObservation:
    """One reported revenue figure for the December 2025 quarter."""
    fields: dict[str, object] = {
        "taxonomy": "us-gaap",
        "concept": "Revenues",
        "value": value,
        "unit": "USD",
        "period_start": date(2025, 10, 1),
        "period_end": PERIOD,
        "filed_date": filed,
        "source": "sec-edgar",
    }
    fields.update(overrides)
    return FilingObservation(**fields)  # type: ignore[arg-type]


def _company(session: Session, ticker: str = "ACME") -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


@pytest.mark.integration
def test_a_restatement_appends_and_the_original_survives(session: Session) -> None:
    """The worked example: 100M filed in February, 104M filed in April.

    Both observations must exist afterwards. The first must not have been
    touched by the second.
    """
    company_id = _company(session)
    provider = FakeFilingSource(
        {
            "ACME": [
                _observation(
                    100_000_000.0, date(2026, 2, 15), accession="0000-26-000001", form="10-K"
                ),
                _observation(
                    104_000_000.0, date(2026, 4, 30), accession="0000-26-000009", form="10-Q"
                ),
            ]
        }
    )

    result = ingest_filing_observations(session, provider)
    session.flush()

    assert result.stored == 2
    revisions = FundamentalObservationRepository(session).revisions(company_id, PERIOD, "Revenues")
    assert [row.value for row in revisions] == [100_000_000.0, 104_000_000.0]
    assert [row.filed_date for row in revisions] == [date(2026, 2, 15), date(2026, 4, 30)]
    assert [row.accession for row in revisions] == ["0000-26-000001", "0000-26-000009"]
    assert [row.form for row in revisions] == ["10-K", "10-Q"]


@pytest.mark.integration
def test_the_filed_date_is_preserved(session: Session) -> None:
    _company(session)
    provider = FakeFilingSource({"ACME": [_observation(100.0, date(2026, 2, 15))]})

    ingest_filing_observations(session, provider)
    session.flush()

    company = CompanyRepository(session).get_by_ticker("ACME")
    assert company is not None
    stored = FundamentalObservationRepository(session).revisions(company.id, PERIOD, "Revenues")
    assert stored[0].filed_date == date(2026, 2, 15)
    assert stored[0].period_end == PERIOD
    assert stored[0].period_start == date(2025, 10, 1)


@pytest.mark.integration
def test_point_in_time_selection_respects_the_filing_date(session: Session) -> None:
    """A decision in March cannot see April's restatement."""
    company_id = _company(session)
    provider = FakeFilingSource(
        {
            "ACME": [
                _observation(100_000_000.0, date(2026, 2, 15)),
                _observation(104_000_000.0, date(2026, 4, 30)),
            ]
        }
    )
    ingest_filing_observations(session, provider)
    session.flush()

    repository = FundamentalObservationRepository(session)
    assert [row.value for row in repository.as_of(company_id, date(2026, 3, 1))] == [100_000_000.0]
    assert [row.value for row in repository.as_of(company_id, date(2026, 5, 1))] == [104_000_000.0]


@pytest.mark.integration
def test_period_end_is_never_used_as_availability(session: Session) -> None:
    """Filed in February, so invisible to a decision made in January."""
    company_id = _company(session)
    provider = FakeFilingSource({"ACME": [_observation(100.0, date(2026, 2, 15))]})
    ingest_filing_observations(session, provider)
    session.flush()

    repository = FundamentalObservationRepository(session)
    assert repository.as_of(company_id, date(2026, 1, 31)) == []
    assert len(repository.as_of(company_id, date(2026, 2, 15))) == 1


@pytest.mark.integration
def test_re_running_the_pass_appends_nothing(session: Session) -> None:
    _company(session)
    provider = FakeFilingSource({"ACME": [_observation(100.0, date(2026, 2, 15))]})

    first = ingest_filing_observations(session, provider)
    session.flush()
    second = ingest_filing_observations(session, provider)
    session.flush()

    assert (first.stored, second.stored) == (1, 0)
    assert FundamentalObservationRepository(session).count() == 1


@pytest.mark.integration
def test_an_undated_observation_is_stored_and_counted(session: Session) -> None:
    """Kept for the record, excluded from selection, and reported as a gap."""
    company_id = _company(session)
    provider = FakeFilingSource({"ACME": [_observation(90.0, None)]})

    result = ingest_filing_observations(session, provider)
    session.flush()

    assert result.stored == 1
    assert result.undated == 1
    assert FundamentalObservationRepository(session).as_of(company_id, date(2030, 1, 1)) == []


@pytest.mark.integration
def test_a_provider_without_filing_dates_records_nothing_and_says_so(
    session: Session,
) -> None:
    """A vendor that cannot supply a filing date must not have one invented."""
    _company(session)

    result = ingest_filing_observations(session, PlainProvider())

    assert result.companies == 0
    assert result.stored == 0
    assert FundamentalObservationRepository(session).count() == 0


@pytest.mark.integration
def test_one_failing_company_does_not_end_the_pass(session: Session) -> None:
    _company(session, "ACME")
    _company(session, "BETA")

    class Flaky(FakeFilingSource):
        def get_filing_observations(self, ticker: str) -> list[FilingObservation]:
            if ticker == "ACME":
                raise RuntimeError("edgar is having a day")
            return super().get_filing_observations(ticker)

    provider = Flaky({"BETA": [_observation(100.0, date(2026, 2, 15))]})
    result = ingest_filing_observations(session, provider)
    session.flush()

    assert result.skipped == 1
    assert result.stored == 1


@pytest.mark.integration
def test_the_pass_can_be_restricted_to_named_tickers(session: Session) -> None:
    _company(session, "ACME")
    _company(session, "BETA")
    provider = FakeFilingSource(
        {
            "ACME": [_observation(100.0, date(2026, 2, 15))],
            "BETA": [_observation(200.0, date(2026, 2, 15))],
        }
    )

    ingest_filing_observations(session, provider, tickers=["ACME"])
    session.flush()

    assert provider.calls == ["ACME"]
    assert FundamentalObservationRepository(session).count() == 1


@pytest.mark.integration
def test_the_existing_normalised_table_is_untouched(
    session: Session, seed: type[Seed], make: type[Make]
) -> None:
    """The append-only layer sits beside the operational one, not over it."""
    from data_access import FinancialSnapshotRepository

    company_id = seed.company(session, make, "ACME")
    before = len(FinancialSnapshotRepository(session).list_for_company(company_id))

    provider = FakeFilingSource({"ACME": [_observation(100.0, date(2026, 2, 15))]})
    ingest_filing_observations(session, provider)
    session.flush()

    after = FinancialSnapshotRepository(session).list_for_company(company_id)
    assert len(after) == before
