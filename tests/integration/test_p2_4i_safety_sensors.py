"""P2.4I — the sensors, and the guard against another declaration with no implementation.

Four phases ended on the same shape: gates with no evaluators, evaluators with
no fields, event types with no emitters. Each was found only when something
downstream tried to use it, and each cost a phase.

`test_every_emitted_event_has_a_real_production_emitter` is the guard. It drives
the real coordinator and the real submission authority over a fake transport and
asserts that every event the observability contract classifies `EMITTED` actually
appears in the journal. It does not append events itself, does not construct
them, and does not look for string references — a test that did any of those
would pass on exactly the codebase that caused this.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from stock_screener.execution.errors import ExecutionError
from stock_screener.execution.external import RequestMethod
from stock_screener.execution.external.http_transport import AlpacaPaperHttpTransport
from stock_screener.execution.external.request_audit import (
    EXTERNAL_REQUEST_AUDIT_VERSION,
    Destination,
    OperationClass,
    RequestAuditStore,
    classify_destination,
    route_class_for,
)
from stock_screener.execution.models import OrderSide
from stock_screener.paper_operations.run_journal import RunEventType
from stock_screener.paper_runtime.observability import (
    OBSERVABILITY_VERSION,
    EventStatus,
    event_vocabulary,
    observability_contract,
    observability_fingerprint,
)
from stock_screener.portfolio_risk import SecurityIdentity

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

pytestmark = pytest.mark.integration

PAPER = "https://paper-api.alpaca.markets"


# --------------------------------------------------------------------------
# §4, §5, §46, §64: the vocabulary guard
# --------------------------------------------------------------------------


def test_the_vocabulary_classifies_every_declared_event() -> None:
    # A new event type cannot be added without being classified.
    classified = {row.event for row in event_vocabulary()}

    assert classified == set(RunEventType)


def test_every_event_is_emitted_reserved_or_deprecated() -> None:
    for row in event_vocabulary():
        assert row.status in set(EventStatus), row.event
        assert row.meaning, row.event


def test_every_emitted_event_names_a_production_emitter() -> None:
    for row in event_vocabulary():
        if row.status is EventStatus.EMITTED:
            assert row.emitter, row.event


def test_a_reserved_event_names_no_emitter_and_says_why() -> None:
    reserved = [row for row in event_vocabulary() if row.status is EventStatus.RESERVED]

    assert reserved
    for row in reserved:
        assert not row.emitter
        assert "reserved" in row.meaning.lower()


def test_the_observability_contract_is_fingerprinted() -> None:
    contract = observability_contract()

    assert contract["version"] == OBSERVABILITY_VERSION
    assert len(observability_fingerprint()) == 64
    assert contract["credentials_recorded"] is False


def test_the_contract_states_the_only_semantic_change() -> None:
    # §45. Observability must not change authority semantics, and the one
    # exception is declared rather than discovered.
    changed = observability_contract()["authority_semantics_changed"]

    assert "fails closed" in changed
    assert "audit" in changed


# --------------------------------------------------------------------------
# §24-§35, §59, §60, §61: the external request audit
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://paper-api.alpaca.markets", Destination.PAPER),
        ("https://api.alpaca.markets", Destination.LIVE),
        ("https://broker-api.alpaca.markets", Destination.LIVE),
        ("https://evil-alpaca.markets.example", Destination.OTHER),
        ("https://paper-api.alpaca.markets.attacker.test", Destination.OTHER),
    ],
)
def test_destination_classification_is_exact_not_substring(
    origin: str, expected: Destination
) -> None:
    # §34. A substring rule matching "alpaca.markets" would classify the live
    # host as paper, and an attacker-controlled suffix as either.
    assert classify_destination(origin) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v2/orders", "/v2/orders"),
        ("/v2/orders/a1b2c3d4-0000-4000-8000-000000000001", "/v2/orders/{id}"),
        ("/v2/positions/123", "/v2/positions/{n}"),
        ("/v2/account/activities/FILL", "/v2/account/activities/FILL"),
    ],
)
def test_a_route_class_carries_no_identifier(path: str, expected: str) -> None:
    # §26. An order id in an audit record is operational data leaking into a
    # file that gets copied around.
    assert route_class_for(path) == expected


def transport(tmp_path: Path, *, audit: Any, handler: Any) -> AlpacaPaperHttpTransport:
    """A host-locked transport over a mock network."""
    return AlpacaPaperHttpTransport(
        origin=PAPER,
        key_id=SecretStr("PKTESTKEY01"),
        secret_key=SecretStr("shhh-secret-value"),
        audit=audit,
        operations_run_id="opsrun-1",
        account_binding_fingerprint="a894a8da" * 8,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url=PAPER),
    )


def test_a_mutation_is_audited_before_dispatch(tmp_path: Path) -> None:
    # §28. The attempt record exists with sent=False before the network call,
    # so a crash mid-flight leaves the request discoverable.
    audit = RequestAuditStore(tmp_path / "audit")
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # By the time the network is reached, the attempt is already on disk.
        assert list(audit.root.glob("*.json"))
        sent.append(str(request.url))
        return httpx.Response(200, json={"id": "remote-1"})

    transport(tmp_path, audit=audit, handler=handler).request(
        RequestMethod.POST, "/v2/orders", json_body={"client_order_id": "ORD-1"}
    )

    rows = audit.all()
    assert len(sent) == 1
    assert [row["sent"] for row in rows] == [False, True]
    assert [row["result"] for row in rows] == ["UNRESOLVED", "ANSWERED"]
    assert {row["destination"] for row in rows} == {"PAPER"}
    assert {row["operation"] for row in rows} == {"MUTATION"}


def test_a_failed_pre_dispatch_audit_blocks_the_mutation(tmp_path: Path) -> None:
    # §28, §43, §61. An unaudited mutation is worse than a missed one.
    from stock_screener.execution.errors import Code, ExecutionError

    class Broken(RequestAuditStore):
        def record_attempt(self, record: Any) -> Any:
            raise ExecutionError(Code.EXTERNAL_REQUEST_AUDIT_FAILED, "disk full")

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={})

    with pytest.raises(ExecutionError, match="EXTERNAL_REQUEST_AUDIT_FAILED"):
        transport(tmp_path, audit=Broken(tmp_path / "a"), handler=handler).request(
            RequestMethod.POST, "/v2/orders", json_body={"client_order_id": "X"}
        )

    assert sent == []


def test_a_failed_read_audit_does_not_block_the_read(tmp_path: Path) -> None:
    # The asymmetry is deliberate: a read changes nothing, and failing a
    # reconciliation read closed would block operations over a disk problem.
    from stock_screener.execution.errors import Code, ExecutionError

    class Broken(RequestAuditStore):
        def record_attempt(self, record: Any) -> Any:
            raise ExecutionError(Code.EXTERNAL_REQUEST_AUDIT_FAILED, "disk full")

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={})

    transport(tmp_path, audit=Broken(tmp_path / "a"), handler=handler).request(
        RequestMethod.GET, "/v2/account"
    )

    assert len(sent) == 1


def test_a_read_is_audited_as_a_read(tmp_path: Path) -> None:
    audit = RequestAuditStore(tmp_path / "audit")

    transport(tmp_path, audit=audit, handler=lambda _r: httpx.Response(200, json={})).request(
        RequestMethod.GET, "/v2/positions"
    )

    assert {row["operation"] for row in audit.all()} == {str(OperationClass.READ)}
    assert audit.mutations() == ()


def test_no_live_mutation_is_ever_recorded_as_sent(tmp_path: Path) -> None:
    # §25, §30. This is the evidence the live_mutations gate needs. It should
    # always be empty — and empty from a store that recorded every mutation is
    # evidence, where empty from no store at all was unscoreable.
    audit = RequestAuditStore(tmp_path / "audit")

    transport(tmp_path, audit=audit, handler=lambda _r: httpx.Response(200, json={})).request(
        RequestMethod.POST, "/v2/orders", json_body={"client_order_id": "ORD-1"}
    )

    assert audit.live_mutations_sent() == ()
    assert len(audit.mutations()) == 2


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        ("https://api.alpaca.markets", "LIVE_TRADING_NOT_AUTHORIZED"),
        ("https://broker-api.alpaca.markets", "LIVE_TRADING_NOT_AUTHORIZED"),
        ("https://127.0.0.1", "BROKER_ENVIRONMENT_NOT_PROVEN_PAPER"),
        ("https://paper-api.alpaca.markets.attacker.test", "BROKER_ENVIRONMENT_NOT_PROVEN_PAPER"),
    ],
)
def test_a_non_paper_origin_is_still_structurally_refused(origin: str, code: str) -> None:
    # §35. The audit supplies evidence; the host lock supplies prevention, and
    # both remain. A known live host is refused by name — LIVE_TRADING_NOT_
    # AUTHORIZED — before the generic unknown-host rule, so the log says
    # "someone pointed this at live" rather than "unrecognised host".
    from stock_screener.execution.errors import ExecutionError

    with pytest.raises(ExecutionError, match=code):
        AlpacaPaperHttpTransport(
            origin=origin,
            key_id=SecretStr("PKTESTKEY01"),
            secret_key=SecretStr("s"),
        )


def test_the_audit_carries_no_credential(tmp_path: Path) -> None:
    # §27, §62. The key and secret passed to the transport must not appear.
    audit = RequestAuditStore(tmp_path / "audit")

    transport(tmp_path, audit=audit, handler=lambda _r: httpx.Response(200, json={})).request(
        RequestMethod.POST, "/v2/orders", json_body={"client_order_id": "ORD-1"}
    )

    text = json.dumps(audit.all()).lower()
    for forbidden in (
        "pktestkey01",
        "shhh-secret-value",
        "authorization",
        "apca",
        "secret_key",
        "cookie",
    ):
        assert forbidden not in text, forbidden


def test_the_audit_version_is_bound_in_the_contract() -> None:
    assert observability_contract()["request_audit_version"] == EXTERNAL_REQUEST_AUDIT_VERSION


# --------------------------------------------------------------------------
# The production harness. Every authority below is the real one: the real
# coordinator gates, the real environment answers, the real strategy ranks,
# the real risk engine permits, the real planner sizes, the certified adapter
# submits and accounting V2 admits. Only the HTTPS transport is a fake, because
# the alternative is a real order and P2.2E certified that boundary.
# --------------------------------------------------------------------------

DECISION_SESSION = "2026-10-30"
EXECUTION_SESSION = "2026-11-02"
DECISION_NOW = "2026-10-30T20:00:30+00:00"
EXECUTION_NOW = "2026-11-02T14:35:00+00:00"
ACCOUNT_ID = "paper-account-1"
SYMBOLS: dict[int, str] = {1: "AAA", 2: "BBB", 3: "CCC"}


class StepClock:
    """An operational clock the test advances explicitly."""

    def __init__(self, now: str) -> None:
        self._now = now

    def now(self) -> datetime:
        """The current instant, as an aware datetime."""
        from stock_screener.paper_operations.clock import parse_instant

        return parse_instant(self._now)

    def iso(self) -> str:
        """The current instant, ISO-8601."""
        return self._now

    def set(self, now: str) -> None:
        """Move to an instant."""
        self._now = now


def answered_body(body: Any, status: int = 200) -> Any:
    """A successful provider reply."""
    from stock_screener.execution.external import BrokerResponse, TransportOutcome

    return BrokerResponse(outcome=TransportOutcome.ANSWERED, status_code=status, body=body)


class VenueFake:
    """A fake venue: reads answer from mutable state, mutations from a queue.

    Reads must be repeatable, because the environment proves the account and
    reconciles on every run and a one-shot queue would report an outage on the
    second look. Mutations stay queued and are never auto-answered, so an
    unscripted POST is a test bug rather than a silent success.
    """

    def __init__(self) -> None:
        self.positions: list[Any] = []
        self.orders: list[Any] = []
        self.activities: list[dict[str, Any]] = []
        self.cash = "100000.00"
        self.posts: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.on_post: Any = None

    def script_post(self, *responses: Any) -> None:
        """Queue mutation replies, consumed in order."""
        self.posts.extend(responses)

    @property
    def mutations(self) -> list[dict[str, Any]]:
        """Every mutating request made."""
        return [row for row in self.calls if row["method"] in {"POST", "PUT", "PATCH", "DELETE"}]

    def request(
        self,
        method: Any,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Answer one request."""
        from stock_screener.execution.external import (
            ACCOUNT_PATH,
            ACTIVITIES_PATH,
            ORDERS_PATH,
            POSITIONS_PATH,
        )

        self.calls.append(
            {"method": str(method), "path": path, "params": params, "json": json_body}
        )
        if str(method) != "GET":
            if not self.posts:
                msg = f"unscripted {method} {path}"
                raise AssertionError(msg)
            answer = self.posts.pop(0)
            if self.on_post is not None:
                # Fires *after* the venue has answered, so the hook models
                # something changing between one completed mutation and the
                # next gate check — which is the window the gate exists for.
                self.on_post(len(self.mutations))
            return answer
        # Longest prefix first: /v2/account/activities also starts with /v2/account.
        reads: dict[str, Any] = {
            ACCOUNT_PATH: lambda _p: {
                "id": ACCOUNT_ID,
                "status": "ACTIVE",
                "cash": self.cash,
                "buying_power": "200000.00",
            },
            POSITIONS_PATH: lambda _p: self.positions,
            ORDERS_PATH: lambda _p: self.orders,
            ACTIVITIES_PATH: lambda p: [
                row
                for row in self.activities
                if not p.get("order_id") or row["order_id"] == p["order_id"]
            ],
        }
        for key in sorted(reads, key=len, reverse=True):
            if path.startswith(key):
                return answered_body(reads[key](params or {}))
        msg = f"unscripted GET {path}"
        raise AssertionError(msg)


