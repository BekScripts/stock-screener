"""Guards on the inputs a foreign filer produces, none of which change the score.

CompounderScore V1.1 is unchanged by international support. What changes is what
reaches it: a currency that must not be mixed with a dollar market capitalisation,
and an annual history that must not be counted as quarters. Both are guarded here
rather than in the formula, so no curve, weight or threshold moves.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from domain import (
    CompanyMetrics,
    CompanyProfile,
    EligibilityThresholds,
    EligibilityWarning,
    ExclusionReason,
    FinancialPeriod,
    FxConversion,
    PriceBar,
    VolumeBasis,
    build_company_metrics,
    enterprise_value,
    evaluate_eligibility,
    recent_revenue_growth,
)

MILLION = 1_000_000.0


def quarters(count: int, *, start: date = date(2023, 3, 31)) -> list[FinancialPeriod]:
    """Return `count` quarterly periods growing steadily, oldest first."""
    return [
        FinancialPeriod(
            period_end=start + timedelta(days=91 * index),
            revenue=100.0 * MILLION * (1.0 + 0.05 * index),
            reported_currency="USD",
        )
        for index in range(count)
    ]


def years(count: int, *, currency: str = "TWD") -> list[FinancialPeriod]:
    """Return `count` annual periods growing steadily, oldest first."""
    return [
        FinancialPeriod(
            period_end=date(2016 + index, 12, 31),
            revenue=100.0 * MILLION * (1.0 + 0.2 * index),
            cash=50.0 * MILLION,
            total_debt=10.0 * MILLION,
            reported_currency=currency,
        )
        for index in range(count)
    ]


def bars(count: int = 30) -> list[PriceBar]:
    """Return a short run of price history."""
    return [
        PriceBar(
            date=date(2026, 1, 1) + timedelta(days=index),
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=1_000_000.0,
        )
        for index in range(count)
    ]


# -- growth persistence -----------------------------------------------------


@pytest.mark.unit
def test_annual_history_yields_comparable_annual_observations() -> None:
    # Phase 7B returned nothing here, because an annual run was being reported
    # as "4 of 4 comparable quarters" and that was a lie about the evidence.
    # Phase 7D compares each fiscal year with the one before it and calls them
    # what they are: four comparable *periods*. The measurement is honest and
    # the wording matches it.
    observations = recent_revenue_growth(years(6))

    assert len(observations) == 4
    assert all(growth > 0 for growth in observations)


@pytest.mark.unit
def test_quarterly_history_still_yields_persistence_observations() -> None:
    # The domestic path is untouched.
    observations = recent_revenue_growth(quarters(8))

    assert len(observations) == 4
    assert all(growth > 0 for growth in observations)


@pytest.mark.unit
def test_one_missing_quarter_does_not_withdraw_persistence() -> None:
    # A filer that skipped a quarter still reports quarterly. Demanding an
    # unbroken run would take a sub-score away from companies this has always
    # scored, so the guard only rejects a history with no quarterly spacing at
    # all.
    history = quarters(8)
    del history[5]

    assert recent_revenue_growth(history) != ()


@pytest.mark.unit
def test_half_yearly_history_compares_each_half_with_its_own_prior_year() -> None:
    # Most foreign private issuers report twice a year. Six-month periods are
    # still never halved into quarters — they are compared with the
    # corresponding half a year earlier, which is the same year-over-year
    # measurement every other cadence makes.
    half_years = [
        FinancialPeriod(
            period_end=date(2023, 6, 30) + timedelta(days=182 * index),
            period_start=date(2023, 1, 1) + timedelta(days=182 * index),
            revenue=100.0 * MILLION * (1.0 + 0.1 * index),
            reported_currency="EUR",
        )
        for index in range(6)
    ]

    observations = recent_revenue_growth(half_years)

    assert len(observations) == 4
    assert all(growth > 0 for growth in observations)


@pytest.mark.unit
def test_an_incoherent_history_yields_no_persistence_observations() -> None:
    # Brookfield's shape: three-month facts that are all second quarters, one
    # per year. They claim a quarterly cadence their own dates do not support,
    # so they are not a run of comparable observations and nothing is counted.
    q2_only = [
        FinancialPeriod(
            period_end=date(2020 + index, 6, 30),
            period_start=date(2020 + index, 4, 1),
            revenue=100.0 * MILLION * (1.0 + 0.1 * index),
            reported_currency="USD",
        )
        for index in range(6)
    ]

    assert recent_revenue_growth(q2_only) == ()


# -- currency ---------------------------------------------------------------


@pytest.mark.unit
def test_enterprise_value_is_withheld_when_the_currency_does_not_match() -> None:
    # A USD market capitalisation less a TWD balance sheet is not a small error.
    # For TSM it yields an EV/Revenue of 0.40x against a true 24.68x, which would
    # rank the most expensive large cap on the board as the cheapest.
    profile = CompanyProfile(ticker="TSM", name="TSM", market_cap=2_212_000.0 * MILLION)

    metrics = build_company_metrics(profile, years(6), bars())

    assert metrics.reported_currency == "TWD"
    assert metrics.enterprise_value is None


@pytest.mark.unit
def test_enterprise_value_is_computed_when_the_currency_matches() -> None:
    profile = CompanyProfile(ticker="AAA", name="AAA", market_cap=1_000.0 * MILLION)

    metrics = build_company_metrics(profile, years(6, currency="USD"), bars())

    assert metrics.enterprise_value == pytest.approx(960.0 * MILLION)


@pytest.mark.unit
def test_the_filings_currency_overrules_a_providers_profile() -> None:
    # A market-data vendor reports an ADR's *trading* currency, which is USD for
    # every foreign issuer on a U.S. exchange. Believing it would hide the fact
    # that a conversion is needed at all.
    profile = CompanyProfile(
        ticker="TSM", name="TSM", quote_currency="USD", market_cap=500.0 * MILLION
    )
    metrics = CompanyMetrics(
        ticker="TSM",
        price=100.0,
        market_cap=500.0 * MILLION,
        average_dollar_volume_20d=50.0 * MILLION,
        liquidity_basis=VolumeBasis.CONSOLIDATED,
        trading_days_used=30,
        reported_currency="TWD",
        quote_currency="USD",
    )

    result = evaluate_eligibility(profile, metrics, EligibilityThresholds())

    assert metrics.needs_conversion is True
    assert metrics.market_cap_for_ratios is None
    assert EligibilityWarning.FX_UNAVAILABLE in result.warnings


@pytest.mark.unit
def test_an_unstated_currency_is_still_treated_as_usd() -> None:
    # Most filings do not restate the obvious, and excluding every company whose
    # statements did not say would empty the universe.
    profile = CompanyProfile(ticker="AAA", name="AAA", market_cap=500.0 * MILLION)
    metrics = CompanyMetrics(
        ticker="AAA",
        price=100.0,
        market_cap=500.0 * MILLION,
        average_dollar_volume_20d=50.0 * MILLION,
        liquidity_basis=VolumeBasis.CONSOLIDATED,
        trading_days_used=30,
    )

    result = evaluate_eligibility(profile, metrics, EligibilityThresholds())

    assert ExclusionReason.UNSUPPORTED_CURRENCY not in result.reasons


# -- currency-safe valuation ------------------------------------------------


def fx(quote: str, rate: float) -> FxConversion:
    """A conversion from the listing's dollars into a reporting currency."""
    return FxConversion(
        base="USD", quote=quote, rate=rate, rate_date=date(2026, 8, 14), provider="test"
    )


