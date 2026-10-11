"""Writing real operational records for a hypothetical session.

The preflight has to answer "could this protocol ever reach A, and would it
reach FAIL if something went wrong" *before* a soak is armed. That needs
evidence, and there is exactly one honest way to produce it: write the records a
real session would have written, then run the real derivations over them.

The dishonest way is what this replaces. The old reachability fixtures set
`live_requests=1` on a synthetic observation and watched the scorer say FAIL.
That proved the comparison operator worked. It proved nothing about whether
anything could ever *observe* a live request, which is precisely the question,
and fourteen gates were unobservable while those fixtures passed.

So everything here writes through the production stores — the real journal with
its real hash chain, the real request audit, the real reconciliation and
decision records — and the derivations then read them without knowing a test
wrote them. A breach is created by writing the records a breach would leave
behind, never by setting the number the breach would have produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SYNTHETIC_RECORD_BUILDER_VERSION",
    "Breach",
    "SoakFixture",
    "SyntheticSession",
    "build_soak_fixture",
    "write_session_records",
]

#: The fixture builder's contract. Bound into preflight output so a reader can
#: tell which builder produced a reachability claim.
SYNTHETIC_RECORD_BUILDER_VERSION = "SOAK_SYNTHETIC_SOURCE_RECORDS_V1"


class Breach:
    """The underlying conditions a reachability fixture can create.

    Each names a *condition in the records*, not a metric. `TWO_REMOTE_ORDERS`
    writes two provider order ids under one derived client order id; it does not
    set `duplicate_remote_orders` to 1. Whether that condition is detected is
    the thing under test.
    """

    TWO_REMOTE_ORDERS = "TWO_REMOTE_ORDERS"
    DOUBLE_ADMITTED_FILL = "DOUBLE_ADMITTED_FILL"
    TWO_ACCOUNTING_EVENTS = "TWO_ACCOUNTING_EVENTS"
    TWO_EXECUTING_RUNS = "TWO_EXECUTING_RUNS"
    STALE_INPUT_EXECUTION = "STALE_INPUT_EXECUTION"
    MISSING_INPUT_EXECUTION = "MISSING_INPUT_EXECUTION"
    BACKFILLED_MISSED_DECISION = "BACKFILLED_MISSED_DECISION"
    EXECUTED_AFTER_BREAK = "EXECUTED_AFTER_BREAK"
    EXECUTED_WITH_REMOTE_UNKNOWN = "EXECUTED_WITH_REMOTE_UNKNOWN"
    COMPLETED_WITHOUT_RECONCILIATION = "COMPLETED_WITHOUT_RECONCILIATION"
    MUTATION_WITHOUT_GATE_CHECK = "MUTATION_WITHOUT_GATE_CHECK"
    CANCELLED_UNKNOWN_ORDER = "CANCELLED_UNKNOWN_ORDER"
    SECOND_OPENING_BALANCE = "SECOND_OPENING_BALANCE"
    RERANKED_EXECUTION = "RERANKED_EXECUTION"
    RISK_EXPANSION = "RISK_EXPANSION"
    LIVE_MUTATION = "LIVE_MUTATION"


@dataclass
class _Clock:
    """A clock the builder advances, so journal entries are ordered."""

    moment: str

    def iso(self) -> str:
        """The current instant."""
        return self.moment

    def now(self) -> Any:
        """The current instant as an aware datetime."""
        from stock_screener.paper_operations.clock import parse_instant

        return parse_instant(self.moment)


@dataclass
class SyntheticSession:
    """What one synthetic session should contain.

    Attributes:
        session: The market session date.
        run_id: The operational run that traded, derived-looking but arbitrary.
        orders: How many orders the session submitted.
        decision_session: The session whose decision this one executed.
        decision_fingerprint: That decision's fingerprint.
        decided: Whether to write the decision record itself.
        breach: One underlying condition to create, or None for a clean session.
        at: The instant the records are stamped with, ISO-8601. Separate from
            the session label on purpose: a session identity is an opaque key
            to these records — a protocol may legitimately label its sessions
            `S000` — while a timestamp has to be a real instant. Deriving one
            from the other produced `S000T14:35:00+00:00`, which the execution
            journal rightly refused.
    """

    session: str
    run_id: str = ""
    orders: int = 0
    decision_session: str = ""
    decision_fingerprint: str = ""
    decided: bool = False
    breach: str | None = None
    at: str = ""
    _clock: _Clock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Derive the run id and open the clock."""
        if not self.run_id:
            self.run_id = f"opsrun-{self.session.replace('-', '')}"
        if not self.at:
            self.at = f"{self.session}T14:35:00+00:00"
        self._clock = _Clock(self.at)