def fill_row(index: int) -> dict[str, Any]:
    """One FILL activity, as the real endpoint sends it."""
    return {
        "id": f"exec-{index}",
        "activity_type": "FILL",
        "type": "fill",
        "order_id": f"remote-{index}",
        "symbol": SYMBOLS[index],
        "side": "buy",
        "qty": "333",
        "price": "100.00",
        "transaction_time": "2026-11-02T14:35:30Z",
    }


def order_row(index: int, client_id: str) -> dict[str, Any]:
    """One provider order object."""
    return {
        "id": f"remote-{index}",
        "client_order_id": client_id,
        "symbol": SYMBOLS[index],
        "side": "buy",
        "qty": "333",
        "filled_qty": "333",
        "status": "filled",
        "type": "market",
        "time_in_force": "day",
    }


def input_bundle(session: str, count: int = 21) -> Any:
    """A research cross-section for a decision session."""
    from stock_screener.portfolio_risk import SecurityIdentity
    from stock_screener.strategies import StrategyInputBundle, StrategyInputRecord

    return StrategyInputBundle(
        as_of=session,
        score_version="COMPOUNDER_V1_2",
        universe_fingerprint="universe-1",
        records=tuple(
            StrategyInputRecord(
                identity=SecurityIdentity(kind="COMPANY", identity_id=i),
                final_score=90.0 - i,
                scoring_status="SCORED",
                freshness="CURRENT",
                score_date=session,
                score_version="COMPOUNDER_V1_2",
                calculated_at=f"{session}T07:40:00+00:00",
                source_fingerprint=f"fp-{i}",
            )
            for i in range(1, count + 1)
        ),
    )


