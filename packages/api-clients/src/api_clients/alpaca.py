"""Alpaca adapter: the tradable universe and daily OHLCV bars.

Two Alpaca services are involved and they live on different hosts. Assets come
from the trading API (`api.alpaca.markets`), bars from the market-data API
(`data.alpaca.markets`), so the adapter holds two clients.

Alpaca supplies no fundamentals — no revenue, no market capitalisation, not even
a sector. Profiles returned here therefore carry identity and venue only, and the
missing fields stay None for the fundamentals provider to fill. That is a real
coverage gap, not a bug in this adapter.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from api_clients._http import RateLimiter, RetryPolicy, request_json
from api_clients.errors import ProviderDataError
from domain import CompanyProfile, PriceBar, normalise_ticker

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

PROVIDER_NAME = "alpaca"

DEFAULT_TRADING_BASE_URL = "https://api.alpaca.markets"
DEFAULT_DATA_BASE_URL = "https://data.alpaca.markets"

#: Alpaca accepts a comma-separated symbol list on the bars endpoint. The
#: documented ceiling is higher, but long URLs get truncated by proxies, and a
#: failed batch costs more than an extra request.
DEFAULT_BATCH_SIZE = 100

_DAILY_TIMEFRAME = "1Day"

#: `all` applies both split and dividend adjustments, which is what a return
#: calculation needs. Raw closes would show a 2-for-1 split as a 50% loss.
_ADJUSTMENT = "all"

DEFAULT_FEED = "iex"
"""Which tape to read.

`sip` is the consolidated tape — every U.S. exchange — and is the correct source
for both price and volume.

`iex` is a single exchange and is what a free account reads in real time. **Its
prices are sound but its volume is not comparable**: measured against a vendor's
consolidated figure across 259 companies, IEX carried between 0.006% and 12% of
it, a spread of two thousand times. A dollar-volume threshold calibrated for the
whole market therefore cannot be applied to an IEX figure at all — not even
scaled, because the scale factor is not a constant.

**A free account can still read SIP historically.** The refusal is about
recency, not about the tape: a request whose `end` is at least
`SIP_MIN_DELAY_MINUTES` in the past is served, and one inside that window returns
`403 subscription does not permit querying recent SIP data`. That is enough for
an eligibility screen, which asks what a company's average volume has been rather
than what it is this second — see `get_average_volume`.
"""

CONSOLIDATED_FEED = "sip"
"""The consolidated tape, and the only feed a liquidity threshold may read."""

#: RFC3339, which is what the recency check parses. A bare date is not this.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SIP_MIN_DELAY_MINUTES = 15
"""How far in the past a SIP query must end to be served without a paid plan.

Measured against the live account rather than taken from documentation: 5 and 10
minutes are refused, 15 and 20 are served.

