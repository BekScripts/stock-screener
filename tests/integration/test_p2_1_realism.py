"""P2.1 — deterministic execution realism and multi-session paper fills.

The claims under test are mostly about cost and about time. Cost: a buy pays the
ask and more, a sell receives the bid and less, and the decomposition into spread
and slippage sums exactly to the shortfall. Time: a DAY order lives for one
session and no longer, residual quantity reaches later sessions as new children
with deterministic ids, and the horizon is finite.

Several tests exist to pin down what the engine refuses rather than what it does
— a crossed quote, an unknown volume, a negative slippage, a snapshot from the
wrong session. Each of those has a permissive alternative that would silently
remove a cost or invent a trade.

Two things are asserted as *not* guaranteed: V1.1 models no market impact,
latency or queue position, and a journal rolled back together with its anchor
still validates. Neither is claimed away.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import stock_screener.portfolio_accounting as accounting
from stock_screener.execution import (
    DAY_EXPIRY_POLICY,
    EXECUTION_ENGINE_VERSION,
    EXECUTION_ENGINE_VERSION_V1_1,
    EXECUTION_MANIFEST_VERSION_V1_1,
    PAPER_SESSION_FILL_MODEL,
    SESSION_ROLLOVER_POLICY,
    TERMINAL_STATES,
    V1_1_ADDED_ERROR_CODES,
    V1_ERROR_CODES,
    V1_STATES,
    BookBinding,
    Code,
    ExecutionCalendar,
    ExecutionError,
    ExecutionJournal,
    ExecutionSizingSnapshot,
    ExecutionStore,
    OrderSide,
    OrderState,
    PaperExecutionPolicy,
    PaperMarketSessionSnapshot,
    PlanStatus,
    SessionExecutionSession,
    SessionPaperBroker,
    SessionQuote,
    SizingPrice,
    build_child_order,
    build_cost_report,
    child_order_id,
    cost_of_fill,
    execution_manifest,
    fill_price_for,
    load_realism_request,
    order_state_fingerprint,
    plan_execution,
    realism_manifest,
    run_session_execution,
    touch_price_for,
    transition_table,
)
from stock_screener.portfolio_risk import Action, ExecutionIntent, SecurityIdentity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stock_screener.execution import ExecutionPlan

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "execution"
SOURCE = Path(__file__).resolve().parents[2] / "src" / "stock_screener" / "execution"

X = SecurityIdentity(kind="COMPANY", identity_id=1, symbol="X")
Y = SecurityIdentity(kind="COMPANY", identity_id=2, symbol="Y")
V2 = accounting.ACCOUNTING_V2

#: Friday, Monday, Tuesday, Thursday — a weekend and a Wednesday holiday.
SESSIONS = ("2026-10-02", "2026-10-05", "2026-10-06", "2026-10-08")


def at(session: str, hour: int = 16, minute: int = 0) -> str:
    """An aware instant in a session."""
    return f"{session}T{hour:02d}:{minute:02d}:00-04:00"


def policy(
    *,
    bps: str = "0",
    participation: str = "0.10",
    sessions: int = 3,
    spread_required: bool = True,
    volume_required: bool = True,
) -> PaperExecutionPolicy:
    """A friction policy. Every value declared, none calibrated."""
    return PaperExecutionPolicy(
        policy_id="TEST_POLICY",
        adverse_slippage_bps=Decimal(bps),
        max_volume_participation=Decimal(participation),
        maximum_execution_sessions=sessions,
        spread_required=spread_required,
        volume_required=volume_required,
    )


def calendar(sessions: Sequence[str] = SESSIONS) -> ExecutionCalendar:
    """A calendar over the given sessions."""
    return ExecutionCalendar.of(sessions, name="TEST_CALENDAR")


def quote(
    *,
    identity: SecurityIdentity = X,
    bid: str | None = "99",
    ask: str | None = "101",
    volume: str | None = "100000",
) -> SessionQuote:
    """One session quote."""
    return SessionQuote(
        identity=identity,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        observed_share_volume=None if volume is None else Decimal(volume),
    )


def market(session: str, *quotes: SessionQuote) -> PaperMarketSessionSnapshot:
    """A market snapshot for one session."""
    return PaperMarketSessionSnapshot(
        session=session, as_of=at(session), quotes=quotes or (quote(),), source="TEST"
    )


def open_book(
    tmp_path: Path,
    *,
    cash: str = "500000",
    positions: Sequence[accounting.OpeningPosition] = (),
) -> tuple[accounting.PortfolioStore, accounting.PortfolioLedger]:
    """A V2 accounting book."""
    store = accounting.PortfolioStore(tmp_path / ".portfolio", accounting_version=V2)
    ledger = accounting.PortfolioLedger()
    store.commit(
        ledger,
        accounting.OpeningBalanceEvent(
            event_id="OB",
            event_type=accounting.EventType.OPENING_BALANCE,
            effective_at=at(SESSIONS[0], 9, 30),
            source_id="IMPORT",
            source_fingerprint="fp-ob",
            cash=Decimal(cash),
            positions=tuple(positions),
        ),
    )
    return store, ledger


def make_plan(
    store: accounting.PortfolioStore,
    ledger: accounting.PortfolioLedger,
    *,
    notional: float = 100000.0,
    action: Action = Action.ENTER,
    price: str = "100",
    identity: SecurityIdentity = X,
    attribution: dict[str, Sequence[tuple[str, Decimal]]] | None = None,
    strategy: str | None = "ALPHA",
) -> ExecutionPlan:
    """An authorised plan for one security."""
    state = accounting.replay(ledger, accounting_version=V2)
    return plan_execution(
        execution_plan_id="PLAN",
        as_of=at(SESSIONS[0]),
        intents=[
            ExecutionIntent(
                identity=identity,
                action=action,
                current_weight=0.0 if action is Action.ENTER else 0.4,
                target_weight=0.2 if action is Action.ENTER else 0.0,
                notional_delta=notional,
                strategy_id=strategy,
            )
        ],
        sizing=ExecutionSizingSnapshot(
            as_of=at(SESSIONS[0]),
            nav=Decimal("500000"),
            cash=Decimal("500000"),
            prices=(SizingPrice(identity=identity, price=Decimal(price)),),
        ),
        book=BookBinding(
            ledger_generation=store.generation,
            accounting_engine_version=V2,
            accounting_state_fingerprint=accounting.state_fingerprint(state),
            ledger_sequence=len(ledger),
            ledger_head=ledger.head_hash,
        ),
        approved_portfolio_fingerprint="ap" * 32,
        valued_snapshot_fingerprint="vs" * 32,
        attribution=attribution,
        created_at=at(SESSIONS[0]),
    )


def session_for(
    tmp_path: Path,
    plan: ExecutionPlan,
    store: accounting.PortfolioStore,
    ledger: accounting.PortfolioLedger,
    *,
    friction: PaperExecutionPolicy | None = None,
    sessions: Sequence[str] = SESSIONS,
) -> SessionExecutionSession:
    """A V1.1 session over a fresh execution store."""
    return SessionExecutionSession(
        plan=plan,
        policy=friction or policy(),
        calendar=calendar(sessions),
        store=ExecutionStore(tmp_path / ".execution", execution_plan_id=plan.execution_plan_id),
        ledger=ledger,
        accounting_store=store,
        journal=ExecutionJournal(),
    )


def book_state(ledger: accounting.PortfolioLedger) -> accounting.PortfolioState:
    """The book as it stands."""
    return accounting.replay(ledger, accounting_version=V2)


# --------------------------------------------------------------------------- #
# Versioning: P2.0 is untouched
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_two_engine_versions_are_distinct() -> None:
    assert EXECUTION_ENGINE_VERSION == "EXECUTION_ENGINE_V1"
    assert EXECUTION_ENGINE_VERSION_V1_1 == "EXECUTION_ENGINE_V1_1"


@pytest.mark.integration
def test_the_two_fill_models_are_distinct() -> None:
    from stock_screener.execution import PAPER_FILL_MODEL

    assert PAPER_FILL_MODEL == "PAPER_REFERENCE_PRICE_FILL_V1"
    assert PAPER_SESSION_FILL_MODEL == "PAPER_SESSION_PARTICIPATION_FILL_V1"


@pytest.mark.integration
def test_the_v1_manifest_describes_the_v1_lifecycle_without_expiry() -> None:
    lifecycle = execution_manifest()["order_lifecycle"]

    assert "EXPIRED" not in lifecycle["states"]
    assert len(lifecycle["states"]) == len(V1_STATES)


@pytest.mark.integration
def test_the_v1_manifest_lists_only_the_v1_codes() -> None:
    assert execution_manifest()["error_codes"] == sorted(V1_ERROR_CODES)
    assert not V1_ERROR_CODES & V1_1_ADDED_ERROR_CODES


@pytest.mark.integration
def test_the_v1_1_codes_are_absent_from_the_v1_vocabulary() -> None:
    for code in ("PAPER_QUOTE_REQUIRED", "ORDER_ALREADY_EXPIRED", "INVALID_PAPER_QUOTE"):
        assert code in V1_1_ADDED_ERROR_CODES
        assert code not in V1_ERROR_CODES


@pytest.mark.integration
@pytest.mark.parametrize(
    "name", ["clean_execution", "partial_fill_execution", "rejected_execution"]
)
def test_the_p2_0_goldens_still_reproduce(name: str, tmp_path: Path) -> None:
    from stock_screener.execution import load_request, run_paper_execution

    request = load_request(FIXTURES / f"{name}.json")
    golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))

    assert run_paper_execution(request, root=tmp_path) == golden


# --------------------------------------------------------------------------- #
# The friction policy
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_negative_slippage_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_EXECUTION_POLICY):
        policy(bps="-1")


@pytest.mark.integration
def test_the_refusal_explains_why_improvement_is_not_modelled() -> None:
    with pytest.raises(ExecutionError, match="manufacture edge"):
        policy(bps="-1")


@pytest.mark.integration
@pytest.mark.parametrize("participation", ["0", "-0.1", "1.5"])
def test_a_participation_outside_zero_to_one_is_refused(participation: str) -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_EXECUTION_POLICY):
        policy(participation=participation)


@pytest.mark.integration
def test_a_participation_of_exactly_one_is_permitted() -> None:
    assert policy(participation="1").max_volume_participation == Decimal("1")


@pytest.mark.integration
def test_a_non_positive_session_horizon_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_EXECUTION_POLICY):
        policy(sessions=0)


@pytest.mark.integration
def test_the_policy_is_never_production_authorized() -> None:
    from stock_screener.execution import EXAMPLE_REALISM_POLICY

    assert EXAMPLE_REALISM_POLICY.production_authorized is False
    assert EXAMPLE_REALISM_POLICY.as_json()["authorization"] == "NOT_PRODUCTION_AUTHORIZED"


@pytest.mark.integration
def test_the_policy_states_it_is_not_calibrated() -> None:
    body = policy().as_json()

    assert "NONE" in body["calibration"]
    assert body["slippage_direction"] == "ADVERSE_ONLY"
    assert "NOT_MODELLED" in body["market_impact"]


@pytest.mark.integration
def test_the_policy_fingerprint_moves_with_its_friction() -> None:
    assert policy(bps="0").fingerprint() != policy(bps="10").fingerprint()
    assert policy(participation="0.1").fingerprint() != policy(participation="0.2").fingerprint()


# --------------------------------------------------------------------------- #
# Quotes, spread and slippage
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_midpoint_is_the_cost_reference() -> None:
    assert quote().mid == Decimal("100.00000000")
    assert quote().spread == Decimal("2.00000000")


@pytest.mark.integration
def test_a_buy_crosses_to_the_ask_and_a_sell_to_the_bid() -> None:
    row = quote()
    assert row.bid is not None
    assert row.ask is not None

    assert touch_price_for(OrderSide.BUY, bid=row.bid, ask=row.ask) == row.ask
    assert touch_price_for(OrderSide.SELL, bid=row.bid, ask=row.ask) == row.bid


@pytest.mark.integration
def test_with_no_slippage_a_buy_fills_at_the_ask() -> None:
    row = quote()
    assert row.bid is not None and row.ask is not None

    assert (
        fill_price_for(OrderSide.BUY, bid=row.bid, ask=row.ask, policy=policy(bps="0")) == row.ask
    )


@pytest.mark.integration
def test_with_no_slippage_a_sell_fills_at_the_bid() -> None:
    row = quote()
    assert row.bid is not None and row.ask is not None

    assert (
        fill_price_for(OrderSide.SELL, bid=row.bid, ask=row.ask, policy=policy(bps="0")) == row.bid
    )


@pytest.mark.integration
def test_slippage_worsens_a_buy() -> None:
    row = quote()
    assert row.bid is not None and row.ask is not None

    price = fill_price_for(OrderSide.BUY, bid=row.bid, ask=row.ask, policy=policy(bps="10"))

    assert price == Decimal("101.10100000")
    assert price > row.ask


@pytest.mark.integration
def test_slippage_worsens_a_sell() -> None:
    row = quote()
    assert row.bid is not None and row.ask is not None

    price = fill_price_for(OrderSide.SELL, bid=row.bid, ask=row.ask, policy=policy(bps="10"))

    assert price == Decimal("98.90100000")
    assert price < row.bid


@pytest.mark.integration
def test_a_zero_spread_quote_is_valid() -> None:
    row = SessionQuote(identity=X, bid=Decimal("100"), ask=Decimal("100"))

    assert row.spread == Decimal("0")
    assert row.mid == Decimal("100.00000000")


@pytest.mark.integration
def test_a_crossed_quote_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_PAPER_QUOTE):
        SessionQuote(identity=X, bid=Decimal("101"), ask=Decimal("99"))


@pytest.mark.integration
def test_the_crossed_quote_refusal_explains_the_sign() -> None:
    with pytest.raises(ExecutionError, match="wrong sign"):
        SessionQuote(identity=X, bid=Decimal("101"), ask=Decimal("99"))


@pytest.mark.integration
@pytest.mark.parametrize("side", ["bid", "ask"])
def test_a_missing_required_quote_side_is_refused(side: str) -> None:
    row = quote(**{side: None})  # type: ignore[arg-type]

    with pytest.raises(ExecutionError, match=Code.PAPER_QUOTE_REQUIRED):
        row.require_quote(policy=policy())


@pytest.mark.integration
def test_the_missing_quote_refusal_names_no_fallback() -> None:
    with pytest.raises(ExecutionError, match="No sizing price, close or last"):
        quote(ask=None).require_quote(policy=policy())


@pytest.mark.integration
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_quote_side_is_refused(value: str) -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_PAPER_QUOTE):
        SessionQuote(identity=X, bid=Decimal(value), ask=Decimal("101"))


# --------------------------------------------------------------------------- #
# Participation
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_capacity_is_volume_times_participation() -> None:
    row = quote(volume="5000")

    assert row.capacity(policy=policy(participation="0.10")) == Decimal("500.00000000")


@pytest.mark.integration
def test_an_unknown_volume_is_refused_rather_than_treated_as_unlimited() -> None:
    with pytest.raises(ExecutionError, match=Code.PAPER_VOLUME_REQUIRED):
        quote(volume=None).require_volume(policy=policy())


@pytest.mark.integration
def test_the_unknown_volume_refusal_distinguishes_unknown_from_unlimited() -> None:
    with pytest.raises(ExecutionError, match="not an unlimited one"):
        quote(volume=None).require_volume(policy=policy())


@pytest.mark.integration
def test_a_zero_volume_yields_zero_capacity_rather_than_a_refusal() -> None:
    assert quote(volume="0").capacity(policy=policy()) == Decimal("0")


@pytest.mark.integration
def test_a_negative_volume_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_PAPER_QUOTE):
        SessionQuote(
            identity=X,
            bid=Decimal("99"),
            ask=Decimal("101"),
            observed_share_volume=Decimal("-1"),
        )


@pytest.mark.integration
def test_the_participation_ceiling_caps_a_single_fill(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )

    assert plan.orders[0].quantity == Decimal("1000.00000000")
    assert outcomes[0].capacity_ceiling == Decimal("500.00000000")
    assert outcomes[0].filled == Decimal("500.00000000")


@pytest.mark.integration
def test_the_ceiling_is_shared_across_orders_in_one_session(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    snapshots = {SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))}
    session.execute_identity(plan.orders[0], snapshots=snapshots, first_session=SESSIONS[0])

    # A second child in the same session finds the ceiling already used up.
    capacity = session.broker.capacity(
        build_child_order(
            plan=plan,
            parent=plan.orders[0],
            session=SESSIONS[0],
            generation=9,
            residual=Decimal("100"),
            sizing_price=Decimal("100"),
            created_at=at(SESSIONS[0]),
        ),
        snapshot=snapshots[SESSIONS[0]],
        session=SESSIONS[0],
        order_sessions=session._order_sessions,
    )

    assert capacity.consumed == Decimal("500.00000000")
    assert capacity.remaining == Decimal("0")
    assert capacity.is_exhausted


@pytest.mark.integration
def test_replaying_a_fill_does_not_consume_capacity_twice(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    snapshots = {SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))}
    session.execute_identity(plan.orders[0], snapshots=snapshots, first_session=SESSIONS[0])
    derived = session.journal.derive()
    fill = next(iter(derived.fills.values()))
    from stock_screener.execution import ExecutionEventType, OrderFilledEvent

    # The same fill, re-presented. Its id is the key capacity is derived from, so
    # it contributes once.
    session.journal.append(
        OrderFilledEvent(
            event_id=f"{fill.fill_id}:REPLAY",
            event_type=ExecutionEventType.ORDER_FILLED,
            order_id=fill.order_id,
            execution_plan_id=plan.execution_plan_id,
            occurred_at=fill.effective_at,
            fill=fill,
        )
    )

    from stock_screener.execution import capacity_for

    capacity = capacity_for(
        quote(volume="5000"),
        session=SESSIONS[0],
        policy=policy(participation="0.10"),
        derived=session.journal.derive(),
        order_sessions=session._order_sessions,
    )

    assert capacity.consumed == Decimal("500.00000000")


@pytest.mark.integration
def test_the_capacity_reports_its_consumption_source() -> None:
    from stock_screener.execution import SessionCapacity

    body = SessionCapacity(
        identity=X,
        session=SESSIONS[0],
        observed_share_volume=Decimal("1000"),
        ceiling=Decimal("100"),
        consumed=Decimal("10"),
    ).as_json()

    assert body["scope"] == "GLOBAL_PER_IDENTITY_AND_SESSION"
    assert "keyed by fill id" in body["consumption_source"]
    assert body["volume_unit"] == "SHARES"


# --------------------------------------------------------------------------- #
# DAY expiry
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_expired_is_terminal() -> None:
    from stock_screener.execution import VALID_TRANSITIONS

    assert OrderState.EXPIRED in TERMINAL_STATES
    assert VALID_TRANSITIONS[OrderState.EXPIRED] == frozenset()


@pytest.mark.integration
def test_the_v1_1_table_permits_expiry_from_working_states() -> None:
    table = transition_table()

    assert "EXPIRED" in table["transitions"]["ACKNOWLEDGED"]
    assert "EXPIRED" in table["transitions"]["PARTIALLY_FILLED"]
    assert table["transitions"]["EXPIRED"] == []


@pytest.mark.integration
def test_expiry_is_distinguished_from_cancellation() -> None:
    table = transition_table()

    assert "a cancellation is a decision" in table["expiry"]
    assert {"CANCELLED", "EXPIRED"} <= set(table["terminal"])


@pytest.mark.integration
def test_a_partially_filled_child_expires_at_its_session_close(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )
    status = session.journal.derive().status(outcomes[0].order_id)

    assert outcomes[0].expired is True
    assert status.state is OrderState.EXPIRED
    assert status.cumulative_filled_quantity == Decimal("500.00000000")
    assert status.remaining_quantity == Decimal("500.00000000")


@pytest.mark.integration
def test_a_fully_filled_child_does_not_expire(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="1", sessions=1)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="1000000"))},
        first_session=SESSIONS[0],
    )

    assert outcomes[0].expired is False
    assert session.journal.derive().status(outcomes[0].order_id).state is OrderState.FILLED


@pytest.mark.integration
def test_an_expired_child_cannot_fill_again(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    snapshots = {SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))}
    outcomes = session.execute_identity(
        plan.orders[0], snapshots=snapshots, first_session=SESSIONS[0]
    )
    child = session.journal.derive().status(outcomes[0].order_id).order

    with pytest.raises(ExecutionError, match=Code.ORDER_ALREADY_EXPIRED):
        session.broker.fill(
            plan,
            child,
            snapshot=snapshots[SESSIONS[0]],
            session=SESSIONS[0],
            order_sessions=session._order_sessions,
        )


@pytest.mark.integration
def test_no_day_order_spans_two_sessions(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="4000")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="4000")),
        },
        first_session=SESSIONS[0],
    )

    # Every child belongs to exactly one session, and no order id repeats across
    # them: a surviving order would show as the same id in two outcomes.
    assert len({row.order_id for row in outcomes}) == len(outcomes)
    assert len({row.session for row in outcomes}) == len(outcomes)


@pytest.mark.integration
def test_the_expiry_policy_is_named() -> None:
    assert DAY_EXPIRY_POLICY == "DAY_ORDER_SESSION_EXPIRY_V1"
    assert SESSION_ROLLOVER_POLICY == "CHILD_DAY_ORDER_ROLLOVER_V1"


# --------------------------------------------------------------------------- #
# Multi-session rollover
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_residual_rolls_through_three_sessions(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )

    assert [row.filled for row in outcomes] == [
        Decimal("400.00000000"),
        Decimal("350.00000000"),
        Decimal("250.00000000"),
    ]
    assert outcomes[-1].residual_after == Decimal("0")
    assert session.settle() is PlanStatus.COMPLETED
    assert book_state(ledger).position(X).quantity == Decimal("1000.00000000")


@pytest.mark.integration
def test_the_residual_comes_from_admitted_fills_not_submitted_quantity(
    tmp_path: Path,
) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
        },
        first_session=SESSIONS[0],
    )

    # The first child was submitted for the whole 1000 and filled 400, so the
    # second is authorised for 600 — not for 0, which submitted quantity implies.
    assert outcomes[0].authorized == Decimal("1000.00000000")
    assert outcomes[1].authorized == Decimal("600.00000000")


@pytest.mark.integration
def test_the_session_horizon_is_finite(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=2)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )

    assert len(outcomes) == 2
    assert outcomes[-1].residual_after == Decimal("250.00000000")
    assert session.settle() is PlanStatus.PARTIALLY_COMPLETED
    assert book_state(ledger).position(X).quantity == Decimal("750.00000000")


@pytest.mark.integration
def test_the_next_session_comes_from_the_calendar_not_arithmetic() -> None:
    cal = calendar()

    # Friday 2 October rolls to Monday 5th, not Saturday 3rd.
    assert cal.next_session("2026-10-02") == "2026-10-05"
    # Tuesday 6th rolls to Thursday 8th, skipping the Wednesday holiday.
    assert cal.next_session("2026-10-06") == "2026-10-08"


@pytest.mark.integration
def test_a_weekend_is_skipped_in_a_real_rollover(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=2)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="4000")),
        },
        first_session=SESSIONS[0],
    )

    assert [row.session for row in outcomes] == ["2026-10-02", "2026-10-05"]


@pytest.mark.integration
def test_a_session_outside_the_calendar_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.UNKNOWN_EXECUTION_SESSION):
        calendar().next_session("2026-10-03")


@pytest.mark.integration
def test_the_end_of_the_calendar_stops_rollover() -> None:
    assert calendar().next_session(SESSIONS[-1]) is None


# --------------------------------------------------------------------------- #
# Child order identity
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_child_id_is_deterministic() -> None:
    first = child_order_id(
        plan_fingerprint="p" * 64,
        identity_label="COMPANY:1",
        session=SESSIONS[0],
        generation=1,
        authorized_residual=Decimal("1000"),
    )
    second = child_order_id(
        plan_fingerprint="p" * 64,
        identity_label="COMPANY:1",
        session=SESSIONS[0],
        generation=1,
        authorized_residual=Decimal("1000"),
    )

    assert first == second


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        {"session": "2026-10-05"},
        {"generation": 2},
        {"authorized_residual": Decimal("999")},
        {"identity_label": "COMPANY:2"},
        {"plan_fingerprint": "q" * 64},
    ],
)
def test_a_child_id_changes_with_every_semantic_input(change: dict[str, Any]) -> None:
    base = {
        "plan_fingerprint": "p" * 64,
        "identity_label": "COMPANY:1",
        "session": SESSIONS[0],
        "generation": 1,
        "authorized_residual": Decimal("1000"),
    }

    assert child_order_id(**base) != child_order_id(**{**base, **change})  # type: ignore[arg-type]


@pytest.mark.integration
def test_a_child_order_carries_no_random_identity(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)

    first = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=Decimal("1000"),
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )
    second = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=Decimal("1000"),
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )

    assert first.order_id == second.order_id
    assert first.fingerprint() == second.fingerprint()


@pytest.mark.integration
def test_a_child_for_no_residual_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)

    with pytest.raises(ExecutionError, match=Code.ORDER_INVALID):
        build_child_order(
            plan=plan,
            parent=plan.orders[0],
            session=SESSIONS[0],
            generation=1,
            residual=Decimal("0"),
            sizing_price=Decimal("100"),
            created_at=at(SESSIONS[0]),
        )


@pytest.mark.integration
def test_a_child_exceeding_the_plans_authorisation_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    oversized = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=Decimal("5000"),
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_AUTHORIZATION_MISMATCH):
        plan.require_authorizes_child(oversized, session=SESSIONS[0], generation=1)


@pytest.mark.integration
def test_a_fabricated_child_id_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    child = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=Decimal("1000"),
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )
    forged = dataclasses.replace(child, order_id="COMPANY:1@2026-10-02#G1:deadbeefdeadbeef")

    with pytest.raises(ExecutionError, match=Code.EXECUTION_AUTHORIZATION_MISMATCH):
        plan.require_authorizes_child(forged, session=SESSIONS[0], generation=1)


@pytest.mark.integration
def test_a_child_for_an_unplanned_security_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)

    with pytest.raises(ExecutionError, match=Code.UNAUTHORIZED_ORDER):
        plan.parent_for(Y.key)


# --------------------------------------------------------------------------- #
# Session binding
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_snapshot_from_another_session_cannot_fill(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(tmp_path, plan, store, ledger)
    child = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=Decimal("1000"),
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )
    session.store.commit(
        session.journal,
        session.broker.create(plan, child, session=SESSIONS[0], at=at(SESSIONS[0])),
    )

    with pytest.raises(ExecutionError, match=Code.PAPER_MARKET_SESSION_MISMATCH):
        session.broker.fill(
            plan,
            child,
            snapshot=market(SESSIONS[1]),
            session=SESSIONS[0],
            order_sessions={child.order_id: SESSIONS[0]},
        )


@pytest.mark.integration
def test_a_snapshot_binds_one_timing_semantic() -> None:
    body = market(SESSIONS[0]).body()

    assert "completed session" in body["timing"]
    assert "No fill is backdated" in body["timing"]


@pytest.mark.integration
def test_a_naive_snapshot_instant_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_INVALID):
        PaperMarketSessionSnapshot(session=SESSIONS[0], as_of="2026-10-02T16:00:00")


@pytest.mark.integration
def test_a_security_quoted_twice_in_one_session_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_PAPER_QUOTE):
        PaperMarketSessionSnapshot(
            session=SESSIONS[0], as_of=at(SESSIONS[0]), quotes=(quote(), quote())
        )


# --------------------------------------------------------------------------- #
# Cost decomposition
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_buy_cost_decomposes_exactly() -> None:
    cost = cost_of_fill(
        identity=X,
        session=SESSIONS[0],
        side=OrderSide.BUY,
        bid=Decimal("99"),
        ask=Decimal("101"),
        filled_quantity=Decimal("100"),
        fill_price=Decimal("101.101"),
        fee=Decimal("1"),
        fill_id="F1",
        observed_share_volume=Decimal("5000"),
        participation_ceiling=Decimal("500"),
    )

    assert cost.mid == Decimal("100.00000000")
    assert cost.spread_cost == Decimal("100.00000000")
    assert cost.slippage_cost == Decimal("10.10000000")
    assert cost.implementation_shortfall == Decimal("110.10000000")
    assert cost.spread_cost + cost.slippage_cost == cost.implementation_shortfall
    assert cost.total_cost == Decimal("111.10000000")


@pytest.mark.integration
def test_a_sell_cost_decomposes_exactly() -> None:
    cost = cost_of_fill(
        identity=X,
        session=SESSIONS[0],
        side=OrderSide.SELL,
        bid=Decimal("99"),
        ask=Decimal("101"),
        filled_quantity=Decimal("100"),
        fill_price=Decimal("98.901"),
        fee=Decimal("1"),
        fill_id="F2",
    )

    assert cost.spread_cost == Decimal("100.00000000")
    assert cost.slippage_cost == Decimal("9.90000000")
    assert cost.implementation_shortfall == Decimal("109.90000000")
    assert cost.spread_cost + cost.slippage_cost == cost.implementation_shortfall


@pytest.mark.integration
def test_both_sides_report_cost_as_positive() -> None:
    buy = cost_of_fill(
        identity=X,
        session=SESSIONS[0],
        side=OrderSide.BUY,
        bid=Decimal("99"),
        ask=Decimal("101"),
        filled_quantity=Decimal("10"),
        fill_price=Decimal("101"),
        fee=Decimal("0"),
        fill_id="B",
    )
    sell = cost_of_fill(
        identity=X,
        session=SESSIONS[0],
        side=OrderSide.SELL,
        bid=Decimal("99"),
        ask=Decimal("101"),
        filled_quantity=Decimal("10"),
        fill_price=Decimal("99"),
        fee=Decimal("0"),
        fill_id="S",
    )

    assert buy.spread_cost > Decimal("0")
    assert sell.spread_cost > Decimal("0")


@pytest.mark.integration
def test_a_broken_decomposition_is_detected() -> None:
    from stock_screener.execution import SessionFillCost

    tampered = SessionFillCost(
        identity=X,
        session=SESSIONS[0],
        side=OrderSide.BUY,
        bid=Decimal("99"),
        ask=Decimal("101"),
        mid=Decimal("100"),
        observed_share_volume=Decimal("5000"),
        participation_ceiling=Decimal("500"),
        filled_quantity=Decimal("100"),
        fill_price=Decimal("101"),
        spread_cost=Decimal("100"),
        slippage_cost=Decimal("0"),
        implementation_shortfall=Decimal("999"),
        fee=Decimal("0"),
        fill_id="F",
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_COST_DECOMPOSITION_VIOLATED):
        tampered.check()


@pytest.mark.integration
def test_the_total_is_the_same_by_both_routes() -> None:
    costs = [
        cost_of_fill(
            identity=X,
            session=SESSIONS[0],
            side=OrderSide.BUY,
            bid=Decimal("99"),
            ask=Decimal("101"),
            filled_quantity=Decimal("100"),
            fill_price=Decimal("101.101"),
            fee=Decimal("1"),
            fill_id="F1",
        ),
        cost_of_fill(
            identity=X,
            session=SESSIONS[1],
            side=OrderSide.BUY,
            bid=Decimal("98"),
            ask=Decimal("102"),
            filled_quantity=Decimal("50"),
            fill_price=Decimal("102.102"),
            fee=Decimal("1"),
            fill_id="F2",
        ),
    ]
    report = build_cost_report(
        execution_plan_id="P",
        policy_fingerprint="pf",
        session_costs=costs,
        planned={X.key: (X, Decimal("150"))},
    )

    report.check()
    assert report.total_execution_cost == report.implementation_shortfall + report.fees
    assert report.total_execution_cost == (report.spread_cost + report.slippage_cost + report.fees)


@pytest.mark.integration
def test_the_vwap_across_sessions_is_volume_weighted() -> None:
    costs = [
        cost_of_fill(
            identity=X,
            session=SESSIONS[0],
            side=OrderSide.BUY,
            bid=Decimal("99"),
            ask=Decimal("101"),
            filled_quantity=Decimal("900"),
            fill_price=Decimal("101"),
            fee=Decimal("0"),
            fill_id="F1",
        ),
        cost_of_fill(
            identity=X,
            session=SESSIONS[1],
            side=OrderSide.BUY,
            bid=Decimal("199"),
            ask=Decimal("201"),
            filled_quantity=Decimal("100"),
            fill_price=Decimal("201"),
            fee=Decimal("0"),
            fill_id="F2",
        ),
    ]
    report = build_cost_report(
        execution_plan_id="P",
        policy_fingerprint="pf",
        session_costs=costs,
        planned={X.key: (X, Decimal("1000"))},
    )

    # Weighted: (900*101 + 100*201) / 1000 = 111. An unweighted mean would be 151.
    assert report.identity_costs[0].vwap_fill_price == Decimal("111.00000000")
    assert report.identity_costs[0].sessions_used == 2


@pytest.mark.integration
def test_participation_is_undefined_when_a_session_traded_nothing() -> None:
    cost = cost_of_fill(
        identity=X,
        session=SESSIONS[0],
        side=OrderSide.BUY,
        bid=Decimal("99"),
        ask=Decimal("101"),
        filled_quantity=Decimal("1"),
        fill_price=Decimal("101"),
        fee=Decimal("0"),
        fill_id="F",
        observed_share_volume=Decimal("0"),
    )

    assert cost.actual_participation == "UNDEFINED"


@pytest.mark.integration
def test_the_cost_report_posts_nothing_to_the_book(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(bps="10", participation="1", sessions=1)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="1000000"))},
        first_session=SESSIONS[0],
    )
    before = accounting.state_fingerprint(book_state(ledger))
    records = len(ledger)

    report = session.cost_report()

    assert report.total_execution_cost > Decimal("0")
    assert accounting.state_fingerprint(book_state(ledger)) == before
    assert len(ledger) == records
    assert report.as_json()["accounting_effect"] == "NONE"


@pytest.mark.integration
def test_the_cost_report_explains_why_it_posts_nothing(tmp_path: Path) -> None:
    report = build_cost_report(
        execution_plan_id="P", policy_fingerprint="pf", session_costs=[], planned={}
    )

    assert "charge it twice" in report.as_json()["accounting_note"]


@pytest.mark.integration
def test_the_book_already_holds_the_execution_cost(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(bps="10", participation="1", sessions=1)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="1000000"))},
        first_session=SESSIONS[0],
    )

    # The cost is inside the price the book paid: 1000 shares at 101.101 rather
    # than at the midpoint's 100.
    position = book_state(ledger).position(X)

    assert position.total_cost == Decimal("101101.0000")
    assert session.cost_report().implementation_shortfall == Decimal("1101.00000000")


# --------------------------------------------------------------------------- #
# Drift between sessions
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_unrelated_event_between_sessions_stops_rollover(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )
    snapshots = {
        SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
        SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
    }
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: snapshots[SESSIONS[0]]},
        first_session=SESSIONS[0],
    )
    filled = book_state(ledger).position(X).quantity

    store.commit(
        ledger,
        accounting.ExternalCashFlowEvent(
            event_id="OUTSIDE",
            event_type=accounting.EventType.EXTERNAL_CASH_FLOW,
            effective_at=at(SESSIONS[1], 9),
            source_id="BANK",
            source_fingerprint="fp-bank",
            direction=accounting.CashFlowDirection.DEPOSIT,
            amount=Decimal("1000"),
        ),
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_STATE_DRIFT):
        session.execute_identity(plan.orders[0], snapshots=snapshots, first_session=SESSIONS[1])
    assert book_state(ledger).position(X).quantity == filled


@pytest.mark.integration
def test_drift_does_not_re_run_strategy_or_risk(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(tmp_path, plan, store, ledger)
    store.commit(
        ledger,
        accounting.ExternalCashFlowEvent(
            event_id="OUTSIDE",
            event_type=accounting.EventType.EXTERNAL_CASH_FLOW,
            effective_at=at(SESSIONS[0], 10),
            source_id="BANK",
            source_fingerprint="fp-bank",
            direction=accounting.CashFlowDirection.DEPOSIT,
            amount=Decimal("1000"),
        ),
    )

    with pytest.raises(ExecutionError, match="a new plan is required"):
        session.require_no_drift()
    assert session.status is PlanStatus.FAILED


@pytest.mark.integration
def test_this_plans_own_fills_are_not_drift(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )

    assert session.drift_detected is False
    assert len(outcomes) == 3


# --------------------------------------------------------------------------- #
# Crash recovery
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_partial_fill_survives_a_fresh_process(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )
    expected = order_state_fingerprint(session.journal.derive())

    reopened = ExecutionStore(tmp_path / ".execution", execution_plan_id=plan.execution_plan_id)
    derived = reopened.load_journal().derive()

    assert order_state_fingerprint(derived) == expected
    assert derived.status(outcomes[0].order_id).cumulative_filled_quantity == Decimal(
        "500.00000000"
    )
    assert derived.status(outcomes[0].order_id).state is OrderState.EXPIRED


@pytest.mark.integration
def test_a_multi_session_sequence_reconstructs_from_the_journal(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )
    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )

    derived = (
        ExecutionStore(tmp_path / ".execution", execution_plan_id=plan.execution_plan_id)
        .load_journal()
        .derive()
    )

    assert len(derived.statuses) == len(outcomes)
    assert sum(
        (row.cumulative_filled_quantity for row in derived.statuses.values()),
        Decimal(0),
    ) == Decimal("1000.00000000")
    assert sum(1 for row in derived.statuses.values() if row.state is OrderState.EXPIRED) == 2


@pytest.mark.integration
def test_a_child_created_before_a_crash_is_not_created_twice(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    child = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=plan.orders[0].quantity,
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )
    # Created, then the process dies before submission.
    session.store.commit(
        session.journal,
        session.broker.create(plan, child, session=SESSIONS[0], at=at(SESSIONS[0])),
    )
    records_before = len(session.journal)

    # The same residual in the same session derives the same id, so resuming
    # finds the child rather than making a second one.
    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )

    assert outcomes[0].order_id == child.order_id
    assert len(session.journal.derive().statuses) == 1
    assert len(session.journal) > records_before


@pytest.mark.integration
def test_a_pending_fill_is_applied_exactly_once_on_recovery(tmp_path: Path) -> None:
    from stock_screener.execution import admit_fill

    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    child = build_child_order(
        plan=plan,
        parent=plan.orders[0],
        session=SESSIONS[0],
        generation=1,
        residual=plan.orders[0].quantity,
        sizing_price=Decimal("100"),
        created_at=at(SESSIONS[0]),
    )
    broker = session.broker
    for event in (
        broker.create(plan, child, session=SESSIONS[0], at=at(SESSIONS[0])),
        broker.submit(plan, child, at=at(SESSIONS[0])),
        broker.acknowledge(plan, child, at=at(SESSIONS[0])),
    ):
        session.store.commit(session.journal, event)
    fill_event = session.broker.fill(
        plan,
        child,
        snapshot=market(SESSIONS[0], quote(volume="5000")),
        session=SESSIONS[0],
        order_sessions={child.order_id: SESSIONS[0]},
    )
    assert fill_event is not None
    session.store.commit(session.journal, fill_event)
    assert fill_event.fill is not None

    # The book receives it, then the process dies before FILL_APPLIED.
    first = admit_fill(fill_event.fill, ledger=ledger, store=store)
    second = admit_fill(fill_event.fill, ledger=ledger, store=store)

    assert first["written"] is True
    assert second["written"] is False
    assert book_state(ledger).position(X).quantity == Decimal("500.00000000")


@pytest.mark.integration
def test_journal_truncation_is_still_detected(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )
    session.store.record_paths()[-1].unlink()

    assert session.store.classify()["status"] == "TRUNCATION_DETECTED"


@pytest.mark.integration
def test_journal_tampering_is_still_detected(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )
    path = session.store.record_paths()[1]
    body = json.loads(path.read_text(encoding="utf-8"))
    body["event"]["payload"]["venue"] = "TAMPERED"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH):
        session.store.load_journal()


@pytest.mark.integration
def test_the_anchor_remains_mandatory(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )

    anchor = session.store.read_anchor()

    assert anchor is not None
    assert anchor.sequence == len(session.journal)


@pytest.mark.integration
def test_a_refused_event_never_becomes_durable(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )
    child = session.journal.derive().status(outcomes[0].order_id).order
    records_before = len(session.store.record_paths())
    anchor_before = session.store.read_anchor()
    from stock_screener.execution import ExecutionEventType, OrderExpiredEvent

    # The child is already EXPIRED; a second expiry is not a legal move.
    with pytest.raises(ExecutionError):
        session.store.commit(
            session.journal,
            OrderExpiredEvent(
                event_id=f"{child.order_id}:EXPIRED-AGAIN",
                event_type=ExecutionEventType.ORDER_EXPIRED,
                order_id=child.order_id,
                execution_plan_id=plan.execution_plan_id,
                occurred_at=at(SESSIONS[1]),
                session=SESSIONS[1],
                residual_quantity=Decimal("1"),
            ),
        )

    assert len(session.store.record_paths()) == records_before
    assert session.store.read_anchor() == anchor_before


@pytest.mark.integration
def test_a_coordinated_rollback_is_still_not_detected(tmp_path: Path) -> None:
    from stock_screener.execution import ExecutionJournalHeadAnchor

    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=1)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={SESSIONS[0]: market(SESSIONS[0], quote(volume="5000"))},
        first_session=SESSIONS[0],
    )
    keep = 3
    for path in session.store.record_paths()[keep:]:
        path.unlink()
    rolled = session.store.load_journal()
    (tmp_path / ".execution" / "head.json").unlink()
    session.store.write_anchor(
        ExecutionJournalHeadAnchor(
            journal_generation=session.store.generation,
            sequence=keep,
            journal_head_hash=rolled.records[-1].record_hash,
            order_state_fingerprint=order_state_fingerprint(rolled.derive()),
            execution_plan_id=plan.execution_plan_id,
        )
    )

    # The honest result. There is no test here claiming otherwise.
    assert session.store.classify()["consistent"] is True


# --------------------------------------------------------------------------- #
# Attribution across children
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_attribution_is_preserved_across_child_orders(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(
        store,
        ledger,
        strategy=None,
        attribution={"COMPANY:1": (("ALPHA", Decimal("600")), ("BETA", Decimal("400")))},
    )
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )

    session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )
    position = book_state(ledger).position(X)
    assert isinstance(position, accounting.PooledPosition)
    pools = {pool.owner: pool.quantity for pool in position.retained_pools()}

    assert position.quantity == Decimal("1000.00000000")
    assert sum(pools.values(), Decimal(0)) == Decimal("1000.00000000")
    assert pools["ALPHA"] == Decimal("600.00000000")
    assert pools["BETA"] == Decimal("400.00000000")


@pytest.mark.integration
def test_every_fill_attribution_sums_to_its_quantity(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(
        store,
        ledger,
        strategy=None,
        attribution={"COMPANY:1": (("ALPHA", Decimal("600")), ("BETA", Decimal("400")))},
    )
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )

    for fill in session.journal.derive().fills.values():
        assert sum((row.quantity for row in fill.attribution), Decimal(0)) == fill.quantity


@pytest.mark.integration
def test_no_owner_exceeds_its_authorised_attribution(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path)
    plan = make_plan(
        store,
        ledger,
        strategy=None,
        attribution={"COMPANY:1": (("ALPHA", Decimal("600")), ("BETA", Decimal("400")))},
    )
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(participation="0.10", sessions=3)
    )
    session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )
    position = book_state(ledger).position(X)
    assert isinstance(position, accounting.PooledPosition)
    pools = {pool.owner: pool.quantity for pool in position.retained_pools()}

    assert pools["ALPHA"] <= Decimal("600.00000000")
    assert pools["BETA"] <= Decimal("400.00000000")


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

V1_1_MODULES = (
    "calendar.py",
    "cost.py",
    "participation.py",
    "realism_policy.py",
    "session_broker.py",
    "session_engine.py",
    "session_market.py",
)


def imported(path: Path) -> set[str]:
    """Every module a file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.integration
