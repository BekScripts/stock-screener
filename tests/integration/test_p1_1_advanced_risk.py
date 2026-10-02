"""P1.1: volatility, liquidity, covariance and drawdown controls.

The property these tests exist to establish is narrow and important: a risk
control may reduce exposure or refuse it, and may never hand back risk an earlier
control removed. Everything else — the caps, the bands, the ceilings — is in
service of that, so the suite checks monotonicity directly as well as behaviourally.

Every number here is a test constant. None was chosen by running historical
performance, and none is a recommendation.
"""

from __future__ import annotations

import json
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
    evaluate_request,
)
from stock_screener.portfolio_risk.advanced import ADVANCED_STAGE_ORDER
from stock_screener.portfolio_risk.drawdown import (
    MODE_SEVERITY,
    DrawdownBand,
    derive_effective_policy,
    stricter_mode,
    validate_bands,
)
from stock_screener.portfolio_risk.policy import (
    EXAMPLE_ADVANCED_RISK_POLICY_V1,
    MissingMetricPolicy,
    PolicyError,
    PolicyMode,
    RiskPolicy,
)
from stock_screener.portfolio_risk.risk_inputs import (
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
EARLIER = "2026-10-01T16:00:00-04:00"
LATER = "2026-10-03T16:00:00-04:00"
NAV = 1_000_000.0
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "portfolio_risk"

#: A generous ADV, so liquidity is not the binding constraint unless a test means
#: it to be.
AMPLE_ADV = 500_000_000.0


def _identity(number: int) -> SecurityIdentity:
    return SecurityIdentity(kind="COMPANY", identity_id=number, symbol=f"T{number:02d}")


def _intent(number: int, weight: float, *, priority: float | None = None) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="S",
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
        Position(identity=_identity(number), quantity=weight * NAV / 100.0, mark_price=100.0)
        for number, weight in sorted(held.items())
    )
    invested = sum(position.market_value for position in positions)
    return PortfolioSnapshot(as_of=AS_OF, cash=NAV - invested, positions=positions)


def _metrics(
    number: int,
    *,
    volatility: float | None = 0.20,
    adv: float | None = AMPLE_ADV,
    as_of: str = AS_OF,
) -> SecurityRiskMetrics:
    return SecurityRiskMetrics(
        identity=_identity(number),
        metric_as_of=as_of,
        source_fingerprint="metrics",
        annualized_volatility=volatility,
        volatility_state=MetricState.KNOWN if volatility is not None else MetricState.UNKNOWN,
        average_daily_dollar_volume=adv,
        liquidity_state=MetricState.KNOWN if adv is not None else MetricState.UNKNOWN,
    )


def _covariance(
    numbers: Sequence[int], *, volatility: float = 0.20, correlation: float = 0.0
) -> CovarianceSnapshot:
    """A covariance matrix with uniform volatility and uniform correlation."""
    identities = tuple(_identity(number) for number in numbers)
    matrix = tuple(
        tuple(
            volatility * volatility * (1.0 if left == right else correlation)
            for right in range(len(identities))
        )
        for left in range(len(identities))
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
    metrics: Sequence[SecurityRiskMetrics] | None = None,
    as_of: str = AS_OF,
) -> PortfolioRiskSnapshot:
    return PortfolioRiskSnapshot(
        as_of=as_of,
        portfolio_nav=nav,
        portfolio_peak_nav=peak,
        metrics=tuple(
            metrics
            if metrics is not None
            else [_metrics(number, volatility=volatility, adv=adv) for number in numbers]
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
        "policy_id": "TEST_ADVANCED_POLICY",
        "policy_version": "1",
        "max_gross_exposure": 1.00,
        "max_position_weight": 0.25,
        "max_positions": 20,
        "min_cash_weight": 0.05,
        "production_authorized": False,
    }
    return RiskPolicy(**{**base, **overrides})


def _report(decision: Any) -> dict[str, Any]:
    """The risk report, asserted present.

    `risk_report` is None under V1 by design, so every V1.1 test narrows it here
    rather than repeating the assertion.
    """
    report: dict[str, Any] | None = decision.risk_report
    assert report is not None, "a V1.1 decision must carry a risk report"
    return report


def _rows(decision: Any) -> dict[int, dict[str, Any]]:
    return {int(row["identity"]["identity_id"]): row for row in decision.approved.risk_decisions}


def _actions(decision: Any) -> dict[int, str]:
    return {i.identity.identity_id: str(i.action) for i in decision.execution_intents}


# --------------------------------------------------------------------------- #
# §2 V1 stays reproducible
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_without_risk_inputs_the_engine_reports_v1_and_omits_the_advanced_block() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy())

    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1"
    assert decision.manifest["manifest_id"] == "PORTFOLIO_DECISION_MANIFEST_V1"
    assert decision.risk_report is None
    assert "risk_snapshot_fingerprint" not in decision.manifest


@pytest.mark.integration
def test_with_risk_inputs_the_engine_reports_v1_1() -> None:
    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy(), risk=_risk([1]))

    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_1"
    assert decision.manifest["manifest_id"] == "PORTFOLIO_DECISION_MANIFEST_V1_1"
    assert decision.risk_report is not None


@pytest.mark.integration
def test_a_policy_using_no_advanced_control_digests_as_it_did_under_v1() -> None:
    plain = _policy()
    advanced = _policy(max_portfolio_volatility=0.20)

    assert plain.uses_advanced_controls is False
    assert advanced.uses_advanced_controls is True
    assert plain.fingerprint() != advanced.fingerprint()
    assert "advanced_risk_controls" not in plain.as_json()
    assert "advanced_risk_controls" in advanced.as_json()


# --------------------------------------------------------------------------- #
# §54-§56 portfolio volatility
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_volatility_below_the_limit_causes_no_resize() -> None:
    intents = [_intent(n, 0.10) for n in range(1, 5)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_portfolio_volatility=0.50),
        risk=_risk([1, 2, 3, 4], volatility=0.20, correlation=0.0),
    )
    report = _report(decision)

    assert report["volatility_scaling_applied"] is False
    assert decision.approved.targets == {f"COMPANY:{n}": 0.10 for n in range(1, 5)}
    assert report["portfolio_volatility_before_risk"] == report["portfolio_volatility_after_risk"]


@pytest.mark.integration
def test_volatility_above_the_limit_scales_proportionally_to_the_limit() -> None:
    """The volatility stage runs after the budget, which is why 0.19 and not 0.20.

    Four names at 0.25 sum to gross 1.00, but the 5% cash reserve caps the
    investable budget at 0.95, so each is 0.2375 by the time volatility is
    measured: sigma_p = 0.2375 * 0.40 * sqrt(4) = 0.19. The ordering is frozen and
    this test pins it.
    """
    intents = [_intent(n, 0.25) for n in range(1, 5)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_portfolio_volatility=0.10, max_position_weight=0.25),
        risk=_risk([1, 2, 3, 4], volatility=0.40, correlation=0.0),
    )
    report = _report(decision)

    assert report["volatility_scaling_applied"] is True
    assert report["portfolio_volatility_before_risk"] == pytest.approx(0.19)
    assert report["portfolio_volatility_after_risk"] <= 0.10 + 1e-7
    assert report["volatility_scale"] == pytest.approx(0.10 / 0.19)
    # Proportional: one scale for every name, so every ratio is preserved.
    assert all(weight == pytest.approx(0.125) for weight in decision.approved.targets.values())


