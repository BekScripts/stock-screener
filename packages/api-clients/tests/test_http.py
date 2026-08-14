"""Tests for the shared retry and rate-limiting layer.

`sleep` is injected everywhere, so these exercise the real backoff arithmetic
without any test taking longer than a microsecond. A retry test that actually
slept would be the slowest thing in the suite and would tempt someone to delete
it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api_clients import (
    ProviderAuthError,
    ProviderDataError,
    ProviderRateLimitError,
    ProviderRequestError,
    RateLimiter,
    RetryPolicy,
)
from api_clients._http import request_json


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        """Return the current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record a sleep and advance the clock by it."""
        self.slept.append(seconds)
        self.now += seconds


def _client(handler: Any) -> httpx.Client:
    """Build a client backed by a mock transport."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test")


def _request(client: httpx.Client, **kwargs: Any) -> Any:
    """Issue a GET through the retry layer."""
    return request_json(client, "GET", "/thing", provider="test", **kwargs)


@pytest.mark.unit
def test_a_successful_request_returns_the_decoded_body() -> None:
    client = _client(lambda _request: httpx.Response(200, json={"ok": True}))

    assert _request(client) == {"ok": True}


@pytest.mark.unit
def test_a_server_error_is_retried_then_succeeds() -> None:
    responses = [httpx.Response(503), httpx.Response(200, json={"ok": True})]
    clock = FakeClock()

    client = _client(lambda _request: responses.pop(0))
    result = _request(client, retry=RetryPolicy(attempts=3, backoff_seconds=1.0, sleep=clock.sleep))

    assert result == {"ok": True}
    assert clock.slept == [1.0]


@pytest.mark.unit
def test_backoff_doubles_between_attempts() -> None:
    clock = FakeClock()
    client = _client(lambda _request: httpx.Response(500))

    with pytest.raises(ProviderRequestError, match="HTTP 500"):
        _request(client, retry=RetryPolicy(attempts=4, backoff_seconds=0.5, sleep=clock.sleep))

    assert clock.slept == [0.5, 1.0, 2.0]


@pytest.mark.unit
def test_a_rate_limit_is_retried_and_reported_as_such_if_it_persists() -> None:
    clock = FakeClock()
    client = _client(lambda _request: httpx.Response(429))

    with pytest.raises(ProviderRateLimitError, match="rate limit"):
        _request(client, retry=RetryPolicy(attempts=2, backoff_seconds=0.1, sleep=clock.sleep))

    assert len(clock.slept) == 1


@pytest.mark.unit
def test_rejected_credentials_are_not_retried() -> None:
    # Repeating a rejected key does not make it valid, and hammering the
    # endpoint is how an API key gets suspended.
    clock = FakeClock()
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401)

    with pytest.raises(ProviderAuthError):
        _request(
            _client(handler), retry=RetryPolicy(attempts=5, backoff_seconds=1.0, sleep=clock.sleep)
        )

    assert attempts["n"] == 1
    assert clock.slept == []


@pytest.mark.unit
def test_a_client_error_that_is_not_a_rate_limit_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404)

    with pytest.raises(ProviderRequestError, match="HTTP 404"):
        _request(_client(handler), retry=RetryPolicy(attempts=3, backoff_seconds=0.0))

    assert attempts["n"] == 1


@pytest.mark.unit
def test_a_transport_failure_is_retried() -> None:
    clock = FakeClock()
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={"ok": True})

    result = _request(
        _client(handler), retry=RetryPolicy(attempts=3, backoff_seconds=0.1, sleep=clock.sleep)
    )

    assert result == {"ok": True}
    assert attempts["n"] == 2


@pytest.mark.unit
def test_a_persistent_transport_failure_reports_the_error_type_not_the_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to https://test/thing?apikey=secret")

    with pytest.raises(ProviderRequestError) as exc_info:
        _request(_client(handler), retry=RetryPolicy(attempts=1))

    assert "ConnectError" in str(exc_info.value)
    assert "apikey" not in str(exc_info.value)


@pytest.mark.unit
def test_a_non_json_body_raises_a_data_error() -> None:
    client = _client(lambda _request: httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(ProviderDataError, match="non-JSON"):
        _request(client)


@pytest.mark.unit
def test_the_rate_limiter_waits_out_the_configured_interval() -> None:
    clock = FakeClock()
    limiter = RateLimiter(0.5, clock=clock.time, sleep=clock.sleep)

    limiter.wait()  # first call never waits
    limiter.wait()  # immediately after, so it must wait the full interval

    assert clock.slept == [0.5]


@pytest.mark.unit
def test_the_rate_limiter_does_not_wait_when_enough_time_has_passed() -> None:
    clock = FakeClock()
    limiter = RateLimiter(0.5, clock=clock.time, sleep=clock.sleep)

    limiter.wait()
    clock.now += 2.0
    limiter.wait()

    assert clock.slept == []


@pytest.mark.unit
def test_a_zero_interval_disables_rate_limiting() -> None:
    clock = FakeClock()
    limiter = RateLimiter(0.0, clock=clock.time, sleep=clock.sleep)

    limiter.wait()
    limiter.wait()

    assert clock.slept == []


@pytest.mark.unit
def test_the_rate_limiter_applies_to_retries_as_well_as_first_attempts() -> None:
    clock = FakeClock()
    responses = [httpx.Response(500), httpx.Response(200, json={})]
    limiter = RateLimiter(0.25, clock=clock.time, sleep=clock.sleep)

    _request(
        _client(lambda _request: responses.pop(0)),
        limiter=limiter,
        retry=RetryPolicy(attempts=2, backoff_seconds=0.0, sleep=clock.sleep),
    )

    # The limiter's interval separates the retry from the first attempt.
    assert 0.25 in clock.slept
