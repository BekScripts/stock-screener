"""P1.2: covariance safety, multi-strategy allocation, correlation concentration.

Two properties these tests exist to establish. First, covariance inputs fail
closed: an indefinite matrix is refused and a negative portfolio variance can
never present itself as zero volatility. Second, several strategies can request
capital without any of them gaining authority over the final economic exposure —
budgets are ceilings, unused capacity becomes cash, and the global risk engine
remains the last word.

Every number here is a test constant. None came from historical performance.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from stock_screener.portfolio_risk import (
    PortfolioSnapshot,
    Position,
    SecurityIdentity,
    StrategyIntent,
    ValidationError,
    evaluate,
)
from stock_screener.portfolio_risk.allocation import (
    EXAMPLE_MULTI_STRATEGY_POLICY_V1,
    BudgetSemantics,
    StrategyAllocationPolicy,
    StrategyBudget,
)
from stock_screener.portfolio_risk.concentration import (
    apply_correlation_concentration,
    correlation_of,
)
from stock_screener.portfolio_risk.linalg import is_positive_semidefinite
from stock_screener.portfolio_risk.policy import MissingMetricPolicy, PolicyError, RiskPolicy
from stock_screener.portfolio_risk.risk_inputs import (
    COVARIANCE_TOLERANCE,
    CovarianceSnapshot,
    MetricState,
    PortfolioRiskSnapshot,
    RiskInputError,
    SecurityRiskMetrics,
)
from stock_screener.portfolio_risk.validation import ReasonCode

if TYPE_CHECKING:
    from collections.abc import Sequence

AS_OF = "2026-10-02T16:00:00-04:00"
NAV = 1_000_000.0
AMPLE_ADV = 500_000_000.0
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "portfolio_risk"

COMPOUNDER = "COMPOUNDER_EXAMPLE"
MOMENTUM = "MOMENTUM_EXAMPLE"
EARNINGS = "EARNINGS_EXAMPLE"


def _identity(number: int) -> SecurityIdentity:
    return SecurityIdentity(kind="COMPANY", identity_id=number, symbol=f"T{number:02d}")


def _intent(
    strategy: str, number: int, weight: float, *, priority: float | None = None
) -> StrategyIntent:
    return StrategyIntent(
        strategy_id=strategy,
        strategy_version="1",
        decision_id="d1",
        as_of=AS_OF,
        identity=_identity(number),
        requested_weight=weight,
        priority=float(100 - number) if priority is None else priority,
        source_fingerprint="fp",
    )


def _snapshot(held: dict[int, float] | None = None) -> PortfolioSnapshot:
    held = held or {}
    positions = tuple(
        Position(identity=_identity(n), quantity=w * NAV / 100.0, mark_price=100.0)
        for n, w in sorted(held.items())
    )
    invested = sum(p.market_value for p in positions)
    return PortfolioSnapshot(as_of=AS_OF, cash=NAV - invested, positions=positions)


def _covariance(
    numbers: Sequence[int], *, volatility: float = 0.20, correlation: float = 0.0
) -> CovarianceSnapshot:
    identities = tuple(_identity(n) for n in numbers)
    matrix = tuple(
        tuple(
            volatility * volatility * (1.0 if i == j else correlation)
            for j in range(len(identities))
        )
        for i in range(len(identities))
    )
    return CovarianceSnapshot(
        identities=identities, matrix=matrix, as_of=AS_OF, source_fingerprint="cov"
    )


def _risk(
    numbers: Sequence[int],
    *,
    nav: float = NAV,
    peak: float = NAV,
    volatility: float = 0.20,
    correlation: float = 0.0,
    adv: float | None = AMPLE_ADV,
    covariance: CovarianceSnapshot | str | None = "default",
) -> PortfolioRiskSnapshot:
    return PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=nav,
        portfolio_peak_nav=peak,
        metrics=tuple(
            SecurityRiskMetrics(
                identity=_identity(n),
                metric_as_of=AS_OF,
                source_fingerprint="m",
                annualized_volatility=volatility,
                volatility_state=MetricState.KNOWN,
                average_daily_dollar_volume=adv,
                liquidity_state=MetricState.KNOWN if adv is not None else MetricState.UNKNOWN,
            )
            for n in numbers
        ),
        covariance=(
            _covariance(numbers, volatility=volatility, correlation=correlation)
            if isinstance(covariance, str)
            else covariance
        ),
        source_fingerprint="risk",
    )


def _policy(**overrides: Any) -> RiskPolicy:
    base: dict[str, Any] = {
        "policy_id": "TEST_P12_POLICY",
        "policy_version": "1",
        "max_gross_exposure": 1.00,
        "max_position_weight": 0.25,
        "max_positions": 20,
        "min_cash_weight": 0.05,
        "production_authorized": False,
    }
    return RiskPolicy(**{**base, **overrides})


def _alloc(*budgets: StrategyBudget, **overrides: Any) -> StrategyAllocationPolicy:
    return StrategyAllocationPolicy(
        allocation_policy_id=overrides.pop("allocation_policy_id", "TEST_ALLOC"),
        allocation_policy_version=overrides.pop("allocation_policy_version", "1"),
        budgets=budgets,
        **overrides,
    )


def _strategy_report(decision: Any) -> dict[str, Any]:
    report: dict[str, Any] | None = decision.strategy_report
    assert report is not None, "a multi-strategy decision must carry a strategy report"
    return report


def _sleeve(decision: Any, strategy: str) -> dict[str, Any]:
    return next(
        row for row in _strategy_report(decision)["sleeves"] if row["strategy_id"] == strategy
    )


# --------------------------------------------------------------------------- #
# §3-§9, §64-§66 the covariance safety gate
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_indefinite_matrix_p1_1_would_have_accepted_is_now_refused() -> None:
    """Symmetric, finite, non-negative diagonal — and indefinite.

    This is the exact shape P1.1 said it could not catch: every check it ran
    passes, and the matrix still implies a portfolio with negative variance.
    """
    matrix = ((0.04, 0.10), (0.10, 0.09))

    assert matrix[0][1] == matrix[1][0]
    assert all(matrix[i][i] >= 0 for i in (0, 1))
    assert all(math.isfinite(v) for row in matrix for v in row)

    with pytest.raises(RiskInputError, match="COVARIANCE_INDEFINITE"):
        CovarianceSnapshot(identities=(_identity(1), _identity(2)), matrix=matrix, as_of=AS_OF)


@pytest.mark.integration
def test_an_indefinite_matrix_produces_no_portfolio_at_all() -> None:
    with pytest.raises(RiskInputError, match="COVARIANCE_INDEFINITE"):
        _risk(
            [1, 2],
            covariance=CovarianceSnapshot(
                identities=(_identity(1), _identity(2)),
                matrix=((0.04, 0.10), (0.10, 0.09)),
                as_of=AS_OF,
            ),
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "matrix"),
    [
        ("perfectly correlated", ((0.04, 0.04), (0.04, 0.04))),
        ("zero-variance component", ((0.04, 0.0), (0.0, 0.0))),
        ("rank-one three-asset", ((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0))),
    ],
)
def test_a_singular_but_valid_psd_matrix_is_accepted(
    name: str, matrix: tuple[tuple[float, ...], ...]
) -> None:
    """Semidefinite is not invalid.

    Perfectly correlated holdings, a duplicated factor and a zero-volatility
    instrument all produce legitimately singular matrices. Rejecting them would
    refuse correct inputs.
    """
    identities = tuple(_identity(n) for n in range(1, len(matrix) + 1))

    snapshot = CovarianceSnapshot(identities=identities, matrix=matrix, as_of=AS_OF)

    assert snapshot.psd.semidefinite is True
    assert snapshot.psd.definite is False
    assert snapshot.psd.rank < len(matrix), name


@pytest.mark.integration
def test_a_zero_pivot_beside_a_nonzero_column_is_indefinite() -> None:
    # [[0, b], [b, c]] has determinant -b^2: indefinite for any non-zero b.
    with pytest.raises(RiskInputError, match="COVARIANCE_INDEFINITE"):
        CovarianceSnapshot(
            identities=(_identity(1), _identity(2)),
            matrix=((0.0, 0.02), (0.02, 0.04)),
            as_of=AS_OF,
        )


@pytest.mark.integration
def test_a_materially_negative_portfolio_variance_fails_closed() -> None:
    """The P1.1 behaviour that is no longer permitted.

    The matrix is constructed to be accepted by the snapshot's own validation
    (it is not), so this exercises `variance_of` directly against a matrix built
    without going through validation — the belt as well as the braces.
    """
    identities = (_identity(1), _identity(2))
    snapshot = CovarianceSnapshot(
        identities=identities, matrix=((0.04, 0.0), (0.0, 0.04)), as_of=AS_OF
    )
    # Replace the matrix post-construction to reach the quadratic-form guard.
    object.__setattr__(snapshot, "matrix", ((0.04, 0.10), (0.10, 0.09)))
    weights = {identities[0].key: 1.0, identities[1].key: -1.0}

    assert (
        sum(
            weights[a] * weights[b] * snapshot.matrix[i][j]
            for i, a in enumerate(k.key for k in identities)
            for j, b in enumerate(k.key for k in identities)
        )
        < -COVARIANCE_TOLERANCE
    )

    with pytest.raises(RiskInputError, match="COVARIANCE_INDEFINITE"):
        snapshot.variance_of(weights)


@pytest.mark.integration
def test_a_negative_variance_never_reports_as_zero_volatility() -> None:
    snapshot = CovarianceSnapshot(identities=(_identity(1),), matrix=((0.04,),), as_of=AS_OF)
    object.__setattr__(snapshot, "matrix", ((-1.0,),))

    with pytest.raises(RiskInputError, match="never clamped to zero"):
        snapshot.volatility_of({_identity(1).key: 1.0})


@pytest.mark.integration
def test_a_residue_inside_the_covariance_tolerance_normalises_to_zero() -> None:
    """Floating-point noise is noise; a material negative is information."""
    snapshot = CovarianceSnapshot(identities=(_identity(1),), matrix=((0.0,),), as_of=AS_OF)
    object.__setattr__(snapshot, "matrix", ((-COVARIANCE_TOLERANCE / 2,),))

    assert snapshot.variance_of({_identity(1).key: 1.0}) == 0.0


@pytest.mark.integration
def test_the_psd_check_locates_the_failure() -> None:
    verdict = is_positive_semidefinite(((0.04, 0.10), (0.10, 0.09)), tolerance=COVARIANCE_TOLERANCE)

    assert verdict.semidefinite is False
    assert verdict.failure == "NEGATIVE_PIVOT"
    assert verdict.failure_index == 1
    assert verdict.minimum_pivot < 0
    assert verdict.as_json()["singular_is_valid"] is True


@pytest.mark.integration
def test_the_covariance_is_never_projected_onto_the_psd_cone() -> None:
    with pytest.raises(RiskInputError, match="not projected onto the semidefinite cone"):
        CovarianceSnapshot(
            identities=(_identity(1), _identity(2)),
            matrix=((0.04, 0.10), (0.10, 0.09)),
            as_of=AS_OF,
        )


# --------------------------------------------------------------------------- #
# §10-§18, §61-§63, §82-§83 allocation policy
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_two_strategies_without_a_policy_are_still_refused() -> None:
    intents = [_intent(COMPOUNDER, 1, 0.05), _intent(MOMENTUM, 2, 0.05)]

    with pytest.raises(ValidationError, match=ReasonCode.MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED):
        evaluate(intents, _snapshot(), _policy())


@pytest.mark.integration
def test_no_equal_weighting_is_ever_inferred() -> None:
    """Two strategies do not imply fifty-fifty, and the error says why."""
    intents = [_intent(COMPOUNDER, 1, 0.05), _intent(MOMENTUM, 2, 0.05)]

    with pytest.raises(ValidationError) as caught:
        evaluate(intents, _snapshot(), _policy())

    assert "Equal weighting is not a default" in str(caught.value)


@pytest.mark.integration
def test_a_strategy_not_named_in_the_policy_is_refused() -> None:
    policy = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.4),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.3),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(EARNINGS, 2, 1.0)]

    with pytest.raises(ValidationError, match=ReasonCode.UNKNOWN_STRATEGY):
        evaluate(intents, _snapshot(), _policy(), allocation=policy)


@pytest.mark.integration
def test_an_unknown_strategy_produces_no_portfolio() -> None:
    policy = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.4))

    with pytest.raises(ValidationError, match="should not be able to reach the portfolio"):
        evaluate([_intent(MOMENTUM, 1, 1.0)], _snapshot(), _policy(), allocation=policy)


@pytest.mark.integration
def test_an_allocation_policy_with_no_budgets_is_refused() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.STRATEGY_BUDGET_INVALID):
        _alloc()


@pytest.mark.integration
def test_sleeve_ceilings_above_the_whole_portfolio_are_refused() -> None:
    with pytest.raises(ValidationError, match="above the whole portfolio"):
        _alloc(
            StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.7),
            StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.5),
        )


@pytest.mark.integration
def test_a_repeated_strategy_budget_is_refused() -> None:
    with pytest.raises(ValidationError, match="more than one budget"):
        _alloc(
            StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.3),
            StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.2),
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "overrides",
    [
        {"max_capital_weight": 1.5},
        {"max_capital_weight": -0.1},
        {"max_capital_weight": 0.3, "max_strategy_gross": 0.0},
        {"max_capital_weight": 0.3, "max_strategy_volatility": -0.2},
    ],
)
def test_an_invalid_strategy_budget_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match=ReasonCode.STRATEGY_BUDGET_INVALID):
        StrategyBudget(strategy_id=COMPOUNDER, **overrides)


@pytest.mark.integration
def test_the_single_strategy_api_is_unchanged_by_p1_2() -> None:
    """An existing caller keeps absolute weights, not sleeve-relative ones."""
    decision = evaluate([_intent(COMPOUNDER, 1, 0.05)], _snapshot(), _policy())

    assert decision.approved.targets == {"COMPANY:1": 0.05}
    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1"
    assert decision.strategy_report is None


@pytest.mark.integration
def test_the_example_allocation_policy_is_not_production_authorised() -> None:
    policy = EXAMPLE_MULTI_STRATEGY_POLICY_V1

    assert policy.production_authorized is False
    assert policy.semantics is BudgetSemantics.CEILING_UNUSED_BECOMES_CASH
    assert policy.total_capital_ceiling == pytest.approx(0.90)
    assert policy.as_json()["performance_weighted"] is False


# --------------------------------------------------------------------------- #
# §15-§19, §27-§31, §63, §67-§68, §72-§73 sleeves
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_two_clean_sleeves_stay_inside_their_combined_ceiling() -> None:
    policy = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [
        _intent(COMPOUNDER, 1, 0.5),
        _intent(COMPOUNDER, 2, 0.5),
        _intent(MOMENTUM, 3, 1.0),
    ]

    # A per-name cap generous enough that only the sleeve ceilings bind here.
    decision = evaluate(intents, _snapshot(), _policy(max_position_weight=0.40), allocation=policy)

    assert decision.approved.gross_exposure <= 0.70 + 1e-7
    assert decision.approved.targets == {
        "COMPANY:1": pytest.approx(0.20),
        "COMPANY:2": pytest.approx(0.20),
        "COMPANY:3": pytest.approx(0.30),
    }
    assert decision.approved.cash_target_weight == pytest.approx(0.30)


@pytest.mark.integration
def test_sleeve_weights_are_relative_and_scaled_by_the_budget() -> None:
    policy = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40))
    # 3:1 inside the sleeve becomes 0.30 : 0.10 globally.
    intents = [_intent(COMPOUNDER, 1, 0.75), _intent(COMPOUNDER, 2, 0.25)]

    decision = evaluate(intents, _snapshot(), _policy(max_position_weight=0.40), allocation=policy)

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.30)
    assert decision.approved.targets["COMPANY:2"] == pytest.approx(0.10)


@pytest.mark.integration
def test_sleeve_weights_are_normalised_whatever_their_scale() -> None:
    """Magnitude carries no meaning once the policy declares them relative."""
    policy = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40))

    small = evaluate(
        [_intent(COMPOUNDER, 1, 0.3), _intent(COMPOUNDER, 2, 0.1)],
        _snapshot(),
        _policy(max_position_weight=0.40),
        allocation=policy,
    )
    large = evaluate(
        [_intent(COMPOUNDER, 1, 30.0), _intent(COMPOUNDER, 2, 10.0)],
        _snapshot(),
        _policy(max_position_weight=0.40),
        allocation=policy,
    )

    assert small.approved.targets == large.approved.targets


@pytest.mark.integration
def test_unused_sleeve_capacity_becomes_cash_and_not_another_sleeve() -> None:
    """The property that keeps one strategy's restraint from funding another."""
    policy = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40, max_strategy_gross=0.20),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]

    decision = evaluate(intents, _snapshot(), _policy(max_position_weight=0.40), allocation=policy)
    compounder, momentum = _sleeve(decision, COMPOUNDER), _sleeve(decision, MOMENTUM)

    assert compounder["approved_post_risk_gross"] == pytest.approx(0.20)
    assert compounder["unused_budget"] == pytest.approx(0.20)
    assert momentum["approved_post_risk_gross"] == pytest.approx(0.30)
    assert decision.approved.cash_target_weight == pytest.approx(0.50)


