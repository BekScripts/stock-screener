"""Edge cases: the metric engine must fail predictably, never plausibly.

Each test here corresponds to a way real provider data is broken. The assertion
is almost always `is None`, and the point is always the same: a metric the data
cannot support must be absent, because a fabricated `0.0` will be ranked,
averaged and acted on as if it were an observation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from domain import (
    CompanyProfile,
    FinancialPeriod,
    average_dollar_volume,
    build_company_metrics,
    distance_from_52w_high,
    fcf_margin,
    free_cash_flow,
    gross_margin,
    high_52w,
    net_cash,
    operating_margin,
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


# -- missing and invalid denominators ---------------------------------------


@pytest.mark.unit
def test_yoy_growth_is_none_when_the_prior_year_quarter_is_missing(make: type[Make]) -> None:
    periods = make.quarters([120.0, 125.0, 135.0])  # only three quarters of history

    assert yoy_revenue_growth(periods) is None


@pytest.mark.unit
def test_yoy_growth_is_none_when_the_prior_year_revenue_is_zero(make: type[Make]) -> None:
    # Dividing by zero would be infinite growth, which is not a fact.
    periods = make.quarters([0.0, 110.0, 120.0, 125.0, 135.0])

    assert yoy_revenue_growth(periods) is None


@pytest.mark.unit
def test_yoy_growth_is_none_when_the_prior_year_revenue_is_negative(make: type[Make]) -> None:
    # "Up 300% from minus a million" is arithmetic, not information.
    periods = make.quarters([-50.0, 110.0, 120.0, 125.0, 135.0])

    assert yoy_revenue_growth(periods) is None


@pytest.mark.unit
def test_yoy_growth_is_none_when_the_prior_year_revenue_was_not_reported(
    make: type[Make],
) -> None:
    periods = make.quarters([None, 110.0, 120.0, 125.0, 135.0])

    assert yoy_revenue_growth(periods) is None


@pytest.mark.unit
def test_yoy_growth_is_none_when_the_year_ago_quarter_is_absent_from_the_history(
    make: type[Make],
) -> None:
    # Q-4 never filed. Comparing against Q-5 instead would silently report a
    # fifteen-month growth rate as a yearly one.
    periods = [p for p in make.quarters([100.0, 105.0, 110.0, 120.0, 135.0]) if p.revenue != 100.0]
    periods.append(FinancialPeriod(period_end=make.quarter_end(5), revenue=90.0))

    assert yoy_revenue_growth(periods) is None


@pytest.mark.unit
def test_gross_margin_is_none_when_revenue_is_zero(make: type[Make]) -> None:
    assert gross_margin(make.period(0, revenue=0.0, gross_profit=10.0)) is None


@pytest.mark.unit
def test_gross_margin_is_none_when_revenue_was_not_reported(make: type[Make]) -> None:
    assert gross_margin(make.period(0, gross_profit=10.0)) is None


@pytest.mark.unit
def test_operating_margin_is_none_when_operating_income_is_missing(make: type[Make]) -> None:
    assert operating_margin(make.period(0, revenue=100.0)) is None


@pytest.mark.unit
def test_margin_of_zero_is_preserved_as_a_real_observation(make: type[Make]) -> None:
    # A company that reported exactly zero gross profit did report something.
    # This must be 0.0, not None — the mirror image of every test above.
    assert gross_margin(make.period(0, revenue=100.0, gross_profit=0.0)) == 0.0


# -- free cash flow ---------------------------------------------------------


@pytest.mark.unit
def test_negative_free_cash_flow_is_preserved(make: type[Make]) -> None:
    quarter = make.period(0, revenue=1_000.0, operating_cash_flow=-40.0, capital_expenditure=42.0)

    assert free_cash_flow(quarter) == pytest.approx(-82.0)
    assert fcf_margin(quarter) == pytest.approx(-0.082)


@pytest.mark.unit
def test_free_cash_flow_is_none_when_capex_is_missing(make: type[Make]) -> None:
    # Operating cash flow alone is not free cash flow; returning it would
    # overstate every capital-intensive business.
    assert free_cash_flow(make.period(0, operating_cash_flow=100.0)) is None


@pytest.mark.unit
def test_free_cash_flow_is_none_when_nothing_was_reported(make: type[Make]) -> None:
    assert free_cash_flow(make.period(0)) is None


@pytest.mark.unit
def test_reported_free_cash_flow_of_zero_is_not_treated_as_missing(make: type[Make]) -> None:
    quarter = make.period(0, free_cash_flow=0.0, operating_cash_flow=99.0, capital_expenditure=1.0)

    assert free_cash_flow(quarter) == 0.0


# -- balance sheet ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields",
    [{"cash": 100.0}, {"total_debt": 100.0}, {}],
    ids=["debt-missing", "cash-missing", "both-missing"],
)
def test_net_cash_is_none_when_either_side_is_missing(
    make: type[Make], fields: dict[str, float]
) -> None:
    assert net_cash(make.period(0, **fields)) is None


@pytest.mark.unit
def test_net_cash_of_zero_is_a_real_observation(make: type[Make]) -> None:
    assert net_cash(make.period(0, cash=50.0, total_debt=50.0)) == 0.0


# -- trailing twelve months -------------------------------------------------


@pytest.mark.unit
def test_ttm_revenue_is_none_with_only_three_quarters(make: type[Make]) -> None:
    # Explicitly not 330. Summing three quarters and calling it a year would
    # understate revenue by a quarter without saying so.
    assert ttm_revenue(make.quarters([100.0, 110.0, 120.0])) is None


@pytest.mark.unit
def test_ttm_revenue_is_none_when_one_quarter_has_no_revenue(make: type[Make]) -> None:
    assert ttm_revenue(make.quarters([100.0, None, 120.0, 130.0])) is None


@pytest.mark.unit
def test_ttm_revenue_is_none_when_a_quarter_is_missing_from_the_middle(
    make: type[Make],
) -> None:
    # Four periods, but they span five quarters, so they are not a year.
    periods = make.quarters([100.0, 110.0, 120.0, 125.0, 135.0])
    del periods[2]

    assert ttm_revenue(periods) is None


@pytest.mark.unit
def test_ttm_revenue_growth_is_none_with_fewer_than_eight_quarters(make: type[Make]) -> None:
    assert ttm_revenue_growth(make.quarters([100.0] * 7)) is None


@pytest.mark.unit
def test_revenue_cagr_is_none_with_fewer_than_sixteen_quarters(make: type[Make]) -> None:
    assert revenue_cagr_3y(make.quarters([100.0] * 15)) is None


@pytest.mark.unit
def test_revenue_cagr_is_none_when_the_starting_year_had_no_revenue(make: type[Make]) -> None:
    periods = make.quarters([0.0] * 4 + [50.0] * 8 + [200.0] * 4)

    assert revenue_cagr_3y(periods) is None


# -- share counts -----------------------------------------------------------


@pytest.mark.unit
def test_share_count_growth_is_none_without_a_year_ago_observation(make: type[Make]) -> None:
    assert share_count_growth_yoy(make.series("shares_outstanding", [100.0, 105.0])) is None


@pytest.mark.unit
def test_share_count_growth_is_none_when_the_prior_count_is_missing(make: type[Make]) -> None:
    periods = make.series("shares_outstanding", [None, 102.0, 104.0, 106.0, 120.0])

    assert share_count_growth_yoy(periods) is None


# -- price history ----------------------------------------------------------


@pytest.mark.unit
def test_average_dollar_volume_is_none_without_any_bars() -> None:
    assert average_dollar_volume([]) is None
    assert trading_days_used([]) == 0


@pytest.mark.unit
def test_average_dollar_volume_uses_what_exists_and_reports_the_shortfall(
    make: type[Make],
) -> None:
    # Fewer than twenty sessions still produces a figure, but `trading_days_used`
    # says how thin it is. Eligibility is what refuses to trust it.
    bars = make.flat_series(sessions=4, close=10.0, volume=1_000.0)

    assert average_dollar_volume(bars) == pytest.approx(10_000.0)
    assert trading_days_used(bars) == 4


@pytest.mark.unit
def test_returns_are_none_when_history_does_not_reach_back_far_enough(
    make: type[Make],
) -> None:
    bars = make.flat_series(sessions=30, close=10.0)

    assert return_6m(bars) is None
    assert return_12m(bars) is None


@pytest.mark.unit
def test_total_return_is_none_when_the_baseline_close_is_zero(make: type[Make]) -> None:
    bars = [make.bar(200, close=0.0), make.bar(0, close=10.0)]

    assert total_return(bars, 182) is None


@pytest.mark.unit
def test_52_week_high_is_none_without_any_bars() -> None:
    assert high_52w([]) is None
    assert distance_from_52w_high([]) is None


# -- malformed input --------------------------------------------------------


@pytest.mark.unit
def test_duplicate_periods_are_collapsed_with_the_later_entry_winning(
    make: type[Make],
) -> None:
    # A restatement arrives as a second record for the same period end. Summing
    # both would double that quarter inside the trailing-twelve-month total.
    periods = make.quarters([100.0, 100.0, 100.0, 100.0])
    restated = FinancialPeriod(period_end=periods[-1].period_end, revenue=250.0)

    assert ttm_revenue([*periods, restated]) == pytest.approx(550.0)


@pytest.mark.unit
def test_periods_supplied_newest_first_give_the_same_answer(make: type[Make]) -> None:
    periods = make.quarters([100.0, 110.0, 120.0, 125.0, 135.0])

    assert yoy_revenue_growth(list(reversed(periods))) == pytest.approx(0.35)


@pytest.mark.unit
def test_duplicate_bars_are_collapsed_before_averaging(make: type[Make]) -> None:
    # Two pages of a paginated response overlapping by one session must not make
    # that session count twice in a twenty-day average.
    bars = make.flat_series(sessions=20, close=10.0, volume=1_000.0)

    assert average_dollar_volume([*bars, *bars]) == pytest.approx(10_000.0)
    assert trading_days_used([*bars, *bars]) == 20


@pytest.mark.unit
def test_bars_supplied_out_of_order_give_the_same_latest_price(make: type[Make]) -> None:
    bars = [make.bar(0, close=42.0), make.bar(10, close=10.0), make.bar(5, close=20.0)]
    profile = CompanyProfile(ticker="ANY", name="Any Corp")

    assert build_company_metrics(profile, [], bars).price == pytest.approx(42.0)


@pytest.mark.unit
def test_a_fiscal_quarter_a_few_days_off_the_grid_still_matches(make: type[Make]) -> None:
    # A 13-week fiscal calendar drifts against a 91-day grid. Tolerating that
    # drift is why period matching uses a window rather than an exact date.
    periods = make.quarters([100.0, 110.0, 120.0, 125.0, 135.0])
    drifted = [
        p.model_copy(update={"period_end": p.period_end + timedelta(days=6)}) for p in periods[:-1]
    ]

    assert yoy_revenue_growth([*drifted, periods[-1]]) == pytest.approx(0.35)
