"""P1.0: the portfolio and risk engine foundation.

These tests exist to establish one property above all others: a strategy cannot
get a position the risk policy does not permit. Everything else — the caps, the
counts, the modes, the fingerprints — is in service of that, so the suite checks
the authority boundary structurally as well as behaviourally.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from stock_screener.portfolio_risk import (
    EXAMPLE_POLICY_V1,
    REASON_CODES,
    Action,
    ConstructionMode,
    Decision,
    IntentError,
    InvariantViolationError,
    PolicyError,
    PolicyMode,
    PortfolioSnapshot,
    PortfolioStateError,
    Position,
    ReasonCode,
    RiskPolicy,
    SecurityIdentity,
    StrategyIntent,
    ValidationError,
    evaluate,
    evaluate_request,
    intents_fingerprint,
)

AS_OF = "2026-10-01T16:00:00-04:00"
NAV = 1_000_000.0
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "portfolio_risk"


def _identity(number: int, symbol: str | None = None) -> SecurityIdentity:
    return SecurityIdentity(kind="COMPANY", identity_id=number, symbol=symbol or f"T{number:02d}")


def _intent(
    number: int,
    weight: float,
    *,
    priority: float | None = None,
    strategy: str = "S",
    decision: str = "d1",
    as_of: str = AS_OF,
    symbol: str | None = None,
    notional: float | None = None,
    fingerprint: str | None = "fp",
) -> StrategyIntent:
    return StrategyIntent(
        strategy_id=strategy,
        strategy_version="1",
        decision_id=decision,
        as_of=as_of,
        identity=_identity(number, symbol),
        requested_weight=None if notional is not None else weight,
        requested_notional=notional,
        priority=float(100 - number) if priority is None else priority,
        reason="test",
        source_fingerprint=fingerprint,
    )


def _snapshot(
    held: dict[int, float] | None = None, *, cash: float | None = None
) -> PortfolioSnapshot:
    """A snapshot whose positions sit at the requested weights of NAV."""
    held = held or {}
    positions = tuple(
        Position(identity=_identity(number), quantity=weight * NAV / 100.0, mark_price=100.0)
        for number, weight in sorted(held.items())
    )
    invested = sum(position.market_value for position in positions)
    return PortfolioSnapshot(
        as_of=AS_OF,
        cash=NAV - invested if cash is None else cash,
        positions=positions,
        price_fingerprint="prices",
    )


def _policy(**overrides: Any) -> RiskPolicy:
    base: dict[str, Any] = {
        "policy_id": "TEST_POLICY",
        "policy_version": "1",
        "long_only": True,
        "max_gross_exposure": 1.00,
        "max_position_weight": 0.10,
        "max_positions": 20,
        "min_cash_weight": 0.05,
        "production_authorized": False,
    }
    return RiskPolicy(**{**base, **overrides})


def _by_identity(decision: Any) -> dict[int, Any]:
    return {row.identity.identity_id: row for row in decision.approved.risk_decisions and ()} or {
        int(row["identity"]["identity_id"]): row for row in decision.approved.risk_decisions
    }


def _actions(decision: Any) -> dict[int, str]:
    return {
        intent.identity.identity_id: str(intent.action) for intent in decision.execution_intents
    }


def _targets(decision: Any) -> dict[int, float]:
    return {
        intent.identity.identity_id: intent.target_weight for intent in decision.execution_intents
    }


# --------------------------------------------------------------------------- #
# §52 a clean request passes through unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_ten_intents_within_every_limit_are_approved_unchanged() -> None:
    intents = [_intent(number, 0.05) for number in range(1, 11)]

    decision = evaluate(intents, _snapshot(), _policy())

    assert decision.risk_summary["approved_unchanged_count"] == 10
    assert decision.risk_summary["resized_count"] == 0
    assert decision.risk_summary["rejected_count"] == 0
    assert decision.approved.gross_exposure == 0.5
    assert decision.approved.cash_target_weight == 0.5
    assert all(
        row["decision"] == str(Decision.APPROVED) for row in decision.approved.risk_decisions
    )


# --------------------------------------------------------------------------- #
# §53 per-name cap
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_oversized_request_is_resized_to_the_position_cap() -> None:
    decision = evaluate([_intent(1, 0.25)], _snapshot(), _policy(max_position_weight=0.10))
    row = _by_identity(decision)[1]

    assert row["requested_weight"] == 0.25
    assert row["approved_weight"] == 0.10
    assert row["decision"] == str(Decision.RESIZED)
    assert ReasonCode.MAX_POSITION_WEIGHT in row["reason_codes"]


@pytest.mark.integration
def test_the_cap_is_never_exceeded_however_many_names_ask() -> None:
    intents = [_intent(number, 0.50) for number in range(1, 6)]

    decision = evaluate(intents, _snapshot(), _policy(max_position_weight=0.10))

    assert all(weight <= 0.10 for weight in decision.approved.targets.values())


# --------------------------------------------------------------------------- #
# §54 position count, deterministically
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_more_names_than_the_count_cap_keeps_the_highest_priority_set() -> None:
    intents = [_intent(number, 0.05, priority=float(100 - number)) for number in range(1, 11)]

    decision = evaluate(intents, _snapshot(), _policy(max_positions=4))
    rows = _by_identity(decision)

    assert len(decision.approved.targets) == 4
    assert sorted(int(label.split(":")[1]) for label in decision.approved.targets) == [1, 2, 3, 4]
    for number in (5, 6, 7, 8, 9, 10):
        assert rows[number]["decision"] == str(Decision.REJECTED)
        assert ReasonCode.MAX_POSITION_COUNT in rows[number]["reason_codes"]


@pytest.mark.integration
def test_equal_priority_is_broken_by_durable_identity_not_insertion_order() -> None:
    forward = [_intent(number, 0.05, priority=1.0) for number in (9, 2, 7, 4)]
    reversed_order = list(reversed(forward))

    first = evaluate(forward, _snapshot(), _policy(max_positions=2))
    second = evaluate(reversed_order, _snapshot(), _policy(max_positions=2))

    assert sorted(first.approved.targets) == ["COMPANY:2", "COMPANY:4"]
    assert first.approved.fingerprint() == second.approved.fingerprint()


# --------------------------------------------------------------------------- #
# §55 cash reserve, §56 gross exposure
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_fully_invested_request_leaves_the_cash_reserve_intact() -> None:
    intents = [_intent(number, 0.10) for number in range(1, 11)]

    decision = evaluate(intents, _snapshot(), _policy(min_cash_weight=0.05))

    assert decision.approved.gross_exposure <= 0.95
    assert decision.approved.cash_target_weight >= 0.05
    assert decision.approved.cash_target_weight == pytest.approx(0.05)


@pytest.mark.integration
def test_an_oversubscribed_request_is_scaled_into_the_budget() -> None:
    # 14 names at 10% is 1.40 requested against a 0.95 budget.
    intents = [_intent(number, 0.10) for number in range(1, 15)]

    decision = evaluate(intents, _snapshot(), _policy(max_positions=20, min_cash_weight=0.05))

    assert decision.risk_summary["requested_gross_exposure"] == pytest.approx(1.40)
    assert decision.approved.gross_exposure == pytest.approx(0.95)
    assert decision.approved.cash_target_weight == pytest.approx(0.05)
    assert all(
        ReasonCode.BUDGET_SCALED in row["reason_codes"]
        for row in decision.approved.risk_decisions
        if row["approved_weight"] > 0
    )


@pytest.mark.integration
def test_the_gross_cap_binds_when_it_is_tighter_than_the_cash_reserve() -> None:
    intents = [_intent(number, 0.10) for number in range(1, 11)]

    decision = evaluate(
        intents, _snapshot(), _policy(max_gross_exposure=0.60, min_cash_weight=0.05)
    )

    assert decision.approved.gross_exposure == pytest.approx(0.60)
    assert any(
        ReasonCode.MAX_GROSS_EXPOSURE in row["reason_codes"]
        for row in decision.approved.risk_decisions
    )


# --------------------------------------------------------------------------- #
# §57 exits, §58 resizes
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_holding_the_strategy_no_longer_wants_is_exited() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot({1: 0.05, 99: 0.06}), _policy())

    assert _actions(decision)[99] == str(Action.EXIT)
    assert _targets(decision)[99] == 0.0
    assert "COMPANY:99" not in decision.approved.targets
    assert decision.risk_summary["forced_exit_count"] == 1


@pytest.mark.integration
def test_a_held_position_with_a_larger_target_increases_by_the_delta() -> None:
    decision = evaluate([_intent(1, 0.08)], _snapshot({1: 0.05}), _policy())
    intent = decision.execution_intents[0]

    assert intent.action is Action.INCREASE
    assert intent.current_weight == pytest.approx(0.05)
    assert intent.target_weight == pytest.approx(0.08)
    assert intent.weight_delta == pytest.approx(0.03)
    assert intent.notional_delta == pytest.approx(0.03 * NAV)


@pytest.mark.integration
def test_a_held_position_with_a_smaller_target_reduces_by_the_delta() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot({1: 0.08}), _policy())
    intent = decision.execution_intents[0]

    assert intent.action is Action.REDUCE
    assert intent.weight_delta == pytest.approx(-0.03)


@pytest.mark.integration
def test_a_position_already_at_its_target_is_held() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot({1: 0.05}), _policy())

    assert decision.execution_intents[0].action is Action.HOLD
    assert decision.delta_summary["positions_held"] == 1


# --------------------------------------------------------------------------- #
# §59 REDUCE_ONLY, §60 HALTED
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_reduce_only_blocks_increases_and_entries_but_allows_reductions() -> None:
    held = {1: 0.05, 3: 0.07, 4: 0.06}
    intents = [
        _intent(1, 0.09),  # increase A -> blocked
        _intent(2, 0.05),  # enter B    -> blocked
        _intent(3, 0.04),  # reduce C   -> allowed
    ]  # D (4) absent -> exit, allowed

    decision = evaluate(intents, _snapshot(held), _policy(mode=PolicyMode.REDUCE_ONLY))
    rows = _by_identity(decision)
    actions = _actions(decision)

    assert rows[1]["approved_weight"] == pytest.approx(0.05)
    assert ReasonCode.REDUCE_ONLY in rows[1]["reason_codes"]
    assert actions[1] == str(Action.HOLD)
    assert rows[2]["decision"] == str(Decision.REJECTED)
    assert ReasonCode.REDUCE_ONLY in rows[2]["reason_codes"]
    assert "COMPANY:2" not in decision.approved.targets
    assert actions[3] == str(Action.REDUCE)
    assert actions[4] == str(Action.EXIT)


@pytest.mark.integration
def test_halted_holds_every_position_and_liquidates_nothing() -> None:
    held = {1: 0.05, 2: 0.06}
    intents = [_intent(1, 0.09), _intent(3, 0.05)]

    decision = evaluate(intents, _snapshot(held), _policy(mode=PolicyMode.HALTED))
    actions = _actions(decision)

    assert decision.approved.targets == {"COMPANY:1": 0.05, "COMPANY:2": 0.06}
    assert set(actions.values()) == {str(Action.HOLD)}
    assert str(Action.EXIT) not in actions.values()
    assert decision.delta_summary["positions_exited"] == 0
    assert decision.delta_summary["weight_turnover_estimate"] == 0.0


@pytest.mark.integration
def test_halted_rejects_every_request_without_emitting_a_sale() -> None:
    decision = evaluate([_intent(5, 0.10)], _snapshot({1: 0.05}), _policy(mode=PolicyMode.HALTED))
    rows = _by_identity(decision)

    assert rows[5]["decision"] == str(Decision.REJECTED)
    assert ReasonCode.TRADING_HALTED in rows[5]["reason_codes"]
    assert all(intent.notional_delta == 0.0 for intent in decision.execution_intents)


# --------------------------------------------------------------------------- #
# §61 duplicates, §62 ticker collision, §63 multi-strategy
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_two_intents_for_one_identity_are_refused_rather_than_summed() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.DUPLICATE_STRATEGY_INTENT) as caught:
        evaluate([_intent(1, 0.04), _intent(1, 0.03)], _snapshot(), _policy())

    assert caught.value.code == ReasonCode.DUPLICATE_STRATEGY_INTENT


@pytest.mark.integration
def test_two_identities_sharing_a_ticker_remain_separate() -> None:
    first, second = _identity(1, "SAME"), _identity(2, "SAME")
    intents = [
        _intent(1, 0.05, symbol="SAME"),
        _intent(2, 0.05, symbol="SAME"),
    ]

    decision = evaluate(intents, _snapshot(), _policy())

    assert first != second
    assert first.key != second.key
    assert len(decision.approved.targets) == 2
    assert sorted(decision.approved.targets) == ["COMPANY:1", "COMPANY:2"]


@pytest.mark.integration
def test_intents_from_two_strategies_are_refused_without_an_allocation_policy() -> None:
    intents = [_intent(1, 0.05, strategy="ALPHA"), _intent(2, 0.05, strategy="BETA")]

    with pytest.raises(ValidationError, match=ReasonCode.MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED):
        evaluate(intents, _snapshot(), _policy())


# --------------------------------------------------------------------------- #
# §64 determinism, §65 input drift, §37 binding
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_identical_inputs_produce_identical_fingerprints() -> None:
    def run() -> Any:
        return evaluate([_intent(n, 0.06) for n in range(1, 9)], _snapshot({1: 0.05}), _policy())

    first, second = run(), run()

    assert first.approved.fingerprint() == second.approved.fingerprint()
    assert first.execution_fingerprint == second.execution_fingerprint
    assert (
        first.manifest["risk_decisions_fingerprint"]
        == second.manifest["risk_decisions_fingerprint"]
    )


@pytest.mark.integration
def test_supplying_intents_in_another_order_changes_nothing() -> None:
    intents = [_intent(n, 0.06) for n in range(1, 9)]

    forward = evaluate(intents, _snapshot(), _policy())
    backward = evaluate(list(reversed(intents)), _snapshot(), _policy())

    assert forward.approved.fingerprint() == backward.approved.fingerprint()
    assert intents_fingerprint(intents) == intents_fingerprint(list(reversed(intents)))


@pytest.mark.integration
@pytest.mark.parametrize(
    "what",
    ["strategy_weight", "current_holding", "policy_limit"],
)
def test_changing_any_one_input_changes_the_output_fingerprint(what: str) -> None:
    intents = [_intent(n, 0.06) for n in range(1, 9)]
    base = evaluate(intents, _snapshot({1: 0.05}), _policy())

    if what == "strategy_weight":
        changed = evaluate([_intent(1, 0.07), *intents[1:]], _snapshot({1: 0.05}), _policy())
    elif what == "current_holding":
        changed = evaluate(intents, _snapshot({1: 0.055}), _policy())
    else:
        changed = evaluate(intents, _snapshot({1: 0.05}), _policy(max_position_weight=0.065))

    assert changed.approved.fingerprint() != base.approved.fingerprint()


@pytest.mark.integration
def test_the_output_binds_all_three_inputs() -> None:
    snapshot, policy = _snapshot(), _policy()
    intents = [_intent(1, 0.05)]

    decision = evaluate(intents, snapshot, policy)
    bindings = decision.approved.bindings

    assert bindings["strategy_intents_fingerprint"] == intents_fingerprint(intents)
    assert bindings["portfolio_snapshot_fingerprint"] == snapshot.fingerprint()
    assert bindings["risk_policy_fingerprint"] == policy.fingerprint()


@pytest.mark.integration
def test_the_wall_clock_is_not_inside_any_semantic_fingerprint() -> None:
    first = evaluate([_intent(1, 0.05)], _snapshot(), _policy())
    second = evaluate([_intent(1, 0.05)], _snapshot(), _policy())

    assert first.manifest["evaluated_at_utc"] != second.manifest["evaluated_at_utc"]
    assert first.approved.fingerprint() == second.approved.fingerprint()
    assert "evaluated_at_utc" not in json.dumps(first.approved.body())


# --------------------------------------------------------------------------- #
# §23 malformed intents fail closed
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_intent_without_a_source_fingerprint_is_refused() -> None:
    with pytest.raises(IntentError, match="source_fingerprint"):
        _intent(1, 0.05, fingerprint=None)


@pytest.mark.integration
@pytest.mark.parametrize("weight", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_requested_weight_is_refused(weight: float) -> None:
    with pytest.raises(IntentError, match="finite"):
        _intent(1, weight)


@pytest.mark.integration
def test_supplying_both_a_weight_and_a_notional_is_refused() -> None:
    with pytest.raises(IntentError, match="exactly one"):
        StrategyIntent(
            strategy_id="S",
            strategy_version="1",
            decision_id="d1",
            as_of=AS_OF,
            identity=_identity(1),
            requested_weight=0.05,
            requested_notional=50_000.0,
            source_fingerprint="fp",
        )


@pytest.mark.integration
def test_supplying_neither_a_weight_nor_a_notional_is_refused() -> None:
    with pytest.raises(IntentError, match="exactly one"):
        StrategyIntent(
            strategy_id="S",
            strategy_version="1",
            decision_id="d1",
            as_of=AS_OF,
            identity=_identity(1),
            source_fingerprint="fp",
        )


@pytest.mark.integration
def test_a_negative_weight_under_long_only_is_refused_not_clamped() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.LONG_ONLY_VIOLATION):
        evaluate([_intent(1, -0.05)], _snapshot(), _policy(long_only=True))


@pytest.mark.integration
def test_intents_spanning_two_decisions_are_refused() -> None:
    intents = [_intent(1, 0.05, decision="d1"), _intent(2, 0.05, decision="d2")]

    with pytest.raises(ValidationError, match=ReasonCode.DECISION_ID_MISMATCH):
        evaluate(intents, _snapshot(), _policy())


@pytest.mark.integration
def test_intents_dated_after_the_portfolio_snapshot_are_refused() -> None:
    later = [_intent(1, 0.05, as_of="2026-12-31T16:00:00-05:00")]

    with pytest.raises(ValidationError, match=ReasonCode.AS_OF_MISMATCH):
        evaluate(later, _snapshot(), _policy())


@pytest.mark.integration
def test_a_missing_policy_or_snapshot_authorises_nothing() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.RISK_POLICY_UNAVAILABLE):
        evaluate([_intent(1, 0.05)], _snapshot(), None)
    with pytest.raises(ValidationError, match=ReasonCode.INVALID_PORTFOLIO_SNAPSHOT):
        evaluate([_intent(1, 0.05)], None, _policy())


@pytest.mark.integration
def test_a_portfolio_cannot_hold_one_identity_twice() -> None:
    position = Position(identity=_identity(1), quantity=10.0, mark_price=100.0)

    with pytest.raises(PortfolioStateError, match="appears twice"):
        PortfolioSnapshot(as_of=AS_OF, cash=1.0, positions=(position, position))


@pytest.mark.integration
def test_a_portfolio_refuses_negative_cash_and_an_unpriced_position() -> None:
    with pytest.raises(PortfolioStateError, match="may not be negative"):
        PortfolioSnapshot(as_of=AS_OF, cash=-1.0)
    with pytest.raises(PortfolioStateError, match="positive price"):
        Position(identity=_identity(1), quantity=10.0, mark_price=0.0)


@pytest.mark.integration
def test_an_unknown_identity_kind_is_refused() -> None:
    with pytest.raises(PortfolioStateError, match="unknown identity kind"):
        SecurityIdentity(kind="CRYPTO", identity_id=1)


# --------------------------------------------------------------------------- #
# §16, §35 invariants
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_positions_plus_cash_always_total_one() -> None:
    for count, weight in ((1, 0.10), (8, 0.09), (14, 0.10), (20, 0.07)):
        intents = [_intent(n, weight) for n in range(1, count + 1)]
        decision = evaluate(intents, _snapshot(), _policy(max_positions=20))
        total = sum(decision.approved.targets.values()) + decision.approved.cash_target_weight
        assert total == pytest.approx(1.0, abs=1e-7), (count, weight, total)


@pytest.mark.integration
def test_cash_is_never_implicitly_fully_invested() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy())

    assert decision.approved.cash_target_weight == pytest.approx(0.95)
    assert decision.approved.gross_exposure == pytest.approx(0.05)


@pytest.mark.integration
def test_an_invariant_breach_raises_rather_than_returning_a_degraded_portfolio() -> None:
    from stock_screener.portfolio_risk.engine import _assert_invariants
    from stock_screener.portfolio_risk.risk import RiskOutcome

    policy = _policy(max_position_weight=0.10)
    outcome = RiskOutcome(
        decisions=(),
        approved={("COMPANY", 1): 0.40},
        identities={("COMPANY", 1): _identity(1)},
        cash_weight=0.60,
        policy=policy,
    )

    with pytest.raises(InvariantViolationError, match="exceeds max_position_weight"):
        _assert_invariants(outcome, policy)


@pytest.mark.integration
def test_an_invariant_breach_names_every_violation_at_once() -> None:
    from stock_screener.portfolio_risk.engine import _assert_invariants
    from stock_screener.portfolio_risk.risk import RiskOutcome

    policy = _policy(max_position_weight=0.10, max_positions=1, min_cash_weight=0.05)
    outcome = RiskOutcome(
        decisions=(),
        approved={("COMPANY", 1): 0.60, ("COMPANY", 2): 0.60},
        identities={("COMPANY", 1): _identity(1), ("COMPANY", 2): _identity(2)},
        cash_weight=0.0,
        policy=policy,
    )

    with pytest.raises(InvariantViolationError) as caught:
        _assert_invariants(outcome, policy)
    message = str(caught.value)

    for expected in (
        "exceeds max_position_weight",
        "exceeds max_positions",
        "below min_cash_weight",
    ):
        assert expected in message


# --------------------------------------------------------------------------- #
# §46 policy validation, §13 versioning
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"min_cash_weight": 1.5}, "between 0 and 1"),
        ({"max_gross_exposure": -1.0}, "may not be negative"),
        ({"max_position_weight": 0.0}, "must be positive"),
        ({"max_positions": 0}, "at least 1"),
        ({"min_position_weight": 0.5, "max_position_weight": 0.1}, "no greater than"),
        ({"max_trade_notional_weight": -0.1}, "must be positive"),
    ],
)
def test_an_inconsistent_policy_is_refused(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(PolicyError, match=match):
        _policy(**overrides)


@pytest.mark.integration
def test_changing_any_limit_changes_the_policy_fingerprint() -> None:
    base = _policy()

    assert _policy(max_position_weight=0.11).fingerprint() != base.fingerprint()
    assert _policy(max_positions=19).fingerprint() != base.fingerprint()
    assert _policy(min_cash_weight=0.06).fingerprint() != base.fingerprint()
    assert _policy(mode=PolicyMode.REDUCE_ONLY).fingerprint() != base.fingerprint()
    assert _policy().fingerprint() == base.fingerprint()


@pytest.mark.integration
def test_the_example_policy_is_not_production_authorised() -> None:
    assert EXAMPLE_POLICY_V1.production_authorized is False
    assert EXAMPLE_POLICY_V1.policy_id == "EXAMPLE_POLICY_V1"
    assert EXAMPLE_POLICY_V1.policy_version == "1"


@pytest.mark.integration
def test_every_decision_records_the_policy_that_authorised_it() -> None:
    policy = _policy()
    decision = evaluate([_intent(1, 0.05)], _snapshot(), policy)

    for row in decision.approved.risk_decisions:
        assert row["policy_id"] == policy.policy_id
        assert row["policy_version"] == policy.policy_version
    assert decision.manifest["risk_policy_fingerprint"] == policy.fingerprint()


# --------------------------------------------------------------------------- #
# §49, §50 normalisation modes
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_underinvested_request_is_preserved_by_default() -> None:
    intents = [_intent(n, 0.10) for n in range(1, 7)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(construction_mode=ConstructionMode.PRESERVE_ABSOLUTE_WEIGHTS),
    )

    assert decision.approved.gross_exposure == pytest.approx(0.60)
    assert decision.approved.cash_target_weight == pytest.approx(0.40)


@pytest.mark.integration
def test_the_normalising_mode_fills_the_budget_without_breaching_the_cap() -> None:
    intents = [_intent(n, 0.06) for n in range(1, 11)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(construction_mode=ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET),
    )

    assert decision.approved.gross_exposure == pytest.approx(0.95)
    assert decision.approved.cash_target_weight == pytest.approx(0.05)
    assert all(weight <= 0.10 + 1e-9 for weight in decision.approved.targets.values())


@pytest.mark.integration
def test_the_normalising_mode_stops_when_every_name_is_capped() -> None:
    """The policy has capacity for the budget, but this request does not use it.

    Five names at a 0.10 cap can only reach 0.50, so the redistribution loop runs
    out of headroom and holds the remainder as cash. It neither spins nor breaches
    the cap to reach a budget the request cannot fill.
    """
    intents = [_intent(n, 0.02) for n in range(1, 6)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(
            construction_mode=ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET,
            max_positions=10,
            max_position_weight=0.10,
            min_cash_weight=0.05,
        ),
    )

    assert decision.approved.gross_exposure == pytest.approx(0.50)
    assert all(weight == pytest.approx(0.10) for weight in decision.approved.targets.values())
    assert decision.approved.cash_target_weight == pytest.approx(0.50)


@pytest.mark.integration
def test_the_normalising_mode_is_declared_not_inferred() -> None:
    # Weights the per-name cap does not bind on, so the modes genuinely diverge
    # rather than both being pulled back to the same capped total.
    intents = [_intent(n, 0.05) for n in range(1, 11)]
    preserved = evaluate(intents, _snapshot(), _policy())
    normalised = evaluate(
        intents,
        _snapshot(),
        _policy(construction_mode=ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET),
    )

    # Identical numbers, opposite treatment: only the declared mode separates them.
    assert preserved.approved.gross_exposure == pytest.approx(0.50)
    assert normalised.approved.gross_exposure == pytest.approx(0.95)


# --------------------------------------------------------------------------- #
# §22 cash reserve, §18 dust, §5 trade size
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_target_below_the_dust_floor_is_dropped() -> None:
    intents = [_intent(1, 0.10), _intent(2, 0.004)]

    decision = evaluate(intents, _snapshot(), _policy(min_position_weight=0.01))
    rows = _by_identity(decision)

    assert rows[2]["decision"] == str(Decision.REJECTED)
    assert ReasonCode.MIN_POSITION_WEIGHT in rows[2]["reason_codes"]
    assert "COMPANY:2" not in decision.approved.targets


@pytest.mark.integration
def test_a_trade_larger_than_the_trade_cap_is_pulled_back_toward_the_holding() -> None:
    decision = evaluate(
        [_intent(1, 0.10)], _snapshot({1: 0.02}), _policy(max_trade_notional_weight=0.03)
    )
    rows = _by_identity(decision)

    assert rows[1]["approved_weight"] == pytest.approx(0.05)
    assert ReasonCode.MAX_TRADE_NOTIONAL in rows[1]["reason_codes"]
    assert decision.execution_intents[0].weight_delta == pytest.approx(0.03)


# --------------------------------------------------------------------------- #
# §68, §69, §70 summaries
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_risk_summary_reports_every_deterministic_count() -> None:
    intents = [_intent(1, 0.25), *(_intent(n, 0.05) for n in range(2, 8))]

    decision = evaluate(intents, _snapshot({99: 0.04}), _policy(max_positions=5))
    summary = decision.risk_summary

    for key in (
        "requested_gross_exposure",
        "approved_gross_exposure",
        "requested_position_count",
        "approved_count",
        "cash_target_weight",
        "approved_unchanged_count",
        "resized_count",
        "rejected_count",
        "forced_exit_count",
    ):
        assert key in summary, key
    assert summary["requested_position_count"] == 7
    assert summary["approved_count"] == 5
    assert summary["forced_exit_count"] == 1


@pytest.mark.integration
def test_the_delta_summary_counts_each_action_and_names_its_turnover() -> None:
    intents = [_intent(1, 0.08), _intent(2, 0.03), _intent(3, 0.05), _intent(4, 0.05)]

    decision = evaluate(intents, _snapshot({1: 0.05, 2: 0.06, 4: 0.05, 99: 0.04}), _policy())
    delta = decision.delta_summary

    assert delta["positions_entered"] == 1
    assert delta["positions_increased"] == 1
    assert delta["positions_reduced"] == 1
    assert delta["positions_held"] == 1
    assert delta["positions_exited"] == 1
    assert delta["turnover_definition"] == "PORTFOLIO_WEIGHT_TURNOVER_ESTIMATE_V1"
    assert delta["is_realised_execution_turnover"] is False


@pytest.mark.integration
def test_the_turnover_estimate_is_half_the_absolute_weight_change() -> None:
    decision = evaluate([_intent(1, 0.09)], _snapshot({1: 0.05}), _policy())

    assert decision.delta_summary["weight_turnover_estimate"] == pytest.approx(0.5 * 0.04)


@pytest.mark.integration
def test_every_reason_code_emitted_is_one_a_consumer_could_be_written_against() -> None:
    intents = [_intent(1, 0.25), *(_intent(n, 0.10) for n in range(2, 15))]

    decision = evaluate(intents, _snapshot({99: 0.04}), _policy(max_positions=10))

    for row in decision.approved.risk_decisions:
        for code in row["reason_codes"]:
            assert code in REASON_CODES, code


# --------------------------------------------------------------------------- #
# §4, §41, §85 the authority boundary, checked structurally
# --------------------------------------------------------------------------- #

PACKAGE = REPO_ROOT / "src" / "stock_screener" / "portfolio_risk"

#: Anything that would let a strategy overrule the risk engine.
BYPASS_NAMES: frozenset[str] = frozenset(
    {
        "force",
        "force_approve",
        "ignore_risk",
        "ignore_limits",
        "bypass",
        "bypass_limits",
        "override",
        "override_risk",
        "skip_risk",
        "unchecked",
    }
)

#: Anything that could reach a broker.
BROKER_NAMES: frozenset[str] = frozenset(
    {
        "submit_order",
        "place_order",
        "create_order",
        "cancel_order",
        "TradingClient",
        "AlpacaTrading",
        "OrderRequest",
        "MarketOrderRequest",
        "LimitOrderRequest",
        "alpaca",
        "requests",
        "httpx",
        "urllib",
        "socket",
    }
)


def _trees() -> dict[Path, ast.Module]:
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(PACKAGE.glob("*.py"))
    }


@pytest.mark.integration
def test_no_public_function_accepts_a_risk_bypass_argument() -> None:
    offenders: list[str] = []
    for path, tree in _trees().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args
            names = [
                arg.arg
                for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                if arg.arg in BYPASS_NAMES
            ]
            offenders.extend(f"{path.name}:{node.name}({name})" for name in names)

    assert offenders == [], (
        f"the strategy-facing surface exposes a risk bypass: {offenders}. An override "
        "that exists is an override that gets used during the incident it worsens."
    )


@pytest.mark.integration
def test_the_package_cannot_reach_a_broker_or_the_network() -> None:
    reachable: set[str] = set()
    for tree in _trees().values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                reachable.add(node.id)
            elif isinstance(node, ast.Attribute):
                reachable.add(node.attr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    reachable.update(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    reachable.update(node.module.split("."))
                for alias in node.names:
                    reachable.add(alias.name)

    assert not (reachable & BROKER_NAMES), sorted(reachable & BROKER_NAMES)


@pytest.mark.integration
def test_the_package_imports_no_database_or_repository_module() -> None:
    forbidden = {"sqlite3", "sqlalchemy", "data_access", "Session", "Repository"}
    reachable: set[str] = set()
    for tree in _trees().values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    reachable.update(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom) and node.module:
                reachable.update(node.module.split("."))
                reachable.update(alias.name for alias in node.names)

    assert not (reachable & forbidden), sorted(reachable & forbidden)


@pytest.mark.integration
def test_an_execution_intent_carries_no_broker_field_and_is_not_an_order() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy())
    body = decision.execution_intents[0].as_json()

    assert body["is_an_order"] is False
    assert body["broker_fields"] is None
    assert "quantity" not in body
    assert "shares" not in body
    assert "time_in_force" not in body
    assert "order_type" not in body
    assert decision.manifest["orders_submitted"] == 0
    assert decision.manifest["broker_requests"] == 0


# --------------------------------------------------------------------------- #
# §38, §66, §67 purity
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_engine_completes_with_every_socket_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the portfolio engine attempted a network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    decision = evaluate([_intent(n, 0.06) for n in range(1, 9)], _snapshot({1: 0.05}), _policy())

    assert len(decision.approved.targets) == 8


@pytest.mark.integration
def test_the_engine_completes_with_every_sqlite_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the portfolio engine attempted a database read")

    monkeypatch.setattr(sqlite3, "connect", refuse)

    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy())

    assert decision.approved.cash_target_weight == pytest.approx(0.95)


# --------------------------------------------------------------------------- #
# §75 serialisation, §77 manifest
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_set_cannot_be_fingerprinted() -> None:
    from stock_screener.portfolio_risk import digest

    with pytest.raises(TypeError, match="iteration order"):
        digest({"names": {"a", "b"}})


@pytest.mark.integration
def test_every_artifact_serialises_deterministically() -> None:
    decision = evaluate([_intent(n, 0.05) for n in range(1, 6)], _snapshot({1: 0.05}), _policy())
    body = decision.as_json()
    body["manifest"].pop("evaluated_at_utc")

    first = json.dumps(body, indent=2, sort_keys=True)
    second = json.dumps(body, indent=2, sort_keys=True)

    assert first == second
    assert json.loads(first) == body


@pytest.mark.integration
def test_the_manifest_binds_every_fingerprint() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy(), code_commit="abc123")
    manifest = decision.manifest

    for key in (
        "strategy_intents_fingerprint",
        "portfolio_snapshot_fingerprint",
        "risk_policy_fingerprint",
        "constructed_portfolio_fingerprint",
        "risk_decisions_fingerprint",
        "approved_portfolio_fingerprint",
        "execution_intents_fingerprint",
    ):
        assert manifest[key], key
    assert manifest["code_commit"] == "abc123"
    assert manifest["engine_version"] == "PORTFOLIO_RISK_ENGINE_V1"
    assert manifest["policy_production_authorized"] is False
    assert manifest["approved_portfolio_fingerprint"] == decision.approved.fingerprint()
    assert manifest["execution_intents_fingerprint"] == decision.execution_fingerprint


# --------------------------------------------------------------------------- #
# §7 notional requests, §8 identity
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_notional_request_is_converted_against_nav() -> None:
    decision = evaluate([_intent(1, 0.0, notional=80_000.0)], _snapshot(), _policy())

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.08)


@pytest.mark.integration
def test_a_ticker_never_appears_as_a_key_in_any_artifact() -> None:
    decision = evaluate([_intent(1, 0.05, symbol="AAA")], _snapshot(), _policy())
    body = decision.as_json()

    assert list(body["approved_portfolio"]["targets"]) == ["COMPANY:1"]
    assert "AAA" not in body["approved_portfolio"]["targets"]
    # The symbol survives as display metadata on the intent, and nowhere else.
    assert body["execution_intents"][0]["identity"]["symbol"] == "AAA"


# --------------------------------------------------------------------------- #
# §81, §82 the example fixture and its golden output
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_example_fixture_exercises_every_baseline_behaviour() -> None:
    decision = evaluate_request(FIXTURES / "example_request.json")
    summary, delta = decision.risk_summary, decision.delta_summary

    assert summary["requested_position_count"] == 12
    assert summary["approved_count"] == 10
    assert summary["resized_count"] == 10
    assert summary["rejected_count"] == 2
    assert summary["forced_exit_count"] == 1
    assert delta["positions_increased"] == 1
    assert delta["positions_reduced"] == 1
    assert delta["positions_exited"] == 1
    assert decision.approved.cash_target_weight == pytest.approx(0.05)
    assert decision.approved.gross_exposure == pytest.approx(0.95)


@pytest.mark.integration
def test_the_example_fixture_still_matches_its_golden_output() -> None:
    """The golden makes semantic drift obvious.

    A difference here is a behaviour change. It should be explained and the golden
    re-recorded deliberately, never re-recorded to make the test pass.
    """
    produced = evaluate_request(FIXTURES / "example_request.json").as_json()
    produced["manifest"].pop("evaluated_at_utc")
    expected = json.loads((FIXTURES / "example_request.golden.json").read_text())
    expected.pop("_note", None)
    expected["manifest"].pop("evaluated_at_utc", None)
    # The golden was recorded without a commit; the engine records whatever it is
    # given, so that one operational field is normalised rather than compared.
    produced["manifest"]["code_commit"] = expected["manifest"].get("code_commit")

    assert produced == expected


@pytest.mark.integration
def test_the_example_policy_in_the_fixture_is_labelled_unauthorised() -> None:
    body = json.loads((FIXTURES / "example_request.json").read_text())

    assert body["policy"]["production_authorized"] is False
    assert "NOT_PRODUCTION_AUTHORIZED" in body["note"]


# --------------------------------------------------------------------------- #
# §17, §19, §20, §45 exposure concepts kept distinct
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_gross_and_net_exposure_are_reported_separately_even_when_they_coincide() -> None:
    decision = evaluate([_intent(n, 0.05) for n in range(1, 6)], _snapshot(), _policy())
    body = decision.approved.as_json()

    # Long-only makes them equal here. They remain distinct fields, because a
    # later short-capable policy needs both and a single number would hide one.
    assert body["gross_exposure"] == body["net_exposure"] == pytest.approx(0.25)
    assert "gross_exposure" in body
    assert "net_exposure" in body


@pytest.mark.integration
def test_a_net_exposure_cap_is_validated_and_enforced() -> None:
    intents = [_intent(n, 0.10) for n in range(1, 8)]

    decision = evaluate(intents, _snapshot(), _policy(max_net_exposure=0.70))

    assert decision.approved.net_exposure <= 0.70 + 1e-9


@pytest.mark.integration
def test_a_long_only_policy_cannot_require_negative_net_exposure() -> None:
    with pytest.raises(PolicyError, match="cannot require net exposure below zero"):
        _policy(long_only=True, max_net_exposure=-0.5)


@pytest.mark.integration
def test_the_baseline_never_authorises_leverage() -> None:
    intents = [_intent(n, 0.50) for n in range(1, 11)]

    decision = evaluate(intents, _snapshot(), _policy())

    assert decision.approved.gross_exposure <= 1.0
    assert decision.approved.cash_target_weight >= 0.0


@pytest.mark.integration
def test_halting_is_not_liquidating() -> None:
    """The distinction an operator depends on during an incident.

    A halt stops trading. It must not quietly become a market-wide sell order,
    which is what a kill switch that liquidates would do at the worst moment.
    """
    held = {1: 0.05, 2: 0.06, 3: 0.04}

    halted = evaluate([], _snapshot(held), _policy(mode=PolicyMode.HALTED))
    reduce_only = evaluate([], _snapshot(held), _policy(mode=PolicyMode.REDUCE_ONLY))

    assert sorted(halted.approved.targets) == ["COMPANY:1", "COMPANY:2", "COMPANY:3"]
    assert halted.delta_summary["positions_exited"] == 0
    assert halted.delta_summary["weight_turnover_estimate"] == 0.0
    # REDUCE_ONLY with no intents does exit, because the strategy asked for nothing
    # and exits are risk-reducing. That is the deliberate difference between them.
    assert reduce_only.delta_summary["positions_exited"] == 3


@pytest.mark.integration
def test_the_policy_modes_are_exactly_the_three_defined() -> None:
    assert sorted(str(mode) for mode in PolicyMode) == ["HALTED", "NORMAL", "REDUCE_ONLY"]
    assert _policy(mode=PolicyMode.NORMAL).permits_increase is True
    assert _policy(mode=PolicyMode.REDUCE_ONLY).permits_increase is False
    assert _policy(mode=PolicyMode.REDUCE_ONLY).permits_reduction is True
    assert _policy(mode=PolicyMode.HALTED).permits_increase is False
    assert _policy(mode=PolicyMode.HALTED).permits_reduction is False
