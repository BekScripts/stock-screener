"""The concrete strategy-to-paper-operations pipeline.

Everything here runs the *real* authorities — S1.0, the risk engine, the
execution journal, accounting V2, the real coordinator and its real gates — with
only the broker transport faked. A stub `OperationsEnvironment` would prove the
coordinator's shape and nothing about the wiring, which is the mistake the first
P2.4 soak was halted for.

The assertions cluster around one semantic, because it is the one that can go
wrong silently: a decision and its execution are different market events, and
between them the selection does not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.execution import ExecutionCalendar
from stock_screener.paper_operations.strategy_schedule import (
    RunPurpose,
    StrategyOperationsSchedule,
)
from stock_screener.paper_runtime import (
    AlphaTraderPaperOperationsEnvironment,
    DecisionStatus,
    RuntimeBindingError,
    RuntimePlanner,
    StrategyDecisionStore,
    bootstrap_paper_book,
)
from stock_screener.portfolio_risk import SecurityIdentity
from stock_screener.strategies import (
    CompounderMonthlyStrategy,
    StrategyInputBundle,
    StrategyInputRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

SESSIONS: tuple[str, ...] = (
    "2026-10-28", "2026-10-29", "2026-10-30",
    "2026-11-02", "2026-11-03", "2026-11-30",
)  # fmt: skip

DECISION_SESSION = "2026-10-30"
EXECUTION_SESSION = "2026-11-02"
MID_MONTH = "2026-11-03"

#: 03:40 New York on the decision session — the instant the real operational
#: score run actually lands, verified against production rows.
SCORE_COMPUTED_AT = "2026-10-30T07:40:00+00:00"

ACCOUNT = "a894a8dad3b7b87f" * 4
GRANT = "b" * 64


@dataclass
class FakeAccountSnapshot:
    """What the venue says about a clean paper account."""

    cash: Decimal = Decimal("100000.00")
    positions: tuple[Any, ...] = ()
    buying_power: Decimal = Decimal("200000.00")
    status: str = "ACTIVE"
    trading_blocked: bool = False
    account_binding_fingerprint: str = ACCOUNT


@dataclass
class FakeBinding:
    """A proven account binding."""

    fingerprint: str = ACCOUNT

    def binding_fingerprint(self) -> str:
        """The account binding fingerprint."""
        return self.fingerprint


@dataclass
class FakeEvidence:
    """An external read's evidence envelope."""

    value: Any


@dataclass
class FakeAdapter:
    """A broker adapter that reads and can never mutate.

    It has no submit, cancel, POST or DELETE at all — not a flag that refuses
    one. A test double that *could* mutate would be the wrong shape for proving
    that an observation makes zero mutations.
    """

    account: FakeAccountSnapshot = field(default_factory=FakeAccountSnapshot)
    positions: tuple[Any, ...] = ()
    working_orders: tuple[Any, ...] = ()
    reachable: bool = True
    binding: FakeBinding = field(default_factory=FakeBinding)
    reads: list[str] = field(default_factory=list)

    def account_evidence(self) -> FakeEvidence:
        """Read the account."""
        if not self.reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        self.reads.append("account")
        return FakeEvidence(self.account)

    def positions_evidence(self) -> FakeEvidence:
        """Read the positions."""
        if not self.reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        self.reads.append("positions")
        return FakeEvidence(self.positions)

    def open_orders_evidence(self) -> FakeEvidence:
        """Read the working orders."""
        if not self.reachable:
            msg = "the paper venue could not be reached"
            raise ConnectionError(msg)
        self.reads.append("open_orders")
        return FakeEvidence(self.working_orders)


@pytest.fixture
def calendar() -> ExecutionCalendar:
    """The market calendar."""
    return ExecutionCalendar(sessions=SESSIONS, name="P2_3_1B")


@pytest.fixture
def schedule(calendar: ExecutionCalendar) -> StrategyOperationsSchedule:
    """The V1.1 schedule: decision at the close, execution near the open."""
    return StrategyOperationsSchedule(
        schedule_id="ALPHATRADER_PAPER_V1_1",
        timezone="America/New_York",
        calendar=calendar,
    )