def write_session_records(root: Path, plan: SyntheticSession) -> tuple[str, ...]:
    """Write the records a session like this would have left behind.

    Args:
        root: The operations root to write into.
        plan: What the session contained.

    Returns:
        The run ids written, for the caller to pass to the derivation.
    """
    from stock_screener.paper_operations.run_journal import OperationsRunJournal, RunEventType

    journal = OperationsRunJournal(root)
    # The builder owns the plan object it was handed.
    clock = plan._clock
    runs = [plan.run_id]

    _write_inputs(journal, clock, plan)
    _write_reconciliation(root, journal, clock, plan)
    if plan.decided or plan.decision_fingerprint:
        _write_decision(root, plan)
    if plan.orders:
        _write_plan_authorized(journal, clock, plan)
        runs += _write_orders(root, journal, clock, plan)
    if plan.breach == Breach.CANCELLED_UNKNOWN_ORDER:
        _write_cancel(root, plan)
    if plan.breach == Breach.RISK_EXPANSION:
        _write_transformation(root, plan, expand=True)
    elif plan.decision_fingerprint:
        _write_transformation(root, plan, expand=False)
    _write_opening_balance(root, plan)
    if plan.breach != Breach.COMPLETED_WITHOUT_RECONCILIATION:
        journal.append(
            clock=clock, event_type=RunEventType.RUN_COMPLETED, run_id=plan.run_id, detail="done"
        )
    else:
        # Completed with orders and no reconciliation evidence at all.
        journal.append(
            clock=clock, event_type=RunEventType.RUN_COMPLETED, run_id=plan.run_id, detail="done"
        )
    return tuple(dict.fromkeys(runs))


def _write_inputs(journal: Any, clock: _Clock, plan: SyntheticSession) -> None:
    """Record the input freeze, stale or missing where the breach asks."""
    from stock_screener.execution import fingerprint_of
    from stock_screener.paper_operations.run_journal import RunEventType

    stale = 1 if plan.breach == Breach.STALE_INPUT_EXECUTION else 0
    absent = 1 if plan.breach == Breach.MISSING_INPUT_EXECUTION else 0
    body = {
        "evaluated_at": clock.iso(),
        "sources": [{"name": "prices", "fresh": not stale, "present": not absent}],
    }
    journal.append(
        clock=clock,
        event_type=RunEventType.INPUTS_FROZEN,
        run_id=plan.run_id,
        detail=f"{1 - stale - absent} fresh source(s)",
        evidence=(
            ("inputs", fingerprint_of(body)),
            ("stale_count", str(stale)),
            ("missing_count", str(absent)),
        ),
    )