@pytest.mark.parametrize("name", V1_1_MODULES)
def test_no_v1_1_module_imports_a_broker_sdk(name: str) -> None:
    assert not imported(SOURCE / name) & {
        "alpaca",
        "alpaca_trade_api",
        "ib_insync",
        "ibapi",
        "schwab",
        "robinhood",
        "ccxt",
    }


@pytest.mark.integration
@pytest.mark.parametrize("name", V1_1_MODULES)
def test_no_v1_1_module_imports_a_network_client(name: str) -> None:
    assert not imported(SOURCE / name) & {
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "aiohttp",
        "websockets",
    }


@pytest.mark.integration
@pytest.mark.parametrize("name", (*V1_1_MODULES, "realism_run.py"))
def test_no_v1_1_module_imports_randomness(name: str) -> None:
    assert not imported(SOURCE / name) & {"random", "secrets", "numpy.random", "uuid"}


@pytest.mark.integration
def test_the_v1_1_modules_do_not_couple_to_the_research_layer() -> None:
    for name in V1_1_MODULES:
        names = imported(SOURCE / name)
        assert not {row for row in names if "prospective" in row}
        assert not {row for row in names if "membership" in row}
        assert not {row for row in names if "backtest" in row}
        assert not {row for row in names if "research" in row}