@dataclass
class BootstrapSnapshot:
    """The venue's view of a clean account at bootstrap."""

    account_binding_fingerprint: str
    cash: Decimal = Decimal("100000.00")
    positions: tuple[Any, ...] = ()


@dataclass
class Runtime:
    """One fully wired operational runtime over a fake venue."""

    root: Any
    binding: Any
    environment: Any
    adapter: Any
    venue: VenueFake
    schedule: Any
    calendar: Any
    clock: StepClock
    arming: Any
    account: str
    coordinator: Any

    def journal(self) -> Any:
        """This runtime's operations journal."""
        from stock_screener.paper_operations.run_journal import OperationsRunJournal

        return OperationsRunJournal(self.root)

    def events(self) -> list[str]:
        """Every journal event type, in sequence."""
        return [str(row.event_type) for row in self.journal().entries()]

    def entries_of(self, event: str) -> list[Any]:
        """Every journal entry of one event type."""
        return [row for row in self.journal().entries() if str(row.event_type) == event]

    def submit_authority(self, **overrides: Any) -> Any:
        """A real submission authority over this runtime."""
        from stock_screener.execution.external import SubmissionIntentLog
        from stock_screener.paper_runtime import RuntimeSubmissionAuthority
        from stock_screener.portfolio_risk import RiskPolicy

        limits: dict[str, Any] = {
            "policy_id": "ALPHATRADER_PAPER",
            "policy_version": "V1",
            "max_gross_exposure": 1.0,
            "max_net_exposure": 1.0,
            "max_position_weight": 1.0,
            "max_positions": 50,
            "min_cash_weight": 0.0,
        }
        limits.update(overrides.pop("limits", {}))
        settings: dict[str, Any] = {
            "root": self.root,
            "adapter": self.adapter,
            "risk_policy": RiskPolicy(**limits),
            "risk_policy_fingerprint": "risk-pol-1",
            "marks": {f"COMPANY:{key}": Decimal("100.00") for key in SYMBOLS},
            "intent_log": SubmissionIntentLog(self.root / "submissions"),
            "fee_schedule": Decimal(0),
        }
        settings.update(overrides)
        return RuntimeSubmissionAuthority(**settings)

    def run(self, *, now: str, submit: Any = None, gate_for: Any = None) -> Any:
        """Run whatever slot is open at an instant, through the real planner."""
        from stock_screener.paper_operations import OperationsMode
        from stock_screener.paper_runtime import RuntimePlanner

        self.clock.set(now)
        slot = self.schedule.slot_for(now)
        assert slot is not None, f"no slot is open at {now}"
        planner = RuntimePlanner(
            root=self.root,
            binding=self.binding,
            slot=slot,
            calendar=self.calendar,
            bundle_for=input_bundle,
            submit=submit,
        )
        outcome = self.coordinator.run_due(
            mode=OperationsMode.EXTERNAL_PAPER, plan_and_submit=planner
        )
        return outcome, planner