**`end` must be a timestamp, not a date.** A bare `YYYY-MM-DD` is read as the end
of that day, which is in the future for today and is refused however old the rest
of the range is — the failure this constant exists to avoid.
"""


class AlpacaMarketData:
    """Market data from Alpaca's trading and data APIs.

    Args:
        api_key: Alpaca API key ID.
        secret_key: Alpaca API secret.
        trading_base_url: Host serving `/v2/assets`.
        data_base_url: Host serving `/v2/stocks/bars`.
        timeout_seconds: Per-request timeout.
        limiter: Minimum spacing between requests. One is created if omitted.
        retry: Retry policy. Defaults apply if omitted.
        batch_size: Symbols per bars request.
        feed: Which tape to read — `sip` (consolidated, paid) or `iex` (single
            exchange, free). See `DEFAULT_FEED`; the choice materially changes
            reported volume.
        client: Trading-API client, injected by tests.
        data_client: Market-data client, injected by tests.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        trading_base_url: str = DEFAULT_TRADING_BASE_URL,
        data_base_url: str = DEFAULT_DATA_BASE_URL,
        timeout_seconds: float = 30.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        feed: str = DEFAULT_FEED,
        client: httpx.Client | None = None,
        data_client: httpx.Client | None = None,
    ) -> None:
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "accept": "application/json",
        }
        self._client = client or httpx.Client(
            base_url=trading_base_url, headers=headers, timeout=timeout_seconds
        )
        self._data_client = data_client or httpx.Client(
            base_url=data_base_url, headers=headers, timeout=timeout_seconds
        )
        self._limiter = limiter or RateLimiter(0.0)
        self._retry = retry or RetryPolicy()
        self._batch_size = max(1, batch_size)
        self._feed = feed

    def close(self) -> None:
        """Release both underlying HTTP connections."""
        self._client.close()
        self._data_client.close()

    def __enter__(self) -> AlpacaMarketData:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close both clients on exit."""
        self.close()

    # -- MarketDataProvider ------------------------------------------------

    def get_stock_universe(self) -> list[CompanyProfile]:
        """Return every active, tradable U.S. equity Alpaca lists.

        Returns:
            One profile per listing, with fundamentals fields left None.

        Raises:
            ProviderError: If the request failed or the payload was unusable.
        """
        payload = request_json(
            self._client,
            "GET",
            "/v2/assets",
            provider=PROVIDER_NAME,
            params={"status": "active", "asset_class": "us_equity"},
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, list):
            raise ProviderDataError("alpaca returned a non-list asset payload")

        return [self._parse_asset(item) for item in payload if isinstance(item, dict)]

    def get_daily_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        """Return daily bars for one ticker over an inclusive date range.

        Args:
            ticker: The symbol to fetch.
            start: First session to include.
            end: Last session to include.

        Returns:
            Bars ordered oldest first; empty when Alpaca has no data.

        Raises:
            ProviderError: If the request failed.
        """
        # Alpaca keys its response by the canonical upper-case symbol, so a
        # caller passing "aapl" would otherwise silently receive no bars.
        symbol = normalise_ticker(ticker)
        return self.get_daily_prices_batch([symbol], start, end).get(symbol, [])

    def get_daily_prices_batch(
        self, tickers: Sequence[str], start: date, end: date
    ) -> dict[str, list[PriceBar]]:
        """Return daily bars for several tickers, batched and paginated.

        Args:
            tickers: Symbols to fetch.
            start: First session to include.
            end: Last session to include.

        Returns:
            A mapping of ticker to bars ordered oldest first. Symbols Alpaca
            returned nothing for are absent.

        Raises:
            ProviderError: If a request failed.
        """
        results: dict[str, list[PriceBar]] = {}
        for batch in _chunked(list(tickers), self._batch_size):
            self._collect_batch(batch, start.isoformat(), end.isoformat(), results, feed=self._feed)

        # Sorted once at the end rather than per batch: pages arrive in no
        # guaranteed order, but re-sorting every accumulated symbol after each
        # batch would be quadratic across a full-market fetch.
        for bars in results.values():
            bars.sort(key=lambda bar: bar.date)
        return results

    def get_average_volume(
        self,
        tickers: Sequence[str],
        *,
        window: int = 20,
        as_of: datetime | None = None,
        delay_minutes: int = SIP_MIN_DELAY_MINUTES,
    ) -> dict[str, float]:
        """Return average daily share volume from the consolidated tape.

        Deliberately narrow. It exists so the eligibility screen can apply a
        threshold calibrated for the whole market to a figure that describes the
        whole market — nothing else here reads this feed, and no bar fetched by
        this method is stored as price history. Mixing two feeds' volume in one
        table is the failure it is built to avoid.

        The query ends `delay_minutes` in the past because that is what a free
        plan permits on SIP, and it ends on a *timestamp* because a bare date is
        read as end-of-day and refused.

        Args:
            tickers: Symbols to fetch.
            window: Sessions to average over. The eligibility threshold is a
                twenty-day average, which is the default.
            as_of: Moment to measure back from. Defaults to now.
            delay_minutes: How far before `as_of` the query ends.

        Returns:
            Average share volume by ticker, over however many of the last
            `window` sessions Alpaca returned. Symbols it had no bars for are
            absent rather than zero — an unknown volume is not a quiet one.

        Raises:
            ProviderError: If a request failed, including the `403` a plan
                without SIP access returns for too recent a window.
        """
        end = (as_of or datetime.now(UTC)) - timedelta(minutes=delay_minutes)
        # Calendar days, not sessions: weekends and holidays are not bars, so a
        # window of twenty sessions needs appreciably more than twenty days of
        # range. Over-fetching costs nothing — the tail is trimmed below.
        start = end - timedelta(days=window * 2 + 15)

        results: dict[str, list[PriceBar]] = {}
        for batch in _chunked(list(tickers), self._batch_size):
            self._collect_batch(
                batch,
                start.strftime(_TIMESTAMP_FORMAT),
                end.strftime(_TIMESTAMP_FORMAT),
                results,
                feed=CONSOLIDATED_FEED,
            )

        averages: dict[str, float] = {}
        for symbol, bars in results.items():
            if not bars:
                continue
            recent = sorted(bars, key=lambda bar: bar.date)[-window:]
            averages[symbol] = sum(bar.volume for bar in recent) / len(recent)
        return averages

    # -- internals ---------------------------------------------------------

    def _collect_batch(
        self,
        batch: list[str],
        start: str,
        end: str,
        results: dict[str, list[PriceBar]],
        *,
        feed: str,
    ) -> None:
        """Fetch one batch, following `next_page_token` until it is exhausted.

        `start` and `end` arrive already formatted because the two callers format
        them differently: price history wants whole sessions and passes dates,
        while the eligibility read must pass a timestamp to stay outside the SIP
        recency window.
        """
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(batch),
                "timeframe": _DAILY_TIMEFRAME,
                "start": start,
                "end": end,
                "adjustment": _ADJUSTMENT,
                "feed": feed,
                "limit": 10_000,
            }
            if page_token:
                params["page_token"] = page_token

            payload = request_json(
                self._data_client,
                "GET",
                "/v2/stocks/bars",
                provider=PROVIDER_NAME,
                params=params,
                limiter=self._limiter,
                retry=self._retry,
            )
            if not isinstance(payload, dict):
                raise ProviderDataError("alpaca returned a non-object bars payload")

            for symbol, raw_bars in (payload.get("bars") or {}).items():
                results.setdefault(symbol, []).extend(
                    self._parse_bar(raw) for raw in raw_bars if isinstance(raw, dict)
                )

            page_token = payload.get("next_page_token")
            if not page_token:
                break

    @staticmethod
    def _parse_asset(raw: dict[str, Any]) -> CompanyProfile:
        """Convert one `/v2/assets` entry into a domain profile.

        Alpaca reports NYSE American as `AMEX`; the exchange code is passed
        through unchanged and interpreted by `domain.universe`.
        """
        symbol = normalise_ticker(str(raw.get("symbol", "")))
        if not symbol:
            raise ProviderDataError("alpaca returned an asset without a symbol")

        return CompanyProfile(
            ticker=symbol,
            name=str(raw.get("name") or symbol),
            exchange=_optional_str(raw.get("exchange")),
            is_active=raw.get("status") == "active" and bool(raw.get("tradable", False)),
        )

    @staticmethod
    def _parse_bar(raw: dict[str, Any]) -> PriceBar:
        """Convert one bar into a domain `PriceBar`.

        Alpaca's single-letter keys are its own compression scheme and stop here:
        `o` becomes `open`, `t` becomes a date. Nothing downstream ever sees `o`.
        """
        timestamp = raw.get("t")
        if not isinstance(timestamp, str):
            raise ProviderDataError("alpaca returned a bar without a timestamp")

        try:
            session = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
            return PriceBar(
                date=session,
                open=float(raw["o"]),
                high=float(raw["h"]),
                low=float(raw["l"]),
                close=float(raw["c"]),
                volume=float(raw["v"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderDataError(f"alpaca returned an unparseable bar: {exc}") from exc


def _optional_str(value: object) -> str | None:
    """Return a stripped string, or None when the value is absent or blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _chunked(items: list[str], size: int) -> Iterator[list[str]]:
    """Yield successive slices of `items` no longer than `size`."""
    for index in range(0, len(items), size):
        yield items[index : index + size]
