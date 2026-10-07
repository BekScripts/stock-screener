"""P2.2D — the four defects the first real preflight exposed, and their repairs.

Each test here corresponds to something that was wrong and is now not. They are
worth reading as a record of what a fake transport cannot catch: every one of
these defects passed 211 fake-transport tests, because a fake answers whatever
it is told to and therefore agrees with whatever the adapter asks for. Only the
documented provider surface disagreed.

No test here makes a network request or places an order.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.external import (
    ACCOUNT_PATH,
    ACTIVITIES_PATH,
    OPEN_ORDER_QUERY,
    ORDER_HISTORY_QUERY,
    ORDERS_PATH,
    POSITIONS_PATH,
    PREFLIGHT_GATES,
    AlpacaPaperAdapter,
    BrokerCapabilities,
    BrokerEnvironment,
    BrokerResponse,
    CancellationOutcome,
    ExternalBrokerAccountBinding,
    ExternalReadEvidence,
    FakeTransport,
    ProviderPaperEnvironmentProof,
    RequestMethod,
    SubmissionIntentLog,
    SymbolMapping,
    TransportOutcome,
    VenueSymbol,
    check_no_credentials,
    client_order_id_for,
    prove_paper_environment,
)
from stock_screener.execution.external.preflight import preflight

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

PAPER_URL = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-1"
REMOTE_ORDER = "a1b2c3d4-0000-4000-8000-000000000001"
OTHER_ORDER = "ffffffff-9999-4000-8000-000000000099"


def binding(**caps: Any) -> ExternalBrokerAccountBinding:
    """A proven paper binding."""
    proof = prove_paper_environment(
        environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="PKTESTKEY01"
    )
    return ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=ACCOUNT_ID,
        proof=proof,
        capabilities=BrokerCapabilities(**caps) if caps else BrokerCapabilities(),
    )


def mapping() -> SymbolMapping:
    """A mapping covering the canary symbol only."""
    return SymbolMapping.of([VenueSymbol(kind="COMPANY", identity_id=11, symbol="AAPL")])


def adapter(transport: FakeTransport, **caps: Any) -> AlpacaPaperAdapter:
    """An adapter over a scripted transport."""
    return AlpacaPaperAdapter(transport=transport, binding=binding(**caps), mapping=mapping())


def answered(body: Any, *, status: int = 200, request_id: str | None = None) -> BrokerResponse:
    """A successful provider reply."""
    return BrokerResponse(
        outcome=TransportOutcome.ANSWERED,
        status_code=status,
        body=body,
        request_id=request_id,
        safe_headers=(("x-request-id", request_id),) if request_id else (),
    )


def activity(
    *,
    remote_id: str = "exec-1",
    order_id: str = REMOTE_ORDER,
    symbol: str = "AAPL",
    qty: str = "1",
    price: str = "100.00",
    activity_type: str = "FILL",
    kind: str = "fill",
    at: str = "2026-10-06T14:31:00Z",
) -> dict[str, Any]:
    """One provider activity row, shaped as the real endpoint sends it."""
    return {
        "id": remote_id,
        "activity_type": activity_type,
        "type": kind,
        "order_id": order_id,
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "price": price,
        "transaction_time": at,
    }


def order_payload(*, status: str = "new", filled: str = "0", qty: str = "1") -> dict[str, Any]:
    """One provider order object."""
    return {
        "id": REMOTE_ORDER,
        "client_order_id": client_order_id_for("ORD-CANARY"),
        "symbol": "AAPL",
        "side": "buy",
        "qty": qty,
        "filled_qty": filled,
        "status": status,
        "type": "market",
        "time_in_force": "day",
    }


# ---------------------------------------------------------------------------
# §31 the fill query uses the provider order id
# ---------------------------------------------------------------------------


def test_the_fill_query_sends_the_provider_order_id() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity()]))

    adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)

    sent = [row for row in transport.calls if row["path"] == ACTIVITIES_PATH]
    assert sent[0]["params"] == {"order_id": REMOTE_ORDER}


def test_the_fill_query_never_sends_a_client_order_id() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity()]))

    adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)

    sent = [row for row in transport.calls if row["path"] == ACTIVITIES_PATH]
    assert "client_order_id" not in sent[0]["params"]


def test_reading_fills_without_a_provider_order_id_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.REMOTE_PROVIDER_ORDER_ID_REQUIRED):
        adapter(FakeTransport()).read_fills(remote_order_id="   ")


def test_the_fill_path_never_derives_a_client_order_id() -> None:
    """No fallback: the fill path must not even compute a client order id.

    Asserted on the source rather than on behaviour, because a fallback would
    only show up in a situation the fake cannot produce — a provider that
    ignores one filter and honours another. What is checkable is that the code
    never derives the id that would be sent.
    """
    import inspect

    from stock_screener.execution.external import alpaca

    source = inspect.getsource(alpaca.AlpacaPaperAdapter.fills_evidence)

    assert "client_order_id_for" not in source
    assert '"client_order_id"' not in source


# ---------------------------------------------------------------------------
# §32 account-wide activity contamination
# ---------------------------------------------------------------------------


def test_an_unrelated_order_fill_on_the_same_page_is_refused() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered([activity(), activity(remote_id="exec-9", order_id=OTHER_ORDER)]),
    )

    with pytest.raises(ExecutionError, match=Code.REMOTE_FILL_ORDER_MISMATCH):
        adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)


def test_an_activity_naming_no_order_is_refused() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity(order_id="")]))

    with pytest.raises(ExecutionError, match=Code.REMOTE_FILL_ORDER_MISMATCH):
        adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)


def test_a_fill_in_an_unmapped_symbol_is_refused() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity(symbol="TSLA")]))

    with pytest.raises(ExecutionError, match=Code.REMOTE_FILL_ORDER_MISMATCH):
        adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)


@pytest.mark.parametrize(
    ("activity_type", "kind"),
    [("DIV", "div"), ("FEE", "fee"), ("JNLC", "jnlc"), ("CSD", "csd")],
)
def test_a_non_fill_activity_is_ignored_not_posted(activity_type: str, kind: str) -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered([activity(), activity(remote_id="x-2", activity_type=activity_type, kind=kind)]),
    )

    fills = adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)

    assert [fill.remote_fill_id for fill in fills] == ["exec-1"]


def test_a_mixed_page_admits_only_the_requested_orders_fills() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered(
            [
                activity(remote_id="exec-1", qty="1"),
                activity(remote_id="div-1", activity_type="DIV", kind="div"),
                activity(remote_id="fee-1", activity_type="FEE", kind="fee"),
            ]
        ),
    )

    fills = adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)

    assert len(fills) == 1
    assert fills[0].quantity == Decimal(1)


def test_a_partial_fill_activity_is_admitted() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity(kind="partial_fill")]))

    fills = adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)

    assert len(fills) == 1


# ---------------------------------------------------------------------------
# §33 repeated retrieval stays exactly once
# ---------------------------------------------------------------------------


def test_the_same_activity_returned_ten_times_reads_as_one() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity()] * 10))

    fills = adapter(transport).read_fills(remote_order_id=REMOTE_ORDER)

    assert len(fills) == 1


def test_polling_the_same_fill_repeatedly_yields_the_same_single_fill() -> None:
    page = answered([activity()])
    transport = FakeTransport(default=page)
    reader = adapter(transport)

    first = reader.read_fills(remote_order_id=REMOTE_ORDER)
    second = reader.read_fills(remote_order_id=REMOTE_ORDER)
    third = reader.read_fills(remote_order_id=REMOTE_ORDER)

    assert len(first) == len(second) == len(third) == 1
    assert first[0].remote_fill_id == second[0].remote_fill_id == third[0].remote_fill_id
    assert transport.mutations == []


def test_the_provider_order_id_is_resolved_from_the_durable_record(tmp_path: Path) -> None:
    log = SubmissionIntentLog(tmp_path / "exec")
    log.record_resolution(
        client_order_id_for("ORD-CANARY"),
        outcome=__import__(
            "stock_screener.execution.external", fromlist=["SubmissionOutcome"]
        ).SubmissionOutcome.REMOTE_CONFIRMED,
        remote_order_id=REMOTE_ORDER,
    )
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([activity()]))

    fills = adapter(transport).read_fills_for_local_order("ORD-CANARY", log=log)

    assert len(fills) == 1
    sent = [row for row in transport.calls if row["path"] == ACTIVITIES_PATH]
    assert sent[0]["params"] == {"order_id": REMOTE_ORDER}


def test_an_order_with_no_recorded_provider_id_cannot_have_fills_read(tmp_path: Path) -> None:
    log = SubmissionIntentLog(tmp_path / "exec")

    with pytest.raises(ExecutionError, match=Code.REMOTE_PROVIDER_ORDER_ID_REQUIRED):
        adapter(FakeTransport()).read_fills_for_local_order("ORD-NEVER-SENT", log=log)


# ---------------------------------------------------------------------------
# §38-§40 the paper proof cannot come from configuration
# ---------------------------------------------------------------------------


def evidence(*, status: int = 200, fingerprint_from: Any = None) -> ExternalReadEvidence[Any]:
    """Read evidence standing in for a successful account read."""
    return ExternalReadEvidence.of(
        fingerprint_from or {"id": ACCOUNT_ID},
        answered({"id": ACCOUNT_ID}, status=status, request_id="rq-1"),
    )


def test_a_paper_proof_requires_a_successful_response() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        ProviderPaperEnvironmentProof.from_account_evidence(
            provider="ALPACA",
            origin=PAPER_URL,
            endpoint=ACCOUNT_PATH,
            account_binding_fingerprint="abc",
            evidence=evidence(status=401),
        )


def test_a_paper_proof_requires_an_account_binding() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        ProviderPaperEnvironmentProof.from_account_evidence(
            provider="ALPACA",
            origin=PAPER_URL,
            endpoint=ACCOUNT_PATH,
            account_binding_fingerprint="",
            evidence=evidence(),
        )


def test_a_paper_proof_from_a_live_origin_is_an_incident_not_a_proof() -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        ProviderPaperEnvironmentProof.from_account_evidence(
            provider="ALPACA",
            origin="https://api.alpaca.markets",
            endpoint=ACCOUNT_PATH,
            account_binding_fingerprint="abc",
            evidence=evidence(),
        )


def test_a_paper_proof_from_an_unapproved_origin_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        ProviderPaperEnvironmentProof.from_account_evidence(
            provider="ALPACA",
            origin="https://proxy.example.test",
            endpoint=ACCOUNT_PATH,
            account_binding_fingerprint="abc",
            evidence=evidence(),
        )


def test_a_paper_proof_binds_the_response_it_was_built_from() -> None:
    read = evidence()

    proof = ProviderPaperEnvironmentProof.from_account_evidence(
        provider="ALPACA",
        origin=PAPER_URL,
        endpoint=ACCOUNT_PATH,
        account_binding_fingerprint="abc",
        evidence=read,
    )

    assert proof.response_evidence_fingerprint == read.fingerprint
    assert proof.is_proven is True
    assert proof.environment is BrokerEnvironment.PAPER


def test_a_paper_proof_cannot_be_built_from_configuration() -> None:
    import inspect

    source = inspect.getsource(ProviderPaperEnvironmentProof)

    assert "Settings" not in source
    assert "def from_settings" not in source


def test_a_paper_proof_discloses_that_it_is_not_provider_attestation() -> None:
    proof = ProviderPaperEnvironmentProof.from_account_evidence(
        provider="ALPACA",
        origin=PAPER_URL,
        endpoint=ACCOUNT_PATH,
        account_binding_fingerprint="abc",
        evidence=evidence(),
    )

    notes = " ".join(proof.limitations)
    assert "NOT PROVIDER ATTESTATION" in notes
    assert "ONE MOMENT ONLY" in notes


def test_a_paper_proof_carries_no_credential_or_account_number() -> None:
    proof = ProviderPaperEnvironmentProof.from_account_evidence(
        provider="ALPACA",
        origin=PAPER_URL,
        endpoint=ACCOUNT_PATH,
        account_binding_fingerprint="abc",
        evidence=evidence(),
    )

    serialised = json.dumps(proof.as_json())
    check_no_credentials(proof.as_json(), context="a paper proof")
    assert ACCOUNT_ID not in serialised


def test_the_proof_fingerprint_ignores_when_it_was_taken() -> None:
    kwargs: dict[str, Any] = {
        "provider": "ALPACA",
        "origin": PAPER_URL,
        "endpoint": ACCOUNT_PATH,
        "account_binding_fingerprint": "abc",
        "evidence": evidence(),
    }
    early = ProviderPaperEnvironmentProof.from_account_evidence(**kwargs, verified_at="T1")
    late = ProviderPaperEnvironmentProof.from_account_evidence(**kwargs, verified_at="T2")

    assert early.fingerprint() == late.fingerprint()


def test_the_gate_fails_without_a_proof(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACCOUNT_PATH,
        answered({"id": ACCOUNT_ID, "status": "ACTIVE", "cash": "1.00"}),
    )
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered([]))
    transport.script(RequestMethod.GET, ORDERS_PATH, answered([]))

    readiness = preflight(
        adapter(transport),
        log=SubmissionIntentLog(tmp_path / "exec"),
        origin="https://proxy.example.test",
    )

    assert readiness.is_ready is False
    assert "PROVIDER_CONFIRMS_PAPER" in dict(readiness.failures)


def test_the_gate_passes_only_with_evidence(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACCOUNT_PATH,
        answered({"id": ACCOUNT_ID, "status": "ACTIVE", "cash": "1.00"}),
    )
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered([]))
    transport.script(RequestMethod.GET, ORDERS_PATH, answered([]))

    readiness = preflight(
        adapter(transport), log=SubmissionIntentLog(tmp_path / "exec"), origin=PAPER_URL
    )

    assert "PROVIDER_CONFIRMS_PAPER" in readiness.passed
    assert readiness.paper_proof is not None
    assert readiness.paper_proof.is_proven is True


# ---------------------------------------------------------------------------
# §20, §41 one physical read per endpoint
# ---------------------------------------------------------------------------


def ready_transport() -> FakeTransport:
    """A transport answering all three preflight endpoints once."""
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACCOUNT_PATH,
        answered({"id": ACCOUNT_ID, "status": "ACTIVE", "cash": "1.00"}, request_id="rq-account"),
    )
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered([], request_id="rq-positions"))
    transport.script(RequestMethod.GET, ORDERS_PATH, answered([], request_id="rq-open-orders"))
    return transport


def test_the_preflight_reads_each_endpoint_exactly_once(tmp_path: Path) -> None:
    transport = ready_transport()

    preflight(adapter(transport), log=SubmissionIntentLog(tmp_path / "exec"), origin=PAPER_URL)

    paths = [row["path"] for row in transport.calls]
    assert paths.count(ACCOUNT_PATH) == 1
    assert paths.count(POSITIONS_PATH) == 1
    assert paths.count(ORDERS_PATH) == 1
    assert len(paths) == 3


def test_the_preflight_makes_no_mutating_request(tmp_path: Path) -> None:
    transport = ready_transport()

    preflight(adapter(transport), log=SubmissionIntentLog(tmp_path / "exec"), origin=PAPER_URL)

    assert transport.mutations == []


def test_the_reported_values_and_the_fingerprints_come_from_one_response(
    tmp_path: Path,
) -> None:
    transport = ready_transport()

    readiness = preflight(
        adapter(transport), log=SubmissionIntentLog(tmp_path / "exec"), origin=PAPER_URL
    )

    assert readiness.account is not None
    assert readiness.positions is not None
    assert readiness.open_orders is not None
    # the proof's digest is the account read's digest — the same response
    assert readiness.paper_proof is not None
    assert readiness.paper_proof.response_evidence_fingerprint == readiness.account.fingerprint
    assert readiness.account.request_id == "rq-account"
    assert readiness.open_orders.request_id == "rq-open-orders"


def test_evidence_received_at_is_excluded_from_the_fingerprint() -> None:
    response = answered({"a": "b"}, request_id="rq")

    early = ExternalReadEvidence.of("v", response, received_at="T1")
    late = ExternalReadEvidence.of("v", response, received_at="T2")

    assert early.fingerprint == late.fingerprint


# ---------------------------------------------------------------------------
# §22 open orders are not order history
# ---------------------------------------------------------------------------


def test_the_working_order_read_asks_for_open_orders() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ORDERS_PATH, answered([]))

    adapter(transport).open_orders_evidence()

    assert transport.calls[0]["params"] == OPEN_ORDER_QUERY
    assert transport.calls[0]["params"] == {"status": "open"}


def test_the_history_read_asks_for_all_orders() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ORDERS_PATH, answered([]))

    adapter(transport).order_history_evidence()

    assert transport.calls[0]["params"] == ORDER_HISTORY_QUERY
    assert transport.calls[0]["params"] == {"status": "all"}


def test_terminal_history_is_not_counted_as_working_orders() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ORDERS_PATH,
        answered([order_payload(status="filled", filled="1")]),
    )

    history = adapter(transport).order_history_evidence().value

    assert len(history) == 1
    assert history[0].is_terminal is True


def test_the_preflight_never_fetches_order_history(tmp_path: Path) -> None:
    transport = ready_transport()

    preflight(adapter(transport), log=SubmissionIntentLog(tmp_path / "exec"), origin=PAPER_URL)

    order_calls = [row for row in transport.calls if row["path"] == ORDERS_PATH]
    assert all(row["params"] == {"status": "open"} for row in order_calls)


# ---------------------------------------------------------------------------
# §42 no mutation retry survived the repair
# ---------------------------------------------------------------------------


def test_no_mutating_request_is_ever_repeated_by_the_adapter(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.DELETE,
        f"{ORDERS_PATH}/{REMOTE_ORDER}",
        BrokerResponse(outcome=TransportOutcome.TIMED_OUT),
    )
    transport.script(
        RequestMethod.GET,
        f"{ORDERS_PATH}/{REMOTE_ORDER}",
        answered(order_payload(status="canceled")),
    )

    adapter(transport).cancel_order(
        remote_order_id=REMOTE_ORDER,
        log=SubmissionIntentLog(tmp_path / "exec"),
        requested_at="t",
    )

    assert len(transport.mutations) == 1


@pytest.mark.parametrize(
    "module",
    ["alpaca", "http_transport", "submission", "paper_preflight", "cancellation"],
)
def test_no_external_module_contains_a_mutation_retry_loop(module: str) -> None:
    import importlib
    import inspect

    source = inspect.getsource(
        importlib.import_module(f"stock_screener.execution.external.{module}")
    )

    assert "def mutate_with_retry" not in source
    assert "def write_with_retry" not in source
    assert "while True" not in source


def test_the_adapter_sends_exactly_one_delete_per_cancellation_call(tmp_path: Path) -> None:
    import inspect

    from stock_screener.execution.external import alpaca

    source = inspect.getsource(alpaca.AlpacaPaperAdapter.cancel_order)

    assert source.count("RequestMethod.DELETE") == 1


def test_cancellation_resolution_makes_no_mutating_request() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        f"{ORDERS_PATH}/{REMOTE_ORDER}",
        answered(order_payload(status="canceled")),
    )

    result = adapter(transport).resolve_cancellation(REMOTE_ORDER)

    assert result.outcome is CancellationOutcome.REMOTE_CANCELLED
    assert result.mutating_requests == 0
    assert transport.mutations == []


# ---------------------------------------------------------------------------
# the gate list is unchanged, so the repair did not widen the contract
# ---------------------------------------------------------------------------


def test_the_seven_gates_are_unchanged() -> None:
    assert PREFLIGHT_GATES == (
        "ENVIRONMENT_PROVEN_PAPER",
        "ACCOUNT_REACHABLE",
        "ACCOUNT_MATCHES_BINDING",
        "PROVIDER_CONFIRMS_PAPER",
        "TRADING_NOT_BLOCKED",
        "SYMBOLS_RESOLVED",
        "NO_UNRESOLVED_SUBMISSIONS",
    )


def test_the_engine_version_did_not_move() -> None:
    from stock_screener.execution.versions import EXECUTION_ENGINE_VERSION_V1_2

    assert EXECUTION_ENGINE_VERSION_V1_2 == "EXECUTION_ENGINE_V1_2"


def test_the_frozen_v1_and_v1_1_code_sets_did_not_move() -> None:
    from stock_screener.execution.errors import V1_1_ERROR_CODES, V1_ERROR_CODES

    assert len(V1_ERROR_CODES) == 40
    assert len(V1_1_ERROR_CODES) == 50