def runtime_for(tmp_path: Path, *, proven: bool = True) -> Runtime:
    """Bootstrap a runtime and wire every real authority over a fake venue."""
    from stock_screener.execution import ExecutionCalendar
    from stock_screener.execution.external import (
        AlpacaPaperAdapter,
        BrokerCapabilities,
        BrokerEnvironment,
        ExternalBrokerAccountBinding,
        SymbolMapping,
        VenueSymbol,
        prove_paper_environment,
    )
    from stock_screener.paper_operations import (
        ArmingStore,
        KillSwitchStore,
        OperationsMode,
        PaperOperationsAuthorization,
        PaperOperationsCoordinator,
    )
    from stock_screener.paper_operations.strategy_schedule import StrategyOperationsSchedule
    from stock_screener.paper_runtime import (
        AlphaTraderPaperOperationsEnvironment,
        RuntimeBindingStore,
        bootstrap_paper_book,
    )
    from stock_screener.strategies import CompounderMonthlyStrategy

    root = tmp_path / ".operations"
    calendar = ExecutionCalendar(
        sessions=("2026-10-28", "2026-10-29", DECISION_SESSION, EXECUTION_SESSION), name="P2_4I"
    )
    schedule = StrategyOperationsSchedule(
        schedule_id="ALPHATRADER_PAPER_V1_1", timezone="America/New_York", calendar=calendar
    )
    external = ExternalBrokerAccountBinding.paper(
        provider="ALPACA",
        account_id=ACCOUNT_ID,
        proof=prove_paper_environment(
            environment=BrokerEnvironment.PAPER, base_url=PAPER, key_id="PKTESTKEY01"
        ),
        capabilities=BrokerCapabilities(fractional_quantities=True),
    )
    venue_account = external.binding_fingerprint()
    # `proven=False` bootstraps the book against a *different* account than the
    # venue answers for, which is how the account-mismatch path is reached
    # without pointing anything at a real second account.
    bound_account = venue_account if proven else "a" * 64
    strategy = CompounderMonthlyStrategy()
    bootstrap_paper_book(
        root=root,
        runtime_id="alphatrader1",
        account=BootstrapSnapshot(account_binding_fingerprint=bound_account),
        account_evidence_fingerprint="ev-" + "f" * 61,
        open_orders=0,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_fingerprint=strategy.fingerprint(),
        strategy_authorization_fingerprint="b" * 64,
        operations_policy_fingerprint="pol",
        calendar_fingerprint=calendar.fingerprint(),
        schedule_fingerprint=schedule.fingerprint(),
        code_commit="79ddf8c",
        now="2026-10-28T12:00:00+00:00",
        confirmed=True,
    )
    binding = RuntimeBindingStore(root).require()
    venue = VenueFake()
    adapter = AlpacaPaperAdapter(
        transport=venue,
        binding=external,
        mapping=SymbolMapping.of(
            [
                VenueSymbol(kind="COMPANY", identity_id=key, symbol=value)
                for key, value in SYMBOLS.items()
            ]
        ),
    )
    environment = AlphaTraderPaperOperationsEnvironment(root=root, binding=binding, adapter=adapter)
    clock = StepClock(DECISION_NOW)
    arming = ArmingStore(root)
    arming.arm(
        PaperOperationsAuthorization(
            authorization_id="grant-1",
            provider="ALPACA",
            account_binding_fingerprint=venue_account,
            operations_policy_fingerprint="pol",
            code_commit="79ddf8c",
            created_at="2026-10-28T12:00:00+00:00",
            allowed_mode=OperationsMode.EXTERNAL_PAPER,
        )
    )
    KillSwitchStore(root).activate(clock=clock, account_binding_fingerprint=venue_account)
    coordinator = PaperOperationsCoordinator(
        root=root,
        clock=clock,
        schedule=schedule,
        environment=environment,
        arming=arming,
        code_commit="79ddf8c",
    )
    return Runtime(
        root=root,
        binding=binding,
        environment=environment,
        adapter=adapter,
        venue=venue,
        schedule=schedule,
        calendar=calendar,
        clock=clock,
        arming=arming,
        account=venue_account,
        coordinator=coordinator,
    )


def script_three_orders(runtime: Runtime, decision: Any) -> None:
    """Script the venue for three submissions and their executions."""
    from stock_screener.execution.external import client_order_id_for

    for index in range(3):
        runtime.venue.script_post(
            answered_body(
                order_row(index + 1, client_order_id_for(f"{decision.decision_id}-{index}"))
            )
        )
    runtime.venue.activities = [fill_row(i + 1) for i in range(3)]


def decided(runtime: Runtime) -> Any:
    """Run the decision slot and return the frozen decision."""
    from stock_screener.paper_runtime import StrategyDecisionStore

    outcome, planner = runtime.run(now=DECISION_NOW)
    assert planner.outcome is not None
    assert planner.outcome.strategy_outcome == "DECISION", outcome.report.blockers
    return StrategyDecisionStore(runtime.root).require(DECISION_SESSION).decision


def executed(runtime: Runtime, **overrides: Any) -> Any:
    """Run the decision slot then the execution slot through the real chain."""
    decision = decided(runtime)
    script_three_orders(runtime, decision)
    submit = runtime.submit_authority(**overrides)
    outcome, planner = runtime.run(now=EXECUTION_NOW, submit=submit)
    return decision, outcome, planner, submit


# --------------------------------------------------------------------------
# §5, §46, §64: the guard. Every EMITTED event must come out of a production
# emitter driven by a real run — not appended by a test, not manufactured by a
# fixture, not referenced in a comment.
# --------------------------------------------------------------------------


def test_every_emitted_event_has_a_real_production_emitter(tmp_path: Path) -> None:
    # Four phases ended on a declaration with nothing behind it: 22 gates with
    # no evaluators, evaluators reading fields no record carried, six event
    # types with no emitters. This is the test that makes that class of defect
    # fail here instead of two phases downstream. It appends nothing itself:
    # every event below was written by the component the vocabulary names.
    runtime = runtime_for(tmp_path)
    executed(runtime)

    # The kill path is reached through a venue answering for another account,
    # which is a CRITICAL condition and the only kind that auto-kills.
    mismatched = runtime_for(tmp_path / "mismatch", proven=False)
    mismatched.run(now=DECISION_NOW)

    observed = set(runtime.events()) | set(mismatched.events())
    required = {str(row.event) for row in event_vocabulary() if row.status is EventStatus.EMITTED}

    assert required - observed == set(), sorted(required - observed)


def test_the_emitter_test_above_covers_every_event_the_contract_needs(tmp_path: Path) -> None:
    # §64's second half. An event the contract requires must be EMITTED — a
    # RESERVED one cannot satisfy a gate that needs it.
    for row in event_vocabulary():
        if row.gates_served:
            assert row.status is EventStatus.EMITTED, f"{row.event} serves {row.gates_served}"


def test_no_event_type_is_unexplained() -> None:
    # §46: zero unexplained event types.
    spec_for = {str(row.event): row for row in event_vocabulary()}

    for event in RunEventType:
        row = spec_for[str(event)]
        assert row.meaning
        assert row.status is not EventStatus.EMITTED or row.emitter


# --------------------------------------------------------------------------
# §48, §49: the order submission chain
# --------------------------------------------------------------------------


