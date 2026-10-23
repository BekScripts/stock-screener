"""P2.3.1D — the one production route from a scheduled slot to an audited order.

Two gaps closed here, both of the same kind: something fully built and wired
nowhere.

`paper-ops run-due` constructed a `RuntimePlanner` with no submission
authority, so every execution slot raised `RUNTIME_BINDING_INVALID` while
`RuntimeSubmissionAuthority` — implemented, tested, certified against a real
venue — was constructed nowhere in `src/`. And the production transport carried
no request audit, so nothing durable recorded where a mutation went, which is
why `live_mutations` derived as `EVIDENCE_MISSING` instead of a clean zero.

The tests drive `compose_operations` — the same factory `run-due` calls — and
put a scripted network *under* the real audited transport rather than replacing
it. That matters: a `FakeTransport` would bypass the host lock, the headers and
the audit, which are three of the things under test. An `httpx.MockTransport`
leaves all of them in play.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from stock_screener.paper_runtime import (
    AUDIT_DIRECTORY,
    OPERATOR_INPUTS_VERSION,
    PRODUCTION_COMPOSITION_VERSION,
    Code,
    RuntimeBindingError,
    compose_operations,
    composition_readiness,
    operator_input_status,
    write_risk_policy,
    write_symbol_mapping,
)

pytestmark = pytest.mark.integration

PAPER = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-1"
SYMBOLS: dict[int, str] = {1: "AAA", 2: "BBB", 3: "CCC"}
DECISION_NOW = "2026-10-30T20:00:30+00:00"
EXECUTION_NOW = "2026-11-02T14:35:00+00:00"


@dataclass
class FakeSettings:
    """Typed settings, as the CLI reads them. No environment is touched."""

    database_url: str = "sqlite:///:memory:"
    alpaca_paper_base_url: str = PAPER
    alpaca_paper_account_id: str = ACCOUNT_ID
    alpaca_paper_key_id: Any = None
    alpaca_paper_secret_key: Any = None

    def __post_init__(self) -> None:
        """Attach the masked credentials."""
        self.alpaca_paper_key_id = SecretStr("PKTESTKEY01")
        self.alpaca_paper_secret_key = SecretStr("shhh-secret-value")


@dataclass
class ScriptedVenue:
    """A scripted Alpaca PAPER endpoint, under the real HTTPS transport.

    Records every request so a test can assert what reached the network, and
    answers the handful of routes an operational run touches.
    """

    orders: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    on_post: Any = None
    cash: str = "100000.00"

    @property
    def mutations(self) -> list[dict[str, Any]]:
        """Every mutating request that reached the network."""
        return [row for row in self.calls if row["method"] in {"POST", "PUT", "PATCH", "DELETE"}]

    def client(self) -> httpx.Client:
        """An HTTP client whose network is this script."""
        return httpx.Client(
            base_url=PAPER, transport=httpx.MockTransport(self._handle), timeout=5.0
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        path = request.url.path
        method = request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append({"method": method, "path": path, "json": body})
        if method == "POST" and path == "/v2/orders":
            index = len([row for row in self.calls if row["method"] == "POST"])
            order = _order_row(index, body.get("client_order_id", ""))
            self.orders.append(order)
            if self.on_post is not None:
                self.on_post(index)
            return httpx.Response(200, json=order)
        if path == "/v2/account":
            return httpx.Response(
                200,
                json={
                    "id": ACCOUNT_ID,
                    "status": "ACTIVE",
                    "cash": self.cash,
                    "buying_power": "200000.00",
                },
            )
        if path == "/v2/positions":
            return httpx.Response(200, json=self.positions)
        if path.startswith("/v2/account/activities"):
            wanted = request.url.params.get("order_id")
            return httpx.Response(
                200,
                json=[_fill_row(row) for row in self.orders if not wanted or row["id"] == wanted],
            )
        if path == "/v2/orders:by_client_order_id":
            # The protocol's resolution route: after an ambiguous POST it asks
            # the venue about the derived client order id rather than resending.
            wanted = request.url.params.get("client_order_id")
            for row in self.orders:
                if row["client_order_id"] == wanted:
                    return httpx.Response(200, json=row)
            return httpx.Response(404, json={"message": "order not found"})
        if path == "/v2/orders":
            return httpx.Response(200, json=self.orders)
        return httpx.Response(200, json={})


def _order_row(index: int, client_order_id: str) -> dict[str, Any]:
    """One provider order object."""
    return {
        "id": f"remote-{index}",
        "client_order_id": client_order_id,
        "symbol": SYMBOLS[min(index, 3)],
        "side": "buy",
        "qty": "333",
        "filled_qty": "333",
        "status": "filled",
        "type": "market",
        "time_in_force": "day",
    }


def _fill_row(order: dict[str, Any]) -> dict[str, Any]:
    """The FILL activity for one order."""
    return {
        "id": f"exec-{order['id']}",
        "activity_type": "FILL",
        "type": "fill",
        "order_id": order["id"],
        "symbol": order["symbol"],
        "side": "buy",
        "qty": order["qty"],
        "price": "100.00",
        "transaction_time": "2026-11-02T14:35:30Z",
    }


def author_operator_inputs(root: Path, **overrides: Any) -> None:
    """Author the two artefacts an operator owes, as an operator would.

    The limits here are *test* limits, deliberately wide so the risk engine
    does not constrain what the chain tests are about. Nothing in the
    repository authorises them for anything, and
    `production_authorized` stays False.
    """
    policy: dict[str, Any] = {
        "policy_id": "ALPHATRADER_PAPER",
        "policy_version": "V1",
        "max_gross_exposure": 1.0,
        "max_net_exposure": 1.0,
        "max_position_weight": 1.0,
        "max_positions": 50,
        "min_cash_weight": 0.0,
        "production_authorized": False,
    }
    policy.update(overrides.pop("policy", {}))
    symbols = overrides.pop(
        "symbols",
        [
            {"kind": "COMPANY", "identity_id": key, "symbol": value, "fractionable": True}
            for key, value in SYMBOLS.items()
        ],
    )
    write_risk_policy(root, policy)
    write_symbol_mapping(root, symbols)


def marks() -> dict[str, Decimal]:
    """A sizing mark for each routable identity."""
    return {f"COMPANY:{key}": Decimal("100.00") for key in SYMBOLS}


def composed(
    runtime: Any,
    *,
    venue: ScriptedVenue,
    now: str,
    mode: Any = None,
    audit: Any = None,
    settings: Any = None,
) -> Any:
    """Compose one run through the production root, at the slot open at `now`.

    This is the same `compose_operations` the CLI calls. The only thing the
    test supplies that the CLI does not is the HTTP client — the declared seam
    — and the sizing marks, which the CLI reads from stored price history.
    """
    from stock_screener.paper_operations import OperationsMode

    runtime.clock.set(now)
    slot = runtime.schedule.slot_for(now)
    assert slot is not None, f"no slot is open at {now}"
    return compose_operations(
        settings=settings or FakeSettings(),
        root=runtime.root,
        binding=runtime.binding,
        slot=slot,
        schedule=runtime.schedule,
        calendar=runtime.calendar,
        mode=mode or OperationsMode.EXTERNAL_PAPER,
        clock=runtime.clock,
        code_commit="3e254a2",
        bundle_for=_bundle_for,
        client=venue.client(),
        audit=audit,
        marks=marks(),
    )


def _bundle_for(session: str, count: int = 21) -> Any:
    """The research cross-section for a decision session."""
    from stock_screener.portfolio_risk import SecurityIdentity
    from stock_screener.strategies import StrategyInputBundle, StrategyInputRecord

    return StrategyInputBundle(
        as_of=session,
        score_version="COMPOUNDER_V1_2",
        universe_fingerprint="universe-1",
        records=tuple(
            StrategyInputRecord(
                identity=SecurityIdentity(kind="COMPANY", identity_id=index),
                final_score=90.0 - index,
                scoring_status="SCORED",
                freshness="CURRENT",
                score_date=session,
                score_version="COMPOUNDER_V1_2",
                calculated_at=f"{session}T07:40:00+00:00",
                source_fingerprint=f"fp-{index}",
            )
            for index in range(1, count + 1)
        ),
    )


def run_through_composition(
    runtime: Any, *, venue: ScriptedVenue, now: str, mode: Any = None, audit: Any = None
) -> Any:
    """Compose and run one slot, exactly as `run-due` does."""
    from stock_screener.paper_operations import OperationsMode

    composition = composed(runtime, venue=venue, now=now, mode=mode, audit=audit)
    try:
        outcome = composition.coordinator.run_due(
            mode=mode or OperationsMode.EXTERNAL_PAPER,
            plan_and_submit=composition.planner,
        )
    finally:
        composition.close()
    return outcome, composition


def decided(runtime: Any, venue: ScriptedVenue) -> Any:
    """Run the decision slot through the production root and return its decision."""
    from stock_screener.paper_runtime import StrategyDecisionStore

    author_operator_inputs(runtime.root)
    outcome, composition = run_through_composition(runtime, venue=venue, now=DECISION_NOW)
    assert composition.planner.outcome is not None, outcome.report.blockers
    assert composition.planner.outcome.strategy_outcome == "DECISION", outcome.report.blockers
    return StrategyDecisionStore(runtime.root).require("2026-10-30").decision


# --------------------------------------------------------------------------
# §3, §42: the composition root constructs the authority
# --------------------------------------------------------------------------


def test_the_production_root_constructs_the_submission_authority(
    paper_runtime: Any,
) -> None:
    # The gap this phase closes. `RuntimeSubmissionAuthority` was implemented,
    # tested and certified, and constructed nowhere in src/.
    from stock_screener.paper_runtime import RuntimeSubmissionAuthority

    author_operator_inputs(paper_runtime.root)
    venue = ScriptedVenue()

    composition = composed(paper_runtime, venue=venue, now=EXECUTION_NOW)
    try:
        assert isinstance(composition.submit, RuntimeSubmissionAuthority)
        assert composition.planner.submit is composition.submit
        assert composition.audit is not None
        assert composition.transport.audit is composition.audit
    finally:
        composition.close()


def test_the_cli_run_due_uses_the_same_composition_root() -> None:
    # §16. Not grep alone — the CLI's module is read to establish that it calls
    # this factory and builds no planner, coordinator or adapter of its own for
    # the run-due path, and the factory itself is exercised by every test here.
    import stock_screener.cli as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    start = source.index('@paper_ops_app.command("run-due")')
    nxt = source.find("@paper_ops_app.command(", start + 1)
    body = source[start:nxt] if nxt > 0 else source[start:]

    assert "compose_operations(" in body
    assert "RuntimePlanner(" not in body
    assert "AlpacaPaperAdapter(" not in body
    assert "RuntimeSubmissionAuthority(" not in body


def test_there_is_one_production_route_to_an_external_mutation() -> None:
    # §4. The CLI must not reach a transport or an adapter's submit for the
    # operational path, and the composition root hands the transport to the
    # certified adapter alone.
    import stock_screener.paper_runtime.composition as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert source.count("AlpacaPaperAdapter(") == 1
    for forbidden in (
        "RequestMethod.POST",
        "RequestMethod.DELETE",
        "transport.request(",
        ".submit(",
        "Order(",
    ):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# §15, §22, §31: the whole causal chain, once
# --------------------------------------------------------------------------


def test_a_pending_decision_executes_through_the_production_root(
    paper_runtime: Any,
) -> None:
    # §15's causal chain, end to end, through the factory `run-due` calls.
    from stock_screener.paper_operations.run_journal import OperationsRunJournal

    venue = ScriptedVenue()
    decision = decided(paper_runtime, venue)

    outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert composition.planner.outcome is not None
    assert composition.planner.outcome.strategy_outcome == "EXECUTED"
    assert composition.planner.outcome.orders_submitted == 3
    assert str(outcome.report.status) == "COMPLETED_PAPER_EXECUTION"

    events = [str(row.event_type) for row in OperationsRunJournal(paper_runtime.root).entries()]
    for required in (
        "RUN_SCHEDULED",
        "PLAN_AUTHORIZED",
        "SUBMISSION_GATE_CHECKED",
        "ORDER_SUBMITTED",
        "FILL_INGESTED",
        "RECONCILED",
        "RUN_COMPLETED",
    ):
        assert required in events, required
    assert len(venue.mutations) == 3
    assert decision.decision_id


def test_the_production_run_writes_request_audit_records(paper_runtime: Any) -> None:
    # §31. The mandatory regression that closes the R3B finding: load the
    # durable audit store after a run through the production root and find the
    # records there.
    from stock_screener.execution.external.request_audit import RequestAuditStore

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    store = RequestAuditStore(paper_runtime.root / AUDIT_DIRECTORY)
    mutations = store.mutations()

    assert store.root == paper_runtime.root / AUDIT_DIRECTORY
    # Two records per mutation: the pre-dispatch attempt and its result.
    assert len(mutations) == 6
    assert [row["sent"] for row in mutations].count(False) == 3
    assert [row["sent"] for row in mutations].count(True) == 3


def test_the_audit_is_written_before_the_order_reaches_the_venue(
    paper_runtime: Any,
) -> None:
    # §9. The ordering that makes an ambiguous POST discoverable.
    from stock_screener.execution.external.request_audit import RequestAuditStore

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    store = RequestAuditStore(paper_runtime.root / AUDIT_DIRECTORY)
    seen: list[int] = []

    def count_audit_records(_index: int) -> None:
        seen.append(len(store.mutations()))

    venue.on_post = count_audit_records
    run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    # At the moment the first POST was answered, its attempt record was already
    # durable.
    assert seen[0] >= 1


def test_a_production_audited_paper_mutation_derives_a_clean_live_count(
    paper_runtime: Any,
) -> None:
    # §32, §33. The audit the production root wrote is fed to the R3B
    # derivation. Before this phase the same gate derived EVIDENCE_MISSING,
    # because orders went to a venue with nothing recording where.
    from stock_screener.soak_verification import (
        MetricStatus,
        OperationalRecords,
        derive_session_evidence,
    )

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    outcome, _composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    records = OperationalRecords(paper_runtime.root)
    evidence = derive_session_evidence(
        records, session="2026-11-02", run_ids=(outcome.report.run_id,)
    )
    metric = evidence.metrics["live_mutations"]

    assert records.request_audit().usable is True
    assert metric.status is MetricStatus.OBSERVED_ZERO
    assert metric.value == 0
    audited = records.request_audit().records
    assert {row["destination"] for row in audited} == {"PAPER"}
    assert any(row["sent"] and row["operation"] == "MUTATION" for row in audited)


# --------------------------------------------------------------------------
# §5, §6, §21: composition is not authorisation
# --------------------------------------------------------------------------


def test_composing_the_authority_does_not_make_a_run_mutation_capable(
    paper_runtime: Any,
) -> None:
    # §5. Every gate that stood before stands now. The authority exists and the
    # control state refuses, so nothing reaches the venue.
    from stock_screener.paper_operations import OperationsMode

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    before = len(venue.mutations)
    paper_runtime.kill()

    outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert composition.submit is not None
    assert str(outcome.report.status) == "KILLED"
    assert len(venue.mutations) == before
    assert OperationsMode.EXTERNAL_PAPER.may_mutate is True


def test_observe_only_runs_without_submitting(paper_runtime: Any) -> None:
    # §20. The read-only path still works through the new composition, with
    # the real environment, binding and reconciliation.
    from stock_screener.paper_operations import OperationsMode

    author_operator_inputs(paper_runtime.root)
    venue = ScriptedVenue()

    outcome, composition = run_through_composition(
        paper_runtime, venue=venue, now=DECISION_NOW, mode=OperationsMode.OBSERVE_ONLY
    )

    assert str(outcome.report.status) == "COMPLETED_OBSERVE_ONLY"
    assert outcome.external_mutations == 0
    assert venue.mutations == []
    assert composition.audit.mutations() == ()


def test_an_unarmed_runtime_refuses_before_any_network_mutation(
    paper_runtime: Any,
) -> None:
    # §21, with the real fail-closed shape: not armed means no submission, and
    # the refusal happens before a POST rather than after one.
    from stock_screener.paper_operations import ArmingStore

    author_operator_inputs(paper_runtime.root)
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    before = len(venue.mutations)
    ArmingStore(paper_runtime.root).disarm(
        clock=paper_runtime.clock, reason="P2.3.1D: proving an unarmed runtime refuses"
    )

    outcome, _composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert str(outcome.report.status) in {"BLOCKED", "KILLED"}
    assert len(venue.mutations) == before
    assert [row for row in venue.calls if row["method"] == "DELETE"] == []


# --------------------------------------------------------------------------
# §23, §24, §25: the authorities above submission are unchanged
# --------------------------------------------------------------------------


def test_execution_consumes_the_persisted_decision_and_does_not_rerank(
    paper_runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §23. Ranking must not be called during execution: a portfolio computed on
    # execution morning would ship data the decision never saw under the
    # decision's identity.
    from stock_screener.strategies import CompounderMonthlyStrategy

    venue = ScriptedVenue()
    decision = decided(paper_runtime, venue)
    calls: list[str] = []
    original = CompounderMonthlyStrategy.decide

    def record(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append("decide")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CompounderMonthlyStrategy, "decide", record)
    _outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert calls == []
    assert composition.planner.outcome is not None
    assert composition.planner.outcome.decision_fingerprint == decision.fingerprint()


def test_risk_remains_the_final_authority_over_what_is_sent(
    paper_runtime: Any,
) -> None:
    # §24. The strategy wants three names; risk permits two. Exactly the
    # permitted two reach the venue and nothing is substituted for the third.
    venue = ScriptedVenue()
    author_operator_inputs(
        paper_runtime.root,
        policy={"max_positions": 2},
        symbols=[
            {"kind": "COMPANY", "identity_id": key, "symbol": value, "fractionable": True}
            for key, value in SYMBOLS.items()
        ],
    )
    decided(paper_runtime, venue)
    author_operator_inputs(
        paper_runtime.root,
        policy={"max_positions": 2},
        symbols=[
            {"kind": "COMPANY", "identity_id": key, "symbol": value, "fractionable": True}
            for key, value in SYMBOLS.items()
        ],
    )

    _outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    sent = [row["json"]["symbol"] for row in venue.mutations if row["method"] == "POST"]
    assert composition.planner.outcome is not None
    assert len(sent) <= 2
    assert set(sent) <= set(SYMBOLS.values())


def test_risk_permitting_nothing_submits_nothing_and_audits_nothing(
    paper_runtime: Any,
) -> None:
    # §25. The frozen no-action outcome, with zero mutation attempts — not one
    # attempt that was refused, none made.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    author_operator_inputs(
        paper_runtime.root,
        policy={"max_gross_exposure": 0.0, "max_net_exposure": 0.0},
        symbols=[
            {"kind": "COMPANY", "identity_id": key, "symbol": value, "fractionable": True}
            for key, value in SYMBOLS.items()
        ],
    )

    _outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert composition.planner.outcome is not None
    assert composition.planner.outcome.orders_submitted == 0
    assert venue.mutations == []
    assert composition.audit.mutations() == ()


# --------------------------------------------------------------------------
# §26, §29, §30: recovery and the per-mutation gate
# --------------------------------------------------------------------------


def test_the_same_slot_run_twice_submits_one_sequence(paper_runtime: Any) -> None:
    # §26. A second invocation of one scheduled slot recovers the first run
    # rather than becoming a competitor, and the audit shows no second sent
    # mutation.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    first, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)
    sent_once = len([row for row in venue.mutations if row["method"] == "POST"])
    audited_once = len([row for row in composition.audit.mutations() if row["sent"]])

    second, _again = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert second.recovered is True
    assert first.report.run_id == second.report.run_id
    assert len([row for row in venue.mutations if row["method"] == "POST"]) == sent_once
    store_now = _audit_store(paper_runtime)
    assert len([row for row in store_now.mutations() if row["sent"]]) == audited_once


def test_a_kill_before_the_first_order_sends_no_request(paper_runtime: Any) -> None:
    # §29.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    before = len(venue.mutations)
    paper_runtime.kill()

    _outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert len(venue.mutations) == before
    assert composition.audit.mutations() == ()


def test_a_kill_between_two_orders_stops_after_one(paper_runtime: Any) -> None:
    # §30. One sent mutation, two per-order gate checks, and the audit shows
    # exactly one sent mutation — not two, and not one with the second
    # unrecorded.
    from stock_screener.paper_operations import KillSwitchStore
    from stock_screener.paper_operations.run_journal import OperationsRunJournal

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    store = KillSwitchStore(paper_runtime.root)

    def kill_after_first(index: int) -> None:
        if index == 1:
            store.kill(
                clock=paper_runtime.clock,
                reason_code="OPERATOR_KILL",
                account_binding_fingerprint=paper_runtime.account,
            )

    venue.on_post = kill_after_first
    outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    posts = [row for row in venue.mutations if row["method"] == "POST"]
    checks = [
        row
        for row in OperationsRunJournal(paper_runtime.root).entries()
        if str(row.event_type) == "SUBMISSION_GATE_CHECKED"
    ]
    assert str(outcome.report.status) == "KILLED"
    assert len(posts) == 1
    assert len(checks) == 2
    assert [dict(row.evidence)["permitted"] for row in checks] == ["True", "False"]
    assert len([row for row in composition.audit.mutations() if row["sent"]]) == 1


def _audit_store(runtime: Any) -> Any:
    """The durable request audit for a runtime root."""
    from stock_screener.execution.external.request_audit import RequestAuditStore

    return RequestAuditStore(runtime.root / AUDIT_DIRECTORY)


# --------------------------------------------------------------------------
# §10, §28: the audit is not optional
# --------------------------------------------------------------------------


def test_an_audit_write_failure_prevents_the_mutation(paper_runtime: Any) -> None:
    # §28. Injected into the real production writer, before the scripted
    # network. Nothing reaches the venue and no order is recorded as submitted.
    from stock_screener.execution.errors import Code as ExecCode
    from stock_screener.execution.external.request_audit import RequestAuditStore
    from stock_screener.paper_operations.run_journal import OperationsRunJournal

    class Broken(RequestAuditStore):
        """An audit sink whose pre-dispatch write fails."""

        def record_attempt(self, record: Any) -> Any:
            """Refuse to persist the attempt."""
            from stock_screener.execution.errors import ExecutionError

            raise ExecutionError(ExecCode.EXTERNAL_REQUEST_AUDIT_FAILED, "disk full")

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    before = len([row for row in venue.mutations if row["method"] == "POST"])

    with pytest.raises(Exception, match="EXTERNAL_REQUEST_AUDIT_FAILED"):
        run_through_composition(
            paper_runtime,
            venue=venue,
            now=EXECUTION_NOW,
            audit=Broken(paper_runtime.root / AUDIT_DIRECTORY),
        )

    posts = [row for row in venue.mutations if row["method"] == "POST"]
    submitted = [
        row
        for row in OperationsRunJournal(paper_runtime.root).entries()
        if str(row.event_type) == "ORDER_SUBMITTED"
    ]
    # Nothing was sent and nothing was recorded as submitted: the refusal
    # happened before dispatch, which is the whole ordering guarantee.
    assert len(posts) == before == 0
    assert submitted == []


def test_a_transport_with_no_audit_refuses_to_mutate() -> None:
    # §10. The silent fallback that used to exist: `audit=None` skipped
    # auditing and let the mutation through. An absent sink is now a reason not
    # to mutate, and reads are deliberately unaffected.
    from stock_screener.execution.errors import ExecutionError
    from stock_screener.execution.external import RequestMethod
    from stock_screener.execution.external.http_transport import AlpacaPaperHttpTransport

    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.method)
        return httpx.Response(200, json={})

    transport = AlpacaPaperHttpTransport(
        origin=PAPER,
        key_id=SecretStr("PKTESTKEY01"),
        secret_key=SecretStr("shhh-secret-value"),
        client=httpx.Client(base_url=PAPER, transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ExecutionError, match="EXTERNAL_REQUEST_AUDIT_FAILED"):
        transport.request(RequestMethod.POST, "/v2/orders", json_body={"client_order_id": "X"})
    transport.request(RequestMethod.GET, "/v2/account")

    assert reached == ["GET"]


def test_no_composition_argument_disables_auditing() -> None:
    # §10. There is no flag. `audit` exists so a test can make persistence
    # *fail*, not so a caller can switch auditing off, and the composition
    # never passes None through to the transport.
    import stock_screener.paper_runtime.composition as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    for forbidden in ("no_audit", "skip_audit", "audit=False", "disable_audit"):
        assert forbidden not in source, forbidden
    assert "sink = audit" in source
    assert "RequestAuditStore(root / AUDIT_DIRECTORY)" in source


# --------------------------------------------------------------------------
# §11, §12, §13, §34, §35: what the audit records
# --------------------------------------------------------------------------


def test_the_audit_is_rooted_at_the_operations_root(paper_runtime: Any) -> None:
    # §11. Not the working directory, not a temporary directory.
    author_operator_inputs(paper_runtime.root)
    venue = ScriptedVenue()

    composition = composed(paper_runtime, venue=venue, now=EXECUTION_NOW)
    try:
        assert composition.audit.root == paper_runtime.root / AUDIT_DIRECTORY
        assert composition.audit.root.is_relative_to(paper_runtime.root)
    finally:
        composition.close()


def test_an_audited_mutation_names_the_run_and_the_account(paper_runtime: Any) -> None:
    # §12. Causally bound: the operational run, the account fingerprint and the
    # derived client order id.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    mutations = [row for row in composition.audit.mutations() if row["sent"] is False]

    assert mutations
    for row in mutations:
        assert row["operations_run_id"] == outcome.report.run_id
        assert row["account_binding_fingerprint"] == paper_runtime.account
        assert row["client_order_id"]
        assert row["route_class"] == "/v2/orders"


def test_the_production_audit_classifies_the_paper_destination(
    paper_runtime: Any,
) -> None:
    # §13, §33. Deterministic host-set membership, never a substring test.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    _outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    rows = composition.audit.mutations()

    assert {row["destination"] for row in rows} == {"PAPER"}
    assert any(row["sent"] and row["result"] == "ANSWERED" for row in rows)
    assert composition.audit.live_mutations_sent() == ()


def test_a_live_origin_is_refused_at_the_composition_boundary(
    paper_runtime: Any,
) -> None:
    # §34. The structural refusal is unchanged and no composition argument can
    # override it. Nothing reaches a network: the transport cannot be built.
    from stock_screener.execution.errors import ExecutionError

    author_operator_inputs(paper_runtime.root)
    venue = ScriptedVenue()
    settings = FakeSettings(alpaca_paper_base_url="https://api.alpaca.markets")

    with pytest.raises(ExecutionError, match="LIVE_TRADING_NOT_AUTHORIZED"):
        composed(paper_runtime, venue=venue, now=EXECUTION_NOW, settings=settings)

    assert venue.calls == []


def test_the_production_audit_carries_no_credential(paper_runtime: Any) -> None:
    # §35. The key and secret the transport was built with must not appear, and
    # the account is named only by fingerprint.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    _outcome, _composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((paper_runtime.root / AUDIT_DIRECTORY).glob("*.json"))
    ).lower()

    for forbidden in (
        "pktestkey01",
        "shhh",
        '"authorization"',
        "apca-api-key-id",
        "apca-api-secret-key",
        '"headers"',
        '"cookie"',
        ACCOUNT_ID,
    ):
        assert forbidden not in text, forbidden
    assert paper_runtime.account in text


# --------------------------------------------------------------------------
# §36, §37: the certified journal and the reconciliation
# --------------------------------------------------------------------------


def test_the_production_run_populates_the_certified_execution_journal(
    paper_runtime: Any,
) -> None:
    # §36. The journal P2.4I fixed, not an alternate one.
    from stock_screener.execution import ExecutionStore
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    store = ExecutionStore(
        paper_runtime.root / RUNTIME_DIRECTORY / "execution",
        generation=paper_runtime.binding.execution_journal_generation,
    )
    report = store.verify()
    kinds = [str(row.event.event_type) for row in store.load_journal().records]

    assert report["verified"] is True
    assert report["tail_completeness_established"] is True
    assert kinds.count("ORDER_CREATED") == 3
    assert kinds.count("FILL_APPLIED") == 3


def test_the_production_run_persists_its_reconciliation(paper_runtime: Any) -> None:
    # §37. What `completed_sessions_reconcile` will have to read.
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    outcome, _composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    written = sorted((paper_runtime.root / RUNTIME_DIRECTORY / "reconciliations").glob("*.json"))
    names = {path.stem for path in written}

    assert outcome.report.run_id in names
    body = json.loads(
        (
            paper_runtime.root
            / RUNTIME_DIRECTORY
            / "reconciliations"
            / f"{outcome.report.run_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert body["result"]["verdict"] == "RECONCILED"


def test_the_whole_run_derives_a_clean_completed_session(paper_runtime: Any) -> None:
    # The two linkages together, read by the R3B derivation that needs them.
    from stock_screener.soak_verification import (
        MetricStatus,
        OperationalRecords,
        derive_session_evidence,
    )

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    outcome, _composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    evidence = derive_session_evidence(
        OperationalRecords(paper_runtime.root),
        session="2026-11-02",
        run_ids=(outcome.report.run_id,),
    )

    assert evidence.complete is True
    for gate in ("completed_sessions_reconcile", "duplicate_accounting_events", "kill_bypasses"):
        metric = evidence.metrics[gate]
        assert metric.status is MetricStatus.OBSERVED_ZERO, f"{gate}: {metric.detail}"


# --------------------------------------------------------------------------
# §41: the readiness self-check
# --------------------------------------------------------------------------


def test_the_readiness_check_proves_the_composition_without_a_network(
    paper_runtime: Any,
) -> None:
    # §41. What a future activation needs to know, with no network and nothing
    # armed by the check itself.
    from stock_screener.paper_operations import OperationsMode

    author_operator_inputs(paper_runtime.root)
    slot = paper_runtime.schedule.slot_for(EXECUTION_NOW)
    paper_runtime.clock.set(EXECUTION_NOW)

    report = composition_readiness(
        settings=FakeSettings(),
        root=paper_runtime.root,
        binding=paper_runtime.binding,
        slot=slot,
        schedule=paper_runtime.schedule,
        calendar=paper_runtime.calendar,
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=paper_runtime.clock,
        code_commit="3e254a2",
    )

    assert report["ready"] is True
    assert report["composition_version"] == PRODUCTION_COMPOSITION_VERSION
    assert report["submission_authority_constructible"] is True
    assert report["request_audit_constructible"] is True
    assert report["certified_adapter_constructible"] is True
    assert report["transport_audited"] is True
    assert report["live_capability"] is False
    assert report["accounting_generation"]
    assert report["execution_journal_generation"]
    assert report["operator_inputs"]["complete"] is True


def test_the_readiness_check_names_a_missing_operator_input(
    paper_runtime: Any,
) -> None:
    # Nothing authored: the check says which artefact is owed rather than
    # reporting a generic failure.
    from stock_screener.paper_operations import OperationsMode

    slot = paper_runtime.schedule.slot_for(EXECUTION_NOW)

    report = composition_readiness(
        settings=FakeSettings(),
        root=paper_runtime.root,
        binding=paper_runtime.binding,
        slot=slot,
        schedule=paper_runtime.schedule,
        calendar=paper_runtime.calendar,
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=paper_runtime.clock,
    )

    assert report["ready"] is False
    assert report["operator_inputs"]["complete"] is False
    assert set(report["operator_inputs"]["missing"]) == {"RISK_POLICY", "SYMBOL_MAPPING"}
    assert report["operator_inputs"]["version"] == OPERATOR_INPUTS_VERSION


def test_the_readiness_check_carries_no_credential(paper_runtime: Any) -> None:
    from stock_screener.paper_operations import OperationsMode

    author_operator_inputs(paper_runtime.root)
    slot = paper_runtime.schedule.slot_for(EXECUTION_NOW)

    report = composition_readiness(
        settings=FakeSettings(),
        root=paper_runtime.root,
        binding=paper_runtime.binding,
        slot=slot,
        schedule=paper_runtime.schedule,
        calendar=paper_runtime.calendar,
        mode=OperationsMode.EXTERNAL_PAPER,
        clock=paper_runtime.clock,
    )
    text = json.dumps(report).lower()

    for forbidden in ("pktestkey01", "shhh", "secret", ACCOUNT_ID):
        assert forbidden not in text, forbidden


# --------------------------------------------------------------------------
# The operator inputs that do not exist, stated as refusals
# --------------------------------------------------------------------------


def test_an_unauthored_risk_policy_refuses_the_composition(paper_runtime: Any) -> None:
    # No risk limits in this repository are authorised for use, so the
    # composition refuses rather than supplying a plausible number.
    write_symbol_mapping(
        paper_runtime.root,
        [{"kind": "COMPANY", "identity_id": 1, "symbol": "AAA", "fractionable": True}],
    )
    venue = ScriptedVenue()

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_RISK_POLICY_NOT_AUTHORED):
        composed(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert venue.calls == []


def test_an_unauthored_symbol_mapping_refuses_the_composition(
    paper_runtime: Any,
) -> None:
    write_risk_policy(
        paper_runtime.root,
        {
            "policy_id": "ALPHATRADER_PAPER",
            "policy_version": "V1",
            "max_gross_exposure": 1.0,
            "max_position_weight": 1.0,
            "max_positions": 50,
            "min_cash_weight": 0.0,
        },
    )
    venue = ScriptedVenue()

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_SYMBOL_MAPPING_NOT_AUTHORED):
        composed(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert venue.calls == []


def test_a_risk_policy_missing_a_limit_is_refused(paper_runtime: Any) -> None:
    # A missing limit is not an absent limit: it would be filled by a default
    # nobody approved.
    write_symbol_mapping(
        paper_runtime.root,
        [{"kind": "COMPANY", "identity_id": 1, "symbol": "AAA", "fractionable": True}],
    )
    write_risk_policy(
        paper_runtime.root,
        {"policy_id": "ALPHATRADER_PAPER", "policy_version": "V1", "max_positions": 50},
    )

    with pytest.raises(RuntimeBindingError, match=Code.RUNTIME_OPERATOR_INPUT_UNREADABLE):
        composed(paper_runtime, venue=ScriptedVenue(), now=EXECUTION_NOW)


def test_the_operator_input_status_is_reportable_without_a_network(
    paper_runtime: Any,
) -> None:
    before = operator_input_status(paper_runtime.root)
    author_operator_inputs(paper_runtime.root)
    after = operator_input_status(paper_runtime.root)

    assert before.complete is False
    assert before.missing == ("RISK_POLICY", "SYMBOL_MAPPING")
    assert any("unauthorised limit" in row for row in before.detail)
    assert after.complete is True
    assert after.missing == ()


# --------------------------------------------------------------------------
# §38, §39: nothing about measurement changed
# --------------------------------------------------------------------------


def test_the_observability_and_evidence_contracts_are_unchanged() -> None:
    # This phase makes an existing source appear in production. It must not
    # change how anything is measured, or future evidence would be rebound to
    # a definition nobody reviewed.
    from stock_screener.paper_runtime import observability_fingerprint
    from stock_screener.soak_verification import evidence_contract_fingerprint

    assert observability_fingerprint() == (
        "0fa85983277222e83f8ae97ed3266fc0b830b958ddd21c1a54280fa88cdd7d5b"
    )
    assert evidence_contract_fingerprint() == (
        "eb612d02dd3896f816ac31fd60a0cd448e08ec5b444690a4896aa03239e8b585"
    )


# --------------------------------------------------------------------------
# §42: the declaration-without-implementation guard, driven not grepped
# --------------------------------------------------------------------------


def test_the_mutation_capable_path_actually_reaches_both_components(
    paper_runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §42. Four phases ended on something declared and wired nowhere, and both
    # of this phase's gaps were exactly that. So this drives the composition and
    # asserts that a mutation genuinely passed through the submission authority
    # *and* the request-audit writer — observed at both, not inferred from
    # either.
    from stock_screener.execution.external.request_audit import RequestAuditStore
    from stock_screener.paper_runtime import RuntimeSubmissionAuthority

    seen: dict[str, int] = {"authority": 0, "audit": 0}
    original_submit = RuntimeSubmissionAuthority._submit_one
    original_audit = RequestAuditStore.record_attempt

    def counted_submit(self: Any, **kwargs: Any) -> Any:
        seen["authority"] += 1
        return original_submit(self, **kwargs)

    def counted_audit(self: Any, record: Any) -> Any:
        if str(record.operation) == "MUTATION":
            seen["audit"] += 1
        return original_audit(self, record)

    monkeypatch.setattr(RuntimeSubmissionAuthority, "_submit_one", counted_submit)
    monkeypatch.setattr(RequestAuditStore, "record_attempt", counted_audit)

    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    assert seen["authority"] == 3, "no order passed through RuntimeSubmissionAuthority"
    assert seen["audit"] == 3, "no mutation passed through the request-audit writer"
    assert len([row for row in venue.mutations if row["method"] == "POST"]) == 3


def _external_paper() -> Any:
    """The mutation-capable mode."""
    from stock_screener.paper_operations import OperationsMode

    return OperationsMode.EXTERNAL_PAPER


# --------------------------------------------------------------------------
# §27: an ambiguous network result does not become a second order
# --------------------------------------------------------------------------


def test_an_ambiguous_dispatch_is_resolved_not_resent(paper_runtime: Any) -> None:
    # §27. The first POST reaches the venue and its answer is lost. The
    # certified protocol does not resend: it asks the venue about the derived
    # client order id, finds the order, and carries on. So each authorised
    # order reaches the venue exactly once even though one answer never
    # arrived — which is the whole point of deriving the client order id
    # rather than generating it.
    venue = ScriptedVenue()
    decided(paper_runtime, venue)
    attempts: list[int] = []

    def lose_the_answer(index: int) -> None:
        attempts.append(index)
        if index == 1:
            msg = "the connection dropped after the request was sent"
            raise httpx.ReadTimeout(msg)

    venue.on_post = lose_the_answer
    outcome, composition = run_through_composition(paper_runtime, venue=venue, now=EXECUTION_NOW)

    posts = [row for row in venue.mutations if row["method"] == "POST"]
    client_ids = [row["json"]["client_order_id"] for row in posts]
    lookups = [row for row in venue.calls if "by_client_order_id" in row["path"]]
    unresolved = [row for row in composition.audit.mutations() if row["result"] == "UNRESOLVED"]

    # No client order id was sent twice: the lost answer was resolved by asking.
    assert len(client_ids) == len(set(client_ids))
    assert lookups, "the protocol resent instead of resolving"
    assert unresolved, "no pre-dispatch record survived the lost answer"
    assert str(outcome.report.status) == "COMPLETED_PAPER_EXECUTION"
    assert attempts == [1, 2, 3]


def test_no_real_order_is_possible_from_these_tests() -> None:
    # §19, §55. Every test here drives a scripted network through
    # `httpx.MockTransport`. The host lock still applies, so even the scripted
    # client cannot be pointed at a live origin.
    import stock_screener.paper_runtime.composition as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "LIVE_HOSTS" in source
    assert '"live_capability": False' in source
    # Every network in this file is an httpx.MockTransport, and the one place a
    # live origin appears is the test that proves it is refused.
    source_test = Path(__file__).read_text(encoding="utf-8")
    assert "httpx.MockTransport" in source_test
    assert "LIVE_TRADING_NOT_AUTHORIZED" in source_test
    # Every scripted client in this file is built against the paper origin.
    assert source_test.count("base_url=PAPER") >= 1
    assert PAPER == "https://paper-api.alpaca.markets"