@pytest.mark.integration
def test_a_strategy_gross_ceiling_scales_only_its_own_sleeve() -> None:
    policy = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40, max_strategy_gross=0.15),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [
        _intent(COMPOUNDER, 1, 0.5),
        _intent(COMPOUNDER, 2, 0.5),
        _intent(MOMENTUM, 3, 1.0),
    ]

    decision = evaluate(intents, _snapshot(), _policy(max_position_weight=0.40), allocation=policy)
    compounder = _sleeve(decision, COMPOUNDER)

    assert ReasonCode.STRATEGY_GROSS_LIMIT in compounder["reason_codes"]
    assert compounder["approved_post_risk_gross"] == pytest.approx(0.15)
    # Ratios inside the sleeve survive the scale.
    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.075)
    assert decision.approved.targets["COMPANY:2"] == pytest.approx(0.075)
    assert _sleeve(decision, MOMENTUM)["approved_post_risk_gross"] == pytest.approx(0.30)


@pytest.mark.integration
def test_a_strategy_volatility_ceiling_scales_only_its_own_sleeve() -> None:
    # COMPOUNDER at 0.40 across two uncorrelated 0.40-vol names: sigma = 0.40 *
    # 0.40 / sqrt(2) ~ 0.1131, above a 0.08 ceiling.
    policy = _alloc(
        StrategyBudget(
            strategy_id=COMPOUNDER, max_capital_weight=0.40, max_strategy_volatility=0.08
        ),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [
        _intent(COMPOUNDER, 1, 0.5),
        _intent(COMPOUNDER, 2, 0.5),
        _intent(MOMENTUM, 3, 1.0),
    ]
    risk = _risk([1, 2, 3], volatility=0.40, correlation=0.0)

    decision = evaluate(
        intents, _snapshot(), _policy(max_position_weight=0.40), risk=risk, allocation=policy
    )
    compounder, momentum = _sleeve(decision, COMPOUNDER), _sleeve(decision, MOMENTUM)

    assert ReasonCode.STRATEGY_VOLATILITY_LIMIT in compounder["reason_codes"]
    assert compounder["standalone_volatility"] <= 0.08 + 1e-7
    assert compounder["approved_post_risk_gross"] < 0.40
    assert momentum["approved_post_risk_gross"] == pytest.approx(0.30)


@pytest.mark.integration
def test_a_sleeve_below_its_volatility_ceiling_is_not_enlarged() -> None:
    policy = _alloc(
        StrategyBudget(
            strategy_id=COMPOUNDER, max_capital_weight=0.20, max_strategy_volatility=0.50
        )
    )

    decision = evaluate(
        [_intent(COMPOUNDER, 1, 1.0)],
        _snapshot(),
        _policy(),
        risk=_risk([1], volatility=0.20),
        allocation=policy,
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.20)
    assert ReasonCode.STRATEGY_VOLATILITY_LIMIT not in _sleeve(decision, COMPOUNDER)["reason_codes"]


@pytest.mark.integration
def test_a_configured_strategy_with_no_intents_stays_idle_and_its_capital_is_cash() -> None:
    policy = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )

    decision = evaluate(
        [_intent(COMPOUNDER, 1, 1.0)],
        _snapshot(),
        _policy(max_position_weight=0.40),
        allocation=policy,
    )
    momentum = _sleeve(decision, MOMENTUM)

    assert ReasonCode.STRATEGY_IDLE in momentum["reason_codes"]
    assert momentum["approved_post_risk_gross"] == 0.0
    assert momentum["unused_budget"] == pytest.approx(0.30)
    assert decision.approved.cash_target_weight == pytest.approx(0.70)