def _write_reconciliation(root: Path, journal: Any, clock: _Clock, plan: SyntheticSession) -> None:
    """Record the reconciliation verdict, and persist the report beside it."""
    from stock_screener.execution import fingerprint_of
    from stock_screener.paper_operations.persistence import atomic_write_verified
    from stock_screener.paper_operations.run_journal import RunEventType

    if plan.breach == Breach.COMPLETED_WITHOUT_RECONCILIATION:
        # The source exists — an earlier run reconciled — but *this* run has no
        # record. That is an observed breach, not missing evidence, and the
        # distinction is the whole point: a reader can tell "nobody kept
        # reconciliations" from "this session's is absent".
        atomic_write_verified(
            root / "runtime" / "reconciliations" / f"{plan.run_id}-earlier.json",
            {
                "version": "OPERATIONAL_RECONCILIATION_RECORD_V1",
                "run_id": f"{plan.run_id}-earlier",
                "evaluated_at": clock.iso(),
                "result": {"verdict": "RECONCILED", "may_execute": True},
            },
        )
        return
    broke = plan.breach == Breach.EXECUTED_AFTER_BREAK
    unknown = plan.breach == Breach.EXECUTED_WITH_REMOTE_UNKNOWN
    body = {
        "verdict": "BREAKS_BLOCKING" if broke else "RECONCILED",
        "may_execute": not broke,
        "remote_state_unknown": unknown,
        "unexpected_remote_orders": 0,
        "unexplained_remote_positions": 1 if broke else 0,
    }
    journal.append(
        clock=clock,
        event_type=RunEventType.RECONCILED,
        run_id=plan.run_id,
        detail=str(body["verdict"]),
        evidence=(
            ("reconciliation", fingerprint_of(body)),
            ("verdict", str(body["verdict"])),
            ("may_execute", str(body["may_execute"])),
            ("remote_state_unknown", str(body["remote_state_unknown"])),
            ("unexpected_remote_orders", "0"),
            ("unexplained_remote_positions", str(body["unexplained_remote_positions"])),
        ),
    )
    atomic_write_verified(
        root / "runtime" / "reconciliations" / f"{plan.run_id}.json",
        {
            "version": "OPERATIONAL_RECONCILIATION_RECORD_V1",
            "run_id": plan.run_id,
            "evaluated_at": clock.iso(),
            "result": body,
        },
    )


def _write_decision(root: Path, plan: SyntheticSession) -> None:
    """Write the persisted decision record for the session being executed."""
    from stock_screener.paper_operations.persistence import atomic_write_verified

    missed = plan.breach == Breach.BACKFILLED_MISSED_DECISION
    status = "EXECUTION_MISSED" if missed else ("EXECUTION_COMPLETED" if plan.orders else "DECIDED")
    session = plan.decision_session or plan.session
    atomic_write_verified(
        root / "runtime" / "decisions" / f"{session}.json",
        {
            "version": "PERSISTED_STRATEGY_DECISION_V1",
            "decision_fingerprint": plan.decision_fingerprint,
            "status": status,
            "runtime_id": "alphatrader1",
            "decision_run_id": plan.run_id,
            "execution_run_id": plan.run_id if plan.orders else "",
            "recorded_at": plan.at,
            "history": [],
        },
    )


def _write_plan_authorized(journal: Any, clock: _Clock, plan: SyntheticSession) -> None:
    """Record the plan, naming the decision it consumed."""
    from stock_screener.execution import fingerprint_of
    from stock_screener.paper_operations.run_journal import RunEventType

    consumed = "f" * 64 if plan.breach == Breach.RERANKED_EXECUTION else plan.decision_fingerprint
    journal.append(
        clock=clock,
        event_type=RunEventType.PLAN_AUTHORIZED,
        run_id=plan.run_id,
        detail=f"{plan.orders} order(s) authorised",
        evidence=(
            ("plan_authorized", fingerprint_of({"orders": plan.orders})),
            ("decision", consumed),
            ("plan", fingerprint_of({"plan": plan.run_id})),
        ),
    )