@pytest.mark.integration
def test_the_volatility_limit_is_a_ceiling_and_never_a_target() -> None:
    intents = [_intent(1, 0.05)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_portfolio_volatility=0.50),
        risk=_risk([1], volatility=0.20),
    )

    # Far below the ceiling; exposure is not raised to reach it.
    assert decision.approved.targets == {"COMPANY:1": 0.05}
    assert _report(decision)["volatility_scaling_applied"] is False


@pytest.mark.integration
def test_volatility_scaling_preserves_unequal_relative_weights() -> None:
    intents = [_intent(1, 0.20), _intent(2, 0.10)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(max_portfolio_volatility=0.05, max_position_weight=0.25),
        risk=_risk([1, 2], volatility=0.40, correlation=0.0),
    )
    targets = decision.approved.targets

    assert targets["COMPANY:1"] / targets["COMPANY:2"] == pytest.approx(2.0)
    assert _report(decision)["portfolio_volatility_after_risk"] <= 0.05 + 1e-7


@pytest.mark.integration
def test_a_genuinely_zero_volatility_security_is_not_unknown() -> None:
    zero = _metrics(1, volatility=0.0)

    assert zero.volatility_state is MetricState.KNOWN
    assert zero.annualized_volatility == 0.0

    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(max_portfolio_volatility=0.10),
        risk=_risk([1], volatility=0.0, metrics=[zero]),
    )

    assert decision.approved.targets == {"COMPANY:1": 0.10}
    assert _report(decision)["portfolio_volatility_before_risk"] == 0.0


@pytest.mark.integration
def test_a_known_state_with_no_value_is_a_contradiction() -> None:
    with pytest.raises(RiskInputError, match="declared KNOWN with no value"):
        SecurityRiskMetrics(
            identity=_identity(1),
            metric_as_of=AS_OF,
            source_fingerprint="m",
            annualized_volatility=None,
            volatility_state=MetricState.KNOWN,
        )


@pytest.mark.integration
def test_an_unknown_state_carrying_a_value_is_a_contradiction() -> None:
    with pytest.raises(RiskInputError, match="while declared"):
        SecurityRiskMetrics(
            identity=_identity(1),
            metric_as_of=AS_OF,
            source_fingerprint="m",
            annualized_volatility=0.2,
            volatility_state=MetricState.UNKNOWN,
        )


@pytest.mark.integration
def test_a_volatility_expressed_as_a_percent_number_is_refused() -> None:
    with pytest.raises(RiskInputError, match="annualised decimal"):
        _metrics(1, volatility=25.0)


@pytest.mark.integration
def test_a_volatility_limit_expressed_as_a_percent_number_is_refused() -> None:
    with pytest.raises(PolicyError, match="annualised decimal"):
        _policy(max_portfolio_volatility=20.0)


# --------------------------------------------------------------------------- #
# §57, §43, §71 missing volatility and covariance
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_name_with_no_volatility_may_not_take_new_risk() -> None:
    decision = evaluate(
        [_intent(1, 0.10), _intent(2, 0.10)],
        _snapshot(),
        _policy(max_portfolio_volatility=0.50),
        risk=_risk(
            [1, 2],
            metrics=[_metrics(1), _metrics(2, volatility=None)],
            covariance=_covariance([1, 2]),
        ),
    )
    rows = _rows(decision)

    assert "COMPANY:2" not in decision.approved.targets
    assert ReasonCode.RISK_METRIC_MISSING in rows[2]["reason_codes"]
    assert _report(decision)["missing_risk_metric"] == ["COMPANY:2"]


@pytest.mark.integration
def test_an_identity_absent_from_the_covariance_is_not_assumed_uncorrelated() -> None:
    decision = evaluate(
        [_intent(1, 0.10), _intent(2, 0.10)],
        _snapshot(),
        _policy(max_portfolio_volatility=0.50),
        risk=_risk([1, 2], covariance=_covariance([1])),
    )
    rows = _rows(decision)

    assert "COMPANY:2" not in decision.approved.targets
    assert ReasonCode.RISK_METRIC_MISSING in rows[2]["reason_codes"]


@pytest.mark.integration
def test_an_absent_covariance_matrix_blocks_new_risk_under_the_fail_safe_policy() -> None:
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(max_portfolio_volatility=0.20),
        risk=_risk([1], covariance=None),
    )
    rows = _rows(decision)

    assert decision.approved.targets == {}
    assert ReasonCode.COVARIANCE_UNAVAILABLE in rows[1]["reason_codes"]


@pytest.mark.integration
def test_requiring_the_metric_refuses_the_whole_decision() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.COVARIANCE_UNAVAILABLE):
        evaluate(
            [_intent(1, 0.10)],
            _snapshot(),
            _policy(
                max_portfolio_volatility=0.20,
                missing_volatility_policy=MissingMetricPolicy.REQUIRE_METRIC,
            ),
            risk=_risk([1], covariance=None),
        )


@pytest.mark.integration
def test_ignoring_the_metric_permits_the_position_and_skips_the_scaling() -> None:
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(
            max_portfolio_volatility=0.20,
            missing_volatility_policy=MissingMetricPolicy.IGNORE_METRIC,
        ),
        risk=_risk([1], covariance=None),
    )

    assert decision.approved.targets == {"COMPANY:1": 0.10}
    assert _report(decision)["volatility_scaling_applied"] is False


@pytest.mark.integration
def test_a_missing_metric_still_permits_a_reduction() -> None:
    decision = evaluate(
        [_intent(1, 0.04)],
        _snapshot({1: 0.10}),
        _policy(max_portfolio_volatility=0.50),
        risk=_risk([1], metrics=[_metrics(1, volatility=None)]),
    )

    # The ceiling is the current weight, so a smaller request passes untouched.
    assert decision.approved.targets == {"COMPANY:1": 0.04}
    assert _actions(decision)[1] == "REDUCE"


# --------------------------------------------------------------------------- #
# §11, §69, §70 covariance validation
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_an_asymmetric_covariance_is_refused_not_averaged() -> None:
    with pytest.raises(RiskInputError, match="COVARIANCE_NOT_SYMMETRIC"):
        CovarianceSnapshot(
            identities=(_identity(1), _identity(2)),
            matrix=((0.04, 0.01), (0.02, 0.09)),
            as_of=AS_OF,
        )


@pytest.mark.integration
def test_a_negative_variance_is_refused() -> None:
    with pytest.raises(RiskInputError, match="COVARIANCE_NEGATIVE_VARIANCE"):
        CovarianceSnapshot(identities=(_identity(1),), matrix=((-0.01,),), as_of=AS_OF)