# --------------------------------------------------------------------------- #
# §20-§26, §51-§53, §69-§71, §85-§86 overlap and attribution
# --------------------------------------------------------------------------- #


def _overlap_case(**policy_overrides: Any) -> Any:
    """Two strategies both wanting COMPANY:2."""
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [
        _intent(COMPOUNDER, 1, 0.5),
        _intent(COMPOUNDER, 2, 0.5),
        _intent(MOMENTUM, 2, 1.0),
    ]
    return evaluate(intents, _snapshot(), _policy(**policy_overrides), allocation=allocation)


@pytest.mark.integration
def test_two_strategies_wanting_one_security_hold_one_economic_position() -> None:
    decision = _overlap_case(max_position_weight=0.60)

    assert decision.approved.targets["COMPANY:2"] == pytest.approx(0.50)
    assert len(decision.approved.targets) == 2
    assert decision.risk_summary["approved_count"] == 2


@pytest.mark.integration
def test_a_shared_position_keeps_both_strategy_attributions() -> None:
    decision = _overlap_case(max_position_weight=0.60)
    row = next(
        entry
        for entry in _strategy_report(decision)["attribution_matrix"]
        if entry["identity"] == "COMPANY:2"
    )

    assert row[COMPOUNDER] == pytest.approx(0.20)
    assert row[MOMENTUM] == pytest.approx(0.30)
    assert row[COMPOUNDER] + row[MOMENTUM] == pytest.approx(row["approved_weight"])


