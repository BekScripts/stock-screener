"""When a filing became public, and the contract that turns it into an answer.

The point of this suite is that a *date* cannot decide availability and an
*instant* can. Most of it is therefore about the boundary: what happens to a
filing accepted a minute either side of the close, and what happens when nobody
knows when it was accepted at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from data_access import CompanyRepository, FilingAcceptanceRepository
from domain import AcceptedFiling, CompanyProfile
from stock_screener.pit import ingest_filing_acceptance
from stock_screener.pit.decision_time import (
    MARKET_TIMEZONE,
    DecisionPoint,
    decision_instant,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class FakeAcceptanceSource:
    """A provider that publishes acceptance times, as EDGAR does."""

    def __init__(self, records: dict[str, list[AcceptedFiling]]) -> None:
        self._records = records
        self.calls: list[str] = []

    def get_filing_acceptance(self, ticker: str) -> list[AcceptedFiling]:
        """Return the records for one ticker, noting that it was asked."""
        self.calls.append(ticker)
        return list(self._records.get(ticker, ()))


class PlainProvider:
    """A provider with no acceptance clock at all."""

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[object]:
        """Return nothing; this fake exists only to lack the method."""
        return []


def _company(session: Session, ticker: str = "ACME") -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _accepted(accession: str, moment: datetime) -> AcceptedFiling:
    """One acceptance record."""
    return AcceptedFiling(
        accession=accession,
        accepted_at=moment,
        filed_date=moment.date(),
        form="10-K",
        source="sec-edgar",
    )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_decision_instant_is_the_close_in_new_york() -> None:
    moment = decision_instant(date(2026, 8, 27))

    assert moment.hour == 16
    assert moment.minute == 0
    assert moment.tzinfo is MARKET_TIMEZONE


@pytest.mark.unit
def test_the_close_moves_with_daylight_saving() -> None:
    """A fixed offset would be an hour wrong for two-thirds of the year."""
    summer = decision_instant(date(2026, 8, 27)).astimezone(UTC)
    winter = decision_instant(date(2026, 1, 15)).astimezone(UTC)

    assert summer.hour == 20
    assert winter.hour == 21


@pytest.mark.unit
def test_the_open_is_available_for_a_future_strategy_that_needs_it() -> None:
    moment = decision_instant(date(2026, 8, 27), DecisionPoint.OPEN)

    assert (moment.hour, moment.minute) == (9, 30)


@pytest.mark.unit
def test_a_non_trading_day_still_yields_an_instant() -> None:
    """There is no market calendar, and this documents what that costs.

    A Sunday gets Sunday's close. Harmless for an availability test; wrong for
    anything claiming to describe a session.
    """
    assert decision_instant(date(2026, 8, 30)).hour == 16


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_an_acceptance_time_is_stored_once_per_accession(session: Session) -> None:
    """One filing carries hundreds of facts; its acceptance is one row."""
    repo = FilingAcceptanceRepository(session)
    row = {
        "accession": "0000-26-000001",
        "accepted_at": datetime(2026, 2, 15, 21, 30, tzinfo=UTC),
        "filed_date": date(2026, 2, 15),
        "form": "10-K",
        "source": "sec-edgar",
    }

    assert repo.add_all([row]) == 1
    session.flush()
    assert repo.add_all([row]) == 0
    session.flush()

    assert repo.count() == 1


@pytest.mark.integration
def test_an_instant_in_another_zone_is_normalised_to_utc(session: Session) -> None:
    """Stored timestamps are UTC. A New York instant must not be written as its
    own clock reading, or every comparison against it is hours out."""
    repo = FilingAcceptanceRepository(session)
    repo.add_all(
        [
            {
                "accession": "0000-26-000002",
                "accepted_at": datetime(2026, 2, 15, 16, 30, tzinfo=MARKET_TIMEZONE),
                "filed_date": date(2026, 2, 15),
                "form": "10-K",
                "source": "sec-edgar",
            }
        ]
    )
    session.flush()

    stored = session.execute(
        __import__("sqlalchemy").text("SELECT accepted_at FROM filing_acceptance")
    ).scalar()
    # 16:30 New York in February is 21:30 UTC.
    assert "21:30" in str(stored)


@pytest.mark.integration
def test_known_accessions_reports_what_is_already_resolved(session: Session) -> None:
    repo = FilingAcceptanceRepository(session)
    repo.add_all(
        [
            {
                "accession": "known",
                "accepted_at": datetime(2026, 2, 15, 21, 0, tzinfo=UTC),
                "filed_date": date(2026, 2, 15),
                "form": "10-K",
                "source": "sec-edgar",
            }
        ]
    )
    session.flush()

    assert repo.known_accessions(["known", "unknown"]) == {"known"}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_acceptance_times_are_ingested(session: Session) -> None:
    _company(session)
    provider = FakeAcceptanceSource(
        {"ACME": [_accepted("a1", datetime(2026, 2, 15, 21, 30, tzinfo=UTC))]}
    )

    result = ingest_filing_acceptance(session, provider, only_missing=False)
    session.flush()

    assert result.stored == 1
    assert FilingAcceptanceRepository(session).count() == 1


@pytest.mark.integration
def test_re_running_appends_nothing(session: Session) -> None:
    _company(session)
    provider = FakeAcceptanceSource(
        {"ACME": [_accepted("a1", datetime(2026, 2, 15, 21, 30, tzinfo=UTC))]}
    )

    first = ingest_filing_acceptance(session, provider, only_missing=False)
    session.flush()
    second = ingest_filing_acceptance(session, provider, only_missing=False)
    session.flush()

    assert (first.stored, second.stored) == (1, 0)


@pytest.mark.integration
def test_a_company_with_nothing_unresolved_is_skipped_without_a_request(
    session: Session,
) -> None:
    """What makes a repeat pass nearly free."""
    _company(session)
    provider = FakeAcceptanceSource(
        {"ACME": [_accepted("a1", datetime(2026, 2, 15, 21, 30, tzinfo=UTC))]}
    )

    ingest_filing_acceptance(session, provider, only_missing=True)

    # No observations exist, so nothing is unresolved and nothing is fetched.
    assert provider.calls == []


@pytest.mark.integration
def test_a_provider_without_an_acceptance_clock_records_nothing(
    session: Session,
) -> None:
    """A vendor that cannot supply one must not have it invented."""
    _company(session)

    result = ingest_filing_acceptance(session, PlainProvider(), only_missing=False)

    assert result.companies == 0
    assert result.stored == 0


@pytest.mark.integration
def test_one_failing_company_does_not_end_the_pass(session: Session) -> None:
    _company(session, "ACME")
    _company(session, "BETA")

    class Flaky(FakeAcceptanceSource):
        def get_filing_acceptance(self, ticker: str) -> list[AcceptedFiling]:
            if ticker == "ACME":
                raise RuntimeError("edgar is having a day")
            return super().get_filing_acceptance(ticker)

    provider = Flaky({"BETA": [_accepted("b1", datetime(2026, 2, 15, 21, tzinfo=UTC))]})
    result = ingest_filing_acceptance(session, provider, only_missing=False)
    session.flush()

    assert result.stored == 1
    assert [ticker for ticker, _ in result.failures] == ["ACME"]
    assert "ACME" in result.retry_report()


@pytest.mark.integration
def test_a_company_the_provider_has_nothing_for_is_not_a_failure(
    session: Session,
) -> None:
    """The SEC genuinely has no filings for many symbols. That is a gap, not an
    outage, and conflating the two would make a normal day look broken."""
    _company(session)
    provider = FakeAcceptanceSource({})

    result = ingest_filing_acceptance(session, provider, only_missing=False)

    assert result.failures == ()
    assert result.empty == ("ACME",)
    assert result.retry_report() == "no failures"