@pytest.mark.integration
def test_a_non_square_covariance_is_refused() -> None:
    with pytest.raises(RiskInputError, match="COVARIANCE_NOT_SQUARE"):
        CovarianceSnapshot(identities=(_identity(1), _identity(2)), matrix=((0.04,),), as_of=AS_OF)


@pytest.mark.integration
def test_a_non_finite_covariance_is_refused() -> None:
    with pytest.raises(RiskInputError, match="COVARIANCE_NOT_FINITE"):
        CovarianceSnapshot(identities=(_identity(1),), matrix=((float("nan"),),), as_of=AS_OF)


@pytest.mark.integration
def test_a_repeated_identity_in_the_covariance_is_refused() -> None:
    with pytest.raises(RiskInputError, match="COVARIANCE_IDENTITY_REPEATED"):
        CovarianceSnapshot(
            identities=(_identity(1), _identity(1)),
            matrix=((0.04, 0.0), (0.0, 0.04)),
            as_of=AS_OF,
        )


@pytest.mark.integration
def test_a_covariance_covering_extra_identities_is_subselected_deterministically() -> None:
    covariance = _covariance([1, 2, 3, 4, 5])

    decision = evaluate(
        [_intent(1, 0.10), _intent(2, 0.10)],
        _snapshot(),
        _policy(max_portfolio_volatility=0.50),
        risk=_risk([1, 2], covariance=covariance),
    )

    assert covariance.covers([_identity(1).key, _identity(2).key])
    assert len(decision.approved.targets) == 2
    assert _report(decision)["portfolio_volatility_before_risk"] == pytest.approx(
        math.sqrt(2) * 0.10 * 0.20
    )


@pytest.mark.integration
def test_the_portfolio_variance_equals_the_sum_of_component_contributions() -> None:
    covariance = _covariance([1, 2, 3], volatility=0.30, correlation=0.4)
    weights = {_identity(n).key: 0.1 * n for n in (1, 2, 3)}

    components = covariance.component_variance(weights)

    assert sum(components.values()) == pytest.approx(covariance.variance_of(weights))


@pytest.mark.integration
def test_correlation_diagnostics_report_undefined_rather_than_zero() -> None:
    identities = (_identity(1), _identity(2))
    covariance = CovarianceSnapshot(
        identities=identities, matrix=((0.0, 0.0), (0.0, 0.04)), as_of=AS_OF
    )

    diagnostics = covariance.correlation_diagnostics([i.key for i in identities])

    assert diagnostics["undefined_pairs"] == 1.0
    assert diagnostics["average_pairwise_correlation"] is None


@pytest.mark.integration
def test_a_weighted_identity_with_no_covariance_row_raises_rather_than_assuming_zero() -> None:
    covariance = _covariance([1])

    with pytest.raises(RiskInputError, match="not a zero covariance"):
        covariance.variance_of({_identity(1).key: 0.1, _identity(2).key: 0.1})


# --------------------------------------------------------------------------- #
# §58-§61 liquidity capacity
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_position_above_its_adv_capacity_is_resized_to_it() -> None:
    # ADV 400,000 x 5% = 20,000 of a 1,000,000 NAV, so 2% is the capacity.
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(max_position_adv_fraction=0.05),
        risk=_risk([1], adv=400_000.0),
    )
    rows = _rows(decision)

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.02)
    assert ReasonCode.POSITION_LIQUIDITY_LIMIT in rows[1]["reason_codes"]
    assert _report(decision)["position_capacity_limited"] == ["COMPANY:1"]


@pytest.mark.integration
def test_a_trade_larger_than_its_adv_capacity_is_resized_to_the_step() -> None:
    # ADV 1,000,000 x 2% = 20,000, so one decision may move 2% of NAV.
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot({1: 0.04}),
        _policy(max_trade_adv_fraction=0.02),
        risk=_risk([1], adv=1_000_000.0),
    )
    rows = _rows(decision)
    approved = decision.approved.targets["COMPANY:1"]

    assert approved == pytest.approx(0.06)
    assert abs(approved - 0.04) * NAV <= 1_000_000.0 * 0.02 + 1e-6
    assert ReasonCode.TRADE_LIQUIDITY_LIMIT in rows[1]["reason_codes"]


@pytest.mark.integration
def test_position_and_trade_capacity_are_separate_limits() -> None:
    policy = _policy(max_position_adv_fraction=0.10, max_trade_adv_fraction=0.01)

    decision = evaluate([_intent(1, 0.20)], _snapshot(), policy, risk=_risk([1], adv=1_000_000.0))
    rows = _rows(decision)

    # Position capacity allows 10%, trade capacity only 1% in one decision.
    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.01)
    assert ReasonCode.TRADE_LIQUIDITY_LIMIT in rows[1]["reason_codes"]
    assert _report(decision)["trade_capacity_limited"] == ["COMPANY:1"]


@pytest.mark.integration
def test_a_holding_already_above_capacity_may_still_be_reduced() -> None:
    # Held at 15%, capacity 2%. The strategy asks for 10%, a reduction.
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot({1: 0.15}),
        _policy(max_position_adv_fraction=0.05, max_position_weight=0.25),
        risk=_risk([1], adv=400_000.0),
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.10)
    assert _actions(decision)[1] == "REDUCE"


@pytest.mark.integration
def test_a_holding_above_capacity_is_not_bought_up_to_the_limit() -> None:
    """The existing 15% is existing risk, not a 2% purchase instruction."""
    decision = evaluate(
        [_intent(1, 0.15)],
        _snapshot({1: 0.15}),
        _policy(max_position_adv_fraction=0.05, max_position_weight=0.25),
        risk=_risk([1], adv=400_000.0),
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.15)
    assert _actions(decision)[1] == "HOLD"


@pytest.mark.integration
def test_a_holding_above_capacity_may_be_exited_entirely() -> None:
    decision = evaluate(
        [],
        _snapshot({1: 0.15}),
        _policy(max_position_adv_fraction=0.05),
        risk=_risk([1], adv=400_000.0),
    )

    assert decision.approved.targets == {}
    assert _actions(decision)[1] == "EXIT"


@pytest.mark.integration
def test_an_unknown_adv_blocks_an_entry_under_the_fail_safe_policy() -> None:
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(max_position_adv_fraction=0.05),
        risk=_risk([1], adv=None),
    )
    rows = _rows(decision)

    assert decision.approved.targets == {}
    assert ReasonCode.LIQUIDITY_UNKNOWN in rows[1]["reason_codes"]
    assert _report(decision)["liquidity_unknown"] == ["COMPANY:1"]


@pytest.mark.integration
def test_an_unknown_adv_still_permits_an_exit() -> None:
    decision = evaluate(
        [], _snapshot({1: 0.10}), _policy(max_position_adv_fraction=0.05), risk=_risk([1], adv=None)
    )

    assert _actions(decision)[1] == "EXIT"


