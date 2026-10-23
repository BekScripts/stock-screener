"""P2.2C — the real read-only preflight, driven through a stubbed HTTP layer.

These tests exercise `AlpacaPaperPreflightRun` with the concrete transport in
place and `httpx.MockTransport` underneath it, so the whole path runs — transport
URL building, credential headers, response classification, the adapter's read
methods, the seven gates, the artefact — with no socket and no order.

The property every one of them protects is that a preflight is read-only. It is
asserted by counting methods, not by reading the code.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from stock_screener.config import Settings
from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.external import (
    PREFLIGHT_ARTEFACT_VERSION,
    READY_VERDICT,
    TRANSPORT_VERSION,
    AlpacaPaperHttpTransport,
    AlpacaPaperPreflightRun,
    BrokerEnvironment,
    ExecutionVenue,
    ExternalBrokerAccountBinding,
    SubmissionIntentLog,
    SymbolMapping,
    VenueSymbol,
    check_no_credentials,
    client_order_id_for,
    prove_paper_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.integration

PAPER_ORIGIN = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-2f91"
KEY_ID = "PKPREFLIGHTKEY01"
SECRET = "PREFLIGHTSECRET01"  # noqa: S105 - dummy fixture, asserted absent from artefacts


def account_payload(**overrides: Any) -> dict[str, Any]:
    """Build an Alpaca account object as the real API shapes it.

    Note there is no `is_paper` field: the real account object has none, which
    is why paper-ness rests on the origin rather than on the payload.

    Args:
        overrides: Fields to replace.

    Returns:
        The payload.
    """
    return {
        "id": ACCOUNT_ID,
        "account_number": "PA3DK8FH",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "100000.00",
        "buying_power": "200000.00",
        "trading_blocked": False,
        "account_blocked": False,
        **overrides,
    }


def venue_handler(
    *,
    account: dict[str, Any] | None = None,
    positions: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
    account_status: int = 200,
) -> tuple[list[tuple[str, str]], Callable[[httpx.Request], httpx.Response]]:
    """Build a handler answering the three read endpoints.

    Args:
        account: The account payload.
        positions: The positions payload.
        orders: The orders payload.
        account_status: Status for the account read.

    Returns:
        The recorded `(method, path)` calls and the handler.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/v2/account":
            return httpx.Response(
                account_status,
                json=account if account is not None else account_payload(),
                headers={"X-Request-ID": "rq-account"},
            )
        if path == "/v2/positions":
            return httpx.Response(
                200, json=positions or [], headers={"X-Request-ID": "rq-positions"}
            )
        if path == "/v2/orders":
            return httpx.Response(
                200, json=orders or [], headers={"X-Request-ID": "rq-open-orders"}
            )
        return httpx.Response(404, json={"message": "no such endpoint"})

    return calls, handler