def _write_orders(root: Path, journal: Any, clock: _Clock, plan: SyntheticSession) -> list[str]:
    """Record the gate checks, the orders, the fills and the audit."""
    from stock_screener.execution import fingerprint_of
    from stock_screener.paper_operations.persistence import atomic_write_verified
    from stock_screener.paper_operations.run_journal import RunEventType

    extra_runs: list[str] = []
    for index in range(plan.orders):
        client = f"ssx1-{plan.session}-{index}"
        run_id = plan.run_id
        if plan.breach == Breach.TWO_EXECUTING_RUNS and index == plan.orders - 1:
            # A second, different run id submitting for the same session.
            run_id = f"{plan.run_id}-second"
            extra_runs.append(run_id)
        remotes = [f"remote-{plan.session}-{index}"]
        if plan.breach == Breach.TWO_REMOTE_ORDERS and index == 0:
            remotes.append(f"remote-{plan.session}-{index}-again")
        for position, remote in enumerate(remotes):
            # One permitting check per mutation, which is what the authority
            # does. A duplicate remote order is then a duplicate-order breach
            # and not also a kill bypass, so each fixture isolates one gate.
            if plan.breach != Breach.MUTATION_WITHOUT_GATE_CHECK:
                journal.append(
                    clock=clock,
                    event_type=RunEventType.SUBMISSION_GATE_CHECKED,
                    run_id=run_id,
                    detail="permitted",
                    evidence=(
                        ("gate_check", fingerprint_of({"order": index, "n": position})),
                        ("permitted", "True"),
                    ),
                )
            journal.append(
                clock=clock,
                event_type=RunEventType.ORDER_SUBMITTED,
                run_id=run_id,
                detail=f"ANSWERED remote={remote}",
                evidence=(
                    ("order_submitted", fingerprint_of({"remote": remote})),
                    ("remote_order_id", remote),
                    ("client_order_id", client),
                ),
            )
        atomic_write_verified(
            root / "submissions" / "submission_intents" / f"{client}.json",
            {"intent": {"client_order_id": client, "order_id": f"order-{index}"}},
        )
        _write_fills(root, journal, clock, plan, run_id=run_id, index=index)
        _write_execution_journal(root, plan, index=index)
    _write_audit(root, plan)
    return extra_runs


def _write_execution_journal(root: Path, plan: SyntheticSession, *, index: int) -> None:
    """Commit the certified execution lifecycle for one order.

    Through `ExecutionStore.commit`, so the chain and head anchor are the real
    ones: a derivation that refuses an unverified journal must be given a
    journal that genuinely verifies, or the fixture proves nothing.
    """
    from decimal import Decimal

    from stock_screener.execution import ExecutionStore, Order
    from stock_screener.execution.events import (
        ExecutionEventType,
        ExecutionFill,
        FillAppliedEvent,
        OrderAcknowledgedEvent,
        OrderCancelledEvent,
        OrderCreatedEvent,
        OrderFilledEvent,
        OrderSubmittedEvent,
    )
    from stock_screener.execution.models import AttributionShare, OrderSide
    from stock_screener.portfolio_risk import SecurityIdentity

    store = ExecutionStore(root / "runtime" / "execution", generation="synthetic-gen-1")
    journal = store.load_journal()
    moment = plan.at
    order_id = f"order-{plan.session}-{index}"
    plan_id = f"{plan.run_id}-plan"
    quantity = Decimal(10)
    order = Order(
        order_id=order_id,
        execution_plan_id=plan_id,
        authorization_fingerprint="a" * 64,
        identity=SecurityIdentity(kind="COMPANY", identity_id=index + 1),
        side=OrderSide.BUY,
        quantity=quantity,
        sizing_price=Decimal(100),
        attribution=(AttributionShare(strategy_id="ALPHATRADER", quantity=quantity),),
        created_at=moment,
        symbol="AAA",
    )
    store.commit(
        journal,
        OrderCreatedEvent(
            event_id=f"CREATED:{order_id}",
            event_type=ExecutionEventType.ORDER_CREATED,
            order_id=order_id,
            execution_plan_id=plan_id,
            occurred_at=moment,
            recorded_at=moment,
            order=order,
        ),
    )
    store.commit(
        journal,
        OrderSubmittedEvent(
            event_id=f"SUBMITTED:{order_id}",
            event_type=ExecutionEventType.ORDER_SUBMITTED,
            order_id=order_id,
            execution_plan_id=plan_id,
            occurred_at=moment,
            recorded_at=moment,
            venue="EXTERNAL_PAPER",
        ),
    )
    store.commit(
        journal,
        OrderAcknowledgedEvent(
            event_id=f"ACKNOWLEDGED:{order_id}",
            event_type=ExecutionEventType.ORDER_ACKNOWLEDGED,
            order_id=order_id,
            execution_plan_id=plan_id,
            occurred_at=moment,
            recorded_at=moment,
            venue="EXTERNAL_PAPER",
        ),
    )
    execution = f"exec-{plan.session}-{index}"
    fill = ExecutionFill(
        fill_id=f"EXT:{execution}",
        order_id=order_id,
        identity=order.identity,
        side=OrderSide.BUY,
        quantity=quantity,
        price=Decimal(100),
        effective_at=moment,
        attribution=(AttributionShare(strategy_id="ALPHATRADER", quantity=quantity),),
        source_fingerprint="f" * 64,
        venue="EXTERNAL_PAPER",
    )
    store.commit(
        journal,
        OrderFilledEvent(
            event_id=f"FILLED:{fill.fill_id}",
            event_type=ExecutionEventType.ORDER_FILLED,
            order_id=order_id,
            execution_plan_id=plan_id,
            occurred_at=moment,
            recorded_at=moment,
            fill=fill,
        ),
    )
    applications = [f"APPLIED:{fill.fill_id}"]
    for event_id in applications:
        store.commit(
            journal,
            FillAppliedEvent(
                event_id=event_id,
                event_type=ExecutionEventType.FILL_APPLIED,
                order_id=order_id,
                execution_plan_id=plan_id,
                occurred_at=moment,
                recorded_at=moment,
                fill_id=fill.fill_id,
                accounting_event_id=f"EXEC_FILL:{fill.fill_id}",
                accounting_sequence=index + 2,
                accounting_state_fingerprint="s" * 64,
            ),
        )
    if plan.breach == Breach.CANCELLED_UNKNOWN_ORDER and index == 0:
        # A *separate*, unfilled order: the state machine refuses cancelling a
        # filled one, and rightly — a terminal order is settled history. The
        # breach under test is cancelling something this runtime does not own,
        # so the cancelled order is created and acknowledged but never filled.
        foreign = f"foreign-{plan.session}"
        foreign_order = Order(
            order_id=foreign,
            execution_plan_id=plan_id,
            authorization_fingerprint="a" * 64,
            identity=SecurityIdentity(kind="COMPANY", identity_id=99),
            side=OrderSide.BUY,
            quantity=quantity,
            sizing_price=Decimal(100),
            attribution=(AttributionShare(strategy_id="ALPHATRADER", quantity=quantity),),
            created_at=moment,
            symbol="ZZZ",
        )
        store.commit(
            journal,
            OrderCreatedEvent(
                event_id=f"CREATED:{foreign}",
                event_type=ExecutionEventType.ORDER_CREATED,
                order_id=foreign,
                execution_plan_id=plan_id,
                occurred_at=moment,
                recorded_at=moment,
                order=foreign_order,
            ),
        )
        store.commit(
            journal,
            OrderSubmittedEvent(
                event_id=f"SUBMITTED:{foreign}",
                event_type=ExecutionEventType.ORDER_SUBMITTED,
                order_id=foreign,
                execution_plan_id=plan_id,
                occurred_at=moment,
                recorded_at=moment,
                venue="EXTERNAL_PAPER",
            ),
        )
        store.commit(
            journal,
            OrderCancelledEvent(
                event_id=f"CANCELLED:{foreign}",
                event_type=ExecutionEventType.ORDER_CANCELLED,
                order_id=foreign,
                execution_plan_id=plan_id,
                occurred_at=moment,
                recorded_at=moment,
            ),
        )


