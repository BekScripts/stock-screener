"""Shared request machinery: rate limiting, retries, and error translation.

Both adapters talk to metered APIs that will happily throttle or ban a client
that hammers them. This module is the one place that decides how hard to push, so
the policy is consistent across vendors and testable without a network.

Everything time-related is injected. `RateLimiter` takes a clock and a sleep
function, and `request_with_retry` takes a sleep function, so the tests exercise
real backoff logic in microseconds instead of actually waiting.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from api_clients.errors import (
    ProviderAuthError,
    ProviderDataError,
    ProviderError,
    ProviderPlanError,
    ProviderRateLimitError,
    ProviderRequestError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_TOO_MANY_REQUESTS = 429
_PAYMENT_REQUIRED = 402
_SERVER_ERROR_FLOOR = 500
_AUTH_STATUSES = frozenset({401, 403})

#: How much of a provider's error body to quote back. Enough to carry the
#: explanation ("limit must be between 0 and 5"), short enough that a body
#: echoing the query string cannot spill a whole credential into a log.
_ERROR_BODY_CHARS = 200


class RateLimiter:
    """Enforces a minimum interval between requests.

    A deliberately simple approach: no token bucket, no concurrency, just a floor
    on the gap between calls. Phase 1 ingestion is a single-threaded nightly job,
    and a bucket would be machinery in search of a problem.

    Args:
        min_interval_seconds: Smallest permitted gap between requests. Zero
            disables limiting entirely.
        clock: Monotonic time source, injected for testing.
        sleep: Blocking sleep, injected for testing.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, min_interval_seconds)
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None

    def wait(self) -> None:
        """Block until enough time has passed since the previous request."""
        if self._min_interval == 0:
            return

        now = self._clock()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_at = now


class RetryPolicy:
    """How many times to retry a transient failure, and how long to wait.

    Args:
        attempts: Total tries including the first. One disables retrying.
        backoff_seconds: Base delay, doubled after each failed attempt.
        sleep: Blocking sleep, injected for testing.
    """

    def __init__(
        self,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.attempts = max(1, attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self._sleep = sleep

    def pause(self, attempt: int) -> None:
        """Sleep before retry number `attempt`, counting from one."""
        if self.backoff_seconds:
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))


def _is_retryable(status_code: int) -> bool:
    """Return whether a status code is worth trying again."""
    return status_code == _TOO_MANY_REQUESTS or status_code >= _SERVER_ERROR_FLOOR


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    """Translate an unsuccessful response into the matching provider error.

    The response body is never included in the message: it can echo query
    parameters, and for these vendors the API key travels in the query string.
    """
    status = response.status_code
    if status in _AUTH_STATUSES:
        raise ProviderAuthError(f"{provider} rejected the credentials (HTTP {status})")
    if status == _PAYMENT_REQUIRED:
        raise ProviderPlanError(f"{provider} plan does not allow this request: {_hint(response)}")
    if status == _TOO_MANY_REQUESTS:
        raise ProviderRateLimitError(f"{provider} rate limit reached (HTTP {status})")
    if status >= httpx.codes.BAD_REQUEST:
        raise ProviderRequestError(f"{provider} request failed with HTTP {status}")


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    params: Mapping[str, Any] | None = None,
    json: Mapping[str, Any] | None = None,
    limiter: RateLimiter | None = None,
    retry: RetryPolicy | None = None,
) -> Any:
    """Perform one request and return its decoded JSON body.

    Retries rate limits, server errors and transport failures with exponential
    backoff. Authentication failures are not retried, because a rejected key
    stays rejected and hammering the endpoint invites a longer ban.

    Args:
        client: The HTTP client to use. Headers and base URL come from it.
        method: HTTP verb.
        url: Absolute or client-relative URL.
        provider: Name used in error messages.
        params: Query parameters.
        json: Request body, encoded as JSON. For vendors whose search endpoint
            is a POST — the same retry and error policy applies either way.
        limiter: Applied before every attempt, including retries.
        retry: Retry policy. Defaults to three attempts with one-second backoff.

    Returns:
        The decoded JSON body.

    Raises:
        ProviderAuthError: If the credentials were rejected.
        ProviderRateLimitError: If rate limited on the final attempt.
        ProviderRequestError: If the request failed on the final attempt.
        ProviderDataError: If the body was not valid JSON.
    """
    policy = retry or RetryPolicy()
    last_error: ProviderError = ProviderRequestError(f"{provider} request was never attempted")

    for attempt in range(1, policy.attempts + 1):
        if limiter is not None:
            limiter.wait()

        try:
            response = client.request(method, url, params=params, json=json)
        except httpx.HTTPError as exc:
            last_error = ProviderRequestError(f"{provider} request failed: {type(exc).__name__}")
        else:
            if response.status_code < httpx.codes.BAD_REQUEST:
                return _decode(response, provider)
            if not _is_retryable(response.status_code):
                _raise_for_status(response, provider)
            last_error = _status_error(response.status_code, provider)

        if attempt < policy.attempts:
            policy.pause(attempt)

    raise last_error


def request_text(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    limiter: RateLimiter | None = None,
    retry: RetryPolicy | None = None,
) -> str:
    """Perform one request and return its body as text.

    The same policy as `request_json`, for endpoints that serve documents rather
    than data: SEC filings are HTML, and decoding them as JSON would turn every
    successful fetch into a data error.

    Args:
        client: The HTTP client to use. Headers and base URL come from it.
        method: HTTP verb.
        url: Absolute or client-relative URL.
        provider: Name used in error messages.
        limiter: Applied before every attempt, including retries.
        retry: Retry policy. Defaults to three attempts with one-second backoff.

    Returns:
        The response body, decoded as text.

    Raises:
        ProviderAuthError: If the credentials were rejected.
        ProviderRateLimitError: If rate limited on the final attempt.
        ProviderRequestError: If the request failed on the final attempt.
    """
    policy = retry or RetryPolicy()
    last_error: ProviderError = ProviderRequestError(f"{provider} request was never attempted")

    for attempt in range(1, policy.attempts + 1):
        if limiter is not None:
            limiter.wait()

        try:
            response = client.request(method, url)
        except httpx.HTTPError as exc:
            last_error = ProviderRequestError(f"{provider} request failed: {type(exc).__name__}")
        else:
            if response.status_code < httpx.codes.BAD_REQUEST:
                return response.text
            if not _is_retryable(response.status_code):
                _raise_for_status(response, provider)
            last_error = _status_error(response.status_code, provider)

        if attempt < policy.attempts:
            policy.pause(attempt)

    raise last_error


def _status_error(status_code: int, provider: str) -> ProviderError:
    """Build the error to raise if a retryable status is still failing at the end."""
    if status_code == _TOO_MANY_REQUESTS:
        return ProviderRateLimitError(f"{provider} rate limit reached (HTTP {status_code})")
    return ProviderRequestError(f"{provider} request failed with HTTP {status_code}")


def _hint(response: httpx.Response) -> str:
    """Return the provider's own error text, truncated.

    The key travels in the query string for some providers, so the body is
    capped rather than echoed whole — and only ever the body, never the URL.
    """
    text = response.text.strip().replace("\n", " ")
    return text[:_ERROR_BODY_CHARS] if text else f"HTTP {response.status_code}"


def _decode(response: httpx.Response, provider: str) -> Any:
    """Decode a JSON body, converting a parse failure into a provider error."""
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderDataError(f"{provider} returned a non-JSON response") from exc