@pytest.mark.integration
def test_the_session_broker_holds_no_book(tmp_path: Path) -> None:
    broker = SessionPaperBroker(journal=ExecutionJournal(), policy=policy())
    described = broker.describe()

    assert described["holds_cash"] is False
    assert described["holds_positions"] is False
    assert described["holds_pnl"] is False
    assert not hasattr(broker, "cash")
    assert not hasattr(broker, "positions")
    assert not hasattr(broker, "buying_power")


@pytest.mark.integration
def test_the_session_broker_declares_what_it_does_not_model() -> None:
    described = SessionPaperBroker(journal=ExecutionJournal(), policy=policy()).describe()

    for omission in ("market impact", "latency", "queue position", "hidden liquidity"):
        assert omission in described["does_not_model"]


@pytest.mark.integration
def test_the_v1_1_engine_works_with_the_network_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    import sqlite3

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("network is unavailable")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)

    store, ledger = open_book(tmp_path)
    plan = make_plan(store, ledger)
    session = session_for(
        tmp_path, plan, store, ledger, friction=policy(bps="10", participation="0.10", sessions=3)
    )

    outcomes = session.execute_identity(
        plan.orders[0],
        snapshots={
            SESSIONS[0]: market(SESSIONS[0], quote(volume="4000")),
            SESSIONS[1]: market(SESSIONS[1], quote(volume="3500")),
            SESSIONS[2]: market(SESSIONS[2], quote(volume="3000")),
        },
        first_session=SESSIONS[0],
    )

    assert len(outcomes) == 3
    assert session.settle() is PlanStatus.COMPLETED
    assert session.cost_report().total_execution_cost > Decimal("0")


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_v1_1_manifest_is_a_distinct_version() -> None:
    manifest = realism_manifest(policy=policy(), calendar=calendar())

    assert manifest["manifest_version"] == EXECUTION_MANIFEST_VERSION_V1_1
    assert manifest["manifest_version"] != execution_manifest()["manifest_version"]