def record(identity_id: int, score: float | None) -> StrategyInputRecord:
    """One research evidence record, admissible at the decision close."""
    return StrategyInputRecord(
        identity=SecurityIdentity(kind="COMPANY", identity_id=identity_id),
        final_score=score,
        scoring_status="SCORED" if score is not None else "INSUFFICIENT_DATA",
        freshness="CURRENT",
        score_date=DECISION_SESSION,
        score_version="COMPOUNDER_V1_2",
        calculated_at=SCORE_COMPUTED_AT,
        source_fingerprint=f"fp-{identity_id}",
    )


def bundle() -> StrategyInputBundle:
    """A 21-name cross-section, so the decile rule asks for three."""
    return StrategyInputBundle(
        as_of=DECISION_SESSION,
        score_version="COMPOUNDER_V1_2",
        universe_fingerprint="universe-1",
        records=tuple(record(i, 90.0 - i) for i in range(1, 22)),
    )


@pytest.fixture
def runtime(
    tmp_path: Path, calendar: ExecutionCalendar, schedule: StrategyOperationsSchedule
) -> Any:
    """A bootstrapped runtime over a clean fake account."""
    strategy = CompounderMonthlyStrategy()
    root = tmp_path / ".operations"
    report = bootstrap_paper_book(
        root=root,
        runtime_id="alphatrader1",
        account=FakeAccountSnapshot(),
        account_evidence_fingerprint="ev-" + "f" * 61,
        open_orders=0,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_fingerprint=strategy.fingerprint(),
        strategy_authorization_fingerprint=GRANT,
        operations_policy_fingerprint="pol",
        calendar_fingerprint=calendar.fingerprint(),
        schedule_fingerprint=schedule.fingerprint(),
        code_commit="91f7e06",
        now="2026-10-28T12:00:00+00:00",
        confirmed=True,
    )
    from stock_screener.paper_runtime import RuntimeBindingStore

    binding = RuntimeBindingStore(root).require()
    # The bootstrap derives the account fingerprint from the snapshot's own
    # binding field; the fake has none, so align it with the fake adapter.
    return {"root": root, "binding": binding, "report": report}


def environment(runtime: Any, *, adapter: FakeAdapter | None = None, score_as_of: str = "") -> Any:
    """Build the concrete environment over a bootstrapped runtime."""
    return AlphaTraderPaperOperationsEnvironment(
        root=runtime["root"],
        binding=runtime["binding"],
        adapter=adapter if adapter is not None else FakeAdapter(),
        score_as_of=score_as_of,
    )


# --------------------------------------------------------------------------
# §9, §10, §43: the schedule expresses two events in one session
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_decision_slot_is_the_month_end_close(schedule: StrategyOperationsSchedule) -> None:
    slots = schedule.slots_for_session(DECISION_SESSION)

    assert [slot.purpose for slot in slots] == [RunPurpose.STRATEGY_DECISION]
    assert slots[0].due_at().strftime("%H:%M") == "16:00"


@pytest.mark.integration
def test_the_execution_slot_is_the_next_session_near_the_open(
    schedule: StrategyOperationsSchedule,
) -> None:
    slots = schedule.slots_for_session(EXECUTION_SESSION)
    execution = next(slot for slot in slots if slot.purpose is RunPurpose.STRATEGY_EXECUTION)

    assert execution.decision_session == DECISION_SESSION
    assert execution.due_at().strftime("%H:%M") == "09:35"


@pytest.mark.integration
def test_an_execution_slot_is_never_later_in_the_day_than_a_decision_slot(
    calendar: ExecutionCalendar,
) -> None:
    # The one check that stops a decision executing in its own session.
    from stock_screener.paper_operations import OperationsError

    with pytest.raises(OperationsError, match="not before"):
        StrategyOperationsSchedule(
            schedule_id="BAD",
            timezone="America/New_York",
            calendar=calendar,
            decision_at_local="09:35",
            execution_at_local="16:00",
        )


@pytest.mark.integration
def test_two_runs_in_one_session_have_distinct_ids(
    schedule: StrategyOperationsSchedule,
) -> None:
    from stock_screener.paper_operations import OperationsMode

    slots = schedule.slots_for_session(EXECUTION_SESSION)
    ids = {
        slot.run_id(account_binding_fingerprint=ACCOUNT, mode=OperationsMode.EXTERNAL_PAPER)
        for slot in slots
    }

    assert len(slots) == 2
    assert len(ids) == 2