@pytest.mark.integration
def test_an_unknown_adv_holds_an_existing_position_without_increasing_it() -> None:
    decision = evaluate(
        [_intent(1, 0.20)],
        _snapshot({1: 0.08}),
        _policy(max_position_adv_fraction=0.05, max_position_weight=0.25),
        risk=_risk([1], adv=None),
    )

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.08)
    assert _actions(decision)[1] == "HOLD"


@pytest.mark.integration
def test_ignoring_the_liquidity_metric_permits_the_entry() -> None:
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(
            max_position_adv_fraction=0.05,
            missing_liquidity_policy=MissingMetricPolicy.IGNORE_METRIC,
        ),
        risk=_risk([1], adv=None),
    )

    assert decision.approved.targets == {"COMPANY:1": 0.10}


@pytest.mark.integration
def test_requiring_the_liquidity_metric_refuses_the_decision() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.LIQUIDITY_UNKNOWN):
        evaluate(
            [_intent(1, 0.10)],
            _snapshot(),
            _policy(
                max_position_adv_fraction=0.05,
                missing_liquidity_policy=MissingMetricPolicy.REQUIRE_METRIC,
            ),
            risk=_risk([1], adv=None),
        )


@pytest.mark.integration
def test_the_liquidity_control_makes_no_market_impact_claim() -> None:
    decision = evaluate(
        [_intent(1, 0.10)],
        _snapshot(),
        _policy(max_position_adv_fraction=0.05),
        risk=_risk([1], adv=400_000.0),
    )
    body = _report(decision)

    assert body["estimates_no_market_impact"] is True
    assert body["makes_no_performance_claim"] is True


# --------------------------------------------------------------------------- #
# §62-§68 drawdown
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_drawdown_above_every_threshold_leaves_the_base_limits_alone() -> None:
    policy = EXAMPLE_ADVANCED_RISK_POLICY_V1
    effective = derive_effective_policy(policy, _risk([1], nav=96.0, peak=100.0))

    assert effective.drawdown == pytest.approx(-0.04)
    assert effective.active_band is None
    assert effective.mode is policy.mode
    assert effective.max_gross_exposure == policy.max_gross_exposure
    assert effective.tightened is False


@pytest.mark.integration
def test_a_caution_band_tightens_the_gross_limit() -> None:
    policy = EXAMPLE_ADVANCED_RISK_POLICY_V1
    effective = derive_effective_policy(policy, _risk([1], nav=85.0, peak=100.0))

    assert effective.active_band is not None
    assert effective.active_band["label"] == "caution"
    assert effective.max_gross_exposure < policy.max_gross_exposure
    assert effective.mode is PolicyMode.NORMAL
    assert effective.tightened is True


@pytest.mark.integration
def test_a_tightened_gross_limit_binds_the_approved_exposure() -> None:
    intents = [_intent(n, 0.10) for n in range(1, 11)]

    decision = evaluate(
        intents,
        _snapshot(),
        _policy(drawdown_bands=(DrawdownBand(threshold=-0.10, max_gross_multiplier=0.50),)),
        risk=_risk(list(range(1, 11)), nav=85.0, peak=100.0),
    )
    rows = _rows(decision)

    assert decision.approved.gross_exposure <= 0.50 + 1e-7
    assert _report(decision)["effective_policy"]["max_gross_exposure"] == pytest.approx(0.50)
    assert any(ReasonCode.DRAWDOWN_GROSS_LIMIT in row["reason_codes"] for row in rows.values())


@pytest.mark.integration
def test_a_drawdown_band_can_impose_reduce_only() -> None:
    policy = _policy(
        drawdown_bands=(
            DrawdownBand(threshold=-0.20, max_gross_multiplier=1.0, mode=PolicyMode.REDUCE_ONLY),
        )
    )
    held = {1: 0.05, 3: 0.07, 4: 0.06}
    intents = [_intent(1, 0.09), _intent(2, 0.05), _intent(3, 0.04)]

    decision = evaluate(
        intents, _snapshot(held), policy, risk=_risk([1, 2, 3, 4], nav=75.0, peak=100.0)
    )
    rows, actions = _rows(decision), _actions(decision)

    assert _report(decision)["effective_policy"]["mode"] == "REDUCE_ONLY"
    assert actions[1] == "HOLD"
    assert ReasonCode.DRAWDOWN_REDUCE_ONLY in rows[1]["reason_codes"]
    assert "COMPANY:2" not in decision.approved.targets
    assert ReasonCode.DRAWDOWN_REDUCE_ONLY in rows[2]["reason_codes"]
    assert actions[3] == "REDUCE"
    assert actions[4] == "EXIT"


@pytest.mark.integration
def test_a_drawdown_band_can_halt_without_liquidating() -> None:
    policy = _policy(
        drawdown_bands=(
            DrawdownBand(threshold=-0.30, max_gross_multiplier=1.0, mode=PolicyMode.HALTED),
        )
    )
    held = {1: 0.05, 2: 0.06}

    decision = evaluate(
        [_intent(1, 0.10), _intent(3, 0.05)],
        _snapshot(held),
        policy,
        risk=_risk([1, 2, 3], nav=60.0, peak=100.0),
    )
    rows = _rows(decision)

    assert _report(decision)["effective_policy"]["mode"] == "HALTED"
    assert decision.approved.targets == {"COMPANY:1": 0.05, "COMPANY:2": 0.06}
    assert decision.delta_summary["positions_exited"] == 0
    assert decision.delta_summary["weight_turnover_estimate"] == 0.0
    assert ReasonCode.DRAWDOWN_HALTED in rows[3]["reason_codes"]


@pytest.mark.integration
def test_a_base_mode_stricter_than_the_band_is_never_loosened() -> None:
    policy = _policy(
        mode=PolicyMode.HALTED,
        drawdown_bands=(DrawdownBand(threshold=-0.05, mode=PolicyMode.NORMAL),),
    )

    effective = derive_effective_policy(policy, _risk([1], nav=90.0, peak=100.0))

    assert effective.mode is PolicyMode.HALTED
    assert effective.base_mode is PolicyMode.HALTED


@pytest.mark.integration
def test_mode_severity_is_ordered_and_the_stricter_always_wins() -> None:
    assert MODE_SEVERITY[PolicyMode.NORMAL] < MODE_SEVERITY[PolicyMode.REDUCE_ONLY]
    assert MODE_SEVERITY[PolicyMode.REDUCE_ONLY] < MODE_SEVERITY[PolicyMode.HALTED]
    assert stricter_mode(PolicyMode.NORMAL, PolicyMode.HALTED) is PolicyMode.HALTED
    assert stricter_mode(PolicyMode.HALTED, PolicyMode.NORMAL) is PolicyMode.HALTED
    assert stricter_mode(PolicyMode.REDUCE_ONLY, PolicyMode.NORMAL) is PolicyMode.REDUCE_ONLY