@pytest.mark.integration
def test_the_v1_1_manifest_binds_the_policy_and_calendar() -> None:
    friction = policy(bps="10")
    cal = calendar()
    manifest = realism_manifest(policy=friction, calendar=cal)

    assert manifest["bindings"]["execution_policy_fingerprint"] == friction.fingerprint()
    assert manifest["bindings"]["calendar_fingerprint"] == cal.fingerprint()


@pytest.mark.integration
def test_the_v1_1_manifest_binds_everything_a_run_depends_on() -> None:
    manifest = realism_manifest(
        policy=policy(),
        calendar=calendar(),
        execution_plan_fingerprint="p" * 64,
        journal_generation="J1",
        journal_sequence=9,
        journal_head="h" * 64,
        journal_anchor_fingerprint="a" * 64,
        accounting_generation="G1",
        accounting_version=V2,
        initial_accounting_state="i" * 64,
        final_accounting_state="f" * 64,
        session_snapshot_fingerprints=("s" * 64,),
        cost_report_fingerprint="c" * 64,
        realism_report_fingerprint="r" * 64,
        code_commit="abc123",
    )

    assert all(value is not None for value in manifest["bindings"].values())


@pytest.mark.integration
def test_the_v1_1_manifest_states_the_cost_identities() -> None:
    invariants = realism_manifest(policy=policy(), calendar=calendar())["invariants"]

    assert any("spread cost + slippage cost" in line for line in invariants)
    assert any("never fills below the ask" in line for line in invariants)
    assert any("never survives its session" in line for line in invariants)
    assert any("participation ceiling" in line for line in invariants)


