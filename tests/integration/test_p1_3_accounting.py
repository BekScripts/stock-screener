"""P1.3 — portfolio state, NAV/P&L accounting and reconciliation.

These tests are organised around the claims the phase makes rather than around
the modules that implement them. The claims that matter most are negative: that
a buy without cash is refused, that a mark from after the valuation cannot be
used, that a reconciliation break is never repaired, and that nothing in the
accounting core can reach a broker or a database. Each of those has a test whose
failure would mean the guarantee is gone.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.portfolio_accounting import (
    ACCOUNTING_ERROR_CODES,
    COST_BASIS_POLICY,
    FORBIDDEN_ROOTS,
    GENESIS_HASH,
    LEDGER_CHAIN_VERSION,
    UNATTRIBUTED,
    AccountingError,
    CashDividendEvent,
    CashFlowDirection,
    Checkpoint,
    Code,
    CorporateActionEvent,
    EventType,
    ExternalCashFlowEvent,
    ExternalPosition,
    ExternalStatement,
    FillEvent,
    MarkPrice,
    MarkPriceSnapshot,
    OpeningBalanceEvent,
    OpeningPosition,
    PortfolioLedger,
    PortfolioState,
    PortfolioStore,
    PositionState,
    Side,
    SplitEvent,
    StrategySlice,
    accounting_manifest,
    apply_event,
    as_decimal,
    attribute_portfolio,
    classify_action,
    evaluate_request,
    event_fingerprint,
    event_from_json,
    is_zero,
    load_request,
    money_policy,
    quantise_cash,
    reconcile,
    refuse_unsupported,
    replay,
    seam_policy,
    to_float,
    to_risk_snapshot,
    value_portfolio,
    verify_chain,
)
from stock_screener.portfolio_risk import SecurityIdentity

if TYPE_CHECKING:
    from collections.abc import Sequence

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "portfolio_accounting"
ACCOUNTING_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "stock_screener" / "portfolio_accounting"
)

A = SecurityIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
B = SecurityIdentity(kind="COMPANY", identity_id=2, symbol="BBB")
C = SecurityIdentity(kind="COMPANY", identity_id=3, symbol="CCC")
T0 = "2026-10-01T09:30:00-04:00"


def opening(
    *, cash: str = "100000", positions: Sequence[OpeningPosition] = (), at: str = T0
) -> OpeningBalanceEvent:
    """Build an opening balance event."""
    return OpeningBalanceEvent(
        event_id="OB",
        event_type=EventType.OPENING_BALANCE,
        effective_at=at,
        source_id="IMPORT",
        source_fingerprint="fp-opening",
        cash=Decimal(cash),
        positions=tuple(positions),
    )


def fill(
    *,
    event_id: str,
    identity: SecurityIdentity = A,
    side: Side = Side.BUY,
    quantity: str = "100",
    price: str = "50",
    fees: str = "0",
    at: str = "2026-10-01T10:00:00-04:00",
    strategy: str | None = "S1",
) -> FillEvent:
    """Build a fill event."""
    attribution = (
        (StrategySlice(strategy_id=strategy, quantity=Decimal(quantity)),) if strategy else ()
    )
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
        attribution=attribution,
    )


def funded_ledger() -> PortfolioLedger:
    """A ledger holding cash and two positions."""
    ledger = PortfolioLedger()
    ledger.append(opening())
    ledger.append(fill(event_id="B1", identity=A, quantity="100", price="50", strategy="S1"))
    ledger.append(
        fill(
            event_id="B2",
            identity=B,
            quantity="200",
            price="25",
            strategy="S2",
            at="2026-10-01T10:05:00-04:00",
        )
    )
    return ledger


def marks(*, at: str = "2026-10-02T16:00:00-04:00", **prices: str) -> MarkPriceSnapshot:
    """Build a mark snapshot from `label=price` pairs."""
    lookup = {"A": A, "B": B, "C": C}
    return MarkPriceSnapshot(
        valuation_time=at,
        marks=tuple(
            MarkPrice(identity=lookup[key], price=Decimal(value), as_of=at)
            for key, value in prices.items()
        ),
    )


# --------------------------------------------------------------------------- #
# The event envelope
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_event_without_a_source_fingerprint_is_refused() -> None:
    with pytest.raises(AccountingError, match="source_fingerprint"):
        OpeningBalanceEvent(
            event_id="OB",
            event_type=EventType.OPENING_BALANCE,
            effective_at=T0,
            source_id="IMPORT",
            source_fingerprint="",
        )


@pytest.mark.integration
def test_the_event_fingerprint_excludes_the_ingestion_clock() -> None:
    first = fill(event_id="F1")
    second = dataclasses.replace(first, ingested_at="2026-10-01T10:00:01-04:00")

    assert first.fingerprint() == second.fingerprint()


@pytest.mark.integration
def test_the_event_fingerprint_changes_with_the_semantic_payload() -> None:
    assert (
        fill(event_id="F1", price="50").fingerprint()
        != fill(event_id="F1", price="51").fingerprint()
    )


@pytest.mark.integration
def test_a_set_cannot_be_fingerprinted_because_its_order_is_not_its_value() -> None:
    with pytest.raises(AccountingError, match="order is not part of its value"):
        event_fingerprint({"tickers": {"AAA", "BBB"}})


@pytest.mark.integration
def test_a_fill_carries_direction_in_its_side_not_the_sign_of_its_quantity() -> None:
    with pytest.raises(AccountingError, match="positive quantity"):
        fill(event_id="F1", quantity="-100", strategy=None)


@pytest.mark.integration
def test_an_external_cash_flow_carries_direction_in_its_field_not_its_sign() -> None:
    with pytest.raises(AccountingError, match="must be positive"):
        ExternalCashFlowEvent(
            event_id="F",
            event_type=EventType.EXTERNAL_CASH_FLOW,
            effective_at=T0,
            source_id="BANK",
            source_fingerprint="fp",
            direction=CashFlowDirection.WITHDRAWAL,
            amount=Decimal("-100"),
        )


@pytest.mark.integration
def test_an_attribution_that_does_not_sum_to_the_fill_is_refused_not_scaled() -> None:
    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_DOES_NOT_SUM):
        FillEvent(
            event_id="F",
            event_type=EventType.FILL,
            effective_at=T0,
            source_id="EXEC",
            source_fingerprint="fp",
            identity=A,
            side=Side.BUY,
            quantity=Decimal("100"),
            price=Decimal("50"),
            attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("40")),),
        )


# --------------------------------------------------------------------------- #
# The ledger and its hash chain
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_empty_ledger_has_the_genesis_head() -> None:
    assert PortfolioLedger().head_hash == GENESIS_HASH


@pytest.mark.integration
def test_the_first_event_must_be_an_opening_balance() -> None:
    with pytest.raises(AccountingError, match=Code.OPENING_BALANCE_REQUIRED):
        PortfolioLedger().append(fill(event_id="F1"))


@pytest.mark.integration
def test_a_second_opening_balance_is_refused() -> None:
    ledger = PortfolioLedger()
    ledger.append(opening())

    with pytest.raises(AccountingError, match=Code.OPENING_BALANCE_ALREADY_EXISTS):
        ledger.append(dataclasses.replace(opening(), event_id="OB2"))


@pytest.mark.integration
def test_re_presenting_an_identical_event_is_a_no_op() -> None:
    ledger = funded_ledger()
    before = len(ledger)
    event = fill(event_id="B1", identity=A, quantity="100", price="50", strategy="S1")

    record = ledger.append(event)

    assert len(ledger) == before
    assert record.sequence == 2


@pytest.mark.integration
def test_a_reused_event_id_with_a_different_payload_is_a_conflict() -> None:
    ledger = funded_ledger()

    with pytest.raises(AccountingError, match=Code.EVENT_ID_CONFLICT):
        ledger.append(fill(event_id="B1", identity=A, quantity="999", price="50"))


@pytest.mark.integration
def test_an_event_effective_before_the_last_one_is_refused() -> None:
    ledger = funded_ledger()

    with pytest.raises(AccountingError, match=Code.EVENT_TIME_REGRESSION):
        ledger.append(fill(event_id="B3", at="2026-09-30T10:00:00-04:00"))


@pytest.mark.integration
def test_a_naive_effective_time_cannot_be_ordered_and_is_refused() -> None:
    ledger = PortfolioLedger()

    with pytest.raises(AccountingError, match="UTC offset"):
        ledger.append(opening(at="2026-10-01T09:30:00"))


@pytest.mark.integration
def test_tampering_with_a_committed_event_breaks_the_chain() -> None:
    records = list(funded_ledger().records)
    event = records[1].event
    assert isinstance(event, FillEvent)
    records[1] = dataclasses.replace(
        records[1], event=dataclasses.replace(event, price=Decimal("1"))
    )

    result = verify_chain(records)

    assert not result["verified"]
    assert result["failure"]["code"] == Code.LEDGER_HASH_CHAIN_MISMATCH


@pytest.mark.integration
def test_deleting_a_record_from_the_middle_breaks_the_chain() -> None:
    records = list(funded_ledger().records)

    result = verify_chain([records[0], records[2]])

    assert not result["verified"]
    assert result["failure"]["code"] == Code.LEDGER_SEQUENCE_GAP


@pytest.mark.integration
def test_reordering_two_records_breaks_the_chain() -> None:
    records = list(funded_ledger().records)

    result = verify_chain([records[0], records[2], records[1]])

    assert not result["verified"]
    assert result["failure"]["code"] == Code.LEDGER_SEQUENCE_GAP


@pytest.mark.integration
def test_inserting_a_forged_record_breaks_the_chain() -> None:
    records = list(funded_ledger().records)
    forged = dataclasses.replace(records[2], sequence=2)

    result = verify_chain([records[0], forged, records[2]])

    assert not result["verified"]
    assert result["failure"]["code"] == Code.LEDGER_HASH_CHAIN_MISMATCH


@pytest.mark.integration
def test_a_ledger_cannot_be_opened_over_a_broken_chain() -> None:
    records = list(funded_ledger().records)

    with pytest.raises(AccountingError, match=Code.LEDGER_SEQUENCE_GAP):
        PortfolioLedger(records=[records[0], records[2]])


@pytest.mark.integration
def test_the_chain_binds_its_version_so_a_rule_change_is_not_silent() -> None:
    assert LEDGER_CHAIN_VERSION == "PORTFOLIO_LEDGER_HASH_CHAIN_V1"
    assert funded_ledger().verify()["chain_version"] == LEDGER_CHAIN_VERSION


# --------------------------------------------------------------------------- #
# Cost basis
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_fees_are_expensed_rather_than_capitalised_into_cost_basis() -> None:
    position = PositionState(identity=A).buy(
        quantity=Decimal("100"),
        price=Decimal("50"),
        fees=Decimal("25"),
        attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("100")),),
    )

    assert position.average_cost == Decimal("50.00000000")
    assert position.fees_paid == Decimal("25.0000")


@pytest.mark.integration
def test_two_purchases_produce_a_weighted_average_cost() -> None:
    position = PositionState(identity=A).buy(
        quantity=Decimal("100"),
        price=Decimal("50"),
        fees=Decimal("0"),
        attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("100")),),
    )

    position = position.buy(
        quantity=Decimal("300"),
        price=Decimal("60"),
        fees=Decimal("0"),
        attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("300")),),
    )

    assert position.average_cost == Decimal("57.50000000")


@pytest.mark.integration
def test_closing_a_position_leaves_exactly_zero_cost_rather_than_a_crumb() -> None:
    position = PositionState(identity=A).buy(
        quantity=Decimal("3"),
        price=Decimal("10"),
        fees=Decimal("0"),
        attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("3")),),
    )

    closed, _ = position.sell(quantity=Decimal("3"), price=Decimal("11"), fees=Decimal("0"))

    assert closed.total_cost == Decimal("0")
    assert not closed.is_open


@pytest.mark.integration
def test_realised_pnl_is_gross_of_fees_so_fees_are_charged_exactly_once() -> None:
    ledger = PortfolioLedger()
    ledger.append(opening())
    ledger.append(fill(event_id="B1", quantity="100", price="50", fees="7"))
    ledger.append(
        fill(
            event_id="S1",
            side=Side.SELL,
            quantity="100",
            price="55",
            fees="3",
            at="2026-10-01T11:00:00-04:00",
        )
    )

    state = replay(ledger)

    assert state.realised_pnl == Decimal("500.0000")
    assert state.fees_paid == Decimal("10.0000")


@pytest.mark.integration
def test_the_cost_basis_policy_is_named_so_a_change_is_a_new_version() -> None:
    assert COST_BASIS_POLICY == "WEIGHTED_AVERAGE_COST_V1"


# --------------------------------------------------------------------------- #
# Trading refusals
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_buy_the_cash_cannot_fund_is_refused_rather_than_overdrawn() -> None:
    state = replay(funded_ledger())

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_CASH):
        apply_event(state, fill(event_id="X", quantity="100000", price="50"), sequence=4)


@pytest.mark.integration
def test_the_fees_count_toward_whether_a_buy_can_be_funded() -> None:
    ledger = PortfolioLedger()
    ledger.append(opening(cash="5000"))
    state = replay(ledger)

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_CASH):
        apply_event(state, fill(event_id="X", quantity="100", price="50", fees="1"), sequence=2)


@pytest.mark.integration
def test_a_sale_beyond_the_holding_is_refused_rather_than_shorted() -> None:
    state = replay(funded_ledger())

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_POSITION):
        apply_event(
            state,
            fill(event_id="X", side=Side.SELL, quantity="500", price="50", strategy=None),
            sequence=4,
        )


@pytest.mark.integration
def test_selling_a_security_never_held_is_refused() -> None:
    state = replay(funded_ledger())

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_POSITION):
        apply_event(
            state,
            fill(event_id="X", identity=C, side=Side.SELL, quantity="1", strategy=None),
            sequence=4,
        )


@pytest.mark.integration
def test_a_withdrawal_beyond_the_cash_balance_is_refused() -> None:
    state = replay(funded_ledger())
    event = ExternalCashFlowEvent(
        event_id="W",
        event_type=EventType.EXTERNAL_CASH_FLOW,
        effective_at="2026-10-01T11:00:00-04:00",
        source_id="BANK",
        source_fingerprint="fp",
        direction=CashFlowDirection.WITHDRAWAL,
        amount=Decimal("999999"),
    )

    with pytest.raises(AccountingError, match=Code.INSUFFICIENT_CASH):
        apply_event(state, event, sequence=4)


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_sale_from_a_co_owned_position_must_say_whose_shares_it_sold() -> None:
    ledger = PortfolioLedger()
    ledger.append(opening())
    ledger.append(fill(event_id="B1", quantity="100", price="50", strategy="S1"))
    ledger.append(
        fill(
            event_id="B2",
            quantity="100",
            price="50",
            strategy="S2",
            at="2026-10-01T10:05:00-04:00",
        )
    )
    state = replay(ledger)

    with pytest.raises(AccountingError, match=Code.SELL_ATTRIBUTION_REQUIRED):
        apply_event(
            state,
            fill(
                event_id="S",
                side=Side.SELL,
                quantity="50",
                price="55",
                strategy=None,
                at="2026-10-01T11:00:00-04:00",
            ),
            sequence=4,
        )


@pytest.mark.integration
def test_a_strategy_cannot_sell_more_than_its_own_slice() -> None:
    position = PositionState(identity=A)
    position = position.buy(
        quantity=Decimal("100"),
        price=Decimal("50"),
        fees=Decimal("0"),
        attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("100")),),
    )
    position = position.buy(
        quantity=Decimal("100"),
        price=Decimal("50"),
        fees=Decimal("0"),
        attribution=(StrategySlice(strategy_id="S2", quantity=Decimal("100")),),
    )

    with pytest.raises(AccountingError, match=Code.ATTRIBUTION_EXCEEDS_SLICE):
        position.sell(
            quantity=Decimal("150"),
            price=Decimal("55"),
            fees=Decimal("0"),
            attribution=(StrategySlice(strategy_id="S1", quantity=Decimal("150")),),
        )


@pytest.mark.integration
def test_a_sale_from_a_single_owner_needs_no_explicit_attribution() -> None:
    state = replay(funded_ledger())

    applied = apply_event(
        state,
        fill(
            event_id="S",
            side=Side.SELL,
            quantity="50",
            price="55",
            strategy=None,
            at="2026-10-01T11:00:00-04:00",
        ),
        sequence=4,
    )

    assert applied.state.position(A).attribution["S1"] == Decimal("50.00000000")


@pytest.mark.integration
def test_an_imported_holding_is_attributed_to_an_explicit_unknown_owner() -> None:
    ledger = PortfolioLedger()
    ledger.append(
        opening(
            positions=[
                OpeningPosition(
                    identity=A, quantity=Decimal("100"), cost_basis_per_share=Decimal("40")
                )
            ]
        )
    )

    state = replay(ledger)

    assert state.position(A).owners == (UNATTRIBUTED,)
    assert state.unattributed_quantity == Decimal("100.0000")


@pytest.mark.integration
def test_the_unknown_owner_bucket_is_reported_as_such_not_hidden() -> None:
    ledger = PortfolioLedger()
    ledger.append(
        opening(
            positions=[
                OpeningPosition(
                    identity=A, quantity=Decimal("100"), cost_basis_per_share=Decimal("40")
                )
            ]
        )
    )
    state = replay(ledger)
    valued = value_portfolio(state, marks(A="50"))

    report = attribute_portfolio(state, valued)

    assert [row.strategy_id for row in report.strategies] == [UNATTRIBUTED]
    assert report.strategies[0].is_unattributed
    assert report.unattributed_market_value == Decimal("5000.0000")


@pytest.mark.integration
def test_cash_is_not_attributed_to_a_strategy_and_says_why() -> None:
    state = replay(funded_ledger())
    valued = value_portfolio(state, marks(A="55", B="26"))

    report = attribute_portfolio(state, valued)

    assert "fungible" in report.cash_attribution_note
    assert report.cash == valued.cash


# --------------------------------------------------------------------------- #
# External cash flows
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_deposit_changes_nav_and_never_pnl() -> None:
    ledger = funded_ledger()
    ledger.append(
        ExternalCashFlowEvent(
            event_id="D",
            event_type=EventType.EXTERNAL_CASH_FLOW,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="BANK",
            source_fingerprint="fp",
            direction=CashFlowDirection.DEPOSIT,
            amount=Decimal("50000"),
        )
    )

    valued = value_portfolio(replay(ledger), marks(A="50", B="25"))

    assert valued.pnl.total == Decimal("0")
    assert valued.pnl.external_flow_net == Decimal("50000.0000")
    assert valued.nav == Decimal("150000.0000")


@pytest.mark.integration
def test_a_withdrawal_is_not_a_loss() -> None:
    ledger = funded_ledger()
    ledger.append(
        ExternalCashFlowEvent(
            event_id="W",
            event_type=EventType.EXTERNAL_CASH_FLOW,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="BANK",
            source_fingerprint="fp",
            direction=CashFlowDirection.WITHDRAWAL,
            amount=Decimal("20000"),
        )
    )

    valued = value_portfolio(replay(ledger), marks(A="50", B="25"))

    assert valued.pnl.total == Decimal("0")
    assert valued.pnl.external_flow_net == Decimal("-20000.0000")


# --------------------------------------------------------------------------- #
# Corporate actions
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_dividend_is_income_and_leaves_cost_basis_alone() -> None:
    ledger = funded_ledger()
    ledger.append(
        CashDividendEvent(
            event_id="DV",
            event_type=EventType.CASH_DIVIDEND,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            cash_amount=Decimal("250"),
        )
    )

    state = replay(ledger)

    assert state.dividend_income == Decimal("250.0000")
    assert state.position(A).average_cost == Decimal("50.00000000")


@pytest.mark.integration
def test_a_split_creates_no_pnl_and_scales_average_cost_by_the_ratio() -> None:
    ledger = funded_ledger()
    before = replay(ledger).position(A)
    ledger.append(
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            numerator=Decimal("4"),
            denominator=Decimal("1"),
        )
    )

    after = replay(ledger).position(A)

    assert after.quantity == before.quantity * 4
    assert after.total_cost == before.total_cost
    assert after.average_cost == Decimal("12.50000000")
    assert after.realised_pnl == before.realised_pnl


@pytest.mark.integration
def test_a_reverse_split_keeps_its_fractional_residue_rather_than_rounding_it_away() -> None:
    ledger = funded_ledger()
    ledger.append(
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            numerator=Decimal("1"),
            denominator=Decimal("3"),
        )
    )

    position = replay(ledger).position(A)

    assert position.quantity == Decimal("33.33333333")
    assert position.total_cost == Decimal("5000.0000")


@pytest.mark.integration
def test_a_split_keeps_attribution_summing_to_the_position() -> None:
    ledger = PortfolioLedger()
    ledger.append(opening())
    ledger.append(fill(event_id="B1", quantity="100", price="50", strategy="S1"))
    ledger.append(
        fill(
            event_id="B2",
            quantity="100",
            price="50",
            strategy="S2",
            at="2026-10-01T10:05:00-04:00",
        )
    )
    ledger.append(
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            numerator=Decimal("1"),
            denominator=Decimal("3"),
        )
    )

    position = replay(ledger).position(A)

    position.check_attribution()
    assert sum(position.attribution.values()) == position.quantity


@pytest.mark.integration
def test_an_invalid_split_ratio_is_refused() -> None:
    with pytest.raises(AccountingError, match=Code.SPLIT_RATIO_INVALID):
        SplitEvent(
            event_id="SP",
            event_type=EventType.SPLIT,
            effective_at=T0,
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            numerator=Decimal("0"),
            denominator=Decimal("1"),
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "action",
    ["MERGER", "SPINOFF", "RIGHTS_DISTRIBUTION", "TENDER_OFFER", "RETURN_OF_CAPITAL"],
)
def test_an_unsupported_corporate_action_is_refused_by_name(action: str) -> None:
    state = replay(funded_ledger())
    event = CorporateActionEvent(
        event_id="CA",
        event_type=EventType.CORPORATE_ACTION,
        effective_at="2026-10-01T11:00:00-04:00",
        source_id="CA",
        source_fingerprint="fp",
        identity=A,
        action=action,
    )

    with pytest.raises(AccountingError, match=Code.UNSUPPORTED_CORPORATE_ACTION):
        apply_event(state, event, sequence=4)


@pytest.mark.integration
def test_an_unrecognised_corporate_action_is_refused_rather_than_ignored() -> None:
    with pytest.raises(AccountingError, match=Code.UNSUPPORTED_CORPORATE_ACTION):
        refuse_unsupported("SOMETHING_NEW", identity_label="COMPANY:1")


@pytest.mark.integration
def test_the_two_supported_actions_are_classified_as_supported() -> None:
    assert classify_action("CASH_DIVIDEND")["supported"]
    assert classify_action("split")["supported"]
    assert not classify_action("MERGER")["supported"]


@pytest.mark.integration
def test_a_refused_action_explains_which_terms_it_would_need() -> None:
    with pytest.raises(AccountingError, match="exchange ratio"):
        refuse_unsupported("MERGER", identity_label="COMPANY:1")


# --------------------------------------------------------------------------- #
# Valuation
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_mark_stamped_after_the_valuation_instant_is_refused() -> None:
    with pytest.raises(AccountingError, match=Code.MARK_AFTER_VALUATION_TIME):
        MarkPriceSnapshot(
            valuation_time="2026-10-02T16:00:00-04:00",
            marks=(MarkPrice(identity=A, price=Decimal("50"), as_of="2026-10-02T16:00:01-04:00"),),
        )


@pytest.mark.integration
def test_a_mark_from_earlier_than_the_valuation_is_admissible() -> None:
    snapshot = MarkPriceSnapshot(
        valuation_time="2026-10-02T16:00:00-04:00",
        marks=(MarkPrice(identity=A, price=Decimal("50"), as_of="2026-10-02T15:59:00-04:00"),),
    )

    assert snapshot.mark_for(A) is not None


@pytest.mark.integration
def test_a_valuation_missing_a_holding_is_refused_rather_than_valued_at_zero() -> None:
    state = replay(funded_ledger())

    with pytest.raises(AccountingError, match=Code.VALUATION_INCOMPLETE):
        value_portfolio(state, marks(A="55"))


@pytest.mark.integration
def test_an_incomplete_valuation_can_be_requested_explicitly_and_names_what_is_missing() -> None:
    state = replay(funded_ledger())

    valued = value_portfolio(state, marks(A="55"), require_complete=False)

    assert [identity.label for identity in valued.coverage.missing] == [B.label]
    assert valued.pnl.unpriced_cost_basis == Decimal("5000.0000")


@pytest.mark.integration
def test_a_complete_valuation_has_no_unpriced_cost_basis() -> None:
    valued = value_portfolio(replay(funded_ledger()), marks(A="55", B="26"))

    assert valued.pnl.unpriced_cost_basis == Decimal("0")


@pytest.mark.integration
def test_a_stale_mark_is_reported_rather_than_refused() -> None:
    state = replay(funded_ledger())
    snapshot = MarkPriceSnapshot(
        valuation_time="2026-10-02T16:00:00-04:00",
        marks=(
            MarkPrice(identity=A, price=Decimal("55"), as_of="2026-08-01T16:00:00-04:00"),
            MarkPrice(identity=B, price=Decimal("26"), as_of="2026-10-02T16:00:00-04:00"),
        ),
    )

    valued = value_portfolio(state, snapshot)

    assert valued.stale_marks == (A.label,)
    assert valued.nav > Decimal("0")


@pytest.mark.integration
def test_a_zero_mark_is_refused_because_it_is_a_decision_not_a_price() -> None:
    with pytest.raises(AccountingError, match=Code.MARK_INVALID):
        MarkPrice(identity=A, price=Decimal("0"), as_of="2026-10-02T16:00:00-04:00")


@pytest.mark.integration
def test_marking_one_security_twice_is_refused() -> None:
    with pytest.raises(AccountingError, match="marked twice"):
        MarkPriceSnapshot(
            valuation_time="2026-10-02T16:00:00-04:00",
            marks=(
                MarkPrice(identity=A, price=Decimal("50"), as_of="2026-10-02T16:00:00-04:00"),
                MarkPrice(identity=A, price=Decimal("51"), as_of="2026-10-02T16:00:00-04:00"),
            ),
        )


@pytest.mark.integration
def test_a_naive_mark_timestamp_is_refused() -> None:
    with pytest.raises(AccountingError, match="UTC offset"):
        MarkPrice(identity=A, price=Decimal("50"), as_of="2026-10-02T16:00:00")


# --------------------------------------------------------------------------- #
# NAV and the P&L decomposition
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_nav_is_cash_plus_the_market_value_of_the_holdings() -> None:
    valued = value_portfolio(replay(funded_ledger()), marks(A="60", B="30"))

    assert valued.market_value == Decimal("12000.0000")
    assert valued.nav == quantise_cash(valued.cash + valued.market_value)


@pytest.mark.integration
def test_the_pnl_components_sum_to_the_total() -> None:
    valued = value_portfolio(replay(funded_ledger()), marks(A="60", B="20"))
    pnl = valued.pnl

    assert pnl.total == quantise_cash(pnl.realised + pnl.unrealised + pnl.income - pnl.fees)


@pytest.mark.integration
def test_performance_explains_the_whole_move_in_nav() -> None:
    ledger = funded_ledger()
    ledger.append(
        ExternalCashFlowEvent(
            event_id="D",
            event_type=EventType.EXTERNAL_CASH_FLOW,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="BANK",
            source_fingerprint="fp",
            direction=CashFlowDirection.DEPOSIT,
            amount=Decimal("25000"),
        )
    )
    ledger.append(
        CashDividendEvent(
            event_id="DV",
            event_type=EventType.CASH_DIVIDEND,
            effective_at="2026-10-01T12:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            cash_amount=Decimal("300"),
        )
    )
    state = replay(ledger)

    valued = value_portfolio(state, marks(A="58", B="23"))

    assert valued.nav - valued.inception_nav_at_cost == quantise_cash(
        valued.pnl.total + valued.pnl.external_flow_net
    )


@pytest.mark.integration
def test_an_imported_position_values_its_inception_nav_at_cost() -> None:
    ledger = PortfolioLedger()
    ledger.append(
        opening(
            cash="10000",
            positions=[
                OpeningPosition(
                    identity=A, quantity=Decimal("100"), cost_basis_per_share=Decimal("40")
                )
            ],
        )
    )
    state = replay(ledger)

    valued = value_portfolio(state, marks(A="55"))

    assert valued.inception_nav_at_cost == Decimal("14000.0000")
    assert valued.pnl.unrealised == Decimal("1500.0000")


@pytest.mark.integration
def test_the_cash_identity_holds_after_every_event_type() -> None:
    ledger = funded_ledger()
    ledger.append(
        ExternalCashFlowEvent(
            event_id="D",
            event_type=EventType.EXTERNAL_CASH_FLOW,
            effective_at="2026-10-01T11:00:00-04:00",
            source_id="BANK",
            source_fingerprint="fp",
            direction=CashFlowDirection.DEPOSIT,
            amount=Decimal("1000"),
        )
    )
    ledger.append(
        CashDividendEvent(
            event_id="DV",
            event_type=EventType.CASH_DIVIDEND,
            effective_at="2026-10-01T12:00:00-04:00",
            source_id="CA",
            source_fingerprint="fp",
            identity=A,
            cash_amount=Decimal("50"),
        )
    )
    ledger.append(
        fill(
            event_id="S",
            side=Side.SELL,
            quantity="50",
            price="52",
            fees="1",
            strategy="S1",
            at="2026-10-01T13:00:00-04:00",
        )
    )

    state = replay(ledger)

    state.check_cash_identity()
    assert state.cash == state.cash_summary.balance


@pytest.mark.integration
def test_a_broken_pnl_decomposition_is_detected() -> None:
    valued = value_portfolio(replay(funded_ledger()), marks(A="60", B="30"))
    tampered = dataclasses.replace(valued, nav=quantise_cash(valued.nav + Decimal("1")))

    with pytest.raises(AccountingError, match=Code.NAV_IDENTITY_VIOLATED):
        tampered.check_nav_identity()


@pytest.mark.integration
def test_an_unexplained_change_in_nav_is_detected() -> None:
    valued = value_portfolio(replay(funded_ledger()), marks(A="60", B="30"))
    tampered = dataclasses.replace(
        valued,
        inception_nav_at_cost=quantise_cash(valued.inception_nav_at_cost + Decimal("500")),
    )

    with pytest.raises(AccountingError, match=Code.PNL_DECOMPOSITION_VIOLATED):
        tampered.check_nav_change_identity()


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_replaying_the_same_ledger_twice_gives_the_same_state() -> None:
    ledger = funded_ledger()

    assert replay(ledger).as_json() == replay(ledger).as_json()


@pytest.mark.integration
def test_a_state_binds_the_ledger_head_it_was_derived_from() -> None:
    ledger = funded_ledger()

    assert replay(ledger).ledger_head_hash == ledger.head_hash


@pytest.mark.integration
def test_replay_can_stop_at_a_sequence_for_an_as_of_state() -> None:
    ledger = funded_ledger()

    assert replay(ledger, through_sequence=1).cash == Decimal("100000.0000")
    assert replay(ledger, through_sequence=2).cash == Decimal("95000.0000")


@pytest.mark.integration
def test_a_broken_ledger_cannot_be_replayed() -> None:
    records = list(funded_ledger().records)

    with pytest.raises(AccountingError, match="cannot be replayed"):
        replay([records[0], records[2]])


@pytest.mark.integration
def test_an_empty_state_satisfies_its_invariants() -> None:
    state = PortfolioState()

    state.check_invariants()
    assert not state.is_established


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def agreeing_statement(**overrides: Any) -> ExternalStatement:
    """A statement that agrees with `funded_ledger`."""
    defaults: dict[str, Any] = {
        "as_of": "2026-10-01T16:00:00-04:00",
        "source": "CUSTODIAN",
        "cash": Decimal("90000"),
        "positions": (
            ExternalPosition(identity=A, quantity=Decimal("100")),
            ExternalPosition(identity=B, quantity=Decimal("200")),
        ),
    }
    defaults.update(overrides)
    return ExternalStatement(**defaults)


@pytest.mark.integration
def test_an_agreeing_statement_reconciles() -> None:
    result = reconcile(replay(funded_ledger()), agreeing_statement())

    assert result.reconciled
    assert result.breaks == ()


@pytest.mark.integration
def test_a_cash_difference_is_a_break() -> None:
    result = reconcile(replay(funded_ledger()), agreeing_statement(cash=Decimal("89000")))

    assert [row.code for row in result.breaks] == [Code.CASH_MISMATCH]
    assert result.breaks[0].difference == "-1000.0000"


@pytest.mark.integration
def test_a_quantity_difference_is_a_break() -> None:
    result = reconcile(
        replay(funded_ledger()),
        agreeing_statement(
            positions=(
                ExternalPosition(identity=A, quantity=Decimal("150")),
                ExternalPosition(identity=B, quantity=Decimal("200")),
            )
        ),
    )

    assert [row.code for row in result.breaks] == [Code.POSITION_QUANTITY_MISMATCH]


@pytest.mark.integration
def test_a_holding_only_the_custodian_reports_is_a_break() -> None:
    result = reconcile(
        replay(funded_ledger()),
        agreeing_statement(
            positions=(
                ExternalPosition(identity=A, quantity=Decimal("100")),
                ExternalPosition(identity=B, quantity=Decimal("200")),
                ExternalPosition(identity=C, quantity=Decimal("10")),
            )
        ),
    )

    assert [row.code for row in result.breaks] == [Code.EXTERNAL_POSITION_MISSING_INTERNAL]


@pytest.mark.integration
def test_a_holding_only_the_ledger_reports_is_a_break() -> None:
    result = reconcile(
        replay(funded_ledger()),
        agreeing_statement(positions=(ExternalPosition(identity=A, quantity=Decimal("100")),)),
    )

    assert [row.code for row in result.breaks] == [Code.INTERNAL_POSITION_MISSING_EXTERNAL]


@pytest.mark.integration
def test_an_incomplete_statement_never_reconciles_cleanly() -> None:
    result = reconcile(replay(funded_ledger()), agreeing_statement(complete=False))

    assert not result.reconciled
    assert [row.code for row in result.breaks] == [Code.INCOMPLETE_EXTERNAL]


@pytest.mark.integration
def test_an_unestablished_ledger_cannot_be_reconciled_against() -> None:
    result = reconcile(PortfolioState(), agreeing_statement())

    assert Code.INCOMPLETE_INTERNAL in [row.code for row in result.breaks]


@pytest.mark.integration
def test_reconciliation_leaves_the_state_untouched() -> None:
    ledger = funded_ledger()
    state = replay(ledger)
    before = state.as_json()

    reconcile(state, agreeing_statement(cash=Decimal("1")))

    assert state.as_json() == before


@pytest.mark.integration
def test_a_cash_tolerance_is_honoured_but_never_defaulted_to_something_permissive() -> None:
    state = replay(funded_ledger())

    assert reconcile(
        state, agreeing_statement(cash=Decimal("90000.0001")), cash_tolerance=Decimal("0.01")
    ).reconciled
    assert not reconcile(state, agreeing_statement(cash=Decimal("90000.0001"))).reconciled


@pytest.mark.integration
def test_a_break_names_what_to_investigate_and_never_a_repair() -> None:
    result = reconcile(replay(funded_ledger()), agreeing_statement(cash=Decimal("1")))
    body = result.as_json()

    assert body["auto_repair"] == "NOT_IMPLEMENTED_BY_DESIGN"
    assert "never adopts the external figure" in body["remediation"]
    assert result.breaks[0].likely_causes


@pytest.mark.integration
def test_the_reconciliation_module_exposes_no_repair_function() -> None:
    source = (ACCOUNTING_SOURCE / "reconciliation.py").read_text(encoding="utf-8")
    names = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert not {name for name in names if "repair" in name or "adopt" in name}


# --------------------------------------------------------------------------- #
# The risk adapter
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_valued_portfolio_crosses_into_the_risk_layer_with_its_nav_intact() -> None:
    valued = value_portfolio(replay(funded_ledger()), marks(A="60", B="30"))

    snapshot = to_risk_snapshot(valued)

    assert snapshot.nav == float(valued.nav)
    assert snapshot.as_of == valued.valuation_time


@pytest.mark.integration
def test_the_risk_snapshot_binds_the_ledger_head_as_its_price_provenance() -> None:
    state = replay(funded_ledger())
    valued = value_portfolio(state, marks(A="60", B="30"))

    assert to_risk_snapshot(valued).price_fingerprint == state.ledger_head_hash


@pytest.mark.integration
def test_the_seam_is_one_directional_and_says_so() -> None:
    policy = seam_policy()

    assert policy["direction"] == "ACCOUNTING_TO_RISK_ONLY"
    assert "only a FillEvent" in policy["targets_never_become_holdings"]


@pytest.mark.integration
def test_the_adapter_exposes_no_function_that_writes_a_target_into_a_holding() -> None:
    source = (ACCOUNTING_SOURCE / "adapter.py").read_text(encoding="utf-8")
    names = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    assert not {name for name in names if "target" in name or "execute" in name}


@pytest.mark.integration
def test_a_non_finite_figure_cannot_cross_into_risk_arithmetic() -> None:
    with pytest.raises(AccountingError, match="not finite"):
        to_float(Decimal("NaN"))


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_stored_ledger_round_trips_every_accounting_figure(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    ledger = funded_ledger()
    for record in ledger.records:
        store.append(record)

    reloaded = store.load_ledger()

    assert reloaded.head_hash == ledger.head_hash
    assert replay(reloaded).cash == replay(ledger).cash
    assert replay(reloaded).total_cost_basis == replay(ledger).total_cost_basis


@pytest.mark.integration
def test_the_display_symbol_is_deliberately_not_persisted(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    for record in funded_ledger().records:
        store.append(record)

    position = replay(store.load_ledger()).position(SecurityIdentity(kind="COMPANY", identity_id=1))

    assert position.identity.symbol is None
    assert position.identity.key == A.key


@pytest.mark.integration
def test_appending_the_same_record_twice_is_a_no_op(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    record = funded_ledger().records[0]

    store.append(record)
    store.append(record)

    assert len(store.record_paths()) == 1


@pytest.mark.integration
def test_overwriting_a_stored_record_is_refused(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    record = funded_ledger().records[0]
    store.append(record)

    with pytest.raises(AccountingError, match=Code.PARTIAL_EVENT_RECORD):
        store.append(dataclasses.replace(record, record_hash="f" * 64))


@pytest.mark.integration
def test_tampering_with_a_stored_record_is_detected_on_load(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    for record in funded_ledger().records:
        store.append(record)
    path = store.record_paths()[1]
    body = json.loads(path.read_text(encoding="utf-8"))
    body["event"]["payload"]["price"] = "1"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(AccountingError, match=Code.LEDGER_HASH_CHAIN_MISMATCH):
        store.load_ledger()


@pytest.mark.integration
def test_deleting_a_stored_record_is_detected_on_load(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    for record in funded_ledger().records:
        store.append(record)
    store.record_paths()[1].unlink()

    with pytest.raises(AccountingError, match=Code.LEDGER_SEQUENCE_GAP):
        store.load_ledger()


@pytest.mark.integration
def test_truncating_a_ledger_leaves_a_valid_chain_that_a_checkpoint_catches(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    ledger = funded_ledger()
    for record in ledger.records:
        store.append(record)
    store.write_checkpoint(
        Checkpoint(
            sequence=len(ledger),
            head_hash=ledger.head_hash,
            record_count=len(ledger),
            taken_at="2026-10-01T16:05:00-04:00",
        )
    )
    store.record_paths()[-1].unlink()

    assert verify_chain(store.load_ledger().records)["verified"]
    with pytest.raises(AccountingError, match=Code.CHECKPOINT_UNVERIFIABLE):
        store.verify()


@pytest.mark.integration
def test_a_rewritten_history_fails_its_checkpoint(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    ledger = funded_ledger()
    for record in ledger.records:
        store.append(record)
    store.write_checkpoint(
        Checkpoint(
            sequence=len(ledger),
            head_hash="a" * 64,
            record_count=len(ledger),
            taken_at="2026-10-01T16:05:00-04:00",
        )
    )

    with pytest.raises(AccountingError, match="rewritten"):
        store.verify()


@pytest.mark.integration
def test_a_clean_store_verifies(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    ledger = funded_ledger()
    for record in ledger.records:
        store.append(record)
    store.write_checkpoint(
        Checkpoint(
            sequence=len(ledger),
            head_hash=ledger.head_hash,
            record_count=len(ledger),
            taken_at="2026-10-01T16:05:00-04:00",
        )
    )

    result = store.verify()

    assert result["verified"]
    assert result["checkpoint_count"] == 1


@pytest.mark.integration
def test_a_reconciliation_report_is_kept_whether_or_not_it_reconciled(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / ".portfolio")
    result = reconcile(replay(funded_ledger()), agreeing_statement(cash=Decimal("1")))

    path = store.write_reconciliation(result, name="2026-10-01")

    assert json.loads(path.read_text(encoding="utf-8"))["reconciled"] is False


@pytest.mark.integration
@pytest.mark.parametrize("fragment", FORBIDDEN_ROOTS)
def test_a_store_cannot_be_rooted_in_a_research_namespace(fragment: str) -> None:
    with pytest.raises(AccountingError, match=Code.FORBIDDEN_LEDGER_NAMESPACE):
        PortfolioStore(Path("some") / fragment / "portfolio")


@pytest.mark.integration
def test_an_unknown_event_type_cannot_be_loaded_from_storage() -> None:
    with pytest.raises(AccountingError, match=Code.EVENT_INVALID):
        event_from_json(
            {
                "event_id": "X",
                "event_type": "SOMETHING_ELSE",
                "effective_at": T0,
                "source_id": "S",
                "source_fingerprint": "fp",
                "payload": {},
            }
        )


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_float_becomes_the_decimal_it_reads_as_not_its_binary_expansion() -> None:
    assert as_decimal(0.1) == Decimal("0.1")


@pytest.mark.integration
def test_a_dust_quantity_counts_as_zero() -> None:
    assert is_zero(Decimal("0.000000001"))
    assert not is_zero(Decimal("0.00000001"))


@pytest.mark.integration
def test_the_money_policy_is_named() -> None:
    assert money_policy()["policy_id"] == "PORTFOLIO_ACCOUNTING_DECIMAL_V1"


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_manifest_fingerprint_is_stable() -> None:
    assert (
        accounting_manifest()["manifest_fingerprint"]
        == (accounting_manifest()["manifest_fingerprint"])
    )


@pytest.mark.integration
def test_the_manifest_names_every_policy_in_the_package() -> None:
    policies = accounting_manifest()["policies"]

    assert set(policies) == {
        "accounting_engine",
        "corporate_actions",
        "cost_basis",
        "ledger_chain",
        "mark_admissibility",
        "money",
        "pnl_decomposition",
        "reconciliation",
        "risk_adapter",
        "store",
        "strategy_attribution",
    }


@pytest.mark.integration
def test_every_declared_error_code_is_in_the_registry() -> None:
    declared = {value for key, value in vars(Code).items() if not key.startswith("_")}

    assert declared == set(ACCOUNTING_ERROR_CODES)


@pytest.mark.integration
def test_the_manifest_states_what_the_engine_never_does() -> None:
    never = accounting_manifest()["never_does"]

    assert any("place or cancel an order" in line for line in never)
    assert any("simulate a fill" in line for line in never)
    assert any("count an external cash flow as P&L" in line for line in never)
    assert any("adopting the external figure" in line for line in never)


# --------------------------------------------------------------------------- #
# The accounting core reaches nothing
# --------------------------------------------------------------------------- #

CORE_MODULES = (
    "accounting.py",
    "attribution.py",
    "corporate_actions.py",
    "errors.py",
    "events.py",
    "ledger.py",
    "money.py",
    "pnl.py",
    "positions.py",
    "reconciliation.py",
    "state.py",
    "valuation.py",
)


def imported_modules(path: Path) -> set[str]:
    """Every module name a file imports."""
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
def test_the_accounting_core_imports_no_network_or_database_module(name: str) -> None:
    forbidden = {
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "urllib.request",
        "sqlite3",
        "sqlalchemy",
        "alembic",
        "alpaca",
    }

    assert not imported_modules(ACCOUNTING_SOURCE / name) & forbidden


@pytest.mark.integration
@pytest.mark.parametrize("name", CORE_MODULES)
def test_the_accounting_core_mentions_no_order_submission(name: str) -> None:
    source = (ACCOUNTING_SOURCE / name).read_text(encoding="utf-8")
    identifiers = (
        {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
        | {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}
        | {
            node.name
            for node in ast.walk(ast.parse(source))
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
    }


@pytest.mark.integration
def test_the_whole_package_imports_no_research_or_strategy_module() -> None:
    for path in sorted(ACCOUNTING_SOURCE.glob("*.py")):
        imports = imported_modules(path)
        assert not {name for name in imports if "research" in name}
        assert not {name for name in imports if "membership_hypothesis" in name}
        assert not {name for name in imports if "prospective" in name}


@pytest.mark.integration
def test_the_package_reads_no_clock_in_any_semantic_path() -> None:
    for name in CORE_MODULES:
        source = (ACCOUNTING_SOURCE / name).read_text(encoding="utf-8")
        calls = {
            node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "now" not in calls, name
        assert "today" not in calls, name


# --------------------------------------------------------------------------- #
# The goldens
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_example_ledger_reproduces_its_golden_output() -> None:
    request = load_request(FIXTURES / "example_ledger.json")
    golden = json.loads((FIXTURES / "example_ledger.golden.json").read_text(encoding="utf-8"))

    assert evaluate_request(request) == golden


@pytest.mark.integration
def test_the_reconciliation_example_reproduces_its_golden_output() -> None:
    request = load_request(FIXTURES / "reconciliation_example.json")
    golden = json.loads(
        (FIXTURES / "reconciliation_example.golden.json").read_text(encoding="utf-8")
    )

    assert evaluate_request(request) == golden


@pytest.mark.integration
def test_the_reconciliation_example_reports_all_four_break_kinds() -> None:
    books = evaluate_request(load_request(FIXTURES / "reconciliation_example.json"))

    assert {row["code"] for row in books["reconciliation"]["breaks"]} == {
        Code.CASH_MISMATCH,
        Code.POSITION_QUANTITY_MISMATCH,
        Code.EXTERNAL_POSITION_MISSING_INTERNAL,
        Code.INTERNAL_POSITION_MISSING_EXTERNAL,
    }


@pytest.mark.integration
def test_a_request_with_no_events_is_refused_rather_than_reported_as_zeroes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"events": []}), encoding="utf-8")

    with pytest.raises(AccountingError, match=Code.LEDGER_EMPTY):
        load_request(path)