@pytest.mark.integration
def test_the_deepest_triggered_band_is_the_one_in_force() -> None:
    policy = EXAMPLE_ADVANCED_RISK_POLICY_V1

    for nav, expected in ((98.0, None), (85.0, "caution"), (75.0, "reduce-only"), (60.0, "halted")):
        effective = derive_effective_policy(policy, _risk([1], nav=nav, peak=100.0))
        label = effective.active_band["label"] if effective.active_band else None
        assert label == expected, (nav, label)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bands", "match"),
    [
        (
            (DrawdownBand(threshold=-0.20), DrawdownBand(threshold=-0.10)),
            "shallowest to deepest",
        ),
        (
            (DrawdownBand(threshold=-0.10), DrawdownBand(threshold=-0.10)),
            "shallowest to deepest",
        ),
        (
            (
                DrawdownBand(threshold=-0.10, max_gross_multiplier=0.5),
                DrawdownBand(threshold=-0.20, max_gross_multiplier=0.9),
            ),
            "may not permit more risk",
        ),
        (
            (
                DrawdownBand(threshold=-0.10, mode=PolicyMode.HALTED),
                DrawdownBand(threshold=-0.20, mode=PolicyMode.NORMAL),
            ),
            "weaker than",
        ),
    ],
)
def test_an_inconsistent_band_ladder_is_refused(
    bands: tuple[DrawdownBand, ...], match: str
) -> None:
    with pytest.raises(PolicyError, match=match):
        validate_bands(bands)


@pytest.mark.integration
@pytest.mark.parametrize("multiplier", [1.5, -0.1])
def test_a_multiplier_outside_zero_to_one_is_refused(multiplier: float) -> None:
    with pytest.raises(PolicyError, match="between 0 and 1"):
        DrawdownBand(threshold=-0.10, max_gross_multiplier=multiplier)


@pytest.mark.integration
def test_a_positive_drawdown_threshold_is_refused() -> None:
    with pytest.raises(PolicyError, match="non-positive"):
        DrawdownBand(threshold=0.10)


@pytest.mark.integration
def test_the_drawdown_overlay_can_only_tighten() -> None:
    policy = EXAMPLE_ADVANCED_RISK_POLICY_V1

    for nav in (100.0, 95.0, 85.0, 75.0, 60.0, 10.0):
        effective = derive_effective_policy(policy, _risk([1], nav=nav, peak=100.0))
        assert effective.max_gross_exposure <= policy.max_gross_exposure
        assert MODE_SEVERITY[effective.mode] >= MODE_SEVERITY[policy.mode]


@pytest.mark.integration
def test_a_peak_below_the_current_nav_is_refused() -> None:
    with pytest.raises(RiskInputError, match="below current NAV"):
        PortfolioRiskSnapshot(as_of=AS_OF, portfolio_nav=110.0, portfolio_peak_nav=100.0)


@pytest.mark.integration
def test_the_drawdown_formula_is_nav_over_peak_minus_one() -> None:
    snapshot = PortfolioRiskSnapshot(as_of=AS_OF, portfolio_nav=82.0, portfolio_peak_nav=100.0)

    assert snapshot.drawdown == pytest.approx(-0.18)
    assert snapshot.drawdown <= 0.0


# --------------------------------------------------------------------------- #
# §37-§40, §72-§74 order, monotonicity and additivity
# --------------------------------------------------------------------------- #


def _all_binding() -> tuple[
    list[StrategyIntent], PortfolioSnapshot, RiskPolicy, PortfolioRiskSnapshot
]:
    """A case where the weight cap, liquidity and the volatility ceiling all bind."""
    policy = _policy(
        max_position_weight=0.15,
        max_positions=4,
        min_cash_weight=0.05,
        max_portfolio_volatility=0.12,
        max_position_adv_fraction=0.05,
        max_trade_adv_fraction=0.03,
        drawdown_bands=(DrawdownBand(threshold=-0.10, max_gross_multiplier=0.80),),
    )
    intents = [
        _intent(1, 0.40, priority=100.0),  # above the per-name cap
        _intent(2, 0.15, priority=90.0),  # ADV-limited
        _intent(3, 0.15, priority=80.0),
        _intent(4, 0.15, priority=70.0),
        _intent(5, 0.15, priority=60.0),  # beyond max_positions
    ]
    risk = PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=NAV,
        portfolio_peak_nav=NAV / 0.85,
        metrics=(
            _metrics(1, volatility=0.35),
            _metrics(2, volatility=0.35, adv=1_000_000.0),
            _metrics(3, volatility=0.35),
            _metrics(4, volatility=0.35),
            _metrics(5, volatility=0.35),
        ),
        covariance=_covariance([1, 2, 3, 4, 5], volatility=0.35, correlation=0.30),
        source_fingerprint="risk",
    )
    return intents, _snapshot({1: 0.02}), policy, risk


@pytest.mark.integration
def test_every_control_binding_at_once_is_deterministic() -> None:
    intents, snapshot, policy, risk = _all_binding()

    first = evaluate(intents, snapshot, policy, risk=risk)
    second = evaluate(list(reversed(intents)), snapshot, policy, risk=risk)

    assert first.approved.targets == second.approved.targets
    assert first.approved.fingerprint() == second.approved.fingerprint()
    assert first.execution_fingerprint == second.execution_fingerprint
    assert _report(first)["fingerprint"] == _report(second)["fingerprint"]
    assert [row["reason_codes"] for row in first.approved.risk_decisions] == [
        row["reason_codes"] for row in second.approved.risk_decisions
    ]


@pytest.mark.integration
def test_every_control_binding_at_once_respects_every_limit() -> None:
    intents, snapshot, policy, risk = _all_binding()

    decision = evaluate(intents, snapshot, policy, risk=risk)
    report = _report(decision)

    assert len(decision.approved.targets) <= policy.max_positions
    assert all(w <= policy.max_position_weight + 1e-9 for w in decision.approved.targets.values())
    assert (
        decision.approved.gross_exposure <= report["effective_policy"]["max_gross_exposure"] + 1e-7
    )
    assert decision.approved.cash_target_weight >= policy.min_cash_weight - 1e-7
    assert policy.max_portfolio_volatility is not None
    assert report["portfolio_volatility_after_risk"] <= policy.max_portfolio_volatility + 1e-7


@pytest.mark.integration
def test_no_risk_control_ever_increases_a_target_above_its_pre_risk_value() -> None:
    intents, snapshot, policy, risk = _all_binding()

    decision = evaluate(intents, snapshot, policy, risk=risk)
    constructed = {
        f"{kind}:{number}": weight
        for (kind, number), weight in dict(
            zip(
                [i.identity.key for i in intents],
                [i.requested_weight or 0.0 for i in intents],
                strict=True,
            )
        ).items()
    }

    for label, approved in decision.approved.targets.items():
        assert approved <= constructed[label] + 1e-7, (label, approved, constructed[label])