@pytest.mark.integration
def test_a_v1_run_id_is_unchanged_by_the_purpose_parameter() -> None:
    # The additive guarantee. A V1 caller's run ids must not move because V1.1
    # exists, or every in-flight V1 run would become a different run.
    from stock_screener.paper_operations import OperationsMode
    from stock_screener.paper_operations.schedule import operations_run_id

    def derive(purpose: str | None = None, *, pass_purpose: bool = True) -> str:
        if not pass_purpose:
            return operations_run_id(
                schedule_fingerprint="fp",
                session=EXECUTION_SESSION,
                account_binding_fingerprint=ACCOUNT,
                mode=OperationsMode.EXTERNAL_PAPER,
            )
        return operations_run_id(
            schedule_fingerprint="fp",
            session=EXECUTION_SESSION,
            account_binding_fingerprint=ACCOUNT,
            mode=OperationsMode.EXTERNAL_PAPER,
            purpose=purpose,
        )

    assert derive(pass_purpose=False) == derive(None)
    assert derive(pass_purpose=False) != derive("STRATEGY_EXECUTION")


# --------------------------------------------------------------------------
# §72, §73, §74: bootstrap
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_bootstrap_opens_a_v2_book_with_provenance(runtime: Any) -> None:
    report = runtime["report"]

    assert report.opening_cash == Decimal("100000.00")
    assert report.cash_field == "cash"
    assert report.positions_at_venue == 0
    assert report.mutating_requests == 0
    assert report.body()["accounting_engine_version"] == "PORTFOLIO_ACCOUNTING_V2"
    assert report.body()["positions_imported"] is False
    assert report.body()["historical_activity_imported"] is False


@pytest.mark.integration
def test_bootstrap_creates_all_three_lineages(runtime: Any) -> None:
    # §33: the next soak activation may not begin with "journals do not exist".
    root = runtime["root"]

    assert (root / "runtime" / "portfolio" / "generation.json").exists()
    assert (root / "runtime" / "execution" / "generation.json").exists()
    assert (root / "runtime" / "binding.json").exists()


@pytest.mark.integration
def test_a_second_bootstrap_of_the_same_root_is_refused(runtime: Any) -> None:
    strategy = CompounderMonthlyStrategy()

    with pytest.raises(RuntimeBindingError, match="BOOTSTRAP_ALREADY_DONE"):
        bootstrap_paper_book(
            root=runtime["root"],
            runtime_id="alphatrader2",
            account=FakeAccountSnapshot(),
            account_evidence_fingerprint="ev2",
            open_orders=0,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            strategy_fingerprint=strategy.fingerprint(),
            strategy_authorization_fingerprint=GRANT,
            operations_policy_fingerprint="pol",
            calendar_fingerprint="cal",
            schedule_fingerprint="sch",
            code_commit="91f7e06",
            now="2026-10-29T12:00:00+00:00",
            confirmed=True,
        )


# --------------------------------------------------------------------------
# §32, §33, §61: observe-only through the real environment
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_environment_satisfies_the_frozen_protocol(runtime: Any) -> None:
    # `OperationsEnvironment` is a plain Protocol, not runtime-checkable, so the
    # surface is checked structurally — which is what the coordinator actually
    # relies on.
    env = environment(runtime)

    for name in (
        "verify_local_integrity",
        "prove_paper_environment",
        "reconcile",
        "required_inputs",
        "accounting_state_fingerprint",
    ):
        assert callable(getattr(env, name)), name


@pytest.mark.integration
def test_the_environment_verifies_this_runtimes_own_records(runtime: Any) -> None:
    integrity = environment(runtime).verify_local_integrity()

    assert integrity["execution_journal_verified"] is True
    assert integrity["accounting_ledger_verified"] is True


@pytest.mark.integration
def test_the_environment_proves_the_bound_paper_account(runtime: Any) -> None:
    proof = environment(runtime).prove_paper_environment()

    assert proof["proven"] is True
    assert proof["account_binding_fingerprint"] == runtime["binding"].account_binding_fingerprint