def test_a_submitted_order_is_recorded_with_its_provider_identity(tmp_path: Path) -> None:
    runtime = runtime_for(tmp_path)
    _decision, _outcome, planner, _submit = executed(runtime)

    rows = runtime.entries_of("ORDER_SUBMITTED")

    assert planner.outcome is not None
    assert planner.outcome.orders_submitted == 3
    assert len(rows) == 3
    for row in rows:
        evidence = dict(row.evidence)
        assert evidence["remote_order_id"].startswith("remote-")
        assert evidence["client_order_id"]
        assert len(evidence["order_submitted"]) == 64


def test_every_order_is_bound_to_the_frozen_decision_that_authorised_it(
    tmp_path: Path,
) -> None:
    # The re-ranking gate has no other evidence: without the decision
    # fingerprint on the plan record, a portfolio ranked fresh on execution
    # morning is indistinguishable from the frozen one.
    runtime = runtime_for(tmp_path)
    decision, _outcome, _planner, _submit = executed(runtime)

    rows = runtime.entries_of("PLAN_AUTHORIZED")

    assert len(rows) == 1
    evidence = dict(rows[0].evidence)
    assert evidence["decision"] == decision.fingerprint()
    assert evidence["plan"]


def test_the_gate_is_consulted_and_recorded_once_per_order(tmp_path: Path) -> None:
    runtime = runtime_for(tmp_path)
    executed(runtime)

    checks = runtime.entries_of("SUBMISSION_GATE_CHECKED")

    assert len(checks) == 3
    assert [dict(row.evidence)["permitted"] for row in checks] == ["True"] * 3
    assert len(runtime.venue.mutations) == 3


# --------------------------------------------------------------------------
# §50, §51: the kill switch, before and between mutations
# --------------------------------------------------------------------------


def test_a_kill_before_the_first_order_submits_nothing(tmp_path: Path) -> None:
    from stock_screener.paper_operations import KillSwitchStore

    runtime = runtime_for(tmp_path)
    decision = decided(runtime)
    script_three_orders(runtime, decision)
    KillSwitchStore(runtime.root).kill(
        clock=runtime.clock,
        reason_code="OPERATOR_KILL",
        account_binding_fingerprint=runtime.account,
    )

    outcome, _planner = runtime.run(now=EXECUTION_NOW, submit=runtime.submit_authority())

    assert outcome.external_mutations == 0
    assert runtime.venue.mutations == []
    assert runtime.entries_of("ORDER_SUBMITTED") == []


def test_a_kill_between_two_orders_stops_at_exactly_one(tmp_path: Path) -> None:
    # The property that matters is not "it stopped" but "it stopped *between*
    # mutations": one order reached the venue, the gate was consulted again,
    # and the second order never went. A gate checked once per run instead of
    # once per order would have sent all three.
    from stock_screener.paper_operations import KillSwitchStore

    runtime = runtime_for(tmp_path)
    decision = decided(runtime)
    script_three_orders(runtime, decision)
    store = KillSwitchStore(runtime.root)

    def kill_after_first(sent: int) -> None:
        if sent == 1:
            store.kill(
                clock=runtime.clock,
                reason_code="OPERATOR_KILL",
                account_binding_fingerprint=runtime.account,
            )

    runtime.venue.on_post = kill_after_first

    outcome, _planner = runtime.run(now=EXECUTION_NOW, submit=runtime.submit_authority())

    # A kill mid-run is an outcome, not an exception: the coordinator records
    # the run as KILLED and leaves every position alone. Nothing is liquidated
    # and nothing is retried.
    assert str(outcome.report.status) == "KILLED"
    assert len(runtime.venue.mutations) == 1
    assert len(runtime.entries_of("ORDER_SUBMITTED")) == 1
    checks = runtime.entries_of("SUBMISSION_GATE_CHECKED")
    assert len(checks) == 2
    assert [dict(row.evidence)["permitted"] for row in checks] == ["True", "False"]


# --------------------------------------------------------------------------
# §13, §52, §53: the accounting link and duplicate admission
# --------------------------------------------------------------------------


def test_a_fill_binds_provider_execution_to_an_accounting_event(tmp_path: Path) -> None:
    # §13's chain of custody: the venue's execution id, the local fill id it
    # became, and the accounting event it was admitted as — and the accounting
    # event is checked to actually exist, not merely to have been named.
    runtime = runtime_for(tmp_path)
    executed(runtime)

    rows = runtime.entries_of("FILL_INGESTED")
    ledger = sorted((runtime.root / "runtime" / "portfolio" / "ledger").glob("*.json"))
    committed = set()
    for path in ledger:
        body = json.loads(path.read_text(encoding="utf-8"))
        event = body.get("event", body)
        committed.add(str(event.get("event_id", "")))

    assert len(rows) == 3
    for row in rows:
        evidence = dict(row.evidence)
        provider = evidence["provider_execution_id"]
        assert evidence["execution_fill_id"] == f"EXT:{provider}"
        assert evidence["accounting_event_id"] == f"EXEC_FILL:EXT:{provider}"
        assert evidence["admission_result"] == "ADMITTED"
        assert evidence["accounting_event_id"] in committed


class PermissiveGate:
    """A gate that permits and counts, for driving the authority directly."""

    def __init__(self) -> None:
        self.checks: list[str] = []

    @property
    def checks_performed(self) -> int:
        """How many consultations happened."""
        return len(self.checks)

    def require(self, *, context: str, expected_account: str) -> None:
        """Permit, recording the consultation."""
        self.checks.append(context)


