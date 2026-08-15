"""Market capitalisation, its source, and what happens without a provider.

The rule these protect: a company no vendor covers must still be screenable.
Before this, market cap arrived from one metered provider, and a company it did
not answer for was excluded as missing required data however complete its
filings were.
"""

from __future__ import annotations

import pytest

from domain import (
    EligibilityWarning,
    ExclusionReason,
    MarketCapSource,
    build_company_metrics,
    calculated_market_cap,
    evaluate_eligibility,
    latest_common_shares,
    market_cap_discrepancy,
    resolve_market_cap,
)


@pytest.mark.unit
def test_market_cap_is_price_times_period_end_shares() -> None:
    assert calculated_market_cap(20.0, 50_000_000.0) == 1_000_000_000.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("price", "shares"),
    [(None, 50_000_000.0), (20.0, None), (0.0, 50_000_000.0), (20.0, 0.0), (-5.0, 10.0)],
)
def test_market_cap_cannot_be_calculated_from_a_missing_or_impossible_input(
    price: float | None, shares: float | None
) -> None:
    assert calculated_market_cap(price, shares) is None


@pytest.mark.unit
def test_the_providers_figure_wins_when_there_is_one() -> None:
    value, source = resolve_market_cap(1_100_000_000.0, 1_000_000_000.0)

    assert value == 1_100_000_000.0
    assert source is MarketCapSource.PROVIDER


@pytest.mark.unit
def test_the_calculated_figure_is_used_when_no_provider_answered() -> None:
    value, source = resolve_market_cap(None, 1_000_000_000.0)

    assert value == 1_000_000_000.0
    assert source is MarketCapSource.CALCULATED


@pytest.mark.unit
def test_market_cap_is_unknown_when_neither_is_available() -> None:
    value, source = resolve_market_cap(None, None)

    assert value is None
    assert source is MarketCapSource.UNKNOWN


@pytest.mark.unit
def test_the_two_figures_are_compared_rather_than_averaged() -> None:
    assert market_cap_discrepancy(1_000_000_000.0, 1_200_000_000.0) == pytest.approx(0.20)
    assert market_cap_discrepancy(1_000_000_000.0, 800_000_000.0) == pytest.approx(0.20)


@pytest.mark.unit
def test_a_discrepancy_needs_both_figures() -> None:
    assert market_cap_discrepancy(None, 1_000.0) is None
    assert market_cap_discrepancy(1_000.0, None) is None


@pytest.mark.unit
def test_the_share_count_used_is_the_most_recent_one(make) -> None:
    periods = [
        make.period(2, common_shares_outstanding=40_000_000.0),
        make.period(1, common_shares_outstanding=45_000_000.0),
        make.period(0, common_shares_outstanding=50_000_000.0),
    ]

    assert latest_common_shares(periods) == 50_000_000.0


@pytest.mark.unit
def test_a_share_count_older_than_a_year_is_not_current(make) -> None:
    periods = [make.period(6, common_shares_outstanding=40_000_000.0), make.period(0)]

    assert latest_common_shares(periods) is None


@pytest.mark.unit
def test_a_missing_share_count_leaves_market_cap_unknown(make) -> None:
    profile = make.profile(market_cap=None)
    periods = [make.period(index, revenue=100.0) for index in range(4)]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))

    assert metrics.market_cap is None
    assert metrics.market_cap_source is MarketCapSource.UNKNOWN
    assert metrics.calculated_market_cap is None


@pytest.mark.unit
def test_a_company_no_provider_covers_is_still_screenable(make) -> None:
    # The whole point of the correction: filings and a price are enough.
    profile = make.profile(market_cap=None)
    periods = [
        make.period(index, revenue=100.0, common_shares_outstanding=50_000_000.0)
        for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))
    verdict = evaluate_eligibility(profile, metrics, None)

    assert metrics.market_cap == 1_000_000_000.0
    assert metrics.market_cap_source is MarketCapSource.CALCULATED
    assert ExclusionReason.MISSING_REQUIRED_DATA not in verdict.reasons
    assert EligibilityWarning.MARKET_CAP_CALCULATED in verdict.warnings


@pytest.mark.unit
def test_a_provider_figure_is_kept_and_the_calculation_still_runs(make) -> None:
    profile = make.profile(market_cap=1_050_000_000.0)
    periods = [
        make.period(index, revenue=100.0, common_shares_outstanding=50_000_000.0)
        for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))

    assert metrics.market_cap == 1_050_000_000.0
    assert metrics.market_cap_source is MarketCapSource.PROVIDER
    # Kept so the two can be compared rather than one silently replacing the other.
    assert metrics.calculated_market_cap == 1_000_000_000.0
    assert metrics.market_cap_discrepancy == pytest.approx(0.0476, abs=1e-3)


@pytest.mark.unit
def test_a_material_disagreement_is_flagged_without_excluding_the_company(make) -> None:
    profile = make.profile(market_cap=2_000_000_000.0)
    periods = [
        make.period(index, revenue=100.0, common_shares_outstanding=50_000_000.0)
        for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))
    verdict = evaluate_eligibility(profile, metrics, None)

    assert metrics.market_cap_discrepancy == pytest.approx(0.50)
    assert EligibilityWarning.MARKET_CAP_DISCREPANCY in verdict.warnings
    assert verdict.eligible is True


@pytest.mark.unit
def test_a_close_agreement_raises_no_warning(make) -> None:
    profile = make.profile(market_cap=1_020_000_000.0)
    periods = [
        make.period(index, revenue=100.0, common_shares_outstanding=50_000_000.0)
        for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))
    verdict = evaluate_eligibility(profile, metrics, None)

    assert EligibilityWarning.MARKET_CAP_DISCREPANCY not in verdict.warnings


@pytest.mark.unit
def test_enterprise_value_uses_the_resolved_market_cap(make) -> None:
    profile = make.profile(market_cap=None)
    periods = [
        make.period(
            index,
            revenue=100.0,
            common_shares_outstanding=50_000_000.0,
            cash=200_000_000.0,
            total_debt=50_000_000.0,
        )
        for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))

    assert metrics.enterprise_value == pytest.approx(850_000_000.0)


@pytest.mark.unit
def test_the_weighted_average_share_count_is_not_used_for_market_cap(make) -> None:
    # Dilution needs the weighted average; market cap needs the point-in-time
    # count. Substituting one for the other would value the company on a share
    # base that was never outstanding on any single day.
    profile = make.profile(market_cap=None)
    periods = [
        make.period(index, revenue=100.0, shares_outstanding=90_000_000.0) for index in range(4)
    ]

    metrics = build_company_metrics(profile, periods, make.flat_series(25, close=20.0))

    assert metrics.market_cap is None