@pytest.mark.unit
def test_a_supplied_rate_converts_the_market_cap_not_the_statements() -> None:
    # Only one number crosses. Restating the reported history would put exchange
    # rate movement into revenue growth, which is a property of the business.
    profile = CompanyProfile(
        ticker="TSM", name="TSM", market_cap=2_212_000.0 * MILLION, quote_currency="USD"
    )
    periods = years(6)

    metrics = build_company_metrics(profile, periods, bars(), fx=fx("TWD", 31.99226))

    assert metrics.market_cap == 2_212_000.0 * MILLION
    assert metrics.market_cap_reporting_currency == pytest.approx(2_212_000.0 * MILLION * 31.99226)
    assert metrics.market_cap_for_ratios == metrics.market_cap_reporting_currency
    # The statements are untouched, in the currency they were filed in.
    assert metrics.cash == 50.0 * MILLION
    assert metrics.reported_currency == "TWD"


@pytest.mark.unit
def test_enterprise_value_is_built_from_the_converted_market_cap() -> None:
    profile = CompanyProfile(
        ticker="TSM", name="TSM", market_cap=1_000.0 * MILLION, quote_currency="USD"
    )

    metrics = build_company_metrics(profile, years(6), bars(), fx=fx("TWD", 30.0))

    # 1,000M USD -> 30,000M TWD, plus 10M debt, less 50M cash.
    assert metrics.enterprise_value == pytest.approx(29_960.0 * MILLION)


