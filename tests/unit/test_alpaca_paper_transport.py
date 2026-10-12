"""P2.2C — the concrete Alpaca paper transport, exercised without a network.

Every test here drives the real `AlpacaPaperHttpTransport` code — its URL
building, query encoding, JSON handling, failure classification, header
redaction, host lock and redirect refusal — against `httpx.MockTransport`, which
answers at the HTTP layer without opening a socket. That is deliberately *not*
the same as the `FakeTransport` used in the P2.2 suite: `FakeTransport` replaces
this module, so it proves nothing about it.

No test in this file makes a network request, and none places an order anywhere.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from stock_screener.config import Settings
from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.external import (
    KEY_ID_HEADER,
    SECRET_KEY_HEADER,
    TRANSPORT_VERSION,
    AlpacaPaperHttpTransport,
    BrokerTransport,
    RequestMethod,
    TransportOutcome,
    approved_paper_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit

PAPER_ORIGIN = "https://paper-api.alpaca.markets"
KEY_ID = "PKTESTKEYID0001"
# S105 is suppressed because this is a dummy fixture value asserted *not* to
# appear in reprs, details, evidence bodies or reports. It authenticates nothing.
SECRET = "TESTSECRETVALUE0001"  # noqa: S105


def transport_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AlpacaPaperHttpTransport:
    """Build a transport whose HTTP layer is a stub.

    Args:
        handler: Answers each request.

    Returns:
        The transport, with this module's own logic fully in play.
    """
    client = httpx.Client(
        base_url=PAPER_ORIGIN,
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    return AlpacaPaperHttpTransport(
        origin=PAPER_ORIGIN,
        key_id=SecretStr(KEY_ID),
        secret_key=SecretStr(SECRET),
        client=client,
        audit=MemoryAudit(),
    )


class MemoryAudit:
    """An in-memory request-audit sink.

    The transport refuses to mutate without an audit sink bound, so these tests
    bind one. It is in memory rather than a `RequestAuditStore` because these
    are unit tests and must not touch the filesystem — and because what the
    transport requires is a *sink*, not a particular implementation of one.
    """

    def __init__(self) -> None:
        self.attempts: list[Any] = []
        self.results: list[Any] = []

    def record_attempt(self, record: Any) -> Any:
        """Record the pre-dispatch attempt."""
        self.attempts.append(record)
        return record

    def record_result(self, record: Any) -> Any:
        """Record how the attempt resolved."""
        self.results.append(record)
        return record


def recorder() -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    """Build a handler that records every request and answers 200.

    Returns:
        The recorded requests and the handler.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"X-Request-ID": "req-1"})

    return seen, handler


# ---------------------------------------------------------------------------
# host lock
# ---------------------------------------------------------------------------


def test_the_paper_origin_is_approved() -> None:
    assert approved_paper_origin(PAPER_ORIGIN) == PAPER_ORIGIN


def test_a_path_on_the_paper_origin_is_reduced_to_the_origin() -> None:
    assert approved_paper_origin(f"{PAPER_ORIGIN}/v2/orders") == PAPER_ORIGIN


@pytest.mark.parametrize(
    "live",
    [
        "https://api.alpaca.markets",
        "https://broker-api.alpaca.markets",
        "https://broker-api.sandbox.alpaca.markets",
        "https://api.alpaca.markets/v2/orders",
    ],
)
def test_a_live_or_brokerage_host_is_refused_by_name(live: str) -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        approved_paper_origin(live)


@pytest.mark.parametrize(
    "rejected",
    [
        "http://paper-api.alpaca.markets",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://[::1]",
        "https://user:pass@paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:8080",
        "https://evil.example.test",
        "https://paper-api.alpaca.markets.evil.test",
        "",
        "   ",
    ],
)
def test_anything_not_provably_the_paper_origin_is_refused(rejected: str) -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        approved_paper_origin(rejected)


def test_the_constructor_enforces_the_host_lock_too() -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        AlpacaPaperHttpTransport(
            origin="https://api.alpaca.markets",
            key_id=SecretStr(KEY_ID),
            secret_key=SecretStr(SECRET),
        )