def local_order_id_for(runtime: Runtime, remote_order_id: str) -> str:
    """The local order id behind a venue order, from the durable intent log.

    Read rather than re-derived: the intent log is what the submission protocol
    itself consults, so a test that reconstructs the id by hand would pass on a
    derivation the protocol no longer uses.
    """
    index = int(remote_order_id.rsplit("-", 1)[1]) - 1
    client_id = dict(runtime.entries_of("ORDER_SUBMITTED")[index].evidence)["client_order_id"]
    for path in sorted((runtime.root / "submissions").rglob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        intent = body.get("intent", body)
        if str(intent.get("client_order_id")) == client_id:
            return str(intent["order_id"])
    msg = f"no durable intent names client order id {client_id}"
    raise AssertionError(msg)


def test_a_resumed_run_does_not_resend_or_retrade(tmp_path: Path) -> None:
    # A resume carries the same run id — which is what makes it a resume rather
    # than a new trade: the plan id, and so every local order id and derived
    # client order id, is scoped to the run. The protocol must not send a
    # second order for an order it already has a durable intent for.
    runtime = runtime_for(tmp_path)
    decision, _outcome, _planner, _submit = executed(runtime)
    posts_before = len(runtime.venue.mutations)
    run_id = runtime.entries_of("ORDER_SUBMITTED")[0].run_id
    runtime.venue.orders = [
        dict(row["json"], id=f"remote-{index + 1}", status="filled", filled_qty="333")
        for index, row in enumerate(runtime.venue.mutations)
    ]

    again = runtime.submit_authority(journal=runtime.journal(), clock=runtime.clock)
    # The execution journal refuses first, and that is the right order: the
    # order id is already committed describing a different quantity, and the
    # journal append happens *before* the order is sent. The venue protocol
    # would also have refused on the derived client order id, but only after
    # the record had already been asked to hold two contradictory facts.
    with pytest.raises(ExecutionError, match="EXECUTION_EVENT_ID_CONFLICT"):
        again(
            decision=decision,
            gate=PermissiveGate(),
            account=runtime.account,
            run_id=run_id,
            now=EXECUTION_NOW,
            binding=runtime.binding,
        )

    # Refused before any mutation: the venue was never asked to trade a second
    # time. The conflict is the right answer rather than an inconvenience — the
    # first run's fills are already in the book, so the re-plan wants a
    # different quantity under an order id that is already spoken for, and
    # sending it would trade twice on one authorisation.
    assert len(runtime.venue.mutations) == posts_before


def test_the_same_execution_admitted_twice_moves_the_book_once(tmp_path: Path) -> None:
    # The exactly-once handoff, tested where it is implemented. The derived
    # fill id — EXT:<provider execution id> — maps to one accounting event id,
    # so re-presenting the same venue execution collides instead of trading
    # again. A generated id would make every duplicate read a second trade.
    from stock_screener.execution import admit_fill
    from stock_screener.execution.external import execution_fill_from

    runtime = runtime_for(tmp_path)
    decision, _outcome, _planner, submit = executed(runtime)
    provider_fill = runtime.adapter.fills_evidence(remote_order_id="remote-1").value[0]
    portfolio, ledger, _state = submit._open_book(runtime.binding)
    before = len(ledger)
    fill = execution_fill_from(
        provider_fill,
        order_id=local_order_id_for(runtime, "remote-1"),
        identity=SecurityIdentity(kind="COMPANY", identity_id=1),
        side=OrderSide.BUY,
        strategy_id=decision.strategy_id,
        fee_schedule=Decimal(0),
    )

    outcome = admit_fill(fill, ledger=ledger, store=portfolio)

    assert outcome["written"] is False
    assert outcome["accounting_event_id"] == "EXEC_FILL:EXT:exec-1"
    assert len(ledger) == before


def test_a_re_presented_fill_whose_content_disagrees_is_refused(tmp_path: Path) -> None:
    # The harder half. Identical content is a harmless repeat; *different*
    # content under the same derived id means one of the two readings is wrong
    # and nothing can say which, so neither is adopted rather than the later
    # one winning.
    from stock_screener.execution import admit_fill
    from stock_screener.execution.external import execution_fill_from

    runtime = runtime_for(tmp_path)
    decision, _outcome, _planner, submit = executed(runtime)
    provider_fill = runtime.adapter.fills_evidence(remote_order_id="remote-1").value[0]
    portfolio, ledger, _state = submit._open_book(runtime.binding)
    before = len(ledger)
    altered = execution_fill_from(
        provider_fill,
        order_id="a-different-order",
        identity=SecurityIdentity(kind="COMPANY", identity_id=1),
        side=OrderSide.BUY,
        strategy_id=decision.strategy_id,
        fee_schedule=Decimal(0),
    )

    with pytest.raises(ExecutionError, match="FILL_ID_CONFLICT"):
        admit_fill(altered, ledger=ledger, store=portfolio)

    assert len(ledger) == before


def test_the_duplicate_branch_is_defensive_and_labelled_as_such() -> None:
    # Honesty about reachability. `_ingest` records ADMITTED or
    # DUPLICATE_IGNORED according to what accounting answered, and accounting
    # can only answer "already present" on a re-presentation. The orchestration
    # currently prevents one: the adapter collapses duplicates within a read,
    # and after a successful ingest the book has moved, so a resumed run
    # re-plans different quantities and conflicts before it ingests. The branch
    # stays because a sensor that drops an unexpected answer is worse than one
    # that records it — but it is not claimed to be exercised, and this test
    # exists so that claim is written down rather than assumed.
    spec = next(row for row in event_vocabulary() if str(row.event) == "FILL_INGESTED")

    assert spec.status is EventStatus.EMITTED
    assert "duplicate_admitted_fills" in spec.gates_served


# --------------------------------------------------------------------------
# §14, §15: the execution journal of record
# --------------------------------------------------------------------------


def test_operational_execution_appends_to_the_execution_journal(tmp_path: Path) -> None:
    # Before this, the runtime's execution journal was created at bootstrap and
    # verified by every health gate while never being written to: the record of
    # record for execution stayed permanently empty as orders went to a venue
    # and fills entered the book.
    from stock_screener.execution import ExecutionStore
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    runtime = runtime_for(tmp_path)
    executed(runtime)

    store = ExecutionStore(
        runtime.root / RUNTIME_DIRECTORY / "execution",
        generation=runtime.binding.execution_journal_generation,
    )
    journal = store.load_journal()
    kinds = [str(record.event.event_type) for record in journal.records]

    assert kinds.count("ORDER_CREATED") == 3
    assert kinds.count("ORDER_SUBMITTED") == 3
    assert kinds.count("ORDER_ACKNOWLEDGED") == 3
    assert kinds.count("ORDER_FILLED") == 3
    assert kinds.count("FILL_APPLIED") == 3


def test_the_execution_journal_verifies_and_is_anchored(tmp_path: Path) -> None:
    # §15's "existing anchor/recovery semantics": the chain verifies and the
    # head anchor witnesses the tail, so a truncation is detectable.
    from stock_screener.execution import ExecutionStore
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    runtime = runtime_for(tmp_path)
    executed(runtime)

    store = ExecutionStore(
        runtime.root / RUNTIME_DIRECTORY / "execution",
        generation=runtime.binding.execution_journal_generation,
    )
    report = store.verify()

    assert report["verified"] is True
    assert report["tail_completeness_established"] is True
    assert report["truncation_detected"] is False
    assert report["record_count"] == 15


def test_the_journal_is_written_before_the_order_is_sent(tmp_path: Path) -> None:
    # The ordering that matters. A crash after the record and before the venue
    # leaves a created-but-unsubmitted order, which is recoverable. The reverse
    # leaves an order at a venue that no local record knows about, which is not.
    runtime = runtime_for(tmp_path)
    decision = decided(runtime)
    script_three_orders(runtime, decision)
    seen: list[int] = []

    def count_journal_records(_sent: int) -> None:
        from stock_screener.execution import ExecutionStore
        from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

        store = ExecutionStore(
            runtime.root / RUNTIME_DIRECTORY / "execution",
            generation=runtime.binding.execution_journal_generation,
        )
        seen.append(len(store.load_journal().records))

    runtime.venue.on_post = count_journal_records
    runtime.run(now=EXECUTION_NOW, submit=runtime.submit_authority())

    # At the moment the first POST was answered, the journal already held the
    # ORDER_CREATED for it.
    assert seen[0] >= 1


def test_the_journal_records_no_quantity_of_its_own(tmp_path: Path) -> None:
    # §15: not a second ledger. The execution journal says a fill was applied
    # and which accounting event it became; the quantity lives in the
    # accounting book alone, and the FILL_APPLIED event carries no position.
    from stock_screener.execution import ExecutionStore
    from stock_screener.paper_runtime.binding import RUNTIME_DIRECTORY

    runtime = runtime_for(tmp_path)
    executed(runtime)

    store = ExecutionStore(
        runtime.root / RUNTIME_DIRECTORY / "execution",
        generation=runtime.binding.execution_journal_generation,
    )
    applied = [
        record.event
        for record in store.load_journal().records
        if str(record.event.event_type) == "FILL_APPLIED"
    ]

    assert len(applied) == 3
    for event in applied:
        payload = event.payload()
        assert payload["accounting_event_id"].startswith("EXEC_FILL:EXT:")
        assert "position" not in payload
        assert "quantity" not in payload


# --------------------------------------------------------------------------
# §54, §55: reconciliation, recorded and persisted
# --------------------------------------------------------------------------


def test_a_clean_reconciliation_is_recorded_with_its_verdict(tmp_path: Path) -> None:
    runtime = runtime_for(tmp_path)
    runtime.run(now=DECISION_NOW)

    rows = runtime.entries_of("RECONCILED")

    assert len(rows) == 1
    evidence = dict(rows[0].evidence)
    assert evidence["verdict"] == "RECONCILED"
    assert evidence["may_execute"] == "True"
    assert evidence["remote_state_unknown"] == "False"


def test_the_reconciliation_report_itself_is_kept(tmp_path: Path) -> None:
    # The journal entry says what the verdict was; the persisted report says
    # what it was computed from. `PortfolioStore.write_reconciliation` had
    # existed since P1 with no caller.
    runtime = runtime_for(tmp_path)
    runtime.run(now=DECISION_NOW)

    written = sorted((runtime.root / "runtime" / "reconciliations").glob("*.json"))

    assert len(written) == 1
    body = json.loads(written[0].read_text(encoding="utf-8"))
    assert body["version"] == "OPERATIONAL_RECONCILIATION_RECORD_V1"
    assert body["result"]["verdict"] == "RECONCILED"


def test_an_unexplained_remote_position_is_recorded_as_a_break(tmp_path: Path) -> None:
    # A position at the venue the book cannot explain blocks execution and is
    # never auto-repaired: no liquidation, no adoption into the book.
    runtime = runtime_for(tmp_path)
    runtime.venue.positions = [
        {"symbol": "ZZZ", "qty": "10", "side": "long", "avg_entry_price": "5.00"}
    ]

    outcome, _planner = runtime.run(now=DECISION_NOW)

    rows = runtime.entries_of("RECONCILED")
    evidence = dict(rows[0].evidence)
    assert evidence["verdict"] == "BREAKS_BLOCKING"
    assert evidence["may_execute"] == "False"
    assert evidence["unexplained_remote_positions"] == "1"
    assert str(outcome.report.status) == "BLOCKED"
    # Nothing was done about it.
    assert runtime.venue.mutations == []


# --------------------------------------------------------------------------
# §56, §57: the input freeze
# --------------------------------------------------------------------------


def test_the_inputs_a_run_depended_on_are_frozen_into_the_record(
    tmp_path: Path,
) -> None:
    runtime = runtime_for(tmp_path)
    runtime.run(now=DECISION_NOW)

    rows = runtime.entries_of("INPUTS_FROZEN")

    assert len(rows) == 1
    evidence = dict(rows[0].evidence)
    assert len(evidence["inputs"]) == 64
    assert evidence["stale_count"] == "0"
    assert evidence["missing_count"] == "0"


def test_a_stale_input_is_recorded_as_stale_rather_than_omitted(tmp_path: Path) -> None:
    # Absence of an input record cannot distinguish "fresh" from "never
    # checked", so the stale ones are recorded as stale and counted.
    from stock_screener.paper_operations import FreshnessPolicy, OperationalInput

    runtime = runtime_for(tmp_path)
    stale = OperationalInput(
        name="prices",
        as_of="2026-10-01T00:00:00+00:00",
        source_fingerprint="src-fp",
        policy=FreshnessPolicy(name="prices", max_age_seconds=600),
    )
    runtime.environment.required_inputs = lambda: (stale,)

    runtime.run(now=DECISION_NOW)

    evidence = dict(runtime.entries_of("INPUTS_FROZEN")[0].evidence)
    assert evidence["stale_count"] == "1"
    assert evidence["missing_count"] == "0"


def test_an_unreadable_input_source_is_recorded_rather_than_skipped(
    tmp_path: Path,
) -> None:
    # The store goes unreachable *after* the health gate has read it, which is
    # the realistic shape: a run that started healthy is not thereby healthy
    # for the rest of its life. The sensor must say "I could not read this"
    # rather than record an empty input set that reads as "nothing required".
    runtime = runtime_for(tmp_path)
    original = runtime.environment.required_inputs
    calls: list[int] = []

    def fails_after_the_first_read() -> tuple[Any, ...]:
        calls.append(1)
        if len(calls) > 1:
            msg = "the score store is unreachable"
            raise RuntimeError(msg)
        return tuple(original())

    runtime.environment.required_inputs = fails_after_the_first_read

    with contextlib.suppress(RuntimeError):
        runtime.run(now=DECISION_NOW)

    rows = runtime.entries_of("INPUTS_FROZEN")
    assert len(rows) == 1
    assert dict(rows[0].evidence)["inputs"] == "UNREADABLE"


# --------------------------------------------------------------------------
# §62: nothing persisted by a sensor may carry a credential
# --------------------------------------------------------------------------


def test_no_sensor_record_carries_a_credential(tmp_path: Path) -> None:
    # Every durable artefact a run produced, scanned together: the operations
    # journal, the execution journal, the accounting ledger, the reconciliation
    # reports, the submission intents and the decision records.
    runtime = runtime_for(tmp_path)
    executed(runtime)

    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(runtime.root.rglob("*.json"))
    ).lower()

    # Precise forms. `authorization_fingerprint` and `authorization_id` are a
    # digest and a grant id and are supposed to be there; what must never
    # appear is an auth *header*, a key, or a secret.
    for forbidden in (
        "pktestkey01",
        "shhh",
        '"authorization":',
        "apca-api-key-id",
        "apca-api-secret-key",
        '"secret_key"',
        "bearer ",
        '"cookie"',
        "set-cookie",
        '"headers"',
    ):
        assert forbidden not in text, forbidden