@pytest.mark.integration
def test_the_environment_refuses_an_account_it_is_not_bound_to(runtime: Any) -> None:
    other = FakeAdapter(binding=FakeBinding(fingerprint="c" * 64))

    proof = environment(runtime, adapter=other).prove_paper_environment()

    assert proof["proven"] is False
    assert "not bound to" in proof["detail"]


@pytest.mark.integration
def test_a_clean_account_reconciles_for_the_runtime_epoch(runtime: Any) -> None:
    result = environment(runtime).reconcile()

    assert result["verdict"] == "RECONCILED"
    assert result["may_execute"] is True
    assert result["unexpected_remote_orders"] == 0
    assert result["unexplained_remote_positions"] == 0


@pytest.mark.integration
def test_an_unreachable_provider_raises_rather_than_reconciling(runtime: Any) -> None:
    # The coordinator's health gate distinguishes "unreachable" from "reachable
    # and wrong"; collapsing them would let an outage read as a mismatch.
    env = environment(runtime, adapter=FakeAdapter(reachable=False))

    with pytest.raises(ConnectionError):
        env.reconcile()


@pytest.mark.integration
def test_an_unknown_working_order_blocks_rather_than_being_cancelled(runtime: Any) -> None:
    @dataclass
    class ForeignOrder:
        client_order_id: str = ""
        symbol: str = "AAPL"

    env = environment(runtime, adapter=FakeAdapter(working_orders=(ForeignOrder(),)))

    result = env.reconcile()

    assert result["verdict"] == "BREAKS_BLOCKING"
    assert result["may_execute"] is False
    assert result["unexpected_remote_orders"] == 1


@pytest.mark.integration
def test_the_environment_reports_the_real_accounting_head(runtime: Any) -> None:
    fingerprint = environment(runtime).accounting_state_fingerprint()

    assert fingerprint
    assert len(fingerprint) == 64


@pytest.mark.integration
def test_generations_must_match_the_records_on_disk(runtime: Any) -> None:
    # §76. The bound lineage is checked against the filesystem, so a run cannot
    # operate on the canary's book or another runtime's journal.
    from dataclasses import replace

    env = environment(runtime)
    env.binding = replace(env.binding, accounting_generation="SOMETHING_ELSE")

    # The accounting store refuses a mismatched lineage before the comparison
    # even runs. Either way the operator gets one code.
    with pytest.raises(RuntimeBindingError, match="RUNTIME_GENERATION_MISMATCH"):
        env.require_generations()


@pytest.mark.integration
def test_a_mid_month_observation_submits_nothing_and_implies_no_exit(
    runtime: Any, calendar: ExecutionCalendar, schedule: StrategyOperationsSchedule
) -> None:
    # §33, §61, §65: the single most important semantic. Reading this as
    # "desired portfolio is empty" would liquidate the book every ordinary day.
    slot = next(
        slot
        for slot in schedule.slots_for_session(MID_MONTH)
        if slot.purpose is RunPurpose.SESSION_OBSERVATION
    )
    planner = RuntimePlanner(
        root=runtime["root"], binding=runtime["binding"], slot=slot, calendar=calendar
    )

    planned = planner(run_id="opsrun-x", mode=None, gate=None, account=ACCOUNT)

    assert planned["strategy_outcome"] == "NO_STRATEGY_DECISION_DUE"
    assert planned["orders_submitted"] == 0
    assert planned["external_mutations"] == 0
    assert planned["implies_liquidation"] is False


# --------------------------------------------------------------------------
# §34, §62, §68: the decision run
# --------------------------------------------------------------------------


def decision_planner(runtime: Any, calendar: ExecutionCalendar, schedule: Any) -> RuntimePlanner:
    """A planner for the month-end decision slot."""
    slot = next(
        slot
        for slot in schedule.slots_for_session(DECISION_SESSION)
        if slot.purpose is RunPurpose.STRATEGY_DECISION
    )
    return RuntimePlanner(
        root=runtime["root"],
        binding=runtime["binding"],
        slot=slot,
        calendar=calendar,
        bundle_for=lambda _session: bundle(),
    )


