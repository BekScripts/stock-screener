"""Exchange-rate adapters.

`unit`: every request goes through `httpx.MockTransport`. Nothing here reaches a
live FX endpoint, because a test whose result depends on today's euro is a test
that fails on a Tuesday for reasons nobody can reproduce.

The cases that matter are the ones where an adapter is tempted to answer anyway:
a pair the source does not publish, a weekend with no fixing, and a source that
is briefly unreachable.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from api_clients import BroadFxRates, CompositeFxRates, EcbFxRates, MockFxRates, ProviderError

FRIDAY = date(2026, 8, 14)
SUNDAY = date(2026, 8, 16)


def ecb(handler: Any) -> EcbFxRates:
    """An ECB adapter served by one canned handler."""
    return EcbFxRates(client=httpx.Client(transport=httpx.MockTransport(handler)))


def broad(handler: Any) -> BroadFxRates:
    """A broad-dataset adapter served by one canned handler."""
    return BroadFxRates(client=httpx.Client(transport=httpx.MockTransport(handler)))


def json_handler(payload: Any, status: int = 200) -> Any:
    """Return a handler answering every request with one payload."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# -- the ECB source ---------------------------------------------------------


@pytest.mark.unit
def test_reads_a_rate_and_the_date_it_belongs_to() -> None:
    provider = ecb(json_handler({"base": "USD", "date": "2026-08-14", "rates": {"EUR": 0.86453}}))

    rate = provider.get_rate("USD", "EUR", FRIDAY)

    assert rate is not None
    assert rate.rate == pytest.approx(0.86453)
    assert rate.rate_date == FRIDAY
    assert rate.provider == "ecb"


@pytest.mark.unit
def test_a_weekend_resolves_to_the_business_day_that_fixed_it() -> None:
    # No market fixed a price on Sunday. The source answers with Friday and says
    # so, and the date recorded is Friday's — a rate stamped Sunday would claim
    # a fixing that never happened.
    provider = ecb(json_handler({"base": "USD", "date": "2026-08-14", "rates": {"EUR": 0.86453}}))

    rate = provider.get_rate("USD", "EUR", SUNDAY)

    assert rate is not None
    assert rate.rate_date == FRIDAY


@pytest.mark.unit
def test_a_currency_the_ecb_does_not_publish_returns_nothing() -> None:
    # Taiwan's dollar is absent from the ECB's thirty reference rates, which is
    # exactly the currency TSM files in. None is an answer, not a failure.
    provider = ecb(json_handler({"base": "USD", "date": "2026-08-14", "rates": {}}))

    assert provider.get_rate("USD", "TWD", FRIDAY) is None


@pytest.mark.unit
def test_a_currency_against_itself_needs_no_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made for an identity rate")

    rate = ecb(handler).get_rate("USD", "usd", FRIDAY)

    assert rate is not None
    assert rate.rate == 1.0


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, -1.5, "0.86", None, True])
def test_an_unusable_rate_is_refused(value: object) -> None:
    # A zero or negative rate would divide a valuation by nothing, and a string
    # would raise somewhere far from here.
    provider = ecb(json_handler({"date": "2026-08-14", "rates": {"EUR": value}}))

    assert provider.get_rate("USD", "EUR", FRIDAY) is None


# -- the broad source -------------------------------------------------------


@pytest.mark.unit
def test_the_broad_source_reads_its_own_shape() -> None:
    provider = broad(json_handler({"date": "2026-08-14", "usd": {"twd": 31.99226}}))

    rate = provider.get_rate("USD", "TWD", FRIDAY)

    assert rate is not None
    assert rate.rate == pytest.approx(31.99226)
    assert rate.provider == "currency-api"


@pytest.mark.unit
def test_the_broad_source_returns_nothing_for_an_unknown_pair() -> None:
    provider = broad(json_handler({"date": "2026-08-14", "usd": {"eur": 0.86}}))

    assert provider.get_rate("USD", "XYZ", FRIDAY) is None


# -- ordering between sources ----------------------------------------------


@pytest.mark.unit
def test_the_first_source_answers_where_it_publishes_the_pair() -> None:
    primary = MockFxRates({("USD", "EUR"): 0.86453})
    secondary = MockFxRates({("USD", "EUR"): 0.99})

    rate = CompositeFxRates((primary, secondary)).get_rate("USD", "EUR", FRIDAY)

    assert rate is not None
    assert rate.rate == pytest.approx(0.86453)


@pytest.mark.unit
def test_the_second_source_answers_only_where_the_first_cannot() -> None:
    # The whole reason the fallback exists: TSM reports in a currency the
    # official fixing does not cover.
    primary = MockFxRates({("USD", "EUR"): 0.86453})
    secondary = MockFxRates({("USD", "TWD"): 31.99226})

    rate = CompositeFxRates((primary, secondary)).get_rate("USD", "TWD", FRIDAY)

    assert rate is not None
    assert rate.rate == pytest.approx(31.99226)


@pytest.mark.unit
def test_a_source_that_fails_is_skipped_rather_than_ending_the_lookup() -> None:
    # A currency the first source never covered should not be lost to that
    # source being briefly unreachable.
    class Failing:
        name = "failing"

        def get_rate(self, base: str, quote: str, as_of: date) -> None:
            raise ProviderError("unreachable")

    rate = CompositeFxRates((Failing(), MockFxRates())).get_rate("USD", "TWD", FRIDAY)

    assert rate is not None
    assert rate.rate == pytest.approx(31.99226)


@pytest.mark.unit
def test_no_source_publishing_the_pair_returns_nothing() -> None:
    composite = CompositeFxRates((MockFxRates({}), MockFxRates({})))

    assert composite.get_rate("USD", "TWD", FRIDAY) is None


# -- conversion arithmetic --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("quote", "rate", "expected"),
    [
        ("EUR", 0.86453, 864_530.0),
        ("DKK", 6.463, 6_463_000.0),
        ("TWD", 31.99226, 31_992_260.0),
    ],
)
def test_a_conversion_multiplies_into_the_reporting_currency(
    quote: str, rate: float, expected: float
) -> None:
    conversion = MockFxRates({("USD", quote): rate}).get_rate("USD", quote, FRIDAY)

    assert conversion is not None
    assert conversion.convert(1_000_000.0) == pytest.approx(expected)


@pytest.mark.unit
def test_converting_a_missing_amount_stays_missing() -> None:
    conversion = MockFxRates().get_rate("USD", "EUR", FRIDAY)

    assert conversion is not None
    assert conversion.convert(None) is None