def test_a_live_host_is_refused_before_any_request_is_made() -> None:
    seen, handler = recorder()

    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        AlpacaPaperHttpTransport(
            origin="https://api.alpaca.markets",
            key_id=SecretStr(KEY_ID),
            secret_key=SecretStr(SECRET),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert seen == []


@pytest.mark.parametrize(
    "absolute",
    [
        "https://api.alpaca.markets/v2/orders",
        "//api.alpaca.markets/v2/orders",
        "http://evil.example.test/v2/orders",
    ],
)
def test_an_absolute_url_as_a_path_cannot_leave_the_locked_origin(absolute: str) -> None:
    seen, handler = recorder()
    transport = transport_with(handler)

    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        transport.request(RequestMethod.POST, absolute, json_body={"a": "b"})

    assert seen == []


def test_every_request_goes_to_the_paper_origin() -> None:
    seen, handler = recorder()

    transport_with(handler).request(RequestMethod.GET, "/v2/account")

    assert str(seen[0].url) == f"{PAPER_ORIGIN}/v2/account"


def test_a_relative_path_is_normalised_onto_the_origin() -> None:
    seen, handler = recorder()

    transport_with(handler).request(RequestMethod.GET, "v2/account")

    assert str(seen[0].url) == f"{PAPER_ORIGIN}/v2/account"


def test_the_production_client_is_locked_down() -> None:
    transport = AlpacaPaperHttpTransport(
        origin=PAPER_ORIGIN, key_id=SecretStr(KEY_ID), secret_key=SecretStr(SECRET)
    )

    assert transport.client is not None
    assert transport.client.follow_redirects is False
    assert transport.client.trust_env is False
    assert transport.client.timeout.connect == 10.0
    assert transport.client.timeout.read == 30.0
    transport.close()


# ---------------------------------------------------------------------------
# protocol conformance and request shaping
# ---------------------------------------------------------------------------


def test_the_transport_satisfies_the_broker_transport_protocol() -> None:
    _, handler = recorder()

    assert isinstance(transport_with(handler), BrokerTransport)


@pytest.mark.parametrize("method", [RequestMethod.GET, RequestMethod.POST, RequestMethod.DELETE])
def test_the_requested_method_is_the_method_sent(method: RequestMethod) -> None:
    seen, handler = recorder()

    transport_with(handler).request(method, "/v2/orders")

    assert seen[0].method == str(method)


def test_query_parameters_are_encoded_onto_the_url() -> None:
    seen, handler = recorder()

    transport_with(handler).request(
        RequestMethod.GET,
        "/v2/orders:by_client_order_id",
        params={"client_order_id": "ssx1-abc", "nested": "a b&c"},
    )

    url = str(seen[0].url)
    assert "client_order_id=ssx1-abc" in url
    assert "nested=a+b%26c" in url or "nested=a%20b%26c" in url


def test_the_colon_path_alpaca_uses_is_not_mangled() -> None:
    seen, handler = recorder()

    transport_with(handler).request(RequestMethod.GET, "/v2/orders:by_client_order_id")

    assert seen[0].url.path == "/v2/orders:by_client_order_id"


def test_a_json_body_is_sent_as_compact_json() -> None:
    seen, handler = recorder()

    transport_with(handler).request(
        RequestMethod.POST, "/v2/orders", json_body={"symbol": "AAPL", "qty": "1"}
    )

    assert json.loads(seen[0].content) == {"symbol": "AAPL", "qty": "1"}
    assert seen[0].headers["content-type"] == "application/json"


def test_a_read_sends_no_body() -> None:
    seen, handler = recorder()

    transport_with(handler).request(RequestMethod.GET, "/v2/account")

    assert seen[0].content == b""


def test_a_decimal_in_the_payload_is_refused_before_anything_is_sent() -> None:
    seen, handler = recorder()

    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        transport_with(handler).request(
            RequestMethod.POST, "/v2/orders", json_body={"qty": Decimal("1.5")}
        )

    assert seen == []


def test_a_nan_in_the_payload_is_refused_before_anything_is_sent() -> None:
    seen, handler = recorder()

    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        transport_with(handler).request(
            RequestMethod.POST, "/v2/orders", json_body={"qty": float("nan")}
        )

    assert seen == []


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_the_documented_authentication_headers_are_sent() -> None:
    seen, handler = recorder()

    transport_with(handler).request(RequestMethod.GET, "/v2/account")

    assert seen[0].headers[KEY_ID_HEADER] == KEY_ID
    assert seen[0].headers[SECRET_KEY_HEADER] == SECRET


def test_the_credentials_never_appear_in_the_repr() -> None:
    _, handler = recorder()

    text = repr(transport_with(handler))

    assert KEY_ID not in text
    assert SECRET not in text
    assert "redacted" in text


def test_the_credentials_never_appear_in_a_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    response = transport_with(handler).request(RequestMethod.GET, "/v2/account")

    serialised = json.dumps(response.as_json())
    assert KEY_ID not in serialised
    assert SECRET not in serialised


def test_the_credentials_never_appear_in_a_classified_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed to connect", request=request)

    response = transport_with(handler).request(RequestMethod.GET, "/v2/account")

    assert response.detail is not None
    assert KEY_ID not in response.detail
    assert SECRET not in response.detail


def test_the_credentials_are_absent_from_the_evidence_fingerprint_body() -> None:
    _, handler = recorder()

    response = transport_with(handler).request(RequestMethod.GET, "/v2/account")

    serialised = json.dumps(response.evidence_body())
    assert KEY_ID not in serialised
    assert SECRET not in serialised
    assert KEY_ID_HEADER.lower() not in serialised.lower()


def test_building_from_settings_requires_both_paper_credentials() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ExecutionError, match=Code.BROKER_CREDENTIALS_MISSING):
        AlpacaPaperHttpTransport.from_settings(settings)


