"""The metrics Phase 2 added: trailing cash flow, margin trends, and EV.

Every expected value here is worked out on paper from the periods the builders
lay on a 91-day grid, so a failure means the calculation changed rather than the
fixture.
"""

from __future__ import annotations

import pytest

from domain import (
    build_company_metrics,
    enterprise_value,
    fcf_margin_change,
    gross_margin_change,
    operating_margin_change,
    recent_revenue_growth,
    ttm_free_cash_flow,
)


@pytest.mark.unit
def test_trailing_free_cash_flow_sums_the_latest_four_quarters(make) -> None:
    periods = [
        make.period(index, operating_cash_flow=10_000_000.0, capital_expenditure=2_000_000.0)
        for index in range(4)
    ]

    assert ttm_free_cash_flow(periods) == pytest.approx(32_000_000.0)


@pytest.mark.unit
def test_trailing_free_cash_flow_prefers_the_reported_figure(make) -> None:
    periods = [make.period(index, free_cash_flow=5_000_000.0) for index in range(4)]

    assert ttm_free_cash_flow(periods) == pytest.approx(20_000_000.0)


@pytest.mark.unit
def test_trailing_free_cash_flow_needs_four_quarters(make) -> None:
    periods = [make.period(index, free_cash_flow=5_000_000.0) for index in range(3)]

    assert ttm_free_cash_flow(periods) is None


@pytest.mark.unit
def test_trailing_free_cash_flow_is_unknown_when_one_quarter_cannot_be_derived(make) -> None:
    periods = [make.period(index, free_cash_flow=5_000_000.0) for index in range(1, 4)]
    periods.append(make.period(0, operating_cash_flow=1_000_000.0))

    assert ttm_free_cash_flow(periods) is None


@pytest.mark.unit
def test_capital_expenditure_is_an_outflow_whatever_its_sign(make) -> None:
    negative_convention = [
        make.period(index, operating_cash_flow=10_000_000.0, capital_expenditure=-2_000_000.0)
        for index in range(4)
    ]

    assert ttm_free_cash_flow(negative_convention) == pytest.approx(32_000_000.0)


@pytest.mark.unit
def test_gross_margin_change_compares_with_the_year_ago_quarter(make) -> None:
    periods = [
        make.period(4, revenue=100.0, gross_profit=40.0),
        make.period(0, revenue=100.0, gross_profit=45.0),
    ]

    assert gross_margin_change(periods) == pytest.approx(0.05)


@pytest.mark.unit
def test_gross_margin_change_is_unknown_without_a_year_ago_quarter(make) -> None:
    periods = [make.period(0, revenue=100.0, gross_profit=45.0)]

    assert gross_margin_change(periods) is None


@pytest.mark.unit
def test_operating_margin_change_is_negative_when_margins_deteriorate(make) -> None:
    periods = [
        make.period(4, revenue=100.0, operating_income=10.0),
        make.period(0, revenue=100.0, operating_income=2.0),
    ]

    assert operating_margin_change(periods) == pytest.approx(-0.08)


@pytest.mark.unit
def test_fcf_margin_change_measures_a_narrowing_burn(make) -> None:
    periods = [
        make.period(4, revenue=100.0, free_cash_flow=-30.0),
        make.period(0, revenue=100.0, free_cash_flow=-10.0),
    ]

    assert fcf_margin_change(periods) == pytest.approx(0.20)


@pytest.mark.unit
def test_recent_growth_returns_one_observation_per_quarter_newest_first(make) -> None:
    # Oldest first: four flat quarters, then four growing ones. The latest
    # quarter is 110 against the 100 it is compared with a year earlier.
    revenues = [100.0, 100.0, 100.0, 100.0, 150.0, 130.0, 120.0, 110.0]
    periods = make.quarters(revenues)

    assert recent_revenue_growth(periods) == pytest.approx((0.10, 0.20, 0.30, 0.50))


@pytest.mark.unit
def test_recent_growth_omits_a_quarter_it_cannot_compare(make) -> None:
    periods = make.quarters([100.0, 110.0, 120.0])

    assert recent_revenue_growth(periods) == ()


@pytest.mark.unit
def test_recent_growth_never_reaches_further_back_than_asked(make) -> None:
    revenues = [100.0] * 4 + [110.0] * 4
    periods = make.quarters(revenues)

    assert len(recent_revenue_growth(periods, count=2)) == 2


@pytest.mark.unit
def test_enterprise_value_is_assembled_into_the_metrics(make) -> None:
    profile = make.profile(market_cap=1_000_000_000.0)
    periods = [
        make.period(index, revenue=100.0, cash=200_000_000.0, total_debt=50_000_000.0)
        for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25))

    assert metrics.enterprise_value == pytest.approx(850_000_000.0)


@pytest.mark.unit
def test_enterprise_value_is_unknown_when_the_latest_quarter_has_no_debt(make) -> None:
    profile = make.profile(market_cap=1_000_000_000.0)
    periods = [make.period(index, revenue=100.0, cash=200_000_000.0) for index in range(4)]

    metrics = build_company_metrics(profile, periods, make.flat_series(25))

    assert metrics.enterprise_value is None
    assert metrics.debt is None


@pytest.mark.unit
def test_enterprise_value_is_unknown_without_a_market_cap() -> None:
    assert enterprise_value(None, 1.0, 1.0) is None