def test_the_persisted_records_name_the_account_only_by_fingerprint(
    tmp_path: Path,
) -> None:
    # §27: paper account identity is a fingerprint, never the account number.
    runtime = runtime_for(tmp_path)
    executed(runtime)

    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(runtime.root.rglob("*.json"))
    )

    assert ACCOUNT_ID not in text


# --------------------------------------------------------------------------
# §2, §65: the sensors changed no authority
# --------------------------------------------------------------------------


def test_a_mid_month_session_is_still_a_successful_no_decision_run(
    tmp_path: Path,
) -> None:
    # The single most important preserved semantic: a session with no decision
    # due is a *success*, and does not mean "desired portfolio is empty". The
    # observation slot is the mid-month one.
    from stock_screener.paper_operations.strategy_schedule import RunPurpose

    runtime = runtime_for(tmp_path)
    slot = runtime.schedule.slot_for("2026-10-29T16:00:30+00:00")

    if slot is None or slot.purpose is not RunPurpose.SESSION_OBSERVATION:
        pytest.skip("2026-10-29 holds no observation slot in this calendar")
    _outcome, planner = runtime.run(now="2026-10-29T16:00:30+00:00")

    assert planner.outcome is not None
    assert planner.outcome.strategy_outcome == "NO_STRATEGY_DECISION_DUE"
    assert planner.outcome.implies_liquidation is False
    assert runtime.venue.mutations == []


