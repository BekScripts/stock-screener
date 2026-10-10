"""The whole authority chain, end to end, with only the network faked.

Every authority here is the real one: S1.0 ranks, the real risk engine permits,
the real execution engine plans, the certified adapter submits, accounting V2
admits the fills. Only the HTTPS transport is scripted, because the alternative
is a real order and P2.2E already certified that boundary.

The assertions concentrate on the authority boundaries rather than on the happy
path, because the happy path is the easy part. What matters is that risk can
reduce the strategy and execution follows risk; that risk cannot invent
exposure; that a frozen decision is never re-ranked; and that nothing reaches
the venue when a gate says no.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.execution import ExecutionCalendar, fingerprint_of
from stock_screener.execution.external import (
    ACCOUNT_PATH,
    ACTIVITIES_PATH,
    ORDERS_PATH,
    POSITIONS_PATH,
    AlpacaPaperAdapter,
    BrokerCapabilities,
    BrokerEnvironment,
    BrokerResponse,
    ExternalBrokerAccountBinding,
    FakeTransport,
    RequestMethod,
    SubmissionIntentLog,
    SymbolMapping,
    TransportOutcome,
    VenueSymbol,
    prove_paper_environment,
)
from stock_screener.paper_runtime import RuntimeBindingError
from stock_screener.paper_runtime.risk_transformation import transformation_for
from stock_screener.paper_runtime.submission import RuntimeSubmissionAuthority
from stock_screener.portfolio_risk import (
    PortfolioSnapshot,
    RiskPolicy,
    SecurityIdentity,
    evaluate,
)
from stock_screener.strategies import (
    StrategyInputBundle,
    StrategyInputRecord,
    evaluate_session,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

PAPER_URL = "https://paper-api.alpaca.markets"
ACCOUNT_ID = "paper-account-1"
DECISION_SESSION = "2026-10-30"
EXECUTION_SESSION = "2026-11-02"
SCORE_COMPUTED_AT = "2026-10-30T07:40:00+00:00"
NOW = "2026-11-02T13:35:00+00:00"

#: Three identities the mapping can route, so a plan can be sized.
SYMBOLS: dict[int, str] = {1: "AAA", 2: "BBB", 3: "CCC"}


def calendar() -> ExecutionCalendar:
    """The market calendar."""
    return ExecutionCalendar(
        sessions=("2026-10-29", DECISION_SESSION, EXECUTION_SESSION), name="P2_3_1C"
    )


def mapping() -> SymbolMapping:
    """A mapping covering the three selectable identities."""
    return SymbolMapping.of(
        [
            VenueSymbol(kind="COMPANY", identity_id=key, symbol=value)
            for key, value in SYMBOLS.items()
        ]
    )


def binding() -> ExternalBrokerAccountBinding:
    """A proven paper binding that permits fractional equity trading."""
    return ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=ACCOUNT_ID,
        proof=prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER_URL, key_id="PKTESTKEY01"
        ),
        capabilities=BrokerCapabilities(fractional_quantities=True),
    )


def answered(body: Any, *, status: int = 200) -> BrokerResponse:
    """A successful provider reply."""
    return BrokerResponse(outcome=TransportOutcome.ANSWERED, status_code=status, body=body)


def decision_for(count: int = 21) -> Any:
    """A frozen month-end decision selecting the top decile of `count` names."""
    records = tuple(
        StrategyInputRecord(
            identity=SecurityIdentity(kind="COMPANY", identity_id=i),
            final_score=90.0 - i,
            scoring_status="SCORED",
            freshness="CURRENT",
            score_date=DECISION_SESSION,
            score_version="COMPOUNDER_V1_2",
            calculated_at=SCORE_COMPUTED_AT,
            source_fingerprint=f"fp-{i}",
        )
        for i in range(1, count + 1)
    )
    report = evaluate_session(
        StrategyInputBundle(
            as_of=DECISION_SESSION,
            score_version="COMPOUNDER_V1_2",
            universe_fingerprint="universe-1",
            records=records,
        ),
        session=DECISION_SESSION,
        calendar=calendar(),
    )
    assert report.decision is not None
    return report.decision


def policy(**overrides: Any) -> RiskPolicy:
    """A risk policy; overrides bind one limit at a time."""
    settings: dict[str, Any] = {
        "policy_id": "ALPHATRADER_PAPER",
        "policy_version": "V1",
        "max_gross_exposure": 1.0,
        "max_net_exposure": 1.0,
        "max_position_weight": 1.0,
        "max_positions": 50,
        "min_cash_weight": 0.0,
    }
    settings.update(overrides)
    return RiskPolicy(**settings)


def risk_for(decision: Any, *, cash: float = 100_000.0, **overrides: Any) -> Any:
    """Run the real risk engine over a frozen decision's own intents."""
    from stock_screener.strategies import CompounderMonthlyStrategy

    return evaluate(
        list(CompounderMonthlyStrategy().intents(decision)),
        PortfolioSnapshot(as_of=NOW, cash=cash, positions=()),
        policy(**overrides),
    )