@pytest.mark.integration
def test_the_overlap_is_reported() -> None:
    report = _strategy_report(_overlap_case(max_position_weight=0.60))

    assert report["strategy_overlaps"] == [
        {"strategies": [COMPOUNDER, MOMENTUM], "shared_identities": ["COMPANY:2"]}
    ]
    assert report["shared_identity_count"] == 1


@pytest.mark.integration
def test_a_global_position_cap_on_an_overlap_reconciles_attribution_proportionally() -> None:
    """0.20 + 0.30 = 0.50 aggregate, capped at 0.30, split 2:3 as requested."""
    decision = _overlap_case(max_position_weight=0.30)
    row = next(
        entry
        for entry in _strategy_report(decision)["attribution_matrix"]
        if entry["identity"] == "COMPANY:2"
    )

    assert decision.approved.targets["COMPANY:2"] == pytest.approx(0.30)
    assert row[COMPOUNDER] == pytest.approx(0.12)
    assert row[MOMENTUM] == pytest.approx(0.18)
    assert row[COMPOUNDER] + row[MOMENTUM] == pytest.approx(0.30)
    assert row[MOMENTUM] / row[COMPOUNDER] == pytest.approx(1.5)


@pytest.mark.integration
def test_the_position_count_cap_counts_economic_identities_not_requests() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
        StrategyBudget(strategy_id=EARNINGS, max_capital_weight=0.30),
    )
    # All three strategies want the same two securities: two positions, not six.
    intents = [
        _intent(strategy, number, 0.5)
        for strategy in (COMPOUNDER, MOMENTUM, EARNINGS)
        for number in (1, 2)
    ]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_positions=2, max_position_weight=0.50),
        allocation=allocation,
    )

    assert len(decision.approved.targets) == 2
    assert decision.risk_summary["rejected_count"] == 0


@pytest.mark.integration
def test_attribution_reconciles_for_every_identity_and_portfolio_wide() -> None:
    decision = _overlap_case(max_position_weight=0.25)
    report = _strategy_report(decision)

    for row in report["attribution_matrix"]:
        attributed = sum(
            value for key, value in row.items() if key not in {"identity", "approved_weight"}
        )
        assert attributed == pytest.approx(row["approved_weight"], abs=1e-7)
    assert report["total_attributed_approved_exposure"] == pytest.approx(
        decision.approved.gross_exposure, abs=1e-7
    )
    assert report["attribution_reconciles"] is True


@pytest.mark.integration
def test_attributed_exposure_plus_cash_accounts_for_the_whole_portfolio() -> None:
    decision = _overlap_case(max_position_weight=0.25)
    report = _strategy_report(decision)

    assert report["total_attributed_approved_exposure"] + decision.approved.cash_target_weight == (
        pytest.approx(1.0, abs=1e-7)
    )