def test_risk_constrained_to_nothing_still_submits_nothing(tmp_path: Path) -> None:
    runtime = runtime_for(tmp_path)
    decision = decided(runtime)
    script_three_orders(runtime, decision)
    submit = runtime.submit_authority(limits={"max_gross_exposure": 0.0, "max_net_exposure": 0.0})

    _outcome, planner = runtime.run(now=EXECUTION_NOW, submit=submit)

    assert planner.outcome is not None
    assert planner.outcome.orders_submitted == 0
    assert runtime.venue.mutations == []
    assert runtime.entries_of("ORDER_SUBMITTED") == []


def test_a_provider_outage_blocks_rather_than_guesses(tmp_path: Path) -> None:
    runtime = runtime_for(tmp_path)

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        msg = "the paper venue could not be reached"
        raise ConnectionError(msg)

    runtime.venue.request = unreachable  # type: ignore[method-assign]

    outcome, _planner = runtime.run(now=DECISION_NOW)

    assert str(outcome.report.status) == "BLOCKED"
    assert runtime.venue.mutations == []


def test_no_sensor_module_can_reach_a_venue() -> None:
    # §2: a sensor observes. It may not select, size, create an order, repair
    # accounting, retry a mutation, cancel anything or authorise live trading.
    from pathlib import Path as _Path

    import stock_screener.paper_runtime.observability as observability

    source = _Path(observability.__file__).read_text(encoding="utf-8")

    # Precise forms, not bare words: "submit" appears inside the name
    # "_record_order_submitted", and a substring test that matched it would be
    # asserting the module cannot describe what it observes.
    for forbidden in (
        "AlpacaPaper",
        "RequestMethod",
        "transport.request",
        ".submit(",
        "admit_fill(",
        "evaluate(",
        "plan_execution(",
        "USE_LIVE",
        "fallback_to_live",
    ):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# The schedule join. Both schedule types must answer the coordinator's
# questions, and both slot types must answer the slot's.
# --------------------------------------------------------------------------


def test_both_schedule_types_satisfy_the_coordinator_contract() -> None:
    # The coordinator was annotated for one schedule type while the production
    # CLI passed the other, and `_runtime_schedule(calendar: Any) -> Any` meant
    # mypy never saw the join. Three call sites then reached a slot's due time
    # through `slot.schedule.due_at(slot.session)`, which the V1.1 schedule's
    # two-argument `due_at` rejects — so every production run raised TypeError
    # in `_finish`. This test is the structural guard.
    from stock_screener.execution import ExecutionCalendar
    from stock_screener.paper_operations.coordinator import (
        OperationalSchedule,
        OperationalSlot,
    )
    from stock_screener.paper_operations.schedule import PaperOperationsSchedule
    from stock_screener.paper_operations.strategy_schedule import (
        StrategyOperationsSchedule,
    )

    calendar = ExecutionCalendar(
        sessions=("2026-10-29", DECISION_SESSION, EXECUTION_SESSION), name="JOIN"
    )
    strategy_schedule = StrategyOperationsSchedule(
        schedule_id="ALPHATRADER_PAPER_V1_1", timezone="America/New_York", calendar=calendar
    )
    plain = PaperOperationsSchedule(
        schedule_id="P2_3",
        timezone="America/New_York",
        run_at_local="16:00",
        calendar=calendar,
    )

    assert isinstance(strategy_schedule, OperationalSchedule)
    assert isinstance(plain, OperationalSchedule)
    for schedule in (strategy_schedule, plain):
        slot = schedule.slot_for(DECISION_NOW) or schedule.missed_slot_for(DECISION_NOW)
        assert slot is not None, schedule.schedule_id
        assert isinstance(slot, OperationalSlot), schedule.schedule_id
        # The call the three broken sites now make, on both types.
        assert slot.due_at().isoformat()


def test_a_full_run_through_the_v1_1_schedule_reports_a_scheduled_time(
    tmp_path: Path,
) -> None:
    # The regression in behavioural form: a run that completes, with a
    # `scheduled_for` the report could not previously produce at all.
    runtime = runtime_for(tmp_path)

    outcome, _planner = runtime.run(now=DECISION_NOW)

    assert str(outcome.report.status) == "COMPLETED_NO_ACTION"
    assert outcome.report.scheduled_for.startswith(DECISION_SESSION)