# --------------------------------------------------------------------------
# §32: risk reduces, execution follows risk
# --------------------------------------------------------------------------


def test_the_strategy_decision_survives_a_risk_reduction_unchanged() -> None:
    decision = decision_for()
    before = decision.fingerprint()

    transformation_for(
        decision=decision,
        approved=risk_for(decision, max_gross_exposure=0.30, max_net_exposure=0.30).approved,
        risk_policy_fingerprint="pol",
    )

    assert decision.fingerprint() == before
    assert [row.identity.identity_id for row in decision.selections] == [1, 2, 3]


def test_a_risk_reduction_is_recorded_rather_than_rewritten() -> None:
    decision = decision_for()

    transformation = transformation_for(
        decision=decision,
        approved=risk_for(decision, max_gross_exposure=0.30, max_net_exposure=0.30).approved,
        risk_policy_fingerprint="pol",
    )

    assert transformation.outcome == "PROCEED"
    assert transformation.desired_identities == ("COMPANY:1", "COMPANY:2", "COMPANY:3")
    assert transformation.approved_identities == ("COMPANY:1", "COMPANY:2", "COMPANY:3")
    assert all(row.reduction > 0 for row in transformation.rows)
    assert transformation.body()["strategy_decision_mutated"] is False


def test_a_name_risk_zeroes_keeps_its_place_in_the_decision() -> None:
    # §32's shape: the decision stays A/B/C and the transformation reports the
    # one risk removed. Nothing replaces it and nothing is re-ranked.
    decision = decision_for()
    approved = risk_for(decision).approved
    reduced = {
        label: (0.0 if label == "COMPANY:2" else weight)
        for label, weight in dict(approved.targets).items()
    }

    transformation = transformation_for(
        decision=decision,
        approved=_ApprovedStub(targets=reduced, gross_exposure=2 / 3),
        risk_policy_fingerprint="pol",
    )

    assert transformation.zeroed_identities == ("COMPANY:2",)
    assert transformation.approved_identities == ("COMPANY:1", "COMPANY:3")
    assert [row.identity.identity_id for row in decision.selections] == [1, 2, 3]


# --------------------------------------------------------------------------
# §33, §34, §35: the authority limits
# --------------------------------------------------------------------------


def test_risk_permitting_nothing_is_a_valid_no_action_outcome() -> None:
    decision = decision_for()

    transformation = transformation_for(
        decision=decision,
        approved=_ApprovedStub(targets={}, cash_target_weight=1.0),
        risk_policy_fingerprint="pol",
    )

    assert transformation.outcome == "RISK_CONSTRAINED_NO_ACTION"
    assert transformation.approved_identities == ()
    assert len(transformation.zeroed_identities) == 3


def test_risk_cannot_grant_exposure_the_strategy_never_requested() -> None:
    decision = decision_for()

    with pytest.raises(RuntimeBindingError, match="RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY"):
        transformation_for(
            decision=decision,
            approved=_ApprovedStub(targets={"COMPANY:1": 0.2, "COMPANY:999": 0.2}),
            risk_policy_fingerprint="pol",
        )


def test_risk_cannot_raise_an_exposure_above_the_request() -> None:
    decision = decision_for()

    with pytest.raises(RuntimeBindingError, match="RISK_OUTPUT_EXCEEDS_STRATEGY_AUTHORITY"):
        transformation_for(
            decision=decision,
            approved=_ApprovedStub(targets={"COMPANY:1": 0.9}),
            risk_policy_fingerprint="pol",
        )