def test_building_from_settings_uses_the_configured_paper_origin() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        alpaca_paper_key_id=SecretStr(KEY_ID),
        alpaca_paper_secret_key=SecretStr(SECRET),
    )

    transport = AlpacaPaperHttpTransport.from_settings(settings)

    assert transport.origin == PAPER_ORIGIN
    transport.close()


def test_building_from_settings_refuses_a_live_origin() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        alpaca_paper_key_id=SecretStr(KEY_ID),
        alpaca_paper_secret_key=SecretStr(SECRET),
        alpaca_paper_base_url="https://api.alpaca.markets",
    )

    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        AlpacaPaperHttpTransport.from_settings(settings)


# ---------------------------------------------------------------------------
# response classification
# ---------------------------------------------------------------------------


def answering(status: int, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    """Build a handler answering one fixed reply.

    Args:
        status: The status to answer.
        body: A JSON body, or None for an empty one.
        headers: Response headers.

    Returns:
        The handler.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, headers=headers or {})
        return httpx.Response(status, json=body, headers=headers or {})

    return handler


def test_a_success_is_answered_with_its_body() -> None:
    response = transport_with(answering(200, {"id": "remote-1"})).request(
        RequestMethod.GET, "/v2/orders"
    )

    assert response.outcome is TransportOutcome.ANSWERED
    assert response.ok is True
    assert response.body == {"id": "remote-1"}


def test_a_404_is_an_answer_not_a_failure() -> None:
    response = transport_with(answering(404, {"message": "order not found"})).request(
        RequestMethod.GET, "/v2/orders:by_client_order_id"
    )

    assert response.not_found is True
    assert response.is_ambiguous is False


@pytest.mark.parametrize("status", [400, 401, 403, 422, 500, 503])
def test_a_non_success_is_answered_and_not_ambiguous(status: int) -> None:
    response = transport_with(answering(status, {"message": "no"})).request(
        RequestMethod.POST, "/v2/orders", json_body={"a": "b"}
    )

    assert response.outcome is TransportOutcome.ANSWERED
    assert response.ok is False
    assert response.status_code == status


def test_a_rate_limit_is_classified_as_such() -> None:
    response = transport_with(answering(429, {"message": "slow down"})).request(
        RequestMethod.GET, "/v2/account"
    )

    assert response.outcome is TransportOutcome.RATE_LIMITED
    assert response.is_ambiguous is True


def test_an_unreadable_body_leaves_the_state_unknown_rather_than_guessed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    response = transport_with(handler).request(RequestMethod.POST, "/v2/orders", json_body={})

    assert response.outcome is TransportOutcome.ANSWERED
    assert response.body is None
    assert response.detail is not None
    assert "not JSON" in response.detail


def test_an_empty_body_is_a_clean_answer() -> None:
    response = transport_with(answering(204)).request(RequestMethod.DELETE, "/v2/orders/x")

    assert response.ok is True
    assert response.body is None


# ---------------------------------------------------------------------------
# redirects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_rather_than_followed(status: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"Location": "https://api.alpaca.markets/v2/orders"})

    response = transport_with(handler).request(
        RequestMethod.POST, "/v2/orders", json_body={"symbol": "AAPL"}
    )

    assert response.outcome is TransportOutcome.UNREACHABLE
    assert response.ok is False
    assert len(seen) == 1
    assert all(request.url.host == "paper-api.alpaca.markets" for request in seen)


def test_a_redirect_on_a_mutation_is_reported_as_ambiguous_not_successful() -> None:
    response = transport_with(
        answering(307, headers={"Location": "https://api.alpaca.markets/v2/orders"})
    ).request(RequestMethod.POST, "/v2/orders", json_body={"symbol": "AAPL"})

    assert response.is_ambiguous is True
    assert response.ok is False


def test_a_redirect_never_reaches_the_live_host() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://api.alpaca.markets/v2/orders"})

    transport_with(handler).request(RequestMethod.POST, "/v2/orders", json_body={})

    assert not any("api.alpaca.markets/v2" in url and "paper" not in url for url in seen)


# ---------------------------------------------------------------------------
# network failure classification
# ---------------------------------------------------------------------------


def raising(error: Exception) -> Any:
    """Build a handler that raises a client-level error.

    Args:
        error: What to raise.

    Returns:
        The handler.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    return handler


def test_a_timeout_leaves_a_mutation_unknown_not_failed() -> None:
    response = transport_with(raising(httpx.ReadTimeout("timed out"))).request(
        RequestMethod.POST, "/v2/orders", json_body={"symbol": "AAPL"}
    )

    assert response.outcome is TransportOutcome.TIMED_OUT
    assert response.is_ambiguous is True
    assert response.ok is False


def test_a_connect_failure_is_unreachable() -> None:
    response = transport_with(raising(httpx.ConnectError("no route"))).request(
        RequestMethod.GET, "/v2/account"
    )

    assert response.outcome is TransportOutcome.UNREACHABLE
    assert response.is_ambiguous is True


def test_a_connect_timeout_is_a_timeout() -> None:
    response = transport_with(raising(httpx.ConnectTimeout("slow"))).request(
        RequestMethod.GET, "/v2/account"
    )

    assert response.outcome is TransportOutcome.TIMED_OUT


def test_a_connection_reset_mid_exchange_is_ambiguous() -> None:
    response = transport_with(raising(httpx.RemoteProtocolError("reset"))).request(
        RequestMethod.POST, "/v2/orders", json_body={}
    )

    assert response.is_ambiguous is True


def test_a_tls_failure_is_classified_without_leaking_its_message() -> None:
    response = transport_with(
        raising(httpx.ConnectError("certificate verify failed for SUPERSECRETVALUE0001"))
    ).request(RequestMethod.GET, "/v2/account")

    assert response.outcome is TransportOutcome.UNREACHABLE
    assert response.detail is not None
    assert SECRET not in response.detail


def test_an_unexpected_client_error_is_classified_not_propagated() -> None:
    response = transport_with(raising(httpx.TooManyRedirects("too many"))).request(
        RequestMethod.GET, "/v2/account"
    )

    assert response.outcome is TransportOutcome.UNREACHABLE


def test_a_failure_detail_names_the_exception_class_not_its_text() -> None:
    response = transport_with(raising(httpx.ReadTimeout("gory internal detail"))).request(
        RequestMethod.GET, "/v2/account"
    )

    assert response.detail is not None
    assert "ReadTimeout" in response.detail
    assert "gory internal detail" not in response.detail


# ---------------------------------------------------------------------------
# no mutation retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("t"),
        httpx.ConnectError("c"),
        httpx.RemoteProtocolError("r"),
    ],
)
def test_a_failed_mutation_is_attempted_exactly_once(error: Exception) -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise error

    transport_with(handler).request(RequestMethod.POST, "/v2/orders", json_body={"a": "b"})

    assert len(attempts) == 1


