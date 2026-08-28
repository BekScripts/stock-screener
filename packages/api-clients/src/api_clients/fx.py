"""Exchange rates, for putting a market capitalisation into a filer's currency.

The screener needs exactly one thing from foreign exchange: a rate on a stated
date, so that a company's dollar market capitalisation can be compared with a
balance sheet it did not file in dollars. That is the whole requirement. There is
no forecasting here, no intraday quotes, no consensus across vendors — those
would be a different product, and each is a way to spend money and complexity on
a question nobody asked.

Two properties do matter, and both are about honesty rather than precision:

**A rate belongs to a date.** Applying today's rate to a score computed three
months ago restates that score in money that did not exist yet, silently. So a
rate carries the date it is *for*, which on a weekend or a holiday is an earlier
day than the one requested — no market fixed a price on a Sunday, and a source
that returns one for a Sunday is interpolating.

**A rate carries its source.** The European Central Bank's daily reference rates
are an official fixing and cover thirty currencies. They do not cover every
currency a foreign private issuer reports in — Taiwan's dollar is absent, which
is exactly the currency TSM files in — so a second source fills the gaps, and
each stored rate records which one produced it. That is a documented fallback
order, not a consensus: one source answers, and the row says which.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from api_clients._http import RateLimiter, RetryPolicy, request_json
from api_clients.errors import ProviderError
from domain import FxConversion, normalise_currency

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from api_clients.base import FxProvider

ECB_PROVIDER_NAME = "ecb"
"""Frankfurter, serving the European Central Bank's daily reference rates."""

FALLBACK_PROVIDER_NAME = "currency-api"
"""A broad community dataset, used only for currencies the ECB does not publish."""

DEFAULT_ECB_URL = "https://api.frankfurter.dev/v1"
DEFAULT_FALLBACK_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api"

#: How far back a rate may be dated relative to the date asked for.
#:
#: Three days covers a weekend; five covers a weekend with a holiday on either
#: side, which is the longest ordinary gap in a published fixing series. Beyond
#: that the series has a real hole, and a rate from the far side of one is not
#: evidence about the date in question — a fortnight of currency movement is
#: larger than most of the valuation differences this system tries to detect.
DEFAULT_MAX_RATE_AGE_DAYS = 5