def test_an_unconstrained_pass_through_is_not_an_expansion() -> None:
    # The float-noise case. An unconstrained policy returns the requested
    # weight exactly, and a strict float comparison would call that an
    # increase — which would block every unconstrained run.
    decision = decision_for()

    transformation = transformation_for(
        decision=decision,
        approved=risk_for(decision).approved,
        risk_policy_fingerprint="pol",
    )

    assert transformation.outcome == "PROCEED"
    assert all(row.reduction == pytest.approx(0.0, abs=1e-9) for row in transformation.rows)


def test_an_exit_at_zero_is_never_read_as_unauthorised_exposure() -> None:
    # A name the strategy exits has desired 0. Risk leaving it at 0 must not
    # trip the expansion guard.
    decision = decision_for()

    transformation = transformation_for(
        decision=decision,
        approved=_ApprovedStub(targets={"COMPANY:1": 0.1}),
        risk_policy_fingerprint="pol",
    )

    assert transformation.outcome == "PROCEED"
    assert transformation.approved_identities == ("COMPANY:1",)


class _ApprovedStub:
    """An approved portfolio with chosen targets, for the authority-limit cases.

    A stub is used only where a *malformed* risk output is the thing under test:
    the real engine cannot produce one, which is the point, so there is no way
    to obtain it except by constructing it.
    """

    def __init__(
        self,
        *,
        targets: dict[str, float],
        gross_exposure: float = 0.0,
        cash_target_weight: float = 0.0,
    ) -> None:
        self.targets = targets
        self.gross_exposure = gross_exposure
        self.cash_target_weight = cash_target_weight
        self.risk_decisions: tuple[Any, ...] = ()

    def fingerprint(self) -> str:
        """A stable digest for the stub."""
        return fingerprint_of(dict(sorted(self.targets.items())))


# --------------------------------------------------------------------------
# §8, §20, §31: the submission authority, through the certified path
# --------------------------------------------------------------------------


def account_body(cash: str = "100000.00") -> dict[str, Any]:
    """A clean paper account."""
    return {"id": ACCOUNT_ID, "status": "ACTIVE", "cash": cash, "buying_power": "200000.00"}


def order_body(*, remote_id: str, client_id: str, symbol: str, qty: str) -> dict[str, Any]:
    """One provider order object, as the real endpoint sends it."""
    return {
        "id": remote_id,
        "client_order_id": client_id,
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "filled_qty": qty,
        "status": "filled",
        "type": "market",
        "time_in_force": "day",
    }


def fill_activity(
    *, remote_id: str, order_id: str, symbol: str, qty: str, price: str
) -> dict[str, Any]:
    """One FILL activity row."""
    return {
        "id": remote_id,
        "activity_type": "FILL",
        "type": "fill",
        "order_id": order_id,
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "price": price,
        "transaction_time": "2026-11-02T13:35:30Z",
    }


class RecordingGate:
    """A submission gate that records every consultation.

    The real `SubmissionGate` needs a kill store and an incident store; this
    records the one property the chain test cares about — that the gate is
    asked once per order rather than once per run — and can be told to refuse
    at a chosen order.
    """

    def __init__(self, refuse_at: int | None = None) -> None:
        self.checks: list[str] = []
        self.refuse_at = refuse_at

    def require(self, *, context: str, expected_account: str) -> None:
        """Record the consultation, refusing at the chosen order."""
        self.checks.append(context)
        if self.refuse_at is not None and len(self.checks) >= self.refuse_at:
            from stock_screener.paper_operations import Code as OpsCode
            from stock_screener.paper_operations import OperationsError

            raise OperationsError(
                OpsCode.PAPER_OPERATIONS_KILLED,
                f"the control state forbids submission ({context})",
            )


