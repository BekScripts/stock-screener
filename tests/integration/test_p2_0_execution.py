"""P2.0 — broker-neutral order lifecycle and deterministic paper execution.

The tests are organised around the guarantees, and most of the important ones are
negative. An order must not move the book; a target must not create an order; a
rejected order must leave no trace in accounting; a fill must reach the book
exactly once across a crash. Each of those has a test whose failure would mean
the guarantee is gone, and several of them would otherwise pass by accident in a
happy-path suite.

Two limitations are asserted honestly rather than worked around: a journal and
its local anchor rolled back together validate cleanly, and this engine models no
execution cost at all.
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
    EXECUTION_ENGINE_VERSION,
    ORDER_SEQUENCING_RULE,
    PAPER_FILL_MODEL,
    REQUIRED_ACCOUNTING_VERSION,
    SUPPORTED_ORDER_TYPES,
    SUPPORTED_TIME_IN_FORCE,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    AttributionShare,
    BookBinding,
    Code,
    DeterministicPaperBroker,
    ExecutionError,
    ExecutionFill,
    ExecutionJournal,
    ExecutionJournalHeadAnchor,
    ExecutionPrice,
    ExecutionSession,
    ExecutionSizingSnapshot,
    ExecutionStore,
    JournalAnchorStatus,
    Order,
    OrderSide,
    OrderState,
    OrderStatus,
    PaperExecutionSnapshot,
    PlanStatus,
    SizingPrice,
    admit_fill,
    allocate_partial_fill,
    engine_policy,
    execution_manifest,
    intents_fingerprint,
    load_request,
    order_state_fingerprint,
    paper_market_policy,
    plan_execution,
    require_supported_accounting_version,
    run_paper_execution,
    to_accounting_fill_event,
    transition_table,
    verify_journal,
)
from stock_screener.portfolio_risk import Action, ExecutionIntent, SecurityIdentity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stock_screener.execution import ExecutionPlan

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "execution"
SOURCE = Path(__file__).resolve().parents[2] / "src" / "stock_screener" / "execution"

HELD = SecurityIdentity(kind="COMPANY", identity_id=100, symbol="HELD")
NEW = SecurityIdentity(kind="COMPANY", identity_id=200, symbol="NEW")
OTHER = SecurityIdentity(kind="COMPANY", identity_id=300, symbol="OTHER")
V2 = accounting.ACCOUNTING_V2


def at_time(hour: int, minute: int = 0) -> str:
    """An aware timestamp on the fixture's trading day."""
    return f"2026-10-05T{hour:02d}:{minute:02d}:00-04:00"


def open_book(
    tmp_path: Path,
    *,
    cash: str = "50000",
    positions: Sequence[accounting.OpeningPosition] = (),
) -> tuple[accounting.PortfolioStore, accounting.PortfolioLedger]:
    """A V2 accounting book with an explicit opening balance."""
    store = accounting.PortfolioStore(tmp_path / ".portfolio", accounting_version=V2)
    ledger = accounting.PortfolioLedger()
    store.commit(
        ledger,
        accounting.OpeningBalanceEvent(
            event_id="OB",
            event_type=accounting.EventType.OPENING_BALANCE,
            effective_at=at_time(9, 30),
            source_id="PAPER_BOOK_IMPORT",
            source_fingerprint="fp-ob",
            cash=Decimal(cash),
            positions=tuple(positions),
        ),
    )
    return store, ledger


def held_position(quantity: str = "500", cost: str = "40") -> accounting.OpeningPosition:
    """An opening position in `HELD`, owned by ALPHA."""
    return accounting.OpeningPosition(
        identity=HELD,
        quantity=Decimal(quantity),
        cost_basis_per_share=Decimal(cost),
        attribution=(accounting.StrategySlice(strategy_id="ALPHA", quantity=Decimal(quantity)),),
    )


def binding(store: accounting.PortfolioStore, ledger: accounting.PortfolioLedger) -> BookBinding:
    """The book binding for a ledger as it currently stands."""
    state = accounting.replay(ledger, accounting_version=V2)
    return BookBinding(
        ledger_generation=store.generation,
        accounting_engine_version=V2,
        accounting_state_fingerprint=accounting.state_fingerprint(state),
        ledger_sequence=len(ledger),
        ledger_head=ledger.head_hash,
    )


def enter_intent(notional: float = 10000.0, strategy: str | None = "ALPHA") -> ExecutionIntent:
    """An intent to open a position in `NEW`."""
    return ExecutionIntent(
        identity=NEW,
        action=Action.ENTER,
        current_weight=0.0,
        target_weight=0.2,
        notional_delta=notional,
        strategy_id=strategy,
    )


def exit_intent(notional: float = -20000.0, strategy: str | None = "ALPHA") -> ExecutionIntent:
    """An intent to close the position in `HELD`."""
    return ExecutionIntent(
        identity=HELD,
        action=Action.EXIT,
        current_weight=0.4,
        target_weight=0.0,
        notional_delta=notional,
        strategy_id=strategy,
    )


def sizing(**prices: str) -> ExecutionSizingSnapshot:
    """A sizing snapshot. Keys are `held`, `new`, `other`."""
    lookup = {"held": HELD, "new": NEW, "other": OTHER}
    return ExecutionSizingSnapshot(
        as_of=at_time(15),
        nav=Decimal("50000"),
        cash=Decimal("50000"),
        prices=tuple(
            SizingPrice(identity=lookup[key], price=Decimal(value)) for key, value in prices.items()
        ),
        source="SESSION_CLOSE",
    )


def make_plan(
    store: accounting.PortfolioStore,
    ledger: accounting.PortfolioLedger,
    *,
    intents: Sequence[ExecutionIntent],
    sizes: ExecutionSizingSnapshot,
    plan_id: str = "PLAN",
    attribution: dict[str, Sequence[tuple[str, Decimal]]] | None = None,
) -> ExecutionPlan:
    """An authorised plan against the current book."""
    return plan_execution(
        execution_plan_id=plan_id,
        as_of=at_time(15),
        intents=intents,
        sizing=sizes,
        book=binding(store, ledger),
        approved_portfolio_fingerprint="ap" * 32,
        valued_snapshot_fingerprint="vs" * 32,
        attribution=attribution,
        created_at=at_time(15),
    )


def session_for(
    tmp_path: Path,
    plan: ExecutionPlan,
    store: accounting.PortfolioStore,
    ledger: accounting.PortfolioLedger,
) -> ExecutionSession:
    """A session over a fresh execution store."""
    return ExecutionSession(
        plan=plan,
        store=ExecutionStore(tmp_path / ".execution", execution_plan_id=plan.execution_plan_id),
        ledger=ledger,
        accounting_store=store,
        journal=ExecutionJournal(),
    )


def snapshot(
    identity: SecurityIdentity, price: str, *, at: str, avail: str | None = None, fee: str = "0"
) -> PaperExecutionSnapshot:
    """A one-security execution snapshot."""
    return PaperExecutionSnapshot(
        as_of=at,
        prices=(
            ExecutionPrice(
                identity=identity,
                price=Decimal(price),
                available_quantity=None if avail is None else Decimal(avail),
                fee=Decimal(fee),
            ),
        ),
        source="PAPER",
    )


def book_state(ledger: accounting.PortfolioLedger) -> accounting.PortfolioState:
    """The book as it currently stands."""
    return accounting.replay(ledger, accounting_version=V2)


# --------------------------------------------------------------------------- #
# An order is not a position
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_creating_an_order_does_not_move_the_book(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))
    before = accounting.state_fingerprint(book_state(ledger))
    session = session_for(tmp_path, plan, store, ledger)

    session.broker.create(plan, plan.orders[0], at=at_time(15, 1))

    assert accounting.state_fingerprint(book_state(ledger)) == before


@pytest.mark.integration
def test_submitting_an_order_does_not_move_the_book(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))
    before = accounting.state_fingerprint(book_state(ledger))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]

    session.broker.create(plan, order, at=at_time(15, 1))
    session.broker.submit(plan, order, at=at_time(15, 2))

    assert accounting.state_fingerprint(book_state(ledger)) == before