def _write_fills(
    root: Path, journal: Any, clock: _Clock, plan: SyntheticSession, *, run_id: str, index: int
) -> None:
    """Record the fill ingestion and the accounting event it became."""
    from stock_screener.execution import fingerprint_of
    from stock_screener.paper_operations.persistence import atomic_write_verified
    from stock_screener.paper_operations.run_journal import RunEventType

    execution = f"exec-{plan.session}-{index}"
    fill = f"EXT:{execution}"
    accounting = f"EXEC_FILL:{fill}"
    admissions = ["ADMITTED"]
    if plan.breach == Breach.DOUBLE_ADMITTED_FILL and index == 0:
        admissions.append("ADMITTED")
    for admission in admissions:
        journal.append(
            clock=clock,
            event_type=RunEventType.FILL_INGESTED,
            run_id=run_id,
            detail=f"{admission} execution={execution}",
            evidence=(
                ("fill_ingested", fingerprint_of({"execution": execution, "n": admission})),
                ("provider_execution_id", execution),
                ("execution_fill_id", fill),
                ("admission_result", admission),
                ("accounting_event_id", accounting),
            ),
        )
    if plan.breach == Breach.TWO_ACCOUNTING_EVENTS and index == 0:
        # One provider execution recorded as two accounting applications.
        journal.append(
            clock=clock,
            event_type=RunEventType.FILL_INGESTED,
            run_id=run_id,
            detail=f"ADMITTED execution={execution}",
            evidence=(
                ("fill_ingested", fingerprint_of({"execution": execution, "n": "second"})),
                ("provider_execution_id", execution),
                ("execution_fill_id", fill),
                ("admission_result", "ADMITTED"),
                ("accounting_event_id", f"{accounting}-second"),
            ),
        )
    sequence = index + 2
    atomic_write_verified(
        root / "runtime" / "portfolio" / "ledger" / f"{sequence:012d}.json",
        {"sequence": sequence, "event": {"event_id": accounting, "event_type": "FILL"}},
    )