def test_a_failed_read_is_also_attempted_once_per_call() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("t")

    transport_with(handler).request(RequestMethod.GET, "/v2/account")

    assert len(attempts) == 1


def test_the_transport_module_contains_no_retry_loop() -> None:
    import inspect

    from stock_screener.execution.external import http_transport

    source = inspect.getsource(http_transport)

    assert "for attempt in" not in source
    assert "while True" not in source
    assert "def mutate_with_retry" not in source


# ---------------------------------------------------------------------------
# request id and evidence
# ---------------------------------------------------------------------------


def test_the_provider_request_id_is_captured_when_present() -> None:
    response = transport_with(
        answering(200, {"ok": True}, headers={"X-Request-ID": "abc-123"})
    ).request(RequestMethod.GET, "/v2/account")

    assert response.request_id == "abc-123"
    assert ("x-request-id", "abc-123") in response.safe_headers


def test_a_missing_request_id_is_not_required() -> None:
    response = transport_with(answering(200, {"ok": True})).request(
        RequestMethod.GET, "/v2/account"
    )

    assert response.request_id is None
    assert response.ok is True


def test_a_request_id_is_captured_on_a_failure_reply_too() -> None:
    response = transport_with(
        answering(422, {"message": "no"}, headers={"X-Request-ID": "err-9"})
    ).request(RequestMethod.POST, "/v2/orders", json_body={})

    assert response.request_id == "err-9"