def bootstrapped(tmp_path: Path, *, cash: str = "100000.00") -> Any:
    """A bootstrapped runtime with a real V2 book."""
    from stock_screener.paper_runtime import RuntimeBindingStore, bootstrap_paper_book
    from stock_screener.strategies import CompounderMonthlyStrategy

    @dataclass
    class Snapshot:
        """The venue's view of a clean account at bootstrap."""

        cash: Decimal
        positions: tuple[Any, ...] = ()
        account_binding_fingerprint: str = ""

    strategy = CompounderMonthlyStrategy()
    root = tmp_path / ".operations"
    bootstrap_paper_book(
        root=root,
        runtime_id="alphatrader1",
        account=Snapshot(cash=Decimal(cash)),
        account_evidence_fingerprint="ev-" + "f" * 61,
        open_orders=0,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_fingerprint=strategy.fingerprint(),
        strategy_authorization_fingerprint="b" * 64,
        operations_policy_fingerprint="pol",
        calendar_fingerprint=calendar().fingerprint(),
        schedule_fingerprint="sch",
        code_commit="2e0b940",
        now="2026-10-28T12:00:00+00:00",
        confirmed=True,
    )
    return RuntimeBindingStore(root).require()


def marks() -> dict[str, Decimal]:
    """A sizing mark for each selectable identity."""
    return {f"COMPANY:{key}": Decimal("100.00") for key in SYMBOLS}


def scripted_transport(decision: Any, *, names: int = 3) -> FakeTransport:
    """A transport scripted for `names` submissions and their fills."""
    from stock_screener.execution.external import client_order_id_for

    transport = FakeTransport()
    transport.script(RequestMethod.GET, ACCOUNT_PATH, answered(account_body()))
    transport.script(RequestMethod.GET, POSITIONS_PATH, answered([]))
    for index in range(names):
        remote = f"remote-{index + 1}"
        transport.script(
            RequestMethod.POST,
            ORDERS_PATH,
            answered(
                order_body(
                    remote_id=remote,
                    client_id=client_order_id_for(f"{decision.decision_id}-{index}"),
                    symbol=SYMBOLS[index + 1],
                    qty="333",
                )
            ),
        )
        transport.script(
            RequestMethod.GET,
            ACTIVITIES_PATH,
            answered(
                [
                    fill_activity(
                        remote_id=f"exec-{index + 1}",
                        order_id=remote,
                        symbol=SYMBOLS[index + 1],
                        qty="333",
                        price="100.00",
                    )
                ]
            ),
        )
    return transport


def authority(tmp_path: Path, transport: FakeTransport, **overrides: Any) -> Any:
    """A submission authority over a real adapter and a scripted transport."""
    adapter = AlpacaPaperAdapter(transport=transport, binding=binding(), mapping=mapping())
    return RuntimeSubmissionAuthority(
        root=tmp_path / ".operations",
        adapter=adapter,
        risk_policy=policy(**overrides),
        risk_policy_fingerprint="risk-pol-1",
        marks=marks(),
        intent_log=SubmissionIntentLog(tmp_path / ".operations" / "submissions"),
    )


def test_the_submission_authority_holds_no_transport_mutation_call() -> None:
    # §9. Every mutation must go through the certified execution authority, and
    # the structural guarantee is that this module cannot reach a transport.
    from pathlib import Path as _Path

    import stock_screener.paper_runtime.submission as module

    source = _Path(module.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "AlpacaPaperHttpTransport",
        "RequestMethod.POST",
        "RequestMethod.DELETE",
        "transport.request",
        ".post(",
        ".delete(",
        "ORDERS_PATH",
    ):
        assert forbidden not in source, forbidden


def test_risk_constrained_to_nothing_submits_no_order(tmp_path: Path) -> None:
    # §33 end to end: the real engine, a policy that permits nothing, and a
    # transport that would have answered a POST had one been made.
    decision = decision_for()
    runtime = bootstrapped(tmp_path)
    transport = scripted_transport(decision)
    submit = authority(tmp_path, transport, max_gross_exposure=0.0, max_net_exposure=0.0)

    result = submit(
        decision=decision,
        gate=RecordingGate(),
        account=runtime.account_binding_fingerprint,
        run_id="opsrun-exec",
        now=NOW,
        binding=runtime,
    )

    assert result["orders_submitted"] == 0
    assert result["external_mutations"] == 0
    assert result["strategy_outcome"] == "COMPLETED_NO_ACTION_RISK_CONSTRAINED"
    assert [row for row in transport.calls if row["method"] == "POST"] == []