def _write_audit(root: Path, plan: SyntheticSession) -> None:
    """Write the sanitised request audit for the session's mutations."""
    from stock_screener.paper_operations.persistence import atomic_write_verified

    for index in range(plan.orders):
        attempt = f"req-{plan.session}-{index}"
        atomic_write_verified(
            root / "request_audit" / f"{attempt}.json",
            {
                "version": "EXTERNAL_REQUEST_AUDIT_V1",
                "attempt_id": attempt,
                "destination": "PAPER",
                "operation": "MUTATION",
                "method": "POST",
                "route_class": "/v2/orders",
                "sent": True,
                "result": "ANSWERED",
                "client_order_id": f"ssx1-{plan.session}-{index}",
            },
        )
    if plan.breach == Breach.LIVE_MUTATION:
        atomic_write_verified(
            root / "request_audit" / f"req-{plan.session}-live.json",
            {
                "version": "EXTERNAL_REQUEST_AUDIT_V1",
                "attempt_id": f"req-{plan.session}-live",
                "destination": "LIVE",
                "operation": "MUTATION",
                "method": "POST",
                "route_class": "/v2/orders",
                "sent": True,
                "result": "ANSWERED",
            },
        )


def _write_cancel(root: Path, plan: SyntheticSession) -> None:
    """Audit a DELETE for an order this runtime has no intent for."""
    from stock_screener.paper_operations.persistence import atomic_write_verified

    atomic_write_verified(
        root / "request_audit" / f"req-{plan.session}-cancel.json",
        {
            "version": "EXTERNAL_REQUEST_AUDIT_V1",
            "attempt_id": f"req-{plan.session}-cancel",
            "destination": "PAPER",
            "operation": "MUTATION",
            "method": "DELETE",
            "route_class": "/v2/orders/{id}",
            "sent": True,
            "result": "ANSWERED",
            "client_order_id": "somebody-elses-order",
        },
    )


def _write_transformation(root: Path, plan: SyntheticSession, *, expand: bool) -> None:
    """Record the desired-to-approved transformation for the session."""
    from stock_screener.paper_operations.persistence import atomic_write_verified

    approved = ["COMPANY:1", "COMPANY:9"] if expand else ["COMPANY:1"]
    atomic_write_verified(
        root / "runtime" / "transformations" / f"{plan.decision_session or plan.session}.json",
        {
            "decision_session": plan.decision_session or plan.session,
            "decision_fingerprint": plan.decision_fingerprint,
            "desired_identities": ["COMPANY:1"],
            "approved_identities": approved,
            "outcome": "PROCEED",
            "rows": [
                {
                    "identity": "COMPANY:1",
                    "desired_weight": 0.5,
                    "approved_weight": 0.9 if expand else 0.4,
                }
            ],
        },
    )


def _write_opening_balance(root: Path, plan: SyntheticSession) -> None:
    """Open the book, twice where the breach asks for a repair."""
    from stock_screener.paper_operations.persistence import atomic_write_verified

    atomic_write_verified(
        root / "runtime" / "portfolio" / "ledger" / "000000000001.json",
        {"sequence": 1, "event": {"event_id": "opening", "event_type": "OPENING_BALANCE"}},
    )
    if plan.breach == Breach.SECOND_OPENING_BALANCE:
        atomic_write_verified(
            root / "runtime" / "portfolio" / "ledger" / "000000000999.json",
            {
                "sequence": 999,
                "event": {"event_id": "opening-again", "event_type": "OPENING_BALANCE"},
            },
        )