@pytest.mark.integration
def test_liquidity_applies_to_the_combined_change_not_per_strategy() -> None:
    """Each strategy must not independently consume the whole ADV budget."""
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 1, 1.0)]
    # ADV 1,000,000 x 2% = 20,000 of a 1,000,000 NAV: one 2% step in total.
    risk = _risk([1], adv=1_000_000.0)

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_trade_adv_fraction=0.02, max_position_weight=0.60),
        risk=risk,
        allocation=allocation,
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.02)
    row = next(
        entry
        for entry in _strategy_report(decision)["attribution_matrix"]
        if entry["identity"] == "COMPANY:1"
    )
    assert row[COMPOUNDER] + row[MOMENTUM] == pytest.approx(0.02)


@pytest.mark.integration
def test_two_identities_sharing_a_ticker_stay_separate_across_strategies() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    first = SecurityIdentity(kind="COMPANY", identity_id=1, symbol="SAME")
    second = SecurityIdentity(kind="COMPANY", identity_id=2, symbol="SAME")
    intents = [
        StrategyIntent(
            strategy_id=strategy,
            strategy_version="1",
            decision_id="d1",
            as_of=AS_OF,
            identity=identity,
            requested_weight=1.0,
            source_fingerprint="fp",
        )
        for strategy, identity in ((COMPOUNDER, first), (MOMENTUM, second))
    ]

    decision = evaluate(
        intents, _snapshot(), _policy(max_position_weight=0.40), allocation=allocation
    )

    assert sorted(decision.approved.targets) == ["COMPANY:1", "COMPANY:2"]
    assert first.key != second.key


@pytest.mark.integration
def test_one_strategy_asking_twice_for_one_security_is_still_refused() -> None:
    allocation = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30))

    with pytest.raises(ValidationError, match=ReasonCode.DUPLICATE_STRATEGY_INTENT):
        evaluate(
            [_intent(COMPOUNDER, 1, 0.5), _intent(COMPOUNDER, 1, 0.5)],
            _snapshot(),
            _policy(),
            allocation=allocation,
        )


# --------------------------------------------------------------------------- #
# §33-§41, §54, §75-§78 correlation concentration
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_highly_correlated_pair_is_reduced_to_its_combined_limit() -> None:
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.30
    )
    intents = [_intent(COMPOUNDER, 1, 0.25), _intent(COMPOUNDER, 2, 0.25)]

    decision = evaluate(intents, _snapshot(), policy, risk=_risk([1, 2], correlation=0.90))
    report = decision.risk_report
    assert report is not None

    total = sum(decision.approved.targets.values())
    assert total == pytest.approx(0.30)
    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.15)
    for row in decision.approved.risk_decisions:
        assert ReasonCode.CORRELATION_CONCENTRATION_LIMIT in row["reason_codes"]
    concentration = report["correlation_concentration"]
    assert concentration["applied"] is True
    assert concentration["counterparts"]["COMPANY:1"] == ["COMPANY:2"]


@pytest.mark.integration
def test_a_pair_below_the_threshold_is_untouched() -> None:
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.30
    )
    intents = [_intent(COMPOUNDER, 1, 0.25), _intent(COMPOUNDER, 2, 0.25)]

    decision = evaluate(intents, _snapshot(), policy, risk=_risk([1, 2], correlation=0.40))

    assert sum(decision.approved.targets.values()) == pytest.approx(0.50)


@pytest.mark.integration
def test_a_negatively_correlated_pair_is_not_concentration() -> None:
    """Absolute correlation would penalise the hedge, so the threshold is signed."""
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.30
    )
    intents = [_intent(COMPOUNDER, 1, 0.25), _intent(COMPOUNDER, 2, 0.25)]

    decision = evaluate(intents, _snapshot(), policy, risk=_risk([1, 2], correlation=-0.90))
    report = decision.risk_report
    assert report is not None

    assert sum(decision.approved.targets.values()) == pytest.approx(0.50)
    assert report["correlation_concentration"]["uses_absolute_correlation"] is False


@pytest.mark.integration
def test_three_mutually_correlated_names_converge_to_a_compliant_portfolio() -> None:
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.20
    )
    intents = [_intent(COMPOUNDER, n, 0.20) for n in (1, 2, 3)]

    decision = evaluate(intents, _snapshot(), policy, risk=_risk([1, 2, 3], correlation=0.95))
    report = decision.risk_report
    assert report is not None
    weights = list(decision.approved.targets.values())

    assert all(
        weights[i] + weights[j] <= 0.20 + 1e-7
        for i in range(len(weights))
        for j in range(i + 1, len(weights))
    )
    assert report["correlation_concentration"]["after"]["pairs_violating_exposure_limit"] == 0


@pytest.mark.integration
def test_the_pairwise_reduction_does_not_depend_on_pair_order() -> None:
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.20
    )
    intents = [_intent(COMPOUNDER, n, 0.20) for n in (1, 2, 3)]
    risk = _risk([1, 2, 3], correlation=0.95)

    forward = evaluate(intents, _snapshot(), policy, risk=risk)
    backward = evaluate(list(reversed(intents)), _snapshot(), policy, risk=risk)

    assert forward.approved.targets == backward.approved.targets
    assert forward.approved.fingerprint() == backward.approved.fingerprint()
    assert [row["reason_codes"] for row in forward.approved.risk_decisions] == [
        row["reason_codes"] for row in backward.approved.risk_decisions
    ]


@pytest.mark.integration
def test_a_zero_variance_security_has_an_undefined_correlation_not_zero() -> None:
    identities = (_identity(1), _identity(2))
    covariance = CovarianceSnapshot(
        identities=identities, matrix=((0.0, 0.0), (0.0, 0.04)), as_of=AS_OF
    )

    assert correlation_of(covariance, identities[0].key, identities[1].key) is None
    assert correlation_of(covariance, identities[1].key, identities[1].key) == pytest.approx(1.0)


@pytest.mark.integration
def test_an_undefined_correlation_refuses_the_decision_when_the_policy_requires_it() -> None:
    identities = (_identity(1), _identity(2))
    covariance = CovarianceSnapshot(
        identities=identities, matrix=((0.0, 0.0), (0.0, 0.04)), as_of=AS_OF
    )

    with pytest.raises(ValidationError, match=ReasonCode.CORRELATION_UNDEFINED):
        apply_correlation_concentration(
            {identities[0].key: 0.2, identities[1].key: 0.2},
            covariance,
            threshold=0.80,
            limit=0.30,
            missing_policy=MissingMetricPolicy.REQUIRE_METRIC,
        )


