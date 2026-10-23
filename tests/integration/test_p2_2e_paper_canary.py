"""P2.2E — the external paper canary, exercised offline.

The canary's job is to prove the authority chain against a real venue, so most
of what matters about it can only be shown by running it. What these tests do is
different and still necessary: they pin the behaviour that must hold *before*
anything is submitted, and the arithmetic that must hold after.

Every test runs against a `FakeTransport`. None makes a network request and none
places an order anywhere.
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
    ASSETS_PATH,
    CANARY_CLASSIFICATION,
    CERTIFICATION_ARTEFACT_VERSION,
    ORDERS_PATH,
    POSITIONS_PATH,
    AlpacaPaperPreflightRun,
    BrokerEnvironment,
    BrokerResponse,
    ExternalAsset,
    ExternalBrokerAccountBinding,
    ExternalPaperCanary,
    FakeTransport,
    RequestMethod,
    SubmissionIntentLog,
    SymbolMapping,
    TransportOutcome,
    VenueSymbol,
    check_no_credentials,
    prove_paper_environment,
)
from stock_screener.portfolio_risk import SecurityIdentity

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

PAPER_URL = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-canary"
IDENTITY = SecurityIdentity(kind="COMPANY", identity_id=11, symbol="F")
SYMBOL = "F"
NOW = "2026-10-07T17:08:00+00:00"
ENTRY_REMOTE = "entry-remote-0001"
CLEANUP_REMOTE = "cleanup-remote-0001"


def answered(body: Any, *, status: int = 200, request_id: str | None = None) -> BrokerResponse:
    """A successful provider reply."""
    return BrokerResponse(
        outcome=TransportOutcome.ANSWERED, status_code=status, body=body, request_id=request_id
    )


def asset_body(
    *, fractionable: bool = True, status: str = "active", cls: str = "us_equity"
) -> dict[str, Any]:
    """A provider asset object."""
    return {
        "symbol": SYMBOL,
        "tradable": True,
        "fractionable": fractionable,
        "status": status,
        "class": cls,
        "exchange": "NYSE",
    }


def account_body(*, cash: str = "100000.00") -> dict[str, Any]:
    """A provider account object."""
    return {"id": ACCOUNT_ID, "status": "ACTIVE", "cash": cash, "trading_blocked": False}


def order_body(
    *, remote_id: str, client_order_id: str, side: str, qty: str, filled: str, status: str
) -> dict[str, Any]:
    """A provider order object."""
    return {
        "id": remote_id,
        "client_order_id": client_order_id,
        "symbol": SYMBOL,
        "side": side,
        "qty": qty,
        "filled_qty": filled,
        "status": status,
        "type": "market",
        "time_in_force": "day",
    }


def activity_body(
    *, remote_id: str, order_id: str, side: str, qty: str, price: str, at: str
) -> dict[str, Any]:
    """A provider FILL activity."""
    return {
        "id": remote_id,
        "activity_type": "FILL",
        "type": "fill",
        "order_id": order_id,
        "symbol": SYMBOL,
        "side": side,
        "qty": qty,
        "price": price,
        "transaction_time": at,
    }


def build_run(tmp_path: Path, transport: FakeTransport) -> AlpacaPaperPreflightRun:
    """A preflight run over a scripted transport."""
    proof = prove_paper_environment(
        environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="PKCANARYKEY1"
    )
    binding = ExternalBrokerAccountBinding.paper(
        provider="ALPACA", account_id=ACCOUNT_ID, proof=proof
    )
    mapping = SymbolMapping.of([VenueSymbol(kind="COMPANY", identity_id=11, symbol=SYMBOL)])
    run = AlpacaPaperPreflightRun(
        transport=transport,  # type: ignore[arg-type]
        binding=binding,
        mapping=mapping,
        log=SubmissionIntentLog(tmp_path / "exec"),
        identities=(IDENTITY,),
    )
    return run


class CanaryTransport(FakeTransport):
    """A venue that accepts two orders and fills each immediately.

    Models the one real behaviour that caught the first live run out: an order
    reports `filled` before its FILL activity is queryable. The first activities
    read for each order returns nothing, so a canary that concluded "no fill"
    from one read would be wrong here too.
    """

    def __init__(
        self, *, cash: str = "100000.00", positions: list[dict[str, Any]] | None = None
    ) -> None:
        """Set up the venue."""
        super().__init__()
        self.origin = PAPER_URL
        self.cash = cash
        self.positions = positions if positions is not None else []
        self.submitted: list[dict[str, Any]] = []
        self.activity_reads: dict[str, int] = {}
        self.held = Decimal(0)

    def request(
        self,
        method: RequestMethod,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> BrokerResponse:
        """Answer one request."""
        self.calls.append(
            {
                "method": str(method),
                "path": path,
                "params": dict(params or {}),
                "json_body": dict(json_body or {}),
                "mutates": method.mutates,
            }
        )
        if path == ACCOUNT_PATH:
            return answered(account_body(cash=self.cash), request_id="rq-account")
        if path == POSITIONS_PATH:
            return answered(self.positions, request_id="rq-positions")
        if path == f"{ASSETS_PATH}/{SYMBOL}":
            return answered(asset_body(), request_id="rq-asset")
        if method is RequestMethod.POST and path == ORDERS_PATH:
            body = json_body or {}
            index = len(self.submitted)
            remote = ENTRY_REMOTE if index == 0 else CLEANUP_REMOTE
            self.submitted.append({**body, "remote": remote})
            return answered(
                order_body(
                    remote_id=remote,
                    client_order_id=str(body.get("client_order_id", "")),
                    side=str(body.get("side", "buy")),
                    qty=str(body.get("qty", "0")),
                    filled="0",
                    status="new",
                ),
                request_id=f"rq-post-{index}",
            )
        if path == f"{ORDERS_PATH}:by_client_order_id":
            cid = str((params or {}).get("client_order_id", ""))
            for row in self.submitted:
                if row.get("client_order_id") == cid:
                    return answered(
                        order_body(
                            remote_id=str(row["remote"]),
                            client_order_id=cid,
                            side=str(row["side"]),
                            qty=str(row["qty"]),
                            filled=str(row["qty"]),
                            status="filled",
                        ),
                        request_id="rq-lookup",
                    )
            return BrokerResponse(outcome=TransportOutcome.ANSWERED, status_code=404)
        if path == ORDERS_PATH:
            return answered([], request_id="rq-open")
        if path == ACTIVITIES_PATH:
            order_id = str((params or {}).get("order_id", ""))
            seen = self.activity_reads.get(order_id, 0)
            self.activity_reads[order_id] = seen + 1
            if seen == 0:
                # the activities feed lags the order status
                return answered([], request_id="rq-acts-lag")
            matching = [r for r in self.submitted if r["remote"] == order_id]
            if not matching:
                return answered([])
            row = matching[0]
            proceeds = Decimal(str(row["qty"])) * Decimal("12.00")
            if not row.get("settled"):
                row["settled"] = True
                self.cash = str(
                    Decimal(self.cash) + (-proceeds if row["side"] == "buy" else proceeds)
                )
                self.held += (
                    Decimal(str(row["qty"])) if row["side"] == "buy" else -Decimal(str(row["qty"]))
                )
                self.positions = (
                    [{"symbol": SYMBOL, "qty": str(self.held)}] if self.held > 0 else []
                )
            return answered(
                [
                    activity_body(
                        remote_id=f"exec-{order_id}",
                        order_id=order_id,
                        side=str(row["side"]),
                        qty=str(row["qty"]),
                        price="12.00",
                        at="2026-10-07T20:00:00Z"
                        if row["side"] == "buy"
                        else "2026-10-07T20:00:10Z",
                    )
                ],
                request_id="rq-acts",
            )
        return BrokerResponse(outcome=TransportOutcome.ANSWERED, status_code=404)


def canary_of(tmp_path: Path, transport: FakeTransport) -> ExternalPaperCanary:
    """A canary over a scripted venue."""
    run = build_run(tmp_path, transport)
    return ExternalPaperCanary(run, identity=IDENTITY, store_root=tmp_path / "canary")


# ---------------------------------------------------------------------------
# the end-to-end chain
# ---------------------------------------------------------------------------


def test_the_canary_certifies_against_a_clean_venue(tmp_path: Path) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.outcome == "CERTIFIED"
    assert report.certified is True


def test_the_canary_submits_exactly_two_orders(tmp_path: Path) -> None:
    transport = CanaryTransport()

    canary_of(tmp_path, transport).execute(quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None)

    assert transport.mutation_count(ORDERS_PATH) == 2
    assert len(transport.mutations) == 2


def test_the_canary_closes_what_it_opened(tmp_path: Path) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.entry is not None
    assert report.cleanup is not None
    assert report.entry.side.value == "BUY"
    assert report.cleanup.side.value == "SELL"
    assert report.residual_position == 0


def test_the_canary_never_cancels_anything(tmp_path: Path) -> None:
    transport = CanaryTransport()

    canary_of(tmp_path, transport).execute(quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None)

    assert [row for row in transport.calls if row["method"] == "DELETE"] == []


def test_the_canary_ingests_each_fill_exactly_once(tmp_path: Path) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.entry is not None
    assert report.entry.admitted_quantity == Decimal("0.1")
    assert report.entry.duplicate_poll_admitted == Decimal(0)
    assert len(report.entry.accounting_events) == 1


def test_the_accounting_event_id_derives_from_the_provider_execution_id(
    tmp_path: Path,
) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.entry is not None
    assert report.entry.accounting_events[0] == f"EXEC_FILL:EXT:exec-{ENTRY_REMOTE}"


def test_the_canary_reconciles_three_ways(tmp_path: Path) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.reconciliation_after_fill == "RECONCILED"
    assert report.reconciliation_after_cleanup == "RECONCILED"
    assert report.blocking_breaks == ()


def test_the_canary_survives_the_activities_feed_lagging(tmp_path: Path) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    # the first activities read per order returned nothing
    assert transport.activity_reads[ENTRY_REMOTE] >= 2
    assert report.certified is True


def test_the_lookup_by_client_order_id_matches_the_submitted_order(tmp_path: Path) -> None:
    transport = CanaryTransport()

    report = canary_of(tmp_path, transport).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.entry is not None
    assert report.entry.lookup_matched is True
    assert report.entry.remote_order_id == ENTRY_REMOTE


# ---------------------------------------------------------------------------
# preconditions, before anything is sent
# ---------------------------------------------------------------------------


def test_a_pre_existing_position_blocks_the_canary(tmp_path: Path) -> None:
    transport = CanaryTransport(positions=[{"symbol": "JNJ", "qty": "37"}])

    with pytest.raises(ExecutionError, match=Code.EXTERNAL_SUBMISSION_BLOCKED):
        canary_of(tmp_path, transport).execute(
            quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
        )

    assert transport.mutations == []


def test_a_blocked_canary_submits_nothing(tmp_path: Path) -> None:
    transport = CanaryTransport(positions=[{"symbol": "JNJ", "qty": "37"}])

    with pytest.raises(ExecutionError):
        canary_of(tmp_path, transport).execute(
            quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
        )

    assert transport.mutation_count(ORDERS_PATH) == 0


# ---------------------------------------------------------------------------
# the instrument is read, not assumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"tradable": False}, "not accepting orders"),
        ({"status": "inactive"}, "listing status"),
        ({"class": "crypto"}, "US equities only"),
    ],
)
def test_an_unsuitable_instrument_is_refused(overrides: dict[str, Any], reason: str) -> None:
    asset = ExternalAsset.from_payload({**asset_body(), **overrides})

    with pytest.raises(ExecutionError, match=Code.BROKER_ASSET_NOT_TRADABLE) as caught:
        asset.require_tradable_us_equity()

    assert reason in str(caught.value)


def test_a_tradable_active_us_equity_passes() -> None:
    ExternalAsset.from_payload(asset_body()).require_tradable_us_equity()


def test_asset_fields_default_to_not_permitted() -> None:
    asset = ExternalAsset.from_payload({"symbol": SYMBOL})

    assert asset.tradable is False
    assert asset.fractionable is False


def test_fractional_is_declared_from_the_asset_read(tmp_path: Path) -> None:
    transport = CanaryTransport()
    canary = canary_of(tmp_path, transport)

    report = canary.execute(quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None)

    assert canary.run.binding.capabilities.fractional_quantities is True
    assert any("fractionable" in note for note in report.notes)


# ---------------------------------------------------------------------------
# the run tag
# ---------------------------------------------------------------------------


def test_the_run_tag_is_stable_within_a_run() -> None:
    assert ExternalPaperCanary._run_tag(NOW) == ExternalPaperCanary._run_tag(NOW)


def test_the_run_tag_differs_between_runs() -> None:
    assert ExternalPaperCanary._run_tag(NOW) != ExternalPaperCanary._run_tag(
        "2026-10-07T18:00:00+00:00"
    )


def test_two_runs_derive_different_client_order_ids(tmp_path: Path) -> None:
    first = CanaryTransport()
    second = CanaryTransport()

    a = canary_of(tmp_path / "a", first).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )
    b = canary_of(tmp_path / "b", second).execute(
        quantity=Decimal("0.1"), now="2026-10-07T19:00:00+00:00", sleep=lambda _: None
    )

    assert a.entry is not None
    assert b.entry is not None
    assert a.entry.client_order_id != b.entry.client_order_id


# ---------------------------------------------------------------------------
# the artefact
# ---------------------------------------------------------------------------


def test_the_artefact_states_it_is_not_live_trading(tmp_path: Path) -> None:
    report = canary_of(tmp_path, CanaryTransport()).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )
    body = report.body()

    assert body["classification"] == list(CANARY_CLASSIFICATION)
    assert "NOT_LIVE_TRADING" in body["classification"]
    assert body["live_trading"] == "NOT_AUTHORIZED"
    assert body["artefact_version"] == CERTIFICATION_ARTEFACT_VERSION


def test_the_artefact_carries_no_credential_or_account_number(tmp_path: Path) -> None:
    report = canary_of(tmp_path, CanaryTransport()).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    serialised = json.dumps(report.as_json())
    check_no_credentials(report.as_json(), context="a canary artefact")
    assert "PKCANARYKEY1" not in serialised
    assert ACCOUNT_ID not in serialised


def test_the_artefact_does_not_claim_reproducibility(tmp_path: Path) -> None:
    report = canary_of(tmp_path, CanaryTransport()).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert "not recreatable" in report.body()["reproducibility"]


def test_the_artefact_fingerprint_is_stable_for_the_same_run(tmp_path: Path) -> None:
    report = canary_of(tmp_path, CanaryTransport()).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert report.fingerprint() == report.fingerprint()


def test_the_artefact_records_the_declared_fee_is_not_observed(tmp_path: Path) -> None:
    report = canary_of(tmp_path, CanaryTransport()).execute(
        quantity=Decimal("0.1"), now=NOW, sleep=lambda _: None
    )

    assert any("declared schedule value" in note for note in report.notes)
    assert report.entry is not None
    assert report.entry.fills[0].fee_is_known is False


# ---------------------------------------------------------------------------
# residual cleanup
# ---------------------------------------------------------------------------


def test_a_residual_is_closed_by_a_compensating_order(tmp_path: Path) -> None:
    transport = CanaryTransport(positions=[{"symbol": SYMBOL, "qty": "0.1"}])

    report = canary_of(tmp_path, transport).close_residual(
        quantity=Decimal("0.1"),
        cost_basis_per_share=Decimal("12.00"),
        now=NOW,
        sleep=lambda _: None,
    )

    assert report.outcome == "RESIDUAL_CLOSED"
    assert report.residual_position == 0
    assert transport.mutation_count(ORDERS_PATH) == 1
    assert [row for row in transport.calls if row["method"] == "DELETE"] == []


def test_closing_a_residual_is_an_authorised_order_not_a_liquidation(tmp_path: Path) -> None:
    transport = CanaryTransport(positions=[{"symbol": SYMBOL, "qty": "0.1"}])

    report = canary_of(tmp_path, transport).close_residual(
        quantity=Decimal("0.1"),
        cost_basis_per_share=Decimal("12.00"),
        now=NOW,
        sleep=lambda _: None,
    )

    assert report.cleanup is not None
    assert report.cleanup.plan_fingerprint
    assert any("compensating authorised order" in note for note in report.notes)
    assert all("positions" not in row["path"] or row["method"] == "GET" for row in transport.calls)