class EcbFxRates:
    """Daily reference rates published by the European Central Bank.

    An official fixing, free, and requiring no key. Asked for a weekend or a
    holiday it answers with the most recent business day and *says so* in the
    response, which is the behaviour that makes its dates trustworthy.

    Thirty currencies, which is most but not all of what a U.S.-listed foreign
    issuer might report in. `TWD` and `ARS` are the notable absences.

    Args:
        base_url: Host serving the rates.
        timeout_seconds: Per-request timeout.
        limiter: Request spacing.
        retry: Retry policy.
        client: HTTP client, injected by tests.
    """

    name = ECB_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_ECB_URL,
        timeout_seconds: float = 20.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self._limiter = limiter
        self._retry = retry or RetryPolicy()

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> EcbFxRates:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on exit."""
        self.close()

    def get_rate(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        """Return the rate converting `base` into `quote` on or before a date.

        Args:
            base: Currency to convert from.
            quote: Currency to convert to.
            as_of: The date wanted. The answer may be dated earlier.

        Returns:
            The conversion, or None when this source does not publish the pair.

        Raises:
            ProviderError: If the request failed.
        """
        base, quote = normalise_currency(base), normalise_currency(quote)
        if base == quote:
            return _identity(base, as_of, self.name)

        payload = request_json(
            self._client,
            "GET",
            f"{self._base_url}/{as_of.isoformat()}",
            provider=self.name,
            params={"base": base, "symbols": quote},
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            return None

        rates = payload.get("rates")
        rate = rates.get(quote) if isinstance(rates, dict) else None
        # The response names the business day it actually used, which is what
        # makes a Sunday request answerable without inventing a Sunday fixing.
        rate_date = _as_date(payload.get("date")) or as_of
        if not isinstance(rate, int | float) or isinstance(rate, bool) or rate <= 0:
            return None

        return FxConversion(
            base=base,
            quote=quote,
            rate=float(rate),
            rate_date=rate_date,
            provider=self.name,
            retrieved_at=datetime.now(UTC),
        )


class BroadFxRates:
    """A wide daily dataset, for the currencies the ECB does not publish.

    Three hundred and forty currencies against a USD base, free and keyless,
    with a file per date. It is a community-maintained aggregate rather than an
    official fixing, which is why it is second in the chain rather than first —
    but for a company reporting in Taiwan dollars it is the difference between a
    valuation and no valuation at all.

    Unlike the ECB source this publishes a figure for weekends, so the date it
    reports is the date asked for rather than the last business day. The
    resolution rule above it treats both the same way: whatever date comes back
    is recorded as the date used.

    Args:
        base_url: Host serving the dataset.
        timeout_seconds: Per-request timeout.
        limiter: Request spacing.
        retry: Retry policy.
        client: HTTP client, injected by tests.
    """

    name = FALLBACK_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_FALLBACK_URL,
        timeout_seconds: float = 20.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self._limiter = limiter
        self._retry = retry or RetryPolicy()

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> BroadFxRates:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on exit."""
        self.close()

    def get_rate(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        """Return the rate converting `base` into `quote` on a date.

        Args:
            base: Currency to convert from.
            quote: Currency to convert to.
            as_of: The date wanted.

        Returns:
            The conversion, or None when the dataset has no such pair.

        Raises:
            ProviderError: If the request failed.
        """
        base, quote = normalise_currency(base), normalise_currency(quote)
        if base == quote:
            return _identity(base, as_of, self.name)

        payload = request_json(
            self._client,
            "GET",
            f"{self._base_url}@{as_of.isoformat()}/v1/currencies/{base.lower()}.json",
            provider=self.name,
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            return None

        rates = payload.get(base.lower())
        rate = rates.get(quote.lower()) if isinstance(rates, dict) else None
        rate_date = _as_date(payload.get("date")) or as_of
        if not isinstance(rate, int | float) or isinstance(rate, bool) or rate <= 0:
            return None

        return FxConversion(
            base=base,
            quote=quote,
            rate=float(rate),
            rate_date=rate_date,
            provider=self.name,
            retrieved_at=datetime.now(UTC),
        )


class CompositeFxRates:
    """Sources tried in order, first answer wins.

    Not a consensus and not an average. The ordering is a statement about
    authority: an official fixing answers where it publishes the pair, and a
    broader dataset answers only where it does not. Every rate records which
    source produced it, so a reader can always tell which kind they are looking
    at rather than having to trust that the blend was sensible.

    A source that raises is skipped rather than ending the resolution, because a
    currency the first source never covered should not be lost to that source
    being briefly unreachable.

    Args:
        sources: Rate sources, most authoritative first.
    """

    name = "composite"

    def __init__(self, sources: Sequence[FxProvider]) -> None:
        self._sources = tuple(sources)

    def close(self) -> None:
        """Close every underlying source that owns a connection."""
        for source in self._sources:
            closer = getattr(source, "close", None)
            if callable(closer):
                closer()

    def __enter__(self) -> CompositeFxRates:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the sources on exit."""
        self.close()

    def get_rate(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        """Return the first rate any source can supply for the pair.

        Args:
            base: Currency to convert from.
            quote: Currency to convert to.
            as_of: The date wanted.

        Returns:
            The conversion, or None when no source publishes the pair.
        """
        for source in self._sources:
            try:
                rate = source.get_rate(base, quote, as_of)
            except ProviderError as exc:
                log.warning(
                    "fx source failed, trying the next",
                    source=getattr(source, "name", type(source).__name__),
                    base=base,
                    quote=quote,
                    error=str(exc),
                )
                continue
            if rate is not None:
                return rate
        return None


def _identity(currency: str, as_of: date, provider: str) -> FxConversion:
    """Return the one rate that needs no source: a currency against itself."""
    return FxConversion(
        base=currency,
        quote=currency,
        rate=1.0,
        rate_date=as_of,
        provider=provider,
        retrieved_at=datetime.now(UTC),
    )


def _as_date(value: object) -> date | None:
    """Parse an ISO date, returning None when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
