"""Resolving, storing and reusing exchange rates across a scoring run.

`integration`: these use a real database session, because the point of the
resolver is that a rate written on one run is the rate read on the next, and an
in-memory fake would prove nothing about that.

No test here reaches a live FX endpoint. The provider is a fake that counts its
calls, which is how the "do not fetch the same rate four hundred times" property
is actually checked rather than asserted about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from api_clients import ProviderError
from data_access import FxRateRepository
from domain import FxConversion
from stock_screener.fx import FxRateResolver, pairs_needed

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

FRIDAY = date(2026, 8, 14)
SUNDAY = date(2026, 8, 16)
MAX_AGE = 5


class CountingFx:
    """A rate source that records every call it receives."""

    name = "counting"

    def __init__(
        self, rates: dict[tuple[str, str], float] | None = None, *, rate_date: date | None = None
    ) -> None:
        self._rates = {("USD", "EUR"): 0.86453} if rates is None else rates
        self._rate_date = rate_date
        self.calls: list[tuple[str, str, date]] = []

    def get_rate(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        self.calls.append((base, quote, as_of))
        rate = self._rates.get((base, quote))
        if rate is None:
            return None
        return FxConversion(
            base=base,
            quote=quote,
            rate=rate,
            rate_date=self._rate_date or as_of,
            provider=self.name,
            retrieved_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        )


class FailingFx:
    """A rate source that is unreachable."""

    name = "failing"

    def get_rate(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        raise ProviderError("fx endpoint unreachable")


# -- what needs converting at all -------------------------------------------


@pytest.mark.unit
def test_only_companies_whose_currencies_differ_need_a_rate() -> None:
    # The domestic path must never reach the FX layer: nearly the whole universe
    # files and trades in dollars, and an FX outage cannot be allowed to matter
    # to it.
    pairs = pairs_needed(
        [("USD", "USD"), (None, None), ("USD", None), ("USD", "TWD"), ("USD", "EUR"), (None, "EUR")]
    )

    assert pairs == {("USD", "TWD"), ("USD", "EUR")}


# -- fetching, storing, reusing ---------------------------------------------


@pytest.mark.integration
def test_a_fetched_rate_is_persisted(session: Session) -> None:
    resolver = FxRateResolver(session, CountingFx(), max_age_days=MAX_AGE)

    resolver.rate_for("USD", "EUR", FRIDAY)
    session.flush()

    stored = FxRateRepository(session).latest_on_or_before(
        "USD", "EUR", FRIDAY, max_age_days=MAX_AGE
    )
    assert stored is not None
    assert stored.rate == pytest.approx(0.86453)
    assert stored.provider == "counting"


@pytest.mark.integration
def test_the_same_pair_is_fetched_once_per_run(session: Session) -> None:
    # Four hundred euro-reporting companies need one USD/EUR rate between them.
    provider = CountingFx()
    resolver = FxRateResolver(session, provider, max_age_days=MAX_AGE)

    for _ in range(50):
        resolver.rate_for("USD", "EUR", FRIDAY)

    assert len(provider.calls) == 1


@pytest.mark.integration
def test_a_stored_rate_is_reused_by_a_later_run(session: Session) -> None:
    # A past day's fixing is final, so a stored observation is the answer rather
    # than a cache to revalidate.
    FxRateResolver(session, CountingFx(), max_age_days=MAX_AGE).rate_for("USD", "EUR", FRIDAY)
    session.flush()

    provider = CountingFx()
    rate = FxRateResolver(session, provider, max_age_days=MAX_AGE).rate_for("USD", "EUR", FRIDAY)

    assert rate is not None
    assert provider.calls == []


@pytest.mark.integration
def test_saving_the_same_observation_twice_does_not_duplicate_it(session: Session) -> None:
    repository = FxRateRepository(session)
    conversion = FxConversion(
        base="USD", quote="EUR", rate=0.86453, rate_date=FRIDAY, provider="ecb"
    )

    repository.save(conversion)
    repository.save(conversion.model_copy(update={"rate": 0.86500}))
    session.flush()

    assert repository.count() == 1
    stored = repository.latest_on_or_before("USD", "EUR", FRIDAY, max_age_days=MAX_AGE)
    assert stored is not None
    assert stored.rate == pytest.approx(0.86500)


# -- dates ------------------------------------------------------------------


@pytest.mark.integration
def test_a_weekend_score_uses_the_preceding_business_day(session: Session) -> None:
    provider = CountingFx(rate_date=FRIDAY)

    rate = FxRateResolver(session, provider, max_age_days=MAX_AGE).rate_for("USD", "EUR", SUNDAY)

    assert rate is not None
    assert rate.rate_date == FRIDAY


@pytest.mark.integration
def test_a_historical_score_uses_the_rate_from_its_own_date(session: Session) -> None:
    # Two observations a month apart. Scoring the older date must not reach
    # forward to the newer rate, which would restate an old score in money that
    # did not exist when it was computed.
    repository = FxRateRepository(session)
    repository.save(
        FxConversion(
            base="USD", quote="EUR", rate=0.90, rate_date=date(2026, 7, 15), provider="ecb"
        )
    )
    repository.save(
        FxConversion(base="USD", quote="EUR", rate=0.86, rate_date=FRIDAY, provider="ecb")
    )
    session.flush()

    resolver = FxRateResolver(session, CountingFx(), max_age_days=MAX_AGE)
    historical = resolver.rate_for("USD", "EUR", date(2026, 7, 16))

    assert historical is not None
    assert historical.rate == pytest.approx(0.90)
    assert historical.rate_date == date(2026, 7, 15)


@pytest.mark.integration
def test_a_stale_stored_rate_is_refused(session: Session) -> None:
    # A fixing series has no ordinary gap longer than a holiday weekend, so a
    # rate from the far side of one is not evidence about the date in question.
    FxRateRepository(session).save(
        FxConversion(
            base="USD",
            quote="EUR",
            rate=0.90,
            rate_date=FRIDAY - timedelta(days=40),
            provider="ecb",
        )
    )
    session.flush()

    resolver = FxRateResolver(session, CountingFx({}), max_age_days=MAX_AGE)

    assert resolver.rate_for("USD", "EUR", FRIDAY) is None


@pytest.mark.integration
def test_a_fetched_rate_from_too_far_back_is_refused(session: Session) -> None:
    provider = CountingFx(rate_date=FRIDAY - timedelta(days=30))

    resolver = FxRateResolver(session, provider, max_age_days=MAX_AGE)

    assert resolver.rate_for("USD", "EUR", FRIDAY) is None
    assert FxRateRepository(session).count() == 0


@pytest.mark.integration
def test_a_rate_from_after_the_score_date_is_refused(session: Session) -> None:
    # Money from the future is not evidence about the past.
    provider = CountingFx(rate_date=FRIDAY + timedelta(days=3))

    resolver = FxRateResolver(session, provider, max_age_days=MAX_AGE)

    assert resolver.rate_for("USD", "EUR", FRIDAY) is None


# -- degradation ------------------------------------------------------------


@pytest.mark.integration
def test_an_fx_outage_yields_no_rate_rather_than_an_exception(session: Session) -> None:
    # Scanning, scoring and ranking must never depend on an FX vendor being
    # reachable. The company loses its currency-sensitive ratios; the run
    # continues.
    resolver = FxRateResolver(session, FailingFx(), max_age_days=MAX_AGE)

    assert resolver.rate_for("USD", "EUR", FRIDAY) is None


@pytest.mark.integration
def test_an_outage_still_serves_a_rate_already_stored(session: Session) -> None:
    FxRateRepository(session).save(
        FxConversion(base="USD", quote="EUR", rate=0.86453, rate_date=FRIDAY, provider="ecb")
    )
    session.flush()

    resolver = FxRateResolver(session, FailingFx(), max_age_days=MAX_AGE)
    rate = resolver.rate_for("USD", "EUR", FRIDAY)

    assert rate is not None
    assert rate.rate == pytest.approx(0.86453)


@pytest.mark.integration
def test_no_provider_configured_serves_only_what_is_stored(session: Session) -> None:
    resolver = FxRateResolver(session, None, max_age_days=MAX_AGE)

    assert resolver.rate_for("USD", "EUR", FRIDAY) is None


@pytest.mark.integration
def test_a_pair_no_source_publishes_is_remembered_as_absent(session: Session) -> None:
    # Asking again within a run must not re-request a pair that already came
    # back empty.
    provider = CountingFx({})
    resolver = FxRateResolver(session, provider, max_age_days=MAX_AGE)

    resolver.rate_for("USD", "XYZ", FRIDAY)
    resolver.rate_for("USD", "XYZ", FRIDAY)

    assert len(provider.calls) == 1


@pytest.mark.integration
def test_warming_reports_how_many_pairs_resolved(session: Session) -> None:
    provider = CountingFx({("USD", "EUR"): 0.86453})

    resolved = FxRateResolver(session, provider, max_age_days=MAX_AGE).warm(
        [("USD", "EUR"), ("USD", "TWD")], FRIDAY
    )

    assert resolved == 1