@pytest.mark.integration
def test_the_v1_1_manifest_states_what_is_not_modelled() -> None:
    not_guaranteed = realism_manifest(policy=policy(), calendar=calendar())["not_guaranteed"]

    assert any("COORDINATED_LOCAL_ROLLBACK_DETECTION" in line for line in not_guaranteed)
    assert any("Market impact, latency, queue position" in line for line in not_guaranteed)
    assert any("is calibrated from any strategy's returns" in line for line in not_guaranteed)


@pytest.mark.integration
def test_the_v1_1_manifest_declares_live_trading_unauthorised() -> None:
    manifest = realism_manifest(policy=policy(), calendar=calendar())

    assert manifest["live_trading"] == "NOT_AUTHORIZED"
    assert manifest["live_broker"] == "NOT_IMPLEMENTED"
    assert manifest["external_paper_broker"] == "NOT_IMPLEMENTED"


# --------------------------------------------------------------------------- #
# The goldens
# --------------------------------------------------------------------------- #

REALISM_GOLDENS = (
    "realism_cost_buy",
    "realism_cost_sell",
    "realism_participation",
    "realism_multi_session",
    "realism_window_exhausted",
)


@pytest.mark.integration
@pytest.mark.parametrize("name", REALISM_GOLDENS)
def test_a_realism_fixture_reproduces_its_golden(name: str, tmp_path: Path) -> None:
    request = load_realism_request(FIXTURES / f"{name}.json")
    golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))

    assert run_session_execution(request, root=tmp_path) == golden