def test_the_transformation_is_recorded_beside_the_decision(tmp_path: Path) -> None:
    # §5. Risk's answer depends on the book and the policy at this instant and
    # is not reconstructable from the decision alone, so it is written down.
    import json

    decision = decision_for()
    runtime = bootstrapped(tmp_path)
    submit = authority(
        tmp_path, scripted_transport(decision), max_gross_exposure=0.0, max_net_exposure=0.0
    )

    submit(
        decision=decision,
        gate=RecordingGate(),
        account=runtime.account_binding_fingerprint,
        run_id="opsrun-exec",
        now=NOW,
        binding=runtime,
    )

    path = tmp_path / ".operations" / "runtime" / "transformations" / f"{DECISION_SESSION}.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["authority_rule"] == "RISK_MAY_REDUCE_NEVER_EXPAND_STRATEGY_AUTHORITY_V1"
    assert body["decision_fingerprint"] == decision.fingerprint()
    assert body["strategy_decision_mutated"] is False


def test_no_risk_policy_is_never_invented(tmp_path: Path) -> None:
    decision = decision_for()
    runtime = bootstrapped(tmp_path)
    submit = authority(tmp_path, scripted_transport(decision))
    submit.risk_policy = None

    with pytest.raises(RuntimeBindingError, match="RISK_POLICY_NOT_BOUND"):
        submit(
            decision=decision,
            gate=RecordingGate(),
            account=runtime.account_binding_fingerprint,
            run_id="opsrun-exec",
            now=NOW,
            binding=runtime,
        )


def test_the_execution_never_reranks_the_frozen_decision(tmp_path: Path) -> None:
    # §10. The intents are re-derived from the record, so the only way the
    # selection could change is if the strategy were asked to rank again — and
    # no score source reaches this module at all.
    from pathlib import Path as _Path

    import stock_screener.paper_runtime.submission as module

    source = _Path(module.__file__).read_text(encoding="utf-8")

    for forbidden in ("load_bundle", "ScoreSnapshotRepository", "evaluate_session", "decide("):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# §16, §17, §18, §66, §67: missed slots
# --------------------------------------------------------------------------


def schedule_for() -> Any:
    """The V1.1 operational schedule over the test calendar."""
    from stock_screener.paper_operations.strategy_schedule import (
        StrategyOperationsSchedule,
    )

    return StrategyOperationsSchedule(
        schedule_id="ALPHATRADER_PAPER_V1_1",
        timezone="America/New_York",
        calendar=calendar(),
    )


def pending_decision(tmp_path: Path) -> Any:
    """A runtime holding a decision awaiting execution."""
    from stock_screener.paper_runtime import (
        DecisionStatus,
        PersistedStrategyDecision,
        StrategyDecisionStore,
    )

    runtime = bootstrapped(tmp_path)
    decision = decision_for()
    store = StrategyDecisionStore(tmp_path / ".operations")
    store.record(
        PersistedStrategyDecision(
            decision=decision,
            status=DecisionStatus.PENDING_EXECUTION,
            runtime_id=runtime.runtime_id,
            strategy_authorization_fingerprint=runtime.strategy_authorization_fingerprint,
            recorded_at="2026-10-30T20:00:00+00:00",
        )
    )
    return runtime, decision, store


def test_a_pending_decision_inside_its_window_is_left_alone(tmp_path: Path) -> None:
    from stock_screener.paper_runtime import DecisionStatus, expire_pending_executions

    _, _, store = pending_decision(tmp_path)

    # 09:40 ET on the execution session: inside the 09:35 + 25m window.
    expired = expire_pending_executions(
        tmp_path / ".operations", schedule=schedule_for(), now="2026-11-02T14:40:00+00:00"
    )

    assert expired == ()
    assert store.require(DECISION_SESSION).status is DecisionStatus.PENDING_EXECUTION


def test_a_closed_execution_window_moves_the_decision_to_terminal(tmp_path: Path) -> None:
    # §18. Left PENDING_EXECUTION it is a loaded gun: the next scheduler tick
    # that found it would submit it.
    from stock_screener.paper_runtime import DecisionStatus, expire_pending_executions

    _, _, store = pending_decision(tmp_path)

    expired = expire_pending_executions(
        tmp_path / ".operations", schedule=schedule_for(), now="2026-11-02T19:00:00+00:00"
    )

    assert len(expired) == 1
    assert expired[0].reason_code == "MISSED_STRATEGY_EXECUTION"
    record = store.require(DECISION_SESSION)
    assert record.status is DecisionStatus.EXECUTION_MISSED
    assert record.status.is_terminal is True
    assert record.status.may_execute is False


