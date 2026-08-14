"""Happy-path tests for every calculation the Phase 1 specification requires."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from domain import (
    CompanyProfile,
    average_dollar_volume,
    build_company_metrics,
    distance_from_52w_high,
    fcf_margin,
    free_cash_flow,
    gross_margin,
    gross_profit_growth_yoy,
    growth_acceleration,
    high_52w,
    low_52w,
    net_cash,
    operating_margin,
    previous_yoy_revenue_growth,
    return_6m,
    return_12m,
    revenue_cagr_3y,
    share_count_growth_yoy,
    total_return,
    trading_days_used,
    ttm_revenue,
    ttm_revenue_growth,
    yoy_revenue_growth,
)

if TYPE_CHECKING:
    from conftest import Make


@pytest.mark.unit
def test_yoy_revenue_growth_compares_against_the_year_ago_quarter(make: type[Make]) -> None:
    # Latest 135 against 100 four quarters back; the quarters between are noise.
    periods = make.quarters([100.0, 110.0, 120.0, 125.0, 135.0])

    assert yoy_revenue_growth(periods) == pytest.approx(0.35)


@pytest.mark.unit
def test_previous_yoy_growth_compares_the_quarter_before_the_latest(make: type[Make]) -> None:
    # The quarter before the latest is 118, against 100 a year before it.
    periods = make.quarters([100.0, 105.0, 110.0, 115.0, 118.0, 135.0])

    assert previous_yoy_revenue_growth(periods) == pytest.approx(0.18)


@pytest.mark.unit
def test_growth_acceleration_subtracts_rates_rather_than_dividing_them() -> None:
    # 35% following 18% is +17 percentage points, not 1.94x.
    assert growth_acceleration(0.35, 0.18) == pytest.approx(0.17)


@pytest.mark.unit
def test_growth_acceleration_is_negative_when_growth_slows() -> None:
    assert growth_acceleration(0.05, 0.25) == pytest.approx(-0.20)


@pytest.mark.unit
def test_ttm_revenue_sums_the_latest_four_quarters(make: type[Make]) -> None:
    periods = make.quarters([50.0, 100.0, 110.0, 120.0, 130.0])

    assert ttm_revenue(periods) == pytest.approx(460.0)


@pytest.mark.unit
def test_ttm_revenue_growth_compares_consecutive_trailing_years(make: type[Make]) -> None:
    periods = make.quarters([100.0] * 4 + [150.0] * 4)

    assert ttm_revenue_growth(periods) == pytest.approx(0.5)


@pytest.mark.unit
def test_revenue_cagr_3y_annualises_growth_over_three_years(make: type[Make]) -> None:
    # Oldest trailing year sums to 400, the latest to 1350. 3.375^(1/3) = 1.5,
    # so 50% a year. The eight quarters in between do not enter the calculation.
    periods = make.quarters([100.0] * 4 + [200.0] * 8 + [337.5] * 4)

    assert revenue_cagr_3y(periods) == pytest.approx(0.5)


@pytest.mark.unit
def test_gross_margin_divides_gross_profit_by_revenue(make: type[Make]) -> None:
    quarter = make.period(0, revenue=200.0, gross_profit=76.4)

    assert gross_margin(quarter) == pytest.approx(0.382)


@pytest.mark.unit
def test_gross_profit_growth_yoy_compares_against_the_year_ago_quarter(make: type[Make]) -> None:
    periods = make.series("gross_profit", [40.0, 45.0, 50.0, 55.0, 60.0])

    assert gross_profit_growth_yoy(periods) == pytest.approx(0.5)


@pytest.mark.unit
def test_operating_margin_divides_operating_income_by_revenue(make: type[Make]) -> None:
    quarter = make.period(0, revenue=500.0, operating_income=45.0)

    assert operating_margin(quarter) == pytest.approx(0.09)


@pytest.mark.unit
def test_free_cash_flow_prefers_the_reported_figure(make: type[Make]) -> None:
    quarter = make.period(0, free_cash_flow=88.0, operating_cash_flow=10.0, capital_expenditure=2.0)

    assert free_cash_flow(quarter) == pytest.approx(88.0)


@pytest.mark.unit
@pytest.mark.parametrize("capex", [25.0, -25.0])
def test_free_cash_flow_treats_capex_as_an_outflow_whatever_its_sign(
    make: type[Make], capex: float
) -> None:
    # Vendors disagree on the sign of capital expenditure. Both must give 75,
    # never 125 — the sign error that turns cash burn into cash generation.
    quarter = make.period(0, operating_cash_flow=100.0, capital_expenditure=capex)

    assert free_cash_flow(quarter) == pytest.approx(75.0)


@pytest.mark.unit
def test_fcf_margin_divides_free_cash_flow_by_revenue(make: type[Make]) -> None:
    quarter = make.period(0, revenue=1_000.0, operating_cash_flow=100.0, capital_expenditure=35.0)

    assert fcf_margin(quarter) == pytest.approx(0.065)


@pytest.mark.unit
def test_net_cash_subtracts_debt_from_cash(make: type[Make]) -> None:
    assert net_cash(make.period(0, cash=410.0, total_debt=95.0)) == pytest.approx(315.0)


@pytest.mark.unit
def test_net_cash_is_negative_for_a_net_debtor(make: type[Make]) -> None:
    assert net_cash(make.period(0, cash=42.0, total_debt=128.0)) == pytest.approx(-86.0)


@pytest.mark.unit
def test_share_count_growth_measures_dilution_over_a_year(make: type[Make]) -> None:
    periods = make.series("shares_outstanding", [100.0, 102.0, 104.0, 106.0, 120.0])

    assert share_count_growth_yoy(periods) == pytest.approx(0.20)


@pytest.mark.unit
def test_share_count_growth_is_negative_after_a_buyback(make: type[Make]) -> None:
    periods = make.series("shares_outstanding", [100.0, 99.0, 98.0, 97.0, 90.0])

    assert share_count_growth_yoy(periods) == pytest.approx(-0.10)


@pytest.mark.unit
def test_average_dollar_volume_averages_close_times_volume(make: type[Make]) -> None:
    bars = make.flat_series(sessions=25, close=10.0, volume=1_000.0)

    assert average_dollar_volume(bars) == pytest.approx(10_000.0)
    assert trading_days_used(bars) == 20


@pytest.mark.unit
def test_average_dollar_volume_ignores_sessions_outside_the_window(make: type[Make]) -> None:
    # One ancient session at $1m of turnover must not lift a 20-day average of
    # $10k, or an illiquid stock passes the liquidity screen on old news.
    bars = [make.bar(400, close=1_000.0, volume=1_000.0), *make.flat_series(20, 10.0, 1_000.0)]

    assert average_dollar_volume(bars) == pytest.approx(10_000.0)


@pytest.mark.unit
def test_six_month_return_uses_the_last_session_on_or_before_the_target(
    make: type[Make],
) -> None:
    bars = [make.bar(200, close=80.0), make.bar(182, close=100.0), make.bar(0, close=125.0)]

    assert return_6m(bars) == pytest.approx(0.25)


@pytest.mark.unit
def test_twelve_month_return_uses_the_last_session_on_or_before_the_target(
    make: type[Make],
) -> None:
    bars = [make.bar(365, close=50.0), make.bar(180, close=90.0), make.bar(0, close=75.0)]

    assert return_12m(bars) == pytest.approx(0.5)


@pytest.mark.unit
def test_total_return_falls_back_to_an_earlier_session_over_a_market_holiday(
    make: type[Make],
) -> None:
    # Nothing traded on day 182, so the session four days earlier is the base.
    bars = [make.bar(186, close=100.0), make.bar(120, close=110.0), make.bar(0, close=130.0)]

    assert total_return(bars, 182) == pytest.approx(0.30)


@pytest.mark.unit
def test_52_week_high_and_low_use_intraday_extremes_within_the_year(make: type[Make]) -> None:
    bars = [
        make.bar(400, close=500.0, high=999.0, low=1.0),  # outside the window
        make.bar(300, close=40.0, high=44.0, low=38.0),
        make.bar(100, close=20.0, high=21.0, low=18.0),
        make.bar(0, close=33.0, high=34.0, low=32.0),
    ]

    assert high_52w(bars) == pytest.approx(44.0)
    assert low_52w(bars) == pytest.approx(18.0)


@pytest.mark.unit
def test_distance_from_52_week_high_is_negative_below_the_high(make: type[Make]) -> None:
    bars = [
        make.bar(100, close=100.0, high=100.0, low=90.0),
        make.bar(0, close=75.0, high=76.0, low=74.0),
    ]

    assert distance_from_52w_high(bars) == pytest.approx(-0.25)


@pytest.mark.unit
def test_distance_from_52_week_high_is_zero_at_the_high(make: type[Make]) -> None:
    bars = [
        make.bar(10, close=50.0, high=50.0, low=49.0),
        make.bar(0, close=60.0, high=60.0, low=59.0),
    ]

    assert distance_from_52w_high(bars) == pytest.approx(0.0)


@pytest.mark.unit
def test_build_company_metrics_assembles_every_field(make: type[Make]) -> None:
    profile = CompanyProfile(
        ticker="XYZ", name="Example Corp", exchange="NASDAQ", market_cap=1_200_000_000.0
    )
    periods = make.quarters(
        [100.0, 110.0, 120.0, 125.0, 135.0],
        gross_profit=40.0,
        operating_income=10.0,
        operating_cash_flow=20.0,
        capital_expenditure=5.0,
        cash=300.0,
        total_debt=100.0,
        shares_outstanding=50.0,
    )
    bars = make.flat_series(sessions=30, close=12.0, volume=100_000.0)

    metrics = build_company_metrics(profile, periods, bars)

    assert metrics.ticker == "XYZ"
    assert metrics.price == pytest.approx(12.0)
    assert metrics.market_cap == pytest.approx(1_200_000_000.0)
    assert metrics.revenue_growth_yoy == pytest.approx(0.35)
    assert metrics.net_cash == pytest.approx(200.0)
    assert metrics.average_dollar_volume_20d == pytest.approx(1_200_000.0)
    assert metrics.trading_days_used == 20


@pytest.mark.unit
def test_build_company_metrics_leaves_unavailable_figures_as_none() -> None:
    profile = CompanyProfile(ticker="NEW", name="Newly Listed Inc")

    metrics = build_company_metrics(profile, [], [])

    assert metrics.price is None
    assert metrics.revenue_growth_yoy is None
    assert metrics.ttm_revenue is None
    assert metrics.net_cash is None
    assert metrics.trading_days_used == 0