@pytest.mark.integration
def test_a_liquidity_reduction_is_not_restored_by_the_budget_redistribution() -> None:
    """The property §73 asks for.

    The normalising mode fills the investable budget, which is exactly the
    mechanism that could hand a liquidity-capped name its risk back. The ceiling
    bounds the redistribution, so it cannot.
    """
    from stock_screener.portfolio_risk.policy import ConstructionMode

    policy = _policy(
        construction_mode=ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET,
        max_position_weight=0.25,
        max_positions=4,
        max_position_adv_fraction=0.05,
    )
    intents = [_intent(n, 0.10) for n in range(1, 5)]
    # COMPANY:1 has a thin ADV: 400,000 x 5% = 20,000 of NAV, so 2%.
    risk = PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=NAV,
        portfolio_peak_nav=NAV,
        metrics=(
            _metrics(1, adv=400_000.0),
            _metrics(2),
            _metrics(3),
            _metrics(4),
        ),
        covariance=_covariance([1, 2, 3, 4]),
        source_fingerprint="risk",
    )

    decision = evaluate(intents, _snapshot(), policy, risk=risk)

    assert decision.approved.targets["COMPANY:1"] == pytest.approx(0.02)
    assert ReasonCode.POSITION_LIQUIDITY_LIMIT in _rows(decision)[1]["reason_codes"]
    # The budget the cap freed becomes cash. It is not handed to the other names,
    # which would leave aggregate risk unchanged and defeat the liquidity limit.
    for label in ("COMPANY:2", "COMPANY:3", "COMPANY:4"):
        assert decision.approved.targets[label] == pytest.approx(0.2375)
    assert decision.approved.cash_target_weight > policy.min_cash_weight


@pytest.mark.integration
def test_volatility_scaling_runs_after_liquidity_so_the_ceiling_still_holds() -> None:
    intents, snapshot, policy, risk = _all_binding()

    decision = evaluate(intents, snapshot, policy, risk=risk)
    order = decision.manifest["advanced_risk_stage_order"]

    assert order.index("position_liquidity_capacity") < order.index("portfolio_volatility_ceiling")
    assert order == list(ADVANCED_STAGE_ORDER)
    assert _report(decision)["portfolio_volatility_after_risk"] <= 0.12 + 1e-7


@pytest.mark.integration
def test_multiple_constraints_on_one_identity_are_all_recorded() -> None:
    # 20% requested, 15% weight cap, ADV capacity 2%, trade capacity 1%.
    policy = _policy(
        max_position_weight=0.15,
        max_position_adv_fraction=0.05,
        max_trade_adv_fraction=0.025,
    )
    risk = _risk([1], adv=400_000.0)

    decision = evaluate([_intent(1, 0.20)], _snapshot(), policy, risk=risk)
    codes = _rows(decision)[1]["reason_codes"]

    assert ReasonCode.MAX_POSITION_WEIGHT in codes
    assert ReasonCode.POSITION_LIQUIDITY_LIMIT in codes
    assert ReasonCode.TRADE_LIQUIDITY_LIMIT in codes
    # Earlier reasons are never overwritten by later ones.
    assert codes.index(ReasonCode.MAX_POSITION_WEIGHT) < codes.index(
        ReasonCode.POSITION_LIQUIDITY_LIMIT
    )
    assert len(codes) == len(set(codes))


@pytest.mark.integration
def test_the_reason_summary_counts_every_code_deterministically() -> None:
    intents, snapshot, policy, risk = _all_binding()

    summary = _report(evaluate(intents, snapshot, policy, risk=risk))["reason_summary"]

    assert summary == dict(sorted(summary.items()))
    assert all(count >= 1 for count in summary.values())


# --------------------------------------------------------------------------- #
# §7, §75, §76 risk timing
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_risk_snapshot_dated_after_the_decision_is_refused() -> None:
    with pytest.raises(ValidationError, match=ReasonCode.RISK_SNAPSHOT_AFTER_DECISION):
        evaluate([_intent(1, 0.05)], _snapshot(), _policy(), risk=_risk([1], as_of=LATER))


@pytest.mark.integration
def test_a_metric_dated_after_the_decision_is_refused() -> None:
    risk = PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=NAV,
        portfolio_peak_nav=NAV,
        metrics=(_metrics(1, as_of=LATER),),
        covariance=_covariance([1]),
    )

    with pytest.raises(ValidationError, match=ReasonCode.RISK_METRIC_AFTER_DECISION):
        evaluate([_intent(1, 0.05)], _snapshot(), _policy(), risk=risk)


@pytest.mark.integration
def test_a_metric_from_before_the_decision_is_admissible() -> None:
    risk = PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=NAV,
        portfolio_peak_nav=NAV,
        metrics=(_metrics(1, as_of=EARLIER),),
        covariance=_covariance([1]),
    )

    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy(), risk=risk)

    assert decision.approved.targets == {"COMPANY:1": 0.05}


@pytest.mark.integration
def test_a_configured_freshness_bound_is_enforced() -> None:
    risk = PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=NAV,
        portfolio_peak_nav=NAV,
        metrics=(_metrics(1, as_of=EARLIER),),
        covariance=_covariance([1]),
    )

    with pytest.raises(ValidationError, match=ReasonCode.RISK_METRIC_STALE):
        evaluate(
            [_intent(1, 0.05)],
            _snapshot(),
            _policy(max_risk_metric_age_seconds=3600.0),
            risk=risk,
        )


@pytest.mark.integration
def test_no_freshness_bound_is_invented_when_the_policy_sets_none() -> None:
    risk = PortfolioRiskSnapshot(
        as_of=AS_OF,
        portfolio_nav=NAV,
        portfolio_peak_nav=NAV,
        metrics=(_metrics(1, as_of="2020-01-01T00:00:00-05:00"),),
        covariance=_covariance([1]),
    )

    decision = evaluate([_intent(1, 0.05)], _snapshot(), _policy(), risk=risk)

    assert decision.manifest["risk_timing"]["freshness_enforced"] is False
    assert decision.approved.targets == {"COMPANY:1": 0.05}


# --------------------------------------------------------------------------- #
# §45-§48, §77 fingerprint drift
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_approved_portfolio_binds_the_risk_snapshot_and_effective_policy() -> None:
    risk = _risk([1])
    policy = _policy(max_portfolio_volatility=0.50)

    decision = evaluate([_intent(1, 0.05)], _snapshot(), policy, risk=risk)
    bindings = decision.approved.bindings

    assert bindings["risk_snapshot_fingerprint"] == risk.fingerprint()
    assert risk.covariance is not None
    assert bindings["covariance_fingerprint"] == risk.covariance.fingerprint()
    assert bindings["engine_version"] == "PORTFOLIO_RISK_ENGINE_V1_1"
    assert bindings["effective_risk_policy_fingerprint"]
    assert (
        decision.manifest["effective_risk_policy_fingerprint"]
        == bindings["effective_risk_policy_fingerprint"]
    )