def test_a_missed_execution_is_recorded_permanently(tmp_path: Path) -> None:
    from stock_screener.paper_runtime import expire_pending_executions, missed_records

    pending_decision(tmp_path)
    expire_pending_executions(
        tmp_path / ".operations", schedule=schedule_for(), now="2026-11-02T19:00:00+00:00"
    )

    rows = missed_records(tmp_path / ".operations")
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "MISSED_STRATEGY_EXECUTION"
    assert rows[0]["backfilled"] is False
    assert rows[0]["recoverable"] is False


def test_expiring_twice_records_one_miss(tmp_path: Path) -> None:
    # A scheduler noticing the same closed window on several ticks must not
    # accumulate duplicates.
    from stock_screener.paper_runtime import expire_pending_executions, missed_records

    pending_decision(tmp_path)
    root = tmp_path / ".operations"
    expire_pending_executions(root, schedule=schedule_for(), now="2026-11-02T19:00:00+00:00")
    expire_pending_executions(root, schedule=schedule_for(), now="2026-11-02T20:00:00+00:00")

    assert len(missed_records(root)) == 1


def test_a_missed_execution_cannot_then_be_executed(tmp_path: Path) -> None:
    # §17. The terminal state is what makes "never late" structural.
    from stock_screener.paper_operations.strategy_schedule import RunPurpose
    from stock_screener.paper_runtime import (
        DecisionStatus,
        RuntimePlanner,
        expire_pending_executions,
    )

    runtime, _, store = pending_decision(tmp_path)
    root = tmp_path / ".operations"
    expire_pending_executions(root, schedule=schedule_for(), now="2026-11-02T19:00:00+00:00")
    assert store.require(DECISION_SESSION).status is DecisionStatus.EXECUTION_MISSED

    slot = next(
        row
        for row in schedule_for().slots_for_session(EXECUTION_SESSION)
        if row.purpose is RunPurpose.STRATEGY_EXECUTION
    )
    planner = RuntimePlanner(
        root=root, binding=runtime, slot=slot, calendar=calendar(), submit=_never_submit
    )

    with pytest.raises(RuntimeBindingError, match="DECISION_EXECUTION_WINDOW_MISSED"):
        planner(run_id="opsrun-late", mode=None, gate=RecordingGate(), account="x", clock=None)


def _never_submit(**_kwargs: Any) -> dict[str, Any]:
    """A submission authority that must never be reached."""
    msg = "nothing may be submitted in this test"
    raise AssertionError(msg)


# --------------------------------------------------------------------------
# §37, §38, §42, §44: recovery and the gates
# --------------------------------------------------------------------------


def execution_planner(tmp_path: Path, runtime: Any, **kwargs: Any) -> Any:
    """A planner for the execution slot."""
    from stock_screener.paper_operations.strategy_schedule import RunPurpose
    from stock_screener.paper_runtime import RuntimePlanner

    slot = next(
        row
        for row in schedule_for().slots_for_session(EXECUTION_SESSION)
        if row.purpose is RunPurpose.STRATEGY_EXECUTION
    )
    return RuntimePlanner(
        root=tmp_path / ".operations",
        binding=runtime,
        slot=slot,
        calendar=calendar(),
        **kwargs,
    )


