"""P1.4 — per-attribution cost pools and mandatory ledger head anchors.

Two P1.3 limitations are closed here, and the tests are organised around proving
exactly that rather than around the modules involved.

The first is that a global weighted-average basis made per-strategy realised P&L
misleading when co-owners entered at different prices — and, less obviously, made
the portfolio's own realised P&L and cost basis wrong in the same case. The tests
for that assert figures computed by hand, because an invariant that only compares
the engine against itself cannot catch a basis convention that is uniformly wrong.

The second is that truncation of the committed tail was only detectable when
somebody had remembered to write a checkpoint. The tests for that delete records
and require a named refusal, and one of them deliberately proves the chain still
verifies afterwards — which is why the anchor has to exist.

One limitation is asserted as *not* closed: a ledger and its local anchor rolled
back together validate cleanly. There is no test claiming otherwise.
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stock_screener.portfolio_accounting import (
    ACCOUNTING_V1,
    ACCOUNTING_V2,
    ANCHOR_ERROR_CODES,
    CURRENT_ACCOUNTING_VERSION,
    DEFAULT_ACCOUNTING_VERSION,
    MIGRATION_MODE,
    PER_ATTRIBUTION_COST_POLICY,
    V1_ERROR_CODES,
    V2_ERROR_CODES,
    AccountingError,
    AccountingVersion,
    AnchorStatus,
    AttributionCostPool,
    CashFlowDirection,
    Code,
    EventType,
    ExternalCashFlowEvent,
    FillEvent,
    InMemoryExternalAnchor,
    LedgerHeadAnchor,
    MarkPrice,
    MarkPriceSnapshot,
    OpeningBalanceEvent,
    OpeningPosition,
    PooledPosition,
    PortfolioLedger,
    PortfolioState,
    PortfolioStore,
    Side,
    SplitEvent,
    StrategySlice,
    accounting_manifest,
    attribute_portfolio,
    classify_anchor,
    compare_external_anchor,
    evaluate_request,
    event_from_json,
    load_ledger,
    load_request,
    migrate_from_events,
    refuse_snapshot_migration,
    replay,
    resolve_version,
    state_fingerprint,
    value_portfolio,
    verify_chain,
)
from stock_screener.portfolio_risk import SecurityIdentity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stock_screener.portfolio_accounting import PortfolioEvent

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "portfolio_accounting"
ACCOUNTING_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "stock_screener" / "portfolio_accounting"
)

X = SecurityIdentity(kind="COMPANY", identity_id=1, symbol="X")
Y = SecurityIdentity(kind="COMPANY", identity_id=2, symbol="Y")
T0 = "2026-10-01T09:30:00-04:00"
MARK_AT = "2026-10-02T16:00:00-04:00"


def opening(
    *, cash: str = "100000", positions: Sequence[OpeningPosition] = ()
) -> OpeningBalanceEvent:
    """An opening balance event."""
    return OpeningBalanceEvent(
        event_id="OB",
        event_type=EventType.OPENING_BALANCE,
        effective_at=T0,
        source_id="IMPORT",
        source_fingerprint="fp-ob",
        cash=Decimal(cash),
        positions=tuple(positions),
    )


def fill(
    *,
    event_id: str,
    identity: SecurityIdentity = X,
    side: Side = Side.BUY,
    quantity: str,
    price: str,
    fees: str = "0",
    at: str = "2026-10-01T10:00:00-04:00",
    owners: Sequence[tuple[str, str]] = (),
) -> FillEvent:
    """A fill event attributed to the given owners."""
    return FillEvent(
        event_id=event_id,
        event_type=EventType.FILL,
        effective_at=at,
        source_id="EXEC",
        source_fingerprint=f"fp-{event_id}",
        identity=identity,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal(fees),
        attribution=tuple(
            StrategySlice(strategy_id=who, quantity=Decimal(qty)) for who, qty in owners
        ),
    )


def pooled(state: PortfolioState, identity: SecurityIdentity = X) -> PooledPosition:
    """The security's holding, narrowed to a pooled one.

    `PortfolioState.position` returns either version's holding, because
    everything downstream of the state machine reads a portfolio without
    branching on the version. A V2 test wants the pools, so the narrowing is
    asserted here once instead of at every call site.
    """
    position = state.position(identity)
    assert isinstance(position, PooledPosition)
    return position


def anchor_of(store: PortfolioStore) -> LedgerHeadAnchor:
    """The store's head anchor, asserted present.

    `read_anchor` returns None for a store that has never been written, which is
    a real state the production code has to handle. In a test that has just
    committed events it is a failure, so it is narrowed here once rather than at
    every call site.
    """
    anchor = store.read_anchor()
    assert anchor is not None
    return anchor


def ledger_of(*events: PortfolioEvent) -> PortfolioLedger:
    """A ledger holding these events."""
    ledger = PortfolioLedger()
    for event in events:
        ledger.append(event)
    return ledger


def differing_entry_ledger() -> PortfolioLedger:
    """The §2 scenario: A in at 10, B in at 20."""
    return ledger_of(
        opening(),
        fill(event_id="A1", quantity="100", price="10", owners=[("A", "100")]),
        fill(
            event_id="B1",
            quantity="100",
            price="20",
            at="2026-10-01T10:05:00-04:00",
            owners=[("B", "100")],
        ),
    )


def v2(ledger: PortfolioLedger) -> PortfolioState:
    """Replay under per-attribution cost pools."""
    return replay(ledger, accounting_version=ACCOUNTING_V2)


def marks(*pairs: tuple[SecurityIdentity, str], at: str = MARK_AT) -> MarkPriceSnapshot:
    """A complete mark snapshot."""
    return MarkPriceSnapshot(
        valuation_time=at,
        marks=tuple(
            MarkPrice(identity=identity, price=Decimal(price), as_of=at)
            for identity, price in pairs
        ),
    )


# --------------------------------------------------------------------------- #
# The version boundary
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_v1_remains_the_default_so_an_existing_ledger_keeps_its_meaning() -> None:
    assert DEFAULT_ACCOUNTING_VERSION == ACCOUNTING_V1
    assert PortfolioState().accounting_version == ACCOUNTING_V1


@pytest.mark.integration
def test_v2_is_what_a_new_ledger_should_use() -> None:
    assert CURRENT_ACCOUNTING_VERSION == ACCOUNTING_V2


@pytest.mark.integration
def test_an_unknown_accounting_version_is_refused_rather_than_approximated() -> None:
    with pytest.raises(AccountingError, match=Code.ACCOUNTING_VERSION_UNKNOWN):
        resolve_version("PORTFOLIO_ACCOUNTING_V99")


@pytest.mark.integration
def test_the_version_enum_covers_exactly_the_two_implemented_versions() -> None:
    assert {member.value for member in AccountingVersion} == {ACCOUNTING_V1, ACCOUNTING_V2}


@pytest.mark.integration
def test_only_v2_keeps_a_basis_per_attribution_owner() -> None:
    assert resolve_version(ACCOUNTING_V2).uses_cost_pools
    assert not resolve_version(ACCOUNTING_V1).uses_cost_pools


@pytest.mark.integration
def test_the_two_versions_name_different_basis_conventions() -> None:
    assert resolve_version(ACCOUNTING_V1).basis_policy == "WEIGHTED_AVERAGE_COST_V1"
    assert resolve_version(ACCOUNTING_V2).basis_policy == PER_ATTRIBUTION_COST_POLICY


# --------------------------------------------------------------------------- #
# The defect P1.4 exists to fix
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_v1_understated_the_selling_strategys_realised_result() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )

    assert replay(ledger).realised_pnl == Decimal("1500.0000")
    assert v2(ledger).realised_pnl == Decimal("2000.0000")


@pytest.mark.integration
def test_the_new_basis_moves_global_figures_which_is_why_it_is_a_major_version() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )

    assert replay(ledger).total_cost_basis != v2(ledger).total_cost_basis
    assert v2(ledger).total_cost_basis == Decimal("2000.0000")


@pytest.mark.integration
def test_v2_agrees_with_v1_whenever_co_owners_entered_at_the_same_price() -> None:
    """V2 is a strict refinement: it differs from V1 only where V1 was wrong.

    This is what makes the version bump defensible. If V2 moved figures in cases
    V1 had right, it would be a different accounting policy rather than a
    correction of a known defect, and no ledger could be migrated with
    confidence.
    """
    ledger = ledger_of(
        opening(),
        fill(event_id="A1", quantity="100", price="15", fees="1", owners=[("A", "100")]),
        fill(
            event_id="B1",
            quantity="100",
            price="15",
            fees="1",
            at="2026-10-01T10:05:00-04:00",
            owners=[("B", "100")],
        ),
    )
    ledger.append(
        fill(
            event_id="S1",
            side=Side.SELL,
            quantity="60",
            price="22",
            fees="2",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "60")],
        )
    )
    under_v1 = replay(ledger)
    under_v2 = v2(ledger)

    assert under_v1.realised_pnl == under_v2.realised_pnl
    assert under_v1.total_cost_basis == under_v2.total_cost_basis
    assert under_v1.cash == under_v2.cash
    assert under_v1.fees_paid == under_v2.fees_paid


@pytest.mark.integration
def test_the_basis_convention_never_moves_cash() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="30",
            fees="3",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )

    assert replay(ledger).cash == v2(ledger).cash


# --------------------------------------------------------------------------- #
# Per-owner pools
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_each_owner_keeps_the_basis_it_actually_paid() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    assert position.pool_for("A").average_cost == Decimal("10.00000000")
    assert position.pool_for("B").average_cost == Decimal("20.00000000")


@pytest.mark.integration
def test_the_global_average_is_derived_from_the_pools() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    assert position.quantity == Decimal("200.00000000")
    assert position.total_cost == Decimal("3000.0000")
    assert position.average_cost == Decimal("15.00000000")


@pytest.mark.integration
def test_a_sale_from_one_pool_leaves_the_other_untouched() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="50",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "50")],
        )
    )

    position = pooled(v2(ledger), X)

    assert position.pool_for("B").quantity == Decimal("100.00000000")
    assert position.pool_for("B").total_cost_basis == Decimal("2000.0000")
    assert position.pool_for("B").realised_trading_pnl_gross == Decimal("0")


@pytest.mark.integration
def test_a_mixed_sale_realises_each_owners_result_from_its_own_basis() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="MIX",
            side=Side.SELL,
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "60"), ("B", "40")],
        )
    )

    position = pooled(v2(ledger), X)

    assert position.pool_for("A").realised_trading_pnl_gross == Decimal("1200.0000")
    assert position.pool_for("B").realised_trading_pnl_gross == Decimal("400.0000")
    assert position.realised_pnl == Decimal("1600.0000")


@pytest.mark.integration
def test_the_same_fill_price_enters_each_pool_against_its_own_prior_basis() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="BOTH",
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "50"), ("B", "50")],
        )
    )

    position = pooled(v2(ledger), X)

    # A: (100*10 + 50*30)/150;  B: (100*20 + 50*30)/150
    assert position.pool_for("A").total_cost_basis == Decimal("2500.0000")
    assert position.pool_for("B").total_cost_basis == Decimal("3500.0000")


@pytest.mark.integration
def test_an_imported_position_seeds_each_named_owner_at_the_stated_basis() -> None:
    ledger = ledger_of(
        opening(
            positions=[
                OpeningPosition(
                    identity=X,
                    quantity=Decimal("200"),
                    cost_basis_per_share=Decimal("15"),
                    attribution=(
                        StrategySlice(strategy_id="A", quantity=Decimal("100")),
                        StrategySlice(strategy_id="B", quantity=Decimal("100")),
                    ),
                )
            ]
        )
    )

    position = pooled(v2(ledger), X)

    assert position.pool_for("A").average_cost == Decimal("15.00000000")
    assert position.pool_for("B").average_cost == Decimal("15.00000000")


@pytest.mark.integration
def test_a_legacy_holding_keeps_its_own_unknown_owner_pool() -> None:
    ledger = ledger_of(
        opening(
            positions=[
                OpeningPosition(
                    identity=X, quantity=Decimal("50"), cost_basis_per_share=Decimal("8")
                )
            ]
        )
    )

    position = pooled(v2(ledger), X)

    assert position.owners == ("UNATTRIBUTED_EXISTING",)
    assert position.pool_for("UNATTRIBUTED_EXISTING").total_cost_basis == Decimal("400.0000")


@pytest.mark.integration
def test_a_sale_may_reduce_the_unknown_owner_pool_at_its_own_basis() -> None:
    ledger = ledger_of(
        opening(
            positions=[
                OpeningPosition(
                    identity=X, quantity=Decimal("50"), cost_basis_per_share=Decimal("8")
                )
            ]
        ),
        fill(
            event_id="S",
            side=Side.SELL,
            quantity="20",
            price="12",
            owners=[("UNATTRIBUTED_EXISTING", "20")],
        ),
    )

    position = pooled(v2(ledger), X)

    assert position.pool_for("UNATTRIBUTED_EXISTING").realised_trading_pnl_gross == Decimal(
        "80.0000"
    )


# --------------------------------------------------------------------------- #
# Attribution refusals
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_unattributed_sale_on_a_co_owned_position_is_refused() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    with pytest.raises(AccountingError, match=Code.SELL_ATTRIBUTION_REQUIRED):
        position.sell(quantity=Decimal("10"), price=Decimal("30"), fees=Decimal("0"))


@pytest.mark.integration
def test_no_pro_rata_or_first_owner_assumption_is_made() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    try:
        position.sell(quantity=Decimal("10"), price=Decimal("30"), fees=Decimal("0"))
    except AccountingError as exc:
        assert "pro-rata or" in exc.detail
        assert "first-owner" in exc.detail


@pytest.mark.integration
def test_one_owner_cannot_sell_more_than_its_own_pool_even_when_the_security_has_enough() -> None:
    ledger = ledger_of(
        opening(),
        fill(event_id="A1", quantity="10", price="5", owners=[("A", "10")]),
        fill(
            event_id="B1",
            quantity="90",
            price="5",
            at="2026-10-01T10:05:00-04:00",
            owners=[("B", "90")],
        ),
    )
    position = pooled(v2(ledger), X)

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_ATTRIBUTED_POSITION):
        position.sell(
            quantity=Decimal("20"),
            price=Decimal("30"),
            fees=Decimal("0"),
            attribution=(StrategySlice(strategy_id="A", quantity=Decimal("20")),),
        )


@pytest.mark.integration
def test_the_attributed_oversell_refusal_says_the_global_quantity_was_sufficient() -> None:
    ledger = ledger_of(
        opening(),
        fill(event_id="A1", quantity="10", price="5", owners=[("A", "10")]),
        fill(
            event_id="B1",
            quantity="90",
            price="5",
            at="2026-10-01T10:05:00-04:00",
            owners=[("B", "90")],
        ),
    )
    position = pooled(v2(ledger), X)

    with pytest.raises(AccountingError, match="global quantity may be"):
        position.sell(
            quantity=Decimal("20"),
            price=Decimal("30"),
            fees=Decimal("0"),
            attribution=(StrategySlice(strategy_id="A", quantity=Decimal("20")),),
        )


@pytest.mark.integration
def test_a_sale_beyond_the_whole_security_is_still_insufficient_position() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_POSITION):
        position.sell(
            quantity=Decimal("500"),
            price=Decimal("30"),
            fees=Decimal("0"),
            attribution=(StrategySlice(strategy_id="A", quantity=Decimal("500")),),
        )


@pytest.mark.integration
def test_a_sale_whose_attribution_does_not_sum_is_refused() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_DOES_NOT_SUM):
        position.sell(
            quantity=Decimal("100"),
            price=Decimal("30"),
            fees=Decimal("0"),
            attribution=(StrategySlice(strategy_id="A", quantity=Decimal("40")),),
        )


@pytest.mark.integration
def test_a_pool_may_not_go_negative() -> None:
    with pytest.raises(AccountingError, match=Code.SHORT_POSITION_NOT_SUPPORTED):
        AttributionCostPool(identity=X, owner="A", quantity=Decimal("-1"))


@pytest.mark.integration
def test_a_pool_may_not_carry_a_negative_basis() -> None:
    with pytest.raises(AccountingError, match="may not be negative"):
        AttributionCostPool(
            identity=X, owner="A", quantity=Decimal("1"), total_cost_basis=Decimal("-1")
        )


@pytest.mark.integration
def test_a_pool_must_name_its_owner() -> None:
    with pytest.raises(AccountingError, match="must name its owner"):
        AttributionCostPool(identity=X, owner="")


@pytest.mark.integration
def test_selling_a_security_no_owner_holds_is_refused() -> None:
    position = PooledPosition(identity=X)

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_POSITION):
        position.sell(quantity=Decimal("0"), price=Decimal("30"), fees=Decimal("0"))


@pytest.mark.integration
def test_a_sale_allocation_reports_the_quantity_debited_from_each_owner() -> None:
    ledger = differing_entry_ledger()
    position = pooled(v2(ledger), X)

    _, allocation = position.sell(
        quantity=Decimal("100"),
        price=Decimal("30"),
        fees=Decimal("2"),
        attribution=(
            StrategySlice(strategy_id="A", quantity=Decimal("60")),
            StrategySlice(strategy_id="B", quantity=Decimal("40")),
        ),
    )

    assert [(row.strategy_id, row.quantity) for row in allocation.per_strategy] == [
        ("A", Decimal("60.00000000")),
        ("B", Decimal("40.00000000")),
    ]
    assert allocation.realised_pnl == allocation.realised_trading_pnl_gross
    assert [row["strategy_id"] for row in allocation.realised_per_strategy] == ["A", "B"]


@pytest.mark.integration
def test_a_sale_allocations_per_owner_realised_sums_to_its_total() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    _, allocation = position.sell(
        quantity=Decimal("100"),
        price=Decimal("30"),
        fees=Decimal("2"),
        attribution=(
            StrategySlice(strategy_id="A", quantity=Decimal("60")),
            StrategySlice(strategy_id="B", quantity=Decimal("40")),
        ),
    )

    assert (
        sum(
            (Decimal(row["realised_pnl"]) for row in allocation.realised_per_strategy),
            Decimal(0),
        )
        == allocation.realised_trading_pnl_gross
    )


@pytest.mark.integration
def test_a_zero_fee_apportions_to_zero_for_every_owner() -> None:
    ledger = ledger_of(
        opening(),
        fill(
            event_id="BOTH",
            quantity="100",
            price="10",
            fees="0",
            owners=[("A", "60"), ("B", "40")],
        ),
    )

    position = pooled(v2(ledger), X)

    assert position.pool_for("A").fees_paid == Decimal("0")
    assert position.pool_for("B").fees_paid == Decimal("0")


@pytest.mark.integration
def test_a_fee_is_apportioned_across_owners_and_loses_nothing() -> None:
    ledger = ledger_of(
        opening(),
        fill(
            event_id="BOTH",
            quantity="3",
            price="10",
            fees="1",
            owners=[("A", "1"), ("B", "1"), ("C", "1")],
        ),
    )

    position = pooled(v2(ledger), X)

    assert position.fees_paid == Decimal("1.0000")
    assert sum((pool.fees_paid for pool in position.pools.values()), Decimal(0)) == Decimal(
        "1.0000"
    )


@pytest.mark.integration
def test_a_dividend_attribution_with_no_quantity_is_refused() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_DOES_NOT_SUM):
        position.credit_dividend(
            Decimal("100"),
            attribution=(StrategySlice(strategy_id="A", quantity=Decimal("0")),),
        )


# --------------------------------------------------------------------------- #
# Closed pools keep their owner
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_fully_exited_pool_keeps_its_realised_result() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )

    pool = pooled(v2(ledger), X).pool_for("A")

    assert pool.quantity == Decimal("0")
    assert pool.total_cost_basis == Decimal("0")
    assert pool.realised_trading_pnl_gross == Decimal("2000.0000")


@pytest.mark.integration
def test_a_fully_exited_pool_is_never_relabelled_as_the_unknown_owner() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )
    state = v2(ledger)
    position = pooled(state, X)

    assert "A" in state.pool_owners()
    assert position.pool_for("A").owner == "A"
    assert "UNATTRIBUTED_EXISTING" not in state.pool_owners()
    assert not position.pool_for("A").as_json()["is_unattributed"]


@pytest.mark.integration
def test_a_closed_pool_still_appears_in_the_attribution_report() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )
    state = v2(ledger)
    valued = value_portfolio(state, marks((X, "25")))

    report = attribute_portfolio(state, valued)
    rows = {row.strategy_id: row for row in report.strategies}

    assert rows["A"].realised_pnl == Decimal("2000.0000")
    assert rows["A"].market_value == Decimal("0")
    assert not rows["A"].is_unattributed


@pytest.mark.integration
def test_a_pool_that_never_traded_is_not_retained() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    assert "NEVER_TRADED" not in {pool.owner for pool in position.retained_pools()}
    assert position.pool_for("NEVER_TRADED").quantity == Decimal("0")


# --------------------------------------------------------------------------- #
# Splits and corporate actions
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_split_scales_every_pool_and_creates_no_pnl() -> None:
    ledger = differing_entry_ledger()
    before = pooled(v2(ledger), X)
    ledger.append(
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp-sp",
            identity=X,
            numerator=Decimal("3"),
            denominator=Decimal("1"),
        )
    )

    after = pooled(v2(ledger), X)

    assert after.quantity == before.quantity * 3
    assert after.total_cost == before.total_cost
    assert after.pool_for("A").average_cost == Decimal("3.33333333")
    assert after.pool_for("B").total_cost_basis == before.pool_for("B").total_cost_basis
    assert after.realised_pnl == before.realised_pnl


@pytest.mark.integration
def test_a_reverse_split_keeps_the_pools_summing_to_the_position() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp-sp",
            identity=X,
            numerator=Decimal("1"),
            denominator=Decimal("3"),
        )
    )

    position = pooled(v2(ledger), X)

    position.check_invariants()
    assert sum((pool.quantity for pool in position.pools.values()), Decimal(0)) == position.quantity


@pytest.mark.integration
def test_a_dividend_stating_only_an_aggregate_is_not_divided_across_holders() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    after = position.credit_dividend(Decimal("300"))

    assert after.pool_for("UNATTRIBUTED_EXISTING").dividends_received == Decimal("300.0000")
    assert after.dividends_received == Decimal("300.0000")
    assert after.pool_for("A").dividends_received == Decimal("0")


@pytest.mark.integration
def test_a_dividend_with_stated_attribution_is_credited_to_those_owners() -> None:
    position = pooled(v2(differing_entry_ledger()), X)

    after = position.credit_dividend(
        Decimal("300"),
        attribution=(
            StrategySlice(strategy_id="A", quantity=Decimal("100")),
            StrategySlice(strategy_id="B", quantity=Decimal("200")),
        ),
    )

    assert after.pool_for("A").dividends_received == Decimal("100.0000")
    assert after.pool_for("B").dividends_received == Decimal("200.0000")
    assert after.dividends_received == Decimal("300.0000")


# --------------------------------------------------------------------------- #
# The exact pool identities
# --------------------------------------------------------------------------- #


def mixed_ledger() -> PortfolioLedger:
    """A ledger exercising buys, sells, a split and a deposit across two owners."""
    ledger = ledger_of(
        opening(cash="250000"),
        fill(event_id="A1", quantity="300", price="11", fees="2", owners=[("A", "300")]),
        fill(
            event_id="B1",
            quantity="700",
            price="23",
            fees="3",
            at="2026-10-01T10:10:00-04:00",
            owners=[("B", "700")],
        ),
        fill(
            event_id="Y1",
            identity=Y,
            quantity="120",
            price="37",
            fees="1",
            at="2026-10-01T10:20:00-04:00",
            owners=[("A", "40"), ("B", "80")],
        ),
    )
    ledger.append(
        fill(
            event_id="S1",
            side=Side.SELL,
            quantity="150",
            price="19",
            fees="2",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100"), ("B", "50")],
        )
    )
    ledger.append(
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at="2026-10-01T12:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp-sp",
            identity=X,
            numerator=Decimal("3"),
            denominator=Decimal("2"),
        )
    )
    ledger.append(
        ExternalCashFlowEvent(
            event_id="D1",
            event_type=EventType.EXTERNAL_CASH_FLOW,
            effective_at="2026-10-01T13:00:00-04:00",
            source_id="BANK",
            source_fingerprint="fp-d1",
            direction=CashFlowDirection.DEPOSIT,
            amount=Decimal("5000"),
        )
    )
    return ledger


@pytest.mark.integration
def test_global_quantity_equals_the_sum_of_the_pool_quantities() -> None:
    state = v2(mixed_ledger())

    for key in sorted(state.positions):
        position = state.positions[key]
        assert isinstance(position, PooledPosition)
        assert position.quantity == sum(
            (pool.quantity for pool in position.pools.values()), Decimal(0)
        )


@pytest.mark.integration
def test_global_cost_basis_equals_the_sum_of_the_pool_bases() -> None:
    state = v2(mixed_ledger())

    assert state.total_cost_basis == sum(
        (pool.total_cost_basis for pool in state.cost_pools()), Decimal(0)
    )


@pytest.mark.integration
def test_global_realised_equals_the_sum_of_the_pool_realised() -> None:
    state = v2(mixed_ledger())

    assert state.realised_pnl == sum(
        (pool.realised_trading_pnl_gross for pool in state.cost_pools()), Decimal(0)
    )


@pytest.mark.integration
def test_global_unrealised_equals_the_sum_of_the_pool_unrealised() -> None:
    state = v2(mixed_ledger())
    snapshot = marks((X, "21"), (Y, "35"))
    valued = value_portfolio(state, snapshot)
    prices = {X.key: Decimal("21"), Y.key: Decimal("35")}

    pool_total = sum(
        (
            pool.unrealised_pnl(prices[pool.identity.key])
            for pool in state.cost_pools()
            if pool.is_open
        ),
        Decimal(0),
    )

    assert pool_total == valued.pnl.unrealised


@pytest.mark.integration
def test_the_pool_invariants_are_checked_on_every_state() -> None:
    v2(mixed_ledger()).check_pool_invariants()


@pytest.mark.integration
def test_the_p1_3_identities_still_hold_under_the_new_basis() -> None:
    state = v2(mixed_ledger())
    valued = value_portfolio(state, marks((X, "21"), (Y, "35")))

    state.check_invariants()
    valued.check_nav_identity()
    valued.check_nav_change_identity()
    valued.pnl.check()


@pytest.mark.integration
def test_fees_remain_expensed_and_realised_remains_gross() -> None:
    ledger = ledger_of(
        opening(),
        fill(event_id="A1", quantity="100", price="10", fees="7", owners=[("A", "100")]),
    )
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="100",
            price="15",
            fees="3",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "100")],
        )
    )
    state = v2(ledger)

    assert state.realised_pnl == Decimal("500.0000")
    assert state.fees_paid == Decimal("10.0000")
    assert pooled(state, X).pool_for("A").fees_paid == Decimal("10.0000")


@pytest.mark.integration
def test_an_external_flow_is_still_never_pnl_under_the_new_basis() -> None:
    state = v2(mixed_ledger())
    valued = value_portfolio(state, marks((X, "21"), (Y, "35")))

    assert valued.pnl.external_flow_net == Decimal("5000.0000")


@pytest.mark.integration
def test_the_unpriced_cost_basis_term_survives_under_the_new_basis() -> None:
    state = v2(mixed_ledger())

    valued = value_portfolio(state, marks((X, "21")), require_complete=False)

    assert valued.pnl.unpriced_cost_basis > Decimal("0")
    valued.check_nav_change_identity()


# --------------------------------------------------------------------------- #
# Property tests over generated sequences
# --------------------------------------------------------------------------- #


def deterministic_sequences() -> list[list[tuple[str, str, str, str]]]:
    """Deterministic buy/sell programmes across two owners.

    Generated from a fixed table rather than randomly: a property that holds for
    a different sequence on every run cannot be reproduced from a failure report.
    """
    programmes: list[list[tuple[str, str, str, str]]] = []
    prices = ("7", "13", "29", "3")
    for first in prices:
        for second in prices:
            programmes.append(
                [
                    ("BUY", "A", "100", first),
                    ("BUY", "B", "100", second),
                    ("SELL", "A", "40", second),
                    ("BUY", "A", "60", first),
                    ("SELL", "B", "100", first),
                    ("SELL", "A", "120", second),
                ]
            )
    return programmes


@pytest.mark.integration
@pytest.mark.parametrize("programme", deterministic_sequences())
def test_the_pools_reconcile_after_every_event(
    programme: list[tuple[str, str, str, str]],
) -> None:
    ledger = ledger_of(opening(cash="1000000"))
    for index, (side, owner, quantity, price) in enumerate(programme, start=1):
        ledger.append(
            fill(
                event_id=f"E{index}",
                side=Side.BUY if side == "BUY" else Side.SELL,
                quantity=quantity,
                price=price,
                at=f"2026-10-01T{10 + index}:00:00-04:00",
                owners=[(owner, quantity)],
            )
        )
        state = v2(ledger)
        position = pooled(state, X)

        assert position.quantity == sum((p.quantity for p in position.pools.values()), Decimal(0))
        assert position.total_cost == sum(
            (p.total_cost_basis for p in position.pools.values()), Decimal(0)
        )
        assert position.realised_pnl == sum(
            (p.realised_trading_pnl_gross for p in position.pools.values()), Decimal(0)
        )
        state.check_pool_invariants()


@pytest.mark.integration
@pytest.mark.parametrize("programme", deterministic_sequences())
def test_no_pool_ever_goes_negative(programme: list[tuple[str, str, str, str]]) -> None:
    ledger = ledger_of(opening(cash="1000000"))
    for index, (side, owner, quantity, price) in enumerate(programme, start=1):
        ledger.append(
            fill(
                event_id=f"E{index}",
                side=Side.BUY if side == "BUY" else Side.SELL,
                quantity=quantity,
                price=price,
                at=f"2026-10-01T{10 + index}:00:00-04:00",
                owners=[(owner, quantity)],
            )
        )
        for pool in v2(ledger).cost_pools():
            assert pool.quantity >= Decimal("0")
            assert pool.total_cost_basis >= Decimal("0")


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_v1_ledger_with_differing_entry_prices_reconstructs_exactly_from_events() -> None:
    ledger = differing_entry_ledger()
    ledger.append(
        fill(
            event_id="A2",
            side=Side.SELL,
            quantity="40",
            price="30",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "40")],
        )
    )

    report = migrate_from_events(ledger)
    pools = {row["owner"]: row for row in report["pools"]}

    assert report["mode"] == MIGRATION_MODE
    assert report["reconstructed"] is True
    assert pools["A"]["average_cost"] == "10.00000000"
    assert pools["B"]["average_cost"] == "20.00000000"
    assert pools["A"]["realised_trading_pnl_gross"] == "800.0000"


@pytest.mark.integration
def test_migration_leaves_the_v1_ledger_replayable_under_v1() -> None:
    ledger = differing_entry_ledger()
    before = replay(ledger).as_json()

    migrate_from_events(ledger)

    assert replay(ledger).as_json() == before


@pytest.mark.integration
def test_migration_never_moves_cash() -> None:
    report = migrate_from_events(differing_entry_ledger())

    assert report["differences"]["cash_unchanged"] is True


@pytest.mark.integration
def test_a_snapshot_cannot_be_migrated_because_the_bases_were_pooled_away() -> None:
    state = replay(differing_entry_ledger())

    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE):
        refuse_snapshot_migration(state)


@pytest.mark.integration
def test_an_incomplete_event_history_cannot_be_migrated() -> None:
    records = list(differing_entry_ledger().records)

    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE):
        migrate_from_events([records[0], records[2]])


@pytest.mark.integration
def test_an_empty_history_cannot_be_migrated() -> None:
    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE):
        migrate_from_events([])


@pytest.mark.integration
def test_there_is_no_automatic_migration() -> None:
    ledger = differing_entry_ledger()

    assert replay(ledger).accounting_version == ACCOUNTING_V1
    assert not replay(ledger).uses_cost_pools


# --------------------------------------------------------------------------- #
# The durable head anchor
# --------------------------------------------------------------------------- #


def anchored_store(tmp_path: Path, *, count: int = 4) -> tuple[PortfolioStore, PortfolioLedger]:
    """A store with `count` events committed through the anchored path."""
    store = PortfolioStore(tmp_path / ".portfolio", accounting_version=ACCOUNTING_V2)
    ledger = PortfolioLedger()
    store.commit(ledger, opening(cash="100000"))
    for index in range(1, count):
        store.commit(
            ledger,
            fill(
                event_id=f"F{index}",
                quantity="10",
                price="50",
                fees="1",
                at=f"2026-10-01T1{index}:00:00-04:00",
                owners=[("A" if index % 2 else "B", "10")],
            ),
        )
    return store, ledger


@pytest.mark.integration
def test_the_opening_balance_is_anchored_at_sequence_one(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path, count=1)

    anchor = anchor_of(store)

    assert anchor is not None
    assert anchor.sequence == 1
    assert anchor.ledger_head_hash == ledger.head_hash


@pytest.mark.integration
def test_every_committed_event_advances_the_anchor(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)

    assert anchor_of(store).sequence == len(ledger)
    assert [a.sequence for a in store.anchor_trail()] == [1, 2, 3, 4]


@pytest.mark.integration
def test_the_anchor_witnesses_the_replayed_state(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)

    expected = state_fingerprint(replay(ledger, accounting_version=ACCOUNTING_V2))

    assert anchor_of(store).state_fingerprint == expected


@pytest.mark.integration
def test_the_anchor_fingerprint_excludes_the_writing_clock() -> None:
    first = LedgerHeadAnchor(
        ledger_generation="G",
        sequence=1,
        ledger_head_hash="a" * 64,
        state_fingerprint="b" * 64,
        engine_version=ACCOUNTING_V2,
    )
    second = dataclasses.replace(first, written_at="2026-10-02T12:00:00Z")

    assert first.fingerprint() == second.fingerprint()


@pytest.mark.integration
def test_retrying_a_committed_event_writes_nothing_new(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)
    before = anchor_of(store).fingerprint()

    result = store.commit(
        ledger,
        fill(
            event_id="F1",
            quantity="10",
            price="50",
            fees="1",
            at="2026-10-01T11:00:00-04:00",
            owners=[("A", "10")],
        ),
    )

    assert result.written is False
    assert len(ledger) == 4
    assert anchor_of(store).fingerprint() == before


@pytest.mark.integration
def test_an_event_id_conflict_leaves_the_anchor_untouched(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)
    before = anchor_of(store).fingerprint()

    with pytest.raises(AccountingError, match=Code.EVENT_ID_CONFLICT):
        store.commit(
            ledger,
            fill(
                event_id="F1",
                quantity="999",
                price="50",
                at="2026-10-01T11:00:00-04:00",
                owners=[("A", "999")],
            ),
        )

    assert anchor_of(store).fingerprint() == before


@pytest.mark.integration
def test_a_refused_event_is_never_written_to_disk(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path, count=1)
    records_before = len(store.record_paths())
    anchor_before = anchor_of(store).fingerprint()

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_CASH):
        store.commit(
            ledger,
            fill(
                event_id="HUGE",
                quantity="1000000",
                price="50",
                at="2026-10-01T11:00:00-04:00",
                owners=[("A", "1000000")],
            ),
        )

    assert len(store.record_paths()) == records_before
    assert len(ledger) == 1
    assert anchor_of(store).fingerprint() == anchor_before


@pytest.mark.integration
def test_a_refused_event_leaves_the_store_replayable(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path, count=1)

    with pytest.raises(AccountingError):
        store.commit(
            ledger,
            fill(
                event_id="HUGE",
                quantity="1000000",
                price="50",
                at="2026-10-01T11:00:00-04:00",
                owners=[("A", "1000000")],
            ),
        )

    assert replay(store.load_ledger(), accounting_version=ACCOUNTING_V2).cash == Decimal(
        "100000.0000"
    )


# --------------------------------------------------------------------------- #
# Crash recovery
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_ledger_ahead_of_its_anchor_is_recoverable(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)
    store.append(
        ledger.append(
            fill(
                event_id="TAIL",
                quantity="5",
                price="50",
                fees="1",
                at="2026-10-01T19:00:00-04:00",
                owners=[("A", "5")],
            )
        )
    )

    result = store.classify()

    assert result["status"] == str(AnchorStatus.RECOVERY_REQUIRED)
    assert result["recoverable"] is True


@pytest.mark.integration
def test_recovery_advances_the_anchor_over_a_verified_tail(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)
    store.append(
        ledger.append(
            fill(
                event_id="TAIL",
                quantity="5",
                price="50",
                fees="1",
                at="2026-10-01T19:00:00-04:00",
                owners=[("A", "5")],
            )
        )
    )

    outcome = store.recover()

    assert outcome.recovered is True
    assert outcome.from_sequence == 4
    assert outcome.to_sequence == 5
    assert store.classify()["consistent"] is True


@pytest.mark.integration
def test_recovery_applies_no_event_twice(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)
    store.append(
        ledger.append(
            fill(
                event_id="TAIL",
                quantity="5",
                price="50",
                fees="1",
                at="2026-10-01T19:00:00-04:00",
                owners=[("A", "5")],
            )
        )
    )
    expected = replay(ledger, accounting_version=ACCOUNTING_V2).cash

    store.recover()

    assert len(store.load_ledger()) == 5
    assert replay(store.load_ledger(), accounting_version=ACCOUNTING_V2).cash == expected


@pytest.mark.integration
def test_a_fresh_process_recovers_the_exact_state(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)
    expected = state_fingerprint(replay(ledger, accounting_version=ACCOUNTING_V2))

    reopened = PortfolioStore(tmp_path / ".portfolio", accounting_version=ACCOUNTING_V2)

    # Compared by state fingerprint rather than by the whole JSON: the display
    # symbol is deliberately not persisted, so a reloaded position carries
    # `symbol=None` while every accounting figure is identical. The fingerprint
    # covers exactly the accounting content, which is what has to survive.
    assert reopened.recover().status is AnchorStatus.CONSISTENT
    assert (
        state_fingerprint(replay(reopened.load_ledger(), accounting_version=ACCOUNTING_V2))
        == expected
    )
    assert anchor_of(store).state_fingerprint == expected


@pytest.mark.integration
def test_a_partial_record_is_a_named_refusal_not_a_parse_error(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    (tmp_path / ".portfolio" / "ledger" / "000000000009.json").write_text(
        '{"sequence": 9, "trunc', encoding="utf-8"
    )

    with pytest.raises(AccountingError, match=Code.PARTIAL_EVENT_RECORD):
        store.load_ledger()


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_truncated_ledger_still_passes_the_hash_chain(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    store.record_paths()[-1].unlink()

    assert verify_chain(store.load_ledger().records)["verified"] is True


@pytest.mark.integration
def test_one_missing_tail_record_is_detected_against_the_anchor(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    store.record_paths()[-1].unlink()

    with pytest.raises(AccountingError, match=Code.LEDGER_TRUNCATION_DETECTED):
        store.recover()


@pytest.mark.integration
def test_three_missing_tail_records_are_detected_against_the_anchor(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path, count=6)
    for path in store.record_paths()[-3:]:
        path.unlink()

    with pytest.raises(AccountingError, match=Code.LEDGER_TRUNCATION_DETECTED):
        store.recover()


@pytest.mark.integration
def test_truncation_is_detected_with_no_checkpoint_written(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    assert store.checkpoints() == ()
    store.record_paths()[-1].unlink()

    with pytest.raises(AccountingError, match=Code.LEDGER_TRUNCATION_DETECTED):
        store.recover()


@pytest.mark.integration
def test_the_anchor_is_never_moved_backward(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)

    with pytest.raises(AccountingError, match=Code.LEDGER_TRUNCATION_DETECTED):
        store.write_anchor(
            LedgerHeadAnchor(
                ledger_generation=store.generation,
                sequence=1,
                ledger_head_hash=ledger.records[0].record_hash,
                state_fingerprint="x" * 64,
                engine_version=ACCOUNTING_V2,
            )
        )


@pytest.mark.integration
def test_a_head_mismatch_at_the_same_sequence_fails_closed(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    anchor = anchor_of(store)
    rewritten = dataclasses.replace(anchor, ledger_head_hash="d" * 64)
    (tmp_path / ".portfolio" / "head.json").write_text(
        json.dumps(rewritten.as_json()), encoding="utf-8"
    )

    with pytest.raises(AccountingError, match=Code.LEDGER_HEAD_ANCHOR_MISMATCH):
        store.recover()


@pytest.mark.integration
def test_an_edited_anchor_fails_its_own_fingerprint(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    body = json.loads((tmp_path / ".portfolio" / "head.json").read_text(encoding="utf-8"))
    body["sequence"] = 99
    (tmp_path / ".portfolio" / "head.json").write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(AccountingError, match=Code.ANCHOR_FINGERPRINT_MISMATCH):
        store.read_anchor()


@pytest.mark.integration
def test_a_ledger_with_no_anchor_does_not_establish_completeness(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    (tmp_path / ".portfolio" / "head.json").unlink()

    with pytest.raises(AccountingError, match=Code.ANCHOR_MISSING):
        store.recover()


@pytest.mark.integration
def test_an_anchor_from_another_ledger_generation_is_refused(tmp_path: Path) -> None:
    anchored_store(tmp_path)

    with pytest.raises(AccountingError, match=Code.LEDGER_GENERATION_MISMATCH):
        PortfolioStore(
            tmp_path / ".portfolio",
            accounting_version=ACCOUNTING_V2,
            generation="SOME_OTHER_LEDGER",
        )


@pytest.mark.integration
def test_an_anchor_at_sequence_zero_must_bind_the_genesis_head() -> None:
    with pytest.raises(AccountingError, match=Code.ANCHOR_INVALID):
        LedgerHeadAnchor(
            ledger_generation="G",
            sequence=0,
            ledger_head_hash="a" * 64,
            state_fingerprint="b" * 64,
            engine_version=ACCOUNTING_V2,
        )


@pytest.mark.integration
def test_an_anchor_above_sequence_zero_may_not_bind_the_genesis_head() -> None:
    with pytest.raises(AccountingError, match=Code.ANCHOR_INVALID):
        LedgerHeadAnchor(
            ledger_generation="G",
            sequence=5,
            ledger_head_hash="0" * 64,
            state_fingerprint="b" * 64,
            engine_version=ACCOUNTING_V2,
        )


@pytest.mark.integration
def test_an_anchor_missing_a_required_field_is_refused() -> None:
    from stock_screener.portfolio_accounting.anchor import ANCHOR_VERSION

    with pytest.raises(AccountingError, match=Code.ANCHOR_INVALID):
        LedgerHeadAnchor.from_json({"anchor_version": ANCHOR_VERSION, "sequence": 1})


@pytest.mark.integration
def test_classify_refuses_an_anchor_from_another_generation_without_comparing() -> None:
    ledger = ledger_of(opening())
    anchor = LedgerHeadAnchor(
        ledger_generation="SOME_OTHER_LEDGER",
        sequence=1,
        ledger_head_hash=ledger.head_hash,
        state_fingerprint="s" * 64,
        engine_version=ACCOUNTING_V2,
    )

    result = classify_anchor(anchor, ledger.records, ledger_generation="THIS_LEDGER")

    assert result["status"] == str(AnchorStatus.GENERATION_MISMATCH)
    assert result["consistent"] is False


@pytest.mark.integration
def test_an_empty_ledger_needs_no_anchor() -> None:
    result = classify_anchor(None, (), ledger_generation="G")

    assert result["status"] == str(AnchorStatus.CONSISTENT)


@pytest.mark.integration
def test_a_state_fingerprint_disagreement_fails_closed() -> None:
    ledger = ledger_of(opening())
    anchor = LedgerHeadAnchor(
        ledger_generation="G",
        sequence=1,
        ledger_head_hash=ledger.head_hash,
        state_fingerprint="s" * 64,
        engine_version=ACCOUNTING_V2,
    )

    result = classify_anchor(
        anchor,
        ledger.records,
        ledger_generation="G",
        replayed_state_fingerprint="d" * 64,
    )

    assert result["status"] == str(AnchorStatus.STATE_FINGERPRINT_MISMATCH)


@pytest.mark.integration
def test_an_anchor_of_an_unknown_version_is_refused() -> None:
    with pytest.raises(AccountingError, match=Code.ANCHOR_INVALID):
        LedgerHeadAnchor.from_json({"anchor_version": "SOMETHING_ELSE"})


# --------------------------------------------------------------------------- #
# P1.3 ledger integrity is unweakened
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_tampering_with_a_stored_record_still_breaks_the_chain(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    path = store.record_paths()[1]
    body = json.loads(path.read_text(encoding="utf-8"))
    body["event"]["payload"]["price"] = "1"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(AccountingError, match=Code.LEDGER_HASH_CHAIN_MISMATCH):
        store.load_ledger()


@pytest.mark.integration
def test_deleting_a_middle_record_still_breaks_the_chain(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)
    store.record_paths()[1].unlink()

    with pytest.raises(AccountingError, match=Code.LEDGER_SEQUENCE_GAP):
        store.load_ledger()


@pytest.mark.integration
def test_overwriting_a_stored_record_is_still_refused(tmp_path: Path) -> None:
    store, ledger = anchored_store(tmp_path)

    with pytest.raises(AccountingError, match=Code.PARTIAL_EVENT_RECORD):
        store.append(dataclasses.replace(ledger.records[0], record_hash="f" * 64))


# --------------------------------------------------------------------------- #
# Checkpoints are an optional cache
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_store_needs_no_checkpoint_to_report_a_consistent_anchor(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path)

    result = store.verify()

    assert result["checkpoint_count"] == 0
    assert result["tail_completeness_established"] is True
    assert result["checkpoints_required_for_completeness"] is False


@pytest.mark.integration
def test_a_checkpoint_may_not_claim_more_history_than_the_anchor(tmp_path: Path) -> None:
    from stock_screener.portfolio_accounting import Checkpoint

    store, ledger = anchored_store(tmp_path)
    store.write_checkpoint(
        Checkpoint(
            sequence=99,
            head_hash=ledger.head_hash,
            record_count=99,
            taken_at="2026-10-01T16:00:00-04:00",
        )
    )

    with pytest.raises(AccountingError, match=Code.CHECKPOINT_UNVERIFIABLE):
        store.verify()


# --------------------------------------------------------------------------- #
# The external anchor seam, and the limit stated honestly
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_coordinated_local_rollback_is_not_detected_locally(tmp_path: Path) -> None:
    store, _ = anchored_store(tmp_path, count=6)
    rolled_back_to = 3
    for path in store.record_paths()[rolled_back_to:]:
        path.unlink()
    records = tuple(store.load_ledger().records)
    state = replay(records, accounting_version=ACCOUNTING_V2)
    (tmp_path / ".portfolio" / "head.json").unlink()
    store.write_anchor(
        LedgerHeadAnchor(
            ledger_generation=store.generation,
            sequence=rolled_back_to,
            ledger_head_hash=records[-1].record_hash,
            state_fingerprint=state_fingerprint(state),
            engine_version=ACCOUNTING_V2,
        )
    )

    # This is the honest result, not a passing guarantee: both files were rolled
    # back together and the pair is self-consistent, so local validation has
    # nothing left to compare against.
    assert store.recover().status is AnchorStatus.CONSISTENT


@pytest.mark.integration
def test_an_external_anchor_is_what_detects_a_coordinated_rollback() -> None:
    sink = InMemoryExternalAnchor()
    sink.publish(sequence=10, ledger_head="a" * 64, anchor_fingerprint="f" * 64)
    rolled_back = LedgerHeadAnchor(
        ledger_generation="G",
        sequence=5,
        ledger_head_hash="b" * 64,
        state_fingerprint="s" * 64,
        engine_version=ACCOUNTING_V2,
    )

    result = compare_external_anchor(rolled_back, sink.latest())

    assert result["rollback_detected"] is True


@pytest.mark.integration
def test_without_an_external_anchor_rollback_detection_is_reported_as_unavailable() -> None:
    result = compare_external_anchor(None, None)

    assert result["external_available"] is False
    assert result["rollback_detected"] is None


@pytest.mark.integration
def test_an_external_anchor_refuses_to_witness_a_regression() -> None:
    sink = InMemoryExternalAnchor()
    sink.publish(sequence=10, ledger_head="a" * 64, anchor_fingerprint="f" * 64)

    with pytest.raises(AccountingError, match=Code.LEDGER_TRUNCATION_DETECTED):
        sink.publish(sequence=2, ledger_head="b" * 64, anchor_fingerprint="g" * 64)


@pytest.mark.integration
def test_a_store_mirrors_each_head_to_an_injected_external_anchor(tmp_path: Path) -> None:
    sink = InMemoryExternalAnchor()
    store = PortfolioStore(
        tmp_path / ".portfolio", accounting_version=ACCOUNTING_V2, external_anchor=sink
    )
    ledger = PortfolioLedger()

    store.commit(ledger, opening(cash="100000"))

    latest = sink.latest()
    assert latest is not None
    assert int(latest["sequence"]) == 1


@pytest.mark.integration
def test_the_external_anchor_seam_holds_no_network_capability() -> None:
    tree = ast.parse((ACCOUNTING_SOURCE / "external_anchor.py").read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    assert not imports & {
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "urllib.request",
        "sqlite3",
        "sqlalchemy",
    }


@pytest.mark.integration
def test_the_store_reports_the_rollback_limitation_rather_than_hiding_it(
    tmp_path: Path,
) -> None:
    store, _ = anchored_store(tmp_path)

    assert (
        store.verify()["coordinated_rollback_detection"] == "NOT_GUARANTEED_WITHOUT_EXTERNAL_ANCHOR"
    )


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_v1_manifest_still_lists_exactly_the_v1_codes() -> None:
    assert accounting_manifest()["error_codes"] == sorted(V1_ERROR_CODES)


@pytest.mark.integration
def test_the_v2_manifest_adds_the_attribution_and_version_codes() -> None:
    manifest = accounting_manifest(accounting_version=ACCOUNTING_V2)

    assert manifest["error_codes"] == sorted(V2_ERROR_CODES)
    assert Code.INSUFFICIENT_ATTRIBUTED_POSITION in manifest["error_codes"]


@pytest.mark.integration
def test_the_v2_manifest_lists_the_anchor_codes_separately() -> None:
    manifest = accounting_manifest(accounting_version=ACCOUNTING_V2)

    assert manifest["anchor_error_codes"] == sorted(ANCHOR_ERROR_CODES)


@pytest.mark.integration
def test_the_v2_manifest_binds_the_ledger_and_its_anchor() -> None:
    manifest = accounting_manifest(
        accounting_version=ACCOUNTING_V2,
        ledger_generation="G1",
        ledger_sequence=7,
        ledger_head="a" * 64,
        head_anchor_fingerprint="f" * 64,
        state_fingerprint="s" * 64,
        code_commit="abc123",
    )

    assert manifest["ledger"]["generation"] == "G1"
    assert manifest["ledger"]["sequence"] == 7
    assert manifest["ledger"]["head_anchor_fingerprint"] == "f" * 64
    assert manifest["code_commit"] == "abc123"


@pytest.mark.integration
def test_the_v2_manifest_names_the_attribution_convention() -> None:
    manifest = accounting_manifest(accounting_version=ACCOUNTING_V2)

    assert manifest["attribution_convention"] == PER_ATTRIBUTION_COST_POLICY
    assert "NOT_IMPLEMENTED" in manifest["tax_accounting"]


@pytest.mark.integration
def test_the_v2_manifest_states_the_four_new_identities() -> None:
    invariants = accounting_manifest(accounting_version=ACCOUNTING_V2)["invariants"]

    assert any("global quantity = sum" in line for line in invariants)
    assert any("global cost basis = sum" in line for line in invariants)
    assert any("global realised trading P&L = sum" in line for line in invariants)
    assert any("global unrealised P&L = sum" in line for line in invariants)


@pytest.mark.integration
def test_the_v2_manifest_states_what_is_not_guaranteed() -> None:
    not_guaranteed = accounting_manifest(accounting_version=ACCOUNTING_V2)["not_guaranteed"]

    assert any("COORDINATED_LOCAL_ROLLBACK_DETECTION" in line for line in not_guaranteed)


@pytest.mark.integration
def test_the_v2_manifest_refuses_a_tax_lot_claim() -> None:
    manifest = accounting_manifest(accounting_version=ACCOUNTING_V2)

    assert any("select a tax lot" in line for line in manifest["never_does"])


@pytest.mark.integration
def test_the_two_manifest_versions_are_distinct() -> None:
    assert (
        accounting_manifest()["manifest_version"]
        != accounting_manifest(accounting_version=ACCOUNTING_V2)["manifest_version"]
    )


# --------------------------------------------------------------------------- #
# No tax-lot machinery was introduced
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_no_module_implements_lot_selection() -> None:
    for path in sorted(ACCOUNTING_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        assert not {
            name
            for name in names
            if "fifo" in name.lower()
            or "lifo" in name.lower()
            or "wash_sale" in name.lower()
            or "tax_lot" in name.lower()
        }, path.name


@pytest.mark.integration
def test_the_pool_module_imports_no_network_or_database_module() -> None:
    for name in ("pools.py", "versions.py", "anchor.py", "migration.py"):
        tree = ast.parse((ACCOUNTING_SOURCE / name).read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        assert not imports & {
            "http",
            "httpx",
            "requests",
            "socket",
            "urllib",
            "sqlite3",
            "sqlalchemy",
            "alpaca",
        }, name


@pytest.mark.integration
def test_the_accounting_core_still_mentions_no_order_submission() -> None:
    for name in ("pools.py", "versions.py", "anchor.py", "migration.py"):
        source = (ACCOUNTING_SOURCE / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
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
            "submit_order",
            "cancel_order",
            "place_order",
            "api_key",
            "api_secret",
            "secret_key",
        }, name


# --------------------------------------------------------------------------- #
# The goldens
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_multi_strategy_basis_fixture_reproduces_its_golden() -> None:
    request = load_request(FIXTURES / "multi_strategy_basis.json")
    golden = json.loads((FIXTURES / "multi_strategy_basis.golden.json").read_text(encoding="utf-8"))

    assert evaluate_request(request) == golden


@pytest.mark.integration
def test_the_multi_strategy_golden_proves_each_identity_holds() -> None:
    golden = json.loads((FIXTURES / "multi_strategy_basis.golden.json").read_text(encoding="utf-8"))

    for key in ("cost_basis", "realised_trading_pnl_gross", "unrealised_pnl", "market_value"):
        assert golden["pool_identities"][key]["holds"] is True


@pytest.mark.integration
def test_the_multi_strategy_golden_shows_different_bases_per_owner() -> None:
    golden = json.loads((FIXTURES / "multi_strategy_basis.golden.json").read_text(encoding="utf-8"))
    pools = {
        (row["identity"], row["owner"]): row for row in golden["state"]["attribution_cost_pools"]
    }

    assert pools[("COMPANY:7001", "ALPHA_COMPOUNDER")]["total_cost_basis"] == "18000.0000"
    assert pools[("COMPANY:7001", "BETA_MEANREV")]["total_cost_basis"] == "15000.0000"
    assert pools[("COMPANY:7001", "ALPHA_COMPOUNDER")]["realised_trading_pnl_gross"] == "8000.0000"
    assert pools[("COMPANY:7001", "BETA_MEANREV")]["realised_trading_pnl_gross"] == "-3750.0000"


@pytest.mark.integration
def test_the_full_exit_fixture_reproduces_its_golden() -> None:
    request = load_request(FIXTURES / "full_strategy_exit.json")
    golden = json.loads((FIXTURES / "full_strategy_exit.golden.json").read_text(encoding="utf-8"))

    assert evaluate_request(request) == golden


@pytest.mark.integration
def test_the_full_exit_golden_keeps_the_exited_strategys_realised_pnl() -> None:
    golden = json.loads((FIXTURES / "full_strategy_exit.golden.json").read_text(encoding="utf-8"))
    alpha = [
        row
        for row in golden["state"]["attribution_cost_pools"]
        if row["owner"] == "ALPHA_COMPOUNDER"
    ]

    assert alpha
    assert all(row["quantity"] == "0" for row in alpha)
    assert all(row["total_cost_basis"] == "0" for row in alpha)
    assert all(not row["is_unattributed"] for row in alpha)
    assert {row["realised_trading_pnl_gross"] for row in alpha} == {"18600.0000", "-100.0000"}


@pytest.mark.integration
def test_the_anchor_golden_is_reproduced_by_a_fresh_store(tmp_path: Path) -> None:
    body = load_request(FIXTURES / "anchor_ledger.json")
    golden = json.loads((FIXTURES / "anchor_ledger.golden.json").read_text(encoding="utf-8"))
    store = PortfolioStore(
        tmp_path / ".portfolio",
        accounting_version=body["accounting_version"],
        generation=body["ledger_generation"],
    )
    ledger = PortfolioLedger()

    results = [store.commit(ledger, event_from_json(event)) for event in body["events"]]
    anchor = anchor_of(store)

    assert anchor.sequence == golden["final_sequence"]
    assert anchor.ledger_head_hash == golden["ledger_head"]
    assert anchor.state_fingerprint == golden["state_fingerprint"]
    assert anchor.fingerprint() == golden["anchor_fingerprint"]
    assert [row.written for row in results] == [row["written"] for row in golden["commits"]]


@pytest.mark.integration
def test_the_anchor_golden_trail_is_chained(tmp_path: Path) -> None:
    body = load_request(FIXTURES / "anchor_ledger.json")
    store = PortfolioStore(
        tmp_path / ".portfolio",
        accounting_version=body["accounting_version"],
        generation=body["ledger_generation"],
    )
    ledger = PortfolioLedger()
    for event in body["events"]:
        store.commit(ledger, event_from_json(event))

    trail = store.anchor_trail()

    assert [row.sequence for row in trail] == [1, 2, 3, 4]
    for earlier, later in itertools.pairwise(trail):
        assert later.previous_anchor_fingerprint == earlier.fingerprint()


@pytest.mark.integration
def test_the_anchor_golden_recovers_consistently_on_reopen(tmp_path: Path) -> None:
    body = load_request(FIXTURES / "anchor_ledger.json")
    golden = json.loads((FIXTURES / "anchor_ledger.golden.json").read_text(encoding="utf-8"))
    store = PortfolioStore(
        tmp_path / ".portfolio",
        accounting_version=body["accounting_version"],
        generation=body["ledger_generation"],
    )
    ledger = PortfolioLedger()
    for event in body["events"]:
        store.commit(ledger, event_from_json(event))

    reopened = PortfolioStore(
        tmp_path / ".portfolio", accounting_version=body["accounting_version"]
    )

    assert reopened.recover().as_json() == golden["recovery_on_reopen"]


@pytest.mark.integration
def test_the_p1_3_goldens_are_untouched_by_the_new_version() -> None:
    for name in ("example_ledger", "reconciliation_example"):
        request = load_request(FIXTURES / f"{name}.json")
        golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))

        assert evaluate_request(request) == golden
        assert golden["state"]["cost_basis_policy"] == "WEIGHTED_AVERAGE_COST_V1"
        assert "attribution_cost_pools" not in golden["state"]


@pytest.mark.integration
def test_a_v1_request_declares_no_version_and_still_replays_as_v1() -> None:
    body = load_request(FIXTURES / "example_ledger.json")

    assert "accounting_version" not in body
    assert replay(load_ledger(body["events"])).accounting_version == ACCOUNTING_V1