@pytest.mark.integration
def test_the_concentration_control_frees_exposure_to_cash() -> None:
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.20
    )
    intents = [_intent(COMPOUNDER, 1, 0.25), _intent(COMPOUNDER, 2, 0.25)]

    decision = evaluate(intents, _snapshot(), policy, risk=_risk([1, 2], correlation=0.95))

    assert sum(decision.approved.targets.values()) == pytest.approx(0.20)
    assert decision.approved.cash_target_weight == pytest.approx(0.80)


@pytest.mark.integration
def test_concentration_and_portfolio_volatility_both_apply() -> None:
    """Neither substitutes for the other: one is shape, one is total risk."""
    policy = _policy(
        max_position_weight=0.25,
        correlation_threshold=0.80,
        max_correlated_pair_exposure=0.30,
        max_portfolio_volatility=0.08,
    )
    intents = [_intent(COMPOUNDER, 1, 0.25), _intent(COMPOUNDER, 2, 0.25)]

    decision = evaluate(
        intents, _snapshot(), policy, risk=_risk([1, 2], volatility=0.40, correlation=0.90)
    )
    report = decision.risk_report
    assert report is not None
    codes = {code for row in decision.approved.risk_decisions for code in row["reason_codes"]}

    assert ReasonCode.CORRELATION_CONCENTRATION_LIMIT in codes
    assert ReasonCode.PORTFOLIO_VOLATILITY_LIMIT in codes
    assert report["portfolio_volatility_after_risk"] <= 0.08 + 1e-7
    assert sum(decision.approved.targets.values()) <= 0.30 + 1e-7


@pytest.mark.integration
def test_concentration_applies_to_the_aggregate_not_to_each_sleeve() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    # Each sleeve holds one of a correlated pair, so only the aggregate breaches.
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]
    policy = _policy(
        max_position_weight=0.40, correlation_threshold=0.80, max_correlated_pair_exposure=0.40
    )

    decision = evaluate(
        intents, _snapshot(), policy, risk=_risk([1, 2], correlation=0.95), allocation=allocation
    )

    assert sum(decision.approved.targets.values()) == pytest.approx(0.40)
    report = _strategy_report(decision)
    assert report["total_attributed_approved_exposure"] == pytest.approx(0.40)


@pytest.mark.integration
def test_correlation_diagnostics_are_reported_before_and_after() -> None:
    policy = _policy(
        max_position_weight=0.25, correlation_threshold=0.80, max_correlated_pair_exposure=0.20
    )
    intents = [_intent(COMPOUNDER, n, 0.20) for n in (1, 2, 3)]

    decision = evaluate(intents, _snapshot(), policy, risk=_risk([1, 2, 3], correlation=0.95))
    report = decision.risk_report
    assert report is not None
    concentration = report["correlation_concentration"]

    for side in ("before", "after"):
        for key in (
            "defined_pairs",
            "undefined_pairs",
            "pairs_above_threshold",
            "pairs_violating_exposure_limit",
            "largest_positive_correlation",
            "largest_correlated_pair_exposure",
        ):
            assert key in concentration[side], (side, key)
    assert concentration["before"]["pairs_violating_exposure_limit"] > 0
    assert concentration["after"]["pairs_violating_exposure_limit"] == 0
    assert concentration["is_not_a_factor_model"] is True
    assert concentration["is_not_diversification_alpha"] is True


@pytest.mark.integration
def test_a_correlation_threshold_without_a_pair_limit_is_refused() -> None:
    with pytest.raises(PolicyError, match="set together or not at all"):
        _policy(correlation_threshold=0.80)


# --------------------------------------------------------------------------- #
# §24, §55-§59, §79-§81 global authority and modes
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_sleeve_budgets_do_not_override_the_global_position_cap() -> None:
    allocation = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40))

    decision = evaluate(
        [_intent(COMPOUNDER, 1, 1.0)],
        _snapshot(),
        _policy(max_position_weight=0.10),
        allocation=allocation,
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.10)


@pytest.mark.integration
def test_sleeve_budgets_do_not_override_the_global_cash_reserve() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.50),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.50),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_position_weight=0.60, min_cash_weight=0.20),
        allocation=allocation,
    )

    assert decision.approved.cash_target_weight >= 0.20 - 1e-7


@pytest.mark.integration
def test_the_drawdown_overlay_stays_global_and_no_strategy_opts_out() -> None:
    from stock_screener.portfolio_risk.drawdown import DrawdownBand

    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.40),
    )
    policy = _policy(
        max_position_weight=0.40,
        drawdown_bands=(DrawdownBand(threshold=-0.10, max_gross_multiplier=0.50),),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]

    decision = evaluate(
        intents,
        _snapshot(),
        policy,
        risk=_risk([1, 2], nav=85.0, peak=100.0),
        allocation=allocation,
    )

    assert decision.approved.gross_exposure <= 0.50 + 1e-7
    for sleeve in _strategy_report(decision)["sleeves"]:
        assert sleeve["approved_post_risk_gross"] <= sleeve["allocated_pre_risk_gross"] + 1e-7


@pytest.mark.integration
def test_reduce_only_lets_no_strategy_contribution_increase() -> None:
    from stock_screener.portfolio_risk.policy import PolicyMode

    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.40),
    )
    # COMPANY:1 held at 10%; one strategy wants more of it, the other less.
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]

    decision = evaluate(
        intents,
        _snapshot({1: 0.10}),
        _policy(mode=PolicyMode.REDUCE_ONLY, max_position_weight=0.40),
        risk=_risk([1, 2]),
        allocation=allocation,
    )
    targets = decision.approved.targets

    assert targets.get("COMPANY:1", 0.0) <= 0.10 + 1e-7
    assert "COMPANY:2" not in targets
    for row in _strategy_report(decision)["attribution_matrix"]:
        attributed = sum(
            value for key, value in row.items() if key not in {"identity", "approved_weight"}
        )
        assert attributed <= row["approved_weight"] + 1e-7