@dataclass
class SoakFixture:
    """A whole soak's worth of real records, and the evidence derived from it.

    Attributes:
        root: The operations root the records were written to.
        store: The soak store holding the protocol, activation and observations.
        evidence: The derived evidence a scorer reads.
        sessions: The sessions written.
        protocol: The protocol the records were written under, so a caller can
            score the fixture against the gates it was actually built for.
    """

    root: Path
    store: Any
    evidence: Any
    sessions: tuple[str, ...]
    protocol: Any = None


def build_soak_fixture(
    root: Path,
    *,
    protocol: Any,
    activation: Any,
    sessions: int,
    breach: str | None = None,
    breach_session: int = -1,
    omit_drills: bool = False,
    finalize: bool = True,
    remove_source: str | None = None,
) -> SoakFixture:
    """Write a soak's records, finalise each session, and derive its evidence.

    This is the real path end to end: records written through the production
    stores, session evidence derived from them by the evidence contract's own
    derivations, V3 observations recorded in the soak store, and a
    `DerivedSoakEvidence` over the result. Nothing sets a gate value.

    Args:
        root: The operations root.
        protocol: The frozen protocol.
        activation: Its activation.
        sessions: How many eligible sessions to write.
        breach: One underlying condition to create, or None for a clean soak.
        breach_session: Which session index carries it. Negative counts back.
        omit_drills: Write no drill evidence, to exercise a promotion gate.
        finalize: Record the observations. False leaves the records unfinalised.
        remove_source: Delete this source schema's records after writing them,
            to prove a gate reports missing evidence rather than a clean zero.
            The records are written first and then removed, so the session
            genuinely happened and only the evidence of it is gone — which is
            the situation the distinction exists for.

    Returns:
        The fixture.
    """
    from .aggregate import DerivedSoakEvidence
    from .drills_v2 import DrillStoreV2
    from .observations import DerivedSessionObservation
    from .session_evidence import derive_session_evidence
    from .sources import OperationalRecords
    from .store import VersionedSoakStore

    store = VersionedSoakStore(root, namespace="r3b")
    store.freeze_protocol(protocol)
    store.activate(activation)

    # The activation decides which sessions are eligible, so the fixture writes
    # those rather than inventing its own: an observation for a session the
    # activation never declared is correctly refused as retrospective, and a
    # fixture that trips that refusal is testing its own dates.
    declared = tuple(getattr(activation, "window", ()) or ())
    dates = (
        declared[:sessions]
        if len(declared) >= sessions
        else tuple(f"2026-11-{index + 1:02d}" for index in range(sessions))
    )
    target = dates[breach_session] if breach is not None and dates else ""
    written: list[str] = []
    for index, session in enumerate(dates):
        session_root = root / "sessions" / session
        plan = SyntheticSession(
            session=session,
            orders=2,
            decision_session=session,
            decision_fingerprint=f"decision-{session}-" + "f" * 40,
            decided=True,
            breach=breach if session == target else None,
            at=_instant_for(index),
        )
        runs = write_session_records(session_root, plan)
        if remove_source is not None:
            _remove_source(session_root, remove_source)
        session_evidence = derive_session_evidence(
            OperationalRecords(session_root),
            session=session,
            run_ids=runs,
            protocol_fingerprint=protocol.fingerprint(),
            qualifying_commit=protocol.qualifying_commit,
            finalized_at=_instant_for(index, hour=21),
        )
        if finalize:
            store.record_observation(
                DerivedSessionObservation(
                    protocol_fingerprint=protocol.fingerprint(),
                    session=session,
                    qualifying_commit=protocol.qualifying_commit,
                    observed_at=_instant_for(index, hour=21),
                    run_ids=runs,
                    gate_evidence=session_evidence,
                )
            )
        written.append(session)
        if index == 0:
            # One soak-wide root carries the decision lifecycle and the drills,
            # so the soak-scoped derivations have somewhere to read from.
            _mirror_decisions(session_root, root)

    if not omit_drills:
        drills = DrillStoreV2(root / "soak" / "drills")
        for record in _drill_set(protocol):
            drills.record(record)

    soak_evidence = DerivedSoakEvidence(
        records=OperationalRecords(root, soak_root=root / "soak"),
        store=store,
        protocol_fingerprint=protocol.fingerprint(),
        qualifying_commit=protocol.qualifying_commit,
        eligible_sessions=tuple(written),
        required_drills=tuple(str(row) for row in protocol.required_drill_types),
        required_sessions=protocol.required_sessions,
        operations_policy_fingerprint=protocol.operations_policy_fingerprint,
    )
    return SoakFixture(
        root=root,
        store=store,
        evidence=soak_evidence,
        sessions=tuple(written),
        protocol=protocol,
    )