@pytest.mark.integration
@pytest.mark.parametrize("what", ["adv", "volatility", "covariance", "drawdown"])
def test_changing_one_risk_input_changes_the_output_fingerprint(what: str) -> None:
    policy = _policy(
        max_portfolio_volatility=0.50,
        max_position_adv_fraction=0.50,
        drawdown_bands=(DrawdownBand(threshold=-0.10, max_gross_multiplier=0.9),),
    )
    intents = [_intent(1, 0.05), _intent(2, 0.05)]
    base_risk = _risk([1, 2])
    base = evaluate(intents, _snapshot(), policy, risk=base_risk)

    if what == "adv":
        changed_risk = _risk([1, 2], adv=AMPLE_ADV / 2)
    elif what == "volatility":
        changed_risk = _risk([1, 2], volatility=0.21)
    elif what == "covariance":
        changed_risk = _risk([1, 2], correlation=0.5)
    else:
        changed_risk = _risk([1, 2], nav=80.0, peak=100.0)

    changed = evaluate(intents, _snapshot(), policy, risk=changed_risk)

    assert changed_risk.fingerprint() != base_risk.fingerprint()
    assert changed.approved.fingerprint() != base.approved.fingerprint()


@pytest.mark.integration
def test_the_covariance_fingerprint_moves_with_every_part_of_it() -> None:
    base = _covariance([1, 2])

    one_value = CovarianceSnapshot(
        identities=base.identities,
        matrix=((0.04, 0.001), (0.001, 0.04)),
        as_of=base.as_of,
        source_fingerprint=base.source_fingerprint,
    )
    one_identity = _covariance([1, 3])
    other_as_of = CovarianceSnapshot(
        identities=base.identities,
        matrix=base.matrix,
        as_of=EARLIER,
        source_fingerprint=base.source_fingerprint,
    )
    other_annualization = CovarianceSnapshot(
        identities=base.identities,
        matrix=base.matrix,
        as_of=base.as_of,
        annualization="52 weeks per year",
        source_fingerprint=base.source_fingerprint,
    )

    digests = {
        base.fingerprint(),
        one_value.fingerprint(),
        one_identity.fingerprint(),
        other_as_of.fingerprint(),
        other_annualization.fingerprint(),
    }
    assert len(digests) == 5


@pytest.mark.integration
def test_the_effective_policy_fingerprint_moves_with_the_drawdown() -> None:
    policy = EXAMPLE_ADVANCED_RISK_POLICY_V1

    shallow = derive_effective_policy(policy, _risk([1], nav=98.0, peak=100.0))
    deep = derive_effective_policy(policy, _risk([1], nav=75.0, peak=100.0))

    assert shallow.fingerprint() != deep.fingerprint()
    assert shallow.base_policy_fingerprint == deep.base_policy_fingerprint


@pytest.mark.integration
def test_the_base_policy_is_never_mutated_by_the_overlay() -> None:
    policy = EXAMPLE_ADVANCED_RISK_POLICY_V1
    before = policy.fingerprint()

    derive_effective_policy(policy, _risk([1], nav=50.0, peak=100.0))

    assert policy.fingerprint() == before
    assert policy.mode is PolicyMode.NORMAL
    assert policy.max_gross_exposure == 1.00


# --------------------------------------------------------------------------- #
# §50, §78, §88, §89 inertness, purity and independence
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_execution_intents_stay_inert_under_every_risk_constraint() -> None:
    intents, snapshot, policy, risk = _all_binding()

    decision = evaluate(intents, snapshot, policy, risk=risk)

    for intent in decision.execution_intents:
        body = intent.as_json()
        assert body["is_an_order"] is False
        assert body["broker_fields"] is None
        for forbidden in ("limit_price", "order_type", "time_in_force", "route", "quantity"):
            assert forbidden not in body
    assert decision.manifest["orders_submitted"] == 0
    assert decision.manifest["broker_requests"] == 0


@pytest.mark.integration
def test_advanced_evaluation_completes_with_network_and_database_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import sqlite3

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the advanced risk engine reached outside its inputs")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)

    intents, snapshot, policy, risk = _all_binding()
    decision = evaluate(intents, snapshot, policy, risk=risk)

    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_1"
    assert decision.risk_report is not None


@pytest.mark.integration
def test_the_package_imports_no_strategy_or_research_module() -> None:
    import ast

    package = REPO_ROOT / "src" / "stock_screener" / "portfolio_risk"
    forbidden = {
        "membership_hypothesis",
        "prospective_capture",
        "prospective_protocol",
        "prospective_runner",
        "prospective_source",
        "scoring",
        "backtest",
        "backtest_harness",
        "research",
        "deep_research",
    }
    reachable: set[str] = set()
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    reachable.update(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom) and node.module:
                reachable.update(node.module.split("."))

    # One exception, and it is identity rather than strategy: the COMPANY and
    # HISTORICAL constants. Nothing about scoring, selection or H2 is reachable.
    assert reachable & forbidden == {"backtest_harness"}
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        # The coupling that matters is to the score itself, not to a fixture label:
        # P1.2's example sleeve is named COMPOUNDER_EXAMPLE and is wired to no
        # implementation, which is the point of calling it an example.
        assert "COMPOUNDER_V1_2" not in text, path.name
        assert "membership_hypothesis" not in text, path.name
        assert "score_company" not in text, path.name


# --------------------------------------------------------------------------- #
# §80-§82, §84 the risk report and the advanced golden fixture
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_report_shows_both_sides_of_every_number() -> None:
    intents, snapshot, policy, risk = _all_binding()

    report = _report(evaluate(intents, snapshot, policy, risk=risk))

    assert report["exposure"]["requested_gross"] >= report["exposure"]["approved_gross"]
    assert report["exposure"]["constructed_gross"] >= report["exposure"]["approved_gross"]
    assert report["portfolio_volatility_before_risk"] is not None
    assert report["portfolio_volatility_after_risk"] is not None
    assert report["base_policy"]["fingerprint"] != report["effective_policy"]["fingerprint"]
    assert report["counts"]["requested_positions"] >= report["counts"]["approved_positions"]
    assert report["per_position"]
    for row in report["per_position"]:
        assert row["weight_times_volatility"] is not None
        assert row["constructed_weight"] is not None


@pytest.mark.integration
def test_the_report_records_component_variance_summing_to_the_portfolio_variance() -> None:
    intents, snapshot, policy, risk = _all_binding()

    diagnostics = _report(evaluate(intents, snapshot, policy, risk=risk))["covariance_diagnostics"]

    assert sum(diagnostics["component_variance"].values()) == pytest.approx(
        diagnostics["portfolio_variance"]
    )
    assert diagnostics["maximum_pairwise_correlation"] == pytest.approx(0.30)
    assert "no correlation or risk-budget rule is enforced" in diagnostics["diagnostics_only"]