@pytest.mark.integration
def test_the_buy_cost_golden_matches_the_hand_calculation() -> None:
    golden = json.loads((FIXTURES / "realism_cost_buy.golden.json").read_text(encoding="utf-8"))
    row = golden["cost_report"]["per_session"][0]

    assert row["bid"] == "99.00000000"
    assert row["ask"] == "101.00000000"
    assert row["mid"] == "100.00000000"
    assert row["fill_price"] == "101.10100000"
    assert row["spread_cost"] == "100.00000000"
    assert row["slippage_cost"] == "10.10000000"
    assert row["implementation_shortfall"] == "110.10000000"
    assert row["total_execution_cost"] == "110.10000000"


@pytest.mark.integration
def test_the_sell_cost_golden_mirrors_the_buy() -> None:
    golden = json.loads((FIXTURES / "realism_cost_sell.golden.json").read_text(encoding="utf-8"))
    row = golden["cost_report"]["per_session"][0]

    assert row["fill_price"] == "98.90100000"
    assert row["spread_cost"] == "100.00000000"
    # 9.9 rather than the buy's 10.1: the bps apply to the bid, not the ask.
    assert row["slippage_cost"] == "9.90000000"
    assert row["implementation_shortfall"] == "109.90000000"


@pytest.mark.integration
def test_the_participation_golden_caps_at_the_ceiling() -> None:
    golden = json.loads(
        (FIXTURES / "realism_participation.golden.json").read_text(encoding="utf-8")
    )
    row = golden["cost_report"]["per_session"][0]
    outcome = golden["session_outcomes"]["COMPANY:8101"][0]

    assert row["observed_share_volume"] == "5000.00000000"
    assert row["participation_ceiling"] == "500.00000000"
    assert row["filled_quantity"] == "500.00000000"
    assert row["actual_participation"] == "0.10000000"
    assert outcome["expired"] is True
    assert golden["plan_status"] == "PARTIALLY_COMPLETED"