#: Where each source's records live, for the removal that proves a gate fails
#: closed rather than reporting zero.
_SOURCE_PATHS: dict[str, tuple[str, ...]] = {
    "OPERATIONS_RUN_JOURNAL": ("run_journal",),
    "EXECUTION_JOURNAL": ("runtime", "execution"),
    "ACCOUNTING_LEDGER": ("runtime", "portfolio", "ledger"),
    "RECONCILIATION_RECORDS": ("runtime", "reconciliations"),
    "STRATEGY_DECISIONS": ("runtime", "decisions"),
    "RISK_TRANSFORMATIONS": ("runtime", "transformations"),
    "SUBMISSION_INTENTS": ("submissions", "submission_intents"),
    "EXTERNAL_REQUEST_AUDIT": ("request_audit",),
}


def _remove_source(root: Path, schema_id: str) -> None:
    """Delete one source's records from a session root.

    Raises:
        KeyError: For a schema this builder does not know where to find. Better
            than silently removing nothing and reporting a clean session.
    """
    import shutil

    target = root
    for part in _SOURCE_PATHS[schema_id]:
        target = target / part
    if target.exists():
        shutil.rmtree(target)


def _instant_for(index: int, *, hour: int = 14) -> str:
    """A real, increasing instant for the nth synthetic session.

    Derived from a fixed base date rather than from the session label, because
    a session label need not be a date.
    """
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 11, 1, hour, 35, tzinfo=UTC) + timedelta(days=index)
    return base.isoformat()


def _mirror_decisions(session_root: Path, root: Path) -> None:
    """Copy one session's decision records to the soak root.

    The soak-scoped derivations read the decision lifecycle over the whole
    soak, and each synthetic session writes into its own root so the
    session-scoped derivations stay isolated. This makes the decision history
    visible at soak scope without a session being able to see another's runs.
    """
    import shutil

    source = session_root / "runtime" / "decisions"
    if not source.exists():
        return
    target = root / "runtime" / "decisions"
    target.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.json"):
        shutil.copy2(path, target / path.name)
    journal = session_root / "run_journal"
    if journal.exists():
        mirror = root / "run_journal"
        mirror.mkdir(parents=True, exist_ok=True)
        for path in journal.glob("*.json"):
            shutil.copy2(path, mirror / path.name)
    anchor = session_root / "run_journal_anchor.json"
    if anchor.exists():
        shutil.copy2(anchor, root / "run_journal_anchor.json")


def _drill_set(protocol: Any) -> tuple[Any, ...]:
    """A passing, fully bound drill record for each required type."""
    from .drills_v2 import DrillRecordV2

    return tuple(
        DrillRecordV2(
            drill_type=drill_type,
            protocol_fingerprint=protocol.fingerprint(),
            qualifying_commit=protocol.qualifying_commit,
            operations_engine_version=_engine_version(),
            operations_policy_fingerprint=protocol.operations_policy_fingerprint,
            performed_at="2026-10-28T12:00:00+00:00",
            pre_state_fingerprint="before",
            action="injected fault",
            expected="refused",
            observed="refused",
            post_state_fingerprint="after",
            passed=True,
        )
        for drill_type in protocol.required_drill_types
    )


def _engine_version() -> str:
    """The control-plane version drill evidence is bound to."""
    from stock_screener.paper_operations import PAPER_OPERATIONS_ENGINE_VERSION

    return str(PAPER_OPERATIONS_ENGINE_VERSION)