def run_with(
    handler: Callable[[httpx.Request], httpx.Response],
    tmp_path: Path,
    *,
    bound_account: str = ACCOUNT_ID,
    mapping: SymbolMapping | None = None,
) -> AlpacaPaperPreflightRun:
    """Build a preflight run over a stubbed HTTP layer.

    Args:
        handler: Answers each request.
        tmp_path: Where the intent log lives.
        bound_account: Which account the binding names.
        mapping: A declared symbol mapping.

    Returns:
        The run.
    """
    client = httpx.Client(
        base_url=PAPER_ORIGIN,
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    transport = AlpacaPaperHttpTransport(
        origin=PAPER_ORIGIN,
        key_id=SecretStr(KEY_ID),
        secret_key=SecretStr(SECRET),
        client=client,
    )
    proof = prove_paper_environment(
        environment=BrokerEnvironment.PAPER, base_url=PAPER_ORIGIN, key_id=KEY_ID
    )
    binding = ExternalBrokerAccountBinding.paper(
        provider="ALPACA", account_id=bound_account, proof=proof
    )
    return AlpacaPaperPreflightRun(
        transport=transport,
        binding=binding,
        mapping=mapping or SymbolMapping.of(()),
        log=SubmissionIntentLog(tmp_path / "exec"),
    )


# ---------------------------------------------------------------------------
# the preflight is read-only
# ---------------------------------------------------------------------------


def test_a_clean_paper_account_is_ready(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.ready is True
    assert report.verdict == READY_VERDICT


def test_the_preflight_makes_no_mutating_request(tmp_path: Path) -> None:
    calls, handler = venue_handler()
    run = run_with(handler, tmp_path)

    run.execute()
    run.close()

    assert {method for method, _ in calls} == {"GET"}
    assert [method for method, _ in calls if method != "GET"] == []


def test_the_preflight_reports_zero_mutations(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.mutating_requests == 0


def test_the_preflight_never_touches_the_orders_submission_path(tmp_path: Path) -> None:
    calls, handler = venue_handler()
    run = run_with(handler, tmp_path)

    run.execute()
    run.close()

    assert all(not (method == "POST" and path == "/v2/orders") for method, path in calls)


# ---------------------------------------------------------------------------
# account binding
# ---------------------------------------------------------------------------


def test_the_expected_account_is_matched(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.account_matched is True
    assert "ACCOUNT_MATCHES_BINDING" in report.gates_passed


def test_another_account_withholds_readiness(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path, bound_account="a-different-paper-account")

    report = run.execute()
    run.close()

    assert report.ready is False
    assert report.account_matched is False
    assert "ACCOUNT_MATCHES_BINDING" in dict(report.failures)


def test_an_account_mismatch_submits_nothing(tmp_path: Path) -> None:
    calls, handler = venue_handler()
    run = run_with(handler, tmp_path, bound_account="a-different-paper-account")

    run.execute()
    run.close()

    assert {method for method, _ in calls} == {"GET"}


def test_blocked_trading_withholds_readiness(tmp_path: Path) -> None:
    _, handler = venue_handler(account=account_payload(trading_blocked=True))
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.ready is False
    assert "TRADING_NOT_BLOCKED" in dict(report.failures)


def test_an_unauthenticated_credential_withholds_readiness(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    run = run_with(handler, tmp_path)
    report = run.execute()
    run.close()

    assert report.ready is False
    assert "ACCOUNT_REACHABLE" in dict(report.failures)


def test_an_unreachable_venue_withholds_readiness(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    run = run_with(handler, tmp_path)
    report = run.execute()
    run.close()

    assert report.ready is False
    assert "ACCOUNT_REACHABLE" in dict(report.failures)


# ---------------------------------------------------------------------------
# what the venue holds
# ---------------------------------------------------------------------------


def test_existing_positions_are_counted(tmp_path: Path) -> None:
    _, handler = venue_handler(
        positions=[
            {"symbol": "AAPL", "qty": "3", "avg_entry_price": "100.00"},
            {"symbol": "MSFT", "qty": "1", "avg_entry_price": "300.00"},
        ]
    )
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.positions_seen == 2


def test_an_order_this_adapter_did_not_mint_is_counted_as_foreign(tmp_path: Path) -> None:
    _, handler = venue_handler(
        orders=[
            {
                "id": "remote-1",
                "client_order_id": "typed-in-the-console",
                "symbol": "AAPL",
                "side": "buy",
                "qty": "1",
                "filled_qty": "0",
                "status": "new",
            }
        ]
    )
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.open_orders_seen == 1
    assert report.foreign_open_orders == 1


def test_an_order_this_adapter_minted_is_not_foreign(tmp_path: Path) -> None:
    _, handler = venue_handler(
        orders=[
            {
                "id": "remote-1",
                "client_order_id": client_order_id_for("ORD-1"),
                "symbol": "AAPL",
                "side": "buy",
                "qty": "1",
                "filled_qty": "0",
                "status": "new",
            }
        ]
    )
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.foreign_open_orders == 0


def test_an_unresolved_intent_withholds_readiness(tmp_path: Path) -> None:
    from decimal import Decimal

    from stock_screener.execution import OrderSide
    from stock_screener.execution.external import SubmissionIntent

    log = SubmissionIntentLog(tmp_path / "exec")
    log.record_intent(
        SubmissionIntent(
            order_id="ORD-STRANDED",
            client_order_id=client_order_id_for("ORD-STRANDED"),
            execution_plan_id="PLAN-1",
            binding_fingerprint="b",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal(1),
            recorded_at="2026-10-06T14:30:00+00:00",
        )
    )
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.ready is False
    assert "NO_UNRESOLVED_SUBMISSIONS" in dict(report.failures)
    assert report.unresolved_submissions == ("ORD-STRANDED",)


def test_an_unmapped_declared_identity_withholds_readiness(tmp_path: Path) -> None:
    from stock_screener.portfolio_risk import SecurityIdentity

    _, handler = venue_handler()
    run = run_with(handler, tmp_path, mapping=SymbolMapping.of(()))
    run.identities = (SecurityIdentity(kind="COMPANY", identity_id=11, symbol="AAPL"),)

    report = run.execute()
    run.close()

    assert report.ready is False
    assert "SYMBOLS_RESOLVED" in dict(report.failures)


def test_a_declared_canary_asset_resolves(tmp_path: Path) -> None:
    _, handler = venue_handler()
    mapping = SymbolMapping.of(
        [VenueSymbol(kind="COMPANY", identity_id=11, symbol="AAPL", tradable=True)]
    )
    run = run_with(handler, tmp_path, mapping=mapping)

    report = run.execute()
    run.close()

    assert "SYMBOLS_RESOLVED" in report.gates_passed


# ---------------------------------------------------------------------------
# the artefact
# ---------------------------------------------------------------------------


def test_the_artefact_records_the_origin_that_answered(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.origin == PAPER_ORIGIN
    assert report.body()["transport_version"] == TRANSPORT_VERSION
    assert report.body()["artefact_version"] == PREFLIGHT_ARTEFACT_VERSION


def test_the_artefact_carries_no_credential(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    serialised = json.dumps(report.as_json())
    assert KEY_ID not in serialised
    assert SECRET not in serialised
    check_no_credentials(report.as_json(), context="the preflight artefact")


def test_the_artefact_carries_no_account_number(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    serialised = json.dumps(report.as_json())
    assert ACCOUNT_ID not in serialised
    assert "PA3DK8FH" not in serialised


def test_the_artefact_records_provider_request_ids(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    recorded = dict(report.request_ids)
    assert recorded["account"] == "rq-account"
    assert recorded["positions"] == "rq-positions"
    assert recorded["open_orders"] == "rq-open-orders"


def test_the_artefact_records_an_evidence_fingerprint_per_read(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    operations = {operation for operation, _ in report.evidence}
    assert operations == {"account", "positions", "open_orders"}
    assert all(len(digest) == 64 for _, digest in report.evidence)


def test_the_artefact_states_what_it_does_not_establish(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    notes = " ".join(report.limitations)
    assert "sends no field" in notes
    assert "THREE-WAY RECONCILIATION" in notes
    assert "FILL INGESTION" in notes


def test_the_artefact_declares_live_trading_unauthorised(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    report = run.execute()
    run.close()

    assert report.body()["live_trading"] == "NOT_AUTHORIZED"


def test_the_run_binds_the_external_paper_venue(tmp_path: Path) -> None:
    _, handler = venue_handler()
    run = run_with(handler, tmp_path)

    venue = run.venue()
    run.close()

    assert venue.venue is ExecutionVenue.EXTERNAL_PAPER
    assert venue.venue.is_live is False


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_a_run_cannot_be_built_without_paper_credentials() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ExecutionError, match=Code.BROKER_CREDENTIALS_MISSING):
        AlpacaPaperPreflightRun.from_settings(settings)


def test_a_run_cannot_be_built_without_an_expected_account_id() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        alpaca_paper_key_id=SecretStr(KEY_ID),
        alpaca_paper_secret_key=SecretStr(SECRET),
    )

    with pytest.raises(ExecutionError, match=Code.BROKER_CREDENTIALS_MISSING):
        AlpacaPaperPreflightRun.from_settings(settings)


def test_a_run_cannot_be_built_against_a_live_origin() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        alpaca_paper_key_id=SecretStr(KEY_ID),
        alpaca_paper_secret_key=SecretStr(SECRET),
        alpaca_paper_account_id=ACCOUNT_ID,
        alpaca_paper_base_url="https://api.alpaca.markets",
    )

    with pytest.raises(ExecutionError, match=Code.LIVE_TRADING_NOT_AUTHORIZED):
        AlpacaPaperPreflightRun.from_settings(settings)