@pytest.mark.integration
def test_halted_holds_the_economic_book_whatever_the_strategies_request() -> None:
    from stock_screener.portfolio_risk.policy import PolicyMode

    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.40),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 3, 1.0)]

    decision = evaluate(
        intents,
        _snapshot({1: 0.05, 2: 0.06}),
        _policy(mode=PolicyMode.HALTED, max_position_weight=0.40),
        risk=_risk([1, 2, 3]),
        allocation=allocation,
    )

    assert decision.approved.targets == {
        "COMPANY:1": pytest.approx(0.05),
        "COMPANY:2": pytest.approx(0.06),
    }
    assert decision.delta_summary["positions_exited"] == 0
    assert decision.delta_summary["weight_turnover_estimate"] == 0.0


@pytest.mark.integration
def test_an_existing_position_no_strategy_claims_is_still_exited() -> None:
    """Historical ownership is not fabricated to make a report complete."""
    allocation = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30))

    decision = evaluate(
        [_intent(COMPOUNDER, 1, 1.0)],
        _snapshot({1: 0.05, 99: 0.08}),
        _policy(max_position_weight=0.40),
        risk=_risk([1, 99]),
        allocation=allocation,
    )
    actions = {i.identity.identity_id: str(i.action) for i in decision.execution_intents}
    matrix = {row["identity"] for row in _strategy_report(decision)["attribution_matrix"]}

    assert actions[99] == "EXIT"
    assert "COMPANY:99" not in matrix


@pytest.mark.integration
def test_no_global_stage_restores_exposure_a_sleeve_cap_removed() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.40, max_strategy_gross=0.10),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]
    policy = _policy(
        max_position_weight=0.40,
        max_portfolio_volatility=0.90,
        correlation_threshold=0.95,
        max_correlated_pair_exposure=0.90,
    )

    decision = evaluate(
        intents, _snapshot(), policy, risk=_risk([1, 2], correlation=0.10), allocation=allocation
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.10)
    assert _sleeve(decision, COMPOUNDER)["approved_post_risk_gross"] == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# §47-§49, §84, §87-§88 determinism, fingerprints and purity
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_strategy_and_intent_order_do_not_change_the_outcome() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
        StrategyBudget(strategy_id=EARNINGS, max_capital_weight=0.20),
    )
    intents = [
        _intent(COMPOUNDER, 1, 0.5),
        _intent(COMPOUNDER, 2, 0.5),
        _intent(MOMENTUM, 2, 1.0),
        _intent(EARNINGS, 3, 1.0),
    ]
    risk = _risk([1, 2, 3])
    policy = _policy(max_position_weight=0.40)

    forward = evaluate(intents, _snapshot(), policy, risk=risk, allocation=allocation)
    backward = evaluate(
        list(reversed(intents)), _snapshot(), policy, risk=risk, allocation=allocation
    )

    assert forward.approved.fingerprint() == backward.approved.fingerprint()
    assert forward.execution_fingerprint == backward.execution_fingerprint
    assert _strategy_report(forward)["fingerprint"] == _strategy_report(backward)["fingerprint"]
    assert (
        forward.manifest["strategy_attribution_fingerprint"]
        == backward.manifest["strategy_attribution_fingerprint"]
    )


@pytest.mark.integration
def test_the_manifest_binds_every_multi_strategy_fingerprint() -> None:
    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_position_weight=0.40),
        risk=_risk([1, 2]),
        allocation=allocation,
    )
    manifest = decision.manifest

    assert manifest["manifest_id"] == "PORTFOLIO_DECISION_MANIFEST_V1_2"
    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_2"
    for key in (
        "strategy_allocation_policy_fingerprint",
        "aggregate_request_fingerprint",
        "strategy_attribution_fingerprint",
        "risk_snapshot_fingerprint",
        "effective_risk_policy_fingerprint",
        "approved_portfolio_fingerprint",
        "execution_intents_fingerprint",
    ):
        assert manifest[key], key
    assert set(manifest["strategy_intent_fingerprints"]) == {COMPOUNDER, MOMENTUM}
    assert set(manifest["strategy_sleeve_fingerprints"]) == {COMPOUNDER, MOMENTUM}
    assert manifest["strategy_allocation_policy_production_authorized"] is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "what",
    [
        "sleeve_budget",
        "strategy_intent",
        "correlation_threshold",
        "pair_limit",
        "strategy_vol_ceiling",
        "covariance_value",
    ],
)
def test_changing_one_p1_2_input_changes_the_relevant_fingerprint(what: str) -> None:
    budgets = (
        StrategyBudget(
            strategy_id=COMPOUNDER, max_capital_weight=0.30, max_strategy_volatility=0.50
        ),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    allocation = _alloc(*budgets)
    intents = [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)]
    policy = _policy(
        max_position_weight=0.40, correlation_threshold=0.95, max_correlated_pair_exposure=0.90
    )
    risk = _risk([1, 2])
    base = evaluate(intents, _snapshot(), policy, risk=risk, allocation=allocation)

    if what == "sleeve_budget":
        changed = evaluate(
            intents,
            _snapshot(),
            policy,
            risk=risk,
            allocation=_alloc(
                StrategyBudget(
                    strategy_id=COMPOUNDER, max_capital_weight=0.25, max_strategy_volatility=0.50
                ),
                budgets[1],
            ),
        )
    elif what == "strategy_intent":
        changed = evaluate(
            [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 3, 1.0)],
            _snapshot(),
            policy,
            risk=_risk([1, 2, 3]),
            allocation=allocation,
        )
    elif what == "correlation_threshold":
        changed = evaluate(
            intents,
            _snapshot(),
            _policy(
                max_position_weight=0.40,
                correlation_threshold=0.70,
                max_correlated_pair_exposure=0.90,
            ),
            risk=risk,
            allocation=allocation,
        )
    elif what == "pair_limit":
        changed = evaluate(
            intents,
            _snapshot(),
            _policy(
                max_position_weight=0.40,
                correlation_threshold=0.95,
                max_correlated_pair_exposure=0.80,
            ),
            risk=risk,
            allocation=allocation,
        )
    elif what == "strategy_vol_ceiling":
        changed = evaluate(
            intents,
            _snapshot(),
            policy,
            risk=risk,
            allocation=_alloc(
                StrategyBudget(
                    strategy_id=COMPOUNDER, max_capital_weight=0.30, max_strategy_volatility=0.40
                ),
                budgets[1],
            ),
        )
    else:
        changed = evaluate(
            intents,
            _snapshot(),
            policy,
            risk=_risk([1, 2], correlation=0.30),
            allocation=allocation,
        )

    assert changed.approved.fingerprint() != base.approved.fingerprint(), what