def test_only_allow_listed_headers_are_recorded() -> None:
    response = transport_with(
        answering(
            200,
            {"ok": True},
            headers={
                "X-Request-ID": "abc",
                "Set-Cookie": "session=secret",
                "Authorization": "Bearer leak",
                "X-Internal-Debug": "leak",
            },
        )
    ).request(RequestMethod.GET, "/v2/account")

    recorded = dict(response.safe_headers)
    assert "x-request-id" in recorded
    assert "set-cookie" not in recorded
    assert "authorization" not in recorded
    assert "x-internal-debug" not in recorded


def test_the_evidence_fingerprint_is_stable_for_the_same_reply() -> None:
    first = transport_with(answering(200, {"id": "remote-1", "qty": "10"})).request(
        RequestMethod.GET, "/v2/orders"
    )
    second = transport_with(answering(200, {"qty": "10", "id": "remote-1"})).request(
        RequestMethod.GET, "/v2/orders"
    )

    assert first.evidence_fingerprint() == second.evidence_fingerprint()


def test_the_evidence_fingerprint_moves_when_the_reply_moves() -> None:
    first = transport_with(answering(200, {"qty": "10"})).request(RequestMethod.GET, "/v2/orders")
    second = transport_with(answering(200, {"qty": "11"})).request(RequestMethod.GET, "/v2/orders")

    assert first.evidence_fingerprint() != second.evidence_fingerprint()


def test_the_transport_version_is_declared() -> None:
    assert TRANSPORT_VERSION == "ALPACA_PAPER_HTTPS_TRANSPORT_V1"


@pytest.mark.unit
def test_a_mutation_without_an_audit_sink_is_refused() -> None:
    """An absent sink is a reason not to mutate, not a reason to skip auditing.

    `audit=None` used to return early and let the mutation through unobserved,
    which is how a production transport came to send orders with nothing
    recording where they went.
    """
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.method)
        return httpx.Response(200, json={})

    transport = AlpacaPaperHttpTransport(
        origin=PAPER_ORIGIN,
        key_id=SecretStr(KEY_ID),
        secret_key=SecretStr(SECRET),
        client=httpx.Client(
            base_url=PAPER_ORIGIN, transport=httpx.MockTransport(handler), timeout=1.0
        ),
    )

    with pytest.raises(ExecutionError, match="EXTERNAL_REQUEST_AUDIT_FAILED"):
        transport.request(RequestMethod.POST, "/v2/orders", json_body={"client_order_id": "X"})

    assert reached == []


@pytest.mark.unit
def test_a_read_without_an_audit_sink_still_proceeds() -> None:
    """The asymmetry is deliberate: a read changes nothing.

    Failing a reconciliation read closed over a missing audit sink would block
    operations without buying any safety.
    """
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.method)
        return httpx.Response(200, json={})

    transport = AlpacaPaperHttpTransport(
        origin=PAPER_ORIGIN,
        key_id=SecretStr(KEY_ID),
        secret_key=SecretStr(SECRET),
        client=httpx.Client(
            base_url=PAPER_ORIGIN, transport=httpx.MockTransport(handler), timeout=1.0
        ),
    )

    reply = transport.request(RequestMethod.GET, "/v2/account")

    assert reached == ["GET"]
    assert reply.status_code == 200
