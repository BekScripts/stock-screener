"""Alpaca adapter tests.

Every test drives the real adapter through `httpx.MockTransport`, so the request
building, pagination and normalisation code all execute — only the socket is
replaced. No test reaches the network; there are no credentials involved beyond
the placeholder strings below.

The recurring assertion is that Alpaca's vocabulary stops at this boundary: the
adapter returns `PriceBar.close`, never `"c"`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from api_clients import AlpacaMarketData, ProviderAuthError, ProviderDataError, ProviderError
from api_clients.alpaca import SIP_MIN_DELAY_MINUTES

START = date(2026, 1, 2)
END = date(2026, 1, 6)


def _adapter(handler: Any) -> AlpacaMarketData:
    """Build an adapter whose clients are backed by a mock transport."""
    transport = httpx.MockTransport(handler)
    return AlpacaMarketData(
        api_key="test-key",
        secret_key="test-secret",  # noqa: S106 — a literal for the mock transport
        client=httpx.Client(transport=transport, base_url="https://api.test"),
        data_client=httpx.Client(transport=transport, base_url="https://data.test"),
    )


def _bar(day: str, close: float) -> dict[str, Any]:
    """Build one raw Alpaca bar in its single-letter wire format."""
    return {
        "t": f"{day}T05:00:00Z",
        "o": close - 1,
        "h": close + 1,
        "l": close - 2,
        "c": close,
        "v": 1_000_000,
    }


@pytest.mark.unit
def test_universe_normalises_assets_into_domain_profiles() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "aapl",
                    "name": "Apple Inc. Common Stock",
                    "exchange": "NASDAQ",
                    "status": "active",
                    "tradable": True,
                }
            ],
        )

    profiles = _adapter(handler).get_stock_universe()

    assert len(profiles) == 1
    assert profiles[0].ticker == "AAPL"
    assert profiles[0].exchange == "NASDAQ"
    assert profiles[0].is_active is True


@pytest.mark.unit
def test_universe_leaves_fundamentals_fields_unset() -> None:
    # Alpaca has no sector or market cap. Leaving them None lets the
    # fundamentals provider fill them; a zero would look like a real datum.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "exchange": "NASDAQ",
                    "status": "active",
                    "tradable": True,
                }
            ],
        )

    profile = _adapter(handler).get_stock_universe()[0]

    assert profile.market_cap is None
    assert profile.sector is None
    assert profile.industry is None


@pytest.mark.unit
def test_an_untradable_asset_is_marked_inactive() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "HALT",
                    "name": "Halted Inc",
                    "exchange": "NYSE",
                    "status": "active",
                    "tradable": False,
                }
            ],
        )

    assert _adapter(handler).get_stock_universe()[0].is_active is False


@pytest.mark.unit
def test_bars_are_normalised_out_of_alpacas_single_letter_keys() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {"XYZ": [_bar("2026-01-02", 10.5)]}})

    bars = _adapter(handler).get_daily_prices("XYZ", START, END)

    assert len(bars) == 1
    assert bars[0].date == date(2026, 1, 2)
    assert bars[0].close == pytest.approx(10.5)
    assert bars[0].high == pytest.approx(11.5)
    assert bars[0].volume == pytest.approx(1_000_000)


@pytest.mark.unit
def test_the_request_asks_for_adjusted_daily_bars() -> None:
    # Unadjusted closes make a 2-for-1 split look like a 50% crash, which would
    # poison every return and 52-week figure calculated downstream.
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"bars": {}})

    _adapter(handler).get_daily_prices("XYZ", START, END)

    assert seen["adjustment"] == "all"
    assert seen["timeframe"] == "1Day"
    assert seen["start"] == "2026-01-02"


@pytest.mark.unit
def test_batching_sends_one_request_for_several_symbols() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["symbols"])
        return httpx.Response(
            200,
            json={"bars": {"AAA": [_bar("2026-01-02", 5.0)], "BBB": [_bar("2026-01-02", 7.0)]}},
        )

    result = _adapter(handler).get_daily_prices_batch(["AAA", "BBB"], START, END)

    assert calls == ["AAA,BBB"]
    assert set(result) == {"AAA", "BBB"}


@pytest.mark.unit
def test_pagination_follows_the_next_page_token_until_it_is_absent() -> None:
    pages = [
        {"bars": {"XYZ": [_bar("2026-01-02", 10.0)]}, "next_page_token": "page-2"},
        {"bars": {"XYZ": [_bar("2026-01-05", 12.0)]}, "next_page_token": None},
    ]
    seen_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.url.params.get("page_token"))
        return httpx.Response(200, json=pages[len(seen_tokens) - 1])

    bars = _adapter(handler).get_daily_prices("XYZ", START, END)

    assert seen_tokens == [None, "page-2"]
    assert [bar.date for bar in bars] == [date(2026, 1, 2), date(2026, 1, 5)]


@pytest.mark.unit
def test_bars_come_back_in_date_order_even_when_pages_do_not() -> None:
    pages = [
        {"bars": {"XYZ": [_bar("2026-01-06", 12.0)]}, "next_page_token": "page-2"},
        {"bars": {"XYZ": [_bar("2026-01-02", 10.0)]}, "next_page_token": None},
    ]
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, json=pages[calls["n"]])
        calls["n"] += 1
        return response

    bars = _adapter(handler).get_daily_prices("XYZ", START, END)

    assert [bar.date for bar in bars] == [date(2026, 1, 2), date(2026, 1, 6)]


@pytest.mark.unit
def test_a_symbol_with_no_data_is_absent_rather_than_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {"AAA": [_bar("2026-01-02", 5.0)]}})

    result = _adapter(handler).get_daily_prices_batch(["AAA", "GONE"], START, END)

    assert "GONE" not in result


@pytest.mark.unit
def test_rejected_credentials_raise_an_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(ProviderAuthError, match="rejected the credentials"):
        _adapter(handler).get_stock_universe()


@pytest.mark.unit
def test_an_auth_error_never_echoes_the_credentials() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad key test-secret"})

    with pytest.raises(ProviderAuthError) as exc_info:
        _adapter(handler).get_stock_universe()

    assert "test-secret" not in str(exc_info.value)
    assert "test-key" not in str(exc_info.value)


@pytest.mark.unit
def test_a_malformed_bar_raises_rather_than_producing_a_zeroed_price() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {"XYZ": [{"t": "2026-01-02T05:00:00Z"}]}})

    with pytest.raises(ProviderDataError, match="unparseable bar"):
        _adapter(handler).get_daily_prices("XYZ", START, END)


@pytest.mark.unit
def test_an_unexpected_payload_shape_raises_a_provider_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "object"})

    with pytest.raises(ProviderError, match="non-list asset payload"):
        _adapter(handler).get_stock_universe()


# -- audit regressions -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("requested", ["xyz", " XYZ ", "Xyz"])
def test_a_ticker_in_any_casing_finds_its_bars(requested: str) -> None:
    # Alpaca keys the response by canonical upper-case symbol. Looking it up
    # with the caller's spelling silently returned zero bars, which reads
    # downstream as "this company has no price history" rather than as an error.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {"XYZ": [_bar("2026-01-02", 10.5)]}})

    assert len(_adapter(handler).get_daily_prices(requested, START, END)) == 1


@pytest.mark.unit
def test_an_asset_symbol_is_normalised() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": " xyz ",
                    "name": "Example",
                    "exchange": "NASDAQ",
                    "status": "active",
                    "tradable": True,
                }
            ],
        )

    assert _adapter(handler).get_stock_universe()[0].ticker == "XYZ"


@pytest.mark.unit
def test_the_feed_is_sent_and_defaults_to_iex() -> None:
    # Omitting `feed` makes Alpaca default to the consolidated SIP tape, which a
    # free data subscription rejects with 403. Sending it explicitly is what
    # makes the adapter work on a free account at all.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"bars": {}})

    _adapter(handler).get_daily_prices("XYZ", START, END)

    assert seen["feed"] == "iex"


@pytest.mark.unit
def test_the_feed_can_be_set_to_sip() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"bars": {}})

    transport = httpx.MockTransport(handler)
    adapter = AlpacaMarketData(
        api_key="test-key",
        secret_key="test-secret",  # noqa: S106 — a literal for the mock transport
        feed="sip",
        client=httpx.Client(transport=transport, base_url="https://api.test"),
        data_client=httpx.Client(transport=transport, base_url="https://data.test"),
    )
    adapter.get_daily_prices("XYZ", START, END)

    assert seen["feed"] == "sip"


@pytest.mark.unit
def test_the_eligibility_read_always_uses_the_consolidated_tape() -> None:
    # Independent of the configured feed. Price history may come from a single
    # exchange, but a liquidity threshold calibrated for the whole market can
    # only be applied to the whole market's volume.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"bars": {}})

    adapter = _adapter(handler)  # built with the default iex feed
    adapter.get_average_volume(["XYZ"])

    assert seen["feed"] == "sip"


@pytest.mark.unit
def test_the_eligibility_read_ends_outside_the_recency_window() -> None:
    # A free plan serves SIP historically and refuses it for recent data, so the
    # query must end in the past — and `end` must be a timestamp, because a bare
    # date is read as end-of-day and refused however old the rest of the range is.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"bars": {}})

    as_of = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
    _adapter(handler).get_average_volume(["XYZ"], as_of=as_of, delay_minutes=15)

    ended = datetime.strptime(seen["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert ended == as_of - timedelta(minutes=15)
    assert as_of - ended >= timedelta(minutes=SIP_MIN_DELAY_MINUTES)


@pytest.mark.unit
def test_the_eligibility_read_averages_only_the_requested_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": {
                    "XYZ": [
                        _bar("2026-08-03", 10.0) | {"v": 999_999_999},
                        _bar("2026-08-04", 10.0) | {"v": 100},
                        _bar("2026-08-05", 10.0) | {"v": 300},
                    ]
                }
            },
        )

    averages = _adapter(handler).get_average_volume(["XYZ"], window=2)

    # The oldest bar is outside the window and must not drag the average.
    assert averages["XYZ"] == pytest.approx(200.0)


@pytest.mark.unit
def test_a_symbol_the_tape_has_no_bars_for_is_absent_rather_than_zero() -> None:
    # An unknown volume is not a volume of zero: absent leaves the company to the
    # vendor figure later, whereas zero would fail it out of the screen outright.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {"XYZ": [_bar("2026-08-05", 10.0)]}})

    averages = _adapter(handler).get_average_volume(["XYZ", "NONE"])

    assert "NONE" not in averages
    assert averages["XYZ"] == pytest.approx(1_000_000.0)
