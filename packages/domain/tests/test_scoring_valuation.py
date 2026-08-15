"""The valuation component: 25 points, and which multiple it used."""

from __future__ import annotations

import pytest

from domain import (
    ComponentStatus,
    ValuationBasis,
    enterprise_value,
    growth_adjusted_points,
    score_valuation,
    valuation_multiple,
)


def _sub(component, name):
    return next(sub for sub in component.subscores if sub.name == name)


def _priced(make, *, multiple: float, growth: float = 0.30, revenue: float = 100_000_000.0):
    """Metrics whose enterprise value is exactly `multiple` times revenue."""
    return make.metrics(
        ttm_revenue=revenue,
        enterprise_value=multiple * revenue,
        market_cap=multiple * revenue,
        revenue_growth_yoy=growth,
    )


@pytest.mark.unit
def test_enterprise_value_adds_debt_and_subtracts_cash() -> None:
    assert enterprise_value(1_000_000_000.0, 100_000_000.0, 200_000_000.0) == 1_100_000_000.0


@pytest.mark.unit
def test_enterprise_value_is_unknown_when_debt_is_unknown() -> None:
    assert enterprise_value(1_000_000_000.0, 100_000_000.0, None) is None


@pytest.mark.unit
def test_enterprise_value_is_unknown_when_cash_is_unknown() -> None:
    assert enterprise_value(1_000_000_000.0, None, 200_000_000.0) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("multiple", "expected"),
    [
        (0.5, 15.0),
        (1.0, 15.0),
        (2.0, 13.0),
        (3.0, 11.0),
        (5.0, 8.0),
        (8.0, 5.0),
        (12.0, 2.0),
        (20.0, 0.0),
        (40.0, 0.0),
        (1.5, 14.0),
        (4.0, 9.5),
        (10.0, 3.5),
    ],
)
def test_the_primary_multiple_scores_its_reference_points(
    multiple: float, expected: float, make
) -> None:
    component, _ = score_valuation(_priced(make, multiple=multiple))

    assert _sub(component, "valuation_multiple").points == pytest.approx(expected)


@pytest.mark.unit
def test_enterprise_value_is_preferred_over_market_cap(make) -> None:
    metrics = make.metrics(
        ttm_revenue=100_000_000.0, market_cap=500_000_000.0, enterprise_value=300_000_000.0
    )

    multiple, basis = valuation_multiple(metrics)

    assert multiple == pytest.approx(3.0)
    assert basis is ValuationBasis.EV_TO_REVENUE


@pytest.mark.unit
def test_valuation_falls_back_to_price_to_sales_when_debt_is_unknown(make) -> None:
    metrics = make.metrics(
        ttm_revenue=100_000_000.0,
        market_cap=500_000_000.0,
        debt=None,
        net_cash=None,
        enterprise_value=None,
    )

    multiple, basis = valuation_multiple(metrics)

    assert multiple == pytest.approx(5.0)
    assert basis is ValuationBasis.PRICE_TO_SALES


@pytest.mark.unit
def test_the_fallback_is_recorded_on_the_component(make) -> None:
    metrics = make.metrics(enterprise_value=None, debt=None, net_cash=None)

    component, basis = score_valuation(metrics)

    assert basis is ValuationBasis.PRICE_TO_SALES
    assert _sub(component, "valuation_multiple").note == ValuationBasis.PRICE_TO_SALES.value


@pytest.mark.unit
def test_valuation_cannot_be_scored_without_trailing_revenue(make) -> None:
    component, basis = score_valuation(make.metrics(ttm_revenue=None))

    assert component.status is ComponentStatus.INSUFFICIENT_DATA
    assert basis is ValuationBasis.NOT_AVAILABLE


@pytest.mark.unit
def test_a_company_valued_below_its_net_cash_is_scored_as_cheap(make) -> None:
    metrics = make.metrics(ttm_revenue=100_000_000.0, enterprise_value=-50_000_000.0)

    component, _ = score_valuation(metrics)

    assert _sub(component, "valuation_multiple").points == 15.0


@pytest.mark.unit
def test_fast_growth_at_a_low_multiple_beats_slow_growth_at_a_high_one(make) -> None:
    cheap_and_growing, _ = score_valuation(_priced(make, multiple=3.0, growth=0.50))
    expensive_and_slow, _ = score_valuation(_priced(make, multiple=12.0, growth=0.15))

    assert cheap_and_growing.score > expensive_and_slow.score
    assert _sub(cheap_and_growing, "growth_adjusted_valuation").points == 7.0
    assert _sub(expensive_and_slow, "growth_adjusted_valuation").points == 1.0


@pytest.mark.unit
def test_extreme_growth_at_an_extreme_multiple_is_not_rewarded(make) -> None:
    component, _ = score_valuation(_priced(make, multiple=25.0, growth=1.00))

    assert _sub(component, "growth_adjusted_valuation").points == 1.0
    assert _sub(component, "valuation_multiple").points == 0.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("growth", "multiple", "expected"),
    [
        (0.50, 1.5, 7.0),
        (0.50, 3.0, 7.0),
        (0.50, 7.0, 5.0),
        (0.50, 15.0, 3.0),
        (0.50, 25.0, 1.0),
        (0.30, 3.0, 6.0),
        (0.30, 25.0, 1.0),
        (0.15, 1.5, 6.0),
        (0.15, 12.0, 1.0),
        (0.05, 1.5, 5.0),
        (0.05, 6.0, 1.0),
        (0.05, 25.0, 0.0),
    ],
)
def test_the_growth_adjusted_table_returns_its_stated_values(
    growth: float, multiple: float, expected: float
) -> None:
    assert growth_adjusted_points(multiple, growth) == expected


@pytest.mark.unit
def test_growth_adjusted_valuation_is_unavailable_without_growth() -> None:
    assert growth_adjusted_points(3.0, None) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fcf_yield", "expected"),
    [(-0.05, 0.0), (0.0, 0.0), (0.02, 1.0), (0.05, 2.0), (0.08, 3.0), (0.20, 3.0), (0.035, 1.5)],
)
def test_fcf_yield_scores_its_reference_points(fcf_yield: float, expected: float, make) -> None:
    market_cap = 1_000_000_000.0
    metrics = make.metrics(market_cap=market_cap, ttm_free_cash_flow=fcf_yield * market_cap)

    component, _ = score_valuation(metrics)

    assert _sub(component, "fcf_yield").points == pytest.approx(expected)


@pytest.mark.unit
def test_a_loss_making_company_scores_zero_for_fcf_yield_but_still_scores(make) -> None:
    component, _ = score_valuation(make.metrics(ttm_free_cash_flow=-50_000_000.0))

    assert _sub(component, "fcf_yield").points == 0.0
    assert component.status is ComponentStatus.SCORED


@pytest.mark.unit
def test_an_unknown_free_cash_flow_is_not_scored_as_zero(make) -> None:
    component, _ = score_valuation(make.metrics(ttm_free_cash_flow=None))

    assert _sub(component, "fcf_yield").points is None
    assert component.redistributed is True


@pytest.mark.unit
def test_a_cheap_profitable_fast_grower_scores_the_full_component(make) -> None:
    metrics = make.metrics(
        ttm_revenue=100_000_000.0,
        enterprise_value=80_000_000.0,
        market_cap=100_000_000.0,
        revenue_growth_yoy=0.60,
        ttm_free_cash_flow=15_000_000.0,
    )

    component, _ = score_valuation(metrics)

    assert component.score == 25.0