@pytest.mark.integration
def test_the_multi_session_golden_uses_three_distinct_children() -> None:
    golden = json.loads(
        (FIXTURES / "realism_multi_session.golden.json").read_text(encoding="utf-8")
    )
    outcomes = golden["session_outcomes"]["COMPANY:8101"]

    assert [row["filled_quantity"] for row in outcomes] == [
        "400.00000000",
        "350.00000000",
        "250.00000000",
    ]
    assert len({row["order_id"] for row in outcomes}) == 3
    assert [row["session"] for row in outcomes] == [
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
    ]
    assert golden["plan_status"] == "COMPLETED"
    assert golden["realism_report"]["expiry_count"] == 2
    assert golden["realism_report"]["unfilled_residual"] == "0"


@pytest.mark.integration
def test_the_window_exhausted_golden_stops_at_two_children() -> None:
    golden = json.loads(
        (FIXTURES / "realism_window_exhausted.golden.json").read_text(encoding="utf-8")
    )
    outcomes = golden["session_outcomes"]["COMPANY:8101"]

    assert len(outcomes) == 2
    assert golden["realism_report"]["unfilled_residual"] == "250.00000000"
    assert golden["plan_status"] == "PARTIALLY_COMPLETED"


@pytest.mark.integration
@pytest.mark.parametrize("name", REALISM_GOLDENS)
def test_no_realism_golden_posts_to_accounting(name: str) -> None:
    golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))

    assert golden["cost_report"]["accounting_effect"] == "NONE"
    assert golden["realism_report"]["accounting_effect"] == "NONE"
    assert golden["realism_report"]["performance_claim"] == "NONE"


@pytest.mark.integration
@pytest.mark.parametrize("name", REALISM_GOLDENS)
def test_every_realism_golden_satisfies_the_cost_identities(name: str) -> None:
    golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))
    totals = golden["cost_report"]["totals"]

    assert Decimal(totals["spread_cost"]) + Decimal(totals["slippage_cost"]) == Decimal(
        totals["implementation_shortfall"]
    )
    assert Decimal(totals["implementation_shortfall"]) + Decimal(totals["fees"]) == Decimal(
        totals["total_execution_cost"]
    )


@pytest.mark.integration
def test_a_realism_request_missing_its_policy_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"opening": {}, "calendar": {}}), encoding="utf-8")

    with pytest.raises(ExecutionError, match=Code.PLAN_INVALID):
        load_realism_request(path)
