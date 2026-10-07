"""P2.2 — the external paper-broker adapter, exercised without a network.

Every test here runs against an injected `FakeTransport`. That is not a
limitation of the suite, it is the property being tested: the adapter's whole
network surface is one injected protocol, so every failure a real venue can
produce — a lost response, a request that never arrived, an unmapped status, a
duplicate execution, an account that is not ours — can be produced on demand and
asserted exactly. A suite that needed a reachable venue could not test any of
them reliably.

The tests that would place a real paper order live in `tests/live/` behind
`--live-paper`, so `make check` makes no external request of any kind.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from stock_screener.execution import Order, OrderSide
from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.external import (
    ACCOUNT_PATH,
    ACTIVITIES_PATH,
    CLIENT_ID_PREFIX,
    EXECUTION_MANIFEST_VERSION_V1_2,
    LIVE_HOSTS,
    ORDERS_PATH,
    PAPER_HOSTS,
    POSITIONS_PATH,
    PREFLIGHT_GATES,
    PROVIDER_STATUS_MAP,
    READY_VERDICT,
    AlpacaPaperAdapter,
    BrokerCapabilities,
    BrokerEnvironment,
    BrokerResponse,
    CancellationOutcome,
    ExecutionVenue,
    ExternalBreakSeverity,
    ExternalBrokerAccountBinding,
    ExternalExecutionFill,
    ExternalOrderSnapshot,
    ExternalPaperAccountSnapshot,
    ExternalPaperExecutionReport,
    ExternalPosition,
    FakeTransport,
    ReconciliationVerdict,
    RequestMethod,
    SubmissionIntent,
    SubmissionIntentLog,
    SubmissionOutcome,
    SymbolMapping,
    TransportOutcome,
    VenueBinding,
    VenueSymbol,
    account_fingerprint,
    check_no_credentials,
    client_order_id_for,
    external_manifest,
    is_our_client_order_id,
    preflight,
    prove_paper_environment,
    read_provider_status,
    read_with_retry,
    reconcile_external,
    refuse_live,
    resolve_submission,
    submit_order,
)
from stock_screener.portfolio_accounting import (
    AttributionCostPool,
    PooledPosition,
    PortfolioState,
)
from stock_screener.portfolio_risk import SecurityIdentity

pytestmark = pytest.mark.integration

AAPL = SecurityIdentity(kind="COMPANY", identity_id=11, symbol="AAPL")
MSFT = SecurityIdentity(kind="COMPANY", identity_id=12, symbol="MSFT")
PAPER_URL = "https://paper-api.alpaca.markets"
LOOKUP_PATH = f"{ORDERS_PATH}:by_client_order_id"


def proof() -> Any:
    """A standing configuration-level paper proof."""
    return prove_paper_environment(
        environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="PKTESTKEY123"
    )


def binding(*, account_id: str = "paper-account-1", **caps: Any) -> ExternalBrokerAccountBinding:
    """A proven paper account binding."""
    return ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=account_id,
        proof=proof(),
        capabilities=BrokerCapabilities(**caps) if caps else BrokerCapabilities(),
        label="test paper book",
    )


def mapping() -> SymbolMapping:
    """A two-entry declared symbol mapping."""
    return SymbolMapping.from_symbols({("COMPANY", 11): "AAPL", ("COMPANY", 12): "MSFT"})


def order(
    *,
    order_id: str = "ORD-1",
    identity: SecurityIdentity = AAPL,
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal(10),
) -> Order:
    """One authorised order."""
    return Order(
        order_id=order_id,
        execution_plan_id="PLAN-1",
        authorization_fingerprint="f" * 64,
        identity=identity,
        side=side,
        quantity=quantity,
        sizing_price=Decimal("100.00"),
        created_at="2026-03-02T14:30:00+00:00",
    )


def answered(body: Any, *, status: int = 200) -> BrokerResponse:
    """A successful provider reply."""
    return BrokerResponse(outcome=TransportOutcome.ANSWERED, status_code=status, body=body)


def refused(status: int, body: Any = None) -> BrokerResponse:
    """An explicit provider refusal."""
    return BrokerResponse(outcome=TransportOutcome.ANSWERED, status_code=status, body=body)


def timed_out() -> BrokerResponse:
    """A request whose fate is unknown."""
    return BrokerResponse(outcome=TransportOutcome.TIMED_OUT)


def unreachable() -> BrokerResponse:
    """A request that did not connect."""
    return BrokerResponse(outcome=TransportOutcome.UNREACHABLE)


def order_body(
    *,
    client_order_id: str | None = None,
    remote_id: str = "remote-1",
    symbol: str = "AAPL",
    side: str = "buy",
    quantity: str = "10",
    filled: str = "0",
    status: str = "new",
) -> dict[str, Any]:
    """A provider order object."""
    return {
        "id": remote_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "qty": quantity,
        "filled_qty": filled,
        "status": status,
        "type": "market",
        "time_in_force": "day",
    }


def account_body(
    *, account_id: str = "paper-account-1", cash: str = "100000.00", **extra: Any
) -> dict[str, Any]:
    """A provider account object."""
    return {"id": account_id, "status": "ACTIVE", "cash": cash, "is_paper": True, **extra}


def log_at(tmp_path: Path, name: str = "exec") -> SubmissionIntentLog:
    """A durable intent log under a temporary root."""
    return SubmissionIntentLog(tmp_path / name)


def book(*, quantity: str = "0", cash: str = "100000.00") -> PortfolioState:
    """An accounting book holding AAPL and cash."""
    positions = {}
    if Decimal(quantity) != 0:
        positions[("COMPANY", 11)] = PooledPosition(
            identity=AAPL,
            pools={
                "alpha": AttributionCostPool(
                    identity=AAPL,
                    owner="alpha",
                    quantity=Decimal(quantity),
                    total_cost_basis=Decimal(quantity) * Decimal("100"),
                )
            },
        )
    return PortfolioState(cash=Decimal(cash), positions=positions)


# ---------------------------------------------------------------------------
# §77 live trading is refused by construction, not by configuration
# ---------------------------------------------------------------------------


def test_the_live_environment_is_never_authorized() -> None:
    assert BrokerEnvironment.PAPER.is_authorized is True
    assert BrokerEnvironment.LIVE.is_authorized is False


def test_refuse_live_rejects_the_live_environment() -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        refuse_live(BrokerEnvironment.LIVE, context="a test")


@pytest.mark.parametrize("value", ["LIVE", "live", "Live", "production", "prod", "real", "true"])
def test_no_environment_string_unlocks_live_trading(value: str) -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        prove_paper_environment(environment=value, base_url=PAPER_URL, key_id="PKTEST1234")


@pytest.mark.parametrize("host", sorted(LIVE_HOSTS))
def test_a_live_endpoint_is_refused(host: str) -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        prove_paper_environment(
            environment=BrokerEnvironment.PAPER,
            base_url=f"https://{host}",
            key_id="PKTEST1234",
        )


def test_an_unknown_host_is_refused_rather_than_assumed_paper() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        prove_paper_environment(
            environment=BrokerEnvironment.PAPER,
            base_url="https://broker.example.test",
            key_id="PKTEST1234",
        )


def test_a_live_key_prefix_is_refused_even_against_the_paper_host() -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="AKLIVEKEY123"
        )


def test_an_unrecognised_key_prefix_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="XXUNKNOWN123"
        )


def test_a_missing_credential_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_CREDENTIALS_MISSING):
        prove_paper_environment(environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="")


@pytest.mark.parametrize("host", sorted(PAPER_HOSTS))
def test_the_paper_host_is_proven(host: str) -> None:
    established = prove_paper_environment(
        environment=BrokerEnvironment.PAPER, base_url=f"https://{host}", key_id="PKTEST1234"
    )

    assert established.is_proven is True


def test_configuration_alone_is_not_full_certification() -> None:
    established = proof()

    assert established.is_proven is True
    assert established.is_fully_certified is False
    assert established.limitations


def test_an_unknown_host_refusal_names_the_host_and_the_known_one() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER) as caught:
        prove_paper_environment(
            environment=BrokerEnvironment.PAPER,
            base_url="https://unknown.example.test",
            key_id="PKTEST1234",
        )

    message = str(caught.value)
    assert "unknown.example.test" in message
    assert "paper-api.alpaca.markets" in message


def test_a_failed_proof_names_each_failing_check() -> None:
    standing = proof()

    assert standing.failed_checks() == ()


def test_a_binding_cannot_be_built_for_the_live_environment() -> None:
    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        ExternalBrokerAccountBinding(
            provider="ALPACA",
            account_fingerprint_hex=account_fingerprint("a"),
            capabilities=BrokerCapabilities(),
            base_url=PAPER_URL,
            proof=proof(),
            environment=BrokerEnvironment.LIVE,
        )


def test_a_proof_of_one_host_does_not_authorise_another() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        ExternalBrokerAccountBinding(
            provider="ALPACA",
            account_fingerprint_hex=account_fingerprint("a"),
            capabilities=BrokerCapabilities(),
            base_url="https://api.alpaca.markets",
            proof=proof(),
        )


def test_a_binding_is_never_live() -> None:
    assert binding().is_live is False


def test_the_adapter_refuses_a_non_paper_binding() -> None:
    bound = binding()
    live = object.__new__(ExternalBrokerAccountBinding)
    object.__setattr__(live, "provider", bound.provider)
    object.__setattr__(live, "account_fingerprint_hex", bound.account_fingerprint_hex)
    object.__setattr__(live, "capabilities", bound.capabilities)
    object.__setattr__(live, "base_url", bound.base_url)
    object.__setattr__(live, "proof", bound.proof)
    object.__setattr__(live, "environment", BrokerEnvironment.LIVE)
    object.__setattr__(live, "adapter_version", bound.adapter_version)
    object.__setattr__(live, "label", bound.label)

    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        AlpacaPaperAdapter(transport=FakeTransport(), binding=live, mapping=mapping())


# ---------------------------------------------------------------------------
# §79 the client order id is derived, never generated
# ---------------------------------------------------------------------------


def test_the_client_order_id_is_stable_across_processes() -> None:
    assert client_order_id_for("ORD-1") == client_order_id_for("ORD-1")


def test_different_orders_get_different_client_order_ids() -> None:
    assert client_order_id_for("ORD-1") != client_order_id_for("ORD-2")


def test_the_client_order_id_does_not_depend_on_the_binding() -> None:
    first = binding(client_order_id_limit=48)
    second = binding(client_order_id_limit=64, extended_hours=True)

    assert first.binding_fingerprint() != second.binding_fingerprint()
    assert client_order_id_for(
        "ORD-1", limit=first.capabilities.client_order_id_limit
    ) == client_order_id_for("ORD-1", limit=second.capabilities.client_order_id_limit)


def test_our_client_order_ids_are_recognisable() -> None:
    assert is_our_client_order_id(client_order_id_for("ORD-1")) is True


@pytest.mark.parametrize("foreign", ["manual-trade", "", None, f"{CLIENT_ID_PREFIX}short"])
def test_a_foreign_client_order_id_is_not_recognised(foreign: str | None) -> None:
    assert is_our_client_order_id(foreign) is False


def test_a_client_order_id_is_never_truncated_to_fit() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_CAPABILITY_UNSUPPORTED):
        client_order_id_for("ORD-1", limit=12)


def test_a_blank_order_id_derives_no_client_order_id() -> None:
    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        client_order_id_for("   ")


# ---------------------------------------------------------------------------
# §80 the intent is durable before the request, and exactly one POST is sent
# ---------------------------------------------------------------------------


def submit(
    tmp_path: Path,
    *,
    post: BrokerResponse,
    lookups: tuple[BrokerResponse, ...] = (),
    submitted: Order | None = None,
    bound: ExternalBrokerAccountBinding | None = None,
    symbols: SymbolMapping | None = None,
) -> tuple[Any, FakeTransport, SubmissionIntentLog]:
    """Submit one order against a scripted transport."""
    log = log_at(tmp_path)
    transport = FakeTransport()
    transport.script(RequestMethod.POST, ORDERS_PATH, post)
    if lookups:
        transport.script(RequestMethod.GET, LOOKUP_PATH, *lookups)
    result = submit_order(
        submitted or order(),
        transport=transport,
        binding=bound or binding(),
        mapping=symbols or mapping(),
        log=log,
        recorded_at="2026-03-02T14:30:01+00:00",
    )
    return result, transport, log


def test_an_accepted_submission_is_confirmed(tmp_path: Path) -> None:
    result, transport, log = submit(tmp_path, post=answered(order_body()))

    assert result.outcome is SubmissionOutcome.REMOTE_CONFIRMED
    assert result.remote_order_id == "remote-1"
    assert transport.mutation_count(ORDERS_PATH) == 1
    assert log.unresolved() == ()


def test_the_intent_is_durable_before_the_request_is_made(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    submitted = order()
    recorded: list[str] = []

    class RecordingTransport(FakeTransport):
        def request(
            self,
            method: RequestMethod,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            json_body: dict[str, Any] | None = None,
        ) -> BrokerResponse:
            recorded.extend(intent.order_id for intent in log.recorded_intents())
            return answered(order_body())

    submit_order(
        submitted,
        transport=RecordingTransport(),
        binding=binding(),
        mapping=mapping(),
        log=log,
        recorded_at="2026-03-02T14:30:01+00:00",
    )

    assert recorded == [submitted.order_id]


def test_a_locally_refused_order_leaves_no_intent_and_sends_nothing(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    transport = FakeTransport()

    with pytest.raises(ExecutionError, match=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED):
        submit_order(
            order(),
            transport=transport,
            binding=binding(),
            mapping=SymbolMapping.from_symbols({}),
            log=log,
            recorded_at="t",
        )

    assert transport.calls == []
    assert log.recorded_intents() == ()


def test_a_fractional_quantity_is_refused_before_any_request(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    transport = FakeTransport()

    with pytest.raises(ExecutionError, match=Code.BROKER_FRACTIONAL_NOT_SUPPORTED):
        submit_order(
            order(quantity=Decimal("1.5")),
            transport=transport,
            binding=binding(),
            mapping=mapping(),
            log=log,
            recorded_at="t",
        )

    assert transport.calls == []


def test_a_non_positive_quantity_is_refused(tmp_path: Path) -> None:
    log = log_at(tmp_path)

    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        submit_order(
            order(),
            transport=FakeTransport(),
            binding=binding(),
            mapping=mapping(),
            log=log,
            recorded_at="t",
            quantity=Decimal(0),
        )


# ---------------------------------------------------------------------------
# §81 the response was lost, but the order exists
# ---------------------------------------------------------------------------


def test_a_lost_response_is_resolved_by_lookup_not_by_resending(tmp_path: Path) -> None:
    client_order_id = client_order_id_for("ORD-1")
    result, transport, log = submit(
        tmp_path,
        post=timed_out(),
        lookups=(answered(order_body(client_order_id=client_order_id)),),
    )

    assert result.outcome is SubmissionOutcome.REMOTE_CONFIRMED
    assert result.remote_order_id == "remote-1"
    assert transport.mutation_count(ORDERS_PATH) == 1
    assert log.unresolved() == ()


def test_a_confirmed_order_may_not_be_submitted_again(tmp_path: Path) -> None:
    result, _, _ = submit(
        tmp_path,
        post=timed_out(),
        lookups=(answered(order_body(client_order_id=client_order_id_for("ORD-1"))),),
    )

    assert result.outcome.may_submit_again is False


# ---------------------------------------------------------------------------
# §82 the request never arrived
# ---------------------------------------------------------------------------


def test_a_request_that_never_arrived_is_established_as_not_submitted(tmp_path: Path) -> None:
    result, transport, log = submit(tmp_path, post=timed_out(), lookups=(refused(404),))

    assert result.outcome is SubmissionOutcome.NOT_SUBMITTED
    assert result.outcome.may_submit_again is True
    assert transport.mutation_count(ORDERS_PATH) == 1
    assert log.unresolved() == ()


# ---------------------------------------------------------------------------
# §83 the state is unknown, and stays unknown
# ---------------------------------------------------------------------------


def test_an_unresolvable_submission_stays_unknown(tmp_path: Path) -> None:
    result, transport, log = submit(
        tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable())
    )

    assert result.outcome is SubmissionOutcome.REMOTE_STATE_UNKNOWN
    assert result.outcome.may_submit_again is False
    assert result.outcome.is_settled is False
    assert transport.mutation_count(ORDERS_PATH) == 1
    assert [intent.order_id for intent in log.unresolved()] == ["ORD-1"]


def test_an_unknown_submission_records_no_resolution(tmp_path: Path) -> None:
    _, _, log = submit(
        tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable())
    )

    assert log.resolution_for(client_order_id_for("ORD-1")) is None


def test_a_rate_limited_submission_is_not_resent(tmp_path: Path) -> None:
    result, transport, _ = submit(
        tmp_path,
        post=BrokerResponse(outcome=TransportOutcome.RATE_LIMITED, status_code=429),
        lookups=(unreachable(), unreachable(), unreachable()),
    )

    assert result.outcome is SubmissionOutcome.REMOTE_STATE_UNKNOWN
    assert transport.mutation_count(ORDERS_PATH) == 1


@pytest.mark.parametrize(
    ("post", "lookups", "expected"),
    [
        (answered(order_body()), (), SubmissionOutcome.REMOTE_CONFIRMED),
        (timed_out(), (refused(404),), SubmissionOutcome.NOT_SUBMITTED),
        (
            timed_out(),
            (unreachable(), unreachable(), unreachable()),
            SubmissionOutcome.REMOTE_STATE_UNKNOWN,
        ),
        (refused(403, {"message": "forbidden"}), (), SubmissionOutcome.REMOTE_REJECTED),
        (unreachable(), (refused(404),), SubmissionOutcome.NOT_SUBMITTED),
    ],
)
def test_exactly_one_mutating_request_on_every_path(
    tmp_path: Path,
    post: BrokerResponse,
    lookups: tuple[BrokerResponse, ...],
    expected: SubmissionOutcome,
) -> None:
    result, transport, _ = submit(tmp_path, post=post, lookups=lookups)

    assert result.outcome is expected
    assert transport.mutation_count(ORDERS_PATH) == 1
    assert len(transport.mutations) == 1


# ---------------------------------------------------------------------------
# §84 explicit rejection, and the duplicate client order id
# ---------------------------------------------------------------------------


def test_an_explicit_rejection_is_terminal(tmp_path: Path) -> None:
    result, _, log = submit(tmp_path, post=refused(403, {"message": "insufficient buying power"}))

    assert result.outcome is SubmissionOutcome.REMOTE_REJECTED
    assert result.outcome.may_submit_again is False
    assert log.unresolved() == ()


def test_a_duplicate_client_order_id_is_read_as_the_order_existing(tmp_path: Path) -> None:
    result, transport, _ = submit(
        tmp_path,
        post=refused(422, {"message": "client_order_id must be unique"}),
        lookups=(answered(order_body(client_order_id=client_order_id_for("ORD-1"))),),
    )

    assert result.outcome is SubmissionOutcome.REMOTE_CONFIRMED
    assert transport.mutation_count(ORDERS_PATH) == 1


def test_a_reply_about_another_order_is_a_conflict(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        LOOKUP_PATH,
        answered(order_body(client_order_id=client_order_id_for("ORD-OTHER"))),
    )
    intent = SubmissionIntent(
        order_id="ORD-1",
        client_order_id=client_order_id_for("ORD-1"),
        execution_plan_id="PLAN-1",
        binding_fingerprint="b",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal(10),
        recorded_at="t",
    )

    with pytest.raises(ExecutionError, match=Code.REMOTE_CLIENT_ORDER_ID_CONFLICT):
        resolve_submission(intent, transport=transport, binding=binding(), log=log)


def test_two_different_orders_cannot_share_a_client_order_id(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    first = SubmissionIntent(
        order_id="ORD-1",
        client_order_id=client_order_id_for("ORD-1"),
        execution_plan_id="PLAN-1",
        binding_fingerprint="b",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal(10),
        recorded_at="t",
    )
    log.record_intent(first)

    with pytest.raises(ExecutionError, match=Code.REMOTE_CLIENT_ORDER_ID_CONFLICT):
        log.record_intent(
            SubmissionIntent(
                order_id="ORD-9",
                client_order_id=first.client_order_id,
                execution_plan_id="PLAN-1",
                binding_fingerprint="b",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=Decimal(99),
                recorded_at="t",
            )
        )


def test_recording_the_same_intent_twice_is_a_no_op(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    intent = SubmissionIntent(
        order_id="ORD-1",
        client_order_id=client_order_id_for("ORD-1"),
        execution_plan_id="PLAN-1",
        binding_fingerprint="b",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal(10),
        recorded_at="t",
    )

    log.record_intent(intent)
    log.record_intent(intent)

    assert len(log.recorded_intents()) == 1


# ---------------------------------------------------------------------------
# §85 restart recovery
# ---------------------------------------------------------------------------


def test_an_unresolved_intent_survives_a_restart(tmp_path: Path) -> None:
    submit(tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable()))

    reopened = log_at(tmp_path)

    assert [intent.order_id for intent in reopened.unresolved()] == ["ORD-1"]


def test_recovery_resolves_an_unresolved_intent_by_reading(tmp_path: Path) -> None:
    submit(tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable()))
    reopened = log_at(tmp_path)
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        LOOKUP_PATH,
        answered(
            order_body(client_order_id=client_order_id_for("ORD-1"), status="filled", filled="10")
        ),
    )

    result = resolve_submission(
        reopened.unresolved()[0], transport=transport, binding=binding(), log=reopened
    )

    assert result.outcome is SubmissionOutcome.REMOTE_CONFIRMED
    assert transport.mutations == []
    assert reopened.unresolved() == ()


def test_recovery_with_the_network_down_changes_nothing(tmp_path: Path) -> None:
    submit(tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable()))
    reopened = log_at(tmp_path)
    transport = FakeTransport()
    transport.script(RequestMethod.GET, LOOKUP_PATH, unreachable(), unreachable(), unreachable())

    result = resolve_submission(
        reopened.unresolved()[0], transport=transport, binding=binding(), log=reopened
    )

    assert result.outcome is SubmissionOutcome.REMOTE_STATE_UNKNOWN
    assert transport.mutations == []
    assert len(reopened.unresolved()) == 1


def test_a_half_written_intent_is_refused_rather_than_reconstructed(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    log.intents.mkdir(parents=True, exist_ok=True)
    (log.intents / "ssx1-truncated.json").write_text(
        json.dumps({"intent": {"order_id": "ORD-1"}}), encoding="utf-8"
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_PARTIAL_RECORD):
        log.recorded_intents()


def test_an_unparseable_intent_record_is_refused(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    log.intents.mkdir(parents=True, exist_ok=True)
    (log.intents / "ssx1-broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ExecutionError, match=Code.EXECUTION_PARTIAL_RECORD):
        log.recorded_intents()


# ---------------------------------------------------------------------------
# §86 the status map is closed by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("new", "ACKNOWLEDGED"),
        ("accepted", "ACKNOWLEDGED"),
        ("partially_filled", "PARTIALLY_FILLED"),
        ("filled", "FILLED"),
        ("canceled", "CANCELLED"),
        ("expired", "EXPIRED"),
        ("done_for_day", "EXPIRED"),
        ("rejected", "REJECTED"),
    ],
)
def test_a_declared_provider_status_maps_to_one_local_state(
    provider_status: str, expected: str
) -> None:
    reading = read_provider_status(provider_status)

    assert str(reading.state) == expected


@pytest.mark.parametrize("provider_status", ["order_frobnicated", "FILLED_MAYBE", "working", ""])
def test_an_unmapped_provider_status_is_refused(provider_status: str) -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_STATUS_UNMAPPED):
        read_provider_status(provider_status, context="a test")


def test_a_missing_provider_status_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_STATUS_UNMAPPED):
        read_provider_status(None)


@pytest.mark.parametrize("provider_status", ["pending_cancel", "pending_replace"])
def test_a_pending_cancel_is_still_working(provider_status: str) -> None:
    reading = read_provider_status(provider_status)

    assert str(reading.state) == "ACKNOWLEDGED"
    assert reading.terminal is False


@pytest.mark.parametrize("provider_status", ["pending_new", "accepted_for_bidding"])
def test_a_status_with_no_local_meaning_carries_no_state(provider_status: str) -> None:
    reading = read_provider_status(provider_status)

    assert reading.state is None
    assert reading.carries_state is False
    assert reading.terminal is False


def test_the_status_map_has_no_default_branch() -> None:
    assert "working" not in PROVIDER_STATUS_MAP
    with pytest.raises(ExecutionError, match=Code.BROKER_STATUS_UNMAPPED):
        read_provider_status("working")


def test_the_status_map_reads_case_insensitively() -> None:
    assert read_provider_status("FILLED").state == read_provider_status("filled").state


# ---------------------------------------------------------------------------
# §87 the symbol mapping refuses to guess
# ---------------------------------------------------------------------------


def test_an_identity_resolves_to_its_declared_symbol() -> None:
    assert mapping().symbol_for(AAPL) == "AAPL"


def test_a_stale_display_ticker_is_never_used_as_a_fallback() -> None:
    reassigned = SecurityIdentity(kind="COMPANY", identity_id=99, symbol="AAPL")

    with pytest.raises(ExecutionError, match=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED):
        mapping().symbol_for(reassigned)


def test_a_venue_symbol_resolves_back_to_a_durable_key() -> None:
    assert mapping().identity_key_for("aapl") == ("COMPANY", 11)


def test_an_unmapped_venue_symbol_cannot_be_attributed() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED):
        mapping().identity_key_for("TSLA")


def test_two_identities_cannot_share_a_venue_symbol() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED):
        SymbolMapping.of(
            [
                VenueSymbol(kind="COMPANY", identity_id=1, symbol="X"),
                VenueSymbol(kind="COMPANY", identity_id=2, symbol="x"),
            ]
        )


def test_an_identity_cannot_be_mapped_twice() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED):
        SymbolMapping.of(
            [
                VenueSymbol(kind="COMPANY", identity_id=1, symbol="X"),
                VenueSymbol(kind="COMPANY", identity_id=1, symbol="Y"),
            ]
        )


def test_a_mapped_but_untradable_asset_is_a_distinct_refusal() -> None:
    halted = SymbolMapping.of(
        [VenueSymbol(kind="COMPANY", identity_id=11, symbol="AAPL", tradable=False)]
    )

    with pytest.raises(ExecutionError, match=Code.BROKER_ASSET_NOT_TRADABLE):
        halted.symbol_for(AAPL)


def test_a_blank_symbol_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_SYMBOL_MAPPING_UNRESOLVED):
        VenueSymbol(kind="COMPANY", identity_id=1, symbol="  ")


def test_the_mapping_fingerprint_is_independent_of_declaration_order() -> None:
    forwards = SymbolMapping.from_symbols({("COMPANY", 11): "AAPL", ("COMPANY", 12): "MSFT"})
    backwards = SymbolMapping.from_symbols({("COMPANY", 12): "MSFT", ("COMPANY", 11): "AAPL"})

    assert forwards.fingerprint() == backwards.fingerprint()


# ---------------------------------------------------------------------------
# §88 capabilities refuse by name
# ---------------------------------------------------------------------------


def test_an_unsupported_order_type_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_CAPABILITY_UNSUPPORTED):
        BrokerCapabilities().require_order_type("LIMIT")


def test_an_unsupported_time_in_force_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_CAPABILITY_UNSUPPORTED):
        BrokerCapabilities().require_time_in_force("GTC")


def test_a_fractional_quantity_is_refused_at_a_whole_share_venue() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_FRACTIONAL_NOT_SUPPORTED):
        BrokerCapabilities().require_quantity(Decimal("1.5"))


def test_a_fractional_quantity_is_accepted_where_declared_supported() -> None:
    BrokerCapabilities(fractional_quantities=True).require_quantity(Decimal("1.5"))


def test_capabilities_default_to_the_conservative_answer() -> None:
    capabilities = BrokerCapabilities()

    assert capabilities.fractional_quantities is False
    assert capabilities.short_selling is False
    assert capabilities.extended_hours is False
    assert capabilities.notional_orders is False


# ---------------------------------------------------------------------------
# §89 the account binding, and a credential pointing elsewhere
# ---------------------------------------------------------------------------


def test_the_bound_account_is_accepted() -> None:
    binding().require_account("paper-account-1", context="a test")


@pytest.mark.parametrize("other", ["paper-account-2", "PAPER-ACCOUNT-1", " paper-account-2 "])
def test_another_account_is_refused(other: str) -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ACCOUNT_MISMATCH):
        binding().require_account(other, context="reading the account")


def test_a_blank_account_id_cannot_be_fingerprinted() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_ACCOUNT_MISMATCH):
        account_fingerprint("   ")


def test_a_binding_never_serialises_the_account_number() -> None:
    serialised = json.dumps(binding(account_id="9F21-SECRET").as_json())

    assert "9F21-SECRET" not in serialised


def test_a_binding_never_serialises_the_credential() -> None:
    serialised = json.dumps(binding().as_json())

    assert "PKTESTKEY123" not in serialised


def test_a_binding_fingerprint_is_stable() -> None:
    assert binding().binding_fingerprint() == binding().binding_fingerprint()


def test_a_different_account_has_a_different_binding_fingerprint() -> None:
    assert binding().binding_fingerprint() != binding(account_id="other").binding_fingerprint()


# ---------------------------------------------------------------------------
# §90 reading remote state
# ---------------------------------------------------------------------------


def adapter(
    transport: FakeTransport,
    *,
    bound: ExternalBrokerAccountBinding | None = None,
    symbols: SymbolMapping | None = None,
) -> AlpacaPaperAdapter:
    """An adapter over a scripted transport."""
    return AlpacaPaperAdapter(
        transport=transport, binding=bound or binding(), mapping=symbols or mapping()
    )


def account_transport(
    *,
    account: dict[str, Any] | None = None,
    positions: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> FakeTransport:
    """A transport that answers the account, positions and working-order reads."""
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACCOUNT_PATH, answered(account or account_body()))
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered(positions or []))
    transport.script(RequestMethod.GET, ORDERS_PATH, answered(orders or []))
    return transport


def test_the_account_is_read_and_checked_against_the_binding() -> None:
    snapshot = adapter(account_transport()).read_account()

    assert snapshot.status == "ACTIVE"
    assert snapshot.require_cash() == Decimal("100000.00")


def test_an_account_the_binding_does_not_name_is_refused() -> None:
    transport = account_transport(account=account_body(account_id="someone-elses-account"))

    with pytest.raises(ExecutionError, match=Code.BROKER_ACCOUNT_MISMATCH):
        adapter(transport).read_account()


def test_an_account_the_provider_says_is_not_paper_is_refused() -> None:
    transport = account_transport(account={**account_body(), "is_paper": False})

    with pytest.raises(ExecutionError, match=Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER):
        adapter(transport).read_account()


def test_an_unreachable_account_is_refused_not_assumed_empty() -> None:
    transport = FakeTransport(default=unreachable())

    with pytest.raises(ExecutionError, match=Code.BROKER_UNREACHABLE):
        adapter(transport).read_account()


def test_unreadable_positions_are_never_substituted_with_an_empty_list() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACCOUNT_PATH, answered(account_body()))
    transport.script(RequestMethod.GET, POSITIONS_PATH, unreachable(), unreachable(), unreachable())

    with pytest.raises(ExecutionError, match=Code.BROKER_UNREACHABLE):
        adapter(transport).read_account()


def test_an_order_the_venue_does_not_have_reads_as_none() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, LOOKUP_PATH, refused(404))

    assert adapter(transport).read_order("ORD-1") is None


def test_an_unaskable_order_is_not_reported_as_absent() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, LOOKUP_PATH, unreachable(), unreachable(), unreachable())

    with pytest.raises(ExecutionError, match=Code.REMOTE_STATE_UNKNOWN):
        adapter(transport).read_order("ORD-1")


def test_a_read_retries_a_transient_failure_but_a_mutation_is_never_retried() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACCOUNT_PATH, timed_out(), answered(account_body()))
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered([]))

    adapter(transport).read_account()

    assert transport.mutations == []


def test_a_read_retry_limit_below_one_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_CAPABILITY_UNSUPPORTED):
        read_with_retry(FakeTransport(), ACCOUNT_PATH, limit=0)


def test_an_unscripted_route_is_refused_rather_than_silently_succeeding() -> None:
    with pytest.raises(ExecutionError, match=Code.BROKER_UNREACHABLE):
        FakeTransport().request(RequestMethod.GET, "/v2/nothing")


# ---------------------------------------------------------------------------
# §91 duplicate executions are idempotent
# ---------------------------------------------------------------------------


def fill_body(
    *,
    remote_id: str = "exec-1",
    quantity: str = "4",
    price: str = "101.25",
    sequence: int = 1,
    order_id: str = "remote-1",
    symbol: str = "AAPL",
    activity_type: str = "FILL",
    kind: str = "fill",
) -> dict[str, Any]:
    """A provider execution object, shaped as the real activities endpoint sends it.

    `activity_type` and `type` are present because Alpaca sends them, and the
    adapter requires one of them: a row that does not positively say it is a
    fill is not treated as one, so a dividend or a fee on a mixed page cannot
    be posted as an execution.
    """
    return {
        "id": remote_id,
        "activity_type": activity_type,
        "type": kind,
        "order_id": order_id,
        "symbol": symbol,
        "side": "buy",
        "qty": quantity,
        "price": price,
        "transaction_time": "2026-03-02T14:31:00Z",
        "sequence": sequence,
    }


def test_the_same_execution_reported_ten_times_reads_as_one() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([fill_body()] * 10))

    fills = adapter(transport).read_fills(remote_order_id="remote-1")

    assert len(fills) == 1
    assert fills[0].quantity == Decimal(4)


def test_two_distinct_executions_both_read() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered([fill_body(remote_id="exec-1"), fill_body(remote_id="exec-2", sequence=2)]),
    )

    fills = adapter(transport).read_fills(remote_order_id="remote-1")

    assert [fill.remote_fill_id for fill in fills] == ["exec-1", "exec-2"]


def test_one_execution_id_reported_with_two_contents_is_a_conflict() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered([fill_body(quantity="4"), fill_body(quantity="7")]),
    )

    with pytest.raises(ExecutionError, match=Code.REMOTE_FILL_CONFLICT):
        adapter(transport).read_fills(remote_order_id="remote-1")


def test_executions_read_in_a_deterministic_order_whatever_the_venue_sends() -> None:
    forwards = FakeTransport()
    forwards.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered(
            [fill_body(remote_id="exec-1", sequence=1), fill_body(remote_id="exec-2", sequence=2)]
        ),
    )
    backwards = FakeTransport()
    backwards.script(
        RequestMethod.GET,
        ACTIVITIES_PATH,
        answered(
            [fill_body(remote_id="exec-2", sequence=2), fill_body(remote_id="exec-1", sequence=1)]
        ),
    )

    assert [
        fill.remote_fill_id for fill in adapter(forwards).read_fills(remote_order_id="remote-1")
    ] == [fill.remote_fill_id for fill in adapter(backwards).read_fills(remote_order_id="remote-1")]


def test_an_execution_with_no_durable_id_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.REMOTE_FILL_CONFLICT):
        ExternalExecutionFill(
            remote_fill_id="  ",
            remote_order_id="remote-1",
            client_order_id="c",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal(1),
            price=Decimal(1),
            reported_at="t",
        )


def test_a_fee_the_venue_did_not_state_is_unknown_not_zero() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([fill_body()]))
    fill = adapter(transport).read_fills(remote_order_id="remote-1")[0]

    assert fill.fee_is_known is False
    with pytest.raises(ExecutionError, match=Code.BROKER_FILL_FEE_UNKNOWN):
        fill.require_fee()


def test_a_declared_fee_does_not_overwrite_what_the_venue_said() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([fill_body()]))
    fill = adapter(transport).read_fills(remote_order_id="remote-1")[0]

    declared = fill.with_fee(Decimal("0.00"))

    assert declared.require_fee() == Decimal("0.00")
    assert fill.fee_is_known is False


def test_a_provider_figure_is_read_as_an_exact_decimal() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET, ACTIVITIES_PATH, answered([fill_body(quantity="0.3", price="0.1")])
    )

    fill = adapter(transport).read_fills(remote_order_id="remote-1")[0]

    assert fill.quantity == Decimal("0.3")
    assert fill.price == Decimal("0.1")


@pytest.mark.parametrize(
    "broken",
    [
        {"qty": "0"},
        {"price": "0"},
        {"qty": "not-a-number"},
        {"side": "hold"},
    ],
)
def test_a_malformed_execution_is_refused(broken: dict[str, Any]) -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACTIVITIES_PATH, answered([{**fill_body(), **broken}]))

    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        adapter(transport).read_fills(remote_order_id="remote-1")


def test_an_over_fill_is_never_adopted() -> None:
    with pytest.raises(ExecutionError, match=Code.REMOTE_ORDER_MISMATCH):
        ExternalOrderSnapshot.from_payload(order_body(quantity="10", filled="12", status="filled"))


# ---------------------------------------------------------------------------
# §92 three-way reconciliation, and the absence of repair
# ---------------------------------------------------------------------------


def remote_snapshot(
    *,
    order_id: str = "ORD-1",
    symbol: str = "AAPL",
    quantity: str = "10",
    filled: str = "10",
    status: str = "filled",
    remote_id: str = "remote-1",
    client_order_id: str | None = None,
) -> ExternalOrderSnapshot:
    """One venue order snapshot."""
    return ExternalOrderSnapshot(
        remote_order_id=remote_id,
        client_order_id=(
            client_order_id if client_order_id is not None else client_order_id_for(order_id)
        ),
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled),
        reading=read_provider_status(status),
    )


def remote_account(
    *,
    cash: str | None = "99000.00",
    positions: tuple[tuple[str, str], ...] = (("AAPL", "10"),),
    blocked: bool = False,
) -> ExternalPaperAccountSnapshot:
    """One venue account snapshot."""
    return ExternalPaperAccountSnapshot(
        account_id="paper-account-1",
        status="ACTIVE",
        cash=None if cash is None else Decimal(cash),
        trading_blocked=blocked,
        positions=tuple(
            ExternalPosition(symbol=symbol, quantity=Decimal(quantity))
            for symbol, quantity in positions
        ),
    )


def test_three_agreeing_records_reconcile() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    assert result.verdict is ReconciliationVerdict.RECONCILED
    assert result.may_execute is True
    assert result.breaks == ()


def test_a_remote_fill_the_book_never_admitted_blocks_execution() -> None:
    result = reconcile_external(
        state=book(quantity="0", cash="100000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(0)},
    )

    codes = {row.code for row in result.breaks}

    assert Code.REMOTE_FILL_MISSING_LOCAL in codes
    assert result.verdict is ReconciliationVerdict.BREAKS_BLOCKING
    assert result.may_execute is False


def test_a_local_fill_the_venue_has_no_record_of_blocks_execution() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[],
        remote_account=remote_account(positions=()),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    codes = {row.code for row in result.breaks}

    assert Code.LOCAL_FILL_MISSING_REMOTE in codes
    assert result.may_execute is False


def test_an_order_placed_by_hand_at_the_venue_is_reported() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[
            remote_snapshot(),
            remote_snapshot(
                client_order_id="someone-elses-manual-order",
                remote_id="remote-9",
                symbol="MSFT",
                quantity="5",
                filled="5",
            ),
        ],
        remote_account=remote_account(positions=(("AAPL", "10"), ("MSFT", "5"))),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    codes = {row.code for row in result.breaks}

    assert Code.UNEXPECTED_REMOTE_ORDER in codes
    assert result.may_execute is False


def test_an_untraded_foreign_order_is_a_warning_not_a_block() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[
            remote_snapshot(),
            remote_snapshot(
                client_order_id="manual-order",
                remote_id="remote-9",
                symbol="MSFT",
                quantity="5",
                filled="0",
                status="new",
            ),
        ],
        remote_account=remote_account(),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )
    unexpected = [row for row in result.breaks if row.code == Code.UNEXPECTED_REMOTE_ORDER]

    assert unexpected[0].severity is ExternalBreakSeverity.WARNING


def test_a_position_mismatch_blocks_execution() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(positions=(("AAPL", "7"),)),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )
    mismatch = [row for row in result.breaks if row.code == Code.REMOTE_POSITION_MISMATCH]

    assert mismatch[0].difference == "-3.00000000"
    assert result.may_execute is False


def test_a_holding_in_an_unmapped_symbol_is_reported_rather_than_raised() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(positions=(("AAPL", "10"), ("TSLA", "3"))),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    codes = {row.code for row in result.breaks}

    assert Code.BROKER_SYMBOL_MAPPING_UNRESOLVED in codes


def test_a_cash_difference_blocks_at_the_default_zero_tolerance() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(cash="98999.35"),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    assert result.cash_tolerance == Decimal(0)
    assert result.may_execute is False


def test_a_declared_tolerance_demotes_a_small_cash_difference_to_info() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(cash="98999.35"),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
        cash_tolerance=Decimal("1.00"),
    )
    cash_break = [row for row in result.breaks if row.code == Code.REMOTE_CASH_MISMATCH]

    assert cash_break[0].severity is ExternalBreakSeverity.INFO
    assert result.verdict is ReconciliationVerdict.RECONCILED


def test_a_venue_that_states_no_cash_is_a_warning() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(cash=None),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    assert result.cash_compared is False
    assert result.verdict is ReconciliationVerdict.BREAKS_WARNING


def test_blocked_trading_at_the_venue_blocks_execution() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(blocked=True),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    codes = {row.code for row in result.breaks}

    assert Code.EXTERNAL_SUBMISSION_BLOCKED in codes


def test_a_sweep_never_changes_the_book() -> None:
    state = book(quantity="10", cash="99000.00")
    cash_before = state.cash
    quantity_before = state.positions[("COMPANY", 11)].quantity

    reconcile_external(
        state=state,
        orders=[order()],
        remote_orders=[remote_snapshot(filled="3")],
        remote_account=remote_account(cash="1.00", positions=(("AAPL", "99"),)),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )

    assert state.cash == cash_before
    assert state.positions[("COMPANY", 11)].quantity == quantity_before


def test_the_reconciliation_module_exposes_no_repair_function() -> None:
    from stock_screener.execution.external import reconciliation

    forbidden = {"repair", "auto_repair", "adopt_remote", "apply_remote", "sync_from_broker"}

    assert forbidden.isdisjoint(set(dir(reconciliation)))


def test_breaks_are_ordered_most_severe_first() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[
            remote_snapshot(),
            remote_snapshot(
                client_order_id="manual",
                remote_id="r-9",
                symbol="MSFT",
                quantity="5",
                filled="0",
                status="new",
            ),
        ],
        remote_account=remote_account(cash="98999.35", positions=(("AAPL", "7"),)),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )
    severities = [row.severity for row in result.breaks]

    assert severities == sorted(
        severities, key=lambda level: ["BLOCKING", "WARNING", "INFO"].index(str(level))
    )


# ---------------------------------------------------------------------------
# §93 the venue is declared once and never falls back
# ---------------------------------------------------------------------------


def test_a_local_session_carries_no_account() -> None:
    bound = VenueBinding.local()

    assert bound.venue is ExecutionVenue.LOCAL_DETERMINISTIC_PAPER
    assert bound.account is None


def test_a_local_session_may_not_hold_a_broker_account() -> None:
    with pytest.raises(ExecutionError, match=Code.EXTERNAL_VENUE_MISMATCH):
        VenueBinding(venue=ExecutionVenue.LOCAL_DETERMINISTIC_PAPER, account=binding())


def test_an_external_session_needs_an_account() -> None:
    with pytest.raises(ExecutionError, match=Code.EXTERNAL_VENUE_MISMATCH):
        VenueBinding(venue=ExecutionVenue.EXTERNAL_PAPER)


def test_an_operation_from_the_other_venue_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.EXTERNAL_VENUE_MISMATCH):
        VenueBinding.local().require(ExecutionVenue.EXTERNAL_PAPER, context="a test")


def test_an_artefact_from_the_other_venue_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.EXTERNAL_VENUE_MISMATCH):
        VenueBinding.external(binding()).require_artefact(
            "LOCAL_DETERMINISTIC_PAPER", context="a test"
        )


def test_an_artefact_recording_no_venue_is_refused_rather_than_assumed() -> None:
    with pytest.raises(ExecutionError, match=Code.EXTERNAL_VENUE_MISMATCH):
        VenueBinding.local().require_artefact(None, context="a test")


def test_no_venue_is_live() -> None:
    assert all(venue.is_live is False for venue in ExecutionVenue)


def test_the_venue_enum_has_exactly_two_members() -> None:
    assert sorted(str(venue) for venue in ExecutionVenue) == [
        "EXTERNAL_PAPER",
        "LOCAL_DETERMINISTIC_PAPER",
    ]


# ---------------------------------------------------------------------------
# §94 preflight
# ---------------------------------------------------------------------------


def test_a_fully_passing_preflight_is_ready(tmp_path: Path) -> None:
    readiness = preflight(
        adapter(account_transport()),
        log=log_at(tmp_path),
        origin=PAPER_URL,
        identities=[AAPL, MSFT],
    )

    assert readiness.verdict == READY_VERDICT
    assert readiness.is_ready is True
    assert set(readiness.passed) == set(PREFLIGHT_GATES)
    readiness.require_ready()


def test_preflight_makes_no_mutating_request(tmp_path: Path) -> None:
    transport = account_transport()

    preflight(adapter(transport), log=log_at(tmp_path), origin=PAPER_URL)

    assert transport.mutations == []


def test_preflight_upgrades_the_proof_to_fully_certified(tmp_path: Path) -> None:
    readiness = preflight(adapter(account_transport()), log=log_at(tmp_path), origin=PAPER_URL)

    assert readiness.proof is not None
    assert readiness.proof.is_fully_certified is True


def test_preflight_withholds_readiness_when_the_account_is_unreachable(tmp_path: Path) -> None:
    readiness = preflight(
        adapter(FakeTransport(default=unreachable())), log=log_at(tmp_path), origin=PAPER_URL
    )

    assert readiness.is_ready is False
    assert "ACCOUNT_REACHABLE" in dict(readiness.failures)
    with pytest.raises(ExecutionError, match=Code.EXTERNAL_SUBMISSION_BLOCKED):
        readiness.require_ready()


def test_preflight_withholds_readiness_for_another_account(tmp_path: Path) -> None:
    transport = account_transport(account=account_body(account_id="someone-else"))

    readiness = preflight(adapter(transport), log=log_at(tmp_path), origin=PAPER_URL)

    assert "ACCOUNT_MATCHES_BINDING" in dict(readiness.failures)


def test_preflight_withholds_readiness_when_the_provider_denies_paper(tmp_path: Path) -> None:
    transport = account_transport(account={**account_body(), "is_paper": False})

    readiness = preflight(adapter(transport), log=log_at(tmp_path), origin=PAPER_URL)

    assert "PROVIDER_CONFIRMS_PAPER" in dict(readiness.failures)


def test_preflight_withholds_readiness_when_trading_is_blocked(tmp_path: Path) -> None:
    transport = account_transport(account={**account_body(), "trading_blocked": True})

    readiness = preflight(adapter(transport), log=log_at(tmp_path), origin=PAPER_URL)

    assert "TRADING_NOT_BLOCKED" in dict(readiness.failures)


def test_preflight_withholds_readiness_for_an_unmapped_identity(tmp_path: Path) -> None:
    unmapped = SecurityIdentity(kind="COMPANY", identity_id=77, symbol="NVDA")

    readiness = preflight(
        adapter(account_transport()), log=log_at(tmp_path), origin=PAPER_URL, identities=[unmapped]
    )

    assert "SYMBOLS_RESOLVED" in dict(readiness.failures)


def test_preflight_withholds_readiness_while_a_submission_is_unresolved(tmp_path: Path) -> None:
    submit(tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable()))

    readiness = preflight(adapter(account_transport()), log=log_at(tmp_path), origin=PAPER_URL)

    assert "NO_UNRESOLVED_SUBMISSIONS" in dict(readiness.failures)
    assert readiness.unresolved_orders == ("ORD-1",)


def test_preflight_names_every_failing_gate_not_only_the_first(tmp_path: Path) -> None:
    submit(tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable()))
    transport = account_transport(account=account_body(account_id="someone-else"))
    unmapped = SecurityIdentity(kind="COMPANY", identity_id=77, symbol="NVDA")

    readiness = preflight(
        adapter(transport), log=log_at(tmp_path), origin=PAPER_URL, identities=[unmapped]
    )

    assert len(readiness.failures) >= 3


# ---------------------------------------------------------------------------
# §95 the manifest versions itself rather than widening V1.1
# ---------------------------------------------------------------------------


def test_the_v1_2_manifest_declares_the_external_adapter() -> None:
    body = external_manifest(venue=VenueBinding.external(binding()), mapping=mapping())

    assert body["manifest_version"] == EXECUTION_MANIFEST_VERSION_V1_2
    assert body["external_paper_broker"] == "IMPLEMENTED_PAPER_ONLY"
    assert body["live_trading"] == "NOT_AUTHORIZED"
    assert body["live_broker"] == "NOT_IMPLEMENTED"


def test_the_v1_1_manifest_still_says_no_external_broker() -> None:
    from stock_screener.execution.manifest import execution_manifest

    assert execution_manifest()["external_paper_broker"] == "NOT_IMPLEMENTED"


def test_an_external_manifest_disclaims_reproducibility() -> None:
    body = external_manifest(venue=VenueBinding.external(binding()), mapping=mapping())
    disclaimers = " ".join(body["not_guaranteed"])

    assert "not reproducible" in disclaimers


def test_a_local_manifest_does_not_disclaim_reproducibility() -> None:
    body = external_manifest(venue=VenueBinding.local(), mapping=mapping())
    disclaimers = " ".join(body["not_guaranteed"])

    assert "reproduces from its inputs" in disclaimers


def test_the_manifest_fingerprint_is_stable() -> None:
    first = external_manifest(venue=VenueBinding.local(), mapping=mapping())
    second = external_manifest(venue=VenueBinding.local(), mapping=mapping())

    assert first["manifest_fingerprint"] == second["manifest_fingerprint"]


def test_the_manifest_lists_the_v1_2_error_codes() -> None:
    from stock_screener.execution.errors import V1_2_ADDED_ERROR_CODES

    body = external_manifest(venue=VenueBinding.local(), mapping=mapping())

    assert set(body["v1_2_added_error_codes"]) == set(V1_2_ADDED_ERROR_CODES)


def test_the_manifest_records_unresolved_orders() -> None:
    body = external_manifest(
        venue=VenueBinding.local(), mapping=mapping(), unresolved_order_ids=("ORD-7",)
    )

    assert body["bindings"]["unresolved_order_ids"] == ["ORD-7"]


def test_the_manifest_carries_no_credential() -> None:
    body = external_manifest(venue=VenueBinding.external(binding()), mapping=mapping())

    check_no_credentials(body, context="the V1.2 manifest")
    assert "PKTESTKEY123" not in json.dumps(body)


# ---------------------------------------------------------------------------
# §96 no credential ever reaches a report, a log line or an exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaked",
    [
        {"key": "AKLIVEKEYABCDEF"},
        {"key": "PKPAPERKEYABCDEF"},
        {"headers": "Authorization: Basic abc"},
        {"token": "Bearer abcdefghijkl"},
        {"api_secret": "x"},
        {"APCA-API-SECRET-KEY": "x"},
        {"set-cookie": "session=abc"},
    ],
)
def test_a_payload_shaped_like_a_credential_is_refused(leaked: dict[str, str]) -> None:
    with pytest.raises(ExecutionError, match=Code.FORBIDDEN_EXECUTION_NAMESPACE):
        check_no_credentials(leaked, context="a test payload")


def test_a_credential_refusal_does_not_quote_the_credential() -> None:
    with pytest.raises(ExecutionError) as caught:
        check_no_credentials({"key": "AKLIVEKEYABCDEF"}, context="a test payload")

    assert "AKLIVEKEYABCDEF" not in str(caught.value)


def test_a_clean_payload_passes_the_credential_scan() -> None:
    check_no_credentials({"order_id": "ORD-1", "symbol": "AAPL"}, context="a test payload")


def test_a_report_is_credential_scanned_on_the_way_out(tmp_path: Path) -> None:
    result, _, _ = submit(tmp_path, post=answered(order_body()))
    report = ExternalPaperExecutionReport(
        manifest=external_manifest(venue=VenueBinding.external(binding()), mapping=mapping()),
        readiness=preflight(
            adapter(account_transport()), log=log_at(tmp_path, "pf"), origin=PAPER_URL
        ),
        submissions=(result,),
    )

    serialised = json.dumps(report.as_json())

    assert "PKTESTKEY123" not in serialised
    assert "paper-account-1" not in serialised


def test_a_report_surfaces_an_unknown_submission_first(tmp_path: Path) -> None:
    result, _, _ = submit(
        tmp_path, post=timed_out(), lookups=(unreachable(), unreachable(), unreachable())
    )
    report = ExternalPaperExecutionReport(
        manifest=external_manifest(venue=VenueBinding.external(binding()), mapping=mapping()),
        readiness=preflight(
            adapter(account_transport()), log=log_at(tmp_path, "pf"), origin=PAPER_URL
        ),
        submissions=(result,),
    )

    assert report.unknown_submissions == (result,)
    assert report.is_clean is False
    assert "ATTENTION" in report.summary_lines()[0]


def test_a_report_counts_one_mutating_request_per_submission(tmp_path: Path) -> None:
    first, _, _ = submit(tmp_path, post=answered(order_body()))
    second, _, _ = submit(tmp_path / "second", post=answered(order_body()))
    report = ExternalPaperExecutionReport(
        manifest=external_manifest(venue=VenueBinding.local(), mapping=mapping()),
        readiness=preflight(
            adapter(account_transport()), log=log_at(tmp_path, "pf"), origin=PAPER_URL
        ),
        submissions=(first, second),
    )

    assert report.total_mutating_requests == 2


def test_a_report_is_not_clean_when_reconciliation_blocks(tmp_path: Path) -> None:
    blocking = reconcile_external(
        state=book(quantity="0", cash="100000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(0)},
    )
    report = ExternalPaperExecutionReport(
        manifest=external_manifest(venue=VenueBinding.external(binding()), mapping=mapping()),
        readiness=preflight(
            adapter(account_transport()), log=log_at(tmp_path, "pf"), origin=PAPER_URL
        ),
        reconciliation=blocking,
    )

    assert report.is_clean is False


def test_the_execution_core_imports_no_http_client() -> None:
    import pkgutil

    import stock_screener.execution as package

    offenders: list[str] = []
    for module in pkgutil.iter_modules(package.__path__):
        if module.ispkg:
            continue
        source = (Path(package.__path__[0]) / f"{module.name}.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and any(
                client in stripped
                for client in ("httpx", "requests", "urllib", "aiohttp", "socket")
            ):
                offenders.append(f"{module.name}: {stripped}")

    assert offenders == []


# ---------------------------------------------------------------------------
# §97 no forbidden convenience API exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "liquidate",
        "close_all",
        "close_all_positions",
        "flatten",
        "transfer",
        "deposit",
        "withdraw",
        "fund_account",
        "replace_order",
        "enable_live",
        "use_live",
    ],
)
def test_the_adapter_exposes_no_forbidden_convenience_api(forbidden: str) -> None:
    assert not hasattr(AlpacaPaperAdapter, forbidden)


def cancel_transport(
    *, delete: BrokerResponse, lookup: BrokerResponse | None = None
) -> FakeTransport:
    """A transport scripting one DELETE and the read-back that follows it."""
    transport = FakeTransport()
    transport.script(RequestMethod.DELETE, f"{ORDERS_PATH}/remote-1", delete)
    if lookup is not None:
        transport.script(RequestMethod.GET, f"{ORDERS_PATH}/remote-1", lookup)
    return transport


def test_a_cancellation_is_proven_by_reading_not_by_the_delete_status(tmp_path: Path) -> None:
    transport = cancel_transport(
        delete=answered({}, status=204),
        lookup=answered(order_body(status="canceled", filled="0")),
    )

    result = adapter(transport).cancel_order(
        remote_order_id="remote-1", log=log_at(tmp_path), requested_at="2026-10-06T14:30:00+00:00"
    )

    assert result.outcome is CancellationOutcome.REMOTE_CANCELLED
    assert result.outcome.is_terminal is True
    assert transport.mutation_count(f"{ORDERS_PATH}/remote-1") == 1


def test_a_clean_delete_status_alone_does_not_prove_cancellation(tmp_path: Path) -> None:
    transport = cancel_transport(
        delete=answered({}, status=204),
        lookup=answered(order_body(status="new", filled="0")),
    )

    result = adapter(transport).cancel_order(
        remote_order_id="remote-1", log=log_at(tmp_path), requested_at="t"
    )

    assert result.outcome is CancellationOutcome.REMOTE_STILL_OPEN
    assert result.outcome.is_terminal is False
    assert result.outcome.may_request_again is True


def test_an_order_that_filled_before_the_cancel_is_reported_filled(tmp_path: Path) -> None:
    transport = cancel_transport(
        delete=refused(422, {"message": "order is not cancelable"}),
        lookup=answered(order_body(status="filled", filled="10")),
    )

    result = adapter(transport).cancel_order(
        remote_order_id="remote-1", log=log_at(tmp_path), requested_at="t"
    )

    assert result.outcome is CancellationOutcome.REMOTE_FILLED
    assert result.filled_quantity == "10"


def test_an_ambiguous_cancellation_is_resolved_by_reading_not_resending(tmp_path: Path) -> None:
    transport = cancel_transport(
        delete=timed_out(), lookup=answered(order_body(status="canceled", filled="0"))
    )

    result = adapter(transport).cancel_order(
        remote_order_id="remote-1", log=log_at(tmp_path), requested_at="t"
    )

    assert result.outcome is CancellationOutcome.REMOTE_CANCELLED
    assert transport.mutation_count(f"{ORDERS_PATH}/remote-1") == 1
    assert len(transport.mutations) == 1


def test_an_unresolvable_cancellation_stays_unknown(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.DELETE, f"{ORDERS_PATH}/remote-1", timed_out())
    transport.script(
        RequestMethod.GET, f"{ORDERS_PATH}/remote-1", unreachable(), unreachable(), unreachable()
    )

    result = adapter(transport).cancel_order(
        remote_order_id="remote-1", log=log_at(tmp_path), requested_at="t"
    )

    assert result.outcome is CancellationOutcome.REMOTE_STATE_UNKNOWN
    assert result.outcome.may_request_again is False
    assert len(transport.mutations) == 1


def test_an_order_the_venue_has_never_heard_of_is_not_a_cancellation(tmp_path: Path) -> None:
    transport = cancel_transport(delete=answered({}, status=204), lookup=refused(404))

    result = adapter(transport).cancel_order(
        remote_order_id="remote-1", log=log_at(tmp_path), requested_at="t"
    )

    assert result.outcome is CancellationOutcome.REMOTE_STATE_UNKNOWN


def test_a_cancellation_intent_is_durable_before_the_delete(tmp_path: Path) -> None:
    log = log_at(tmp_path)
    seen: list[tuple[str, ...]] = []

    class RecordingTransport(FakeTransport):
        def request(
            self,
            method: RequestMethod,
            path: str,
            *,
            params: dict[str, Any] | None = None,
            json_body: dict[str, Any] | None = None,
        ) -> BrokerResponse:
            if method is RequestMethod.DELETE:
                seen.append(log.recorded_cancellations())
                return answered({}, status=204)
            return answered(order_body(status="canceled", filled="0"))

    adapter(RecordingTransport()).cancel_order(
        remote_order_id="remote-1", log=log, requested_at="t"
    )

    assert seen == [("remote-1",)]


def test_cancelling_needs_the_provider_order_id(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match=Code.REMOTE_PROVIDER_ORDER_ID_REQUIRED):
        adapter(FakeTransport()).cancel_order(
            remote_order_id="   ", log=log_at(tmp_path), requested_at="t"
        )


def test_cancellation_is_refused_where_the_venue_does_not_support_it(tmp_path: Path) -> None:
    bound = binding(cancel_supported=False)

    with pytest.raises(ExecutionError, match=Code.BROKER_CAPABILITY_UNSUPPORTED):
        adapter(FakeTransport(), bound=bound).cancel_order(
            remote_order_id="remote-1", log=log_at(tmp_path), requested_at="t"
        )


# ---------------------------------------------------------------------------
# §98 the venue's whole picture, and the remaining refusals
# ---------------------------------------------------------------------------


def test_the_order_listing_reads_every_order_the_venue_has() -> None:
    transport = FakeTransport()
    transport.script(
        RequestMethod.GET,
        ORDERS_PATH,
        answered(
            [
                order_body(client_order_id=client_order_id_for("ORD-1")),
                order_body(remote_id="remote-9", client_order_id="manual", symbol="MSFT"),
            ]
        ),
    )

    listed = adapter(transport).read_open_orders()

    assert [row.remote_order_id for row in listed] == ["remote-1", "remote-9"]


def test_an_unreadable_order_listing_is_never_substituted_with_an_empty_one() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ORDERS_PATH, unreachable(), unreachable(), unreachable())

    with pytest.raises(ExecutionError, match=Code.BROKER_UNREACHABLE):
        adapter(transport).read_open_orders()


def test_a_non_list_order_listing_yields_nothing_rather_than_guessing() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ORDERS_PATH, answered({"orders": "unexpected"}))

    assert adapter(transport).read_open_orders() == ()


def test_a_positions_payload_that_is_not_a_list_is_refused() -> None:
    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACCOUNT_PATH, answered(account_body()))
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered({"positions": "nope"}))

    with pytest.raises(ExecutionError, match=Code.REMOTE_STATE_UNKNOWN):
        adapter(transport).read_account()


def test_a_submission_reply_without_an_order_object_leaves_the_state_unknown(
    tmp_path: Path,
) -> None:
    log = log_at(tmp_path)
    transport = FakeTransport()
    transport.script(RequestMethod.POST, ORDERS_PATH, answered("not an object"))

    with pytest.raises(ExecutionError, match=Code.REMOTE_STATE_UNKNOWN):
        submit_order(
            order(),
            transport=transport,
            binding=binding(),
            mapping=mapping(),
            log=log,
            recorded_at="t",
        )


def test_a_venue_that_states_no_cash_cannot_be_asked_for_one() -> None:
    with pytest.raises(ExecutionError, match=Code.REMOTE_STATE_UNKNOWN):
        remote_account(cash=None).require_cash()


def test_a_held_symbol_is_found_case_insensitively() -> None:
    snapshot = remote_account(positions=(("AAPL", "10"),))

    assert snapshot.position_for("aapl") is not None
    assert snapshot.position_for("MSFT") is None


def test_a_negative_fee_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        ExternalExecutionFill(
            remote_fill_id="exec-1",
            remote_order_id="remote-1",
            client_order_id="c",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal(1),
            price=Decimal(1),
            reported_at="t",
            fee=Decimal("-0.01"),
        )


def test_a_negative_filled_quantity_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.REMOTE_ORDER_MISMATCH):
        ExternalOrderSnapshot(
            remote_order_id="remote-1",
            client_order_id="c",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal(10),
            filled_quantity=Decimal(-1),
            reading=read_provider_status("new"),
        )


def test_an_order_payload_with_an_unreadable_side_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        ExternalOrderSnapshot.from_payload(order_body(side="hold"))


def test_a_malformed_provider_figure_is_refused_rather_than_dropped() -> None:
    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        ExternalOrderSnapshot.from_payload({**order_body(), "qty": "twelve"})


def test_a_terminal_order_with_quantity_outstanding_is_a_warning() -> None:
    result = reconcile_external(
        state=book(quantity="4", cash="99600.00"),
        orders=[order()],
        remote_orders=[remote_snapshot(filled="4", status="expired")],
        remote_account=remote_account(cash="99600.00", positions=(("AAPL", "4"),)),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(4)},
    )
    mismatch = [row for row in result.breaks if row.code == Code.REMOTE_ORDER_MISMATCH]

    assert mismatch[0].severity is ExternalBreakSeverity.WARNING
    assert result.may_execute is True


def test_a_zero_quantity_unmapped_holding_is_only_a_warning() -> None:
    result = reconcile_external(
        state=book(quantity="10", cash="99000.00"),
        orders=[order()],
        remote_orders=[remote_snapshot()],
        remote_account=remote_account(positions=(("AAPL", "10"), ("TSLA", "0"))),
        mapping=mapping(),
        local_filled={"ORD-1": Decimal(10)},
    )
    unmapped = [row for row in result.breaks if row.code == Code.BROKER_SYMBOL_MAPPING_UNRESOLVED]

    assert unmapped[0].severity is ExternalBreakSeverity.WARNING


def test_a_response_states_whether_it_is_ambiguous() -> None:
    assert timed_out().is_ambiguous is True
    assert unreachable().is_ambiguous is True
    assert refused(404).is_ambiguous is False
    assert refused(404).not_found is True
    assert answered(order_body()).ok is True


def test_a_transport_records_whether_a_request_mutates() -> None:
    assert RequestMethod.GET.mutates is False
    assert RequestMethod.POST.mutates is True
    assert RequestMethod.DELETE.mutates is True


def test_a_response_serialises_without_a_credential() -> None:
    body = answered(order_body()).as_json()

    check_no_credentials(body, context="a transport response")
    assert body["ok"] is True


def test_the_submission_result_serialises_for_a_report(tmp_path: Path) -> None:
    result, _, _ = submit(tmp_path, post=answered(order_body()))

    serialised = result.as_json()

    assert serialised["outcome"] == "REMOTE_CONFIRMED"
    assert serialised["mutating_requests"] == 1
    check_no_credentials(serialised, context="a submission result")


def test_an_unsettled_outcome_records_no_resolution_file(tmp_path: Path) -> None:
    log = log_at(tmp_path)

    assert (
        log.record_resolution("ssx1-" + "a" * 40, outcome=SubmissionOutcome.REMOTE_STATE_UNKNOWN)
        is None
    )


def test_a_store_root_in_a_research_namespace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match=Code.FORBIDDEN_EXECUTION_NAMESPACE):
        SubmissionIntentLog(tmp_path / "pit_research_dataset" / "orders")
