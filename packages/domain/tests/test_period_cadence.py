"""Reading a company's history at the cadence it actually reports on.

A period is what the company published: three months, six months or twelve. The
failure this guards against is arithmetic that turns one into another — a year
divided by four, two halves averaged into quarters, or four annual observations
counted as four quarterly ones. None of those figures was ever reported by
anybody, and each looks entirely plausible in a ranking.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from domain import (
    CompanyMetrics,
    FinancialPeriod,
    PeriodCadence,
    classify_cadence,
    recent_revenue_growth,
    revenue_cagr_3y,
    ttm_revenue,
)
from domain.metrics import (
    TTM_EXPLICIT_ANNUAL,
    TTM_FOUR_QUARTERS,
    TTM_TWO_HALF_YEARS,
    TTM_UNAVAILABLE,
    fundamentals_are_stale,
    history_cadence,
    previous_yoy_revenue_growth,
    ttm_basis,
    yoy_revenue_growth,
)

MILLION = 1_000_000.0


def period(start: date, end: date, revenue: float) -> FinancialPeriod:
    """One reported period, classified from its own dates."""
    return FinancialPeriod(
        period_start=start, period_end=end, revenue=revenue, reported_currency="USD"
    )


def annual(count: int, *, first_year: int = 2020, growth: float = 0.10) -> list[FinancialPeriod]:
    """Consecutive fiscal years, oldest first."""
    return [
        period(
            date(first_year + i, 1, 1),
            date(first_year + i, 12, 31),
            100.0 * MILLION * (1.0 + growth) ** i,
        )
        for i in range(count)
    ]


def halves(count: int, *, start: date = date(2023, 1, 1)) -> list[FinancialPeriod]:
    """Consecutive non-overlapping half-years, oldest first."""
    out = []
    cursor = start
    for i in range(count):
        end = cursor + timedelta(days=181)
        out.append(period(cursor, end, 100.0 * MILLION * (1.0 + 0.05 * i)))
        cursor = end + timedelta(days=1)
    return out


def quarters(count: int, *, start: date = date(2023, 1, 1)) -> list[FinancialPeriod]:
    """Consecutive non-overlapping quarters, oldest first."""
    out = []
    cursor = start
    for i in range(count):
        end = cursor + timedelta(days=90)
        out.append(period(cursor, end, 100.0 * MILLION * (1.0 + 0.02 * i)))
        cursor = end + timedelta(days=1)
    return out


# -- classification ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2025, 1, 1), date(2025, 3, 31), PeriodCadence.QUARTERLY),
        (date(2025, 1, 1), date(2025, 6, 30), PeriodCadence.SEMIANNUAL),
        (date(2025, 1, 1), date(2025, 12, 31), PeriodCadence.ANNUAL),
        # A 52-week and a 53-week fiscal year, which are 364 and 371 days.
        (date(2024, 9, 29), date(2025, 9, 27), PeriodCadence.ANNUAL),
        (date(2024, 9, 29), date(2025, 10, 4), PeriodCadence.ANNUAL),
        # Four months is not a long quarter or a short half-year.
        (date(2025, 1, 1), date(2025, 4, 30), PeriodCadence.UNKNOWN),
        (date(2025, 1, 1), date(2025, 8, 31), PeriodCadence.UNKNOWN),
    ],
)
def test_a_period_is_classified_by_its_own_duration(
    start: date, end: date, expected: PeriodCadence
) -> None:
    assert classify_cadence(start, end) is expected


@pytest.mark.unit
def test_a_period_with_no_start_cannot_be_classified() -> None:
    # A duration cannot be measured from one end, and guessing would put a
    # figure of unknown length into a series of known ones.
    assert classify_cadence(None, date(2025, 12, 31)) is PeriodCadence.UNKNOWN


# -- trailing twelve months -------------------------------------------------


@pytest.mark.unit
def test_one_stated_fiscal_year_is_a_trailing_year() -> None:
    # A filer that published a full year has published the trailing year.
    # Requiring four quarters of a company that files none would withhold a
    # figure that is sitting right there.
    history = annual(3)

    assert ttm_revenue(history) == pytest.approx(history[-1].revenue)
    assert ttm_basis(history) == TTM_EXPLICIT_ANNUAL


@pytest.mark.unit
def test_four_quarters_still_sum_to_a_trailing_year() -> None:
    history = quarters(8)

    expected = sum(p.revenue or 0.0 for p in history[-4:])
    assert ttm_revenue(history) == pytest.approx(expected)
    assert ttm_basis(history) == TTM_FOUR_QUARTERS


@pytest.mark.unit
def test_two_non_overlapping_halves_form_a_trailing_year() -> None:
    history = halves(4)

    expected = sum(p.revenue or 0.0 for p in history[-2:])
    assert ttm_revenue(history) == pytest.approx(expected)
    assert ttm_basis(history) == TTM_TWO_HALF_YEARS


@pytest.mark.unit
def test_overlapping_halves_do_not_form_a_trailing_year() -> None:
    # H1 and a full year both starting in January cover the same six months
    # twice. Added together they would report the business once and a half.
    overlapping = [
        period(date(2024, 1, 1), date(2024, 6, 30), 50.0 * MILLION),
        period(date(2025, 1, 1), date(2025, 6, 30), 60.0 * MILLION),
        period(date(2025, 1, 1), date(2025, 6, 30), 60.0 * MILLION),
    ]

    assert ttm_revenue(overlapping) is None


@pytest.mark.unit
def test_an_isolated_quarterly_run_forms_no_trailing_year() -> None:
    # Brookfield's shape: three-month facts one per year. Four of them span four
    # years and are not a trailing twelve months by any reading.
    q2_only = [
        period(date(2020 + i, 4, 1), date(2020 + i, 6, 30), 100.0 * MILLION) for i in range(5)
    ]

    assert ttm_revenue(q2_only) is None
    assert ttm_basis(q2_only) == TTM_UNAVAILABLE


@pytest.mark.unit
def test_a_single_half_year_is_not_a_trailing_year() -> None:
    assert ttm_revenue(halves(1)) is None


# -- growth at each cadence -------------------------------------------------


@pytest.mark.unit
def test_annual_growth_compares_a_year_with_the_year_before_it() -> None:
    history = annual(3, growth=0.20)

    assert yoy_revenue_growth(history) == pytest.approx(0.20)


@pytest.mark.unit
def test_annual_acceleration_compares_two_annual_growth_rates() -> None:
    # Steady 10% growth accelerates by nothing. Stepping back by a fixed quarter
    # rather than by one reported period is why this used to be unavailable for
    # every annual filer.
    history = annual(4)

    assert previous_yoy_revenue_growth(history) == pytest.approx(0.10)


@pytest.mark.unit
def test_annual_cagr_spans_three_reported_years() -> None:
    history = annual(4, growth=0.20)

    assert revenue_cagr_3y(history) == pytest.approx(0.20, abs=1e-9)


@pytest.mark.unit
def test_annual_persistence_counts_comparable_years() -> None:
    observations = recent_revenue_growth(annual(6))

    assert len(observations) == 4
    assert all(growth > 0 for growth in observations)


@pytest.mark.unit
def test_semiannual_growth_compares_each_half_with_its_own_prior_year() -> None:
    history = halves(6)

    assert yoy_revenue_growth(history) is not None
    assert len(recent_revenue_growth(history)) == 4


@pytest.mark.unit
def test_an_incoherent_history_yields_no_comparable_observations() -> None:
    q2_only = [
        period(date(2020 + i, 4, 1), date(2020 + i, 6, 30), 100.0 * MILLION * (1 + 0.1 * i))
        for i in range(6)
    ]

    assert recent_revenue_growth(q2_only) == ()


@pytest.mark.unit
def test_history_cadence_reads_the_periods_rather_than_guessing() -> None:
    assert history_cadence(annual(3)) is PeriodCadence.ANNUAL
    assert history_cadence(halves(4)) is PeriodCadence.SEMIANNUAL
    assert history_cadence(quarters(6)) is PeriodCadence.QUARTERLY


@pytest.mark.unit
def test_a_history_that_states_no_cadence_is_inferred_from_its_spacing() -> None:
    # Rows stored before cadence was recorded, and providers that supply no
    # start dates. Spacing is the same evidence as duration.
    undated = [
        FinancialPeriod(period_end=date(2025, 3, 31) + timedelta(days=91 * i), revenue=100.0)
        for i in range(6)
    ]

    assert history_cadence(undated) is PeriodCadence.QUARTERLY


@pytest.mark.unit
def test_a_company_with_no_history_has_no_cadence() -> None:
    # Found in the Stage C cohort: 560 companies with no fundamentals at all
    # were each reported as quarterly filers. Inferring from spacing has nothing
    # to infer from, and "quarterly" is a claim about a company nothing is known
    # about — one a reader of the API or the dashboard would see stated plainly.
    assert history_cadence([]) is PeriodCadence.UNKNOWN


@pytest.mark.unit
def test_a_single_undated_period_keeps_the_legacy_quarterly_default() -> None:
    # Distinct from the empty case: a history exists, it just cannot be measured
    # for spacing. Every provider predating the cadence field supplied quarters.
    one = [FinancialPeriod(period_end=date(2025, 3, 31), revenue=100.0)]

    assert history_cadence(one) is PeriodCadence.QUARTERLY


# -- freshness --------------------------------------------------------------


@pytest.mark.unit
def test_an_annual_filer_is_not_stale_eight_months_after_its_year_end() -> None:
    # Reporting annually is not the same as being out of date. A foreign private
    # issuer has six months to file a 20-F, so this is simply what the cadence
    # looks like.
    metrics = CompanyMetrics(
        ticker="TSM",
        fundamental_cadence=PeriodCadence.ANNUAL,
        fundamentals_through=date(2025, 12, 31),
    )

    assert fundamentals_are_stale(metrics, date(2026, 8, 20)) is False


@pytest.mark.unit
def test_an_annual_filer_that_skipped_a_year_is_stale() -> None:
    metrics = CompanyMetrics(
        ticker="TSM",
        fundamental_cadence=PeriodCadence.ANNUAL,
        fundamentals_through=date(2024, 12, 31),
    )

    assert fundamentals_are_stale(metrics, date(2026, 12, 31)) is True


@pytest.mark.unit
def test_a_quarterly_filer_is_stale_far_sooner() -> None:
    # The same eight-month gap that is normal for an annual filer means a
    # quarterly one has missed two quarters.
    metrics = CompanyMetrics(
        ticker="AAA",
        fundamental_cadence=PeriodCadence.QUARTERLY,
        fundamentals_through=date(2025, 12, 31),
    )

    assert fundamentals_are_stale(metrics, date(2026, 8, 20)) is True


@pytest.mark.unit
def test_an_unknown_cadence_is_never_reported_as_stale() -> None:
    metrics = CompanyMetrics(ticker="AAA", fundamentals_through=date(2020, 1, 1))

    assert fundamentals_are_stale(metrics, date(2026, 8, 20)) is False