@pytest.mark.integration
def test_the_allocation_policy_fingerprint_moves_with_any_part_of_it() -> None:
    base = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30))
    other_budget = _alloc(StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.31))
    other_strategy = _alloc(StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30))
    other_version = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        allocation_policy_version="2",
    )

    digests = {
        base.fingerprint(),
        other_budget.fingerprint(),
        other_strategy.fingerprint(),
        other_version.fingerprint(),
    }
    assert len(digests) == 4


@pytest.mark.integration
def test_multi_strategy_evaluation_completes_with_every_outside_call_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import sqlite3

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the multi-strategy engine reached outside its inputs")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)

    allocation = _alloc(
        StrategyBudget(strategy_id=COMPOUNDER, max_capital_weight=0.30),
        StrategyBudget(strategy_id=MOMENTUM, max_capital_weight=0.30),
    )
    decision = evaluate(
        [_intent(COMPOUNDER, 1, 1.0), _intent(MOMENTUM, 2, 1.0)],
        _snapshot(),
        _policy(
            max_position_weight=0.40, correlation_threshold=0.95, max_correlated_pair_exposure=0.90
        ),
        risk=_risk([1, 2]),
        allocation=allocation,
    )

    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_2"
    assert decision.strategy_report is not None


@pytest.mark.integration
def test_the_allocator_cannot_see_any_performance_measure() -> None:
    """Capital budgets are policy inputs. Learning them is a later research problem."""
    import ast

    package = REPO_ROOT / "src" / "stock_screener" / "portfolio_risk"
    # Identifiers, not prose: the docstrings explain *why* a Sharpe ratio is
    # unavailable, and a text scan would flag the explanation along with a use.
    forbidden = {
        "sharpe",
        "sharpe_ratio",
        "cagr",
        "forward_return",
        "forward_returns",
        "win_rate",
        "total_return",
        "alpha",
        "backtest_metrics",
    }
    reachable: set[str] = set()
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                reachable.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                reachable.add(node.attr.lower())
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    reachable.update(part.lower() for part in alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    reachable.update(part.lower() for part in node.module.split("."))
                for alias in node.names:
                    reachable.add(alias.name.lower())

    assert not (reachable & forbidden), sorted(reachable & forbidden)
    assert not (reachable & {"backtest", "research", "signals", "scoring"})


# --------------------------------------------------------------------------- #
# §90-§92 the multi-strategy golden fixture
# --------------------------------------------------------------------------- #

MULTI_FIXTURE = FIXTURES / "multi_strategy_example_request.json"


@pytest.mark.integration
def test_the_multi_strategy_fixture_exercises_every_p1_2_path() -> None:
    from stock_screener.portfolio_risk import evaluate_request

    decision = evaluate_request(MULTI_FIXTURE)
    report = _strategy_report(decision)
    risk = decision.risk_report
    assert risk is not None
    codes = {code for row in decision.approved.risk_decisions for code in row["reason_codes"]}
    sleeve_codes = {code for row in report["sleeves"] for code in row["reason_codes"]}

    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_2"
    assert len(report["sleeves"]) == 4
    assert report["strategy_overlaps"]
    assert ReasonCode.STRATEGY_IDLE in sleeve_codes
    assert ReasonCode.STRATEGY_GROSS_LIMIT in sleeve_codes
    assert ReasonCode.STRATEGY_VOLATILITY_LIMIT in sleeve_codes
    assert ReasonCode.MAX_POSITION_WEIGHT in codes
    assert ReasonCode.POSITION_LIQUIDITY_LIMIT in codes
    assert ReasonCode.TRADE_LIQUIDITY_LIMIT in codes
    assert ReasonCode.CORRELATION_CONCENTRATION_LIMIT in codes
    assert ReasonCode.PORTFOLIO_VOLATILITY_LIMIT in codes
    assert risk["effective_policy"]["active_band"]["label"] == "caution"
    assert decision.approved.cash_target_weight > 0.05
    assert any(str(i.action) == "EXIT" for i in decision.execution_intents)


@pytest.mark.integration
def test_the_multi_strategy_fixture_reconciles_exactly() -> None:
    from stock_screener.portfolio_risk import evaluate_request

    decision = evaluate_request(MULTI_FIXTURE)
    report = _strategy_report(decision)

    assert report["total_attributed_approved_exposure"] == pytest.approx(
        decision.approved.gross_exposure, abs=1e-7
    )
    assert report["total_attributed_approved_exposure"] + decision.approved.cash_target_weight == (
        pytest.approx(1.0, abs=1e-7)
    )
    for row in report["attribution_matrix"]:
        attributed = sum(
            value for key, value in row.items() if key not in {"identity", "approved_weight"}
        )
        assert attributed == pytest.approx(row["approved_weight"], abs=1e-7)


@pytest.mark.integration
def test_the_multi_strategy_fixture_still_matches_its_golden_output() -> None:
    import json as _json

    from stock_screener.portfolio_risk import evaluate_request

    produced = evaluate_request(MULTI_FIXTURE).as_json()
    produced["manifest"].pop("evaluated_at_utc")
    expected = _json.loads((FIXTURES / "multi_strategy_example_request.golden.json").read_text())
    expected.pop("_note", None)
    expected["manifest"].pop("evaluated_at_utc", None)
    produced["manifest"]["code_commit"] = expected["manifest"].get("code_commit")

    assert produced == expected


@pytest.mark.integration
def test_the_multi_strategy_fixture_claims_no_authorisation() -> None:
    import json as _json

    body = _json.loads(MULTI_FIXTURE.read_text())

    assert body["policy"]["production_authorized"] is False
    assert body["allocation"]["production_authorized"] is False
    assert "NOT recommended AlphaTrader settings" in body["note"]
    assert "none was derived from historical performance" in body["note"]


@pytest.mark.integration
def test_the_prior_goldens_are_unaffected_by_p1_2() -> None:
    """V1 and V1.1 are versions, not former states of the code."""
    from stock_screener.portfolio_risk import evaluate_request

    v1 = evaluate_request(FIXTURES / "example_request.json")
    v1_1 = evaluate_request(FIXTURES / "advanced_example_request.json")

    assert v1.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1"
    assert v1.strategy_report is None
    assert v1_1.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_1"
    assert v1_1.strategy_report is None
    assert v1_1.manifest["advanced_risk_stage_order"][1] == ("seed_ceilings_at_pre_risk_weights")
