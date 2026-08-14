"""Tests for the eligibility screen and the universe rules behind it."""

from __future__ import annotations

import pytest

from domain import (
    CompanyMetrics,
    CompanyProfile,
    EligibilityThresholds,
    ExclusionReason,
    VolumeBasis,
    evaluate_eligibility,
    is_common_stock,
    is_supported_exchange,
)

THRESHOLDS = EligibilityThresholds()


def _profile(**overrides: object) -> CompanyProfile:
    """Build a listing that passes the universe rules unless overridden."""
    fields: dict[str, object] = {
        "ticker": "XYZ",
        "name": "Example Corp",
        "exchange": "NASDAQ",
        "is_active": True,
    }
    fields.update(overrides)
    return CompanyProfile(**fields)  # type: ignore[arg-type]


def _metrics(**overrides: object) -> CompanyMetrics:
    """Build metrics that clear every threshold unless overridden."""
    fields: dict[str, object] = {
        "ticker": "XYZ",
        "price": 25.0,
        "market_cap": 1_200_000_000.0,
        "average_dollar_volume_20d": 12_400_000.0,
        "liquidity_basis": VolumeBasis.CONSOLIDATED,
        "trading_days_used": 20,
    }
    fields.update(overrides)
    return CompanyMetrics(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_a_listing_clearing_every_threshold_is_eligible() -> None:
    result = evaluate_eligibility(_profile(), _metrics(), THRESHOLDS)

    assert result.eligible is True
    assert result.reasons == ()


@pytest.mark.unit
def test_a_stock_at_one_dollar_fifty_is_excluded_on_price() -> None:
    result = evaluate_eligibility(_profile(), _metrics(price=1.50), THRESHOLDS)

    assert result.eligible is False
    assert result.reasons == (ExclusionReason.PRICE_BELOW_MINIMUM,)


@pytest.mark.unit
def test_a_ninety_million_market_cap_is_excluded() -> None:
    result = evaluate_eligibility(_profile(), _metrics(market_cap=90_000_000.0), THRESHOLDS)

    assert result.reasons == (ExclusionReason.MARKET_CAP_BELOW_MINIMUM,)


@pytest.mark.unit
def test_five_hundred_thousand_dollars_of_daily_volume_is_excluded() -> None:
    result = evaluate_eligibility(
        _profile(), _metrics(average_dollar_volume_20d=500_000.0), THRESHOLDS
    )

    assert result.reasons == (ExclusionReason.LOW_LIQUIDITY,)


@pytest.mark.unit
def test_a_value_exactly_at_the_threshold_passes() -> None:
    # The specification says "at least", so $2.00 and $100M are in, not out.
    result = evaluate_eligibility(
        _profile(),
        _metrics(price=2.0, market_cap=100_000_000.0, average_dollar_volume_20d=1_000_000.0),
        THRESHOLDS,
    )

    assert result.eligible is True


@pytest.mark.unit
def test_a_recent_listing_is_excluded_for_having_too_little_history() -> None:
    # Four sessions of heavy turnover is not a twenty-day average, however large
    # the number looks.
    result = evaluate_eligibility(
        _profile(),
        _metrics(trading_days_used=4, average_dollar_volume_20d=50_000_000.0),
        THRESHOLDS,
    )

    assert result.reasons == (ExclusionReason.LOW_LIQUIDITY,)


@pytest.mark.unit
def test_a_delisted_company_is_excluded_as_inactive() -> None:
    result = evaluate_eligibility(_profile(is_active=False), _metrics(), THRESHOLDS)

    assert result.reasons == (ExclusionReason.INACTIVE,)


@pytest.mark.unit
def test_an_etf_is_excluded_as_an_unsupported_security_type() -> None:
    profile = _profile(ticker="BRDX", name="Broad Market Index ETF", exchange="ARCA")

    result = evaluate_eligibility(profile, _metrics(), THRESHOLDS)

    assert result.reasons == (ExclusionReason.UNSUPPORTED_SECURITY_TYPE,)


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing",
    [{"price": None}, {"market_cap": None}],
    ids=["no-price", "no-market-cap"],
)
def test_a_company_missing_required_data_is_excluded_rather_than_assumed(
    missing: dict[str, None],
) -> None:
    result = evaluate_eligibility(_profile(), _metrics(**missing), THRESHOLDS)

    assert ExclusionReason.MISSING_REQUIRED_DATA in result.reasons


@pytest.mark.unit
def test_a_missing_market_cap_does_not_also_trigger_the_minimum_threshold() -> None:
    # "We do not know the market cap" and "the market cap is too small" are
    # different findings, and only the first one is true here.
    result = evaluate_eligibility(_profile(), _metrics(market_cap=None), THRESHOLDS)

    assert ExclusionReason.MARKET_CAP_BELOW_MINIMUM not in result.reasons


@pytest.mark.unit
def test_every_failed_check_is_reported_not_just_the_first() -> None:
    profile = _profile(name="Tiny Warrants Corp", exchange="OTC", is_active=False)
    metrics = _metrics(price=0.4, market_cap=1_000_000.0, average_dollar_volume_20d=100.0)

    result = evaluate_eligibility(profile, metrics, THRESHOLDS)

    assert set(result.reasons) == {
        ExclusionReason.INACTIVE,
        ExclusionReason.UNSUPPORTED_SECURITY_TYPE,
        ExclusionReason.PRICE_BELOW_MINIMUM,
        ExclusionReason.MARKET_CAP_BELOW_MINIMUM,
        ExclusionReason.LOW_LIQUIDITY,
    }


@pytest.mark.unit
def test_thresholds_come_from_configuration_not_from_constants() -> None:
    # A $5 minimum must exclude a $3 stock that the default $2 minimum admits.
    strict = EligibilityThresholds(min_price=5.0)

    assert evaluate_eligibility(_profile(), _metrics(price=3.0), THRESHOLDS).eligible is True
    assert evaluate_eligibility(_profile(), _metrics(price=3.0), strict).eligible is False


@pytest.mark.unit
@pytest.mark.parametrize("exchange", ["NASDAQ", "NYSE", "AMEX", "nyse american"])
def test_the_three_target_exchanges_are_supported(exchange: str) -> None:
    assert is_supported_exchange(exchange) is True


@pytest.mark.unit
@pytest.mark.parametrize("exchange", ["ARCA", "OTC", "LSE", None])
def test_other_venues_are_not_supported(exchange: str | None) -> None:
    assert is_supported_exchange(exchange) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("ACME.WS", "Acme Corp Warrant"),
        ("ACME.U", "Acme Acquisition Corp Units"),
        ("ACME.R", "Acme Corp Rights"),
        ("ACME-P", "Acme Corp Preferred Series A"),
        ("SPY", "SPDR S&P 500 ETF Trust"),
        ("VBND", "Vanguard Total Bond Index Fund"),
    ],
)
def test_non_common_lines_are_rejected(ticker: str, name: str) -> None:
    assert is_common_stock(ticker, name) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("AAPL", "Apple Inc"),
        ("BRK.B", "Berkshire Hathaway Inc Class B"),
        ("NVEX", "Novexa Systems Inc"),
        ("TFC", "Truist Financial Corporation"),
    ],
)
def test_ordinary_common_stock_is_accepted(ticker: str, name: str) -> None:
    assert is_common_stock(ticker, name) is True