@pytest.mark.integration
def test_the_decision_run_persists_a_decision_and_submits_nothing(
    runtime: Any, calendar: ExecutionCalendar, schedule: Any
) -> None:
    planner = decision_planner(runtime, calendar, schedule)

    planned = planner(run_id="opsrun-decide", mode=None, gate=None, account=ACCOUNT, clock=None)

    assert planned["strategy_outcome"] == "DECISION"
    assert planned["orders_submitted"] == 0
    assert planned["external_mutations"] == 0
    stored = StrategyDecisionStore(runtime["root"]).require(DECISION_SESSION)
    assert stored.status is DecisionStatus.DECIDED
    assert stored.first_execution_session == EXECUTION_SESSION


@pytest.mark.integration
def test_the_decision_selects_the_top_decile(
    runtime: Any, calendar: ExecutionCalendar, schedule: Any
) -> None:
    decision_planner(runtime, calendar, schedule)(
        run_id="opsrun-decide", mode=None, gate=None, account=ACCOUNT, clock=None
    )

    stored = StrategyDecisionStore(runtime["root"]).require(DECISION_SESSION)

    assert stored.decision.eligible_count == 21
    assert stored.decision.target_count == 3
    assert [row.identity.identity_id for row in stored.decision.selections] == [1, 2, 3]


@pytest.mark.integration
def test_invoking_the_decision_slot_twice_produces_one_decision(
    runtime: Any, calendar: ExecutionCalendar, schedule: Any
) -> None:
    # §41. The decision is content-addressed, so a repeat is idempotent.
    planner = decision_planner(runtime, calendar, schedule)
    first = planner(run_id="opsrun-decide", mode=None, gate=None, account=ACCOUNT, clock=None)
    second = planner(run_id="opsrun-decide", mode=None, gate=None, account=ACCOUNT, clock=None)

    assert first["decision_fingerprint"] == second["decision_fingerprint"]
    assert len(StrategyDecisionStore(runtime["root"]).all()) == 1


@pytest.mark.integration
def test_a_decision_run_without_a_cross_section_is_refused(
    runtime: Any, calendar: ExecutionCalendar, schedule: Any
) -> None:
    # §53: no prior month's snapshot is substituted.
    slot = next(
        slot
        for slot in schedule.slots_for_session(DECISION_SESSION)
        if slot.purpose is RunPurpose.STRATEGY_DECISION
    )
    planner = RuntimePlanner(
        root=runtime["root"], binding=runtime["binding"], slot=slot, calendar=calendar
    )

    with pytest.raises(RuntimeBindingError, match="SCORE_SNAPSHOT_UNAVAILABLE"):
        planner(run_id="opsrun-decide", mode=None, gate=None, account=ACCOUNT, clock=None)


@pytest.mark.integration
def test_a_stale_cross_section_blocks_the_decision(
    runtime: Any, calendar: ExecutionCalendar, schedule: Any
) -> None:
    # A record computed after the 16:00 close is inadmissible, and S1.0 refuses
    # it rather than deciding on evidence the close did not contain.
    late = StrategyInputBundle(
        as_of=DECISION_SESSION,
        score_version="COMPOUNDER_V1_2",
        universe_fingerprint="u",
        records=(
            StrategyInputRecord(
                identity=SecurityIdentity(kind="COMPANY", identity_id=1),
                final_score=90.0,
                scoring_status="SCORED",
                freshness="CURRENT",
                score_date=DECISION_SESSION,
                score_version="COMPOUNDER_V1_2",
                calculated_at="2026-10-30T13:00:00-08:00",
                source_fingerprint="fp",
            ),
        ),
    )
    slot = next(
        slot
        for slot in schedule.slots_for_session(DECISION_SESSION)
        if slot.purpose is RunPurpose.STRATEGY_DECISION
    )
    planner = RuntimePlanner(
        root=runtime["root"],
        binding=runtime["binding"],
        slot=slot,
        calendar=calendar,
        bundle_for=lambda _session: late,
    )

    from stock_screener.strategies import StrategyError

    with pytest.raises(StrategyError, match="STRATEGY_INPUT_AFTER_DECISION_TIME"):
        planner(run_id="opsrun-decide", mode=None, gate=None, account=ACCOUNT, clock=None)

    assert StrategyDecisionStore(runtime["root"]).read(DECISION_SESSION) is None
