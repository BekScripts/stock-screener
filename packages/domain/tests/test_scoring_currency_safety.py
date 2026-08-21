"""Every scoring ratio that divides a financial figure by a market capitalisation.

CompounderScore V1.1 is unchanged. What changed is which market capitalisation
these four sub-scores read: `market_cap_for_ratios`, which is in the same money
as the statements, rather than `market_cap`, which is in dollars whatever the
company files in.

Four places divide by it — the quality component's cash-vs-debt, the valuation
component's multiple and FCF yield, and the risk assessment's leverage. Each is
pinned here twice: once proving a domestic company is untouched, and once
proving a foreign company without a rate gets nothing rather than something
wrong.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from domain import (
    ComponentStatus,
    FxConversion,
    ScoreWarning,
    ValuationBasis,
    assess_risk,
    score_quality,
    score_valuation,
)

MILLION = 1_000_000.0
RATE = 31.99226


def _sub(component: Any, name: str) -> Any:
    return next(sub for sub in component.subscores if sub.name == name)


def _foreign(make: Any, *, converted: bool, **overrides: Any) -> Any:
    """A TWD-reporting company whose market cap is quoted in dollars."""
    market_cap = 1_000.0 * MILLION
    return make.metrics(
        reported_currency="TWD",
        quote_currency="USD",
        market_cap=market_cap,
        market_cap_reporting_currency=market_cap * RATE if converted else None,
        fx=(
            FxConversion(
                base="USD", quote="TWD", rate=RATE, rate_date=date(2026, 8, 14), provider="test"
            )
            if converted
            else None
        ),
        **overrides,
    )


# -- the accessor itself ----------------------------------------------------


@pytest.mark.unit
def test_a_domestic_company_reads_its_market_cap_unchanged(make: Any) -> None:
    metrics = make.metrics(market_cap=500.0 * MILLION)

    assert metrics.needs_conversion is False
    assert metrics.market_cap_for_ratios == 500.0 * MILLION


@pytest.mark.unit
def test_a_foreign_company_without_a_rate_has_no_usable_market_cap(make: Any) -> None:
    metrics = _foreign(make, converted=False, enterprise_value=None)

    assert metrics.market_cap_for_ratios is None
    # The dollar figure is still there. It is a true fact about a real listing;
    # only its use against a TWD balance sheet is not.
    assert metrics.market_cap == 1_000.0 * MILLION


# -- quality: cash versus debt ----------------------------------------------


@pytest.mark.unit
def test_cash_versus_debt_scores_on_the_converted_market_cap(make: Any) -> None:
    metrics = _foreign(make, converted=True, net_cash=500.0 * MILLION * RATE)

    assert _sub(score_quality(metrics), "cash_vs_debt").points is not None


@pytest.mark.unit
def test_cash_versus_debt_is_unavailable_without_a_rate(make: Any) -> None:
    # Dividing TWD net cash by a USD market cap would report a company holding
    # thirty-two times more net cash than it does.
    metrics = _foreign(
        make, converted=False, enterprise_value=None, net_cash=500.0 * MILLION * RATE
    )

    assert _sub(score_quality(metrics), "cash_vs_debt").points is None


# -- valuation: the multiple and the yield ----------------------------------


@pytest.mark.unit
def test_price_to_sales_falls_back_to_the_converted_market_cap(make: Any) -> None:
    metrics = _foreign(
        make, converted=True, ttm_revenue=100.0 * MILLION * RATE, enterprise_value=None
    )

    component, basis = score_valuation(metrics)

    assert basis is ValuationBasis.PRICE_TO_SALES
    assert _sub(component, "valuation_multiple").points is not None


@pytest.mark.unit
def test_price_to_sales_is_unavailable_without_a_rate(make: Any) -> None:
    # This is the 0.40x-versus-24.68x case. A USD market cap over TWD revenue
    # reports the most expensive large cap on the board as the cheapest.
    metrics = _foreign(
        make, converted=False, ttm_revenue=100.0 * MILLION * RATE, enterprise_value=None
    )

    component, basis = score_valuation(metrics)

    assert basis is ValuationBasis.NOT_AVAILABLE
    assert _sub(component, "valuation_multiple").points is None


@pytest.mark.unit
def test_fcf_yield_is_unavailable_without_a_rate(make: Any) -> None:
    metrics = _foreign(
        make, converted=False, enterprise_value=None, ttm_free_cash_flow=50.0 * MILLION * RATE
    )

    assert _sub(score_valuation(metrics)[0], "fcf_yield").points is None


@pytest.mark.unit
def test_the_valuation_component_cannot_score_without_a_rate(make: Any) -> None:
    # Every sub-score in the component needs the market capitalisation, so the
    # component reaches no coverage at all rather than scoring on a partial and
    # mismatched picture.
    metrics = _foreign(
        make,
        converted=False,
        enterprise_value=None,
        ttm_revenue=100.0 * MILLION * RATE,
        ttm_free_cash_flow=50.0 * MILLION * RATE,
    )

    component, _ = score_valuation(metrics)

    assert component.status is ComponentStatus.INSUFFICIENT_DATA
    assert component.score is None


# -- risk: leverage ---------------------------------------------------------


@pytest.mark.unit
def test_leverage_is_not_assessed_without_a_rate(make: Any) -> None:
    metrics = _foreign(
        make, converted=False, enterprise_value=None, net_cash=-500.0 * MILLION * RATE
    )

    risk = assess_risk(metrics)

    assert ScoreWarning.LEVERAGE_NOT_ASSESSED in risk.warnings
    assert risk.net_debt_to_market_cap is None


@pytest.mark.unit
def test_leverage_is_assessed_on_the_converted_market_cap(make: Any) -> None:
    metrics = _foreign(make, converted=True, net_cash=-500.0 * MILLION * RATE)

    risk = assess_risk(metrics)

    assert ScoreWarning.LEVERAGE_NOT_ASSESSED not in risk.warnings
    # Net debt of 500M USD-equivalent against 1,000M USD-equivalent of market
    # cap is 0.5 whichever currency both sides are expressed in.
    assert risk.net_debt_to_market_cap == pytest.approx(0.5)


@pytest.mark.unit
def test_an_enterprise_value_without_a_conversion_is_rejected_outright(make: Any) -> None:
    # Not merely absent — impossible. A value here would mean some path had
    # already built the mixed-currency figure this layer exists to prevent, and
    # blanking it would hide the bug rather than surface it.
    with pytest.raises(ValueError, match="cannot come from a market capitalisation"):
        _foreign(make, converted=False, enterprise_value=900.0 * MILLION)