@pytest.mark.integration
def test_the_advanced_fixture_exercises_every_p1_1_control() -> None:
    decision = evaluate_request(FIXTURES / "advanced_example_request.json")
    report = _report(decision)
    rows = _rows(decision)

    assert decision.approved.engine_version == "PORTFOLIO_RISK_ENGINE_V1_1"
    # a per-name weight cap
    assert ReasonCode.MAX_POSITION_WEIGHT in rows[1]["reason_codes"]
    # a position capacity cap
    assert ReasonCode.POSITION_LIQUIDITY_LIMIT in rows[2]["reason_codes"]
    # a trade capacity cap
    assert ReasonCode.TRADE_LIQUIDITY_LIMIT in rows[3]["reason_codes"]
    # portfolio volatility scaling
    assert report["volatility_scaling_applied"] is True
    assert report["portfolio_volatility_after_risk"] == pytest.approx(0.12)
    # a drawdown band tightening gross
    assert report["effective_policy"]["active_band"]["label"] == "caution"
    assert report["effective_policy"]["max_gross_exposure"] < 1.0
    # an exit and a cash reserve
    assert _actions(decision)[99] == "EXIT"
    assert decision.approved.cash_target_weight >= 0.05


@pytest.mark.integration
def test_the_advanced_fixture_still_matches_its_golden_output() -> None:
    produced = evaluate_request(FIXTURES / "advanced_example_request.json").as_json()
    produced["manifest"].pop("evaluated_at_utc")
    expected = json.loads((FIXTURES / "advanced_example_request.golden.json").read_text())
    expected.pop("_note", None)
    expected["manifest"].pop("evaluated_at_utc", None)
    produced["manifest"]["code_commit"] = expected["manifest"].get("code_commit")

    assert produced == expected


@pytest.mark.integration
def test_the_advanced_fixture_policy_is_labelled_unauthorised() -> None:
    body = json.loads((FIXTURES / "advanced_example_request.json").read_text())

    assert body["policy"]["production_authorized"] is False
    assert "NOT_PRODUCTION_AUTHORIZED" in body["note"]
    assert "none was derived from historical return" in body["note"]


@pytest.mark.integration
def test_the_p1_0_fixture_is_unaffected_by_p1_1() -> None:
    """P1.0's golden must still reproduce, which is what makes V1 a real version."""
    produced = evaluate_request(FIXTURES / "example_request.json").as_json()

    assert produced["approved_portfolio"]["engine_version"] == "PORTFOLIO_RISK_ENGINE_V1"
    assert "risk_report" not in produced


# --------------------------------------------------------------------------- #
# §79 generated invariants
# --------------------------------------------------------------------------- #


def _generated_cases() -> list[
    tuple[str, list[StrategyIntent], PortfolioSnapshot, RiskPolicy, PortfolioRiskSnapshot]
]:
    """Deterministic generated cases across the parameter space.

    A linear congruential sequence rather than a property-testing dependency: the
    repository has none, and adding one for this would be a large dependency for a
    small benefit. The cases are fixed, so a failure is reproducible by name.
    """
    cases = []
    seed = 20_261_002
    for index in range(24):
        seed = (seed * 1_103_515_245 + 12_345) % (2**31)
        draw = seed / (2**31)
        count = 1 + index % 8
        weight = 0.02 + draw * 0.30
        policy = _policy(
            max_position_weight=0.05 + draw * 0.20,
            max_positions=max(1, count - index % 3),
            min_cash_weight=draw * 0.20,
            max_gross_exposure=0.5 + draw * 0.5,
            max_portfolio_volatility=0.02 + draw * 0.12,
            max_position_adv_fraction=0.01 + draw * 0.10,
            max_trade_adv_fraction=0.005 + draw * 0.05,
            drawdown_bands=(
                DrawdownBand(threshold=-0.05 - draw * 0.10, max_gross_multiplier=0.5 + draw * 0.5),
            ),
        )
        numbers = list(range(1, count + 1))
        intents = [_intent(n, weight, priority=float(count - n)) for n in numbers]
        held = {n: weight / 2 for n in numbers if n % 3 == 0}
        risk = PortfolioRiskSnapshot(
            as_of=AS_OF,
            portfolio_nav=NAV,
            portfolio_peak_nav=NAV / (1.0 - draw * 0.25),
            metrics=tuple(
                _metrics(
                    n,
                    volatility=0.10 + draw * 0.50,
                    adv=None if n % 7 == 0 else 1_000_000.0 * (1 + n),
                )
                for n in numbers
            ),
            covariance=_covariance(numbers, volatility=0.10 + draw * 0.50, correlation=draw * 0.8),
            source_fingerprint="generated",
        )
        cases.append((f"case-{index:02d}", intents, _snapshot(held), policy, risk))
    return cases


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "intents", "snapshot", "policy", "risk"),
    _generated_cases(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_generated_case_satisfies_every_invariant(
    name: str,
    intents: list[StrategyIntent],
    snapshot: PortfolioSnapshot,
    policy: RiskPolicy,
    risk: PortfolioRiskSnapshot,
) -> None:
    decision = evaluate(intents, snapshot, policy, risk=risk)
    report = _report(decision)
    targets = decision.approved.targets
    effective_gross = report["effective_policy"]["max_gross_exposure"]
    constructed = {
        f"{i.identity.kind}:{i.identity.identity_id}": (i.requested_weight or 0.0) for i in intents
    }
    current = {f"{kind}:{number}": weight for (kind, number), weight in snapshot.weights.items()}

    assert all(weight >= 0 for weight in targets.values()), name
    assert all(math.isfinite(weight) for weight in targets.values()), name
    assert all(weight <= policy.max_position_weight + 1e-7 for weight in targets.values()), name
    assert len(targets) <= policy.max_positions, name
    assert decision.approved.gross_exposure <= effective_gross + 1e-7, name
    assert decision.approved.cash_target_weight >= policy.min_cash_weight - 1e-7, name
    assert sum(targets.values()) + decision.approved.cash_target_weight == pytest.approx(
        1.0, abs=1e-7
    ), name
    assert len(targets) == len(set(targets)), name
    after = report["portfolio_volatility_after_risk"]
    if after is not None and policy.max_portfolio_volatility is not None:
        assert after <= policy.max_portfolio_volatility + 1e-7, name
    for label, approved in targets.items():
        reference = constructed.get(label, current.get(label, 0.0))
        assert approved <= reference + 1e-7, (name, label, approved, reference)


@pytest.mark.integration
def test_the_generated_cases_cover_the_space_they_claim_to() -> None:
    """A suite that silently stopped exercising a control would still pass."""
    seen: set[str] = set()
    for _name, intents, snapshot, policy, risk in _generated_cases():
        report = _report(evaluate(intents, snapshot, policy, risk=risk))
        seen.update(report["reason_summary"])

    for code in (
        ReasonCode.MAX_POSITION_WEIGHT,
        ReasonCode.POSITION_LIQUIDITY_LIMIT,
        ReasonCode.PORTFOLIO_VOLATILITY_LIMIT,
        ReasonCode.LIQUIDITY_UNKNOWN,
    ):
        assert code in seen, code
