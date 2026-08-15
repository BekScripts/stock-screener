"""Risk penalties: applied after the score, never folded into it.

The signs are the thing to watch here. A penalty is negative, dilution is
positive when shares are being issued, and net cash is positive when the company
has more cash than debt — three conventions that would each produce a
plausible-looking ranking if inverted.
"""

from __future__ import annotations

import pytest

from domain import (
    MAX_TOTAL_PENALTY,
    RiskLevel,
    ScoreWarning,
    VolumeBasis,
    assess_risk,
    cash_runway_months,
    risk_level,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dilution", "expected"),
    [
        (-0.10, 0.0),
        (0.0, 0.0),
        (0.02, 0.0),
        (0.05, -1.0),
        (0.10, -3.0),
        (0.20, -6.0),
        (0.35, -10.0),
        (1.00, -10.0),
        (0.15, -4.5),
    ],
)
def test_dilution_scores_its_reference_points(dilution: float, expected: float, make) -> None:
    risk = assess_risk(make.metrics(share_count_growth_yoy=dilution))

    assert risk.dilution_penalty == pytest.approx(expected)


@pytest.mark.unit
def test_a_buyback_is_not_penalised(make) -> None:
    risk = assess_risk(make.metrics(share_count_growth_yoy=-0.08))

    assert risk.dilution_penalty == 0.0


@pytest.mark.unit
def test_unknown_dilution_is_not_treated_as_no_dilution(make) -> None:
    risk = assess_risk(make.metrics(share_count_growth_yoy=None))

    assert risk.dilution_penalty is None
    assert ScoreWarning.DILUTION_NOT_ASSESSED in risk.warnings
    assert risk.coverage < 1.0


@pytest.mark.unit
def test_runway_is_cash_over_the_trailing_monthly_burn(make) -> None:
    # $30M of cash against $60M of annual burn is six months.
    metrics = make.metrics(cash=30_000_000.0, ttm_free_cash_flow=-60_000_000.0)

    assert cash_runway_months(metrics) == pytest.approx(6.0)


@pytest.mark.unit
def test_six_months_of_runway_earns_the_full_penalty(make) -> None:
    risk = assess_risk(make.metrics(cash=30_000_000.0, ttm_free_cash_flow=-60_000_000.0))

    assert risk.runway_penalty == -10.0
    assert risk.cash_runway_months == pytest.approx(6.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("months", "expected"),
    [
        (3.0, -10.0),
        (6.0, -10.0),
        (9.0, -8.0),
        (12.0, -6.0),
        (15.0, -4.5),
        (18.0, -3.0),
        (24.0, -1.0),
        (36.0, 0.0),
    ],
)
def test_the_runway_penalty_follows_its_reference_points(
    months: float, expected: float, make
) -> None:
    burn = 120_000_000.0
    metrics = make.metrics(cash=burn / 12 * months, ttm_free_cash_flow=-burn)

    assert assess_risk(metrics).runway_penalty == pytest.approx(expected)


@pytest.mark.unit
def test_a_company_generating_cash_has_no_runway_penalty(make) -> None:
    risk = assess_risk(make.metrics(cash=10_000_000.0, ttm_free_cash_flow=25_000_000.0))

    assert risk.runway_penalty == 0.0
    assert risk.cash_runway_months is None
    assert ScoreWarning.RUNWAY_NOT_ASSESSED not in risk.warnings


@pytest.mark.unit
def test_a_company_generating_cash_has_no_finite_runway(make) -> None:
    assert cash_runway_months(make.metrics(ttm_free_cash_flow=1.0)) is None


@pytest.mark.unit
def test_unknown_cash_leaves_the_runway_unassessed(make) -> None:
    risk = assess_risk(make.metrics(cash=None, ttm_free_cash_flow=-60_000_000.0))

    assert risk.runway_penalty is None
    assert ScoreWarning.RUNWAY_NOT_ASSESSED in risk.warnings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("net_debt_ratio", "expected"),
    [(-0.50, 0.0), (0.0, 0.0), (0.25, 0.0), (0.50, -2.0), (0.75, -4.0), (1.00, -5.0), (3.00, -5.0)],
)
def test_severe_leverage_scores_its_reference_points(
    net_debt_ratio: float, expected: float, make
) -> None:
    market_cap = 1_000_000_000.0
    risk = assess_risk(make.metrics(market_cap=market_cap, net_cash=-net_debt_ratio * market_cap))

    assert risk.balance_sheet_penalty == pytest.approx(expected)


@pytest.mark.unit
def test_unknown_debt_leaves_leverage_unassessed(make) -> None:
    risk = assess_risk(make.metrics(debt=None, net_cash=None))

    assert risk.balance_sheet_penalty is None
    assert ScoreWarning.LEVERAGE_NOT_ASSESSED in risk.warnings


@pytest.mark.unit
def test_partial_market_volume_warns_rather_than_penalising(make) -> None:
    risk = assess_risk(make.metrics(liquidity_basis=VolumeBasis.PARTIAL))

    assert risk.liquidity_penalty == 0.0
    assert ScoreWarning.LIQUIDITY_UNVERIFIED in risk.warnings


@pytest.mark.unit
def test_a_healthy_company_is_barely_penalised(make) -> None:
    risk = assess_risk(
        make.metrics(
            share_count_growth_yoy=0.01,
            cash=200_000_000.0,
            ttm_free_cash_flow=48_000_000.0,
            net_cash=150_000_000.0,
        )
    )

    assert risk.total_penalty == 0.0
    assert risk.level is RiskLevel.LOW
    assert risk.coverage == 1.0


@pytest.mark.unit
def test_a_dilutive_cash_burning_company_is_heavily_penalised(make) -> None:
    risk = assess_risk(
        make.metrics(
            share_count_growth_yoy=0.40,
            cash=30_000_000.0,
            ttm_free_cash_flow=-60_000_000.0,
            market_cap=500_000_000.0,
            net_cash=-100_000_000.0,
        )
    )

    assert risk.dilution_penalty == -10.0
    assert risk.runway_penalty == -10.0
    assert risk.total_penalty == -20.0
    assert risk.level is RiskLevel.VERY_HIGH


@pytest.mark.unit
def test_the_total_penalty_is_floored(make) -> None:
    risk = assess_risk(
        make.metrics(
            share_count_growth_yoy=1.0,
            cash=1_000_000.0,
            ttm_free_cash_flow=-100_000_000.0,
            market_cap=100_000_000.0,
            net_cash=-500_000_000.0,
        )
    )

    assert risk.total_penalty == MAX_TOTAL_PENALTY


@pytest.mark.unit
@pytest.mark.parametrize(
    ("penalty", "expected"),
    [
        (0.0, RiskLevel.LOW),
        (-3.0, RiskLevel.LOW),
        (-3.5, RiskLevel.MEDIUM),
        (-8.0, RiskLevel.MEDIUM),
        (-8.5, RiskLevel.HIGH),
        (-15.0, RiskLevel.HIGH),
        (-15.5, RiskLevel.VERY_HIGH),
        (-25.0, RiskLevel.VERY_HIGH),
    ],
)
def test_the_risk_label_follows_the_total_penalty(penalty: float, expected: RiskLevel) -> None:
    assert risk_level(penalty) == expected


@pytest.mark.unit
def test_risk_coverage_reports_how_much_could_be_assessed(make) -> None:
    risk = assess_risk(make.metrics(share_count_growth_yoy=None, debt=None, net_cash=None))

    assert risk.coverage == pytest.approx(1 / 3, abs=1e-4)