@pytest.mark.unit
def test_a_rate_for_the_wrong_pair_is_not_applied() -> None:
    # A DKK rate cannot value a TWD balance sheet, and silently using it would
    # be the same class of error as using none.
    profile = CompanyProfile(
        ticker="TSM", name="TSM", market_cap=1_000.0 * MILLION, quote_currency="USD"
    )

    metrics = build_company_metrics(profile, years(6), bars(), fx=fx("DKK", 6.463))

    assert metrics.market_cap_for_ratios is None
    assert metrics.enterprise_value is None


@pytest.mark.unit
def test_a_domestic_company_needs_no_rate_and_is_unchanged() -> None:
    profile = CompanyProfile(
        ticker="AAA", name="AAA", market_cap=1_000.0 * MILLION, quote_currency="USD"
    )

    metrics = build_company_metrics(profile, years(6, currency="USD"), bars())

    assert metrics.needs_conversion is False
    assert metrics.market_cap_for_ratios == 1_000.0 * MILLION
    assert metrics.fx is None
    assert metrics.enterprise_value == pytest.approx(960.0 * MILLION)


@pytest.mark.unit
def test_the_conversion_used_is_recorded_for_provenance() -> None:
    profile = CompanyProfile(
        ticker="TSM", name="TSM", market_cap=1_000.0 * MILLION, quote_currency="USD"
    )

    metrics = build_company_metrics(profile, years(6), bars(), fx=fx("TWD", 31.99226))

    assert metrics.fx is not None
    assert metrics.fx.rate_date == date(2026, 8, 14)
    assert metrics.fx.provider == "test"


@pytest.mark.unit
def test_growth_and_margins_do_not_move_with_the_exchange_rate() -> None:
    # The point of converting only the market side. Every metric computed from
    # the statements alone is a property of the business, so the same company
    # scored at two very different rates must produce identical figures — the
    # exchange rate touches the valuation and nothing else.
    profile = CompanyProfile(
        ticker="TSM", name="TSM", market_cap=1_000.0 * MILLION, quote_currency="USD"
    )
    periods = [
        period.model_copy(
            update={
                "gross_profit": (period.revenue or 0.0) * 0.5,
                "operating_income": (period.revenue or 0.0) * 0.3,
            }
        )
        for period in years(6)
    ]

    weak = build_company_metrics(profile, periods, bars(), fx=fx("TWD", 31.99226))
    strong = build_company_metrics(profile, periods, bars(), fx=fx("TWD", 1.0))

    assert weak.revenue_growth_yoy == strong.revenue_growth_yoy
    assert weak.gross_margin == strong.gross_margin == pytest.approx(0.5)
    assert weak.operating_margin == strong.operating_margin == pytest.approx(0.3)
    assert weak.revenue_cagr_3y == strong.revenue_cagr_3y
    # And the valuation, which is the one thing that should move, does.
    assert weak.enterprise_value != strong.enterprise_value


@pytest.mark.unit
def test_tsms_mixed_currency_multiple_cannot_be_produced() -> None:
    # The whole reason this layer exists, pinned to TSM's FY2024 figures as
    # filed. A USD market capitalisation dropped into a TWD balance sheet gives
    # an EV/Revenue of 0.38x; converting the market side first gives 24.07x. The
    # gap is a factor of sixty-three, and it is the difference between the most
    # expensive large cap on the board and the cheapest.
    revenue = 2_894_307.7 * MILLION
    cash = 2_127_684.5 * MILLION
    debt = 1_018_286.8 * MILLION
    market_cap = 2_212_000.0 * MILLION

    periods = [
        FinancialPeriod(
            period_end=date(2023, 12, 31),
            revenue=2_161_735.8 * MILLION,
            cash=cash,
            total_debt=debt,
            reported_currency="TWD",
        ),
        FinancialPeriod(
            period_end=date(2024, 12, 31),
            revenue=revenue,
            cash=cash,
            total_debt=debt,
            reported_currency="TWD",
        ),
    ]
    profile = CompanyProfile(ticker="TSM", name="TSM", market_cap=market_cap, quote_currency="USD")

    unsafe = enterprise_value(market_cap, cash, debt) / revenue
    converted = build_company_metrics(profile, periods, bars(), fx=fx("TWD", 31.99226))
    safe = converted.enterprise_value / revenue

    assert unsafe == pytest.approx(0.38, abs=0.01)
    assert safe == pytest.approx(24.07, abs=0.01)
    assert safe / unsafe == pytest.approx(63.2, abs=0.5)

    # And with no rate, nothing at all rather than the 0.38x.
    withheld = build_company_metrics(profile, periods, bars(), fx=None)
    assert withheld.enterprise_value is None
    assert withheld.market_cap_for_ratios is None