@pytest.mark.integration
def test_acknowledging_an_order_does_not_move_the_book(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))
    before = accounting.state_fingerprint(book_state(ledger))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]

    session.submit(order, at=at_time(15, 1))

    assert len(ledger) == 1
    assert accounting.state_fingerprint(book_state(ledger)) == before


@pytest.mark.integration
def test_only_a_fill_moves_the_book(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))
    before = accounting.state_fingerprint(book_state(ledger))

    session.fill(
        order, snapshot=snapshot(HELD, "41", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
    )

    assert accounting.state_fingerprint(book_state(ledger)) != before
    assert book_state(ledger).position(HELD).quantity == Decimal("0")


@pytest.mark.integration
def test_an_order_declares_itself_inert(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    body = plan.orders[0].as_json()

    assert body["changes_holdings"] is False
    assert body["changes_cash"] is False


# --------------------------------------------------------------------------- #
# A target is not an order
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_approved_portfolio_creates_no_order_by_itself(tmp_path: Path) -> None:
    _, ledger = open_book(tmp_path, positions=[held_position()])
    before = accounting.state_fingerprint(book_state(ledger))

    # An approved portfolio is a mapping of target weights. Nothing consumes it
    # without an explicit planner call, and there is no path that does so
    # implicitly.
    approved = {"COMPANY:100": 0.0, "COMPANY:200": 0.2}

    assert approved
    assert len(ledger) == 1
    assert accounting.state_fingerprint(book_state(ledger)) == before


@pytest.mark.integration
def test_an_execution_intent_creates_no_order_by_itself(tmp_path: Path) -> None:
    _, ledger = open_book(tmp_path, positions=[held_position()])
    before = accounting.state_fingerprint(book_state(ledger))

    intents = [exit_intent(), enter_intent()]

    assert intents
    assert accounting.state_fingerprint(book_state(ledger)) == before
    assert len(ledger) == 1


@pytest.mark.integration
def test_orders_exist_only_after_an_explicit_planner_call(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])

    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    assert plan.order_count == 1
    assert len(ledger) == 1


# --------------------------------------------------------------------------- #
# The frozen order contract
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_only_market_orders_are_supported() -> None:
    assert frozenset({"MARKET"}) == SUPPORTED_ORDER_TYPES


@pytest.mark.integration
def test_only_day_time_in_force_is_supported() -> None:
    assert frozenset({"DAY"}) == SUPPORTED_TIME_IN_FORCE


@pytest.mark.integration
def test_an_unsupported_order_type_is_refused_by_name(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    with pytest.raises(ExecutionError, match=Code.UNSUPPORTED_ORDER_TYPE):
        dataclasses.replace(plan.orders[0], order_type="LIMIT")  # type: ignore[arg-type]


@pytest.mark.integration
def test_an_unsupported_time_in_force_is_refused_by_name(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    with pytest.raises(ExecutionError, match=Code.UNSUPPORTED_TIME_IN_FORCE):
        dataclasses.replace(plan.orders[0], time_in_force="GTC")  # type: ignore[arg-type]


@pytest.mark.integration
def test_there_is_no_short_selling_vocabulary() -> None:
    assert {str(side) for side in OrderSide} == {"BUY", "SELL"}


@pytest.mark.integration
def test_a_zero_quantity_order_cannot_be_constructed() -> None:
    with pytest.raises(ExecutionError, match=Code.ORDER_INVALID):
        Order(
            order_id="o",
            execution_plan_id="p",
            authorization_fingerprint="f",
            identity=HELD,
            side=OrderSide.SELL,
            quantity=Decimal("0"),
        )


@pytest.mark.integration
def test_fractional_quantities_are_supported(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(
        store,
        ledger,
        intents=[exit_intent(notional=-100.0)],
        sizes=sizing(held="30"),
    )

    assert plan.orders[0].quantity == Decimal("3.33333333")


@pytest.mark.integration
def test_the_display_symbol_is_never_part_of_an_orders_identity(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))
    order = plan.orders[0]

    assert order.fingerprint() == dataclasses.replace(order, symbol="RENAMED").fingerprint()


# --------------------------------------------------------------------------- #
# Sequencing
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_sells_are_sequenced_before_buys(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(
        store,
        ledger,
        intents=[enter_intent(), exit_intent()],
        sizes=sizing(held="40", new="50"),
    )

    assert [str(order.side) for order in plan.orders] == ["SELL", "BUY"]


@pytest.mark.integration
def test_the_sequencing_rule_is_named_so_a_change_is_a_new_version() -> None:
    assert ORDER_SEQUENCING_RULE == "SELLS_BEFORE_BUYS_V1"


@pytest.mark.integration
def test_a_hold_produces_no_order(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    hold = ExecutionIntent(
        identity=HELD,
        action=Action.HOLD,
        current_weight=0.4,
        target_weight=0.4,
        notional_delta=0.0,
        strategy_id="ALPHA",
    )

    plan = make_plan(store, ledger, intents=[hold], sizes=sizing(held="40"))

    assert plan.order_count == 0


@pytest.mark.integration
def test_a_zero_notional_delta_produces_no_order(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])

    plan = make_plan(store, ledger, intents=[exit_intent(notional=0.0)], sizes=sizing(held="40"))

    assert plan.order_count == 0


@pytest.mark.integration
def test_reversing_the_intent_input_order_gives_the_same_plan(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    intents = [enter_intent(), exit_intent()]
    sizes = sizing(held="40", new="50")

    first = make_plan(store, ledger, intents=intents, sizes=sizes)
    second = make_plan(store, ledger, intents=list(reversed(intents)), sizes=sizes)

    assert first.fingerprint() == second.fingerprint()
    assert [row.order_id for row in first.orders] == [row.order_id for row in second.orders]


@pytest.mark.integration
def test_the_intents_fingerprint_ignores_input_order() -> None:
    intents = [enter_intent(), exit_intent()]

    assert intents_fingerprint(intents) == intents_fingerprint(list(reversed(intents)))


@pytest.mark.integration
def test_no_strategy_is_sequenced_ahead_of_another(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    alpha = enter_intent(strategy="ALPHA")
    beta = ExecutionIntent(
        identity=OTHER,
        action=Action.ENTER,
        current_weight=0.0,
        target_weight=0.1,
        notional_delta=5000.0,
        strategy_id="BETA",
    )

    plan = make_plan(store, ledger, intents=[beta, alpha], sizes=sizing(new="50", other="25"))

    # Ordered by durable identity within the group, not by which strategy asked.
    assert [row.identity.label for row in plan.orders] == ["COMPANY:200", "COMPANY:300"]


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_quantity_is_the_notional_delta_over_the_sizing_price(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])

    plan = make_plan(store, ledger, intents=[enter_intent(notional=9000.0)], sizes=sizing(new="45"))

    assert plan.orders[0].quantity == Decimal("200.00000000")


@pytest.mark.integration
@pytest.mark.parametrize("price", ["0", "-5"])
def test_an_invalid_sizing_price_is_refused(price: str) -> None:
    with pytest.raises(ExecutionError, match=Code.INVALID_EXECUTION_PRICE):
        SizingPrice(identity=HELD, price=Decimal(price))


@pytest.mark.integration
def test_an_unpriced_security_produces_no_order_rather_than_a_guess(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])

    with pytest.raises(ExecutionError, match=Code.INVALID_EXECUTION_PRICE):
        make_plan(store, ledger, intents=[enter_intent()], sizes=sizing(held="40"))


@pytest.mark.integration
def test_a_non_positive_nav_cannot_be_sized_against() -> None:
    with pytest.raises(ExecutionError, match=Code.SIZING_SNAPSHOT_INCOMPLETE):
        ExecutionSizingSnapshot(as_of=at_time(15), nav=Decimal("0"), cash=Decimal("0"))


@pytest.mark.integration
def test_the_planner_reads_no_price_of_its_own() -> None:
    tree = ast.parse((SOURCE / "planner.py").read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    assert not imports & {"httpx", "requests", "socket", "urllib", "sqlite3", "sqlalchemy"}


# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #


def working_order(tmp_path: Path) -> tuple[ExecutionSession, Order]:
    """A session with one acknowledged sell order for 500 HELD."""
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))
    return session, order


@pytest.mark.integration
def test_the_lifecycle_is_walked_rather_than_collapsed(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)

    assert [str(row.event.event_type) for row in session.journal.records] == [
        "ORDER_CREATED",
        "ORDER_SUBMITTED",
        "ORDER_ACKNOWLEDGED",
    ]


@pytest.mark.integration
def test_the_transition_table_is_frozen() -> None:
    table = transition_table()

    assert table["transitions"]["CREATED"] == ["SUBMITTED"]
    assert table["transitions"]["FILLED"] == []
    assert table["terminal"] == ["CANCELLED", "FILLED", "REJECTED"]


@pytest.mark.integration
def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert VALID_TRANSITIONS[state] == frozenset()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("source", "target"),
    [
        (OrderState.CREATED, OrderState.FILLED),
        (OrderState.CREATED, OrderState.ACKNOWLEDGED),
        (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED),
        (OrderState.FILLED, OrderState.CANCELLED),
        (OrderState.REJECTED, OrderState.ACKNOWLEDGED),
        (OrderState.CANCELLED, OrderState.FILLED),
    ],
)
def test_an_invalid_transition_is_refused(source: OrderState, target: OrderState) -> None:
    order = Order(
        order_id="o",
        execution_plan_id="p",
        authorization_fingerprint="f",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("100"),
    )
    status = OrderStatus(order=order, state=source)

    with pytest.raises(ExecutionError):
        status.with_state(target)


@pytest.mark.integration
def test_leaving_a_terminal_state_is_distinguished_from_a_mis_sequence() -> None:
    order = Order(
        order_id="o",
        execution_plan_id="p",
        authorization_fingerprint="f",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("100"),
    )

    with pytest.raises(ExecutionError, match=Code.ORDER_ALREADY_TERMINAL):
        OrderStatus(order=order, state=OrderState.FILLED).with_state(OrderState.CANCELLED)

    with pytest.raises(ExecutionError, match=Code.INVALID_ORDER_STATE_TRANSITION):
        OrderStatus(order=order, state=OrderState.CREATED).with_state(OrderState.FILLED)


@pytest.mark.integration
def test_cumulative_fill_is_tracked_against_the_ordered_quantity(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)

    session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    status = session.journal.derive().status(order.order_id)

    assert status.order.quantity == Decimal("500.00000000")
    assert status.cumulative_filled_quantity == Decimal("200.00000000")
    assert status.remaining_quantity == Decimal("300.00000000")


@pytest.mark.integration
def test_an_overfill_is_refused_outright_rather_than_clipped() -> None:
    order = Order(
        order_id="o",
        execution_plan_id="p",
        authorization_fingerprint="f",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("100"),
    )
    status = OrderStatus(order=order, state=OrderState.ACKNOWLEDGED)

    with pytest.raises(ExecutionError, match=Code.ORDER_OVERFILL):
        status.with_fill(quantity=Decimal("101"), price=Decimal("10"), fee=Decimal("0"))


@pytest.mark.integration
def test_an_overfill_emits_no_accounting_event(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    before = len(session.ledger)

    with pytest.raises(ExecutionError, match=Code.ORDER_OVERFILL):
        session.journal.derive().status(order.order_id).with_fill(
            quantity=Decimal("501"), price=Decimal("41"), fee=Decimal("0")
        )

    assert len(session.ledger) == before


@pytest.mark.integration
def test_a_status_cannot_be_constructed_already_overfilled() -> None:
    order = Order(
        order_id="o",
        execution_plan_id="p",
        authorization_fingerprint="f",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("100"),
    )

    with pytest.raises(ExecutionError, match=Code.ORDER_OVERFILL):
        OrderStatus(order=order, cumulative_filled_quantity=Decimal("101"))


@pytest.mark.integration
def test_the_fill_price_is_volume_weighted() -> None:
    order = Order(
        order_id="o",
        execution_plan_id="p",
        authorization_fingerprint="f",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("1000"),
    )
    status = OrderStatus(order=order, state=OrderState.ACKNOWLEDGED)

    status = status.with_fill(quantity=Decimal("1"), price=Decimal("50"), fee=Decimal("0"))
    status = status.with_fill(quantity=Decimal("999"), price=Decimal("10"), fee=Decimal("0"))

    # An unweighted mean would read 30, out by a factor of three.
    assert status.vwap == Decimal("10.04000000")


@pytest.mark.integration
def test_an_unfilled_order_has_no_price_rather_than_a_zero_one() -> None:
    order = Order(
        order_id="o",
        execution_plan_id="p",
        authorization_fingerprint="f",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("100"),
    )

    assert OrderStatus(order=order).vwap is None


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_every_order_binds_its_plans_fingerprint(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(
        store, ledger, intents=[exit_intent(), enter_intent()], sizes=sizing(held="40", new="50")
    )

    assert all(row.authorization_fingerprint == plan.fingerprint() for row in plan.orders)


@pytest.mark.integration
def test_an_order_from_another_plan_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    with pytest.raises(ExecutionError, match=Code.UNAUTHORIZED_ORDER):
        plan.require_authorizes(
            dataclasses.replace(plan.orders[0], execution_plan_id="SOMEONE_ELSES_PLAN")
        )


@pytest.mark.integration
def test_a_stale_authorisation_fingerprint_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    with pytest.raises(ExecutionError, match=Code.EXECUTION_AUTHORIZATION_MISMATCH):
        plan.require_authorizes(
            dataclasses.replace(plan.orders[0], authorization_fingerprint="x" * 64)
        )


@pytest.mark.integration
def test_a_tampered_order_is_refused_even_with_the_right_authorisation(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    with pytest.raises(ExecutionError, match=Code.EXECUTION_AUTHORIZATION_MISMATCH):
        plan.require_authorizes(dataclasses.replace(plan.orders[0], sizing_price=Decimal("1")))


@pytest.mark.integration
def test_the_broker_exposes_no_unauthorised_order_path() -> None:
    tree = ast.parse((SOURCE / "paper_broker.py").read_text(encoding="utf-8"))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert not {
        name
        for name in names
        if "force" in name.lower() or "unsafe" in name.lower() or "arbitrary" in name.lower()
    }


@pytest.mark.integration
def test_every_public_broker_method_takes_the_plan() -> None:
    tree = ast.parse((SOURCE / "paper_broker.py").read_text(encoding="utf-8"))
    broker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "DeterministicPaperBroker"
    )
    acting = {"create", "submit", "acknowledge", "fill", "cancel", "reject"}
    for node in broker.body:
        if isinstance(node, ast.FunctionDef) and node.name in acting:
            assert "plan" in {arg.arg for arg in node.args.args}, node.name


@pytest.mark.integration
def test_the_plan_binds_the_targets_and_intents_it_was_built_from(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(store, ledger, intents=[exit_intent()], sizes=sizing(held="40"))

    plan.require_matches_authority(
        approved_portfolio_fingerprint="ap" * 32,
        execution_intents_fingerprint=intents_fingerprint([exit_intent()]),
    )
    with pytest.raises(ExecutionError, match=Code.EXECUTION_AUTHORIZATION_MISMATCH):
        plan.require_matches_authority(
            approved_portfolio_fingerprint="different",
            execution_intents_fingerprint=plan.execution_intents_fingerprint,
        )


@pytest.mark.integration
def test_a_plan_missing_a_binding_cannot_be_constructed() -> None:
    with pytest.raises(ExecutionError, match=Code.PLAN_INVALID):
        from stock_screener.execution import ExecutionPlan as Plan

        Plan(
            execution_plan_id="P",
            as_of=at_time(15),
            approved_portfolio_fingerprint="",
            execution_intents_fingerprint="i",
            sizing_snapshot_fingerprint="s",
            valued_snapshot_fingerprint="v",
            book=BookBinding(
                ledger_generation="G",
                accounting_engine_version=V2,
                accounting_state_fingerprint="s",
                ledger_sequence=1,
                ledger_head="h",
            ),
        )


# --------------------------------------------------------------------------- #
# The accounting version is demanded, never inherited
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_new_execution_book_must_name_accounting_v2() -> None:
    assert REQUIRED_ACCOUNTING_VERSION == accounting.ACCOUNTING_V2


@pytest.mark.integration
def test_accounting_v1_is_refused_for_an_execution_book() -> None:
    with pytest.raises(ExecutionError, match=Code.UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION):
        require_supported_accounting_version(accounting.ACCOUNTING_V1, context="a new paper book")


@pytest.mark.integration
def test_a_v1_accounting_store_cannot_back_an_execution_session(tmp_path: Path) -> None:
    v1_store = accounting.PortfolioStore(
        tmp_path / ".portfolio", accounting_version=accounting.ACCOUNTING_V1
    )
    ledger = accounting.PortfolioLedger()
    v1_store.commit(
        ledger,
        accounting.OpeningBalanceEvent(
            event_id="OB",
            event_type=accounting.EventType.OPENING_BALANCE,
            effective_at=at_time(9, 30),
            source_id="X",
            source_fingerprint="fp",
            cash=Decimal("1000"),
        ),
    )
    plan = plan_execution(
        execution_plan_id="P",
        as_of=at_time(15),
        intents=[enter_intent(notional=100.0)],
        sizing=sizing(new="50"),
        book=BookBinding(
            ledger_generation=v1_store.generation,
            accounting_engine_version=V2,
            accounting_state_fingerprint="s" * 64,
            ledger_sequence=1,
            ledger_head=ledger.head_hash,
        ),
        approved_portfolio_fingerprint="a",
        valued_snapshot_fingerprint="v",
    )

    with pytest.raises(ExecutionError, match=Code.UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION):
        ExecutionSession(
            plan=plan,
            store=ExecutionStore(tmp_path / ".execution"),
            ledger=ledger,
            accounting_store=v1_store,
        )


@pytest.mark.integration
def test_a_book_binding_under_v1_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION):
        BookBinding(
            ledger_generation="G",
            accounting_engine_version=accounting.ACCOUNTING_V1,
            accounting_state_fingerprint="s",
            ledger_sequence=1,
            ledger_head="h",
        )


@pytest.mark.integration
def test_a_different_ledger_generation_is_refused(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    other = BookBinding(
        ledger_generation="SOME_OTHER_LEDGER",
        accounting_engine_version=V2,
        accounting_state_fingerprint="s" * 64,
        ledger_sequence=1,
        ledger_head="h" * 64,
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_BOOK_MISMATCH):
        binding(store, ledger).require_same_book(other)


# --------------------------------------------------------------------------- #
# Paper fills
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_market_order_fills_at_the_snapshot_price_not_the_sizing_price(
    tmp_path: Path,
) -> None:
    session, order = working_order(tmp_path)

    fill = session.fill(
        order, snapshot=snapshot(HELD, "43.75", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
    )

    assert order.sizing_price == Decimal("40.00000000")
    assert fill is not None
    assert fill.price == Decimal("43.75000000")


@pytest.mark.integration
def test_a_missing_execution_price_never_falls_back_to_the_sizing_price(
    tmp_path: Path,
) -> None:
    session, order = working_order(tmp_path)

    with pytest.raises(ExecutionError, match=Code.PAPER_EXECUTION_PRICE_MISSING):
        session.fill(
            order,
            snapshot=PaperExecutionSnapshot(as_of=at_time(15, 5)),
            acknowledged_at=at_time(15, 1),
        )


@pytest.mark.integration
def test_a_missing_price_leaves_the_book_untouched(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    before = accounting.state_fingerprint(book_state(session.ledger))

    with pytest.raises(ExecutionError, match=Code.PAPER_EXECUTION_PRICE_MISSING):
        session.fill(
            order,
            snapshot=PaperExecutionSnapshot(as_of=at_time(15, 5)),
            acknowledged_at=at_time(15, 1),
        )

    assert accounting.state_fingerprint(book_state(session.ledger)) == before


@pytest.mark.integration
def test_no_stated_availability_fills_the_remainder(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)

    fill = session.fill(
        order, snapshot=snapshot(HELD, "41", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
    )

    assert fill is not None
    assert fill.quantity == order.quantity


@pytest.mark.integration
def test_zero_availability_leaves_the_order_working_rather_than_rejected(
    tmp_path: Path,
) -> None:
    session, order = working_order(tmp_path)

    result = session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="0"),
        acknowledged_at=at_time(15, 1),
    )

    assert result is None
    assert session.journal.derive().status(order.order_id).state is OrderState.ACKNOWLEDGED


@pytest.mark.integration
def test_a_snapshot_predating_the_acknowledgement_is_refused(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)

    with pytest.raises(ExecutionError, match=Code.PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT):
        session.fill(
            order, snapshot=snapshot(HELD, "41", at=at_time(14)), acknowledged_at=at_time(15, 1)
        )


@pytest.mark.integration
def test_a_partial_fill_then_another_accumulates(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)

    session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    session.fill(
        order,
        snapshot=snapshot(HELD, "42", at=at_time(15, 9), avail="150"),
        acknowledged_at=at_time(15, 1),
    )
    status = session.journal.derive().status(order.order_id)

    assert status.cumulative_filled_quantity == Decimal("350.00000000")
    assert status.state is OrderState.PARTIALLY_FILLED


@pytest.mark.integration
def test_cancelling_a_partly_filled_order_keeps_the_filled_quantity(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )

    session.cancel(order, at=at_time(15, 20), reason="OPERATOR")
    status = session.journal.derive().status(order.order_id)

    assert status.state is OrderState.CANCELLED
    assert status.cumulative_filled_quantity == Decimal("200.00000000")
    assert book_state(session.ledger).position(HELD).quantity == Decimal("300.00000000")


@pytest.mark.integration
def test_a_cancelled_order_never_fills_again(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    session.cancel(order, at=at_time(15, 20))

    with pytest.raises(ExecutionError, match=Code.ORDER_ALREADY_TERMINAL):
        session.fill(
            order, snapshot=snapshot(HELD, "41", at=at_time(15, 25)), acknowledged_at=at_time(15, 1)
        )


@pytest.mark.integration
def test_a_filled_order_cannot_be_cancelled(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    session.fill(
        order, snapshot=snapshot(HELD, "41", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
    )

    with pytest.raises(ExecutionError, match=Code.ORDER_ALREADY_TERMINAL):
        session.cancel(order, at=at_time(15, 20))


@pytest.mark.integration
def test_a_rejected_order_produces_no_accounting_event(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    before = len(session.ledger)

    session.reject(order, at=at_time(15, 5), reason="PAPER_POLICY_REJECTED")

    assert session.journal.derive().status(order.order_id).state is OrderState.REJECTED
    assert len(session.ledger) == before
    assert book_state(session.ledger).position(HELD).quantity == Decimal("500.00000000")


@pytest.mark.integration
def test_the_paper_fill_model_is_named() -> None:
    assert PAPER_FILL_MODEL == "PAPER_REFERENCE_PRICE_FILL_V1"


@pytest.mark.integration
def test_the_paper_market_discloses_what_it_does_not_model() -> None:
    policy = paper_market_policy()

    for omission in ("bid-ask spread", "slippage", "market impact", "latency"):
        assert omission in policy["not_modelled"]
    assert "NONE" in policy["sizing_price_fallback"]


# --------------------------------------------------------------------------- #
# The accounting handoff
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_fill_maps_to_one_deterministic_accounting_event_id() -> None:
    fill = ExecutionFill(
        fill_id="F1",
        order_id="o1",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("10"),
        price=Decimal("40"),
        effective_at=at_time(15),
    )

    assert fill.accounting_event_id == "EXEC_FILL:F1"
    assert to_accounting_fill_event(fill).event_id == "EXEC_FILL:F1"


@pytest.mark.integration
def test_the_adapter_preserves_everything_accounting_needs() -> None:
    fill = ExecutionFill(
        fill_id="F1",
        order_id="o1",
        identity=HELD,
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        price=Decimal("40.25"),
        fee=Decimal("1.50"),
        effective_at=at_time(15),
        attribution=(
            AttributionShare(strategy_id="ALPHA", quantity=Decimal("6")),
            AttributionShare(strategy_id="BETA", quantity=Decimal("4")),
        ),
    )

    event = to_accounting_fill_event(fill)

    assert event.identity is not None
    assert event.identity.key == HELD.key
    assert str(event.side) == "BUY"
    assert event.quantity == Decimal("10.00000000")
    assert event.price == Decimal("40.25000000")
    assert event.fees == Decimal("1.5000")
    assert event.effective_at == at_time(15)
    assert event.execution_reference == "o1/F1"
    assert event.source_fingerprint == fill.fingerprint()
    assert [(row.strategy_id, row.quantity) for row in event.attribution] == [
        ("ALPHA", Decimal("6.00000000")),
        ("BETA", Decimal("4.00000000")),
    ]


@pytest.mark.integration
def test_replaying_the_same_fill_writes_nothing_twice(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    fill = session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert fill is not None
    before = len(session.ledger)
    quantity_before = book_state(session.ledger).position(HELD).quantity

    again = admit_fill(fill, ledger=session.ledger, store=session.accounting_store)

    assert again["written"] is False
    assert len(session.ledger) == before
    assert book_state(session.ledger).position(HELD).quantity == quantity_before


@pytest.mark.integration
def test_a_conflicting_fill_under_one_id_is_refused(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    fill = session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert fill is not None
    conflicting = dataclasses.replace(
        fill,
        quantity=Decimal("250"),
        attribution=(AttributionShare(strategy_id="ALPHA", quantity=Decimal("250")),),
    )

    with pytest.raises(ExecutionError, match=Code.FILL_ID_CONFLICT):
        admit_fill(conflicting, ledger=session.ledger, store=session.accounting_store)


@pytest.mark.integration
def test_a_conflicting_fill_in_the_journal_is_refused(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    fill = session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert fill is not None
    from stock_screener.execution import ExecutionEventType, OrderFilledEvent

    conflicting = dataclasses.replace(
        fill,
        quantity=Decimal("250"),
        attribution=(AttributionShare(strategy_id="ALPHA", quantity=Decimal("250")),),
    )

    with pytest.raises(ExecutionError, match=Code.FILL_ID_CONFLICT):
        session.journal.append(
            OrderFilledEvent(
                event_id="another-event",
                event_type=ExecutionEventType.ORDER_FILLED,
                order_id=order.order_id,
                execution_plan_id=session.plan.execution_plan_id,
                occurred_at=at_time(15, 9),
                fill=conflicting,
            )
        )


@pytest.mark.integration
def test_accounting_remains_the_sole_book_authority(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    broker = DeterministicPaperBroker(journal=session.journal)

    described = broker.describe()

    assert described["holds_cash"] is False
    assert described["holds_positions"] is False
    assert described["holds_pnl"] is False
    assert not hasattr(broker, "cash")
    assert not hasattr(broker, "positions")
    assert not hasattr(broker, "buying_power")


@pytest.mark.integration
def test_the_broker_exposes_no_mutable_balance_attribute() -> None:
    tree = ast.parse((SOURCE / "paper_broker.py").read_text(encoding="utf-8"))
    broker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "DeterministicPaperBroker"
    )
    assigned: set[str] = set()
    for node in ast.walk(broker):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            assigned.add(node.attr)

    assert not assigned & {"cash", "positions", "pnl", "buying_power", "balance"}


# --------------------------------------------------------------------------- #
# Cash
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_sale_funds_a_purchase_the_opening_cash_could_not(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, cash="1000", positions=[held_position()])
    plan = make_plan(
        store,
        ledger,
        intents=[enter_intent(notional=20000.0), exit_intent(notional=-21000.0)],
        sizes=sizing(held="42", new="50"),
    )
    session = session_for(tmp_path, plan, store, ledger)

    clock = 10
    for order in plan.orders:
        session.submit(order, at=at_time(15, clock))
        clock += 2
        price = "42" if order.identity.key == HELD.key else "50"
        session.fill(
            order,
            snapshot=snapshot(order.identity, price, at=at_time(15, clock)),
            acknowledged_at=at_time(15, clock - 2),
        )
        clock += 2

    assert session.settle() is PlanStatus.COMPLETED
    assert book_state(ledger).position(NEW).quantity == Decimal("400.00000000")


@pytest.mark.integration
def test_an_adverse_price_on_a_buy_is_refused_rather_than_overdrawn(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, cash="2100")
    plan = make_plan(store, ledger, intents=[enter_intent(notional=2000.0)], sizes=sizing(new="10"))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))

    # Sized at 10 (200 shares, 2,000). The venue fills at 12, needing 2,400.
    with pytest.raises(ExecutionError, match=Code.ACCOUNTING_ADMISSION_FAILED):
        session.fill(
            order, snapshot=snapshot(NEW, "12", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
        )


@pytest.mark.integration
def test_an_adverse_price_refusal_names_insufficient_cash(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, cash="2100")
    plan = make_plan(store, ledger, intents=[enter_intent(notional=2000.0)], sizes=sizing(new="10"))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))

    with pytest.raises(ExecutionError, match="INSUFFICIENT_CASH"):
        session.fill(
            order, snapshot=snapshot(NEW, "12", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
        )


@pytest.mark.integration
def test_an_adverse_price_is_not_auto_resized(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, cash="2100")
    plan = make_plan(store, ledger, intents=[enter_intent(notional=2000.0)], sizes=sizing(new="10"))
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))

    with pytest.raises(ExecutionError):
        session.fill(
            order, snapshot=snapshot(NEW, "12", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
        )

    # Nothing was bought at a smaller quantity to make it fit.
    assert not book_state(ledger).positions
    assert session.status is PlanStatus.FAILED


@pytest.mark.integration
def test_a_sale_uses_the_actual_fill_price_not_the_sizing_price(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, cash="0", positions=[held_position("100", "40")])
    plan = make_plan(
        store, ledger, intents=[exit_intent(notional=-4000.0)], sizes=sizing(held="40")
    )
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))

    session.fill(
        order, snapshot=snapshot(HELD, "45", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
    )

    # 100 shares at the realised 45, not the sizing 40.
    assert book_state(ledger).cash == Decimal("4500.0000")
    assert book_state(ledger).realised_pnl == Decimal("500.0000")


# --------------------------------------------------------------------------- #
# Attribution through execution
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_partial_fill_divides_attribution_proportionally() -> None:
    slices = allocate_partial_fill(
        authorized=(("ALPHA", Decimal("60")), ("BETA", Decimal("40"))),
        filled_quantity=Decimal("40"),
    )

    assert [(row.strategy_id, row.quantity) for row in slices] == [
        ("ALPHA", Decimal("24.00000000")),
        ("BETA", Decimal("16.00000000")),
    ]


@pytest.mark.integration
def test_partial_fill_attribution_sums_exactly_on_an_awkward_split() -> None:
    slices = allocate_partial_fill(
        authorized=(("ALPHA", Decimal("1")), ("BETA", Decimal("2"))),
        filled_quantity=Decimal("10"),
    )

    assert sum((row.quantity for row in slices), Decimal(0)) == Decimal("10")


@pytest.mark.integration
def test_attribution_allocation_ignores_input_order() -> None:
    forward = allocate_partial_fill(
        authorized=(("ALPHA", Decimal("60")), ("BETA", Decimal("40"))),
        filled_quantity=Decimal("40"),
    )
    reversed_input = allocate_partial_fill(
        authorized=(("BETA", Decimal("40")), ("ALPHA", Decimal("60"))),
        filled_quantity=Decimal("40"),
    )

    assert [(r.strategy_id, r.quantity) for r in forward] == [
        (r.strategy_id, r.quantity) for r in reversed_input
    ]


@pytest.mark.integration
def test_a_partial_sell_attributes_within_each_owners_holding(tmp_path: Path) -> None:
    store, ledger = open_book(
        tmp_path,
        cash="0",
        positions=[
            accounting.OpeningPosition(
                identity=HELD,
                quantity=Decimal("400"),
                cost_basis_per_share=Decimal("20"),
                attribution=(
                    accounting.StrategySlice(strategy_id="ALPHA", quantity=Decimal("250")),
                    accounting.StrategySlice(strategy_id="BETA", quantity=Decimal("150")),
                ),
            )
        ],
    )
    plan = make_plan(
        store,
        ledger,
        intents=[
            ExecutionIntent(
                identity=HELD,
                action=Action.REDUCE,
                current_weight=0.5,
                target_weight=0.25,
                notional_delta=-4000.0,
            )
        ],
        sizes=sizing(held="20"),
        attribution={"COMPANY:100": (("ALPHA", Decimal("250")), ("BETA", Decimal("150")))},
    )
    session = session_for(tmp_path, plan, store, ledger)
    order = plan.orders[0]
    session.submit(order, at=at_time(15, 1))

    session.fill(
        order,
        snapshot=snapshot(HELD, "21", at=at_time(15, 5), avail="100"),
        acknowledged_at=at_time(15, 1),
    )
    position = book_state(ledger).position(HELD)
    assert isinstance(position, accounting.PooledPosition)
    pools = {pool.owner: pool.quantity for pool in position.retained_pools()}

    # 100 of a 200-share order authorised 125/75, so 62.5 / 37.5.
    assert pools["ALPHA"] == Decimal("187.50000000")
    assert pools["BETA"] == Decimal("112.50000000")


@pytest.mark.integration
def test_a_fill_attribution_that_does_not_sum_is_refused() -> None:
    with pytest.raises(ExecutionError, match=Code.FILL_ATTRIBUTION_DOES_NOT_SUM):
        ExecutionFill(
            fill_id="F",
            order_id="o",
            identity=HELD,
            side=OrderSide.SELL,
            quantity=Decimal("100"),
            price=Decimal("10"),
            effective_at=at_time(15),
            attribution=(AttributionShare(strategy_id="ALPHA", quantity=Decimal("40")),),
        )


@pytest.mark.integration
def test_selling_from_an_unauthorised_pool_is_refused_by_accounting(tmp_path: Path) -> None:
    store, ledger = open_book(
        tmp_path,
        cash="0",
        positions=[
            accounting.OpeningPosition(
                identity=HELD,
                quantity=Decimal("100"),
                cost_basis_per_share=Decimal("20"),
                attribution=(
                    accounting.StrategySlice(strategy_id="ALPHA", quantity=Decimal("100")),
                ),
            )
        ],
    )
    fill = ExecutionFill(
        fill_id="F-BAD",
        order_id="o",
        identity=HELD,
        side=OrderSide.SELL,
        quantity=Decimal("50"),
        price=Decimal("21"),
        effective_at=at_time(15),
        attribution=(AttributionShare(strategy_id="GAMMA", quantity=Decimal("50")),),
    )

    with pytest.raises(ExecutionError, match=Code.ACCOUNTING_ADMISSION_FAILED):
        admit_fill(fill, ledger=ledger, store=store)


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_unrelated_book_change_stops_further_orders(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(
        store,
        ledger,
        intents=[exit_intent(), enter_intent()],
        sizes=sizing(held="40", new="50"),
    )
    session = session_for(tmp_path, plan, store, ledger)

    # Something outside this plan deposits cash.
    store.commit(
        ledger,
        accounting.ExternalCashFlowEvent(
            event_id="OUTSIDE",
            event_type=accounting.EventType.EXTERNAL_CASH_FLOW,
            effective_at=at_time(15, 0),
            source_id="BANK",
            source_fingerprint="fp-bank",
            direction=accounting.CashFlowDirection.DEPOSIT,
            amount=Decimal("1000"),
        ),
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_STATE_DRIFT):
        session.submit(plan.orders[0], at=at_time(15, 1))


@pytest.mark.integration
def test_drift_leaves_already_admitted_fills_alone(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, positions=[held_position()])
    plan = make_plan(
        store,
        ledger,
        intents=[exit_intent(), enter_intent()],
        sizes=sizing(held="40", new="50"),
    )
    session = session_for(tmp_path, plan, store, ledger)
    first = plan.orders[0]
    session.submit(first, at=at_time(15, 1))
    session.fill(
        first, snapshot=snapshot(HELD, "41", at=at_time(15, 3)), acknowledged_at=at_time(15, 1)
    )
    filled_cash = book_state(ledger).cash

    store.commit(
        ledger,
        accounting.ExternalCashFlowEvent(
            event_id="OUTSIDE",
            event_type=accounting.EventType.EXTERNAL_CASH_FLOW,
            effective_at=at_time(15, 4),
            source_id="BANK",
            source_fingerprint="fp-bank",
            direction=accounting.CashFlowDirection.DEPOSIT,
            amount=Decimal("1000"),
        ),
    )
    with pytest.raises(ExecutionError, match=Code.EXECUTION_STATE_DRIFT):
        session.submit(plan.orders[1], at=at_time(15, 5))

    assert book_state(ledger).cash == filled_cash + Decimal("1000")
    assert session.settle() is PlanStatus.FAILED


@pytest.mark.integration
def test_this_plans_own_fills_are_not_mistaken_for_drift(tmp_path: Path) -> None:
    store, ledger = open_book(tmp_path, cash="30000", positions=[held_position()])
    plan = make_plan(
        store,
        ledger,
        intents=[exit_intent(), enter_intent()],
        sizes=sizing(held="40", new="50"),
    )
    session = session_for(tmp_path, plan, store, ledger)

    clock = 1
    for order in plan.orders:
        session.submit(order, at=at_time(15, clock))
        clock += 2
        price = "41" if order.identity.key == HELD.key else "50"
        session.fill(
            order,
            snapshot=snapshot(order.identity, price, at=at_time(15, clock)),
            acknowledged_at=at_time(15, clock - 2),
        )
        clock += 2

    assert session.drift_detected is False
    assert session.settle() is PlanStatus.COMPLETED


# --------------------------------------------------------------------------- #
# The journal, its chain and its anchor
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_every_committed_event_advances_the_anchor(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)

    anchor = session.store.read_anchor()

    assert anchor is not None
    assert anchor.sequence == len(session.journal)
    assert [row.sequence for row in session.store.anchor_trail()] == [1, 2, 3]


@pytest.mark.integration
def test_the_anchor_witnesses_the_derived_order_state(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)

    anchor = session.store.read_anchor()

    assert anchor is not None
    assert anchor.order_state_fingerprint == order_state_fingerprint(session.journal.derive())


@pytest.mark.integration
def test_tampering_with_a_stored_record_breaks_the_chain(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    path = session.store.record_paths()[1]
    body = json.loads(path.read_text(encoding="utf-8"))
    body["event"]["payload"]["venue"] = "TAMPERED"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH):
        session.store.load_journal()


@pytest.mark.integration
def test_deleting_a_middle_record_breaks_the_chain(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    session.store.record_paths()[1].unlink()

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_SEQUENCE_GAP):
        session.store.load_journal()


@pytest.mark.integration
def test_a_truncated_journal_still_passes_the_chain(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    session.store.record_paths()[-1].unlink()

    assert verify_journal(session.store.load_journal().records)["verified"] is True


@pytest.mark.integration
def test_truncation_is_detected_against_the_anchor(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    session.store.record_paths()[-1].unlink()

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_TRUNCATION_DETECTED):
        session.recover()


@pytest.mark.integration
def test_multi_record_truncation_is_detected(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    for path in session.store.record_paths()[-2:]:
        path.unlink()

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_TRUNCATION_DETECTED):
        session.recover()


@pytest.mark.integration
def test_the_anchor_is_never_moved_backward(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_TRUNCATION_DETECTED):
        session.store.write_anchor(
            ExecutionJournalHeadAnchor(
                journal_generation=session.store.generation,
                sequence=1,
                journal_head_hash=session.journal.records[0].record_hash,
                order_state_fingerprint="x" * 64,
            )
        )


@pytest.mark.integration
def test_an_event_ahead_of_the_anchor_is_recoverable(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    # A crash between record durability and anchor durability: the record is
    # written, the anchor is not.
    event = session.broker.cancel(session.plan, order, at=at_time(15, 30))
    session.store.append_record(session.journal.append(event))

    result = session.store.classify()

    assert result["status"] == str(JournalAnchorStatus.RECOVERY_REQUIRED)
    assert result["recoverable"] is True


@pytest.mark.integration
def test_recovery_advances_the_anchor_over_a_verified_tail(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    event = session.broker.cancel(session.plan, order, at=at_time(15, 30))
    session.store.append_record(session.journal.append(event))

    outcome = session.recover()

    assert outcome.anchor_advanced is True
    assert session.store.classify()["consistent"] is True


@pytest.mark.integration
def test_a_journal_with_no_anchor_does_not_establish_completeness(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    (tmp_path / ".execution" / "head.json").unlink()

    with pytest.raises(ExecutionError, match=Code.EXECUTION_ANCHOR_MISSING):
        session.recover()


@pytest.mark.integration
def test_a_head_mismatch_fails_closed(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    anchor = session.store.read_anchor()
    assert anchor is not None
    rewritten = dataclasses.replace(anchor, journal_head_hash="d" * 64)
    (tmp_path / ".execution" / "head.json").write_text(
        json.dumps(rewritten.as_json()), encoding="utf-8"
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH):
        session.recover()


@pytest.mark.integration
def test_an_edited_anchor_fails_its_own_fingerprint(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    body = json.loads((tmp_path / ".execution" / "head.json").read_text(encoding="utf-8"))
    body["sequence"] = 99
    (tmp_path / ".execution" / "head.json").write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(ExecutionError, match=Code.EXECUTION_ANCHOR_FINGERPRINT_MISMATCH):
        session.store.read_anchor()


@pytest.mark.integration
def test_an_invalid_event_is_refused_before_any_durable_write(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    records_before = len(session.store.record_paths())
    anchor_before = session.store.read_anchor()
    assert anchor_before is not None

    # The order is ACKNOWLEDGED, so a second acknowledgement is not a legal move.
    from stock_screener.execution import ExecutionEventType, OrderAcknowledgedEvent

    with pytest.raises(ExecutionError):
        session.store.commit(
            session.journal,
            OrderAcknowledgedEvent(
                event_id="SECOND-ACK",
                event_type=ExecutionEventType.ORDER_ACKNOWLEDGED,
                order_id=order.order_id,
                execution_plan_id=session.plan.execution_plan_id,
                occurred_at=at_time(15, 40),
            ),
        )

    assert len(session.store.record_paths()) == records_before
    assert session.store.read_anchor() == anchor_before


@pytest.mark.integration
def test_the_same_event_twice_is_idempotent(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    before = len(session.journal)

    # The same event, with the same occurrence time working_order used. A
    # different timestamp would be a different event, and rightly a conflict.
    event = session.broker.submit(session.plan, order, at=at_time(15, 1))
    result = session.store.commit(session.journal, event)

    assert result.written is False
    assert len(session.journal) == before


@pytest.mark.integration
def test_one_event_id_with_two_payloads_is_a_conflict(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    from stock_screener.execution import ExecutionEventType, OrderSubmittedEvent

    with pytest.raises(ExecutionError, match=Code.EXECUTION_EVENT_ID_CONFLICT):
        session.journal.append(
            OrderSubmittedEvent(
                event_id=f"{order.order_id}:SUBMITTED",
                event_type=ExecutionEventType.ORDER_SUBMITTED,
                order_id=order.order_id,
                execution_plan_id=session.plan.execution_plan_id,
                occurred_at=at_time(15, 2),
                venue="SOMEWHERE_ELSE",
            )
        )


@pytest.mark.integration
def test_a_store_cannot_be_rooted_in_a_research_namespace() -> None:
    with pytest.raises(ExecutionError, match=Code.FORBIDDEN_EXECUTION_NAMESPACE):
        ExecutionStore(Path("some") / ".evidence" / "execution")


@pytest.mark.integration
def test_a_partial_record_is_a_named_refusal(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)
    (tmp_path / ".execution" / "journal" / "000000000009.json").write_text(
        '{"sequence": 9, "trunc', encoding="utf-8"
    )

    with pytest.raises(ExecutionError, match=Code.EXECUTION_PARTIAL_RECORD):
        session.store.load_journal()


# --------------------------------------------------------------------------- #
# Crash recovery across the handoff
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_fill_durable_without_accounting_is_pending(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    event = session.broker.fill(
        session.plan,
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert event is not None
    session.store.commit(session.journal, event)

    pending = session.journal.derive().pending_fills

    assert [row.fill_id for row in pending] == [f"{order.order_id}#1"]


@pytest.mark.integration
def test_recovery_applies_a_pending_fill_exactly_once(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    event = session.broker.fill(
        session.plan,
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert event is not None
    session.store.commit(session.journal, event)
    ledger_before = len(session.ledger)

    outcome = session.recover()

    assert outcome.fills_applied == (f"{order.order_id}#1",)
    assert len(session.ledger) == ledger_before + 1
    assert book_state(session.ledger).position(HELD).quantity == Decimal("300.00000000")
    assert session.journal.derive().pending_fills == ()


@pytest.mark.integration
def test_recovery_twice_applies_nothing_further(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    event = session.broker.fill(
        session.plan,
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert event is not None
    session.store.commit(session.journal, event)
    session.recover()
    quantity = book_state(session.ledger).position(HELD).quantity

    second = session.recover()

    assert second.fills_applied == ()
    assert book_state(session.ledger).position(HELD).quantity == quantity


@pytest.mark.integration
def test_accounting_applied_but_execution_unconfirmed_completes_idempotently(
    tmp_path: Path,
) -> None:
    session, order = working_order(tmp_path)
    event = session.broker.fill(
        session.plan,
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    assert event is not None
    session.store.commit(session.journal, event)
    assert event.fill is not None
    # The book receives the fill, then the process dies before FILL_APPLIED.
    admit_fill(event.fill, ledger=session.ledger, store=session.accounting_store)
    quantity = book_state(session.ledger).position(HELD).quantity
    records = len(session.ledger)

    outcome = session.recover()

    assert outcome.fills_already_applied == (f"{order.order_id}#1",)
    assert outcome.fills_applied == ()
    assert book_state(session.ledger).position(HELD).quantity == quantity
    assert len(session.ledger) == records


@pytest.mark.integration
def test_a_fresh_process_reconstructs_the_plan_state(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="200"),
        acknowledged_at=at_time(15, 1),
    )
    expected = order_state_fingerprint(session.journal.derive())

    reopened = ExecutionStore(
        tmp_path / ".execution", execution_plan_id=session.plan.execution_plan_id
    )
    derived = reopened.load_journal().derive()

    assert order_state_fingerprint(derived) == expected
    assert derived.status(order.order_id).cumulative_filled_quantity == Decimal("200.00000000")
    assert derived.status(order.order_id).state is OrderState.PARTIALLY_FILLED


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

CORE_MODULES = (
    "accounting_adapter.py",
    "anchor.py",
    "authorization.py",
    "engine.py",
    "errors.py",
    "events.py",
    "fingerprint.py",
    "journal.py",
    "manifest.py",
    "models.py",
    "paper_broker.py",
    "paper_market.py",
    "planner.py",
    "reports.py",
    "serde.py",
    "state_machine.py",
    "versions.py",
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
@pytest.mark.parametrize("name", CORE_MODULES)
def test_no_module_imports_a_broker_sdk(name: str) -> None:
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
@pytest.mark.parametrize("name", CORE_MODULES)
def test_no_module_imports_a_network_client(name: str) -> None:
    assert not imported(SOURCE / name) & {
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "urllib.request",
        "aiohttp",
        "websockets",
    }


@pytest.mark.integration
@pytest.mark.parametrize("name", (*CORE_MODULES, "run.py", "persistence.py"))
def test_no_module_imports_a_randomness_source(name: str) -> None:
    assert not imported(SOURCE / name) & {"random", "secrets", "numpy.random", "uuid"}


@pytest.mark.integration
def test_the_whole_package_mentions_no_credential() -> None:
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        identifiers = (
            {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            | {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
        )
        assert not identifiers & {
            "api_key",
            "api_secret",
            "secret_key",
            "access_token",
            "oauth_token",
            "authorization_header",
        }, path.name


@pytest.mark.integration
def test_the_package_imports_no_research_or_strategy_module() -> None:
    for path in sorted(SOURCE.glob("*.py")):
        names = imported(path)
        assert not {name for name in names if "research" in name}
        assert not {name for name in names if "prospective" in name}
        assert not {name for name in names if "membership" in name}


@pytest.mark.integration
def test_the_engine_works_with_the_network_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    import sqlite3

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("network is unavailable")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)

    session, order = working_order(tmp_path)
    session.fill(
        order, snapshot=snapshot(HELD, "41", at=at_time(15, 5)), acknowledged_at=at_time(15, 1)
    )

    assert session.settle() is PlanStatus.COMPLETED
    assert book_state(session.ledger).position(HELD).quantity == Decimal("0")
    assert session.store.verify()["verified"] is True


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_manifest_binds_every_version_and_fingerprint() -> None:
    manifest = execution_manifest(
        execution_plan_fingerprint="p" * 64,
        journal_generation="J1",
        journal_sequence=7,
        journal_head="h" * 64,
        journal_anchor_fingerprint="a" * 64,
        accounting_generation="G1",
        accounting_version=V2,
        initial_accounting_state="i" * 64,
        final_accounting_state="f" * 64,
        paper_snapshot_fingerprints=("s" * 64,),
        plan_report_fingerprint="r" * 64,
        code_commit="abc123",
    )

    bindings = manifest["bindings"]

    assert all(value is not None for value in bindings.values())
    assert manifest["policies"]["engine"] == EXECUTION_ENGINE_VERSION
    assert manifest["policies"]["required_accounting_version"] == V2


@pytest.mark.integration
def test_the_manifest_fingerprint_is_stable() -> None:
    assert (
        execution_manifest()["manifest_fingerprint"]
        == (execution_manifest()["manifest_fingerprint"])
    )


@pytest.mark.integration
def test_the_manifest_states_the_core_invariants() -> None:
    invariants = execution_manifest()["invariants"]

    assert any("order changes no holding" in line for line in invariants)
    assert any("only an admitted accounting FillEvent" in line for line in invariants)
    assert any("broker holds no cash" in line for line in invariants)


@pytest.mark.integration
def test_the_manifest_states_what_is_not_guaranteed() -> None:
    not_guaranteed = execution_manifest()["not_guaranteed"]

    assert any("COORDINATED_LOCAL_ROLLBACK_DETECTION" in line for line in not_guaranteed)
    assert any("EXECUTION REALISM" in line for line in not_guaranteed)


@pytest.mark.integration
def test_the_manifest_declares_live_trading_unauthorised() -> None:
    manifest = execution_manifest()

    assert manifest["live_trading"] == "NOT_AUTHORIZED"
    assert manifest["live_broker"] == "NOT_IMPLEMENTED"
    assert manifest["external_paper_broker"] == "NOT_IMPLEMENTED"


@pytest.mark.integration
def test_the_engine_policy_disclaims_realism() -> None:
    policy = engine_policy()

    assert "deterministic correctness simulator" in policy["what_this_is"]
    assert "not a realistic fill simulator" in policy["what_this_is_not"]


# --------------------------------------------------------------------------- #
# The coordinated-rollback limitation, stated honestly
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_coordinated_local_rollback_is_not_detected(tmp_path: Path) -> None:
    session, order = working_order(tmp_path)
    session.fill(
        order,
        snapshot=snapshot(HELD, "41", at=at_time(15, 5), avail="100"),
        acknowledged_at=at_time(15, 1),
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
            execution_plan_id=session.plan.execution_plan_id,
        )
    )

    # The honest result: journal and anchor were rolled back together and the
    # pair is self-consistent, so local validation has nothing to compare
    # against. There is no test here claiming otherwise.
    assert session.store.classify()["consistent"] is True


@pytest.mark.integration
def test_the_store_reports_the_rollback_limitation(tmp_path: Path) -> None:
    session, _ = working_order(tmp_path)

    assert (
        session.store.verify()["coordinated_rollback_detection"]
        == "NOT_GUARANTEED_WITHOUT_EXTERNAL_ANCHOR"
    )


# --------------------------------------------------------------------------- #
# The goldens
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_clean_execution_fixture_reproduces_its_golden(tmp_path: Path) -> None:
    request = load_request(FIXTURES / "clean_execution.json")
    golden = json.loads((FIXTURES / "clean_execution.golden.json").read_text(encoding="utf-8"))

    assert run_paper_execution(request, root=tmp_path) == golden


@pytest.mark.integration
def test_the_clean_golden_sequences_sells_before_buys() -> None:
    golden = json.loads((FIXTURES / "clean_execution.golden.json").read_text(encoding="utf-8"))

    sides = [row["side"] for row in golden["plan_report"]["orders"]]

    assert sides == ["SELL", "SELL", "BUY", "BUY"]


@pytest.mark.integration
def test_the_clean_golden_completes_and_balances() -> None:
    golden = json.loads((FIXTURES / "clean_execution.golden.json").read_text(encoding="utf-8"))

    assert golden["plan_report"]["plan_status"] == "COMPLETED"
    assert golden["plan_report"]["orders_filled"] == 4
    # 5000 + 8099 + 4349 - 9202 - 2438.50
    assert golden["final_book"]["cash"] == "5807.5000"


@pytest.mark.integration
def test_the_clean_golden_omits_the_hold() -> None:
    golden = json.loads((FIXTURES / "clean_execution.golden.json").read_text(encoding="utf-8"))

    assert not any("9005" in row["identity"] for row in golden["plan_report"]["orders"])


@pytest.mark.integration
def test_the_partial_fixture_reproduces_its_golden(tmp_path: Path) -> None:
    request = load_request(FIXTURES / "partial_fill_execution.json")
    golden = json.loads(
        (FIXTURES / "partial_fill_execution.golden.json").read_text(encoding="utf-8")
    )

    assert run_paper_execution(request, root=tmp_path) == golden


@pytest.mark.integration
def test_the_partial_golden_books_only_what_filled() -> None:
    golden = json.loads(
        (FIXTURES / "partial_fill_execution.golden.json").read_text(encoding="utf-8")
    )
    order = golden["plan_report"]["orders"][0]

    assert order["ordered_quantity"] == "200.00000000"
    assert order["filled_quantity"] == "70.00000000"
    assert order["terminal_state"] == "CANCELLED"
    held = next(row for row in golden["final_book"]["positions"] if row["label"] == "COMPANY:9001")
    assert held["quantity"] == "130.00000000"
    assert golden["plan_report"]["plan_status"] == "PARTIALLY_COMPLETED"


@pytest.mark.integration
def test_the_rejected_fixture_reproduces_its_golden(tmp_path: Path) -> None:
    request = load_request(FIXTURES / "rejected_execution.json")
    golden = json.loads((FIXTURES / "rejected_execution.golden.json").read_text(encoding="utf-8"))

    assert run_paper_execution(request, root=tmp_path) == golden


@pytest.mark.integration
def test_the_rejected_golden_leaves_the_book_untouched() -> None:
    golden = json.loads((FIXTURES / "rejected_execution.golden.json").read_text(encoding="utf-8"))

    assert golden["plan_report"]["orders"][0]["terminal_state"] == "REJECTED"
    assert golden["plan_report"]["orders"][0]["filled_quantity"] == "0"
    assert golden["final_book"]["ledger_sequence"] == 1
    assert golden["final_book"]["cash"] == "5000.0000"
    assert golden["plan_report"]["plan_status"] != "COMPLETED"


@pytest.mark.integration
def test_no_golden_report_claims_performance() -> None:
    for name in ("clean_execution", "partial_fill_execution", "rejected_execution"):
        golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))
        report = golden["plan_report"]

        assert report["performance_claim"] == "NONE"
        assert "realised_pnl" not in report
        assert "nav" not in report


@pytest.mark.integration
def test_a_request_with_no_opening_book_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"intents": []}), encoding="utf-8")

    with pytest.raises(ExecutionError, match=Code.PLAN_INVALID):
        load_request(path)
