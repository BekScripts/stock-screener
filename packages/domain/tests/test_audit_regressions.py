"""Regression tests from the Phase 1 audit.

Each test here reproduces a defect found while auditing the implementation
against the specification. They are kept together, rather than scattered into
the topical files, so the class of mistake stays visible: every one of them
produced a *plausible-looking* wrong number rather than an error.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain import (
    CompanyMetrics,
    CompanyProfile,
    EligibilityThresholds,
    EligibilityWarning,
    ExclusionReason,
    PriceBar,
    VolumeBasis,
    average_dollar_volume,
    evaluate_eligibility,
    resolve_liquidity,
)

THRESHOLDS = EligibilityThresholds()


def _metrics(**overrides: object) -> CompanyMetrics:
    """Metrics clearing every threshold unless overridden."""
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


def _profile(**overrides: object) -> CompanyProfile:
    """A listing passing the universe rules unless overridden."""
    fields: dict[str, object] = {"ticker": "XYZ", "name": "Example Corp", "exchange": "NASDAQ"}
    fields.update(overrides)
    return CompanyProfile(**fields)  # type: ignore[arg-type]


# -- CRITICAL: ticker identity ----------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("raw", [" XYZ ", "xyz", "\txyz\n", " xYz"])
def test_ticker_is_normalised_so_whitespace_cannot_split_one_company(raw: str) -> None:
    # The unique constraint is on the stored ticker string. A padded symbol from
    # a provider would otherwise create a second company row, splitting one
    # company's prices and fundamentals across two identities — and giving both
    # of them wrong metrics.
    assert CompanyProfile(ticker=raw, name="Example Corp").ticker == "XYZ"


@pytest.mark.unit
def test_metrics_ticker_is_normalised_too() -> None:
    assert CompanyMetrics(ticker=" xyz ").ticker == "XYZ"


# -- HIGH: average dollar volume formula ------------------------------------


@pytest.mark.unit
def test_average_dollar_volume_multiplies_per_session_not_the_averages() -> None:
    # mean(close * volume) and mean(close) * mean(volume) agree whenever price
    # or volume is constant, which is exactly what the original test used — so
    # it would have passed with the wrong formula. These two sessions are chosen
    # to make the answers differ by 3x.
    bars = [
        PriceBar(date=date(2026, 6, 1), open=10, high=10, low=10, close=10.0, volume=1_000_000),
        PriceBar(date=date(2026, 6, 2), open=90, high=90, low=90, close=90.0, volume=100_000),
    ]

    correct = (10.0 * 1_000_000 + 90.0 * 100_000) / 2
    wrong = ((10.0 + 90.0) / 2) * ((1_000_000 + 100_000) / 2)

    assert average_dollar_volume(bars) == pytest.approx(correct)
    assert average_dollar_volume(bars) != pytest.approx(wrong)


# -- MEDIUM: plausibility of stored observations -----------------------------


@pytest.mark.unit
def test_a_bar_whose_high_is_below_its_low_is_rejected() -> None:
    # Impossible data must fail at the boundary. Accepted, it would set a
    # 52-week high below the 52-week low and make the distance-from-high metric
    # nonsense.
    with pytest.raises(ValueError, match="high"):
        PriceBar(date=date(2026, 6, 1), open=10, high=1.0, low=99.0, close=10, volume=1_000)


@pytest.mark.unit
def test_a_bar_with_negative_volume_is_rejected() -> None:
    with pytest.raises(ValueError, match="volume"):
        PriceBar(date=date(2026, 6, 1), open=10, high=11, low=9, close=10, volume=-500)


@pytest.mark.unit
def test_a_bar_with_a_negative_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        PriceBar(date=date(2026, 6, 1), open=10, high=11, low=-1.0, close=10, volume=500)


@pytest.mark.unit
def test_a_negative_market_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="market_cap"):
        CompanyProfile(ticker="X", name="X", market_cap=-5_000_000.0)


# -- HIGH: currency ----------------------------------------------------------


@pytest.mark.unit
def test_a_foreign_reporter_without_a_rate_is_flagged_rather_than_excluded() -> None:
    # Market cap comes from the market in USD while the statements may be filed
    # in another currency. Dividing one by the other is how Phase 2 would
    # produce an EV/Revenue multiple wrong by the exchange rate — so the ratio
    # is withheld, not the company. Its growth and margins are computed in one
    # currency each and remain perfectly good.
    profile = _profile(reporting_currency="EUR")
    metrics = _metrics().model_copy(update={"reported_currency": "EUR", "quote_currency": "USD"})

    result = evaluate_eligibility(profile, metrics, THRESHOLDS)

    assert result.eligible is True
    assert EligibilityWarning.FX_UNAVAILABLE in result.warnings
    assert metrics.market_cap_for_ratios is None


@pytest.mark.unit
def test_a_foreign_reporter_with_a_rate_carries_no_currency_warning() -> None:
    profile = _profile(reporting_currency="EUR")
    metrics = _metrics().model_copy(
        update={
            "reported_currency": "EUR",
            "quote_currency": "USD",
            "market_cap_reporting_currency": _metrics().market_cap * 0.86453,
        }
    )

    result = evaluate_eligibility(profile, metrics, THRESHOLDS)

    assert result.eligible is True
    assert EligibilityWarning.FX_UNAVAILABLE not in result.warnings


@pytest.mark.unit
@pytest.mark.parametrize("currency", ["USD", "usd", None])
def test_usd_and_unknown_currencies_are_accepted(currency: str | None) -> None:
    # None means the provider did not say. The overwhelming majority of
    # U.S.-listed common stock reports in USD, so an unknown is admitted rather
    # than emptying the universe — the risk is documented instead.
    result = evaluate_eligibility(_profile(reporting_currency=currency), _metrics(), THRESHOLDS)

    assert result.eligible is True


# -- Eligibility boundaries, both sides --------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("price", "eligible"),
    [(2.00, True), (1.99, False), (2.01, True)],
)
def test_price_boundary_is_inclusive(price: float, eligible: bool) -> None:
    assert evaluate_eligibility(_profile(), _metrics(price=price), THRESHOLDS).eligible is eligible


@pytest.mark.unit
@pytest.mark.parametrize(
    ("market_cap", "eligible"),
    [(100_000_000.0, True), (99_999_999.0, False), (100_000_001.0, True)],
)
def test_market_cap_boundary_is_inclusive(market_cap: float, eligible: bool) -> None:
    result = evaluate_eligibility(_profile(), _metrics(market_cap=market_cap), THRESHOLDS)

    assert result.eligible is eligible


@pytest.mark.unit
@pytest.mark.parametrize(
    ("adv", "eligible"),
    [(1_000_000.0, True), (999_999.0, False), (1_000_001.0, True)],
)
def test_average_dollar_volume_boundary_is_inclusive(adv: float, eligible: bool) -> None:
    result = evaluate_eligibility(_profile(), _metrics(average_dollar_volume_20d=adv), THRESHOLDS)

    assert result.eligible is eligible


# -- liquidity basis ---------------------------------------------------------


@pytest.mark.unit
def test_a_consolidated_figure_below_the_threshold_still_excludes() -> None:
    result = evaluate_eligibility(
        _profile(),
        _metrics(average_dollar_volume_20d=500_000.0, liquidity_basis=VolumeBasis.CONSOLIDATED),
        THRESHOLDS,
    )

    assert ExclusionReason.LOW_LIQUIDITY in result.reasons


@pytest.mark.unit
@pytest.mark.parametrize("basis", [VolumeBasis.PARTIAL, VolumeBasis.UNKNOWN])
def test_a_partial_figure_warns_instead_of_excluding(basis: VolumeBasis) -> None:
    # A single-exchange feed carries a few percent of real volume. Applying a
    # whole-market threshold to it would be roughly twenty-five times too
    # strict and would remove most of the small companies this screen exists
    # to surface — so the check is reported, not enforced.
    result = evaluate_eligibility(
        _profile(),
        _metrics(average_dollar_volume_20d=500_000.0, liquidity_basis=basis),
        THRESHOLDS,
    )

    assert result.eligible is True
    assert ExclusionReason.LOW_LIQUIDITY not in result.reasons
    assert EligibilityWarning.LIQUIDITY_UNVERIFIED in result.warnings


@pytest.mark.unit
def test_too_little_history_still_excludes_whatever_the_feed() -> None:
    # Independent of basis: four sessions is not a twenty-day average.
    result = evaluate_eligibility(
        _profile(),
        _metrics(trading_days_used=4, liquidity_basis=VolumeBasis.PARTIAL),
        THRESHOLDS,
    )

    assert ExclusionReason.LOW_LIQUIDITY in result.reasons


@pytest.mark.unit
def test_a_consolidated_average_is_preferred_over_single_exchange_bars() -> None:
    # The provider's whole-market average wins over anything derived from bars,
    # and the result is marked comparable with the threshold.
    profile = CompanyProfile(ticker="XYZ", name="Example", average_volume=1_000_000.0)
    bars = [
        PriceBar(date=date(2026, 6, 1), open=10, high=10, low=10, close=10.0, volume=20_000),
        PriceBar(date=date(2026, 6, 2), open=10, high=10, low=10, close=10.0, volume=20_000),
    ]

    value, basis = resolve_liquidity(profile, 10.0, bars, bar_volume_basis=VolumeBasis.PARTIAL)

    assert value == pytest.approx(10_000_000.0)
    assert basis is VolumeBasis.CONSOLIDATED


@pytest.mark.unit
def test_bars_are_used_when_no_consolidated_average_exists() -> None:
    profile = CompanyProfile(ticker="XYZ", name="Example")
    bars = [
        PriceBar(date=date(2026, 6, 1), open=10, high=10, low=10, close=10.0, volume=20_000),
    ]

    value, basis = resolve_liquidity(profile, 10.0, bars, bar_volume_basis=VolumeBasis.PARTIAL)

    assert value == pytest.approx(200_000.0)
    assert basis is VolumeBasis.PARTIAL


@pytest.mark.unit
def test_liquidity_is_unknown_with_neither_source() -> None:
    value, basis = resolve_liquidity(CompanyProfile(ticker="X", name="X"), None, [])

    assert value is None
    assert basis is VolumeBasis.UNKNOWN