def test_a_second_execution_invocation_is_a_no_op(tmp_path: Path) -> None:
    # §37. The first run completes the decision; the second recovers it.
    from stock_screener.paper_runtime import DecisionStatus

    runtime, _, store = pending_decision(tmp_path)
    calls: list[int] = []

    def submit(**_kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return {"orders_submitted": 1, "planned_orders": 1, "external_mutations": 1}

    planner = execution_planner(tmp_path, runtime, submit=submit)
    first = planner(run_id="opsrun-exec", mode=None, gate=RecordingGate(), account="x", clock=None)
    second = planner(run_id="opsrun-exec", mode=None, gate=RecordingGate(), account="x", clock=None)

    assert len(calls) == 1
    assert first["orders_submitted"] == 1
    assert second["orders_submitted"] == 0
    assert second["strategy_outcome"] == "ALREADY_EXECUTED"
    assert store.require(DECISION_SESSION).status is DecisionStatus.EXECUTION_COMPLETED


def test_a_restart_loads_the_identical_frozen_decision(tmp_path: Path) -> None:
    # §38. A fresh store over the same root, as a new process would see it.
    from stock_screener.paper_runtime import StrategyDecisionStore

    _, decision, _ = pending_decision(tmp_path)

    reloaded = StrategyDecisionStore(tmp_path / ".operations").require(DECISION_SESSION)

    assert reloaded.decision_fingerprint == decision.fingerprint()
    assert [row.identity.label for row in reloaded.decision.selections] == [
        "COMPANY:1",
        "COMPANY:2",
        "COMPANY:3",
    ]
    assert [row.desired_weight for row in reloaded.decision.selections] == [
        pytest.approx(1 / 3)
    ] * 3
    assert reloaded.first_execution_session == EXECUTION_SESSION


def test_a_kill_before_the_first_order_submits_nothing(tmp_path: Path) -> None:
    # §42. The gate refuses at the first consultation and no order is sent.
    from stock_screener.paper_operations import OperationsError

    decision = decision_for()
    runtime = bootstrapped(tmp_path)
    transport = scripted_transport(decision)
    submit = authority(tmp_path, transport)
    gate = RecordingGate(refuse_at=1)

    with pytest.raises(OperationsError, match="PAPER_OPERATIONS_KILLED"):
        submit(
            decision=decision,
            gate=gate,
            account=runtime.account_binding_fingerprint,
            run_id="opsrun-exec",
            now=NOW,
            binding=runtime,
        )

    assert [row for row in transport.calls if row["method"] == "POST"] == []
    assert submit.submitted == 0


def test_a_kill_between_two_orders_stops_only_the_second(tmp_path: Path) -> None:
    # The critical mid-run case, and the reason the gate is asked per order
    # rather than per run. Refusing at the second consultation must leave the
    # first order standing and the second unsent.
    from stock_screener.paper_operations import OperationsError

    decision = decision_for()
    runtime = bootstrapped(tmp_path)
    transport = scripted_transport(decision)
    submit = authority(tmp_path, transport)
    gate = RecordingGate(refuse_at=2)

    with pytest.raises(OperationsError, match="PAPER_OPERATIONS_KILLED"):
        submit(
            decision=decision,
            gate=gate,
            account=runtime.account_binding_fingerprint,
            run_id="opsrun-exec",
            now=NOW,
            binding=runtime,
        )

    posts = [row for row in transport.calls if row["method"] == "POST"]
    assert len(gate.checks) == 2
    assert len(posts) == 1
    assert submit.submitted == 1


def test_a_changed_strategy_fingerprint_blocks_execution(tmp_path: Path) -> None:
    # §44. The decision stays valid; it simply may not be executed by a build
    # the runtime was not bound to.
    from dataclasses import replace

    runtime, _, _ = pending_decision(tmp_path)
    planner = execution_planner(
        tmp_path, replace(runtime, strategy_fingerprint="c" * 64), submit=_never_submit
    )

    with pytest.raises(RuntimeBindingError, match="RUNTIME_STRATEGY_MISMATCH"):
        planner(run_id="opsrun-exec", mode=None, gate=RecordingGate(), account="x", clock=None)


def test_a_changed_deployment_grant_blocks_execution(tmp_path: Path) -> None:
    # §43's shape: permission changed between decision and execution, so
    # execution stops. The decision is never re-made to acquire permission.
    from dataclasses import replace

    runtime, _, _ = pending_decision(tmp_path)
    planner = execution_planner(
        tmp_path,
        replace(runtime, strategy_authorization_fingerprint="d" * 64),
        submit=_never_submit,
    )

    with pytest.raises(RuntimeBindingError, match="RUNTIME_STRATEGY_MISMATCH"):
        planner(run_id="opsrun-exec", mode=None, gate=RecordingGate(), account="x", clock=None)
